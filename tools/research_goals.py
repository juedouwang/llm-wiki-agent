#!/usr/bin/env python3
"""Strict project Goal/Milestone schema and registered storage boundary (I-01)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from tools.knowledge_artifacts import (
    KNOWLEDGE_PAGE_KIND, KNOWLEDGE_SCHEMA_VERSION, KnowledgeArtifactError,
    parse_knowledge_page, serialize_knowledge_frontmatter,
)
from tools.project_layout import (
    CURRENT_SCHEMA_VERSION, LayoutError, parse_json_bytes_strict, validate_project_id,
)
from tools.project_registry import ProjectRegistrationResult, load_registered_project
from tools.stable_file_access import (
    StableFileAccessError, StableFileMissingError, exclusive_stable_file_lock,
    read_stable_regular_file, write_atomic_stable_file,
)

GOAL_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
GOAL_KIND = "llmwiki-research-goal"
GOAL_VERSION = "research-goal-v1"
GOAL_MACHINE_FILENAME = "goals.json"
GOAL_MARKDOWN_PATH = "goals.md"
GOAL_STATUSES = frozenset({"draft", "active", "blocked", "completed", "cancelled"})
MILESTONE_STATUSES = frozenset({"draft", "pending", "active", "blocked", "completed", "cancelled"})
_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_RFC3339_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})")
_GOAL_FIELDS = frozenset({"schema_version", "kind", "goal_version", "project_id", "goal_id", "goal", "success_criteria", "deadline", "current_stage", "dependencies", "milestones", "status", "draft_reasons", "created_at", "updated_at"})
_MILESTONE_FIELDS = frozenset({"milestone_id", "title", "success_criteria", "deadline", "current_stage", "dependencies", "status"})
_BLOCK_START = "<!-- llmwiki:goal-schema-v1:start -->"
_BLOCK_END = "<!-- llmwiki:goal-schema-v1:end -->"

class GoalSchemaError(ValueError):
    """Closed-schema validation failure."""
class UnsupportedGoalSchemaVersionError(GoalSchemaError):
    """Unsupported legacy/future Goal schema."""
class GoalStorageError(GoalSchemaError):
    """Unsafe/unavailable registered storage."""
class GoalNotFoundError(GoalStorageError):
    """No current Goal artifact/page exists."""

def _timestamp(value: object | None = None) -> str:
    if value is None:
        current = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, date):
        current = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, str) and _RFC3339_RE.fullmatch(value.strip()):
        try:
            current = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise GoalSchemaError("timestamp must be valid RFC3339") from exc
    else:
        raise GoalSchemaError("timestamp must be RFC3339/date/datetime/null")
    if current.tzinfo is None:
        raise GoalSchemaError("timestamp requires an explicit timezone")
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _parse_timestamp(value: object, name: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or _RFC3339_RE.fullmatch(value) is None:
        raise GoalSchemaError(f"{name} must be canonical UTC RFC3339")
    canonical = _timestamp(value)
    if canonical != value:
        raise GoalSchemaError(f"{name} must be canonical UTC RFC3339 seconds")
    return canonical, datetime.fromisoformat(canonical.replace("Z", "+00:00"))

def _text(value: object, name: str, *, optional: bool = False, maximum: int = 4000) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise GoalSchemaError(f"{name} must be a non-empty string" + (" or null" if optional else ""))
    normalized = value.strip()
    if "\x00" in normalized or len(normalized) > maximum:
        raise GoalSchemaError(f"{name} is invalid or too long")
    return normalized

def _ident(value: object, name: str) -> str:
    text = _text(value, name, maximum=64)
    assert text is not None
    if _ID_RE.fullmatch(text) is None:
        raise GoalSchemaError(f"{name} must be lowercase and path-safe")
    return text

def _items(value: object, name: str, *, identifiers: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise GoalSchemaError(f"{name} must be an array")
    if len(value) > 256:
        raise GoalSchemaError(f"{name} has too many entries")
    result: list[str] = []
    for index, item in enumerate(value):
        normalized = _ident(item, f"{name}[{index}]") if identifiers else _text(item, f"{name}[{index}]", maximum=2000)
        assert normalized is not None
        if normalized in result:
            raise GoalSchemaError(f"{name} must be duplicate-free")
        result.append(normalized)
    return tuple(result)

def _date(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GoalSchemaError(f"{name} must use YYYY-MM-DD or null")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise GoalSchemaError(f"{name} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise GoalSchemaError(f"{name} must use canonical YYYY-MM-DD")
    return value

def _fields(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    missing, unknown = sorted(expected - frozenset(value)), sorted(frozenset(value) - expected)
    if missing or unknown:
        details = (["missing " + ", ".join(missing)] if missing else []) + (["unknown " + ", ".join(unknown)] if unknown else [])
        raise GoalSchemaError(f"{label} fields are invalid: {'; '.join(details)}")

@dataclass(frozen=True)
class Milestone:
    milestone_id: str
    title: str
    success_criteria: tuple[str, ...] = field(default_factory=tuple)
    deadline: str | None = None
    current_stage: str | None = None
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    status: str = "draft"

    def __post_init__(self) -> None:
        object.__setattr__(self, "milestone_id", _ident(self.milestone_id, "milestone_id"))
        object.__setattr__(self, "title", _text(self.title, "milestone.title", maximum=500))
        object.__setattr__(self, "success_criteria", _items(self.success_criteria, "milestone.success_criteria"))
        object.__setattr__(self, "deadline", _date(self.deadline, "milestone.deadline"))
        object.__setattr__(self, "current_stage", _text(self.current_stage, "milestone.current_stage", optional=True, maximum=500))
        object.__setattr__(self, "dependencies", _items(self.dependencies, "milestone.dependencies", identifiers=True))
        if self.milestone_id in self.dependencies:
            raise GoalSchemaError("a milestone cannot depend on itself")
        if not isinstance(self.status, str) or self.status not in MILESTONE_STATUSES:
            raise GoalSchemaError("milestone.status is not in the closed status set")
        if self.status != "draft" and (not self.success_criteria or self.deadline is None or self.current_stage is None):
            raise GoalSchemaError("a non-draft milestone requires criteria, deadline, and current_stage")

    def as_dict(self) -> dict[str, object]:
        return {"milestone_id": self.milestone_id, "title": self.title, "success_criteria": list(self.success_criteria), "deadline": self.deadline, "current_stage": self.current_stage, "dependencies": list(self.dependencies), "status": self.status}

    @classmethod
    def from_dict(cls, value: object) -> "Milestone":
        if not isinstance(value, Mapping):
            raise GoalSchemaError("milestone must be an object")
        _fields(value, _MILESTONE_FIELDS, "milestone")
        return cls(milestone_id=value["milestone_id"], title=value["title"], success_criteria=_items(value["success_criteria"], "milestone.success_criteria"), deadline=_date(value["deadline"], "milestone.deadline"), current_stage=_text(value["current_stage"], "milestone.current_stage", optional=True, maximum=500), dependencies=_items(value["dependencies"], "milestone.dependencies", identifiers=True), status=value["status"])  # type: ignore[arg-type]


@dataclass(frozen=True)
class Goal:
    """Schema v1 goal; incomplete user fields are legal only for ``draft``."""
    project_id: str
    goal: str | None
    success_criteria: tuple[str, ...] = field(default_factory=tuple)
    deadline: str | None = None
    current_stage: str | None = None
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    milestones: tuple[Milestone, ...] = field(default_factory=tuple)
    status: str = "draft"
    goal_id: str = "primary"
    draft_reasons: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "project_id", validate_project_id(self.project_id))
        except LayoutError as exc:
            raise GoalSchemaError(f"invalid project_id: {exc}") from exc
        object.__setattr__(self, "goal_id", _ident(self.goal_id, "goal_id"))
        object.__setattr__(self, "goal", _text(self.goal, "goal", optional=True, maximum=8000))
        object.__setattr__(self, "success_criteria", _items(self.success_criteria, "success_criteria"))
        object.__setattr__(self, "deadline", _date(self.deadline, "deadline"))
        object.__setattr__(self, "current_stage", _text(self.current_stage, "current_stage", optional=True, maximum=1000))
        object.__setattr__(self, "dependencies", _items(self.dependencies, "dependencies", identifiers=True))
        if self.goal_id in self.dependencies:
            raise GoalSchemaError("a goal cannot depend on itself")
        if not isinstance(self.milestones, Sequence) or isinstance(self.milestones, (str, bytes, bytearray)):
            raise GoalSchemaError("milestones must be an array")
        normalized: list[Milestone] = []
        seen: set[str] = set()
        for value in self.milestones:
            item = value if isinstance(value, Milestone) else Milestone.from_dict(value)
            if item.milestone_id in seen:
                raise GoalSchemaError("milestone IDs must be duplicate-free")
            seen.add(item.milestone_id)
            normalized.append(item)
        object.__setattr__(self, "milestones", tuple(normalized))
        if not isinstance(self.status, str) or self.status not in GOAL_STATUSES:
            raise GoalSchemaError("status is not in the closed Goal status set")
        object.__setattr__(self, "draft_reasons", _items(self.draft_reasons, "draft_reasons"))
        created, created_dt = _parse_timestamp(self.created_at, "created_at")
        updated, updated_dt = _parse_timestamp(self.updated_at, "updated_at")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        if updated_dt < created_dt:
            raise GoalSchemaError("updated_at must not precede created_at")
        if self.status != "draft" and self.missing_user_fields:
            raise GoalSchemaError("a non-draft goal requires goal, criteria, deadline, and current_stage")
        if self.status != "draft" and self.draft_reasons:
            raise GoalSchemaError("non-draft goals cannot carry draft_reasons")

    @property
    def missing_user_fields(self) -> tuple[str, ...]:
        missing: list[str] = []
        if self.goal is None:
            missing.append("goal")
        if not self.success_criteria:
            missing.append("success_criteria")
        if self.deadline is None:
            missing.append("deadline")
        if self.current_stage is None:
            missing.append("current_stage")
        return tuple(missing)

    @property
    def is_draft(self) -> bool:
        return self.status == "draft"

    @property
    def display_status(self) -> str:
        return self.status.upper()

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": GOAL_SCHEMA_VERSION, "kind": GOAL_KIND,
            "goal_version": GOAL_VERSION, "project_id": self.project_id,
            "goal_id": self.goal_id, "goal": self.goal,
            "success_criteria": list(self.success_criteria), "deadline": self.deadline,
            "current_stage": self.current_stage, "dependencies": list(self.dependencies),
            "milestones": [item.as_dict() for item in self.milestones],
            "status": self.status, "draft_reasons": list(self.draft_reasons),
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def draft(cls, project_id: str, *, goal: str | None = None,
              success_criteria: Iterable[str] = (), deadline: str | None = None,
              current_stage: str | None = None, dependencies: Iterable[str] = (),
              milestones: Iterable[Milestone] = (), goal_id: str = "primary",
              draft_reasons: Iterable[str] = (), created_at: object | None = None,
              updated_at: object | None = None) -> "Goal":
        timestamp = _timestamp(created_at)
        provisional = cls(project_id=project_id, goal=goal,
            success_criteria=tuple(success_criteria), deadline=deadline,
            current_stage=current_stage, dependencies=tuple(dependencies),
            milestones=tuple(milestones), status="draft", goal_id=goal_id,
            draft_reasons=tuple(draft_reasons), created_at=timestamp,
            updated_at=_timestamp(updated_at if updated_at is not None else timestamp))
        if provisional.draft_reasons:
            return provisional
        reasons = tuple(f"missing-{name}" for name in provisional.missing_user_fields)
        return cls(project_id=provisional.project_id, goal=provisional.goal,
            success_criteria=provisional.success_criteria, deadline=provisional.deadline,
            current_stage=provisional.current_stage, dependencies=provisional.dependencies,
            milestones=provisional.milestones, status="draft", goal_id=provisional.goal_id,
            draft_reasons=reasons or ("awaiting-user-confirmation",),
            created_at=provisional.created_at, updated_at=provisional.updated_at)

    @classmethod
    def from_dict(cls, value: object) -> "Goal":
        if not isinstance(value, Mapping):
            raise GoalSchemaError("goal document must be an object")
        _fields(value, _GOAL_FIELDS, "goal")
        version = value["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise GoalSchemaError("schema_version must be an integer")
        if version != GOAL_SCHEMA_VERSION:
            raise UnsupportedGoalSchemaVersionError(f"goal schema_version {version} is not supported")
        if value["kind"] != GOAL_KIND or value["goal_version"] != GOAL_VERSION:
            raise GoalSchemaError("goal kind/version is not supported")
        raw_milestones = value["milestones"]
        if not isinstance(raw_milestones, Sequence) or isinstance(raw_milestones, (str, bytes, bytearray)):
            raise GoalSchemaError("milestones must be an array")
        return cls(project_id=value["project_id"], goal=value["goal"],
            success_criteria=_items(value["success_criteria"], "success_criteria"),
            deadline=_date(value["deadline"], "deadline"),
            current_stage=_text(value["current_stage"], "current_stage", optional=True, maximum=1000),
            dependencies=_items(value["dependencies"], "dependencies", identifiers=True),
            milestones=tuple(Milestone.from_dict(item) for item in raw_milestones),
            status=value["status"], goal_id=value["goal_id"],
            draft_reasons=_items(value["draft_reasons"], "draft_reasons"),
            created_at=value["created_at"], updated_at=value["updated_at"])  # type: ignore[arg-type]

def serialize_goal(goal: Goal) -> str:
    if not isinstance(goal, Goal):
        raise GoalSchemaError("goal must be a Goal")
    return json.dumps(goal.as_dict(), ensure_ascii=False, indent=2, sort_keys=False, allow_nan=False) + "\n"

def _requested_project_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return validate_project_id(value)
    except (LayoutError, TypeError, ValueError) as exc:
        raise GoalSchemaError("requested project_id is invalid") from exc


def parse_goal(
    payload: str | bytes | Mapping[str, object],
    *,
    project_id: str | None = None,
) -> Goal:
    expected_project = _requested_project_id(project_id)
    if isinstance(payload, bytes):
        raw = payload
    elif isinstance(payload, str):
        try:
            raw = payload.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise GoalSchemaError("goal JSON text is not valid UTF-8") from exc
    elif isinstance(payload, Mapping):
        goal = Goal.from_dict(dict(payload))
        if expected_project is not None and goal.project_id != expected_project:
            raise GoalSchemaError("goal project_id does not match the requested project")
        return goal
    else:
        raise GoalSchemaError("goal JSON must be UTF-8 bytes, text, or an object")
    try:
        decoded = parse_json_bytes_strict(raw, label="goal JSON")
    except (LayoutError, TypeError, UnicodeError, ValueError) as exc:
        raise GoalSchemaError(str(exc)) from exc
    goal = Goal.from_dict(decoded)
    if expected_project is not None and goal.project_id != expected_project:
        raise GoalSchemaError("goal project_id does not match the requested project")
    return goal


def _md_value(value: str | None, missing: str = "DRAFT - awaiting user input") -> str:
    return value if value is not None else missing

def goal_to_markdown(goal: Goal, *, path: str = GOAL_MARKDOWN_PATH,
                     rendered_at: object | None = None,
                     title: str = "Research goals and milestones",
                     ownership: str = "mixed", user_region: str = "\n") -> str:
    """Render a strict Knowledge Schema v2 ``goals.md`` page."""
    if not isinstance(goal, Goal) or path != GOAL_MARKDOWN_PATH:
        raise GoalSchemaError("goal and canonical goals.md path are required")
    if ownership not in {"generated", "mixed", "user"}:
        raise GoalSchemaError("ownership must be generated, mixed, or user")
    if not isinstance(user_region, str) or "<!-- llmwiki:user-region:" in user_region:
        raise GoalSchemaError("user_region contains a reserved marker")
    timestamp = _timestamp(rendered_at if rendered_at is not None else goal.updated_at)
    status = "draft" if goal.status == "draft" else "verified"
    frontmatter = {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION, "kind": KNOWLEDGE_PAGE_KIND,
        "project_id": goal.project_id, "artifact_type": "goal",
        "title": _text(title, "title", maximum=500), "status": status,
        "ownership": ownership, "source_ids": [], "evidence_refs": [],
        "generated_at": goal.created_at, "updated_at": timestamp,
        "last_verified_at": timestamp if status == "verified" else None,
    }
    criteria = "\n".join(f"- {item}" for item in goal.success_criteria) or "- DRAFT - awaiting user input"
    dependencies = "\n".join(f"- `{item}`" for item in goal.dependencies) or "- None declared"
    milestone_blocks: list[str] = []
    for item in goal.milestones:
        criteria_lines = "\n".join(f"  - {criterion}" for criterion in item.success_criteria) or "  - DRAFT - awaiting user input"
        milestone_blocks.append(
            f"### {item.milestone_id}: {item.title}\n\n"
            f"- **Status:** `{item.status.upper()}`\n"
            f"- **Current stage:** {_md_value(item.current_stage)}\n"
            f"- **Deadline:** {_md_value(item.deadline, 'DRAFT - not supplied')}\n"
            "- **Success criteria:**\n" + criteria_lines + "\n"
            "- **Dependencies:** " + (", ".join(f"`{x}`" for x in item.dependencies) or "None declared") + "\n"
        )
    milestones = "\n".join(milestone_blocks) if milestone_blocks else "- No milestones declared.\n"
    machine_json = serialize_goal(goal).rstrip("\n")
    body = (
        "# Research goals and milestones\n\n"
        f"> **{goal.display_status}** - this page does not imply user confirmation beyond the recorded Goal status.\n\n"
        "## Goal\n\n" + _md_value(goal.goal) + "\n\n"
        "## Success criteria\n\n" + criteria + "\n\n"
        "## Current stage and deadline\n\n"
        f"- **Current stage:** {_md_value(goal.current_stage)}\n"
        f"- **Deadline:** {_md_value(goal.deadline, 'DRAFT - not supplied')}\n\n"
        "## Dependencies\n\n" + dependencies + "\n\n"
        "## Milestones\n\n" + milestones + "\n"
        f"{_BLOCK_START}\n```json\n{machine_json}\n```\n{_BLOCK_END}\n"
    )
    if ownership == "mixed":
        body += (
            "\n## User notes and confirmations\n\n"
            '<!-- llmwiki:user-region:start id="user-goals" -->\n'
            + user_region +
            '<!-- llmwiki:user-region:end id="user-goals" -->\n'
        )
    return serialize_knowledge_frontmatter(frontmatter, path=path) + body

def goal_from_markdown(payload: bytes | str, *, path: str = GOAL_MARKDOWN_PATH,
                       project_id: str | None = None) -> Goal:
    if isinstance(payload, str):
        raw = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raise GoalSchemaError("Goal Markdown must be UTF-8 bytes or text")
    try:
        page = parse_knowledge_page(raw, path=path)
    except (KnowledgeArtifactError, TypeError, ValueError, UnicodeError) as exc:
        raise GoalSchemaError(f"invalid Goal Markdown: {exc}") from exc
    if page.frontmatter.schema_version != KNOWLEDGE_SCHEMA_VERSION or page.frontmatter.artifact_type != "goal":
        raise GoalSchemaError("Goal Markdown must be current Schema v2 artifact_type goal")
    expected_project = (
        _requested_project_id(project_id)
        if project_id is not None
        else page.frontmatter.project_id
    )
    assert expected_project is not None
    if page.frontmatter.project_id != expected_project:
        raise GoalSchemaError("Goal Markdown project_id does not match requested project")
    if page.body.count(_BLOCK_START) != 1 or page.body.count(_BLOCK_END) != 1:
        raise GoalSchemaError("Goal Markdown must contain exactly one Goal Schema block")
    start_token = _BLOCK_START + "\n```json\n"
    end_token = "\n```\n" + _BLOCK_END
    start = page.body.find(start_token)
    if start < 0:
        raise GoalSchemaError("Goal Schema block start is malformed")
    json_start = start + len(start_token)
    end = page.body.find(end_token, json_start)
    if end < 0:
        raise GoalSchemaError("Goal Schema block end is malformed")
    goal = parse_goal(page.body[json_start:end], project_id=expected_project)
    expected_status = "draft" if goal.status == "draft" else "verified"
    if page.frontmatter.status != expected_status:
        raise GoalSchemaError("frontmatter status does not match embedded Goal status")
    return goal

@dataclass(frozen=True)
class GoalStore:
    """All writes are constrained to a registered project's machine/knowledge roots."""
    workspace_root: Path
    project_id: str
    registration: ProjectRegistrationResult = field(init=False, repr=False)

    def __post_init__(self) -> None:
        root = Path(self.workspace_root).expanduser().resolve()
        object.__setattr__(self, "workspace_root", root)
        try:
            registration = load_registered_project(root, self.project_id)
        except (LayoutError, OSError, TypeError, ValueError) as exc:
            raise GoalStorageError("registered project could not be loaded safely") from exc
        object.__setattr__(self, "project_id", registration.project_id)
        object.__setattr__(self, "registration", registration)

    @property
    def machine_path(self) -> Path:
        try:
            return self.registration.layout.validate_machine_state_path(
                self.registration.layout.goals_file,
                allow_missing_leaf=True,
            )
        except LayoutError as exc:
            raise GoalStorageError("Goal machine path escapes machine state") from exc

    @property
    def markdown_path(self) -> Path:
        root = self.registration.layout.knowledge_root.resolve()
        target = (root / GOAL_MARKDOWN_PATH).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise GoalStorageError("Goal Markdown path escapes knowledge root") from exc
        return target

    def load(self) -> Goal:
        try:
            observation = read_stable_regular_file(self.registration.layout.indexes_dir, self.machine_path, reject_redirection=True, capture_bytes=True)
        except StableFileMissingError as exc:
            raise GoalNotFoundError(f"no Goal artifact exists for {self.project_id}") from exc
        except StableFileAccessError as exc:
            raise GoalStorageError("Goal artifact could not be read safely") from exc
        if observation.data is None:
            raise GoalStorageError("Goal artifact bytes were not captured")
        return parse_goal(observation.data, project_id=self.project_id)

    def write(self, goal: Goal, *, lock_timeout_seconds: float = 5.0) -> Path:
        if not isinstance(goal, Goal) or goal.project_id != self.project_id:
            raise GoalStorageError("Goal must belong to the registered project")
        payload = serialize_goal(goal).encode("utf-8")
        try:
            with exclusive_stable_file_lock(
                self.registration.layout.indexes_dir,
                self.registration.layout.machine_state_lock_file,
                timeout_seconds=lock_timeout_seconds,
            ) as lease:
                locked = load_registered_project(self.workspace_root, self.project_id)
                if (
                    locked.layout.machine_root != self.registration.layout.machine_root
                    or locked.layout.knowledge_root != self.registration.layout.knowledge_root
                ):
                    raise GoalStorageError("project registration changed while writing Goal")
                target = locked.layout.validate_machine_state_path(
                    locked.layout.goals_file,
                    allow_missing_leaf=True,
                )
                write_atomic_stable_file(
                    locked.layout.indexes_dir,
                    target,
                    payload,
                    root_lease=lease,
                )
        except GoalStorageError:
            raise
        except (StableFileAccessError, LayoutError, OSError, TypeError, ValueError) as exc:
            raise GoalStorageError("Goal artifact could not be written safely") from exc
        return target

    def load_markdown(self) -> Goal:
        try:
            observation = read_stable_regular_file(
                self.registration.layout.knowledge_root,
                self.markdown_path,
                reject_redirection=True,
                capture_bytes=True,
            )
        except StableFileMissingError as exc:
            raise GoalNotFoundError(f"no goals.md page exists for {self.project_id}") from exc
        except StableFileAccessError as exc:
            raise GoalStorageError("goals.md could not be read safely") from exc
        if observation.data is None:
            raise GoalStorageError("goals.md bytes were not captured")
        return goal_from_markdown(observation.data, project_id=self.project_id)

def write_goal(workspace_root: str | Path, project_id: str, goal: Goal, *, lock_timeout_seconds: float = 5.0) -> Path:
    return GoalStore(Path(workspace_root), project_id).write(goal, lock_timeout_seconds=lock_timeout_seconds)

def load_goal(workspace_root: str | Path, project_id: str) -> Goal:
    return GoalStore(Path(workspace_root), project_id).load()

deserialize_goal = parse_goal
markdown_to_goal = goal_from_markdown
render_goal_markdown = goal_to_markdown
