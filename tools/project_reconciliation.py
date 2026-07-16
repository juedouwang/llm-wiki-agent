#!/usr/bin/env python3
"""Conservative H-07 project reconciliation with a full-scan fallback.

Host events and explicit dirty paths are untrusted latency hints. Correctness comes
from a complete E-01 run through ``classify`` and from acknowledging only the
exact event-ledger prefix snapshotted before that run began.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

if __package__:
    from .advisory_lock import (
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
    from .coverage_report import (
        COVERAGE_REPORT_KIND,
        COVERAGE_REPORT_SCHEMA_VERSION,
        COVERAGE_REPORT_VERSION,
        CoverageReportResult,
        build_coverage_report,
    )
    from .host_events import (
        HostEventError,
        HostEventLedgerSnapshot,
        locked_host_event_ledger,
        normalize_dirty_path,
        snapshot_host_event_state,
    )
    from .project_inventory import PROJECT_MANIFEST_VERSION, load_project_manifest
    from .project_layout import (
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        UnsupportedSchemaVersionError,
        load_versioned_json,
        parse_json_bytes_strict,
        parse_versioned_json_bytes,
    )
    from .project_registry import load_registered_project
    from .project_runs import PROJECT_UNDERSTAND_STAGES, ProjectRunResult
else:
    from advisory_lock import (  # type: ignore[no-redef]
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
    from coverage_report import (  # type: ignore[no-redef]
        COVERAGE_REPORT_KIND,
        COVERAGE_REPORT_SCHEMA_VERSION,
        COVERAGE_REPORT_VERSION,
        CoverageReportResult,
        build_coverage_report,
    )
    from host_events import (  # type: ignore[no-redef]
        HostEventError,
        HostEventLedgerSnapshot,
        locked_host_event_ledger,
        normalize_dirty_path,
        snapshot_host_event_state,
    )
    from project_inventory import (  # type: ignore[no-redef]
        PROJECT_MANIFEST_VERSION,
        load_project_manifest,
    )
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        UnsupportedSchemaVersionError,
        load_versioned_json,
        parse_json_bytes_strict,
        parse_versioned_json_bytes,
    )
    from project_registry import load_registered_project  # type: ignore[no-redef]
    from project_runs import (  # type: ignore[no-redef]
        PROJECT_UNDERSTAND_STAGES,
        ProjectRunResult,
    )


RECONCILIATION_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
RECONCILIATION_STATE_KIND = "llmwiki-reconciliation-state"
RECONCILIATION_RESULT_KIND = "llmwiki-project-reconciliation-result"
RECONCILIATION_VERSION = "project-reconciliation-v1"
RECONCILIATION_STATE_RELATIVE_PATH = "indexes/reconciliation-state.json"
RECONCILIATION_MODE = "full-scan"
RECONCILIATION_SOURCE_OF_TRUTH = "manifest-and-hash"
RECONCILIATION_THROUGH_STAGE = "classify"
RECONCILIATION_REQUIRED_STAGES = ("register", "inventory", "classify")

_HINT_STATUSES = frozenset(
    {"absent", "ledger-only", "explicit-only", "consistent", "inconsistent"}
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
_MAX_EXPLICIT_DIRTY_PATHS = 256
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_STATE_FIELDS = {
    "schema_version",
    "kind",
    "reconciliation_version",
    "project_id",
    "revision",
    "acknowledged_through_sequence",
    "acknowledged_ledger_sha256",
    "last_success",
}
_LAST_SUCCESS_FIELDS = {
    "run_id",
    "completed_at",
    "manifest_version",
    "manifest_scan_generation",
    "manifest_sha256",
    "coverage_report_version",
    "coverage_sha256",
    "coverage_failure_count",
    "snapshot_event_count",
    "snapshot_last_sequence",
    "snapshot_ledger_sha256",
    "pending_event_count",
    "pending_dirty_path_count",
    "hint_status",
    "explicit_dirty_path_count",
    "queued_dirty_path_count",
    "projection_rebuilt",
}


class ProjectReconciliationError(LayoutError):
    """Base error for conservative reconciliation."""

    reason_code = "reconciliation-failed"


class ReconciliationHintError(ProjectReconciliationError):
    reason_code = "reconciliation-hint-invalid"


class ReconciliationStateError(ProjectReconciliationError):
    reason_code = "reconciliation-state-invalid"


class ReconciliationLockError(ProjectReconciliationError):
    reason_code = "reconciliation-lock-failed"


class ReconciliationRunError(ProjectReconciliationError):
    reason_code = "reconciliation-run-failed"


class ReconciliationSnapshotError(ProjectReconciliationError):
    reason_code = "reconciliation-snapshot-invalid"


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    if not _is_integer(value) or value < minimum:
        raise ReconciliationStateError(
            f"{label} must be an integer greater than or equal to {minimum}"
        )
    return value


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReconciliationStateError(f"{label} must be non-empty text")
    return value


def _sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ReconciliationStateError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _exact_mapping(value: object, fields: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReconciliationStateError(f"{label} must be an object")
    actual = set(value)
    if actual != fields:
        raise ReconciliationStateError(
            f"{label} fields are invalid; missing={sorted(fields - actual)}, "
            f"extra={sorted(actual - fields)}"
        )
    return dict(value)


def _aware_timestamp(value: object, *, label: str) -> str:
    text = _text(value, label=label)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ReconciliationStateError(f"{label} must be ISO-8601") from exc
    if moment.tzinfo is None:
        raise ReconciliationStateError(f"{label} must include a timezone")
    return text


def _clock_timestamp(clock: Callable[[], datetime] | None) -> str:
    moment = datetime.now(timezone.utc) if clock is None else clock()
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ProjectReconciliationError(
            "reconciliation clock must return a timezone-aware datetime"
        )
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_lock_timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError("lock_timeout_seconds must be a positive finite number")
    return float(value)


@contextmanager
def _exclusive_reconciliation_lock(
    lock_file: Path, *, timeout_seconds: float
) -> Iterator[None]:
    """Hold the stable per-project machine-state advisory lock."""

    timeout = _validate_lock_timeout(timeout_seconds)
    lock = AdvisoryFileLock(lock_file, timeout_seconds=timeout)
    try:
        lock.acquire()
    except AdvisoryLockTimeoutError as exc:
        raise ReconciliationLockError(
            f"timed out waiting for reconciliation lock: {lock_file}"
        ) from exc
    except AdvisoryLockError as exc:
        raise ReconciliationLockError(
            f"could not acquire reconciliation lock {lock_file}: {exc}"
        ) from exc
    try:
        yield
    finally:
        lock.release()


@dataclass(frozen=True)
class ReconciliationState:
    project_id: str
    revision: int
    acknowledged_through_sequence: int
    acknowledged_ledger_sha256: str
    last_success: dict[str, Any] | None

    @classmethod
    def initial(cls, project_id: str) -> "ReconciliationState":
        return cls(project_id, 0, 0, _EMPTY_SHA256, None)

    def as_dict(self) -> dict[str, Any]:
        if self.revision < 1 or self.last_success is None:
            raise ReconciliationStateError("initial reconciliation state is not writable")
        return {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "kind": RECONCILIATION_STATE_KIND,
            "reconciliation_version": RECONCILIATION_VERSION,
            "project_id": self.project_id,
            "revision": self.revision,
            "acknowledged_through_sequence": self.acknowledged_through_sequence,
            "acknowledged_ledger_sha256": self.acknowledged_ledger_sha256,
            "last_success": dict(self.last_success),
        }


@dataclass(frozen=True)
class ProjectReconciliationResult:
    project_id: str
    run_id: str
    run_status: str
    stage_statuses: Mapping[str, str]
    manifest_version: str
    manifest_scan_generation: int
    manifest_sha256: str
    coverage_report_version: str
    coverage_sha256: str
    coverage_failure_count: int
    hint_status: str
    snapshot_event_count: int
    snapshot_last_sequence: int
    snapshot_ledger_sha256: str
    pending_event_count: int
    queued_dirty_path_count: int
    explicit_dirty_path_count: int
    combined_dirty_path_count: int
    projection_rebuilt: bool
    previous_acknowledged_through_sequence: int
    acknowledged_through_sequence: int
    newly_acknowledged_event_count: int
    remaining_event_count: int
    remaining_dirty_path_count: int
    state_revision: int
    acknowledged_ledger_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "kind": RECONCILIATION_RESULT_KIND,
            "reconciliation_version": RECONCILIATION_VERSION,
            "project_id": self.project_id,
            "status": "reconciled",
            "mode": RECONCILIATION_MODE,
            "source_of_truth": RECONCILIATION_SOURCE_OF_TRUTH,
            "run": {
                "run_id": self.run_id,
                "status": self.run_status,
                "through_stage": RECONCILIATION_THROUGH_STAGE,
                "stages": dict(self.stage_statuses),
            },
            "manifest": {
                "manifest_version": self.manifest_version,
                "scan_generation": self.manifest_scan_generation,
                "sha256": self.manifest_sha256,
            },
            "coverage": {
                "report_version": self.coverage_report_version,
                "sha256": self.coverage_sha256,
                "failed_file_count": self.coverage_failure_count,
            },
            "hints": {
                "status": self.hint_status,
                "snapshot_event_count": self.snapshot_event_count,
                "snapshot_last_sequence": self.snapshot_last_sequence,
                "snapshot_ledger_sha256": self.snapshot_ledger_sha256,
                "pending_event_count": self.pending_event_count,
                "queued_dirty_path_count": self.queued_dirty_path_count,
                "explicit_dirty_path_count": self.explicit_dirty_path_count,
                "combined_dirty_path_count": self.combined_dirty_path_count,
                "projection_rebuilt": self.projection_rebuilt,
            },
            "acknowledgement": {
                "previous_through_sequence": self.previous_acknowledged_through_sequence,
                "current_through_sequence": self.acknowledged_through_sequence,
                "newly_acknowledged_event_count": self.newly_acknowledged_event_count,
                "remaining_event_count": self.remaining_event_count,
                "remaining_dirty_path_count": self.remaining_dirty_path_count,
                "state_revision": self.state_revision,
                "acknowledged_ledger_sha256": self.acknowledged_ledger_sha256,
            },
        }

def _validate_last_success(
    value: object,
    *,
    acknowledged_sequence: int,
    acknowledged_sha256: str,
) -> dict[str, Any]:
    record = _exact_mapping(value, _LAST_SUCCESS_FIELDS, label="last_success")
    _text(record["run_id"], label="last_success.run_id")
    _aware_timestamp(record["completed_at"], label="last_success.completed_at")
    if record["manifest_version"] != PROJECT_MANIFEST_VERSION:
        raise ReconciliationStateError("last_success manifest_version is unsupported")
    _integer(
        record["manifest_scan_generation"],
        label="last_success.manifest_scan_generation",
        minimum=1,
    )
    _sha256(record["manifest_sha256"], label="last_success.manifest_sha256")
    if record["coverage_report_version"] != COVERAGE_REPORT_VERSION:
        raise ReconciliationStateError(
            "last_success coverage_report_version is unsupported"
        )
    _sha256(record["coverage_sha256"], label="last_success.coverage_sha256")
    coverage_failure_count = _integer(
        record["coverage_failure_count"],
        label="last_success.coverage_failure_count",
    )
    if coverage_failure_count != 0:
        raise ReconciliationStateError(
            "last_success coverage_failure_count must be zero"
        )
    event_count = _integer(
        record["snapshot_event_count"],
        label="last_success.snapshot_event_count",
    )
    last_sequence = _integer(
        record["snapshot_last_sequence"],
        label="last_success.snapshot_last_sequence",
    )
    if event_count != last_sequence:
        raise ReconciliationStateError(
            "last_success snapshot count and sequence must match the contiguous ledger"
        )
    snapshot_sha = _sha256(
        record["snapshot_ledger_sha256"],
        label="last_success.snapshot_ledger_sha256",
    )
    if last_sequence != acknowledged_sequence or snapshot_sha != acknowledged_sha256:
        raise ReconciliationStateError(
            "last_success snapshot does not match the acknowledged ledger boundary"
        )
    pending_event_count = _integer(
        record["pending_event_count"],
        label="last_success.pending_event_count",
    )
    if pending_event_count > event_count:
        raise ReconciliationStateError(
            "last_success pending_event_count exceeds snapshot_event_count"
        )
    pending_dirty_path_count = _integer(
        record["pending_dirty_path_count"],
        label="last_success.pending_dirty_path_count",
    )
    hint_status = record["hint_status"]
    if hint_status not in _HINT_STATUSES:
        raise ReconciliationStateError("last_success hint_status is unsupported")
    explicit_dirty_path_count = _integer(
        record["explicit_dirty_path_count"],
        label="last_success.explicit_dirty_path_count",
    )
    if explicit_dirty_path_count > _MAX_EXPLICIT_DIRTY_PATHS:
        raise ReconciliationStateError(
            "last_success explicit_dirty_path_count exceeds the supported limit"
        )
    queued_dirty_path_count = _integer(
        record["queued_dirty_path_count"],
        label="last_success.queued_dirty_path_count",
    )
    if pending_dirty_path_count != queued_dirty_path_count:
        raise ReconciliationStateError(
            "last_success pending and queued dirty-path counts must match"
        )
    expected_presence = {
        "absent": (False, False),
        "ledger-only": (True, False),
        "explicit-only": (False, True),
        "consistent": (True, True),
        "inconsistent": (True, True),
    }[hint_status]
    observed_presence = (
        queued_dirty_path_count > 0,
        explicit_dirty_path_count > 0,
    )
    if observed_presence != expected_presence:
        raise ReconciliationStateError(
            "last_success hint_status contradicts its dirty-path counts"
        )
    if (
        hint_status == "consistent"
        and queued_dirty_path_count != explicit_dirty_path_count
    ):
        raise ReconciliationStateError(
            "last_success consistent hints must have matching path counts"
        )
    if not isinstance(record["projection_rebuilt"], bool):
        raise ReconciliationStateError(
            "last_success.projection_rebuilt must be a boolean"
        )
    return record


def load_reconciliation_state(
    state_file: Path,
    *,
    project_id: str,
) -> ReconciliationState:
    """Read current Schema v1 state without migrating legacy data."""

    if state_file.is_symlink():
        raise ReconciliationStateError(
            f"reconciliation state must not be a symbolic link: {state_file}"
        )
    if not state_file.exists():
        return ReconciliationState.initial(project_id)
    if not state_file.is_file():
        raise ReconciliationStateError(
            f"reconciliation state is not a file: {state_file}"
        )
    try:
        document = load_versioned_json(
            state_file,
            allow_legacy=True,
            max_supported=RECONCILIATION_SCHEMA_VERSION,
        )
    except UnsupportedSchemaVersionError:
        raise
    except LayoutError as exc:
        raise ReconciliationStateError(
            f"reconciliation state is malformed and will not be rewritten: {state_file}"
        ) from exc
    if document.is_legacy:
        raise ReconciliationStateError(
            f"reconciliation state is legacy v0 and will not be rewritten: {state_file}"
        )
    record = _exact_mapping(
        document.data, _STATE_FIELDS, label="reconciliation state"
    )
    if record["kind"] != RECONCILIATION_STATE_KIND:
        raise ReconciliationStateError(
            f"unexpected reconciliation state kind {record['kind']!r}"
        )
    if record["reconciliation_version"] != RECONCILIATION_VERSION:
        raise ReconciliationStateError("reconciliation state version is unsupported")
    if record["project_id"] != project_id:
        raise ReconciliationStateError(
            "reconciliation state belongs to a different project"
        )
    revision = _integer(record["revision"], label="revision", minimum=1)
    acknowledged_sequence = _integer(
        record["acknowledged_through_sequence"],
        label="acknowledged_through_sequence",
    )
    acknowledged_sha = _sha256(
        record["acknowledged_ledger_sha256"],
        label="acknowledged_ledger_sha256",
    )
    last_success = _validate_last_success(
        record["last_success"],
        acknowledged_sequence=acknowledged_sequence,
        acknowledged_sha256=acknowledged_sha,
    )
    return ReconciliationState(
        project_id=project_id,
        revision=revision,
        acknowledged_through_sequence=acknowledged_sequence,
        acknowledged_ledger_sha256=acknowledged_sha,
        last_success=last_success,
    )


def _write_reconciliation_state_atomic(
    state_file: Path, state: ReconciliationState
) -> None:
    if state_file.is_symlink():
        raise ReconciliationStateError(
            f"reconciliation state must not be a symbolic link: {state_file}"
        )
    if state_file.exists() and not state_file.is_file():
        raise ReconciliationStateError(
            f"reconciliation state is not a file: {state_file}"
        )
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReconciliationStateError(
            f"could not prepare reconciliation state directory {state_file.parent}: {exc}"
        ) from exc
    payload = (
        json.dumps(
            state.as_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".reconciliation-state.json.",
            suffix=".tmp",
            dir=state_file.parent,
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, state_file)
        temporary = None
    except OSError as exc:
        raise ReconciliationStateError(
            f"could not write reconciliation state {state_file}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _normalize_explicit_paths(
    values: Iterable[str | Path] | str | Path,
) -> tuple[str, ...]:
    raw_values: Iterable[str | Path]
    if isinstance(values, (str, Path)):
        raw_values = (values,)
    else:
        raw_values = values
    normalized: set[str] = set()
    try:
        for item_count, value in enumerate(raw_values, start=1):
            if item_count > _MAX_EXPLICIT_DIRTY_PATHS:
                raise ReconciliationHintError(
                    "dirty_paths may contain at most "
                    f"{_MAX_EXPLICIT_DIRTY_PATHS} supplied paths"
                )
            normalized.add(normalize_dirty_path(value))
    except ReconciliationHintError:
        raise
    except (HostEventError, TypeError) as exc:
        raise ReconciliationHintError("dirty_paths contain an invalid path") from exc
    return tuple(sorted(normalized))


def _hint_status(
    queued_paths: Sequence[str], explicit_paths: Sequence[str]
) -> str:
    queued = set(queued_paths)
    explicit = set(explicit_paths)
    if not queued and not explicit:
        return "absent"
    if queued and not explicit:
        return "ledger-only"
    if explicit and not queued:
        return "explicit-only"
    if queued == explicit:
        return "consistent"
    return "inconsistent"


def _prefix_sha256(snapshot: HostEventLedgerSnapshot, sequence: int) -> str:
    if sequence < 0 or sequence > snapshot.event_count:
        raise ReconciliationStateError(
            "acknowledged sequence lies outside the current host-event ledger"
        )
    payload = b"".join(
        event.serialized_line() for event in snapshot.events[:sequence]
    )
    return hashlib.sha256(payload).hexdigest()


def _validate_prior_acknowledgement(
    state: ReconciliationState, snapshot: HostEventLedgerSnapshot
) -> None:
    observed = _prefix_sha256(snapshot, state.acknowledged_through_sequence)
    if observed != state.acknowledged_ledger_sha256:
        raise ReconciliationStateError(
            "acknowledged host-event ledger prefix no longer matches the checkpoint"
        )


def _validate_snapshot_prefix(
    starting: HostEventLedgerSnapshot, current: HostEventLedgerSnapshot
) -> None:
    if current.event_count < starting.event_count:
        raise ReconciliationSnapshotError(
            "host-event ledger became shorter than the reconciliation snapshot"
        )
    prefix = current.events[: starting.event_count]
    if prefix != starting.events or _prefix_sha256(current, starting.event_count) != (
        starting.ledger_sha256
    ):
        raise ReconciliationSnapshotError(
            "host-event ledger no longer has the reconciliation snapshot as a prefix"
        )


def _stage_statuses(run: ProjectRunResult) -> dict[str, str]:
    record = run.record
    if record.get("through_stage") != RECONCILIATION_THROUGH_STAGE:
        raise ReconciliationRunError(
            "reconciliation run did not preserve the classify boundary"
        )
    stages = record.get("stages")
    if not isinstance(stages, list):
        raise ReconciliationRunError("reconciliation run has no valid stage ledger")
    if len(stages) != len(PROJECT_UNDERSTAND_STAGES):
        raise ReconciliationRunError(
            "reconciliation run stage ledger does not match the closed pipeline"
        )
    by_id: dict[str, str] = {}
    for expected_stage_id, stage in zip(PROJECT_UNDERSTAND_STAGES, stages):
        if not isinstance(stage, dict) or stage.get("stage_id") != expected_stage_id:
            raise ReconciliationRunError(
                "reconciliation run stage ledger does not match the closed pipeline"
            )
        status = stage.get("status")
        if not isinstance(status, str):
            raise ReconciliationRunError(
                f"reconciliation stage {expected_stage_id!r} has no valid status"
            )
        by_id[expected_stage_id] = status

    statuses: dict[str, str] = {}
    for stage_id in RECONCILIATION_REQUIRED_STAGES:
        status = by_id[stage_id]
        if status != "succeeded":
            raise ReconciliationRunError(
                f"required reconciliation stage {stage_id!r} did not succeed"
            )
        statuses[stage_id] = status
    for stage_id in PROJECT_UNDERSTAND_STAGES[len(RECONCILIATION_REQUIRED_STAGES) :]:
        if by_id[stage_id] != "pending":
            raise ReconciliationRunError(
                f"later reconciliation stage {stage_id!r} must remain pending"
            )
    if run.status != "paused":
        raise ReconciliationRunError(
            "reconciliation run did not pause successfully after classify"
        )
    return statuses


def _read_artifact_bytes(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ReconciliationRunError(f"{label} is unavailable or unsafe")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ReconciliationRunError(f"could not read {label}") from exc


def _strict_manifest_snapshot(
    registration: Any,
    payload: bytes,
) -> Any:
    if not payload or not payload.endswith(b"\n"):
        raise ReconciliationRunError("Manifest snapshot is not canonical JSONL")
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        if not raw_line:
            raise ReconciliationRunError("Manifest snapshot contains a blank JSONL line")
        try:
            parse_json_bytes_strict(
                raw_line,
                label=f"Manifest line {line_number}",
            )
        except LayoutError as exc:
            raise ReconciliationRunError("Manifest snapshot contains invalid JSON") from exc

    try:
        with tempfile.TemporaryDirectory(prefix="llmwiki-manifest-snapshot-") as directory:
            snapshot_file = Path(directory) / "manifest.jsonl"
            snapshot_file.write_bytes(payload)
            return load_project_manifest(
                snapshot_file,
                project_id=registration.project_id,
                project_root=registration.project_root,
                required_manifest_version=PROJECT_MANIFEST_VERSION,
            )
    except ReconciliationRunError:
        raise
    except Exception as exc:
        raise ReconciliationRunError("Manifest snapshot failed strict validation") from exc


def _run_artifact_hash(
    run: ProjectRunResult,
    *,
    stage_id: str,
    artifact_type: str,
    artifact_id: str,
    relative_path: str,
) -> str:
    stages = run.record.get("stages")
    if not isinstance(stages, list):
        raise ReconciliationRunError("reconciliation run has no valid stage ledger")
    matching_stages = [
        stage
        for stage in stages
        if isinstance(stage, dict) and stage.get("stage_id") == stage_id
    ]
    if len(matching_stages) != 1:
        raise ReconciliationRunError(
            f"reconciliation run has no unique {stage_id!r} stage record"
        )
    artifacts = matching_stages[0].get("artifacts")
    if not isinstance(artifacts, list):
        raise ReconciliationRunError(
            f"reconciliation stage {stage_id!r} has no artifact ledger"
        )
    matches = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict)
        and artifact.get("artifact_type") == artifact_type
        and artifact.get("artifact_id") == artifact_id
        and artifact.get("relative_path") == relative_path
    ]
    if len(matches) != 1:
        raise ReconciliationRunError(
            f"reconciliation stage {stage_id!r} is not bound to its exact artifact"
        )
    content_hash = matches[0].get("content_hash")
    if not isinstance(content_hash, str) or _SHA256_PATTERN.fullmatch(content_hash) is None:
        raise ReconciliationRunError(
            f"reconciliation stage {stage_id!r} artifact has no valid SHA-256"
        )
    return content_hash


@dataclass(frozen=True)
class _ReconciliationArtifactSnapshot:
    manifest_version: str
    scan_generation: int
    manifest_bytes: bytes
    coverage_bytes: bytes
    manifest_sha256: str
    coverage_sha256: str
    failure_count: int


def _validate_coverage_artifacts(
    registration: Any,
    coverage: CoverageReportResult,
    run: ProjectRunResult,
) -> _ReconciliationArtifactSnapshot:
    expected_manifest_file = registration.layout.manifest_file
    expected_report_file = registration.layout.indexes_dir / "coverage-report.json"
    if coverage.project_id != registration.project_id:
        raise ReconciliationRunError("coverage result belongs to a different project")
    if coverage.manifest_file.resolve() != expected_manifest_file.resolve():
        raise ReconciliationRunError("coverage result references an unexpected Manifest")
    if coverage.report_file.resolve() != expected_report_file.resolve():
        raise ReconciliationRunError("coverage result references an unexpected report")

    manifest_bytes = _read_artifact_bytes(expected_manifest_file, label="Manifest")
    coverage_bytes = _read_artifact_bytes(
        expected_report_file,
        label="coverage report",
    )
    manifest = _strict_manifest_snapshot(registration, manifest_bytes)
    try:
        document = parse_versioned_json_bytes(
            coverage_bytes,
            path=expected_report_file,
            allow_legacy=False,
            max_supported=COVERAGE_REPORT_SCHEMA_VERSION,
        )
    except LayoutError as exc:
        raise ReconciliationRunError("persisted coverage report is invalid") from exc
    report = document.data
    if (
        report.get("kind") != COVERAGE_REPORT_KIND
        or report.get("report_version") != COVERAGE_REPORT_VERSION
        or report.get("project_id") != registration.project_id
    ):
        raise ReconciliationRunError("coverage result schema is unsupported")
    if coverage.report != report:
        raise ReconciliationRunError(
            "persisted coverage report does not match the run result"
        )

    expected_report = build_coverage_report(
        project_id=registration.project_id,
        manifest_file=expected_manifest_file,
        manifest_version=manifest.manifest_version,
        scan_generation=manifest.scan_generation,
        manifest_file_count=manifest.summary["record_counts"]["file"],
        file_records=manifest.file_records,
    )
    if report != expected_report:
        raise ReconciliationRunError(
            "coverage report does not reconcile with the exact Manifest snapshot"
        )
    failure_count = report["totals"]["failed_file_count"]
    if not _is_integer(failure_count) or failure_count < 0:
        raise ReconciliationRunError("coverage failure count is invalid")
    if failure_count != 0:
        raise ReconciliationRunError(
            "coverage contains failed files; host events remain pending"
        )

    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    coverage_sha = hashlib.sha256(coverage_bytes).hexdigest()
    expected_manifest_hash = _run_artifact_hash(
        run,
        stage_id="inventory",
        artifact_type="project-manifest",
        artifact_id=f"manifest-generation-{manifest.scan_generation}",
        relative_path="manifest.jsonl",
    )
    expected_coverage_hash = _run_artifact_hash(
        run,
        stage_id="classify",
        artifact_type="classification-coverage",
        artifact_id=f"coverage-generation-{manifest.scan_generation}",
        relative_path="indexes/coverage-report.json",
    )
    if expected_manifest_hash != manifest_sha or expected_coverage_hash != coverage_sha:
        raise ReconciliationRunError(
            "persisted artifacts do not match the exact reconciliation run"
        )
    return _ReconciliationArtifactSnapshot(
        manifest_version=manifest.manifest_version,
        scan_generation=manifest.scan_generation,
        manifest_bytes=manifest_bytes,
        coverage_bytes=coverage_bytes,
        manifest_sha256=manifest_sha,
        coverage_sha256=coverage_sha,
        failure_count=failure_count,
    )


def _assert_artifact_snapshot_current(
    registration: Any,
    snapshot: _ReconciliationArtifactSnapshot,
) -> None:
    manifest_bytes = _read_artifact_bytes(
        registration.layout.manifest_file,
        label="Manifest",
    )
    coverage_bytes = _read_artifact_bytes(
        registration.layout.indexes_dir / "coverage-report.json",
        label="coverage report",
    )
    if (
        manifest_bytes != snapshot.manifest_bytes
        or coverage_bytes != snapshot.coverage_bytes
    ):
        raise ReconciliationSnapshotError(
            "reconciliation artifacts changed before checkpoint commit"
        )


def reconcile_project(
    workspace_root: str | Path,
    project_id: str,
    *,
    dirty_paths: Iterable[str | Path] | str | Path = (),
    run_factory: Callable[..., ProjectRunResult],
    coverage_factory: Callable[[str], CoverageReportResult],
    clock: Callable[[], datetime] | None = None,
    lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> ProjectReconciliationResult:
    """Run a conservative full scan and acknowledge only its starting event prefix."""

    timeout = _validate_lock_timeout(lock_timeout_seconds)
    explicit_paths = _normalize_explicit_paths(dirty_paths)
    registration = load_registered_project(workspace_root, project_id)
    layout = registration.layout

    with _exclusive_reconciliation_lock(
        layout.reconciliation_lock_file,
        timeout_seconds=timeout,
    ):
        previous = load_reconciliation_state(
            layout.reconciliation_state_file,
            project_id=registration.project_id,
        )
        event_state = snapshot_host_event_state(
            workspace_root,
            registration.project_id,
            lock_timeout_seconds=timeout,
        )
        starting = event_state.ledger
        _validate_prior_acknowledgement(previous, starting)

        pending_events = starting.events[previous.acknowledged_through_sequence :]
        queued_paths = tuple(
            sorted({path for event in pending_events for path in event.paths})
        )
        hint_status = _hint_status(queued_paths, explicit_paths)
        combined_paths = tuple(sorted(set(queued_paths) | set(explicit_paths)))

        try:
            run = run_factory(
                registration.project_id,
                through_stage=RECONCILIATION_THROUGH_STAGE,
            )
        except Exception as exc:
            raise ReconciliationRunError(
                "the full reconciliation run could not be started"
            ) from exc
        stage_statuses = _stage_statuses(run)
        try:
            coverage = coverage_factory(registration.project_id)
            artifact_snapshot = _validate_coverage_artifacts(
                registration,
                coverage,
                run,
            )
        except ReconciliationRunError:
            raise
        except Exception as exc:
            raise ReconciliationRunError(
                "reconciliation artifacts could not be validated"
            ) from exc

        completed_at = _clock_timestamp(clock)
        with locked_host_event_ledger(
            workspace_root,
            registration.project_id,
            lock_timeout_seconds=timeout,
        ) as current:
            _validate_snapshot_prefix(starting, current)
            remaining_events = current.events[starting.event_count :]
            remaining_paths = {
                path for event in remaining_events for path in event.paths
            }
            last_success = {
                "run_id": run.run_id,
                "completed_at": completed_at,
                "manifest_version": artifact_snapshot.manifest_version,
                "manifest_scan_generation": artifact_snapshot.scan_generation,
                "manifest_sha256": artifact_snapshot.manifest_sha256,
                "coverage_report_version": COVERAGE_REPORT_VERSION,
                "coverage_sha256": artifact_snapshot.coverage_sha256,
                "coverage_failure_count": artifact_snapshot.failure_count,
                "snapshot_event_count": starting.event_count,
                "snapshot_last_sequence": starting.last_sequence,
                "snapshot_ledger_sha256": starting.ledger_sha256,
                "pending_event_count": len(pending_events),
                "pending_dirty_path_count": len(queued_paths),
                "hint_status": hint_status,
                "explicit_dirty_path_count": len(explicit_paths),
                "queued_dirty_path_count": len(queued_paths),
                "projection_rebuilt": event_state.projection_rebuilt,
            }
            updated = ReconciliationState(
                project_id=registration.project_id,
                revision=previous.revision + 1,
                acknowledged_through_sequence=starting.last_sequence,
                acknowledged_ledger_sha256=starting.ledger_sha256,
                last_success=last_success,
            )
            _assert_artifact_snapshot_current(registration, artifact_snapshot)
            _write_reconciliation_state_atomic(
                layout.reconciliation_state_file,
                updated,
            )

        return ProjectReconciliationResult(
            project_id=registration.project_id,
            run_id=run.run_id,
            run_status=run.status,
            stage_statuses=stage_statuses,
            manifest_version=artifact_snapshot.manifest_version,
            manifest_scan_generation=artifact_snapshot.scan_generation,
            manifest_sha256=artifact_snapshot.manifest_sha256,
            coverage_report_version=COVERAGE_REPORT_VERSION,
            coverage_sha256=artifact_snapshot.coverage_sha256,
            coverage_failure_count=artifact_snapshot.failure_count,
            hint_status=hint_status,
            snapshot_event_count=starting.event_count,
            snapshot_last_sequence=starting.last_sequence,
            snapshot_ledger_sha256=starting.ledger_sha256,
            pending_event_count=len(pending_events),
            queued_dirty_path_count=len(queued_paths),
            explicit_dirty_path_count=len(explicit_paths),
            combined_dirty_path_count=len(combined_paths),
            projection_rebuilt=event_state.projection_rebuilt,
            previous_acknowledged_through_sequence=(
                previous.acknowledged_through_sequence
            ),
            acknowledged_through_sequence=starting.last_sequence,
            newly_acknowledged_event_count=(
                starting.last_sequence - previous.acknowledged_through_sequence
            ),
            remaining_event_count=len(remaining_events),
            remaining_dirty_path_count=len(remaining_paths),
            state_revision=updated.revision,
            acknowledged_ledger_sha256=starting.ledger_sha256,
        )
