#!/usr/bin/env python3
"""Deterministic, rebuildable registered-project state snapshots (I-03).

The snapshot is a machine-state index.  It binds the current registration and
Manifest, summarizes only already-registered machine state and strict current
Knowledge Schema v2 pages, and never opens the registered source project.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping, Sequence

from tools.evidence_registry import (
    EVIDENCE_REGISTRY_VERSION,
    EvidenceError,
    load_evidence_registry,
    validate_evidence,
)
from tools.experiment_chains import (
    EXPERIMENT_CHAINS_VERSION,
    ExperimentChainError,
    validate_experiment_chains,
)
from tools.knowledge_artifacts import (
    KNOWLEDGE_SCHEMA_VERSION,
    KnowledgeArtifactError,
    KnowledgePage,
    artifact_contract_for_path,
    parse_knowledge_page,
)
from tools.project_analysis import (
    ProjectAnalysisError,
    atomic_write_json,
    canonical_json_bytes,
    current_manifest_bytes,
    exact_mapping,
    locked_project,
    manifest_binding,
    parse_canonical_json,
    sha256_bytes,
)
from tools.project_layout import CURRENT_SCHEMA_VERSION, LayoutError, parse_json_bytes_strict, validate_project_id
from tools.project_registry import load_registered_project
from tools.project_runs import ProjectRunError, validate_project_run_record
from tools.research_goals import GOAL_VERSION, GoalSchemaError, parse_goal
from tools.research_tasks import TASK_VERSION, TaskSchemaError, parse_task_collection
from tools.source_registry import SOURCE_REGISTRY_VERSION, SourceRegistryError, load_source_registry
from tools.stable_file_access import (
    StableFileAccessError,
    StableFileMissingError,
    read_stable_regular_file,
)

PROJECT_STATE_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
PROJECT_STATE_KIND = "llmwiki-project-state"
PROJECT_STATE_VERSION = "project-state-v1"
PROJECT_STATE_MACHINE_FILENAME = "project-state.json"
PROJECT_STATE_RELATIVE_PATH = f"indexes/{PROJECT_STATE_MACHINE_FILENAME}"
PROJECT_STATE_MAX_ITEMS = 256
PROJECT_STATE_MAX_RECENT_CHANGES = 64

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_RFC3339_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
_CODE_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "state_version",
        "artifact_id",
        "project_id",
        "generated_at",
        "registration",
        "manifest",
        "inputs",
        "experiments",
        "results",
        "open_questions",
        "blockers",
        "stale_knowledge",
        "stale_evidence",
        "recent_changes",
        "gaps",
    }
)
_REGISTRATION_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "project_id",
        "name",
        "registered_at",
        "identity_strategy",
        "sha256",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "scan_generation",
        "ordinary_file_count",
        "ordinary_byte_count",
        "sha256",
    }
)
_INPUT_NAMES = (
    "knowledge",
    "experiment_chains",
    "sources",
    "evidence",
    "goal",
    "tasks",
    "runs",
)
_INPUT_FIELDS = frozenset({"status", "item_count", "version", "sha256"})
_COLLECTION_FIELDS = frozenset({"total", "items"})
_ITEM_FIELDS = frozenset(
    {"id", "kind", "title", "status", "path", "updated_at", "reason_codes"}
)
_GAP_FIELDS = frozenset({"code", "artifact", "reason"})
_INPUT_STATUSES = frozenset({"available", "missing"})
_ITEM_KINDS = frozenset(
    {
        "experiment",
        "result",
        "open_question",
        "goal",
        "milestone",
        "task",
        "risk",
        "knowledge",
        "evidence",
        "run",
    }
)


class ProjectStateError(ProjectAnalysisError):
    """Strict schema or storage failure for an I-03 project-state snapshot."""


class UnsupportedProjectStateSchemaVersionError(ProjectStateError):
    """The project-state artifact is legacy or newer than this Core."""


class ProjectStateNotFoundError(ProjectStateError):
    """No current project-state artifact exists."""


def _timestamp(value: object | None = None) -> str:
    if value is None:
        current = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, date):
        current = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, str):
        try:
            current = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProjectStateError("generated_at must be RFC3339") from exc
    else:
        raise ProjectStateError("generated_at must be RFC3339/date/datetime/null")
    if current.tzinfo is None:
        raise ProjectStateError("generated_at requires an explicit timezone")
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(
    value: object,
    *,
    label: str,
    maximum: int = 4096,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ProjectStateError(f"{label} must be non-empty text" + (" or null" if optional else ""))
    normalized = value.strip()
    try:
        encoded = normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProjectStateError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > maximum:
        raise ProjectStateError(f"{label} exceeds {maximum} UTF-8 bytes")
    return normalized


def _code(value: object, *, label: str) -> str:
    normalized = _text(value, label=label, maximum=64)
    assert normalized is not None
    if _CODE_RE.fullmatch(normalized) is None:
        raise ProjectStateError(f"{label} must be a stable lowercase code")
    return normalized


def _sha(value: object, *, label: str) -> str:
    if type(value) is not str or _SHA_RE.fullmatch(value) is None:
        raise ProjectStateError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _nonnegative(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ProjectStateError(f"{label} must be an integer >= 0")
    return value


def _canonical_timestamp(value: object, *, label: str) -> str:
    if type(value) is not str or _RFC3339_RE.fullmatch(value) is None:
        raise ProjectStateError(f"{label} must be canonical UTC RFC3339 seconds")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectStateError(f"{label} must be a valid timestamp") from exc
    if parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") != value:
        raise ProjectStateError(f"{label} must be canonical UTC RFC3339 seconds")
    return value


def _project_run_timestamp_seconds(value: str) -> str:
    """Project a validated ProjectRun timestamp into ProjectState's seconds precision."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _relative_path(value: object, *, label: str, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    normalized = _text(value, label=label, maximum=1024)
    assert normalized is not None
    if "\\" in normalized:
        raise ProjectStateError(f"{label} must use POSIX separators")
    path = PurePosixPath(normalized)
    if path.is_absolute() or normalized.startswith("/") or any(part in {"", ".", ".."} for part in path.parts):
        raise ProjectStateError(f"{label} must be a canonical project-relative path")
    if path.as_posix() != normalized:
        raise ProjectStateError(f"{label} must be canonical")
    return normalized


