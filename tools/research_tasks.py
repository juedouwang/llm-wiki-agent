#!/usr/bin/env python3
"""Strict, executable research-task protocol and registered storage (I-02).

The protocol is deliberately stricter than a generic todo list.  A current task
always carries an explicit reason, bounded inputs and write scope, a Definition
of Done, verification steps, and expected artifacts.  Legacy todo records are
available only through the explicit compatibility reader and can never be
serialized as current tasks without a caller-controlled migration.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from tools.knowledge_artifacts import (
    KNOWLEDGE_PAGE_KIND,
    KNOWLEDGE_SCHEMA_VERSION,
    KnowledgeArtifactError,
    parse_knowledge_page,
    serialize_knowledge_frontmatter,
)
from tools.project_layout import (
    CURRENT_SCHEMA_VERSION,
    LayoutError,
    parse_json_bytes_strict,
    validate_project_id,
)
from tools.project_registry import ProjectRegistrationResult, load_registered_project
from tools.stable_file_access import (
    StableFileAccessError,
    StableFileMissingError,
    exclusive_stable_file_lock,
    read_stable_regular_file,
    write_atomic_stable_file,
)


TASK_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
TASK_KIND = "llmwiki-research-tasks"
TASK_VERSION = "research-task-v1"
TASK_MACHINE_FILENAME = "tasks.json"
TASK_MARKDOWN_PATH = "plans/backlog.md"
TASK_STATUSES = frozenset(
    {"draft", "ready", "in_progress", "blocked", "completed", "cancelled"}
)
_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_EVIDENCE_RE = re.compile(r"evd-[0-9a-f]{64}")
_RFC3339_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:")
_LOGICAL_ARTIFACT_RE = re.compile(
    r"(?:artifact|evidence|git|knowledge|machine|run|test|user-confirmation):"
    r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,511}"
)
_COMPLETION_REF_RE = re.compile(
    r"(?:artifact|evidence|file|git|run|test|user-confirmation):"
    r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,511}"
)
_TASK_FIELDS = frozenset(
    {
        "task_id",
        "title",
        "why_now",
        "inputs",
        "evidence",
        "allowed_paths",
        "denied_paths",
        "dependencies",
        "dod",
        "verification",
        "artifacts",
        "timebox_minutes",
        "status",
        "completion_refs",
        "draft_reasons",
        "created_at",
        "updated_at",
    }
)
_DOCUMENT_FIELDS = frozenset(
    {"schema_version", "kind", "task_version", "project_id", "tasks", "updated_at"}
)
_INDIVIDUAL_FIELDS = frozenset(
    {"schema_version", "kind", "task_version", "project_id", *_TASK_FIELDS}
)
_BLOCK_START = "<!-- llmwiki:task-protocol-v1:start -->"
_BLOCK_END = "<!-- llmwiki:task-protocol-v1:end -->"
_USER_START = '<!-- llmwiki:user-region:start id="user-backlog" -->'
_USER_END = '<!-- llmwiki:user-region:end id="user-backlog" -->'


class TaskSchemaError(ValueError):
    """Closed-schema or executable-task validation failure."""


class UnsupportedTaskSchemaVersionError(TaskSchemaError):
    """A legacy/future task artifact was passed to the current reader."""


class TaskStorageError(TaskSchemaError):
    """Unsafe or unavailable registered task storage."""


class TaskNotFoundError(TaskStorageError):
    """No current task artifact/page exists."""


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
            raise TaskSchemaError("timestamp must be valid RFC3339") from exc
    else:
        raise TaskSchemaError("timestamp must be RFC3339/date/datetime/null")
    if current.tzinfo is None:
        raise TaskSchemaError("timestamp requires an explicit timezone")
    return (
        current.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_timestamp(value: object, name: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or _RFC3339_RE.fullmatch(value) is None:
        raise TaskSchemaError(f"{name} must be canonical UTC RFC3339")
    canonical = _timestamp(value)
    if canonical != value:
        raise TaskSchemaError(f"{name} must be canonical UTC RFC3339 seconds")
    return canonical, datetime.fromisoformat(canonical.replace("Z", "+00:00"))


def _text(
    value: object,
    name: str,
    *,
    optional: bool = False,
    maximum: int = 4000,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        suffix = " or null" if optional else ""
        raise TaskSchemaError(f"{name} must be a non-empty string{suffix}")
    normalized = value.strip()
    if "\x00" in normalized or len(normalized) > maximum:
        raise TaskSchemaError(f"{name} is invalid or too long")
    return normalized


def _ident(value: object, name: str) -> str:
    text = _text(value, name, maximum=64)
    assert text is not None
    if _ID_RE.fullmatch(text) is None:
        raise TaskSchemaError(f"{name} must be lowercase and path-safe")
    return text


def _items(
    values: object,
    name: str,
    *,
    required: bool = False,
    maximum_items: int = 256,
    maximum_text: int = 2000,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TaskSchemaError(f"{name} must be an array of strings")
    result = tuple(
        _text(value, f"{name} entry", maximum=maximum_text) for value in values
    )
    if any(value is None for value in result):  # pragma: no cover - defensive
        raise TaskSchemaError(f"{name} contains an invalid entry")
    normalized = tuple(value for value in result if value is not None)
    if required and not normalized:
        raise TaskSchemaError(f"{name} must contain at least one entry")
    if len(normalized) > maximum_items or len(set(normalized)) != len(normalized):
        raise TaskSchemaError(f"{name} must be bounded and duplicate-free")
    return normalized


def _fields(value: Mapping[str, object], expected: frozenset[str], name: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise TaskSchemaError(
            f"{name} fields must be exact; missing={missing}, unknown={unknown}"
        )


def _safe_project_pattern(value: object, name: str) -> str:
    text = _text(value, name, maximum=512)
    assert text is not None
    if (
        "\\" in text
        or text.startswith("/")
        or _WINDOWS_DRIVE_RE.match(text)
        or "//" in text
    ):
        raise TaskSchemaError(f"{name} must be a project-relative POSIX path/glob")
    segments = text.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise TaskSchemaError(f"{name} cannot contain empty, '.' or '..' segments")
    if any("\x00" in segment or ":" in segment for segment in segments):
        raise TaskSchemaError(f"{name} contains an unsafe path segment")
    return text


def _path_items(values: object, name: str, *, required: bool = False) -> tuple[str, ...]:
    raw = _items(values, name, required=required, maximum_text=512)
    return tuple(_safe_project_pattern(value, f"{name} entry") for value in raw)


def _artifact_ref(value: object) -> str:
    text = _text(value, "artifacts entry", maximum=512)
    assert text is not None
    if _LOGICAL_ARTIFACT_RE.fullmatch(text):
        return text
    return _safe_project_pattern(text, "artifacts entry")


def _completion_ref(value: object) -> str:
    text = _text(value, "completion_refs entry", maximum=512)
    assert text is not None
    if _COMPLETION_REF_RE.fullmatch(text) is None:
        raise TaskSchemaError(
            "completion_refs entries require a controlled evidence namespace"
        )
    if text.startswith("evidence:") and _EVIDENCE_RE.fullmatch(text[9:]) is None:
        raise TaskSchemaError("evidence completion refs require a canonical Evidence ID")
    return text


def _normalize_evidence(values: object) -> tuple[str, ...]:
    evidence = _items(values, "evidence", maximum_text=68)
    if any(_EVIDENCE_RE.fullmatch(value) is None for value in evidence):
        raise TaskSchemaError(
            "evidence entries must be 'evd-' plus 64 lowercase hexadecimal digits"
        )
    return evidence


@dataclass(frozen=True)
class ResearchTask:
    """One current executable task, or a read-only legacy compatibility view."""

    project_id: str
    task_id: str
    title: str
    why_now: str | None
    inputs: tuple[str, ...]
    evidence: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    denied_paths: tuple[str, ...]
    dependencies: tuple[str, ...]
    dod: tuple[str, ...]
    verification: tuple[str, ...]
    artifacts: tuple[str, ...]
    timebox_minutes: int | None = None
    status: str = "draft"
    completion_refs: tuple[str, ...] = ()
    draft_reasons: tuple[str, ...] = ("awaiting-user-confirmation",)
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)
    _legacy_v0: bool = field(default=False, repr=False, compare=True)

    def __post_init__(self) -> None:
        try:
            project_id = validate_project_id(self.project_id)
        except (TypeError, ValueError) as exc:
            raise TaskSchemaError(str(exc)) from exc
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "task_id", _ident(self.task_id, "task_id"))
        title = _text(self.title, "title", maximum=500)
        assert title is not None
        object.__setattr__(self, "title", title)

        legacy = self._legacy_v0 is True
        if not isinstance(self._legacy_v0, bool):
            raise TaskSchemaError("_legacy_v0 must be boolean")
        why_now = _text(self.why_now, "why_now", optional=legacy)
        object.__setattr__(self, "why_now", why_now)
        object.__setattr__(
            self,
            "inputs",
            _items(self.inputs, "inputs", required=not legacy),
        )
        object.__setattr__(self, "evidence", _normalize_evidence(self.evidence))
        object.__setattr__(
            self,
            "allowed_paths",
            _path_items(self.allowed_paths, "allowed_paths", required=not legacy),
        )
        object.__setattr__(
            self,
            "denied_paths",
            _path_items(self.denied_paths, "denied_paths"),
        )
        dependencies = tuple(
            _ident(value, "dependencies entry")
            for value in _items(self.dependencies, "dependencies", maximum_text=64)
        )
        if self.task_id in dependencies:
            raise TaskSchemaError("a task cannot depend on itself")
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(
            self,
            "dod",
            _items(self.dod, "dod", required=not legacy),
        )
        object.__setattr__(
            self,
            "verification",
            _items(self.verification, "verification", required=not legacy),
        )
        artifacts = tuple(
            _artifact_ref(value)
            for value in _items(
                self.artifacts,
                "artifacts",
                required=not legacy,
                maximum_text=512,
            )
        )
        if len(set(artifacts)) != len(artifacts):
            raise TaskSchemaError("artifacts must be duplicate-free")
        object.__setattr__(self, "artifacts", artifacts)

        if self.timebox_minutes is not None and (
            isinstance(self.timebox_minutes, bool)
            or not isinstance(self.timebox_minutes, int)
            or not 1 <= self.timebox_minutes <= 10080
        ):
            raise TaskSchemaError("timebox_minutes must be null or an integer 1..10080")
        if not isinstance(self.status, str) or self.status not in TASK_STATUSES:
            raise TaskSchemaError(
                f"status must be one of {', '.join(sorted(TASK_STATUSES))}"
            )
        if legacy and self.status != "draft":
            raise TaskSchemaError("legacy compatibility tasks must remain draft")

        refs = tuple(
            _completion_ref(value)
            for value in _items(
                self.completion_refs,
                "completion_refs",
                maximum_text=512,
            )
        )
        if len(set(refs)) != len(refs):
            raise TaskSchemaError("completion_refs must be duplicate-free")
        if self.status == "completed" and not refs:
            raise TaskSchemaError("completed tasks require explicit completion_refs")
        if self.status != "completed" and refs:
            raise TaskSchemaError("only completed tasks may carry completion_refs")
        object.__setattr__(self, "completion_refs", refs)

        reasons = tuple(
            _ident(value, "draft_reasons entry")
            for value in _items(self.draft_reasons, "draft_reasons", maximum_text=64)
        )
        if self.status == "draft" and not reasons:
            raise TaskSchemaError("draft tasks require at least one draft reason")
        if self.status != "draft" and reasons:
            raise TaskSchemaError("non-draft tasks cannot carry draft reasons")
        object.__setattr__(self, "draft_reasons", reasons)

        created_at, created_time = _parse_timestamp(self.created_at, "created_at")
        updated_at, updated_time = _parse_timestamp(self.updated_at, "updated_at")
        if created_time > updated_time:
            raise TaskSchemaError("created_at must not be later than updated_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    @property
    def executable(self) -> bool:
        return not self._legacy_v0 and self.status in {"ready", "in_progress"}

    @property
    def is_legacy(self) -> bool:
        return self._legacy_v0

    def as_dict(self) -> dict[str, object]:
        if self._legacy_v0:
            raise TaskSchemaError(
                "legacy v0 tasks are read-only and require explicit migration"
            )
        return {
            "task_id": self.task_id,
            "title": self.title,
            "why_now": self.why_now,
            "inputs": list(self.inputs),
            "evidence": list(self.evidence),
            "allowed_paths": list(self.allowed_paths),
            "denied_paths": list(self.denied_paths),
            "dependencies": list(self.dependencies),
            "dod": list(self.dod),
            "verification": list(self.verification),
            "artifacts": list(self.artifacts),
            "timebox_minutes": self.timebox_minutes,
            "status": self.status,
            "completion_refs": list(self.completion_refs),
            "draft_reasons": list(self.draft_reasons),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def as_document(self) -> dict[str, object]:
        return {
            "schema_version": TASK_SCHEMA_VERSION,
            "kind": TASK_KIND,
            "task_version": TASK_VERSION,
            "project_id": self.project_id,
            **self.as_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        *,
        project_id: str,
    ) -> "ResearchTask":
        _fields(value, _TASK_FIELDS, "task")
        return cls(project_id=project_id, **value)  # type: ignore[arg-type]


# A concise public name is convenient for callers and matches the protocol docs.
Task = ResearchTask


@dataclass(frozen=True)
class TaskCollection:
    """A same-project, acyclic set of research tasks."""

    project_id: str
    tasks: tuple[ResearchTask, ...]
    updated_at: str = field(default_factory=_timestamp)
    source_schema_version: int = field(default=TASK_SCHEMA_VERSION, repr=False)

    def __post_init__(self) -> None:
        try:
            project_id = validate_project_id(self.project_id)
        except (TypeError, ValueError) as exc:
            raise TaskSchemaError(str(exc)) from exc
        object.__setattr__(self, "project_id", project_id)
        if isinstance(self.tasks, (str, bytes)) or not isinstance(self.tasks, Sequence):
            raise TaskSchemaError("tasks must be an array")
        tasks = tuple(self.tasks)
        if len(tasks) > 10000 or any(not isinstance(task, ResearchTask) for task in tasks):
            raise TaskSchemaError("tasks must be a bounded array of ResearchTask objects")
        if any(task.project_id != project_id for task in tasks):
            raise TaskSchemaError("all tasks must belong to the collection project")
        ids = tuple(task.task_id for task in tasks)
        if len(set(ids)) != len(ids):
            raise TaskSchemaError("task_id values must be duplicate-free")
        id_set = set(ids)
        for task in tasks:
            missing = sorted(set(task.dependencies) - id_set)
            if missing:
                raise TaskSchemaError(
                    f"task {task.task_id!r} has unknown dependencies: {missing}"
                )
        _assert_acyclic(tasks)
        if isinstance(self.source_schema_version, bool) or self.source_schema_version not in {
            0,
            TASK_SCHEMA_VERSION,
        }:
            raise UnsupportedTaskSchemaVersionError(
                f"task schema_version {self.source_schema_version} is unsupported"
            )
        if self.source_schema_version == TASK_SCHEMA_VERSION and any(
            task.is_legacy for task in tasks
        ):
            raise TaskSchemaError("current task collections cannot contain legacy views")
        if self.source_schema_version == 0 and any(not task.is_legacy for task in tasks):
            raise TaskSchemaError("legacy task collections require legacy task views")
        updated_at, _ = _parse_timestamp(self.updated_at, "updated_at")
        object.__setattr__(self, "tasks", tasks)
        object.__setattr__(self, "updated_at", updated_at)

    @property
    def is_legacy(self) -> bool:
        return self.source_schema_version == 0

    def as_dict(self) -> dict[str, object]:
        if self.is_legacy:
            raise TaskSchemaError(
                "legacy v0 task collections are read-only and require explicit migration"
            )
        return {
            "schema_version": TASK_SCHEMA_VERSION,
            "kind": TASK_KIND,
            "task_version": TASK_VERSION,
            "project_id": self.project_id,
            "tasks": [task.as_dict() for task in self.tasks],
            "updated_at": self.updated_at,
        }


TaskSet = TaskCollection


def _assert_acyclic(tasks: Sequence[ResearchTask]) -> None:
    graph = {task.task_id: task.dependencies for task in tasks}
    state: dict[str, int] = {}

    def visit(task_id: str, trail: tuple[str, ...]) -> None:
        marker = state.get(task_id, 0)
        if marker == 1:
            cycle = " -> ".join((*trail, task_id))
            raise TaskSchemaError(f"task dependencies contain a cycle: {cycle}")
        if marker == 2:
            return
        state[task_id] = 1
        for dependency in graph[task_id]:
            visit(dependency, (*trail, task_id))
        state[task_id] = 2

    for task_id in graph:
        visit(task_id, ())


def _requested_project_id(value: object | None) -> str | None:
    if value is None:
        return None
    try:
        return validate_project_id(value)
    except (TypeError, ValueError) as exc:
        raise TaskSchemaError("requested project_id is invalid") from exc


def _as_mapping(payload: object, *, name: str = "task artifact") -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise TaskSchemaError(f"{name} must be an object")
    return payload


def _decode_json(payload: bytes | str | Mapping[str, object]) -> object:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, str):
        raw = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raise TaskSchemaError("task artifact must be UTF-8 JSON bytes/text")
    try:
        return parse_json_bytes_strict(raw, label="task JSON")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TaskSchemaError("task artifact is not strict UTF-8 JSON") from exc


def _parse_current_task(value: Mapping[str, object], *, project_id: str | None) -> ResearchTask:
    _fields(value, _INDIVIDUAL_FIELDS, "task")
    schema_version = value["schema_version"]
    if isinstance(schema_version, bool) or schema_version != TASK_SCHEMA_VERSION:
        raise UnsupportedTaskSchemaVersionError(
            f"task schema_version {schema_version!r} is not current"
        )
    if value["kind"] != TASK_KIND or value["task_version"] != TASK_VERSION:
        raise TaskSchemaError("task kind/task_version is not current")
    embedded_project = _requested_project_id(value["project_id"])
    if project_id is not None and embedded_project != project_id:
        raise TaskSchemaError("task project_id does not match requested project")
    assert embedded_project is not None
    body = {key: value[key] for key in _TASK_FIELDS}
    return ResearchTask.from_dict(body, project_id=embedded_project)


def parse_task(
    payload: bytes | str | Mapping[str, object],
    *,
    project_id: str | None = None,
) -> ResearchTask:
    """Parse one current task; legacy input requires ``parse_legacy_tasks``."""

    requested = _requested_project_id(project_id)
    value = _as_mapping(_decode_json(payload), name="task")
    return _parse_current_task(value, project_id=requested)


def parse_task_collection(
    payload: bytes | str | Mapping[str, object],
    *,
    project_id: str | None = None,
) -> TaskCollection:
    """Parse the strict current ``tasks.json`` document."""

    requested = _requested_project_id(project_id)
    value = _as_mapping(_decode_json(payload))
    _fields(value, _DOCUMENT_FIELDS, "task document")
    if (
        isinstance(value["schema_version"], bool)
        or value["schema_version"] != TASK_SCHEMA_VERSION
    ):
        raise UnsupportedTaskSchemaVersionError(
            f"task schema_version {value['schema_version']!r} is not current"
        )
    if value["kind"] != TASK_KIND or value["task_version"] != TASK_VERSION:
        raise TaskSchemaError("task document kind/task_version is not current")
    embedded_project = _requested_project_id(value["project_id"])
    if requested is not None and embedded_project != requested:
        raise TaskSchemaError("task document project_id does not match requested project")
    assert embedded_project is not None
    raw_tasks = value["tasks"]
    if isinstance(raw_tasks, (str, bytes)) or not isinstance(raw_tasks, Sequence):
        raise TaskSchemaError("task document tasks must be an array")
    tasks = tuple(
        ResearchTask.from_dict(_as_mapping(raw, name="task entry"), project_id=embedded_project)
        for raw in raw_tasks
    )
    return TaskCollection(
        project_id=embedded_project,
        tasks=tasks,
        updated_at=value["updated_at"],
    )


def serialize_task(task: ResearchTask) -> str:
    """Serialize one current task as canonical JSON."""

    if not isinstance(task, ResearchTask):
        raise TaskSchemaError("task must be a ResearchTask")
    if task.is_legacy:
        raise TaskSchemaError("legacy task views cannot be serialized")
    return json.dumps(
        task.as_document(),
        ensure_ascii=False,
        indent=2,
        separators=(",", ": "),
    ) + "\n"


def serialize_task_collection(collection: TaskCollection) -> str:
    """Serialize a current task collection as canonical JSON."""

    if not isinstance(collection, TaskCollection):
        raise TaskSchemaError("collection must be a TaskCollection")
    if collection.is_legacy:
        raise TaskSchemaError("legacy task collections cannot be serialized")
    return json.dumps(
        collection.as_dict(),
        ensure_ascii=False,
        indent=2,
        separators=(",", ": "),
    ) + "\n"


def _legacy_status(raw: object, *, done: bool = False) -> tuple[str, tuple[str, ...]]:
    if done or raw is True or (
        isinstance(raw, str) and raw in {"done", "complete", "completed"}
    ):
        return "draft", ("legacy-completion-unverified",)
    if isinstance(raw, str) and raw in TASK_STATUSES and raw != "draft":
        return "draft", ("legacy-status-unconfirmed",)
    return "draft", ("legacy-status-unconfirmed",)


def _legacy_task(
    value: Mapping[str, object],
    *,
    project_id: str,
    ordinal: int,
) -> ResearchTask:
    """Read common Todo/task shapes without granting execution authority."""

    title = value.get("title", value.get("name", value.get("task")))
    if not isinstance(title, str) or not title.strip():
        raise TaskSchemaError("legacy task requires a title")
    raw_id = value.get("task_id", value.get("id", f"legacy-task-{ordinal}"))
    try:
        task_id = _ident(raw_id, "legacy task_id")
    except TaskSchemaError:
        task_id = f"legacy-task-{ordinal}"
    status, reasons = _legacy_status(
        value.get("status"), done=value.get("done") is True
    )

    def alias(*names: str) -> object:
        for name in names:
            if name in value:
                return value[name]
        return ()

    def legacy_items(raw: object, name: str, *, path: bool = False) -> tuple[str, ...]:
        if raw is None:
            return ()
        if isinstance(raw, str):
            raw = (raw,)
        if not isinstance(raw, Sequence) or isinstance(raw, (bytes, str)):
            return ()
        values = tuple(str(item).strip() for item in raw if str(item).strip())
        if path:
            safe: list[str] = []
            for item in values:
                try:
                    safe.append(_safe_project_pattern(item, name))
                except TaskSchemaError:
                    continue
            return tuple(safe)
        return values[:256]

    created = value.get("created_at", _timestamp())
    updated = value.get("updated_at", created)
    try:
        created_text = _timestamp(created)
        updated_text = _timestamp(updated)
    except TaskSchemaError:
        created_text = updated_text = _timestamp()
    return ResearchTask(
        project_id=project_id,
        task_id=task_id,
        title=title,
        why_now=value.get("why_now") if isinstance(value.get("why_now"), str) else None,
        inputs=legacy_items(alias("inputs", "input"), "inputs"),
        evidence=tuple(
            item
            for item in legacy_items(alias("evidence", "evidence_ids"), "evidence")
            if _EVIDENCE_RE.fullmatch(item)
        ),
        allowed_paths=legacy_items(
            alias("allowed_paths", "allowed", "paths"), "allowed_paths", path=True
        ),
        denied_paths=legacy_items(
            alias("denied_paths", "forbidden_paths", "forbidden"),
            "denied_paths",
            path=True,
        ),
        dependencies=legacy_items(alias("dependencies", "depends_on"), "dependencies"),
        dod=legacy_items(
            alias("dod", "definition_of_done", "definition-of-done"), "dod"
        ),
        verification=legacy_items(alias("verification", "verify"), "verification"),
        artifacts=legacy_items(
            alias("artifacts", "expected_artifacts", "expected-artifacts"), "artifacts"
        ),
        timebox_minutes=(
            value.get("timebox_minutes", value.get("timebox"))
            if isinstance(value.get("timebox_minutes", value.get("timebox")), int)
            and not isinstance(value.get("timebox_minutes", value.get("timebox")), bool)
            else None
        ),
        status=status,
        draft_reasons=reasons,
        created_at=created_text,
        updated_at=updated_text,
        _legacy_v0=True,
    )


def parse_legacy_tasks(
    payload: bytes | str | Mapping[str, object] | Sequence[object],
    *,
    project_id: str,
) -> TaskCollection:
    """Explicitly read legacy v0 todo records as non-executable draft views."""

    requested = _requested_project_id(project_id)
    assert requested is not None
    decoded = _decode_json(payload) if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)) else payload
    if isinstance(decoded, Mapping):
        root_project = decoded.get("project_id")
        if root_project is not None and _requested_project_id(root_project) != requested:
            raise TaskSchemaError("legacy task project_id does not match requested project")
        raw_tasks = decoded.get("tasks", decoded.get("todos", [decoded]))
    else:
        raw_tasks = decoded
    if isinstance(raw_tasks, (str, bytes)) or not isinstance(raw_tasks, Sequence):
        raise TaskSchemaError("legacy tasks must be an array or task object")
    tasks = tuple(
        _legacy_task(_as_mapping(raw, name="legacy task"), project_id=requested, ordinal=index + 1)
        for index, raw in enumerate(raw_tasks)
    )
    return TaskCollection(
        project_id=requested,
        tasks=tasks,
        updated_at=_timestamp(),
        source_schema_version=0,
    )


def parse_tasks_compatibility(
    payload: bytes | str | Mapping[str, object] | Sequence[object],
    *,
    project_id: str,
) -> TaskCollection:
    """Read current tasks or explicit legacy v0 data without migration."""

    decoded: object
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        decoded = payload
    else:
        decoded = _decode_json(payload)  # type: ignore[arg-type]
    if isinstance(decoded, Mapping) and "schema_version" in decoded:
        version = decoded["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise UnsupportedTaskSchemaVersionError(
                "task schema_version must be an integer"
            )
        if version == TASK_SCHEMA_VERSION:
            return parse_task_collection(decoded, project_id=project_id)
        if version != 0:
            raise UnsupportedTaskSchemaVersionError(
                f"task schema_version {version} is unsupported"
            )
    return parse_legacy_tasks(decoded, project_id=project_id)  # type: ignore[arg-type]


# Explicit aliases make the compatibility boundary discoverable without making
# the current parser silently accept legacy data.
parse_tasks = parse_task_collection
serialize_tasks = serialize_task_collection


def _md_timestamp(value: object | None) -> str:
    return _timestamp(value) if value is not None else _timestamp()


def _frontmatter(
    collection: TaskCollection,
    *,
    rendered_at: object | None,
) -> str:
    rendered = _md_timestamp(rendered_at)
    verified = bool(collection.tasks) and not collection.is_legacy and all(
        task.status != "draft" for task in collection.tasks
    )
    return serialize_knowledge_frontmatter(
        {
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "kind": KNOWLEDGE_PAGE_KIND,
            "project_id": collection.project_id,
            "artifact_type": "plan",
            "title": "Research task backlog",
            "status": "verified" if verified else "draft",
            "ownership": "mixed",
            "source_ids": [],
            "evidence_refs": [],
            "generated_at": rendered,
            "updated_at": rendered,
            "last_verified_at": rendered if verified else None,
        },
        path=TASK_MARKDOWN_PATH,
    )


def task_collection_to_markdown(
    collection: TaskCollection,
    *,
    rendered_at: object | None = None,
    user_region: str = "",
) -> str:
    """Render a Schema v2 backlog with one canonical machine block."""

    if not isinstance(collection, TaskCollection):
        raise TaskSchemaError("collection must be a TaskCollection")
    if collection.is_legacy:
        raise TaskSchemaError("legacy task collections cannot be rendered as current Markdown")
    if _BLOCK_START in user_region or _BLOCK_END in user_region:
        raise TaskSchemaError("user_region contains a reserved task-protocol marker")
    if _USER_START in user_region or _USER_END in user_region:
        raise TaskSchemaError("user_region contains a reserved user-region marker")
    payload = serialize_task_collection(collection).rstrip("\n")
    draft_notice = (
        "**DRAFT** — one or more tasks still require confirmation.\n"
        if not collection.tasks or any(task.status == "draft" for task in collection.tasks)
        else "**CURRENT** — task structure is validated; execution still follows host authorization.\n"
    )
    lines = [
        _frontmatter(collection, rendered_at=rendered_at),
        "# Research task backlog\n",
        "\n",
        draft_notice,
        "\n",
        "Tasks in this page are executable only after the current task protocol, "
        "dependencies, and user-owned decisions have been checked.\n",
        _BLOCK_START + "\n",
        "```json\n",
        payload + "\n",
        "```\n",
        _BLOCK_END + "\n",
        _USER_START + "\n",
        user_region,
        _USER_END + "\n",
    ]
    return "".join(lines)


def _extract_task_block(body: str) -> str:
    starts = [match.start() for match in re.finditer(re.escape(_BLOCK_START), body)]
    ends = [match.start() for match in re.finditer(re.escape(_BLOCK_END), body)]
    if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        raise TaskSchemaError("task protocol block must occur exactly once")
    block = body[starts[0] + len(_BLOCK_START) : ends[0]]
    match = re.fullmatch(r"\s*```json\n(.*?)\n```\s*", block, flags=re.DOTALL)
    if match is None:
        raise TaskSchemaError("task protocol block must contain one JSON fence")
    return match.group(1) + "\n"


def task_collection_from_markdown(
    payload: bytes | str,
    *,
    project_id: str | None = None,
) -> TaskCollection:
    """Parse a current Schema v2 backlog without changing its user region."""

    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not isinstance(raw, bytes):
        raise TaskSchemaError("Markdown payload must be UTF-8 bytes/text")
    try:
        page = parse_knowledge_page(raw, path=TASK_MARKDOWN_PATH)
    except (KnowledgeArtifactError, UnicodeError) as exc:
        raise TaskSchemaError("backlog Markdown is not a valid Schema v2 plan") from exc
    if page.frontmatter.schema_version != KNOWLEDGE_SCHEMA_VERSION:
        raise TaskSchemaError("Schema v1 backlog is compatibility-only")
    requested = _requested_project_id(project_id)
    if requested is not None and page.frontmatter.project_id != requested:
        raise TaskSchemaError("backlog project_id does not match requested project")
    return parse_task_collection(_extract_task_block(page.body), project_id=page.frontmatter.project_id)


task_to_markdown = task_collection_to_markdown
markdown_to_tasks = task_collection_from_markdown


@dataclass(frozen=True)
class TaskStore:
    """Registered machine-state access for I-02; Markdown remains caller-owned."""

    workspace_root: Path
    project_id: str
    registration: ProjectRegistrationResult = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            root = Path(self.workspace_root).resolve()
            project_id = validate_project_id(self.project_id)
            registration = load_registered_project(root, project_id)
        except (LayoutError, OSError, TypeError, ValueError) as exc:
            raise TaskStorageError("registered project could not be loaded safely") from exc
        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(self, "project_id", registration.project_id)
        object.__setattr__(self, "registration", registration)

    @property
    def machine_path(self) -> Path:
        try:
            return self.registration.layout.validate_machine_state_path(
                self.registration.layout.tasks_file,
                allow_missing_leaf=True,
            )
        except LayoutError as exc:
            raise TaskStorageError("task machine path escapes machine state") from exc

    @property
    def markdown_path(self) -> Path:
        root = self.registration.layout.knowledge_root.resolve()
        target = (root / TASK_MARKDOWN_PATH).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise TaskStorageError("task Markdown path escapes knowledge root") from exc
        return target

    def load(self) -> TaskCollection:
        try:
            observation = read_stable_regular_file(
                self.registration.layout.indexes_dir,
                self.machine_path,
                reject_redirection=True,
                capture_bytes=True,
            )
        except StableFileMissingError as exc:
            raise TaskNotFoundError(f"no task artifact exists for {self.project_id}") from exc
        except StableFileAccessError as exc:
            raise TaskStorageError("task artifact could not be read safely") from exc
        if observation.data is None:
            raise TaskStorageError("task artifact bytes were not captured")
        try:
            return parse_task_collection(observation.data, project_id=self.project_id)
        except TaskSchemaError:
            raise
        except (TypeError, ValueError) as exc:
            raise TaskStorageError("task artifact is malformed") from exc

    def load_compatibility(self) -> TaskCollection:
        """Explicitly read a current or legacy artifact without rewriting it."""

        try:
            observation = read_stable_regular_file(
                self.registration.layout.indexes_dir,
                self.machine_path,
                reject_redirection=True,
                capture_bytes=True,
            )
        except StableFileMissingError as exc:
            raise TaskNotFoundError(f"no task artifact exists for {self.project_id}") from exc
        except StableFileAccessError as exc:
            raise TaskStorageError("task artifact could not be read safely") from exc
        if observation.data is None:
            raise TaskStorageError("task artifact bytes were not captured")
        try:
            return parse_tasks_compatibility(observation.data, project_id=self.project_id)
        except TaskSchemaError:
            raise
        except (TypeError, ValueError) as exc:
            raise TaskStorageError("task artifact is malformed") from exc

    def write(self, collection: TaskCollection, *, lock_timeout_seconds: float = 5.0) -> Path:
        if not isinstance(collection, TaskCollection) or collection.project_id != self.project_id:
            raise TaskStorageError("task collection must belong to the registered project")
        try:
            payload = serialize_task_collection(collection).encode("utf-8")
        except TaskSchemaError as exc:
            raise TaskStorageError("task collection is not current/serializable") from exc
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
                    raise TaskStorageError("project registration changed while writing tasks")
                target = locked.layout.validate_machine_state_path(
                    locked.layout.tasks_file,
                    allow_missing_leaf=True,
                )
                write_atomic_stable_file(
                    locked.layout.indexes_dir,
                    target,
                    payload,
                    root_lease=lease,
                )
        except TaskStorageError:
            raise
        except (StableFileAccessError, LayoutError, OSError, TypeError, ValueError) as exc:
            raise TaskStorageError("task artifact could not be written safely") from exc
        return target

    def load_markdown(self) -> TaskCollection:
        try:
            observation = read_stable_regular_file(
                self.registration.layout.knowledge_root,
                self.markdown_path,
                reject_redirection=True,
                capture_bytes=True,
            )
        except StableFileMissingError as exc:
            raise TaskNotFoundError(f"no backlog page exists for {self.project_id}") from exc
        except StableFileAccessError as exc:
            raise TaskStorageError("backlog page could not be read safely") from exc
        if observation.data is None:
            raise TaskStorageError("backlog bytes were not captured")
        return task_collection_from_markdown(observation.data, project_id=self.project_id)


def write_tasks(
    workspace_root: str | Path,
    project_id: str,
    collection: TaskCollection,
    *,
    lock_timeout_seconds: float = 5.0,
) -> Path:
    return TaskStore(Path(workspace_root), project_id).write(
        collection, lock_timeout_seconds=lock_timeout_seconds
    )


def load_tasks(workspace_root: str | Path, project_id: str) -> TaskCollection:
    return TaskStore(Path(workspace_root), project_id).load()


def load_tasks_compatibility(
    workspace_root: str | Path,
    project_id: str,
) -> TaskCollection:
    """Explicitly load current or legacy data without silently migrating it."""

    return TaskStore(Path(workspace_root), project_id).load_compatibility()


deserialize_tasks = parse_task_collection
render_tasks_markdown = task_collection_to_markdown
