#!/usr/bin/env python3
"""E-05 deterministic research provenance and linkage artifact.

The host explicitly declares semantic entities and directed relations. Core keeps
project implementation, paper claims, and inference separate, validates Evidence
references and current Manifest paths, and persists only a bounded machine index.
Without semantic input it emits an honest metadata-only inventory of paper,
dataset, implementation, and experiment candidates.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

if __package__:
    from .advisory_lock import DEFAULT_LOCK_TIMEOUT_SECONDS
    from .project_analysis import (
        ProjectAnalysisError,
        atomic_write_json,
        canonical_json_bytes,
        current_manifest_bytes,
        exact_mapping,
        locked_project,
        manifest_binding,
        manifest_records_by_path,
        parse_canonical_json,
        sha256_bytes,
        text,
    )
    from .project_layout import CURRENT_SCHEMA_VERSION, validate_project_id
else:  # pragma: no cover
    from advisory_lock import DEFAULT_LOCK_TIMEOUT_SECONDS  # type: ignore[no-redef]
    from project_analysis import (  # type: ignore[no-redef]
        ProjectAnalysisError,
        atomic_write_json,
        canonical_json_bytes,
        current_manifest_bytes,
        exact_mapping,
        locked_project,
        manifest_binding,
        manifest_records_by_path,
        parse_canonical_json,
        sha256_bytes,
        text,
    )
    from project_layout import CURRENT_SCHEMA_VERSION, validate_project_id  # type: ignore[no-redef]


RESEARCH_LINKAGE_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
RESEARCH_LINKAGE_KIND = "llmwiki-research-linkage"
RESEARCH_LINKAGE_VERSION = "research-linkage-v1"
RESEARCH_LINKAGE_FILENAME = "research-linkage.json"
MAX_ENTITIES = 2048
MAX_RELATIONS = 8192
MAX_EVIDENCE_IDS = 128
MAX_TEXT_BYTES = 4096
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_EVIDENCE_RE = re.compile(r"^evd-[0-9a-f]{64}$")
_PATH_RE = re.compile(r"^(?!/)(?![A-Za-z]:)(?!.*(?:^|/)\.\.(?:/|$))[^\\\x00]+$")
_ENTITY_KINDS = frozenset({"paper", "method", "innovation", "dataset", "implementation", "claim", "experiment", "result", "unknown"})
_ASSERTION_CLASSES = frozenset({"implementation", "paper-claim", "inference", "metadata"})
_RELATIONS = frozenset({"implements", "describes", "introduces", "uses", "evaluates", "supports", "contradicts", "derived-from", "references"})
_CERTAINTIES = frozenset({"observed", "inferred", "uncertain"})
_TOP = frozenset({"schema_version", "kind", "linkage_version", "project_id", "manifest", "derivation", "entities", "relations", "provenance_counts", "coverage", "gaps", "artifact_id"})
_MANIFEST = frozenset({"manifest_version", "scan_generation", "ordinary_file_count", "ordinary_byte_count", "sha256"})
_DERIVATION = frozenset({"mode", "source_content_read", "llm_used", "host_observations", "semantic_input"})
_ENTITY = frozenset({"id", "kind", "title", "path", "assertion_class", "summary", "evidence_ids", "certainty", "uncertainty"})
_RELATION = frozenset({"id", "source", "target", "relation", "assertion_class", "summary", "evidence_ids", "certainty", "uncertainty"})
_PROVENANCE = frozenset({"implementation", "paper-claim", "inference", "metadata"})
_COVERAGE = frozenset({"manifest_files", "represented_files", "entities", "relations", "papers", "methods", "datasets", "implementations", "claims", "inferences", "evidence_backed_entities", "evidence_backed_relations"})


class ResearchLinkageError(ProjectAnalysisError):
    """E-05 validation, persistence, or currentness failure."""


def _safe_id(value: object, label: str) -> str:
    if type(value) is not str or not _ID_RE.fullmatch(value):
        raise ResearchLinkageError(f"{label} must be a bounded path-safe stable ID")
    return value


def _path(value: object, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not _PATH_RE.fullmatch(value) or value in {".", ".."} or "//" in value:
        raise ResearchLinkageError(f"{label} must be a canonical project-relative POSIX path")
    return value


def _evidence(value: Iterable[str], label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ResearchLinkageError(f"{label} must be a sequence")
    values = tuple(value)
    if len(values) > MAX_EVIDENCE_IDS or len(values) != len(set(values)):
        raise ResearchLinkageError(f"{label} must be duplicate-free and bounded")
    if any(type(item) is not str or not _EVIDENCE_RE.fullmatch(item) for item in values):
        raise ResearchLinkageError(f"{label} contains an invalid Evidence ID")
    return tuple(sorted(values))


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return text(value, label=label, max_bytes=MAX_TEXT_BYTES)


def _assertion(value: object, label: str) -> str:
    if type(value) is not str or value not in _ASSERTION_CLASSES:
        raise ResearchLinkageError(f"{label} must be one of {sorted(_ASSERTION_CLASSES)!r}")
    return value


def _certainty(value: object, label: str) -> str:
    if type(value) is not str or value not in _CERTAINTIES:
        raise ResearchLinkageError(f"{label} must be one of {sorted(_CERTAINTIES)!r}")
    return value


@dataclass(frozen=True)
class ResearchEntityObservation:
    entity_id: str
    kind: str
    title: str
    path: str | None = None
    assertion_class: str = "inference"
    summary: str = ""
    evidence_ids: tuple[str, ...] = ()
    certainty: str = "observed"
    uncertainty: str | None = None

    def __post_init__(self) -> None:
        _safe_id(self.entity_id, "entity_id")
        if self.kind not in _ENTITY_KINDS:
            raise ResearchLinkageError(f"entity kind must be one of {sorted(_ENTITY_KINDS)!r}")
        text(self.title, label="entity title", max_bytes=MAX_TEXT_BYTES)
        _path(self.path, "entity path")
        _assertion(self.assertion_class, "entity assertion_class")
        text(self.summary, label="entity summary", max_bytes=MAX_TEXT_BYTES, allow_empty=True)
        _evidence(self.evidence_ids, "entity evidence_ids")
        _certainty(self.certainty, "entity certainty")
        if self.certainty == "uncertain" and not self.uncertainty:
            raise ResearchLinkageError("uncertain entity requires a reason")
        _optional_text(self.uncertainty, "entity uncertainty")
        if self.assertion_class == "paper-claim" and self.kind not in {"claim", "innovation", "method"}:
            raise ResearchLinkageError("paper-claim provenance is only valid for claim/method/innovation entities")

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.entity_id, "kind": self.kind, "title": self.title, "path": self.path, "assertion_class": self.assertion_class, "summary": self.summary, "evidence_ids": list(sorted(self.evidence_ids)), "certainty": self.certainty, "uncertainty": self.uncertainty}

    @classmethod
    def from_dict(cls, value: object) -> "ResearchEntityObservation":
        item = exact_mapping(value, _ENTITY, label="research entity")
        if type(item["evidence_ids"]) is not list:
            raise ResearchLinkageError("entity evidence_ids must be an array")
        return cls(item["id"], item["kind"], item["title"], item["path"], item["assertion_class"], item["summary"], tuple(item["evidence_ids"]), item["certainty"], item["uncertainty"])


@dataclass(frozen=True)
class ResearchRelationObservation:
    source: str
    target: str
    relation: str
    assertion_class: str
    summary: str = ""
    evidence_ids: tuple[str, ...] = ()
    certainty: str = "observed"
    uncertainty: str | None = None

    def __post_init__(self) -> None:
        _safe_id(self.source, "relation source")
        _safe_id(self.target, "relation target")
        if self.source == self.target:
            raise ResearchLinkageError("research relations cannot be self-loops")
        if self.relation not in _RELATIONS:
            raise ResearchLinkageError(f"relation must be one of {sorted(_RELATIONS)!r}")
        _assertion(self.assertion_class, "relation assertion_class")
        text(self.summary, label="relation summary", max_bytes=MAX_TEXT_BYTES, allow_empty=True)
        _evidence(self.evidence_ids, "relation evidence_ids")
        _certainty(self.certainty, "relation certainty")
        if self.certainty == "uncertain" and not self.uncertainty:
            raise ResearchLinkageError("uncertain relation requires a reason")
        _optional_text(self.uncertainty, "relation uncertainty")
        if self.assertion_class == "inference" and self.certainty == "observed":
            raise ResearchLinkageError("inference relations cannot claim observed certainty")

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target, "relation": self.relation, "assertion_class": self.assertion_class, "summary": self.summary, "evidence_ids": list(sorted(self.evidence_ids)), "certainty": self.certainty, "uncertainty": self.uncertainty}

    @classmethod
    def from_dict(cls, value: object) -> "ResearchRelationObservation":
        item = exact_mapping(value, _RELATION - {"id"}, label="research relation observation")
        if type(item["evidence_ids"]) is not list:
            raise ResearchLinkageError("relation evidence_ids must be an array")
        return cls(item["source"], item["target"], item["relation"], item["assertion_class"], item["summary"], tuple(item["evidence_ids"]), item["certainty"], item["uncertainty"])


@dataclass(frozen=True)
class ResearchLinkageResult:
    project_id: str
    manifest_file: Path
    research_linkage_file: Path
    research_linkage: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "manifest_file": str(self.manifest_file), "research_linkage_file": str(self.research_linkage_file), "research_linkage": self.research_linkage}


def _stable(prefix: str, *parts: object) -> str:
    return f"{prefix}-" + hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:32]


def _fallback(manifest: Any) -> list[ResearchEntityObservation]:
    result: list[ResearchEntityObservation] = []
    mapping = {
        "paper": ("paper", "metadata"),
        "bibliography": ("paper", "metadata"),
        "dataset": ("dataset", "metadata"),
        "source_code": ("implementation", "implementation"),
        "notebook": ("implementation", "implementation"),
        "experiment": ("experiment", "metadata"),
        "result": ("result", "metadata"),
    }
    for path, record in sorted(manifest_records_by_path(manifest).items()):
        classification = record.get("classification")
        role = classification.get("research_role") if isinstance(classification, dict) else None
        mapped = mapping.get(role)
        if mapped is None:
            continue
        kind, assertion = mapped
        result.append(ResearchEntityObservation(
            entity_id=_stable("research", path, record.get("content_sha256")),
            kind=kind,
            title=path,
            path=path,
            assertion_class=assertion,
            summary="Manifest metadata candidate; semantic relationship has not been established.",
            certainty="uncertain",
            uncertainty="No grounded semantic observation was supplied",
        ))
    return result[:MAX_ENTITIES]


def _normalize(observations: Iterable[ResearchEntityObservation | ResearchRelationObservation | Mapping[str, Any]]) -> tuple[list[ResearchEntityObservation], list[ResearchRelationObservation]]:
    entities: list[ResearchEntityObservation] = []
    relations: list[ResearchRelationObservation] = []
    for observation in observations:
        if isinstance(observation, ResearchEntityObservation):
            entities.append(observation)
        elif isinstance(observation, ResearchRelationObservation):
            relations.append(observation)
        elif isinstance(observation, Mapping):
            kind = observation.get("record_type")
            data = dict(observation)
            data.pop("record_type", None)
            if kind in {"entity", "research-entity"} or "title" in data:
                data.setdefault("summary", "")
                data.setdefault("path", None)
                data.setdefault("assertion_class", "inference")
                data.setdefault("evidence_ids", [])
                data.setdefault("certainty", "observed")
                data.setdefault("uncertainty", None)
                data.setdefault("id", data.pop("entity_id", None))
                entities.append(ResearchEntityObservation.from_dict(data))
            elif kind in {"relation", "research-relation"} or "source" in data:
                data.setdefault("summary", "")
                data.setdefault("evidence_ids", [])
                data.setdefault("certainty", "observed")
                data.setdefault("uncertainty", None)
                relations.append(ResearchRelationObservation.from_dict(data))
            else:
                raise ResearchLinkageError("observation must declare a research entity or relation")
        else:
            raise ResearchLinkageError("unsupported research-linkage observation")
    if len(entities) > MAX_ENTITIES or len(relations) > MAX_RELATIONS:
        raise ResearchLinkageError("research-linkage observations exceed bounded limits")
    return entities, relations


def _relation_id(item: ResearchRelationObservation) -> str:
    return _stable("link", item.source, item.target, item.relation, item.assertion_class, item.summary)


def _artifact_id(payload: Mapping[str, Any]) -> str:
    return "linkage-" + sha256_bytes(canonical_json_bytes(payload))


def build_research_linkage(manifest: Any, *, project_id: str, manifest_sha256: str, observations: Iterable[ResearchEntityObservation | ResearchRelationObservation | Mapping[str, Any]] = ()) -> dict[str, Any]:
    project_id = validate_project_id(project_id)
    records = manifest_records_by_path(manifest)
    raw = tuple(observations)
    if raw:
        entities, relations = _normalize(raw)
        mode = "host-observations"
    else:
        entities, relations = _fallback(manifest), []
        mode = "manifest-metadata-fallback"
    by_id: dict[str, ResearchEntityObservation] = {}
    for entity in entities:
        if entity.entity_id in by_id:
            raise ResearchLinkageError(f"duplicate research entity id: {entity.entity_id}")
        if entity.path is not None and entity.path not in records:
            raise ResearchLinkageError(f"research entity path absent from current Manifest: {entity.path}")
        by_id[entity.entity_id] = entity
    relation_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for relation in relations:
        if relation.source not in by_id or relation.target not in by_id:
            raise ResearchLinkageError("research relation endpoint is not a declared entity")
        key = (relation.source, relation.target, relation.relation, relation.assertion_class, relation.summary)
        if key in seen:
            raise ResearchLinkageError("duplicate research relation")
        seen.add(key)
        row = relation.as_dict()
        row["id"] = _relation_id(relation)
        relation_rows.append(row)
    entity_rows = [item.as_dict() for item in sorted(by_id.values(), key=lambda item: item.entity_id)]
    relation_rows.sort(key=lambda item: (item["source"], item["target"], item["relation"], item["assertion_class"], item["summary"]))
    provenance = {key: 0 for key in sorted(_ASSERTION_CLASSES)}
    for item in entity_rows + relation_rows:
        provenance[item["assertion_class"]] += 1
    coverage = {
        "manifest_files": len(records),
        "represented_files": len({item["path"] for item in entity_rows if item["path"]}),
        "entities": len(entity_rows),
        "relations": len(relation_rows),
        "papers": sum(item["kind"] == "paper" for item in entity_rows),
        "methods": sum(item["kind"] == "method" for item in entity_rows),
        "datasets": sum(item["kind"] == "dataset" for item in entity_rows),
        "implementations": sum(item["kind"] == "implementation" for item in entity_rows),
        "claims": sum(item["kind"] == "claim" for item in entity_rows),
        "inferences": provenance["inference"],
        "evidence_backed_entities": sum(bool(item["evidence_ids"]) for item in entity_rows),
        "evidence_backed_relations": sum(bool(item["evidence_ids"]) for item in relation_rows),
    }
    gaps: list[str] = []
    if not any(item["kind"] == "paper" for item in entity_rows):
        gaps.append("No paper was identified from current grounded inputs")
    if not any(item["kind"] == "method" for item in entity_rows):
        gaps.append("No method-to-implementation link was established")
    if not relation_rows:
        gaps.append("No grounded research relationships were supplied")
    body: dict[str, Any] = {
        "schema_version": RESEARCH_LINKAGE_SCHEMA_VERSION,
        "kind": RESEARCH_LINKAGE_KIND,
        "linkage_version": RESEARCH_LINKAGE_VERSION,
        "project_id": project_id,
        "manifest": manifest_binding(manifest, manifest_sha256),
        "derivation": {"mode": mode, "source_content_read": False, "llm_used": False, "host_observations": bool(raw), "semantic_input": bool(raw)},
        "entities": entity_rows,
        "relations": relation_rows,
        "provenance_counts": provenance,
        "coverage": coverage,
        "gaps": gaps,
    }
    body["artifact_id"] = _artifact_id(body)
    return body


def validate_research_linkage(payload: Mapping[str, Any], *, project_id: str) -> dict[str, Any]:
    top = exact_mapping(payload, _TOP, label="research-linkage artifact")
    if top["schema_version"] != RESEARCH_LINKAGE_SCHEMA_VERSION or top["kind"] != RESEARCH_LINKAGE_KIND or top["linkage_version"] != RESEARCH_LINKAGE_VERSION:
        raise ResearchLinkageError("unsupported research-linkage schema/version")
    if top["project_id"] != validate_project_id(project_id):
        raise ResearchLinkageError("research-linkage project binding mismatch")
    exact_mapping(top["manifest"], _MANIFEST, label="research-linkage Manifest binding")
    derivation = exact_mapping(top["derivation"], _DERIVATION, label="research-linkage derivation")
    if derivation["mode"] not in {"host-observations", "manifest-metadata-fallback"} or derivation["source_content_read"] is not False or derivation["llm_used"] is not False:
        raise ResearchLinkageError("invalid research-linkage derivation boundary")
    if type(top["entities"]) is not list or type(top["relations"]) is not list or type(top["gaps"]) is not list:
        raise ResearchLinkageError("research-linkage collections must be arrays")
    if len(top["entities"]) > MAX_ENTITIES or len(top["relations"]) > MAX_RELATIONS:
        raise ResearchLinkageError("research-linkage limits exceeded")
    entities: dict[str, ResearchEntityObservation] = {}
    for item in top["entities"]:
        entity = ResearchEntityObservation.from_dict(item)
        if entity.entity_id in entities:
            raise ResearchLinkageError("duplicate research entity")
        entities[entity.entity_id] = entity
    expected_relations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for item in top["relations"]:
        row = exact_mapping(item, _RELATION, label="research relation")
        observation = ResearchRelationObservation.from_dict({key: value for key, value in row.items() if key != "id"})
        if observation.source not in entities or observation.target not in entities:
            raise ResearchLinkageError("research relation endpoint is missing")
        key = (observation.source, observation.target, observation.relation, observation.assertion_class, observation.summary)
        if key in seen:
            raise ResearchLinkageError("duplicate research relation")
        seen.add(key)
        expected = observation.as_dict()
        expected["id"] = _relation_id(observation)
        if row != expected:
            raise ResearchLinkageError("research relation stable identity mismatch")
        expected_relations.append(expected)
    if top["entities"] != sorted(top["entities"], key=lambda item: item["id"]) or top["relations"] != sorted(top["relations"], key=lambda item: (item["source"], item["target"], item["relation"], item["assertion_class"], item["summary"])):
        raise ResearchLinkageError("research-linkage collections are not in canonical order")
    provenance = exact_mapping(top["provenance_counts"], _PROVENANCE, label="research provenance counts")
    expected_provenance = {key: 0 for key in sorted(_ASSERTION_CLASSES)}
    for item in top["entities"] + top["relations"]:
        expected_provenance[item["assertion_class"]] += 1
    if provenance != expected_provenance:
        raise ResearchLinkageError("research provenance counts are inconsistent")
    coverage = exact_mapping(top["coverage"], _COVERAGE, label="research-linkage coverage")
    expected_coverage = {
        "manifest_files": coverage["manifest_files"],
        "represented_files": len({item["path"] for item in top["entities"] if item["path"]}),
        "entities": len(top["entities"]),
        "relations": len(top["relations"]),
        "papers": sum(item["kind"] == "paper" for item in top["entities"]),
        "methods": sum(item["kind"] == "method" for item in top["entities"]),
        "datasets": sum(item["kind"] == "dataset" for item in top["entities"]),
        "implementations": sum(item["kind"] == "implementation" for item in top["entities"]),
        "claims": sum(item["kind"] == "claim" for item in top["entities"]),
        "inferences": expected_provenance["inference"],
        "evidence_backed_entities": sum(bool(item["evidence_ids"]) for item in top["entities"]),
        "evidence_backed_relations": sum(bool(item["evidence_ids"]) for item in top["relations"]),
    }
    if coverage != expected_coverage:
        raise ResearchLinkageError("research-linkage coverage is inconsistent")
    for gap in top["gaps"]:
        text(gap, label="research-linkage gap", max_bytes=MAX_TEXT_BYTES)
    without_id = dict(top)
    del without_id["artifact_id"]
    if top["artifact_id"] != _artifact_id(without_id):
        raise ResearchLinkageError("research-linkage artifact stable identity mismatch")
    return top


def load_research_linkage(path: str | Path, *, project_id: str) -> dict[str, Any]:
    target = Path(path)
    if target.is_symlink():
        raise ResearchLinkageError("research-linkage artifact cannot be a symbolic link")
    try:
        payload = parse_canonical_json(target.read_bytes(), label=str(target))
    except ResearchLinkageError:
        raise
    except ProjectAnalysisError as exc:
        raise ResearchLinkageError(str(exc)) from exc
    except OSError as exc:
        raise ResearchLinkageError(f"could not read research-linkage artifact: {exc}") from exc
    return validate_research_linkage(payload, project_id=project_id)


def generate_research_linkage(workspace_root: str | Path, project_id: str, *, observations: Iterable[ResearchEntityObservation | ResearchRelationObservation | Mapping[str, Any]] = (), lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> ResearchLinkageResult:
    with locked_project(workspace_root, project_id, timeout_seconds=lock_timeout_seconds) as locked:
        expected = build_research_linkage(locked.manifest, project_id=locked.registration.project_id, manifest_sha256=locked.manifest_sha256, observations=observations)
        target = locked.registration.layout.research_linkage_file
        atomic_write_json(target, expected, layout=locked.registration.layout, before_replace=lambda: current_manifest_bytes(locked.registration, locked.manifest_bytes), error_label="research-linkage")
        committed = load_research_linkage(target, project_id=locked.registration.project_id)
        if committed != expected:
            raise ResearchLinkageError("committed research-linkage differs from deterministic output")
        return ResearchLinkageResult(locked.registration.project_id, locked.registration.layout.manifest_file, target, committed)


def load_current_research_linkage(workspace_root: str | Path, project_id: str, *, lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> dict[str, Any]:
    with locked_project(workspace_root, project_id, timeout_seconds=lock_timeout_seconds) as locked:
        target = locked.registration.layout.research_linkage_file
        try:
            actual = load_research_linkage(target, project_id=locked.registration.project_id)
        except FileNotFoundError as exc:
            raise ResearchLinkageError("current research-linkage artifact is missing") from exc
        if actual["manifest"] != manifest_binding(locked.manifest, locked.manifest_sha256):
            raise ResearchLinkageError("research-linkage artifact is stale for the current Manifest")
        records = manifest_records_by_path(locked.manifest)
        if actual["coverage"]["manifest_files"] != len(records):
            raise ResearchLinkageError("research-linkage Manifest coverage is stale")
        for entity in actual["entities"]:
            if entity["path"] is not None and entity["path"] not in records:
                raise ResearchLinkageError("research-linkage path is absent from current Manifest")
        if actual["derivation"]["mode"] == "manifest-metadata-fallback":
            expected = build_research_linkage(locked.manifest, project_id=locked.registration.project_id, manifest_sha256=locked.manifest_sha256)
            if actual != expected:
                raise ResearchLinkageError("metadata-only research linkage is not deterministic current truth")
        current_manifest_bytes(locked.registration, locked.manifest_bytes)
        return actual


__all__ = [
    "RESEARCH_LINKAGE_FILENAME", "RESEARCH_LINKAGE_KIND", "RESEARCH_LINKAGE_VERSION",
    "ResearchEntityObservation", "ResearchLinkageError", "ResearchLinkageResult",
    "ResearchRelationObservation", "build_research_linkage", "generate_research_linkage",
    "load_current_research_linkage", "load_research_linkage", "validate_research_linkage",
]