def _exact(value: object, fields: frozenset[str], *, label: str) -> dict[str, Any]:
    try:
        return exact_mapping(value, fields, label=label)
    except ProjectAnalysisError as exc:
        raise ProjectStateError(str(exc)) from exc


def _artifact_id(payload_without_id: Mapping[str, Any]) -> str:
    return "state-" + sha256_bytes(canonical_json_bytes(payload_without_id))


@dataclass(frozen=True)
class ProjectState:
    project_id: str
    generated_at: str
    registration: Mapping[str, Any]
    manifest: Mapping[str, Any]
    inputs: Mapping[str, Any]
    experiments: Mapping[str, Any]
    results: Mapping[str, Any]
    open_questions: Mapping[str, Any]
    blockers: Mapping[str, Any]
    stale_knowledge: Mapping[str, Any]
    stale_evidence: Mapping[str, Any]
    recent_changes: Mapping[str, Any]
    gaps: tuple[Mapping[str, Any], ...]
    artifact_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECT_STATE_SCHEMA_VERSION,
            "kind": PROJECT_STATE_KIND,
            "state_version": PROJECT_STATE_VERSION,
            "artifact_id": self.artifact_id,
            "project_id": self.project_id,
            "generated_at": self.generated_at,
            "registration": dict(self.registration),
            "manifest": dict(self.manifest),
            "inputs": {key: dict(value) for key, value in self.inputs.items()},
            "experiments": _copy_collection(self.experiments),
            "results": _copy_collection(self.results),
            "open_questions": _copy_collection(self.open_questions),
            "blockers": _copy_collection(self.blockers),
            "stale_knowledge": _copy_collection(self.stale_knowledge),
            "stale_evidence": _copy_collection(self.stale_evidence),
            "recent_changes": _copy_collection(self.recent_changes),
            "gaps": [dict(item) for item in self.gaps],
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


ProjectStateSnapshot = ProjectState


@dataclass(frozen=True)
class ProjectStateResult:
    project_id: str
    manifest_file: Path
    project_state_file: Path
    state: ProjectState

    @property
    def project_state(self) -> dict[str, Any]:
        return self.state.as_dict()

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "manifest_file": str(self.manifest_file),
            "project_state_file": str(self.project_state_file),
            "project_state": self.project_state,
        }


def _copy_collection(value: Mapping[str, Any]) -> dict[str, Any]:
    return {"total": value["total"], "items": [dict(item) for item in value["items"]]}


def _validate_input(value: object, *, label: str) -> dict[str, Any]:
    item = _exact(value, _INPUT_FIELDS, label=label)
    status = item["status"]
    if status not in _INPUT_STATUSES:
        raise ProjectStateError(f"{label}.status is invalid")
    count = _nonnegative(item["item_count"], label=f"{label}.item_count")
    version = item["version"]
    digest = item["sha256"]
    if status == "missing":
        if count != 0 or version is not None or digest is not None:
            raise ProjectStateError(f"{label} missing state must have null/zero binding")
    else:
        if type(version) is not str or not version:
            raise ProjectStateError(f"{label}.version is required when available")
        _sha(digest, label=f"{label}.sha256")
    return {"status": status, "item_count": count, "version": version, "sha256": digest}


def _validate_item(value: object, *, label: str) -> dict[str, Any]:
    item = _exact(value, _ITEM_FIELDS, label=label)
    identifier = _text(item["id"], label=f"{label}.id", maximum=512)
    assert identifier is not None
    kind = item["kind"]
    if kind not in _ITEM_KINDS:
        raise ProjectStateError(f"{label}.kind is invalid")
    title = _text(item["title"], label=f"{label}.title", maximum=4096, optional=True)
    status = _code(item["status"], label=f"{label}.status")
    path = _relative_path(item["path"], label=f"{label}.path")
    updated_at = item["updated_at"]
    if updated_at is not None:
        updated_at = _canonical_timestamp(updated_at, label=f"{label}.updated_at")
    reasons = item["reason_codes"]
    if not isinstance(reasons, list) or len(reasons) > 32:
        raise ProjectStateError(f"{label}.reason_codes must be a bounded array")
    normalized_reasons = tuple(_code(reason, label=f"{label}.reason_codes[{index}]") for index, reason in enumerate(reasons))
    if tuple(sorted(normalized_reasons)) != normalized_reasons or len(set(normalized_reasons)) != len(normalized_reasons):
        raise ProjectStateError(f"{label}.reason_codes must be sorted and duplicate-free")
    return {
        "id": identifier,
        "kind": kind,
        "title": title,
        "status": status,
        "path": path,
        "updated_at": updated_at,
        "reason_codes": list(normalized_reasons),
    }


def _validate_collection(
    value: object, *, label: str, stable_order: bool = True
) -> dict[str, Any]:
    collection = _exact(value, _COLLECTION_FIELDS, label=label)
    raw_items = collection["items"]
    if not isinstance(raw_items, list) or len(raw_items) > PROJECT_STATE_MAX_ITEMS:
        raise ProjectStateError(f"{label}.items must be a bounded array")
    items = [_validate_item(raw, label=f"{label}.items[{index}]") for index, raw in enumerate(raw_items)]
    if collection["total"] != len(items):
        raise ProjectStateError(f"{label}.total does not match items")
    keys = [(item["kind"], item["id"], item["path"] or "") for item in items]
    if len(set(keys)) != len(keys):
        raise ProjectStateError(f"{label}.items must be duplicate-free")
    if stable_order and keys != sorted(keys):
        raise ProjectStateError(f"{label}.items must be sorted")
    return {"total": len(items), "items": items}


