#!/usr/bin/env python3
"""Deterministic initial Goal, backlog, and daily-plan machine state (I-04).

This module is intentionally smaller than the later I-05 planner. It consumes
only an already-current project-state snapshot, the persisted onboarding record,
and an optional caller-supplied objective. Missing Goal/task artifacts are
created as explicit drafts; existing artifacts are parsed strictly and left
byte-for-byte unchanged. No task is authorized for execution and no Markdown
is written here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import math
from pathlib import Path, PurePosixPath
import re
from typing import Mapping, Sequence

# Support both ``import tools.research_planning`` and direct sibling imports.
if __package__:
    from .project_analysis import (
        canonical_json_bytes,
        current_manifest_bytes,
        manifest_binding,
        manifest_snapshot,
        parse_canonical_json,
        sha256_bytes,
    )
    from .project_layout import (
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        validate_project_id,
    )
    from .project_registry import ProjectRegistrationResult, load_registered_project
    from .research_goals import (
        GOAL_VERSION,
        Goal,
        GoalSchemaError,
        parse_goal,
        serialize_goal,
    )
    from .research_state import (
        ProjectState,
        ProjectStateError,
        ProjectStateNotFoundError,
        StaleProjectStateError,
        generate_project_state,
        load_project_state,
        parse_project_state,
    )
    from .research_tasks import (
        TASK_VERSION,
        ResearchTask,
        TaskCollection,
        TaskSchemaError,
        parse_task_collection,
        serialize_task_collection,
    )
    from .stable_file_access import (
        StableDirectoryLease,
        StableFileAccessError,
        StableFileMissingError,
        compare_and_swap_atomic_stable_file,
        exclusive_stable_file_lock,
        read_stable_regular_file,
    )
else:
    from project_analysis import (  # type: ignore[no-redef]
        canonical_json_bytes,
        current_manifest_bytes,
        manifest_binding,
        manifest_snapshot,
        parse_canonical_json,
        sha256_bytes,
    )
    from project_layout import (  # type: ignore[no-redef]
        CURRENT_SCHEMA_VERSION,
        LayoutError,
        validate_project_id,
    )
    from project_registry import (  # type: ignore[no-redef]
        ProjectRegistrationResult,
        load_registered_project,
    )
    from research_goals import (  # type: ignore[no-redef]
        GOAL_VERSION,
        Goal,
        GoalSchemaError,
        parse_goal,
        serialize_goal,
    )
    from research_state import (  # type: ignore[no-redef]
        ProjectState,
        ProjectStateError,
        ProjectStateNotFoundError,
        StaleProjectStateError,
        generate_project_state,
        load_project_state,
        parse_project_state,
    )
    from research_tasks import (  # type: ignore[no-redef]
        TASK_VERSION,
        ResearchTask,
        TaskCollection,
        TaskSchemaError,
        parse_task_collection,
        serialize_task_collection,
    )
    from stable_file_access import (  # type: ignore[no-redef]
        StableDirectoryLease,
        StableFileAccessError,
        StableFileMissingError,
        compare_and_swap_atomic_stable_file,
        exclusive_stable_file_lock,
        read_stable_regular_file,
    )


INITIAL_PLAN_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
INITIAL_PLAN_KIND = "llmwiki-initial-plan"
INITIAL_PLAN_VERSION = "initial-plan-v1"
INITIAL_PLAN_STATUS = "draft"
INITIAL_PLAN_MACHINE_FILENAME = "initial-plan.json"
INITIAL_PLAN_RELATIVE_PATH = f"indexes/{INITIAL_PLAN_MACHINE_FILENAME}"

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "plan_version",
        "project_id",
        "plan_date",
        "generated_at",
        "status",
        "goal_id",
        "state_artifact_id",
        "task_ids",
        "why_now",
        "timebox_minutes",
        "inputs",
        "outputs",
        "verification",
        "blockers",
    }
)
_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_STATE_ID_RE = re.compile(r"state-[0-9a-f]{64}")
_RFC3339_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
_WINDOWS_ABSOLUTE_RE = re.compile(r"[A-Za-z]:[/\\]")
_MAX_TASK_IDS = 256
_MAX_LIST_ITEMS = 256


class InitialPlanningError(ValueError):
    """Closed-schema, currentness, or deterministic-generation failure."""


class UnsupportedInitialPlanSchemaVersionError(InitialPlanningError):
    """Legacy or future initial-plan schema version is unsupported."""


class InitialPlanNotFoundError(InitialPlanningError):
    """No current initial-plan artifact exists for the project."""


class InitialPlanStorageError(InitialPlanningError):
    """The registered machine-state boundary could not be used safely."""


def _timestamp(value: object | None = None) -> str:
    if value is None:
        current = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        if value.tzinfo is None:
            raise InitialPlanningError("generated_at requires an explicit timezone")
        current = value
    elif isinstance(value, date):
        current = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, str):
        if _RFC3339_RE.fullmatch(value) is None:
            raise InitialPlanningError(
                "generated_at must be canonical UTC RFC3339 seconds"
            )
        try:
            current = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InitialPlanningError("generated_at must be valid RFC3339") from exc
    else:
        raise InitialPlanningError(
            "generated_at must be RFC3339/date/datetime/null"
        )
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _plan_date(value: object | None, *, generated_at: str) -> str:
    if value is None:
        return generated_at[:10]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise InitialPlanningError(
                "plan_date datetime requires an explicit timezone"
            )
        raw = value.astimezone(timezone.utc).date().isoformat()
    elif isinstance(value, date):
        raw = value.isoformat()
    elif isinstance(value, str):
        raw = value
    else:
        raise InitialPlanningError(
            "plan_date must be YYYY-MM-DD/date/datetime/null"
        )
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise InitialPlanningError(
            "plan_date must be canonical YYYY-MM-DD"
        ) from exc


def _text(value: object, *, label: str, maximum: int = 4096) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise InitialPlanningError(
            f"{label} must be non-empty text without NUL"
        )
    normalized = value.strip()
    try:
        encoded = normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise InitialPlanningError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > maximum:
        raise InitialPlanningError(f"{label} exceeds {maximum} UTF-8 bytes")
    return normalized


def _identifier(value: object, *, label: str) -> str:
    normalized = _text(value, label=label, maximum=64)
    if _ID_RE.fullmatch(normalized) is None:
        raise InitialPlanningError(
            f"{label} must be lowercase and path-safe"
        )
    return normalized


def _items(
    value: object,
    *,
    label: str,
    identifiers: bool = False,
    required: bool = False,
    maximum_items: int = _MAX_LIST_ITEMS,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise InitialPlanningError(f"{label} must be an array")
    if len(value) > maximum_items:
        raise InitialPlanningError(
            f"{label} exceeds its bounded item count"
        )
    normalized = tuple(
        _identifier(item, label=f"{label}[{index}]")
        if identifiers
        else _text(item, label=f"{label}[{index}]", maximum=2048)
        for index, item in enumerate(value)
    )
    if required and not normalized:
        raise InitialPlanningError(f"{label} must not be empty")
    if len(set(normalized)) != len(normalized):
        raise InitialPlanningError(f"{label} must be duplicate-free")
    return normalized


def _relative_reference(value: str, *, label: str) -> str:
    if (
        "\\" in value
        or value.startswith("/")
        or _WINDOWS_ABSOLUTE_RE.match(value)
    ):
        raise InitialPlanningError(
            f"{label} must not contain an absolute/local path"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise InitialPlanningError(
            f"{label} must be a project-relative POSIX reference"
        )
    return value


@dataclass(frozen=True)
class InitialPlan:
    """One strict, user-reviewable initial plan bound to current machine state."""

    project_id: str
    plan_date: str
    generated_at: str
    goal_id: str
    state_artifact_id: str
    task_ids: tuple[str, ...]
    why_now: str
    timebox_minutes: int
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    verification: tuple[str, ...]
    blockers: tuple[str, ...] = ()
    status: str = INITIAL_PLAN_STATUS

    def __post_init__(self) -> None:
        try:
            project_id = validate_project_id(self.project_id)
        except (LayoutError, TypeError, ValueError) as exc:
            raise InitialPlanningError(f"invalid project_id: {exc}") from exc
        object.__setattr__(self, "project_id", project_id)

        generated_at = _timestamp(self.generated_at)
        if generated_at != self.generated_at:
            raise InitialPlanningError(
                "generated_at must be canonical UTC RFC3339 seconds"
            )
        object.__setattr__(self, "generated_at", generated_at)
        normalized_date = _plan_date(self.plan_date, generated_at=generated_at)
        if normalized_date != self.plan_date:
            raise InitialPlanningError("plan_date must be canonical YYYY-MM-DD")
        object.__setattr__(self, "plan_date", normalized_date)

        if self.status != INITIAL_PLAN_STATUS:
            raise InitialPlanningError(
                f"initial plan status must remain {INITIAL_PLAN_STATUS!r}"
            )
        object.__setattr__(self, "goal_id", _identifier(self.goal_id, label="goal_id"))
        if (
            type(self.state_artifact_id) is not str
            or _STATE_ID_RE.fullmatch(self.state_artifact_id) is None
        ):
            raise InitialPlanningError(
                "state_artifact_id must be a canonical project-state artifact ID"
            )

        task_ids = _items(
            self.task_ids,
            label="task_ids",
            identifiers=True,
            maximum_items=_MAX_TASK_IDS,
        )
        object.__setattr__(self, "task_ids", task_ids)
        object.__setattr__(
            self,
            "why_now",
            _text(self.why_now, label="why_now", maximum=8000),
        )
        if (
            isinstance(self.timebox_minutes, bool)
            or not isinstance(self.timebox_minutes, int)
            or not 1 <= self.timebox_minutes <= 1440
        ):
            raise InitialPlanningError(
                "timebox_minutes must be an integer from 1 through 1440"
            )

        for field_name in ("inputs", "outputs"):
            values = _items(
                getattr(self, field_name),
                label=field_name,
                required=True,
            )
            normalized = tuple(
                _relative_reference(value, label=f"{field_name} entry")
                for value in values
            )
            object.__setattr__(self, field_name, normalized)
        object.__setattr__(
            self,
            "verification",
            _items(
                self.verification,
                label="verification",
                required=True,
            ),
        )
        object.__setattr__(
            self,
            "blockers",
            _items(self.blockers, label="blockers"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": INITIAL_PLAN_SCHEMA_VERSION,
            "kind": INITIAL_PLAN_KIND,
            "plan_version": INITIAL_PLAN_VERSION,
            "project_id": self.project_id,
            "plan_date": self.plan_date,
            "generated_at": self.generated_at,
            "status": self.status,
            "goal_id": self.goal_id,
            "state_artifact_id": self.state_artifact_id,
            "task_ids": list(self.task_ids),
            "why_now": self.why_now,
            "timebox_minutes": self.timebox_minutes,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "verification": list(self.verification),
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class InitialPlanningResult:
    """Safe public result for one generated initial planning bundle."""

    project_id: str
    initial_plan: InitialPlan
    goal: Goal
    tasks: TaskCollection
    goal_created: bool
    tasks_created: bool
    state_created: bool
    state_rebuilt: bool

    @property
    def plan(self) -> InitialPlan:
        return self.initial_plan

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "artifacts": {
                "goal": "indexes/goals.json",
                "tasks": "indexes/tasks.json",
                "project_state": "indexes/project-state.json",
                "initial_plan": INITIAL_PLAN_RELATIVE_PATH,
                "goals_markdown": "goals.md",
                "backlog_markdown": "plans/backlog.md",
                "daily_plan_markdown": (
                    f"plans/daily/{self.initial_plan.plan_date}.md"
                ),
            },
            "created": {
                "goal": self.goal_created,
                "tasks": self.tasks_created,
                "project_state": self.state_created,
            },
            "state_rebuilt": self.state_rebuilt,
            "goal": {
                "goal_id": self.goal.goal_id,
                "status": self.goal.status,
                "draft_reasons": list(self.goal.draft_reasons),
            },
            "tasks": {
                "task_ids": [task.task_id for task in self.tasks.tasks],
                "statuses": [task.status for task in self.tasks.tasks],
            },
            "plan": self.initial_plan.as_dict(),
        }


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise InitialPlanningError(f"{label} must be a JSON object")
    return dict(value)


def _exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    fields = set(value)
    if fields != expected:
        missing = sorted(expected - fields)
        unknown = sorted(fields - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if unknown:
            details.append(f"unknown={unknown}")
        raise InitialPlanningError(
            f"{label} fields are not current ({'; '.join(details)})"
        )


def parse_initial_plan(
    payload: bytes | str | Mapping[str, object],
    *,
    project_id: str | None = None,
) -> InitialPlan:
    """Parse one strict canonical initial-plan document."""

    if isinstance(payload, bytes):
        raw = payload
        try:
            value = parse_canonical_json(raw, label="initial-plan")
        except (TypeError, ValueError) as exc:
            raise InitialPlanningError(str(exc)) from exc
    elif isinstance(payload, str):
        try:
            raw = payload.encode("utf-8", errors="strict")
            value = parse_canonical_json(raw, label="initial-plan")
        except (UnicodeEncodeError, TypeError, ValueError) as exc:
            raise InitialPlanningError(str(exc)) from exc
    elif isinstance(payload, Mapping):
        value = dict(payload)
    else:
        raise InitialPlanningError(
            "initial-plan payload must be bytes, text, or a mapping"
        )

    document = _mapping(value, label="initial-plan")
    _exact_fields(document, _TOP_FIELDS, label="initial-plan")
    schema_version = document["schema_version"]
    if (
        isinstance(schema_version, bool)
        or schema_version != INITIAL_PLAN_SCHEMA_VERSION
    ):
        raise UnsupportedInitialPlanSchemaVersionError(
            f"initial-plan schema_version {schema_version!r} is not current"
        )
    if document["kind"] != INITIAL_PLAN_KIND:
        raise InitialPlanningError("initial-plan kind is not current")
    if document["plan_version"] != INITIAL_PLAN_VERSION:
        raise InitialPlanningError("initial-plan plan_version is not current")

    try:
        embedded_project_id = validate_project_id(document["project_id"])
    except (LayoutError, TypeError, ValueError) as exc:
        raise InitialPlanningError(f"invalid project_id: {exc}") from exc
    if project_id is not None:
        try:
            requested_project_id = validate_project_id(project_id)
        except (LayoutError, TypeError, ValueError) as exc:
            raise InitialPlanningError(f"invalid requested project_id: {exc}") from exc
        if embedded_project_id != requested_project_id:
            raise InitialPlanningError(
                "initial-plan project_id does not match requested project"
            )

    return InitialPlan(
        project_id=embedded_project_id,
        plan_date=document["plan_date"],
        generated_at=document["generated_at"],
        status=document["status"],
        goal_id=document["goal_id"],
        state_artifact_id=document["state_artifact_id"],
        task_ids=tuple(document["task_ids"])
        if isinstance(document["task_ids"], Sequence)
        and not isinstance(document["task_ids"], (str, bytes, bytearray))
        else document["task_ids"],
        why_now=document["why_now"],
        timebox_minutes=document["timebox_minutes"],
        inputs=tuple(document["inputs"])
        if isinstance(document["inputs"], Sequence)
        and not isinstance(document["inputs"], (str, bytes, bytearray))
        else document["inputs"],
        outputs=tuple(document["outputs"])
        if isinstance(document["outputs"], Sequence)
        and not isinstance(document["outputs"], (str, bytes, bytearray))
        else document["outputs"],
        verification=tuple(document["verification"])
        if isinstance(document["verification"], Sequence)
        and not isinstance(document["verification"], (str, bytes, bytearray))
        else document["verification"],
        blockers=tuple(document["blockers"])
        if isinstance(document["blockers"], Sequence)
        and not isinstance(document["blockers"], (str, bytes, bytearray))
        else document["blockers"],
    )


def serialize_initial_plan(plan: InitialPlan) -> str:
    """Serialize one current plan as strict canonical JSON."""

    if not isinstance(plan, InitialPlan):
        raise InitialPlanningError("plan must be an InitialPlan")
    try:
        return canonical_json_bytes(plan.as_dict()).decode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InitialPlanningError(str(exc)) from exc

def _load_registration(
    workspace_root: str | Path,
    project_id: str,
) -> ProjectRegistrationResult:
    try:
        return load_registered_project(
            Path(workspace_root).expanduser().resolve(),
            validate_project_id(project_id),
        )
    except (LayoutError, OSError, TypeError, ValueError) as exc:
        raise InitialPlanStorageError(
            "registered project could not be loaded safely"
        ) from exc


def _read_index_bytes(
    registration: ProjectRegistrationResult,
    path: Path,
    *,
    lease: StableDirectoryLease,
    optional: bool,
) -> bytes | None:
    try:
        target = registration.layout.validate_machine_state_path(
            path,
            allow_missing_leaf=optional,
        )
        observation = read_stable_regular_file(
            registration.layout.indexes_dir,
            target,
            reject_redirection=True,
            capture_bytes=True,
            root_lease=lease,
        )
    except StableFileMissingError:
        if optional:
            return None
        raise InitialPlanNotFoundError(
            f"required planning artifact is missing: {path.name}"
        )
    except (LayoutError, StableFileAccessError, OSError) as exc:
        raise InitialPlanStorageError(
            f"planning artifact could not be read safely: {path.name}"
        ) from exc
    if observation.data is None:
        raise InitialPlanStorageError(
            f"planning artifact bytes were not captured: {path.name}"
        )
    return observation.data


def _read_project_bytes(registration: ProjectRegistrationResult) -> bytes:
    try:
        observation = read_stable_regular_file(
            registration.layout.machine_root,
            registration.project_file,
            reject_redirection=True,
            capture_bytes=True,
        )
    except (StableFileAccessError, OSError) as exc:
        raise InitialPlanStorageError(
            "project registration bytes could not be read safely"
        ) from exc
    if observation.data is None:
        raise InitialPlanStorageError(
            "project registration bytes were not captured"
        )
    return observation.data


def _registration_binding(
    registration: ProjectRegistrationResult,
    project_bytes: bytes,
) -> dict[str, object]:
    record = registration.record
    return {
        "schema_version": INITIAL_PLAN_SCHEMA_VERSION,
        "kind": "llmwiki-project",
        "project_id": registration.project_id,
        "name": str(record["name"]),
        "registered_at": str(record["registered_at"]),
        "identity_strategy": str(record["identity_strategy"]),
        "sha256": sha256_bytes(project_bytes),
    }


def _load_current_state_locked(
    registration: ProjectRegistrationResult,
    *,
    lease: StableDirectoryLease,
    manifest: object,
    manifest_sha256: str,
) -> tuple[ProjectState, bytes, bytes]:
    raw = _read_index_bytes(
        registration,
        registration.layout.project_state_file,
        lease=lease,
        optional=False,
    )
    assert raw is not None
    try:
        state = parse_project_state(raw, project_id=registration.project_id)
    except ProjectStateError as exc:
        raise InitialPlanningError(
            f"project-state is not a strict current artifact: {exc}"
        ) from exc
    expected_manifest = manifest_binding(manifest, manifest_sha256)
    if dict(state.manifest) != expected_manifest:
        raise InitialPlanningError(
            "project-state is stale for the current Manifest"
        )
    project_bytes = _read_project_bytes(registration)
    if dict(state.registration) != _registration_binding(
        registration, project_bytes
    ):
        raise InitialPlanningError(
            "project-state is stale for the current registration"
        )
    return state, raw, project_bytes


def _load_goal_locked(
    registration: ProjectRegistrationResult,
    *,
    lease: StableDirectoryLease,
) -> tuple[Goal | None, bytes | None]:
    raw = _read_index_bytes(
        registration,
        registration.layout.goals_file,
        lease=lease,
        optional=True,
    )
    if raw is None:
        return None, None
    try:
        return parse_goal(raw, project_id=registration.project_id), raw
    except GoalSchemaError as exc:
        raise InitialPlanningError(
            f"Goal artifact is not strict/current: {exc}"
        ) from exc


def _load_tasks_locked(
    registration: ProjectRegistrationResult,
    *,
    lease: StableDirectoryLease,
) -> tuple[TaskCollection | None, bytes | None]:
    raw = _read_index_bytes(
        registration,
        registration.layout.tasks_file,
        lease=lease,
        optional=True,
    )
    if raw is None:
        return None, None
    try:
        return (
            parse_task_collection(raw, project_id=registration.project_id),
            raw,
        )
    except TaskSchemaError as exc:
        raise InitialPlanningError(
            f"task artifact is not strict/current: {exc}"
        ) from exc


def _expected_goal_binding(
    goal: Goal | None,
    raw: bytes | None,
) -> dict[str, object]:
    if goal is None:
        if raw is not None:  # pragma: no cover - defensive
            raise InitialPlanningError("Goal bytes exist without a parsed Goal")
        return {
            "status": "missing",
            "item_count": 0,
            "version": None,
            "sha256": None,
        }
    if raw is None:  # pragma: no cover - defensive
        raise InitialPlanningError("parsed Goal is missing exact source bytes")
    return {
        "status": "available",
        "item_count": 1 + len(goal.milestones),
        "version": GOAL_VERSION,
        "sha256": sha256_bytes(raw),
    }


def _expected_tasks_binding(
    tasks: TaskCollection | None,
    raw: bytes | None,
) -> dict[str, object]:
    if tasks is None:
        if raw is not None:  # pragma: no cover - defensive
            raise InitialPlanningError("task bytes exist without parsed tasks")
        return {
            "status": "missing",
            "item_count": 0,
            "version": None,
            "sha256": None,
        }
    if raw is None:  # pragma: no cover - defensive
        raise InitialPlanningError("parsed tasks are missing exact source bytes")
    return {
        "status": "available",
        "item_count": len(tasks.tasks),
        "version": TASK_VERSION,
        "sha256": sha256_bytes(raw),
    }


def _assert_state_planning_inputs(
    state: ProjectState,
    *,
    goal: Goal | None,
    goal_bytes: bytes | None,
    tasks: TaskCollection | None,
    task_bytes: bytes | None,
) -> None:
    expected_goal = _expected_goal_binding(goal, goal_bytes)
    expected_tasks = _expected_tasks_binding(tasks, task_bytes)
    if dict(state.inputs["goal"]) != expected_goal:
        raise InitialPlanningError(
            "project-state Goal binding is stale; rebuild state explicitly"
        )
    if dict(state.inputs["tasks"]) != expected_tasks:
        raise InitialPlanningError(
            "project-state task binding is stale; rebuild state explicitly"
        )


def _assert_plan_bindings(
    plan: InitialPlan,
    *,
    state: ProjectState,
    goal: Goal,
    tasks: TaskCollection,
) -> None:
    if plan.project_id != state.project_id:
        raise InitialPlanningError("initial plan belongs to another project")
    if plan.state_artifact_id != state.artifact_id:
        raise InitialPlanningError(
            "initial plan is stale for the current project-state artifact"
        )
    if plan.goal_id != goal.goal_id:
        raise InitialPlanningError(
            "initial plan goal_id does not match the current Goal"
        )
    task_ids = {task.task_id for task in tasks.tasks}
    unknown = sorted(set(plan.task_ids) - task_ids)
    if unknown:
        raise InitialPlanningError(
            f"initial plan references unknown task IDs: {unknown}"
        )


def _daily_minutes(registration: ProjectRegistrationResult) -> int:
    onboarding = registration.record.get("onboarding", {})
    raw = onboarding.get("daily_available_hours") if isinstance(onboarding, Mapping) else None
    if raw is None:
        return 60
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(float(raw))
        or float(raw) <= 0
    ):
        raise InitialPlanningError(
            "registration onboarding daily_available_hours is invalid"
        )
    return max(1, min(1440, int(round(float(raw) * 60))))


def _onboarding_text(
    registration: ProjectRegistrationResult,
    field_name: str,
) -> str | None:
    onboarding = registration.record.get("onboarding", {})
    if not isinstance(onboarding, Mapping):
        raise InitialPlanningError("registration onboarding record is invalid")
    value = onboarding.get(field_name)
    if value is None:
        return None
    return _text(value, label=f"onboarding.{field_name}", maximum=8000)


def _draft_goal(
    registration: ProjectRegistrationResult,
    *,
    objective: str | None,
    generated_at: str,
) -> Goal:
    chosen_objective = (
        _text(objective, label="objective", maximum=8000)
        if objective is not None
        else _onboarding_text(registration, "final_goal")
    )
    try:
        return Goal.draft(
            registration.project_id,
            goal=chosen_objective,
            success_criteria=(),
            deadline=_onboarding_text(registration, "deadline"),
            current_stage=_onboarding_text(registration, "current_stage"),
            created_at=generated_at,
            updated_at=generated_at,
        )
    except GoalSchemaError as exc:
        raise InitialPlanningError(
            f"deterministic draft Goal could not be built: {exc}"
        ) from exc


def _draft_tasks(
    registration: ProjectRegistrationResult,
    *,
    generated_at: str,
    plan_date: str,
) -> TaskCollection:
    budget = _daily_minutes(registration)
    first_timebox = min(30, budget)
    tasks: list[ResearchTask] = [
        ResearchTask(
            project_id=registration.project_id,
            task_id="confirm-initial-goal",
            title="Review and confirm the initial research goal",
            why_now=(
                "The first project understanding needs an explicit user-confirmed "
                "objective and success criteria before work becomes executable."
            ),
            inputs=(
                "machine:indexes/project-state.json",
                "machine:indexes/goals.json",
            ),
            evidence=(),
            allowed_paths=("README*",),
            denied_paths=(".git/**", ".llmwiki/**"),
            dependencies=(),
            dod=(
                "The research objective and success criteria are explicitly confirmed.",
                "Any missing deadline or current stage is either supplied or acknowledged.",
            ),
            verification=(
                "Record explicit user confirmation before changing the Goal from DRAFT.",
            ),
            artifacts=("user-confirmation:research-goal",),
            timebox_minutes=first_timebox,
            status="draft",
            completion_refs=(),
            draft_reasons=("awaiting-user-confirmation",),
            created_at=generated_at,
            updated_at=generated_at,
        )
    ]
    remaining = budget - first_timebox
    if remaining >= 15:
        second_timebox = min(45, remaining)
        tasks.append(
            ResearchTask(
                project_id=registration.project_id,
                task_id="triage-project-state",
                title="Review project-state gaps and choose the first bounded task",
                why_now=(
                    "The deterministic project-state snapshot exposes gaps and blockers "
                    "that need human prioritization before execution."
                ),
                inputs=(
                    "machine:indexes/project-state.json",
                    "machine:indexes/tasks.json",
                ),
                evidence=(),
                allowed_paths=("README*",),
                denied_paths=(".git/**", ".llmwiki/**"),
                dependencies=("confirm-initial-goal",),
                dod=(
                    "One bounded next task is selected with an explicit reason.",
                    "Its allowed paths, verification, and expected artifacts are reviewed.",
                ),
                verification=(
                    "Confirm the selected task remains within the registered source boundary.",
                    "Confirm no draft task is treated as execution authorization.",
                ),
                artifacts=(
                    "knowledge:plans/backlog.md",
                    f"knowledge:plans/daily/{plan_date}.md",
                ),
                timebox_minutes=second_timebox,
                status="draft",
                completion_refs=(),
                draft_reasons=("awaiting-user-confirmation",),
                created_at=generated_at,
                updated_at=generated_at,
            )
        )
    try:
        return TaskCollection(
            project_id=registration.project_id,
            tasks=tuple(tasks),
            updated_at=generated_at,
        )
    except TaskSchemaError as exc:
        raise InitialPlanningError(
            f"deterministic draft tasks could not be built: {exc}"
        ) from exc

def _selected_tasks(
    registration: ProjectRegistrationResult,
    tasks: TaskCollection,
) -> tuple[tuple[str, ...], int]:
    budget = _daily_minutes(registration)
    selected: list[str] = []
    used = 0
    for task in tasks.tasks:
        if task.status in {"completed", "cancelled"}:
            continue
        duration = task.timebox_minutes or 30
        if selected and used + duration > budget:
            continue
        selected.append(task.task_id)
        used += min(duration, max(1, budget - used))
        if len(selected) >= _MAX_TASK_IDS or used >= budget:
            break
    if not selected:
        return (), min(30, budget)
    return tuple(selected), max(1, min(1440, used))


def _build_initial_plan(
    registration: ProjectRegistrationResult,
    *,
    state: ProjectState,
    goal: Goal,
    tasks: TaskCollection,
    generated_at: str,
    plan_date: str,
) -> InitialPlan:
    task_ids, timebox = _selected_tasks(registration, tasks)
    blockers: list[str] = [
        f"Goal remains DRAFT until {field_name} is confirmed."
        for field_name in goal.missing_user_fields
    ]
    draft_task_count = sum(task.status == "draft" for task in tasks.tasks)
    if draft_task_count:
        blockers.append(
            f"{draft_task_count} backlog task(s) remain DRAFT and are not executable."
        )
    state_blocker_count = state.blockers.get("count", 0)
    if isinstance(state_blocker_count, int) and state_blocker_count > 0:
        blockers.append(
            f"Project state reports {state_blocker_count} unresolved blocker(s)."
        )
    for gap in state.gaps[:32]:
        code = gap.get("code")
        if isinstance(code, str) and code:
            blockers.append(f"Project-state gap requires review: {code}.")
    if not task_ids:
        blockers.append("No non-completed backlog task is selected in this draft plan.")
    if not blockers:
        blockers.append(
            "User review is required before this DRAFT plan authorizes any execution."
        )

    stage = goal.current_stage
    if stage is None:
        why_now = (
            "Turn the current deterministic project-state snapshot into a "
            "user-reviewable DRAFT goal, backlog, and daily plan without "
            "authorizing execution."
        )
    else:
        why_now = (
            f"Review the current {stage!r} stage against the deterministic "
            "project-state snapshot and confirm a bounded next step without "
            "treating this DRAFT as execution authorization."
        )

    return InitialPlan(
        project_id=registration.project_id,
        plan_date=plan_date,
        generated_at=generated_at,
        status=INITIAL_PLAN_STATUS,
        goal_id=goal.goal_id,
        state_artifact_id=state.artifact_id,
        task_ids=task_ids,
        why_now=why_now,
        timebox_minutes=timebox,
        inputs=(
            "indexes/project-state.json",
            "indexes/goals.json",
            "indexes/tasks.json",
        ),
        outputs=(
            "goals.md",
            "plans/backlog.md",
            f"plans/daily/{plan_date}.md",
        ),
        verification=(
            "Confirm the Goal objective, success criteria, deadline, and current stage.",
            "Confirm every selected task has bounded paths, DoD, verification, and expected artifacts.",
            "Keep all generated tasks non-executable until explicit user confirmation.",
        ),
        blockers=tuple(blockers),
    )


def _write_index_bytes_locked(
    registration: ProjectRegistrationResult,
    path: Path,
    payload: bytes,
    *,
    expected_current: bytes | None,
    lease: StableDirectoryLease,
) -> Path:
    try:
        target = registration.layout.validate_machine_state_path(
            path,
            allow_missing_leaf=expected_current is None,
        )
        compare_and_swap_atomic_stable_file(
            registration.layout.indexes_dir,
            target,
            payload,
            expected_current_sha256=(
                sha256_bytes(expected_current)
                if expected_current is not None
                else None
            ),
            root_lease=lease,
        )
        return target
    except (LayoutError, StableFileAccessError, OSError) as exc:
        raise InitialPlanStorageError(
            f"planning artifact could not be written safely: {path.name}"
        ) from exc


def _assert_snapshot_unchanged(
    registration: ProjectRegistrationResult,
    *,
    manifest_bytes: bytes,
    project_bytes: bytes,
) -> None:
    try:
        current_manifest_bytes(registration, manifest_bytes)
    except (OSError, TypeError, ValueError) as exc:
        raise InitialPlanningError(
            "Manifest changed during initial planning"
        ) from exc
    if _read_project_bytes(registration) != project_bytes:
        raise InitialPlanningError(
            "project registration changed during initial planning"
        )


def _ensure_state_exists(
    workspace_root: Path,
    project_id: str,
    *,
    generated_at: str,
    lock_timeout_seconds: float,
) -> tuple[bool, bool]:
    """Ensure a current I-03 snapshot, rebuilding only trusted stale inputs.

    A missing snapshot and a valid snapshot whose Manifest/registration binding
    changed are both deterministic rebuild cases.  Malformed or unsupported state
    remains fail-closed and is never silently replaced.
    """

    try:
        load_project_state(
            workspace_root,
            project_id,
            lock_timeout_seconds=lock_timeout_seconds,
        )
        return False, False
    except ProjectStateNotFoundError:
        created = True
    except StaleProjectStateError:
        created = False
    except ProjectStateError as exc:
        raise InitialPlanningError(
            f"project-state is malformed, future, or stale: {exc}"
        ) from exc

    try:
        generate_project_state(
            workspace_root,
            project_id,
            generated_at=generated_at,
            lock_timeout_seconds=lock_timeout_seconds,
        )
    except ProjectStateError as exc:
        raise InitialPlanningError(
            f"project-state could not be generated: {exc}"
        ) from exc
    return created, True


def _create_missing_planning_artifacts(
    workspace_root: Path,
    project_id: str,
    *,
    objective: str | None,
    generated_at: str,
    plan_date: str,
    lock_timeout_seconds: float,
) -> tuple[bool, bool]:
    registration = _load_registration(workspace_root, project_id)
    try:
        with exclusive_stable_file_lock(
            registration.layout.indexes_dir,
            registration.layout.machine_state_lock_file,
            timeout_seconds=lock_timeout_seconds,
        ) as lease:
            locked = _load_registration(workspace_root, project_id)
            if (
                locked.layout.machine_root != registration.layout.machine_root
                or locked.layout.knowledge_root != registration.layout.knowledge_root
            ):
                raise InitialPlanStorageError(
                    "project registration changed while initial planning started"
                )
            manifest, manifest_bytes, manifest_sha256 = manifest_snapshot(locked)
            state, _state_bytes, project_bytes = _load_current_state_locked(
                locked,
                lease=lease,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
            )
            goal, goal_bytes = _load_goal_locked(locked, lease=lease)
            tasks, task_bytes = _load_tasks_locked(locked, lease=lease)
            _assert_state_planning_inputs(
                state,
                goal=goal,
                goal_bytes=goal_bytes,
                tasks=tasks,
                task_bytes=task_bytes,
            )

            goal_created = False
            tasks_created = False
            if goal is None:
                goal = _draft_goal(
                    locked,
                    objective=objective,
                    generated_at=generated_at,
                )
                goal_payload = serialize_goal(goal).encode("utf-8")
                _write_index_bytes_locked(
                    locked,
                    locked.layout.goals_file,
                    goal_payload,
                    expected_current=None,
                    lease=lease,
                )
                goal_created = True
            if tasks is None:
                tasks = _draft_tasks(
                    locked,
                    generated_at=generated_at,
                    plan_date=plan_date,
                )
                task_payload = serialize_task_collection(tasks).encode("utf-8")
                _write_index_bytes_locked(
                    locked,
                    locked.layout.tasks_file,
                    task_payload,
                    expected_current=None,
                    lease=lease,
                )
                tasks_created = True
            _assert_snapshot_unchanged(
                locked,
                manifest_bytes=manifest_bytes,
                project_bytes=project_bytes,
            )
            return goal_created, tasks_created
    except InitialPlanningError:
        raise
    except (StableFileAccessError, OSError, TypeError, ValueError) as exc:
        raise InitialPlanStorageError(
            "initial planning artifacts could not be created safely"
        ) from exc


def write_initial_plan(
    workspace_root: str | Path,
    project_id: str,
    plan: InitialPlan,
    *,
    lock_timeout_seconds: float = 5.0,
) -> Path:
    """Validate and atomically publish a plan bound to current Goal/tasks/state."""

    if not isinstance(plan, InitialPlan):
        raise InitialPlanningError("plan must be an InitialPlan")
    root = Path(workspace_root).expanduser().resolve()
    registration = _load_registration(root, project_id)
    if plan.project_id != registration.project_id:
        raise InitialPlanningError("plan belongs to another registered project")
    try:
        with exclusive_stable_file_lock(
            registration.layout.indexes_dir,
            registration.layout.machine_state_lock_file,
            timeout_seconds=lock_timeout_seconds,
        ) as lease:
            locked = _load_registration(root, project_id)
            manifest, manifest_bytes, manifest_sha256 = manifest_snapshot(locked)
            state, _state_bytes, project_bytes = _load_current_state_locked(
                locked,
                lease=lease,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
            )
            goal, goal_bytes = _load_goal_locked(locked, lease=lease)
            tasks, task_bytes = _load_tasks_locked(locked, lease=lease)
            if goal is None or tasks is None:
                raise InitialPlanningError(
                    "a current Goal and task collection are required before writing a plan"
                )
            _assert_state_planning_inputs(
                state,
                goal=goal,
                goal_bytes=goal_bytes,
                tasks=tasks,
                task_bytes=task_bytes,
            )
            _assert_plan_bindings(plan, state=state, goal=goal, tasks=tasks)
            existing = _read_index_bytes(
                locked,
                locked.layout.initial_plan_file,
                lease=lease,
                optional=True,
            )
            target = _write_index_bytes_locked(
                locked,
                locked.layout.initial_plan_file,
                serialize_initial_plan(plan).encode("utf-8"),
                expected_current=existing,
                lease=lease,
            )
            committed = _read_index_bytes(
                locked,
                target,
                lease=lease,
                optional=False,
            )
            assert committed is not None
            if parse_initial_plan(
                committed, project_id=locked.project_id
            ) != plan:
                raise InitialPlanStorageError(
                    "committed initial plan differs from validated input"
                )
            _assert_snapshot_unchanged(
                locked,
                manifest_bytes=manifest_bytes,
                project_bytes=project_bytes,
            )
            return target
    except InitialPlanningError:
        raise
    except (StableFileAccessError, OSError, TypeError, ValueError) as exc:
        raise InitialPlanStorageError(
            "initial plan could not be written safely"
        ) from exc


def load_initial_plan(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = 5.0,
) -> InitialPlan:
    """Load one current plan and fail closed on stale state/Goal/task bindings."""

    root = Path(workspace_root).expanduser().resolve()
    registration = _load_registration(root, project_id)
    try:
        with exclusive_stable_file_lock(
            registration.layout.indexes_dir,
            registration.layout.machine_state_lock_file,
            timeout_seconds=lock_timeout_seconds,
        ) as lease:
            locked = _load_registration(root, project_id)
            manifest, manifest_bytes, manifest_sha256 = manifest_snapshot(locked)
            state, _state_bytes, project_bytes = _load_current_state_locked(
                locked,
                lease=lease,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
            )
            goal, goal_bytes = _load_goal_locked(locked, lease=lease)
            tasks, task_bytes = _load_tasks_locked(locked, lease=lease)
            if goal is None or tasks is None:
                raise InitialPlanningError(
                    "a current Goal and task collection are required to load the plan"
                )
            _assert_state_planning_inputs(
                state,
                goal=goal,
                goal_bytes=goal_bytes,
                tasks=tasks,
                task_bytes=task_bytes,
            )
            raw = _read_index_bytes(
                locked,
                locked.layout.initial_plan_file,
                lease=lease,
                optional=True,
            )
            if raw is None:
                raise InitialPlanNotFoundError(
                    f"no initial plan exists for {locked.project_id}"
                )
            plan = parse_initial_plan(raw, project_id=locked.project_id)
            _assert_plan_bindings(plan, state=state, goal=goal, tasks=tasks)
            _assert_snapshot_unchanged(
                locked,
                manifest_bytes=manifest_bytes,
                project_bytes=project_bytes,
            )
            return plan
    except InitialPlanningError:
        raise
    except (StableFileAccessError, OSError, TypeError, ValueError) as exc:
        raise InitialPlanStorageError(
            "initial plan could not be loaded safely"
        ) from exc

def generate_initial_plan(
    workspace_root: str | Path,
    project_id: str,
    *,
    objective: str | None = None,
    generated_at: object | None = None,
    plan_date: object | None = None,
    lock_timeout_seconds: float = 5.0,
) -> InitialPlanningResult:
    """Generate missing draft Goal/tasks and publish a current initial plan.

    Existing Goal and task artifacts are parsed strictly and never rewritten.
    Generated tasks remain ``draft`` and therefore never authorize execution.
    """

    root = Path(workspace_root).expanduser().resolve()
    try:
        normalized_project_id = validate_project_id(project_id)
    except (LayoutError, TypeError, ValueError) as exc:
        raise InitialPlanningError(f"invalid project_id: {exc}") from exc
    timestamp = _timestamp(generated_at)
    normalized_date = _plan_date(plan_date, generated_at=timestamp)
    normalized_objective = (
        _text(objective, label="objective", maximum=8000)
        if objective is not None
        else None
    )

    state_created, state_refreshed = _ensure_state_exists(
        root,
        normalized_project_id,
        generated_at=timestamp,
        lock_timeout_seconds=lock_timeout_seconds,
    )
    goal_created, tasks_created = _create_missing_planning_artifacts(
        root,
        normalized_project_id,
        objective=normalized_objective,
        generated_at=timestamp,
        plan_date=normalized_date,
        lock_timeout_seconds=lock_timeout_seconds,
    )
    state_rebuilt = state_refreshed
    if goal_created or tasks_created:
        try:
            generate_project_state(
                root,
                normalized_project_id,
                generated_at=timestamp,
                lock_timeout_seconds=lock_timeout_seconds,
            )
        except ProjectStateError as exc:
            raise InitialPlanningError(
                f"project-state could not bind generated drafts: {exc}"
            ) from exc
        state_rebuilt = True

    registration = _load_registration(root, normalized_project_id)
    try:
        with exclusive_stable_file_lock(
            registration.layout.indexes_dir,
            registration.layout.machine_state_lock_file,
            timeout_seconds=lock_timeout_seconds,
        ) as lease:
            locked = _load_registration(root, normalized_project_id)
            manifest, manifest_bytes, manifest_sha256 = manifest_snapshot(locked)
            state, _state_bytes, project_bytes = _load_current_state_locked(
                locked,
                lease=lease,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
            )
            goal, goal_bytes = _load_goal_locked(locked, lease=lease)
            tasks, task_bytes = _load_tasks_locked(locked, lease=lease)
            if goal is None or tasks is None:  # pragma: no cover - defensive
                raise InitialPlanningError(
                    "initial planning did not produce Goal and task artifacts"
                )
            _assert_state_planning_inputs(
                state,
                goal=goal,
                goal_bytes=goal_bytes,
                tasks=tasks,
                task_bytes=task_bytes,
            )
            plan = _build_initial_plan(
                locked,
                state=state,
                goal=goal,
                tasks=tasks,
                generated_at=timestamp,
                plan_date=normalized_date,
            )
            existing = _read_index_bytes(
                locked,
                locked.layout.initial_plan_file,
                lease=lease,
                optional=True,
            )
            target = _write_index_bytes_locked(
                locked,
                locked.layout.initial_plan_file,
                serialize_initial_plan(plan).encode("utf-8"),
                expected_current=existing,
                lease=lease,
            )
            committed_bytes = _read_index_bytes(
                locked,
                target,
                lease=lease,
                optional=False,
            )
            assert committed_bytes is not None
            committed = parse_initial_plan(
                committed_bytes,
                project_id=locked.project_id,
            )
            _assert_plan_bindings(
                committed,
                state=state,
                goal=goal,
                tasks=tasks,
            )
            if committed != plan:
                raise InitialPlanStorageError(
                    "committed initial plan differs from deterministic output"
                )
            _assert_snapshot_unchanged(
                locked,
                manifest_bytes=manifest_bytes,
                project_bytes=project_bytes,
            )
            return InitialPlanningResult(
                project_id=locked.project_id,
                initial_plan=committed,
                goal=goal,
                tasks=tasks,
                goal_created=goal_created,
                tasks_created=tasks_created,
                state_created=state_created,
                state_rebuilt=state_rebuilt,
            )
    except InitialPlanningError:
        raise
    except (StableFileAccessError, OSError, TypeError, ValueError) as exc:
        raise InitialPlanStorageError(
            "initial plan could not be generated safely"
        ) from exc


__all__ = [
    "INITIAL_PLAN_SCHEMA_VERSION",
    "INITIAL_PLAN_KIND",
    "INITIAL_PLAN_VERSION",
    "INITIAL_PLAN_STATUS",
    "INITIAL_PLAN_MACHINE_FILENAME",
    "INITIAL_PLAN_RELATIVE_PATH",
    "InitialPlanningError",
    "UnsupportedInitialPlanSchemaVersionError",
    "InitialPlanNotFoundError",
    "InitialPlanStorageError",
    "InitialPlan",
    "InitialPlanningResult",
    "parse_initial_plan",
    "serialize_initial_plan",
    "load_initial_plan",
    "write_initial_plan",
    "generate_initial_plan",
]