#!/usr/bin/env python3
"""Versioned machine state for resumable project-understanding runs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping, Sequence
import uuid

if __package__:
    from .advisory_lock import (
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
    from .project_inventory import PROJECT_MANIFEST_VERSION, load_project_manifest
    from .project_layout import (
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        load_versioned_json,
        write_versioned_json,
    )
    from .project_registry import load_registered_project
else:
    from advisory_lock import (  # type: ignore[no-redef]
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
    from project_inventory import (  # type: ignore[no-redef]
        PROJECT_MANIFEST_VERSION,
        load_project_manifest,
    )
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        load_versioned_json,
        write_versioned_json,
    )
    from project_registry import load_registered_project  # type: ignore[no-redef]


PROJECT_RUN_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
PROJECT_RUN_KIND = "llmwiki-project-run"
PROJECT_RUN_VERSION = "project-run-v1"
PROJECT_RUN_RESULT_KIND = "llmwiki-project-run-result"
PROJECT_RUN_OPERATION = "project-understand"
PROJECT_UNDERSTAND_STAGES = (
    "register",
    "inventory",
    "classify",
    "extract",
    "adaptive-read",
    "synthesize",
    "evidence",
    "status",
    "plan",
    "index",
    "web-render",
)
RUN_STATUSES = frozenset(
    {"pending", "running", "paused", "partial", "succeeded", "failed"}
)
STAGE_STATUSES = frozenset(
    {"pending", "running", "succeeded", "failed", "unavailable"}
)
TERMINAL_RUN_STATUSES = frozenset({"partial", "succeeded", "failed"})
_RUN_ID_RE = re.compile(r"^run-[0-9]{8}t[0-9]{12}z-[a-f0-9]{12}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"\.[0-9]{6}Z$"
)
_RUN_FIELDS = {
    "schema_version",
    "kind",
    "run_version",
    "run_id",
    "project_id",
    "operation",
    "revision",
    "status",
    "created_at",
    "updated_at",
    "started_at",
    "completed_at",
    "duration_ms",
    "active_stage",
    "through_stage",
    "input_snapshot",
    "usage",
    "stages",
    "artifacts",
    "errors",
}
_STAGE_FIELDS = {
    "stage_id",
    "ordinal",
    "status",
    "attempts",
    "last_error",
    "artifacts",
}
_ATTEMPT_FIELDS = {
    "attempt",
    "status",
    "started_at",
    "completed_at",
    "duration_ms",
    "input_versions",
    "provider",
    "model",
    "token_usage",
    "estimated_cost",
    "error",
    "artifacts",
}
_ERROR_FIELDS = {"stage_id", "attempt", "reason_code", "message", "retryable"}
_ARTIFACT_FIELDS = {
    "artifact_type",
    "artifact_id",
    "relative_path",
    "content_hash",
}


class ProjectRunError(LayoutError):
    """Base error for project-run records."""

    reason_code = "project-run-invalid"


class ProjectRunNotFoundError(ProjectRunError):
    reason_code = "project-run-not-found"


class ProjectRunConflictError(ProjectRunError):
    reason_code = "project-run-conflict"


class ProjectRunStateError(ProjectRunError):
    reason_code = "project-run-state-invalid"


def utc_timestamp(value: datetime | None = None) -> str:
    moment = value or datetime.now(timezone.utc)
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ProjectRunError("run clock must return a timezone-aware datetime")
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise ProjectRunStateError("run timestamp must be canonical UTC text")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProjectRunStateError("run timestamp is invalid") from exc


def elapsed_ms(started_at: str, completed_at: str) -> int:
    delta = parse_timestamp(completed_at) - parse_timestamp(started_at)
    if delta.total_seconds() < 0:
        raise ProjectRunStateError("completion timestamp precedes start timestamp")
    return int(delta.total_seconds() * 1000)


def generate_run_id(value: datetime | None = None) -> str:
    timestamp = utc_timestamp(value)
    compact = (
        timestamp.replace("-", "")
        .replace(":", "")
        .replace(".", "")
        .replace("T", "t")
        .replace("Z", "z")
    )
    return f"run-{compact}-{uuid.uuid4().hex[:12]}"


def validate_run_id(value: object) -> str:
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        raise ProjectRunError(
            "run_id must use run-YYYYMMDDtHHMMSSffffffz- plus 12 lowercase hex"
        )
    return value


def validate_stage_id(value: object) -> str:
    if not isinstance(value, str) or value not in PROJECT_UNDERSTAND_STAGES:
        raise ProjectRunError("stage is not part of the project-understand pipeline")
    return value


def _mapping(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectRunStateError(f"{label} must be an object")
    if set(value) != fields:
        raise ProjectRunStateError(f"{label} fields are not the closed Schema v1 set")
    return value


def _text(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ProjectRunStateError(f"{label} must be non-empty text")
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProjectRunStateError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectRunStateError(f"{label} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ProjectRunStateError(f"{label} must be a finite non-negative number")
    return result


def _reason(value: object) -> str:
    text = _text(value, "reason_code")
    assert isinstance(text, str)
    if not _REASON_RE.fullmatch(text):
        raise ProjectRunStateError("reason_code must be stable kebab-case")
    return text


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProjectRunStateError(f"{label} must be an array")
    return value


def _relative_path(value: object) -> str | None:
    text = _text(value, "artifact relative_path", optional=True)
    if text is None:
        return None
    if "\\" in text:
        raise ProjectRunStateError("artifact path must use POSIX separators")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProjectRunStateError("artifact path must be normalized and relative")
    return path.as_posix()


def validate_artifact(value: object) -> dict[str, Any]:
    record = _mapping(value, _ARTIFACT_FIELDS, "artifact")
    artifact_type = _text(record["artifact_type"], "artifact_type")
    artifact_id = _text(record["artifact_id"], "artifact_id")
    relative_path = _relative_path(record["relative_path"])
    content_hash = _text(record["content_hash"], "content_hash", optional=True)
    if content_hash is not None and not _SHA256_RE.fullmatch(content_hash):
        raise ProjectRunStateError("artifact content_hash must be lowercase SHA-256")
    if relative_path is None and content_hash is None:
        raise ProjectRunStateError("artifact needs a relative path or content hash")
    return {
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "relative_path": relative_path,
        "content_hash": content_hash,
    }


def validate_error(value: object) -> dict[str, Any]:
    record = _mapping(value, _ERROR_FIELDS, "run error")
    retryable = record["retryable"]
    if not isinstance(retryable, bool):
        raise ProjectRunStateError("retryable must be a boolean")
    return {
        "stage_id": validate_stage_id(record["stage_id"]),
        "attempt": _integer(record["attempt"], "error attempt", 1),
        "reason_code": _reason(record["reason_code"]),
        "message": _text(record["message"], "error message"),
        "retryable": retryable,
    }


def validate_versions(value: object) -> dict[str, str | int | None]:
    if not isinstance(value, dict):
        raise ProjectRunStateError("input_versions must be an object")
    result: dict[str, str | int | None] = {}
    for key in sorted(value):
        if not isinstance(key, str) or not key:
            raise ProjectRunStateError("input version keys must be non-empty text")
        item = value[key]
        if item is not None and (isinstance(item, bool) or not isinstance(item, (str, int))):
            raise ProjectRunStateError("input version values must be text, integer, or null")
        if item == "":
            raise ProjectRunStateError("input version text must be non-empty")
        result[key] = item
    return result


@dataclass(frozen=True)
class StageOutcome:
    """Closed result returned by one stage handler."""

    status: str
    reason_code: str | None = None
    message: str | None = None
    retryable: bool = True
    input_versions: Mapping[str, str | int | None] | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0
    estimated_cost_usd: float = 0.0
    artifacts: Sequence[Mapping[str, Any]] = ()

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "unavailable"}:
            raise ProjectRunError("stage outcome status is unsupported")
        if self.status in {"failed", "unavailable"}:
            if self.reason_code is None or self.message is None:
                raise ProjectRunError("non-success outcome requires reason and message")
            _reason(self.reason_code)
            _text(self.message, "stage message")
        elif self.reason_code is not None or self.message is not None:
            raise ProjectRunError("successful outcome cannot carry an error reason")
        if not isinstance(self.retryable, bool):
            raise ProjectRunError("retryable must be a boolean")
        validate_versions(dict(self.input_versions or {}))
        _integer(self.input_tokens, "input_tokens")
        _integer(self.output_tokens, "output_tokens")
        _integer(self.cache_hits, "cache_hits")
        _number(self.estimated_cost_usd, "estimated_cost_usd")
        for artifact in self.artifacts:
            validate_artifact(dict(artifact))

    @classmethod
    def succeeded(cls, **kwargs: Any) -> StageOutcome:
        return cls(status="succeeded", **kwargs)

    @classmethod
    def failed(
        cls,
        reason_code: str,
        message: str,
        *,
        retryable: bool = True,
        input_versions: Mapping[str, str | int | None] | None = None,
        provider: str | None = None,
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_hits: int = 0,
        estimated_cost_usd: float = 0.0,
        artifacts: Sequence[Mapping[str, Any]] = (),
    ) -> StageOutcome:
        return cls(
            status="failed",
            reason_code=reason_code,
            message=message,
            retryable=retryable,
            input_versions=input_versions,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hits=cache_hits,
            estimated_cost_usd=estimated_cost_usd,
            artifacts=artifacts,
        )

    @classmethod
    def unavailable(
        cls,
        *,
        reason_code: str = "stage-handler-unavailable",
        message: str = "This project-understanding stage is not implemented yet.",
        input_versions: Mapping[str, str | int | None] | None = None,
    ) -> StageOutcome:
        return cls(
            status="unavailable",
            reason_code=reason_code,
            message=message,
            input_versions=input_versions,
        )


def _validate_attempt(value: object, stage_id: str, number: int) -> dict[str, Any]:
    record = _mapping(value, _ATTEMPT_FIELDS, "stage attempt")
    if _integer(record["attempt"], "attempt", 1) != number:
        raise ProjectRunStateError("attempt numbers must be contiguous")
    status = _text(record["status"], "attempt status")
    if status not in {"running", "succeeded", "failed", "unavailable"}:
        raise ProjectRunStateError("attempt status is unsupported")
    started_at = _text(record["started_at"], "attempt started_at")
    assert isinstance(started_at, str)
    parse_timestamp(started_at)
    completed_at = _text(record["completed_at"], "attempt completed_at", optional=True)
    duration = record["duration_ms"]
    if status == "running":
        if completed_at is not None or duration is not None:
            raise ProjectRunStateError("running attempt cannot be completed")
    else:
        if completed_at is None:
            raise ProjectRunStateError("terminal attempt requires completed_at")
        if _integer(duration, "attempt duration_ms") != elapsed_ms(started_at, completed_at):
            raise ProjectRunStateError("attempt duration does not match timestamps")
    raw_usage = record["token_usage"]
    if not isinstance(raw_usage, dict) or set(raw_usage) != {
        "input_tokens",
        "output_tokens",
        "cache_hits",
    }:
        raise ProjectRunStateError("token_usage fields are invalid")
    token_usage = {
        key: _integer(raw_usage[key], f"token_usage {key}")
        for key in ("input_tokens", "output_tokens", "cache_hits")
    }
    raw_cost = record["estimated_cost"]
    if not isinstance(raw_cost, dict) or set(raw_cost) != {"currency", "amount"}:
        raise ProjectRunStateError("estimated_cost fields are invalid")
    if raw_cost["currency"] != "USD":
        raise ProjectRunStateError("estimated_cost currency must be USD")
    cost = {"currency": "USD", "amount": _number(raw_cost["amount"], "cost")}
    raw_error = record["error"]
    error = None if raw_error is None else validate_error(raw_error)
    if status in {"failed", "unavailable"}:
        if error is None or error["stage_id"] != stage_id or error["attempt"] != number:
            raise ProjectRunStateError(
                "failed or unavailable attempt requires its matching error"
            )
    elif error is not None:
        raise ProjectRunStateError(
            "only failed or unavailable attempts may carry errors"
        )
    provider = _text(record["provider"], "provider", optional=True)
    model = _text(record["model"], "model", optional=True)
    artifacts = [validate_artifact(item) for item in _list(record["artifacts"], "artifacts")]
    return {
        "attempt": number,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": duration,
        "input_versions": validate_versions(record["input_versions"]),
        "provider": provider,
        "model": model,
        "token_usage": token_usage,
        "estimated_cost": cost,
        "error": error,
        "artifacts": artifacts,
    }


def _validate_stage(value: object, expected_id: str, ordinal: int) -> dict[str, Any]:
    record = _mapping(value, _STAGE_FIELDS, "run stage")
    if record["stage_id"] != expected_id or record["ordinal"] != ordinal:
        raise ProjectRunStateError("run stages must use canonical order and ordinals")
    status = _text(record["status"], "stage status")
    if status not in STAGE_STATUSES:
        raise ProjectRunStateError("stage status is unsupported")
    attempts = [
        _validate_attempt(item, expected_id, number)
        for number, item in enumerate(_list(record["attempts"], "attempts"), start=1)
    ]
    if status == "pending" and attempts:
        raise ProjectRunStateError("pending stage cannot have attempts")
    if status != "pending" and (not attempts or attempts[-1]["status"] != status):
        raise ProjectRunStateError("stage status must match its latest attempt")
    raw_error = record["last_error"]
    last_error = None if raw_error is None else validate_error(raw_error)
    expected_error = attempts[-1]["error"] if attempts else None
    if last_error != expected_error:
        raise ProjectRunStateError("stage last_error must match latest attempt")
    artifacts = [validate_artifact(item) for item in _list(record["artifacts"], "artifacts")]
    expected_artifacts = attempts[-1]["artifacts"] if attempts else []
    if artifacts != expected_artifacts:
        raise ProjectRunStateError("stage artifacts must match latest attempt")
    return {
        "stage_id": expected_id,
        "ordinal": ordinal,
        "status": status,
        "attempts": attempts,
        "last_error": last_error,
        "artifacts": artifacts,
    }


def _validate_snapshot(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"project_schema_version", "manifest"}:
        raise ProjectRunStateError("input_snapshot fields are invalid")
    project_version = _integer(value["project_schema_version"], "project schema", 1)
    raw_manifest = value["manifest"]
    manifest = None
    if raw_manifest is not None:
        if not isinstance(raw_manifest, dict) or set(raw_manifest) != {
            "manifest_version",
            "scan_generation",
        }:
            raise ProjectRunStateError("manifest snapshot fields are invalid")
        manifest = {
            "manifest_version": _text(raw_manifest["manifest_version"], "manifest version"),
            "scan_generation": _integer(raw_manifest["scan_generation"], "scan generation", 1),
        }
    return {"project_schema_version": project_version, "manifest": manifest}


def _validate_usage(value: object) -> dict[str, Any]:
    expected = {
        "providers",
        "models",
        "input_tokens",
        "output_tokens",
        "cache_hits",
        "estimated_cost",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ProjectRunStateError("usage fields are invalid")
    providers = _list(value["providers"], "providers")
    models = _list(value["models"], "models")
    if providers != sorted(set(providers)) or any(not isinstance(item, str) or not item for item in providers):
        raise ProjectRunStateError("providers must be unique sorted text")
    if models != sorted(set(models)) or any(not isinstance(item, str) or not item for item in models):
        raise ProjectRunStateError("models must be unique sorted text")
    raw_cost = value["estimated_cost"]
    if not isinstance(raw_cost, dict) or set(raw_cost) != {"currency", "amount"} or raw_cost["currency"] != "USD":
        raise ProjectRunStateError("usage estimated_cost is invalid")
    return {
        "providers": list(providers),
        "models": list(models),
        "input_tokens": _integer(value["input_tokens"], "input tokens"),
        "output_tokens": _integer(value["output_tokens"], "output tokens"),
        "cache_hits": _integer(value["cache_hits"], "cache hits"),
        "estimated_cost": {"currency": "USD", "amount": _number(raw_cost["amount"], "cost")},
    }


def aggregate_attempt_ledger(
    stages: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Derive aggregate usage, artifacts, and errors from the attempt ledger."""

    providers: set[str] = set()
    models: set[str] = set()
    input_tokens = 0
    output_tokens = 0
    cache_hits = 0
    costs: list[float] = []
    artifacts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for stage in stages:
        attempts = stage.get("attempts", ())
        if not isinstance(attempts, Sequence):
            raise ProjectRunStateError("stage attempts are invalid")
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                raise ProjectRunStateError("stage attempt is invalid")
            provider = attempt.get("provider")
            model = attempt.get("model")
            if isinstance(provider, str):
                providers.add(provider)
            if isinstance(model, str):
                models.add(model)
            token_usage = attempt.get("token_usage")
            estimated_cost = attempt.get("estimated_cost")
            if not isinstance(token_usage, Mapping) or not isinstance(
                estimated_cost, Mapping
            ):
                raise ProjectRunStateError("attempt usage is invalid")
            input_tokens += int(token_usage["input_tokens"])
            output_tokens += int(token_usage["output_tokens"])
            cache_hits += int(token_usage["cache_hits"])
            costs.append(float(estimated_cost["amount"]))
            attempt_artifacts = attempt.get("artifacts")
            if not isinstance(attempt_artifacts, Sequence):
                raise ProjectRunStateError("attempt artifacts are invalid")
            artifacts.extend(deepcopy(list(attempt_artifacts)))
            error = attempt.get("error")
            if error is not None:
                if not isinstance(error, Mapping):
                    raise ProjectRunStateError("attempt error is invalid")
                errors.append(deepcopy(dict(error)))
    return (
        {
            "providers": sorted(providers),
            "models": sorted(models),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_hits": cache_hits,
            "estimated_cost": {
                "currency": "USD",
                "amount": math.fsum(costs),
            },
        },
        artifacts,
        errors,
    )