def _validate_recent(value: object) -> dict[str, Any]:
    collection = _validate_collection(
        value, label="recent_changes", stable_order=False
    )
    items = collection["items"]
    # Recent changes are newest first; ties use the same stable key as the
    # generator.  Null timestamps are allowed only for defensive compatibility.
    keys = [
        (item["updated_at"] is not None, item["updated_at"] or "", item["kind"], item["id"])
        for item in items
    ]
    expected = sorted(keys, key=lambda key: (not key[0], key[1], key[2], key[3]), reverse=True)
    if keys != expected:
        raise ProjectStateError("recent_changes.items are not in deterministic order")
    return collection


def _validate_registration(value: object, *, project_id: str) -> dict[str, Any]:
    item = _exact(value, _REGISTRATION_FIELDS, label="registration")
    if item["schema_version"] != PROJECT_STATE_SCHEMA_VERSION:
        raise UnsupportedProjectStateSchemaVersionError("registration schema_version is not current")
    if item["kind"] != "llmwiki-project":
        raise ProjectStateError("registration.kind is invalid")
    if validate_project_id(item["project_id"]) != project_id:
        raise ProjectStateError("registration.project_id does not match snapshot")
    name = _text(item["name"], label="registration.name", maximum=512)
    registered_at = _text(item["registered_at"], label="registration.registered_at", maximum=128)
    identity = _text(item["identity_strategy"], label="registration.identity_strategy", maximum=128)
    assert name is not None and registered_at is not None and identity is not None
    _sha(item["sha256"], label="registration.sha256")
    return {
        "schema_version": PROJECT_STATE_SCHEMA_VERSION,
        "kind": "llmwiki-project",
        "project_id": project_id,
        "name": name,
        "registered_at": registered_at,
        "identity_strategy": identity,
        "sha256": item["sha256"],
    }


def _validate_manifest(value: object) -> dict[str, Any]:
    item = _exact(value, _MANIFEST_FIELDS, label="manifest")
    manifest_version = _text(item["manifest_version"], label="manifest.manifest_version", maximum=128)
    assert manifest_version is not None
    return {
        "manifest_version": manifest_version,
        "scan_generation": _nonnegative(item["scan_generation"], label="manifest.scan_generation"),
        "ordinary_file_count": _nonnegative(item["ordinary_file_count"], label="manifest.ordinary_file_count"),
        "ordinary_byte_count": _nonnegative(item["ordinary_byte_count"], label="manifest.ordinary_byte_count"),
        "sha256": _sha(item["sha256"], label="manifest.sha256"),
    }


def _validate_gap(value: object, *, label: str) -> dict[str, str]:
    item = _exact(value, _GAP_FIELDS, label=label)
    code = _code(item["code"], label=f"{label}.code")
    artifact = _code(item["artifact"], label=f"{label}.artifact")
    reason = _text(item["reason"], label=f"{label}.reason", maximum=1024)
    assert reason is not None
    return {"code": code, "artifact": artifact, "reason": reason}


def _validate_payload(value: Mapping[str, Any], *, project_id: str | None = None) -> dict[str, Any]:
    top = _exact(value, _TOP_FIELDS, label="project-state")
    if top["schema_version"] != PROJECT_STATE_SCHEMA_VERSION:
        raise UnsupportedProjectStateSchemaVersionError("project-state schema_version is not current")
    if top["kind"] != PROJECT_STATE_KIND or top["state_version"] != PROJECT_STATE_VERSION:
        raise ProjectStateError("project-state kind/state_version is not current")
    embedded_project = validate_project_id(top["project_id"])
    if project_id is not None and embedded_project != validate_project_id(project_id):
        raise ProjectStateError("project-state project_id does not match requested project")
    generated_at = _canonical_timestamp(top["generated_at"], label="generated_at")
    registration = _validate_registration(top["registration"], project_id=embedded_project)
    manifest = _validate_manifest(top["manifest"])
    raw_inputs = _exact(top["inputs"], frozenset(_INPUT_NAMES), label="inputs")
    inputs = {name: _validate_input(raw_inputs[name], label=f"inputs.{name}") for name in _INPUT_NAMES}
    experiments = _validate_collection(top["experiments"], label="experiments")
    results = _validate_collection(top["results"], label="results")
    open_questions = _validate_collection(top["open_questions"], label="open_questions")
    blockers = _validate_collection(top["blockers"], label="blockers")
    stale_knowledge = _validate_collection(top["stale_knowledge"], label="stale_knowledge")
    stale_evidence = _validate_collection(top["stale_evidence"], label="stale_evidence")
    recent_changes = _validate_recent(top["recent_changes"])
    raw_gaps = top["gaps"]
    if not isinstance(raw_gaps, list) or len(raw_gaps) > PROJECT_STATE_MAX_ITEMS:
        raise ProjectStateError("gaps must be a bounded array")
    gaps = [_validate_gap(raw, label=f"gaps[{index}]") for index, raw in enumerate(raw_gaps)]
    gap_keys = [(gap["code"], gap["artifact"], gap["reason"]) for gap in gaps]
    if gap_keys != sorted(gap_keys) or len(set(gap_keys)) != len(gap_keys):
        raise ProjectStateError("gaps must be sorted and duplicate-free")
    without_id = dict(top)
    del without_id["artifact_id"]
    if type(top["artifact_id"]) is not str or not top["artifact_id"].startswith("state-") or not _SHA_RE.fullmatch(top["artifact_id"][6:]):
        raise ProjectStateError("artifact_id is invalid")
    if top["artifact_id"] != _artifact_id(without_id):
        raise ProjectStateError("artifact_id does not match snapshot content")
    normalized = {
        "schema_version": PROJECT_STATE_SCHEMA_VERSION,
        "kind": PROJECT_STATE_KIND,
        "state_version": PROJECT_STATE_VERSION,
        "artifact_id": top["artifact_id"],
        "project_id": embedded_project,
        "generated_at": generated_at,
        "registration": registration,
        "manifest": manifest,
        "inputs": inputs,
        "experiments": experiments,
        "results": results,
        "open_questions": open_questions,
        "blockers": blockers,
        "stale_knowledge": stale_knowledge,
        "stale_evidence": stale_evidence,
        "recent_changes": recent_changes,
        "gaps": gaps,
    }
    # A parser must never silently normalize a valid-looking current artifact.
    if canonical_json_bytes(normalized) != canonical_json_bytes(dict(value)):
        raise ProjectStateError("project-state contains non-canonical or non-normalized values")
    return normalized


