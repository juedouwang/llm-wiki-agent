#!/usr/bin/env python3
"""Persisted, resumable orchestration for project-understanding stages."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

if __package__:
    from .advisory_lock import (
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
    from .project_registry import load_registered_project
    from .project_runs import (
        PROJECT_RUN_SCHEMA_VERSION,
        PROJECT_RUN_VERSION,
        PROJECT_UNDERSTAND_STAGES,
        ProjectRunError,
        ProjectRunResult,
        ProjectRunStateError,
        StageOutcome,
        capture_input_snapshot,
        create_project_run,
        elapsed_ms,
        generate_run_id,
        load_project_run,
        refresh_run_ledger,
        save_project_run,
        stage_by_id,
        utc_timestamp,
        validate_run_id,
        validate_stage_id,
        validate_versions,
    )
else:
    from advisory_lock import (  # type: ignore[no-redef]
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        AdvisoryFileLock,
        AdvisoryLockError,
        AdvisoryLockTimeoutError,
    )
    from project_registry import load_registered_project  # type: ignore[no-redef]
    from project_runs import (  # type: ignore[no-redef]
        PROJECT_RUN_SCHEMA_VERSION,
        PROJECT_RUN_VERSION,
        PROJECT_UNDERSTAND_STAGES,
        ProjectRunError,
        ProjectRunResult,
        ProjectRunStateError,
        StageOutcome,
        capture_input_snapshot,
        create_project_run,
        elapsed_ms,
        generate_run_id,
        load_project_run,
        refresh_run_ledger,
        save_project_run,
        stage_by_id,
        utc_timestamp,
        validate_run_id,
        validate_stage_id,
        validate_versions,
    )


Clock = Callable[[], datetime]
RunIdFactory = Callable[[datetime], str]


@dataclass(frozen=True)
class StageContext:
    """Read-only invocation context supplied to one stage handler."""

    workspace_root: Path
    project_id: str
    run_id: str
    stage_id: str
    attempt: int
    run: Mapping[str, Any]


StageRunner = Callable[[StageContext], StageOutcome]


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_run_id_factory(moment: datetime) -> str:
    return generate_run_id(moment)


def _safe_exception_message(exc: Exception) -> str:
    detail = " ".join(str(exc).split())
    prefix = type(exc).__name__
    message = prefix if not detail else f"{prefix}: {detail}"
    return message[:1000]


def _attempt_input_versions(
    record: Mapping[str, Any],
    workspace_root: Path,
    project_id: str,
) -> dict[str, str | int | None]:
    try:
        snapshot: object = capture_input_snapshot(workspace_root, project_id)
    except Exception:
        snapshot = record.get("input_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ProjectRunStateError("run input snapshot is invalid")
    manifest = snapshot.get("manifest")
    versions: dict[str, str | int | None] = {
        "project_schema_version": snapshot.get("project_schema_version"),
        "run_schema_version": PROJECT_RUN_SCHEMA_VERSION,
        "run_version": PROJECT_RUN_VERSION,
        "manifest_version": None,
        "manifest_scan_generation": None,
    }
    if isinstance(manifest, Mapping):
        versions["manifest_version"] = manifest.get("manifest_version")
        versions["manifest_scan_generation"] = manifest.get("scan_generation")
    return validate_versions(versions)


class ProjectRunOrchestrator:
    """Execute canonical stages with durable checkpoints between attempts."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        runners: Mapping[str, StageRunner] | None = None,
        clock: Clock | None = None,
        run_id_factory: RunIdFactory | None = None,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._clock = clock or _default_clock
        self._run_id_factory = run_id_factory or _default_run_id_factory
        self._lock_timeout_seconds = lock_timeout_seconds
        validated_runners: dict[str, StageRunner] = {}
        for stage_id, runner in dict(runners or {}).items():
            validate_stage_id(stage_id)
            if not callable(runner):
                raise ProjectRunError(f"runner for stage {stage_id!r} is not callable")
            validated_runners[stage_id] = runner
        self._runners = validated_runners

    def start(
        self,
        project_id: str,
        *,
        through_stage: str | None = None,
    ) -> ProjectRunResult:
        """Create and execute one new logical project-understanding run."""

        if through_stage is not None:
            validate_stage_id(through_stage)
        lock = self._acquire_machine_state_lock(project_id)
        try:
            moment = self._moment()
            run_id = validate_run_id(self._run_id_factory(moment))
            created = create_project_run(
                self.workspace_root,
                project_id,
                run_id=run_id,
                created_at=utc_timestamp(moment),
                through_stage=through_stage,
            )
            return self._execute(created, through_stage=through_stage)
        finally:
            lock.release()

    def resume(
        self,
        project_id: str,
        run_id: str,
        *,
        through_stage: str | None = None,
    ) -> ProjectRunResult:
        """Resume one persisted run without repeating succeeded stages."""

        validate_run_id(run_id)
        if through_stage is not None:
            validate_stage_id(through_stage)
        lock = self._acquire_machine_state_lock(project_id)
        try:
            current = load_project_run(self.workspace_root, project_id, run_id)
            if current.status == "succeeded":
                return current
            if current.status == "running":
                current = self._record_interruption(current)
            return self._execute(current, through_stage=through_stage)
        finally:
            lock.release()

    def _acquire_machine_state_lock(self, project_id: str) -> AdvisoryFileLock:
        registration = load_registered_project(self.workspace_root, project_id)
        lock = AdvisoryFileLock(
            registration.layout.machine_state_lock_file,
            timeout_seconds=self._lock_timeout_seconds,
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

    def status(self, project_id: str, run_id: str) -> ProjectRunResult:
        """Load a run report without executing or repairing it."""

        return load_project_run(self.workspace_root, project_id, run_id)

    def _moment(self) -> datetime:
        moment = self._clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise ProjectRunError("orchestrator clock must return an aware datetime")
        return moment.astimezone(timezone.utc)

    def _execute(
        self,
        current: ProjectRunResult,
        *,
        through_stage: str | None,
    ) -> ProjectRunResult:
        boundary = (
            len(PROJECT_UNDERSTAND_STAGES) - 1
            if through_stage is None
            else PROJECT_UNDERSTAND_STAGES.index(through_stage)
        )
        made_progress = False
        for stage_id in PROJECT_UNDERSTAND_STAGES[: boundary + 1]:
            stage = stage_by_id(current.record, stage_id)
            if stage["status"] == "succeeded":
                continue
            runner = self._runners.get(stage_id)
            if stage["status"] == "unavailable" and runner is None:
                continue
            current = self._begin_attempt(
                current,
                stage_id=stage_id,
                through_stage=through_stage,
            )
            made_progress = True
            attempt = len(stage_by_id(current.record, stage_id)["attempts"])
            context = StageContext(
                workspace_root=self.workspace_root,
                project_id=current.project_id,
                run_id=current.run_id,
                stage_id=stage_id,
                attempt=attempt,
                run=deepcopy(current.record),
            )
            if runner is None:
                outcome = StageOutcome.unavailable()
            else:
                try:
                    outcome = runner(context)
                    if not isinstance(outcome, StageOutcome):
                        outcome = StageOutcome.failed(
                            "stage-handler-invalid-result",
                            "Stage handler did not return a StageOutcome.",
                            retryable=False,
                        )
                except Exception as exc:
                    outcome = StageOutcome.failed(
                        "stage-handler-exception",
                        _safe_exception_message(exc),
                    )
            current = self._complete_attempt(current, stage_id, outcome)
            if outcome.status == "failed":
                return self._finalize(current, "failed", through_stage)

        record = current.record
        stages = record["stages"]
        if any(stage["status"] == "failed" for stage in stages):
            target_status = "failed"
        elif any(stage["status"] == "pending" for stage in stages):
            target_status = "paused"
        elif any(stage["status"] == "unavailable" for stage in stages):
            target_status = "partial"
        else:
            target_status = "succeeded"

        if (
            not made_progress
            and current.status == target_status
            and current.record["through_stage"] == through_stage
        ):
            return current
        return self._finalize(current, target_status, through_stage)

    def _begin_attempt(
        self,
        current: ProjectRunResult,
        *,
        stage_id: str,
        through_stage: str | None,
    ) -> ProjectRunResult:
        record = deepcopy(current.record)
        stage = stage_by_id(record, stage_id)
        if stage["status"] == "succeeded":
            raise ProjectRunStateError("succeeded stage cannot be started again")
        if stage["status"] == "running":
            raise ProjectRunStateError("running stage must be recovered before retry")
        started_at = utc_timestamp(self._moment())
        attempt_number = len(stage["attempts"]) + 1
        attempt = {
            "attempt": attempt_number,
            "status": "running",
            "started_at": started_at,
            "completed_at": None,
            "duration_ms": None,
            "input_versions": _attempt_input_versions(
                record, self.workspace_root, current.project_id
            ),
            "provider": None,
            "model": None,
            "token_usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_hits": 0,
            },
            "estimated_cost": {"currency": "USD", "amount": 0.0},
            "error": None,
            "artifacts": [],
        }
        stage["attempts"].append(attempt)
        stage["status"] = "running"
        stage["last_error"] = None
        stage["artifacts"] = []
        record["status"] = "running"
        record["active_stage"] = stage_id
        record["through_stage"] = through_stage
        record["started_at"] = record["started_at"] or started_at
        record["completed_at"] = None
        record["duration_ms"] = None
        record["updated_at"] = started_at
        refresh_run_ledger(record)
        return save_project_run(
            self.workspace_root,
            current.project_id,
            current.run_id,
            record,
            expected_revision=current.record["revision"],
        )

    def _complete_attempt(
        self,
        current: ProjectRunResult,
        stage_id: str,
        outcome: StageOutcome,
    ) -> ProjectRunResult:
        record = deepcopy(current.record)
        stage = stage_by_id(record, stage_id)
        if stage["status"] != "running" or not stage["attempts"]:
            raise ProjectRunStateError("stage has no running attempt to complete")
        attempt = stage["attempts"][-1]
        completed_at = utc_timestamp(self._moment())
        input_versions = dict(attempt["input_versions"])
        input_versions.update(dict(outcome.input_versions or {}))
        attempt["status"] = outcome.status
        attempt["completed_at"] = completed_at
        attempt["duration_ms"] = elapsed_ms(attempt["started_at"], completed_at)
        attempt["input_versions"] = validate_versions(input_versions)
        attempt["provider"] = outcome.provider
        attempt["model"] = outcome.model
        attempt["token_usage"] = {
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "cache_hits": outcome.cache_hits,
        }
        attempt["estimated_cost"] = {
            "currency": "USD",
            "amount": float(outcome.estimated_cost_usd),
        }
        attempt["artifacts"] = [deepcopy(dict(item)) for item in outcome.artifacts]
        error = None
        if outcome.status in {"failed", "unavailable"}:
            assert outcome.reason_code is not None and outcome.message is not None
            error = {
                "stage_id": stage_id,
                "attempt": attempt["attempt"],
                "reason_code": outcome.reason_code,
                "message": outcome.message,
                "retryable": outcome.retryable,
            }
        attempt["error"] = error
        stage["status"] = outcome.status
        stage["last_error"] = error
        stage["artifacts"] = deepcopy(attempt["artifacts"])
        record["status"] = "pending"
        record["active_stage"] = None
        record["completed_at"] = None
        record["duration_ms"] = None
        record["updated_at"] = completed_at
        refresh_run_ledger(record)
        return save_project_run(
            self.workspace_root,
            current.project_id,
            current.run_id,
            record,
            expected_revision=current.record["revision"],
        )

    def _record_interruption(self, current: ProjectRunResult) -> ProjectRunResult:
        record = deepcopy(current.record)
        stage_id = record["active_stage"]
        if not isinstance(stage_id, str):
            raise ProjectRunStateError("running run has no active stage")
        stage = stage_by_id(record, stage_id)
        if stage["status"] != "running" or not stage["attempts"]:
            raise ProjectRunStateError("active stage has no running attempt")
        attempt = stage["attempts"][-1]
        completed_at = utc_timestamp(self._moment())
        error = {
            "stage_id": stage_id,
            "attempt": attempt["attempt"],
            "reason_code": "stage-interrupted",
            "message": "The prior process ended before this stage checkpointed.",
            "retryable": True,
        }
        attempt["status"] = "failed"
        attempt["completed_at"] = completed_at
        attempt["duration_ms"] = elapsed_ms(attempt["started_at"], completed_at)
        attempt["error"] = error
        stage["status"] = "failed"
        stage["last_error"] = error
        stage["artifacts"] = deepcopy(attempt["artifacts"])
        record["status"] = "pending"
        record["active_stage"] = None
        record["completed_at"] = None
        record["duration_ms"] = None
        record["updated_at"] = completed_at
        refresh_run_ledger(record)
        return save_project_run(
            self.workspace_root,
            current.project_id,
            current.run_id,
            record,
            expected_revision=current.record["revision"],
        )

    def _finalize(
        self,
        current: ProjectRunResult,
        status: str,
        through_stage: str | None,
    ) -> ProjectRunResult:
        if status not in {"paused", "partial", "succeeded", "failed"}:
            raise ProjectRunStateError("run cannot be finalized to this status")
        record = deepcopy(current.record)
        timestamp = utc_timestamp(self._moment())
        record["status"] = status
        record["active_stage"] = None
        record["through_stage"] = through_stage
        record["updated_at"] = timestamp
        if status == "paused":
            record["completed_at"] = None
            record["duration_ms"] = None
        else:
            started_at = record["started_at"]
            if not isinstance(started_at, str):
                raise ProjectRunStateError("terminal run has no start timestamp")
            record["completed_at"] = timestamp
            record["duration_ms"] = elapsed_ms(started_at, timestamp)
        refresh_run_ledger(record)
        return save_project_run(
            self.workspace_root,
            current.project_id,
            current.run_id,
            record,
            expected_revision=current.record["revision"],
        )