def validate_project_run_record(
    value: object,
    *,
    expected_project_id: str | None = None,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    """Validate and copy one closed current-schema run report."""

    record = _mapping(value, _RUN_FIELDS, "project run")
    if record["schema_version"] != PROJECT_RUN_SCHEMA_VERSION:
        raise ProjectRunStateError("project run schema_version is unsupported")
    if record["kind"] != PROJECT_RUN_KIND or record["run_version"] != PROJECT_RUN_VERSION:
        raise ProjectRunStateError("project run kind or version is unsupported")
    run_id = validate_run_id(record["run_id"])
    project_id = _text(record["project_id"], "project_id")
    assert isinstance(project_id, str)
    if expected_project_id is not None and project_id != expected_project_id:
        raise ProjectRunStateError("project_id does not match requested run")
    if expected_run_id is not None and run_id != expected_run_id:
        raise ProjectRunStateError("run_id does not match requested run")
    if record["operation"] != PROJECT_RUN_OPERATION:
        raise ProjectRunStateError("run operation is unsupported")
    revision = _integer(record["revision"], "revision", 1)
    status = _text(record["status"], "run status")
    if status not in RUN_STATUSES:
        raise ProjectRunStateError("run status is unsupported")
    created_at = _text(record["created_at"], "created_at")
    updated_at = _text(record["updated_at"], "updated_at")
    started_at = _text(record["started_at"], "started_at", optional=True)
    completed_at = _text(record["completed_at"], "completed_at", optional=True)
    assert isinstance(created_at, str) and isinstance(updated_at, str)
    created_moment = parse_timestamp(created_at)
    updated_moment = parse_timestamp(updated_at)
    if updated_moment < created_moment:
        raise ProjectRunStateError("updated_at precedes created_at")
    started_moment = None if started_at is None else parse_timestamp(started_at)
    completed_moment = (
        None if completed_at is None else parse_timestamp(completed_at)
    )
    if started_moment is not None and started_moment < created_moment:
        raise ProjectRunStateError("started_at precedes created_at")
    if completed_moment is not None and completed_moment > updated_moment:
        raise ProjectRunStateError("completed_at follows updated_at")
    if completed_at is None:
        if record["duration_ms"] is not None:
            raise ProjectRunStateError("incomplete run cannot have duration")
    else:
        if started_at is None or _integer(record["duration_ms"], "duration_ms") != elapsed_ms(started_at, completed_at):
            raise ProjectRunStateError("run duration does not match timestamps")
    active_stage = record["active_stage"]
    if active_stage is not None:
        active_stage = validate_stage_id(active_stage)
    through_stage = record["through_stage"]
    if through_stage is not None:
        through_stage = validate_stage_id(through_stage)
    raw_stages = _list(record["stages"], "stages")
    if len(raw_stages) != len(PROJECT_UNDERSTAND_STAGES):
        raise ProjectRunStateError("run must contain every canonical stage")
    stages = [
        _validate_stage(item, stage_id, ordinal)
        for ordinal, (stage_id, item) in enumerate(zip(PROJECT_UNDERSTAND_STAGES, raw_stages, strict=True))
    ]
    running = [stage["stage_id"] for stage in stages if stage["status"] == "running"]
    if status == "running":
        if running != [active_stage]:
            raise ProjectRunStateError("running run must have one matching active stage")
    elif running or active_stage is not None:
        raise ProjectRunStateError("non-running run cannot have active stage")
    if status == "succeeded" and any(stage["status"] != "succeeded" for stage in stages):
        raise ProjectRunStateError("succeeded run requires every stage to succeed")
    if status == "failed" and not any(stage["status"] == "failed" for stage in stages):
        raise ProjectRunStateError("failed run requires a failed stage")
    if status == "partial":
        if not any(stage["status"] == "unavailable" for stage in stages):
            raise ProjectRunStateError("partial run requires an unavailable stage")
        if any(stage["status"] not in {"succeeded", "unavailable"} for stage in stages):
            raise ProjectRunStateError(
                "partial run requires every stage to be terminal"
            )
    if status == "paused" and not any(stage["status"] == "pending" for stage in stages):
        raise ProjectRunStateError("paused run requires pending stages")
    if any(stage["attempts"] for stage in stages) and started_at is None:
        raise ProjectRunStateError("attempted run requires started_at")
    if status in TERMINAL_RUN_STATUSES and completed_at is None:
        raise ProjectRunStateError("terminal run requires completed_at")
    if status not in TERMINAL_RUN_STATUSES and completed_at is not None:
        raise ProjectRunStateError("non-terminal run cannot have completed_at")
    usage = _validate_usage(record["usage"])
    artifacts = [
        validate_artifact(item) for item in _list(record["artifacts"], "artifacts")
    ]
    errors = [validate_error(item) for item in _list(record["errors"], "errors")]
    expected_usage, expected_artifacts, expected_errors = aggregate_attempt_ledger(
        stages
    )
    if usage != expected_usage:
        raise ProjectRunStateError("run usage must match the attempt ledger")
    if artifacts != expected_artifacts:
        raise ProjectRunStateError("run artifacts must match the attempt ledger")
    if errors != expected_errors:
        raise ProjectRunStateError("run errors must match unsuccessful attempts")
    return deepcopy(
        {
            **record,
            "revision": revision,
            "status": status,
            "created_at": created_at,
            "updated_at": updated_at,
            "started_at": started_at,
            "completed_at": completed_at,
            "active_stage": active_stage,
            "through_stage": through_stage,
            "input_snapshot": _validate_snapshot(record["input_snapshot"]),
            "usage": usage,
            "stages": stages,
            "artifacts": artifacts,
            "errors": errors,
        }
    )


def empty_usage() -> dict[str, Any]:
    return {
        "providers": [],
        "models": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_hits": 0,
        "estimated_cost": {"currency": "USD", "amount": 0.0},
    }


def refresh_run_ledger(record: dict[str, Any]) -> None:
    """Refresh run-level aggregates in place from all persisted attempts."""

    stages = record.get("stages")
    if not isinstance(stages, list):
        raise ProjectRunStateError("run stages are invalid")
    usage, artifacts, errors = aggregate_attempt_ledger(stages)
    record["usage"] = usage
    record["artifacts"] = artifacts
    record["errors"] = errors


def capture_input_snapshot(workspace_root: str | Path, project_id: str) -> dict[str, Any]:
    """Capture machine-state versions without reading research-source content."""

    registration = load_registered_project(workspace_root, project_id)
    schema_version = registration.record.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
        raise ProjectRunStateError("registered project schema_version is invalid")
    manifest_snapshot = None
    if registration.layout.manifest_file.exists():
        manifest = load_project_manifest(
            registration.layout.manifest_file,
            project_id=registration.project_id,
            project_root=registration.project_root,
            required_manifest_version=PROJECT_MANIFEST_VERSION,
        )
        manifest_snapshot = {
            "manifest_version": manifest.manifest_version,
            "scan_generation": manifest.scan_generation,
        }
    return {
        "project_schema_version": schema_version,
        "manifest": manifest_snapshot,
    }


def project_run_file(workspace_root: str | Path, project_id: str, run_id: str) -> Path:
    run_id = validate_run_id(run_id)
    registration = load_registered_project(workspace_root, project_id)
    root = registration.layout.runs_dir.resolve()
    target = (root / run_id / "run.json").resolve()
    if root not in target.parents:
        raise ProjectRunError("run path escaped the registered runs directory")
    return target


def new_project_run_record(
    workspace_root: str | Path,
    project_id: str,
    *,
    run_id: str,
    created_at: str,
    through_stage: str | None = None,
) -> dict[str, Any]:
    validate_run_id(run_id)
    parse_timestamp(created_at)
    if through_stage is not None:
        validate_stage_id(through_stage)
    registration = load_registered_project(workspace_root, project_id)
    record = {
        "schema_version": PROJECT_RUN_SCHEMA_VERSION,
        "kind": PROJECT_RUN_KIND,
        "run_version": PROJECT_RUN_VERSION,
        "run_id": run_id,
        "project_id": registration.project_id,
        "operation": PROJECT_RUN_OPERATION,
        "revision": 1,
        "status": "pending",
        "created_at": created_at,
        "updated_at": created_at,
        "started_at": None,
        "completed_at": None,
        "duration_ms": None,
        "active_stage": None,
        "through_stage": through_stage,
        "input_snapshot": capture_input_snapshot(workspace_root, project_id),
        "usage": empty_usage(),
        "stages": [
            {
                "stage_id": stage_id,
                "ordinal": ordinal,
                "status": "pending",
                "attempts": [],
                "last_error": None,
                "artifacts": [],
            }
            for ordinal, stage_id in enumerate(PROJECT_UNDERSTAND_STAGES)
        ],
        "artifacts": [],
        "errors": [],
    }
    return validate_project_run_record(
        record,
        expected_project_id=registration.project_id,
        expected_run_id=run_id,
    )


def _acquire_machine_state_lock(
    workspace_root: str | Path,
    project_id: str,
    *,
    timeout_seconds: float,
) -> AdvisoryFileLock:
    registration = load_registered_project(workspace_root, project_id)
    lock = AdvisoryFileLock(
        registration.layout.machine_state_lock_file,
        timeout_seconds=timeout_seconds,
    )
    try:
        return lock.acquire()
    except AdvisoryLockTimeoutError as exc:
        raise ProjectRunError(
            "timed out waiting for the project machine-state lock"
        ) from exc
    except AdvisoryLockError as exc:
        raise ProjectRunError(
            "could not acquire the project machine-state lock"
        ) from exc


def create_project_run(
    workspace_root: str | Path,
    project_id: str,
    *,
    run_id: str,
    created_at: str,
    through_stage: str | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> ProjectRunResult:
    lock = _acquire_machine_state_lock(
        workspace_root,
        project_id,
        timeout_seconds=lock_timeout_seconds,
    )
    try:
        record = new_project_run_record(
            workspace_root,
            project_id,
            run_id=run_id,
            created_at=created_at,
            through_stage=through_stage,
        )
        run_file = project_run_file(workspace_root, project_id, run_id)
        try:
            run_file.parent.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise ProjectRunConflictError(
                f"project run {run_id!r} already exists"
            ) from exc
        write_versioned_json(run_file, record)
        return ProjectRunResult(run_file, record)
    finally:
        lock.release()


def load_project_run(
    workspace_root: str | Path,
    project_id: str,
    run_id: str,
) -> ProjectRunResult:
    run_file = project_run_file(workspace_root, project_id, run_id)
    if not run_file.is_file():
        raise ProjectRunNotFoundError(f"project run {run_id!r} does not exist")
    document = load_versioned_json(run_file, allow_legacy=True)
    if document.is_legacy:
        raise ProjectRunStateError(
            "legacy project-run data is read-only and has no E-01 compatibility mapping"
        )
    record = validate_project_run_record(
        document.data,
        expected_project_id=project_id,
        expected_run_id=run_id,
    )
    return ProjectRunResult(run_file, record)


def save_project_run(
    workspace_root: str | Path,
    project_id: str,
    run_id: str,
    record: Mapping[str, Any],
    *,
    expected_revision: int,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> ProjectRunResult:
    lock = _acquire_machine_state_lock(
        workspace_root,
        project_id,
        timeout_seconds=lock_timeout_seconds,
    )
    try:
        current = load_project_run(workspace_root, project_id, run_id)
        if current.record["revision"] != expected_revision:
            raise ProjectRunConflictError("project run changed; reload before writing")
        updated = deepcopy(dict(record))
        if updated.get("revision") != expected_revision:
            raise ProjectRunStateError("caller run revision is inconsistent")
        updated["revision"] = expected_revision + 1
        validated = validate_project_run_record(
            updated,
            expected_project_id=project_id,
            expected_run_id=run_id,
        )
        write_versioned_json(current.run_file, validated)
        return ProjectRunResult(current.run_file, validated)
    finally:
        lock.release()


def stage_by_id(record: Mapping[str, Any], stage_id: str) -> dict[str, Any]:
    validate_stage_id(stage_id)
    stages = record.get("stages")
    if not isinstance(stages, list):
        raise ProjectRunStateError("run stages are invalid")
    for stage in stages:
        if isinstance(stage, dict) and stage.get("stage_id") == stage_id:
            return stage
    raise ProjectRunStateError(f"run is missing stage {stage_id!r}")


@dataclass(frozen=True)
class ProjectRunResult:
    """Local, path-bearing Core result for one persisted run."""

    run_file: Path
    record: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_file", Path(self.run_file).expanduser().resolve())
        object.__setattr__(self, "record", validate_project_run_record(self.record))

    @property
    def project_id(self) -> str:
        return self.record["project_id"]

    @property
    def run_id(self) -> str:
        return self.record["run_id"]

    @property
    def status(self) -> str:
        return self.record["status"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECT_RUN_SCHEMA_VERSION,
            "kind": PROJECT_RUN_RESULT_KIND,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "run_file": str(self.run_file),
            "run": deepcopy(self.record),
        }