def parse_project_state(
    payload: bytes | str | Mapping[str, Any],
    *,
    project_id: str | None = None,
) -> ProjectState:
    if isinstance(payload, bytes):
        try:
            value = parse_canonical_json(payload, label="project-state")
        except ProjectAnalysisError as exc:
            raise ProjectStateError(str(exc)) from exc
    elif isinstance(payload, str):
        try:
            value = parse_canonical_json(payload.encode("utf-8", errors="strict"), label="project-state")
        except (UnicodeEncodeError, ProjectAnalysisError) as exc:
            raise ProjectStateError(str(exc)) from exc
    elif isinstance(payload, Mapping):
        value = dict(payload)
    else:
        raise ProjectStateError("project-state payload must be bytes, text, or mapping")
    normalized = _validate_payload(value, project_id=project_id)
    return ProjectState(
        project_id=normalized["project_id"],
        generated_at=normalized["generated_at"],
        registration=normalized["registration"],
        manifest=normalized["manifest"],
        inputs=normalized["inputs"],
        experiments=normalized["experiments"],
        results=normalized["results"],
        open_questions=normalized["open_questions"],
        blockers=normalized["blockers"],
        stale_knowledge=normalized["stale_knowledge"],
        stale_evidence=normalized["stale_evidence"],
        recent_changes=normalized["recent_changes"],
        gaps=tuple(normalized["gaps"]),
        artifact_id=normalized["artifact_id"],
    )


def serialize_project_state(state: ProjectState | Mapping[str, Any]) -> str:
    if isinstance(state, ProjectState):
        payload = state.as_dict()
    elif isinstance(state, Mapping):
        payload = dict(state)
    else:
        raise ProjectStateError("state must be a ProjectState or mapping")
    normalized = _validate_payload(payload, project_id=payload.get("project_id"))
    return canonical_json_bytes(normalized).decode("utf-8")


def _read_machine_file(layout: Any, path: Path, *, optional: bool = False) -> bytes | None:
    try:
        observation = read_stable_regular_file(
            layout.machine_root,
            path,
            reject_redirection=True,
            capture_bytes=True,
        )
    except StableFileMissingError:
        if optional:
            return None
        raise ProjectStateError(f"required machine artifact is missing: {path.name}")
    except (StableFileAccessError, OSError) as exc:
        raise ProjectStateError(f"machine artifact could not be read safely: {path.name}") from exc
    if observation.data is None:
        raise ProjectStateError(f"machine artifact bytes were not captured: {path.name}")
    return observation.data


def _knowledge_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise ProjectStateError("knowledge root must be a real directory")
    result: list[Path] = []
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise ProjectStateError("knowledge directory could not be listed") from exc
        for entry in entries:
            if entry.is_symlink():
                raise ProjectStateError(f"knowledge path cannot be a symbolic link: {entry.name}")
            if entry.is_dir():
                pending.append(entry)
            elif entry.is_file() and entry.suffix.lower() == ".md":
                result.append(entry)
            elif entry.exists() and not entry.is_file():
                raise ProjectStateError(f"unsupported knowledge filesystem entry: {entry.name}")
    return sorted(result, key=lambda item: item.relative_to(root).as_posix())


def _knowledge_snapshot(registration: Any) -> tuple[list[KnowledgePage], list[dict[str, Any]], str, list[str]]:
    root = registration.layout.knowledge_root
    pages: list[KnowledgePage] = []
    metadata: list[dict[str, Any]] = []
    page_digests: list[dict[str, str]] = []
    for target in _knowledge_files(root):
        relative = target.relative_to(root).as_posix()
        try:
            artifact_contract_for_path(relative)
            observation = read_stable_regular_file(
                root,
                target,
                reject_redirection=True,
                capture_bytes=True,
            )
            if observation.data is None:
                raise ProjectStateError(f"knowledge page bytes were not captured: {relative}")
            page = parse_knowledge_page(observation.data, path=relative)
        except (KnowledgeArtifactError, StableFileAccessError, OSError, ValueError) as exc:
            raise ProjectStateError(f"knowledge page is not a current strict Schema v2 page: {relative}") from exc
        if page.frontmatter.schema_version != KNOWLEDGE_SCHEMA_VERSION:
            raise ProjectStateError(f"legacy/future Knowledge page is not accepted: {relative}")
        if page.frontmatter.project_id != registration.project_id:
            raise ProjectStateError(f"Knowledge page project_id mismatch: {relative}")
        page_hash = sha256_bytes(observation.data)
        page_digests.append({"path": relative, "sha256": page_hash})
        metadata.append(
            {
                "id": "knowledge:" + relative,
                "kind": "knowledge",
                "title": page.frontmatter.title,
                "status": page.frontmatter.status,
                "path": relative,
                "updated_at": page.frontmatter.updated_at,
                "reason_codes": [],
            }
        )
        pages.append(page)
    digest = sha256_bytes(canonical_json_bytes({"pages": page_digests}))
    return pages, metadata, digest, [item["path"] for item in page_digests]


def _item(
    identifier: str,
    kind: str,
    *,
    title: str | None = None,
    status: str = "draft",
    path: str | None = None,
    updated_at: str | None = None,
    reasons: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": kind,
        "title": title,
        "status": status,
        "path": path,
        "updated_at": updated_at,
        "reason_codes": sorted(set(reasons)),
    }


def _collection(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        (dict(item) for item in items),
        key=lambda item: (item["kind"], item["id"], item["path"] or ""),
    )
    return {"total": len(ordered), "items": ordered}


def _input(status: str, count: int = 0, version: str | None = None, digest: str | None = None) -> dict[str, Any]:
    return {"status": status, "item_count": count, "version": version, "sha256": digest}


def _gap(code: str, artifact: str, reason: str) -> dict[str, str]:
    return {"code": code, "artifact": artifact, "reason": reason}


def _safe_registration_binding(registration: Any, project_bytes: bytes) -> dict[str, Any]:
    record = registration.record
    return {
        "schema_version": PROJECT_STATE_SCHEMA_VERSION,
        "kind": "llmwiki-project",
        "project_id": registration.project_id,
        "name": str(record["name"]),
        "registered_at": str(record["registered_at"]),
        "identity_strategy": str(record["identity_strategy"]),
        "sha256": sha256_bytes(project_bytes),
    }


def _page_collections(
    pages: Sequence[KnowledgePage],
    metadata: Sequence[Mapping[str, Any]],
    *,
    stale_evidence_ids: set[str],
    source_ids: set[str] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    experiments: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    open_questions: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    stale_knowledge: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    for page, base in zip(pages, metadata):
        fm = page.frontmatter
        path = base["path"]
        artifact_type = fm.artifact_type
        if artifact_type == "experiment":
            experiments.append(dict(base))
        elif artifact_type == "result":
            results.append(dict(base))
        elif artifact_type == "open_question":
            open_questions.append(dict(base))
        if artifact_type == "risk" or fm.status in {"conflicting", "rejected"}:
            blockers.append(
                _item(
                    "knowledge:" + path,
                    "risk" if artifact_type == "risk" else "knowledge",
                    title=fm.title,
                    status=fm.status,
                    path=path,
                    updated_at=fm.updated_at,
                    reasons=("risk-page" if artifact_type == "risk" else "conflicting-knowledge",),
                )
            )
        reasons: list[str] = []
        if fm.status == "stale":
            reasons.append("declared-stale")
        refs = fm.evidence_refs or ()
        if stale_evidence_ids and any(ref.evidence_id in stale_evidence_ids for ref in refs):
            reasons.append("stale-evidence")
        if source_ids is not None and any(source_id not in source_ids for source_id in fm.source_ids):
            reasons.append("unknown-source")
        if fm.status == "verified" and fm.last_verified_at != fm.updated_at:
            reasons.append("verification-outdated")
        if reasons:
            stale_knowledge.append(
                _item(
                    "knowledge:" + path,
                    "knowledge",
                    title=fm.title,
                    status="stale",
                    path=path,
                    updated_at=fm.updated_at,
                    reasons=reasons,
                )
            )
        recent.append(dict(base))
    return (
        _collection(experiments),
        _collection(results),
        _collection(open_questions),
        _collection(blockers),
        stale_knowledge,
        recent,
        [dict(item) for item in metadata],
    )


def _chain_collections(chains: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    experiments: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for row in chains.get("configs", []):
        experiments.append(
            _item(
                "chain:config:" + str(row["config_id"]),
                "experiment",
                title=str(row["title"]),
                status="inferred" if row.get("certainty") == "inferred" else "observed",
                reasons=("experiment-chain",),
            )
        )
    for row in chains.get("runs", []):
        experiments.append(
            _item(
                "chain:run:" + str(row["run_id"]),
                "experiment",
                title=str(row["title"]),
                status=str(row["status"]),
                reasons=("experiment-chain",),
            )
        )
    for row in chains.get("results", []):
        results.append(
            _item(
                "chain:result:" + str(row["result_id"]),
                "result",
                title=str(row["title"]),
                status=str(row["status"]),
                reasons=("experiment-chain",),
            )
        )
    return experiments, results


def _load_optional_chains(layout: Any, project_id: str) -> tuple[dict[str, Any] | None, bytes | None]:
    raw = _read_machine_file(layout, layout.experiment_chains_file, optional=True)
    if raw is None:
        return None, None
    try:
        parsed = parse_canonical_json(raw, label="experiment-chains")
        return validate_experiment_chains(parsed, project_id=project_id), raw
    except (ProjectAnalysisError, ExperimentChainError, OSError) as exc:
        raise ProjectStateError("experiment-chains artifact is invalid or stale") from exc


def _load_optional_goal(layout: Any, project_id: str) -> tuple[Any | None, bytes | None]:
    raw = _read_machine_file(layout, layout.goals_file, optional=True)
    if raw is None:
        return None, None
    try:
        return parse_goal(raw, project_id=project_id), raw
    except (GoalSchemaError, ValueError, TypeError) as exc:
        raise ProjectStateError("Goal artifact is invalid or unsupported") from exc


def _load_optional_tasks(layout: Any, project_id: str) -> tuple[Any | None, bytes | None]:
    raw = _read_machine_file(layout, layout.tasks_file, optional=True)
    if raw is None:
        return None, None
    try:
        return parse_task_collection(raw, project_id=project_id), raw
    except (TaskSchemaError, ValueError, TypeError) as exc:
        raise ProjectStateError("task artifact is invalid or unsupported") from exc


def _load_optional_source_registry(layout: Any, workspace_root: Path, project_id: str) -> tuple[Any | None, bytes | None]:
    raw = _read_machine_file(layout, layout.sources_file, optional=True)
    if raw is None:
        return None, None
    try:
        registry = load_source_registry(workspace_root, project_id)
    except (SourceRegistryError, OSError, ValueError) as exc:
        raise ProjectStateError("source registry is invalid or unsupported") from exc
    # The public loader protects the registry with its own stable reader.  Bind
    # the exact bytes observed by this snapshot as well.
    current = _read_machine_file(layout, layout.sources_file, optional=False)
    if current != raw:
        raise ProjectStateError("source registry changed while state was generated")
    return registry, raw


def _load_optional_evidence_registry(
    layout: Any,
    workspace_root: Path,
    project_id: str,
    source_registry: Any | None,
) -> tuple[Any | None, bytes | None, list[dict[str, Any]], set[str]]:
    raw = _read_machine_file(layout, layout.evidence_file, optional=True)
    if raw is None:
        return None, None, [], set()
    if source_registry is None:
        raise ProjectStateError("Evidence exists but its source registry is missing")
    try:
        registry = load_evidence_registry(workspace_root, project_id)
    except (EvidenceError, OSError, ValueError) as exc:
        raise ProjectStateError("Evidence registry is invalid or unsupported") from exc
    current = _read_machine_file(layout, layout.evidence_file, optional=False)
    if current != raw:
        raise ProjectStateError("Evidence registry changed while state was generated")
    stale: list[dict[str, Any]] = []
    stale_ids: set[str] = set()
    for record in registry.records:
        validation = validate_evidence(record, source_registry)
        if not validation.valid:
            stale_ids.add(record.evidence_id)
            stale.append(
                _item(
                    "evidence:" + record.evidence_id,
                    "evidence",
                    title=record.source_id,
                    status="stale",
                    reasons=(validation.reason_code,),
                )
            )
    return registry, raw, stale, stale_ids


def _goal_items(goal: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if goal is None:
        return [], []
    items = [
        _item(
            "goal:" + goal.goal_id,
            "goal",
            title=goal.goal or goal.goal_id,
            status=goal.status,
            path="goals.md",
            updated_at=goal.updated_at,
            reasons=goal.draft_reasons,
        )
    ]
    blockers: list[dict[str, Any]] = []
    if goal.status == "blocked":
        blockers.append(
            _item(
                "goal:" + goal.goal_id,
                "goal",
                title=goal.goal or goal.goal_id,
                status="blocked",
                path="goals.md",
                updated_at=goal.updated_at,
                reasons=("goal-blocked",),
            )
        )
    for milestone in goal.milestones:
        item = _item(
            "milestone:" + milestone.milestone_id,
            "milestone",
            title=milestone.title,
            status=milestone.status,
            path="goals.md",
            updated_at=goal.updated_at,
            reasons=(),
        )
        items.append(item)
        if milestone.status == "blocked":
            blockers.append(
                _item(
                    "milestone:" + milestone.milestone_id,
                    "milestone",
                    title=milestone.title,
                    status="blocked",
                    path="goals.md",
                    updated_at=goal.updated_at,
                    reasons=("milestone-blocked",),
                )
            )
    return items, blockers


def _task_items(tasks: Any | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if tasks is None:
        return [], []
    items: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for task in tasks.tasks:
        item = _item(
            "task:" + task.task_id,
            "task",
            title=task.title,
            status=task.status,
            path="plans/backlog.md",
            updated_at=task.updated_at,
            reasons=task.draft_reasons,
        )
        items.append(item)
        if task.status == "blocked":
            blockers.append(
                _item(
                    "task:" + task.task_id,
                    "task",
                    title=task.title,
                    status="blocked",
                    path="plans/backlog.md",
                    updated_at=task.updated_at,
                    reasons=("task-blocked",),
                )
            )
    return items, blockers


def _run_items(layout: Any, project_id: str) -> tuple[list[dict[str, Any]], bytes | None, list[str]]:
    root = layout.runs_dir
    if not root.exists():
        return [], None, []
    if root.is_symlink() or not root.is_dir():
        raise ProjectStateError("runs root must be a real directory")
    items: list[dict[str, Any]] = []
    digests: list[dict[str, str]] = []
    run_paths: list[str] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if directory.is_symlink():
            raise ProjectStateError("run directory cannot be a symbolic link")
        if not directory.is_dir():
            raise ProjectStateError("runs directory contains an unexpected entry")
        target = directory / "run.json"
        raw = _read_machine_file(layout, target, optional=False)
        try:
            value = parse_json_bytes_strict(raw, label=f"run {directory.name}")
            record = validate_project_run_record(
                value,
                expected_project_id=project_id,
                expected_run_id=directory.name,
            )
        except (LayoutError, ProjectRunError, ValueError, TypeError) as exc:
            raise ProjectStateError(f"project run is invalid: {directory.name}") from exc
        run_id = directory.name
        items.append(
            _item(
                "run:" + run_id,
                "run",
                title=run_id,
                status=str(record["status"]),
                updated_at=_project_run_timestamp_seconds(record["updated_at"]),
                reasons=("run-failed",) if record["status"] == "failed" else (),
            )
        )
        digests.append({"path": f"runs/{run_id}/run.json", "sha256": sha256_bytes(raw)})
        run_paths.append(run_id)
    digest = sha256_bytes(canonical_json_bytes({"files": digests})) if digests else None
    return items, digest, run_paths


def _recent_changes(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unique: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for item in items:
        key = (str(item["kind"]), str(item["id"]), item.get("path"))
        unique[key] = dict(item)
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item.get("updated_at") is not None,
            item.get("updated_at") or "",
            item["kind"],
            item["id"],
        ),
        reverse=True,
    )[:PROJECT_STATE_MAX_RECENT_CHANGES]
    return {"total": len(ordered), "items": ordered}


def _build_state(
    *,
    registration: Any,
    project_bytes: bytes,
    manifest: Any,
    manifest_digest: str,
    generated_at: object | None,
) -> ProjectState:
    pages, page_metadata, knowledge_digest, _knowledge_paths = _knowledge_snapshot(registration)
    chains, chains_bytes = _load_optional_chains(
        registration.layout, registration.project_id
    )
    source_registry, source_bytes = _load_optional_source_registry(
        registration.layout, registration.layout.workspace_root, registration.project_id
    )
    evidence_registry, evidence_bytes, stale_evidence, stale_evidence_ids = _load_optional_evidence_registry(
        registration.layout,
        registration.layout.workspace_root,
        registration.project_id,
        source_registry,
    )
    goal, goal_bytes = _load_optional_goal(registration.layout, registration.project_id)
    tasks, task_bytes = _load_optional_tasks(registration.layout, registration.project_id)
    run_items, run_digest, _run_paths = _run_items(registration.layout, registration.project_id)

    source_ids = set(source_registry.by_source_id) if source_registry is not None else None
    page_experiments, page_results, page_questions, page_blockers, stale_knowledge, page_recent, _ = _page_collections(
        pages,
        page_metadata,
        stale_evidence_ids=stale_evidence_ids,
        source_ids=source_ids,
    )
    chain_experiments: list[dict[str, Any]] = []
    chain_results: list[dict[str, Any]] = []
    if chains is not None:
        chain_experiments, chain_results = _chain_collections(chains)
    experiments = _collection([*page_experiments["items"], *chain_experiments])
    results = _collection([*page_results["items"], *chain_results])
    open_questions = page_questions
    goal_items, goal_blockers = _goal_items(goal)
    task_items, task_blockers = _task_items(tasks)
    blockers = _collection([*page_blockers["items"], *goal_blockers, *task_blockers])
    stale_evidence_collection = _collection(stale_evidence)
    stale_knowledge_collection = _collection(stale_knowledge)
    recent = _recent_changes([*page_recent, *goal_items, *task_items, *run_items])

    gaps: list[dict[str, str]] = []
    if not pages:
        gaps.append(_gap("missing-knowledge", "knowledge", "No strict current Schema v2 Knowledge pages are available."))
    if chains is None:
        gaps.append(_gap("missing-experiment-chains", "experiment_chains", "No current experiment-chain artifact is available."))
    if source_registry is None:
        gaps.append(_gap("missing-sources", "sources", "No source registry is available for Evidence currentness."))
    if evidence_registry is None:
        gaps.append(_gap("missing-evidence", "evidence", "No Evidence registry is available."))
    if goal is None:
        gaps.append(_gap("missing-goal", "goal", "No user-confirmed Goal artifact is available."))
    if tasks is None:
        gaps.append(_gap("missing-tasks", "tasks", "No executable task collection is available."))
    if not run_items:
        gaps.append(_gap("missing-runs", "runs", "No validated project-run history is available."))
    gaps.sort(key=lambda item: (item["code"], item["artifact"], item["reason"]))

    inputs = {
        "knowledge": _input(
            "available" if pages else "missing",
            len(pages),
            f"knowledge-schema-v{KNOWLEDGE_SCHEMA_VERSION}" if pages else None,
            knowledge_digest if pages else None,
        ),
        "experiment_chains": _input(
            "available" if chains is not None else "missing",
            sum(len(chains.get(key, [])) for key in ("configs", "runs", "results", "claims")) if chains is not None else 0,
            EXPERIMENT_CHAINS_VERSION if chains is not None else None,
            sha256_bytes(chains_bytes) if chains_bytes is not None else None,
        ),
        "sources": _input(
            "available" if source_registry is not None else "missing",
            len(source_registry.records) if source_registry is not None else 0,
            SOURCE_REGISTRY_VERSION if source_registry is not None else None,
            sha256_bytes(source_bytes) if source_bytes is not None else None,
        ),
        "evidence": _input(
            "available" if evidence_registry is not None else "missing",
            len(evidence_registry.records) if evidence_registry is not None else 0,
            EVIDENCE_REGISTRY_VERSION if evidence_registry is not None else None,
            sha256_bytes(evidence_bytes) if evidence_bytes is not None else None,
        ),
        "goal": _input(
            "available" if goal is not None else "missing",
            1 + len(goal.milestones) if goal is not None else 0,
            GOAL_VERSION if goal is not None else None,
            sha256_bytes(goal_bytes) if goal_bytes is not None else None,
        ),
        "tasks": _input(
            "available" if tasks is not None else "missing",
            len(tasks.tasks) if tasks is not None else 0,
            TASK_VERSION if tasks is not None else None,
            sha256_bytes(task_bytes) if task_bytes is not None else None,
        ),
        "runs": _input(
            "available" if run_items else "missing",
            len(run_items),
            "project-run-v1" if run_items else None,
            run_digest,
        ),
    }
    body_without_id = {
        "schema_version": PROJECT_STATE_SCHEMA_VERSION,
        "kind": PROJECT_STATE_KIND,
        "state_version": PROJECT_STATE_VERSION,
        "project_id": registration.project_id,
        "generated_at": _timestamp(generated_at),
        "registration": _safe_registration_binding(registration, project_bytes),
        "manifest": manifest_binding(manifest, manifest_digest),
        "inputs": inputs,
        "experiments": experiments,
        "results": results,
        "open_questions": open_questions,
        "blockers": blockers,
        "stale_knowledge": stale_knowledge_collection,
        "stale_evidence": stale_evidence_collection,
        "recent_changes": recent,
        "gaps": gaps,
    }
    artifact_id = _artifact_id(body_without_id)
    return ProjectState(
        project_id=registration.project_id,
        generated_at=body_without_id["generated_at"],
        registration=body_without_id["registration"],
        manifest=body_without_id["manifest"],
        inputs=body_without_id["inputs"],
        experiments=experiments,
        results=results,
        open_questions=open_questions,
        blockers=blockers,
        stale_knowledge=stale_knowledge_collection,
        stale_evidence=stale_evidence_collection,
        recent_changes=recent,
        gaps=tuple(gaps),
        artifact_id=artifact_id,
    )


class ProjectStateStore:
    """Read/write facade for one registered project's current state snapshot."""

    def __init__(self, workspace_root: str | Path, project_id: str) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.project_id = validate_project_id(project_id)
        self.registration = load_registered_project(self.workspace_root, self.project_id)

    @property
    def machine_path(self) -> Path:
        return self.registration.layout.project_state_file

    def load(self) -> ProjectState:
        # Use the same currentness checks as the module-level loader.  A Store
        # object may outlive an inventory or registration update, so parsing the
        # bytes alone is not an authorization to treat the snapshot as current.
        return load_project_state(
            self.workspace_root,
            self.project_id,
        )

    def write(self, state: ProjectState, *, lock_timeout_seconds: float = 5.0) -> Path:
        if not isinstance(state, ProjectState) or state.project_id != self.project_id:
            raise ProjectStateError("state must belong to the registered project")
        payload = _validate_payload(state.as_dict(), project_id=self.project_id)
        # This method is intentionally a low-level current-artifact writer.  It
        # still uses the shared machine lock and does not create any Markdown,
        # while refusing to publish a snapshot bound to an older registration or
        # Manifest.
        with locked_project(self.workspace_root, self.project_id, timeout_seconds=lock_timeout_seconds) as locked:
            project_bytes = _read_machine_file(
                locked.registration.layout,
                locked.registration.project_file,
                optional=False,
            )
            expected_registration = _safe_registration_binding(locked.registration, project_bytes)
            if payload["registration"] != expected_registration:
                raise ProjectStateError("state is stale for the current registration")
            expected_manifest = manifest_binding(locked.manifest, locked.manifest_sha256)
            if payload["manifest"] != expected_manifest:
                raise ProjectStateError("state is stale for the current Manifest")
            atomic_write_json(
                locked.registration.layout.project_state_file,
                payload,
                layout=locked.registration.layout,
                before_replace=lambda: _assert_inputs_unchanged(
                    locked.registration,
                    locked.manifest_bytes,
                    project_bytes,
                ),
                error_label="project-state",
            )
            return locked.registration.layout.project_state_file


def load_project_state(
    workspace_root: str | Path,
    project_id: str,
    *,
    lock_timeout_seconds: float = 5.0,
) -> ProjectState:
    """Load the current state artifact and bind it to the current registration."""

    with locked_project(
        workspace_root, project_id, timeout_seconds=lock_timeout_seconds
    ) as locked:
        target = locked.registration.layout.project_state_file
        try:
            raw = read_stable_regular_file(
                locked.registration.layout.indexes_dir,
                target,
                reject_redirection=True,
                capture_bytes=True,
            ).data
        except StableFileMissingError as exc:
            raise ProjectStateNotFoundError(
                f"no project-state artifact exists for {locked.registration.project_id}"
            ) from exc
        except StableFileAccessError as exc:
            raise ProjectStateError("project-state artifact could not be read safely") from exc
        if raw is None:
            raise ProjectStateError("project-state artifact bytes were not captured")
        state = parse_project_state(raw, project_id=locked.registration.project_id)
        if state.manifest != manifest_binding(locked.manifest, locked.manifest_sha256):
            raise ProjectStateError("current project-state artifact is stale for the Manifest")
        project_bytes = _read_machine_file(
            locked.registration.layout, locked.registration.project_file, optional=False
        )
        expected_registration = _safe_registration_binding(locked.registration, project_bytes)
        if dict(state.registration) != expected_registration:
            raise ProjectStateError("current project-state artifact is stale for registration")
        return state


def generate_project_state(
    workspace_root: str | Path,
    project_id: str,
    *,
    generated_at: object | None = None,
    lock_timeout_seconds: float = 5.0,
) -> ProjectStateResult:
    """Rebuild and atomically publish one deterministic project-state snapshot."""

    with locked_project(
        workspace_root, project_id, timeout_seconds=lock_timeout_seconds
    ) as locked:
        project_bytes = _read_machine_file(
            locked.registration.layout,
            locked.registration.project_file,
            optional=False,
        )
        state = _build_state(
            registration=locked.registration,
            project_bytes=project_bytes,
            manifest=locked.manifest,
            manifest_digest=locked.manifest_sha256,
            generated_at=generated_at,
        )
        target = locked.registration.layout.project_state_file
        atomic_write_json(
            target,
            state.as_dict(),
            layout=locked.registration.layout,
            before_replace=lambda: _assert_inputs_unchanged(
                locked.registration,
                locked.manifest_bytes,
                project_bytes,
            ),
            error_label="project-state",
        )
        committed = parse_project_state(
            target.read_bytes(), project_id=locked.registration.project_id
        )
        if committed != state:
            raise ProjectStateError("committed project-state differs from deterministic output")
        return ProjectStateResult(
            project_id=locked.registration.project_id,
            manifest_file=locked.registration.layout.manifest_file,
            project_state_file=target,
            state=committed,
        )


def _assert_inputs_unchanged(
    registration: Any,
    manifest_bytes: bytes,
    project_bytes: bytes,
) -> None:
    current_manifest_bytes(registration, manifest_bytes)
    current_project = _read_machine_file(
        registration.layout, registration.layout.project_file, optional=False
    )
    if current_project != project_bytes:
        raise ProjectStateError(
            "project registration changed before project-state was committed"
        )


def rebuild_project_state(
    workspace_root: str | Path,
    project_id: str,
    *,
    generated_at: object | None = None,
    lock_timeout_seconds: float = 5.0,
) -> ProjectStateResult:
    return generate_project_state(
        workspace_root,
        project_id,
        generated_at=generated_at,
        lock_timeout_seconds=lock_timeout_seconds,
    )


# Public aliases used by the Core roadmap and compatibility tests.
build_project_state = generate_project_state
snapshot_project_state = generate_project_state


def write_project_state(
    workspace_root: str | Path,
    project_id: str,
    state: ProjectState,
    *,
    lock_timeout_seconds: float = 5.0,
) -> Path:
    return ProjectStateStore(workspace_root, project_id).write(
        state, lock_timeout_seconds=lock_timeout_seconds
    )


__all__ = [
    "PROJECT_STATE_SCHEMA_VERSION",
    "PROJECT_STATE_KIND",
    "PROJECT_STATE_VERSION",
    "PROJECT_STATE_MACHINE_FILENAME",
    "PROJECT_STATE_RELATIVE_PATH",
    "ProjectStateError",
    "UnsupportedProjectStateSchemaVersionError",
    "ProjectStateNotFoundError",
    "ProjectState",
    "ProjectStateSnapshot",
    "ProjectStateResult",
    "ProjectStateStore",
    "parse_project_state",
    "serialize_project_state",
    "generate_project_state",
    "rebuild_project_state",
    "load_project_state",
    "write_project_state",
    "build_project_state",
    "snapshot_project_state",
]
