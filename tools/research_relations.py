#!/usr/bin/env python3
"""Strict project-scoped research entity and relationship contract (F-03A).

The host Agent supplies research meaning: explicit entity identities, relation
labels, and Evidence references. This module validates deterministic schema,
project/path bindings, referential integrity, and builds a reversible backlink
projection for later Markdown and Web renderers. It never reads or writes project
files, infers relations from prose, merges same-named objects, or exposes a
CLI/MCP/Web surface.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, cast

from tools.knowledge_artifacts import (
    KNOWLEDGE_SCHEMA_VERSION,
    ArtifactPageRole,
    KnowledgeArtifactError,
    KnowledgeArtifactPathError,
    KnowledgePage,
    artifact_contract_for_path,
    parse_knowledge_page,
)
from tools.project_layout import InvalidProjectIdError, validate_project_id

RESEARCH_RELATION_SCHEMA_VERSION = 1
RESEARCH_RELATION_REGISTRY_KIND = "llmwiki-research-relation-registry"
RESEARCH_ENTITY_KIND = "llmwiki-research-entity"
RESEARCH_RELATION_KIND = "llmwiki-research-relation"
RESEARCH_PROJECT_INDEX_KIND = "llmwiki-research-project-index"
RESEARCH_RELATION_REGISTRY_VERSION = "research-relations-v1"
RESEARCH_ENTITY_VERSION = "research-entity-v1"
RESEARCH_RELATION_VERSION = "research-relation-v1"
RESEARCH_PROJECT_INDEX_VERSION = "research-project-index-v1"
RESEARCH_IDENTITY_VERSION = "research-entity-relation-identity-v1"

ResearchEntityType = Literal[
    "paper", "method", "dataset", "experiment", "metric", "result",
    "claim", "decision", "question", "source",
]
RESEARCH_ENTITY_TYPES: frozenset[str] = frozenset(
    {"paper", "method", "dataset", "experiment", "metric", "result",
     "claim", "decision", "question", "source"}
)
_DIRECT_PAGE_TYPES: Mapping[str, tuple[str, ArtifactPageRole]] = MappingProxyType(
    {
        "paper": ("paper", "detail"),
        "method": ("method", "detail"),
        "dataset": ("dataset", "detail"),
        "experiment": ("experiment", "detail"),
        "result": ("result", "detail"),
        "claim": ("claim", "detail"),
        "decision": ("decision", "detail"),
        "source": ("source", "detail"),
    }
)
_EMBEDDED_PAGE_TYPES: Mapping[str, tuple[str, ArtifactPageRole]] = MappingProxyType(
    {
        # F-01 reserves metric without inventing a project metrics/ directory.
        "metric": ("result", "detail"),
        # open-questions.md contains separately anchored question entities.
        "question": ("open_question", "singleton"),
    }
)
_ENTITY_ID_PATTERN = re.compile(r"ent-[0-9a-f]{64}")
_RELATION_ID_PATTERN = re.compile(r"rel-[0-9a-f]{64}")
_EVIDENCE_ID_PATTERN = re.compile(r"evd-[0-9a-f]{64}")
_RELATION_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_ANCHOR_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SUMMARY_FIELDS = frozenset(
    {"schema_version", "kind", "registry_version", "record_type", "project_id",
     "identity_version", "entity_count", "relation_count"}
)
_ENTITY_FIELDS = frozenset(
    {"schema_version", "kind", "entity_version", "record_type", "project_id",
     "identity_version", "entity_id", "entity_type", "identity_key", "title",
     "knowledge_path", "anchor"}
)
_RELATION_FIELDS = frozenset(
    {"schema_version", "kind", "relation_version", "record_type", "project_id",
     "identity_version", "relation_id", "relation_type", "source_entity_id",
     "target_entity_id", "evidence_ids"}
)


class ResearchRelationError(KnowledgeArtifactError):
    """Base error for malformed or inconsistent F-03 research relations."""


class UnsupportedResearchRelationSchemaVersionError(ResearchRelationError):
    """Raised when a relation artifact uses a future unsupported schema."""


class ResearchEntityBindingError(ResearchRelationError):
    """Raised when an entity does not bind to current Knowledge Schema v2."""


class ResearchRelationConflictError(ResearchRelationError):
    """Raised when entity/relation identities or references conflict."""


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ResearchRelationError(
            "research relation data must be canonical JSON-compatible"
        ) from exc


def _identity_digest(kind: str, fields: Mapping[str, object]) -> str:
    payload = {"identity_version": RESEARCH_IDENTITY_VERSION, "kind": kind,
               **dict(fields)}
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _validated_project_id(value: object) -> str:
    if not isinstance(value, str):
        raise ResearchRelationError("project_id must be a string")
    try:
        return validate_project_id(value)
    except InvalidProjectIdError as exc:
        raise ResearchRelationError(str(exc)) from exc


def _bounded_text(value: object, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ResearchRelationError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ResearchRelationError(
            f"{field_name} must be non-empty without outer whitespace"
        )
    if len(value) > maximum:
        raise ResearchRelationError(
            f"{field_name} must contain at most {maximum} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ResearchRelationError(f"{field_name} must not contain control characters")
    return value


def _entity_type(value: object) -> ResearchEntityType:
    if not isinstance(value, str) or value not in RESEARCH_ENTITY_TYPES:
        raise ResearchRelationError(
            "entity_type must be one of " + ", ".join(sorted(RESEARCH_ENTITY_TYPES))
        )
    return cast(ResearchEntityType, value)


def _relation_type(value: object) -> str:
    normalized = _bounded_text(value, "relation_type", maximum=80)
    if _RELATION_TYPE_PATTERN.fullmatch(normalized) is None:
        raise ResearchRelationError("relation_type must use lowercase kebab-case")
    return normalized


def _entity_id(value: object) -> str:
    if not isinstance(value, str) or _ENTITY_ID_PATTERN.fullmatch(value) is None:
        raise ResearchRelationError(
            "entity_id must use 'ent-' plus 64 lowercase hexadecimal digits"
        )
    return value


def _relation_id(value: object) -> str:
    if not isinstance(value, str) or _RELATION_ID_PATTERN.fullmatch(value) is None:
        raise ResearchRelationError(
            "relation_id must use 'rel-' plus 64 lowercase hexadecimal digits"
        )
    return value


def _anchor(value: object) -> str | None:
    if value is None:
        return None
    normalized = _bounded_text(value, "anchor", maximum=128)
    if _ANCHOR_PATTERN.fullmatch(normalized) is None:
        raise ResearchRelationError("anchor must use lowercase kebab-case without '#'")
    return normalized


def _evidence_ids(
    value: object,
    *,
    require_json_array: bool = True,
    require_canonical_order: bool = True,
) -> tuple[str, ...]:
    if require_json_array:
        if not isinstance(value, list):
            raise ResearchRelationError("evidence_ids must be a JSON array")
        items = value
    else:
        if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
            raise ResearchRelationError(
                "evidence_ids must be an iterable of Evidence IDs"
            )
        items = list(value)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str) or _EVIDENCE_ID_PATTERN.fullmatch(item) is None:
            raise ResearchRelationError(
                "evidence_ids entries must use 'evd-' plus 64 lowercase hex digits"
            )
        if item in seen:
            raise ResearchRelationConflictError(
                f"relation contains duplicate Evidence ID: {item}"
            )
        seen.add(item)
        normalized.append(item)
    if require_canonical_order and normalized != sorted(normalized):
        raise ResearchRelationError("evidence_ids must use canonical sorted order")
    return tuple(normalized if require_canonical_order else sorted(normalized))


def research_entity_id_for(
    project_id: str, entity_type: ResearchEntityType | str, identity_key: str,
) -> str:
    """Return a stable ID without deriving identity from a possibly shared title."""
    normalized_project = _validated_project_id(project_id)
    normalized_type = _entity_type(entity_type)
    normalized_key = _bounded_text(identity_key, "identity_key", maximum=512)
    digest = _identity_digest(
        "research-entity",
        {"project_id": normalized_project, "entity_type": normalized_type,
         "identity_key": normalized_key},
    )
    return f"ent-{digest}"


def research_relation_id_for(
    project_id: str, relation_type: str, source_entity_id: str,
    target_entity_id: str,
) -> str:
    """Return the stable ID for one explicit directed relation."""
    normalized_project = _validated_project_id(project_id)
    normalized_type = _relation_type(relation_type)
    source = _entity_id(source_entity_id)
    target = _entity_id(target_entity_id)
    digest = _identity_digest(
        "research-relation",
        {"project_id": normalized_project, "relation_type": normalized_type,
         "source_entity_id": source, "target_entity_id": target},
    )
    return f"rel-{digest}"


def _normalize_entity_location(
    entity_type: ResearchEntityType, knowledge_path: object, anchor: object,
) -> tuple[str | None, str | None]:
    normalized_anchor = _anchor(anchor)
    if knowledge_path is None:
        if normalized_anchor is not None:
            raise ResearchEntityBindingError(
                "an unmaterialized entity cannot declare a Markdown anchor"
            )
        return None, None
    if not isinstance(knowledge_path, str):
        raise ResearchEntityBindingError("knowledge_path must be a string or null")
    try:
        contract = artifact_contract_for_path(knowledge_path)
    except KnowledgeArtifactPathError as exc:
        raise ResearchEntityBindingError(str(exc)) from exc
    if entity_type in _DIRECT_PAGE_TYPES:
        expected_type, expected_role = _DIRECT_PAGE_TYPES[entity_type]
        if normalized_anchor is not None:
            raise ResearchEntityBindingError(
                f"{entity_type} detail entities bind to the page, not a body anchor"
            )
    else:
        expected_type, expected_role = _EMBEDDED_PAGE_TYPES[entity_type]
        if normalized_anchor is None:
            raise ResearchEntityBindingError(
                f"{entity_type} entities require an explicit body anchor"
            )
    if contract.artifact_type != expected_type or contract.page_role != expected_role:
        raise ResearchEntityBindingError(
            f"{entity_type} entity cannot bind to {contract.path!r} "
            f"({contract.artifact_type}/{contract.page_role})"
        )
    return contract.path, normalized_anchor

@dataclass(frozen=True)
class ResearchEntity:
    """One explicit project-local scientific entity identity."""

    project_id: str
    entity_id: str
    entity_type: ResearchEntityType
    identity_key: str
    title: str
    knowledge_path: str | None = None
    anchor: str | None = None

    def __post_init__(self) -> None:
        project_id = _validated_project_id(self.project_id)
        entity_type = _entity_type(self.entity_type)
        identity_key = _bounded_text(self.identity_key, "identity_key", maximum=512)
        title = _bounded_text(self.title, "title", maximum=500)
        knowledge_path, anchor = _normalize_entity_location(
            entity_type, self.knowledge_path, self.anchor
        )
        expected_id = research_entity_id_for(project_id, entity_type, identity_key)
        if _entity_id(self.entity_id) != expected_id:
            raise ResearchEntityBindingError(
                "entity_id does not match project_id/entity_type/identity_key"
            )
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "entity_type", entity_type)
        object.__setattr__(self, "identity_key", identity_key)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "knowledge_path", knowledge_path)
        object.__setattr__(self, "anchor", anchor)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESEARCH_RELATION_SCHEMA_VERSION,
            "kind": RESEARCH_ENTITY_KIND,
            "entity_version": RESEARCH_ENTITY_VERSION,
            "record_type": "entity",
            "project_id": self.project_id,
            "identity_version": RESEARCH_IDENTITY_VERSION,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "identity_key": self.identity_key,
            "title": self.title,
            "knowledge_path": self.knowledge_path,
            "anchor": self.anchor,
        }


@dataclass(frozen=True)
class ResearchRelation:
    """One explicit directed relation supplied by the semantic host Agent."""

    project_id: str
    relation_id: str
    relation_type: str
    source_entity_id: str
    target_entity_id: str
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        project_id = _validated_project_id(self.project_id)
        relation_type = _relation_type(self.relation_type)
        source = _entity_id(self.source_entity_id)
        target = _entity_id(self.target_entity_id)
        if source == target:
            raise ResearchRelationConflictError(
                "research relations must not point an entity to itself"
            )
        evidence_ids = _evidence_ids(
            self.evidence_ids, require_json_array=False
        )
        expected_id = research_relation_id_for(
            project_id, relation_type, source, target
        )
        if _relation_id(self.relation_id) != expected_id:
            raise ResearchRelationConflictError(
                "relation_id does not match project/type/source/target"
            )
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "relation_type", relation_type)
        object.__setattr__(self, "source_entity_id", source)
        object.__setattr__(self, "target_entity_id", target)
        object.__setattr__(self, "evidence_ids", evidence_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESEARCH_RELATION_SCHEMA_VERSION,
            "kind": RESEARCH_RELATION_KIND,
            "relation_version": RESEARCH_RELATION_VERSION,
            "record_type": "relation",
            "project_id": self.project_id,
            "identity_version": RESEARCH_IDENTITY_VERSION,
            "relation_id": self.relation_id,
            "relation_type": self.relation_type,
            "source_entity_id": self.source_entity_id,
            "target_entity_id": self.target_entity_id,
            "evidence_ids": list(self.evidence_ids),
        }


def create_research_entity(
    project_id: str,
    *,
    entity_type: ResearchEntityType | str,
    identity_key: str,
    title: str,
    knowledge_path: str | None = None,
    anchor: str | None = None,
) -> ResearchEntity:
    normalized_type = _entity_type(entity_type)
    return ResearchEntity(
        project_id=project_id,
        entity_id=research_entity_id_for(project_id, normalized_type, identity_key),
        entity_type=normalized_type,
        identity_key=identity_key,
        title=title,
        knowledge_path=knowledge_path,
        anchor=anchor,
    )


def create_research_relation(
    project_id: str,
    *,
    relation_type: str,
    source_entity_id: str,
    target_entity_id: str,
    evidence_ids: Iterable[str] = (),
) -> ResearchRelation:
    normalized_evidence_ids = _evidence_ids(
        evidence_ids,
        require_json_array=False,
        require_canonical_order=False,
    )
    return ResearchRelation(
        project_id=project_id,
        relation_id=research_relation_id_for(
            project_id, relation_type, source_entity_id, target_entity_id
        ),
        relation_type=relation_type,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        evidence_ids=normalized_evidence_ids,
    )


@dataclass(frozen=True)
class ResearchRelationRegistry:
    """Canonical in-memory representation of future ``relations.jsonl`` bytes."""

    project_id: str
    entities: tuple[ResearchEntity, ...] = ()
    relations: tuple[ResearchRelation, ...] = ()

    def __post_init__(self) -> None:
        project_id = _validated_project_id(self.project_id)
        raw_entities = tuple(self.entities)
        raw_relations = tuple(self.relations)
        if any(not isinstance(item, ResearchEntity) for item in raw_entities):
            raise ResearchRelationError(
                "entities must contain ResearchEntity values"
            )
        if any(not isinstance(item, ResearchRelation) for item in raw_relations):
            raise ResearchRelationError(
                "relations must contain ResearchRelation values"
            )
        entities = tuple(sorted(raw_entities, key=lambda item: item.entity_id))
        relations = tuple(sorted(raw_relations, key=lambda item: item.relation_id))
        entity_ids: set[str] = set()
        identity_keys: set[tuple[str, str]] = set()
        locations: set[tuple[str, str | None]] = set()
        for entity in entities:
            if entity.project_id != project_id:
                raise ResearchRelationConflictError(
                    "entity belongs to a different project"
                )
            if entity.entity_id in entity_ids:
                raise ResearchRelationConflictError(
                    f"duplicate research entity ID: {entity.entity_id}"
                )
            entity_ids.add(entity.entity_id)
            identity = (entity.entity_type, entity.identity_key)
            if identity in identity_keys:
                raise ResearchRelationConflictError(
                    "duplicate entity_type/identity_key identity"
                )
            identity_keys.add(identity)
            if entity.knowledge_path is not None:
                location = (entity.knowledge_path, entity.anchor)
                if location in locations:
                    raise ResearchRelationConflictError(
                        f"duplicate research entity Markdown location: {location!r}"
                    )
                locations.add(location)

        relation_ids: set[str] = set()
        relation_keys: set[tuple[str, str, str]] = set()
        for relation in relations:
            if relation.project_id != project_id:
                raise ResearchRelationConflictError(
                    "relation belongs to a different project"
                )
            if relation.relation_id in relation_ids:
                raise ResearchRelationConflictError(
                    f"duplicate research relation ID: {relation.relation_id}"
                )
            relation_ids.add(relation.relation_id)
            key = (
                relation.relation_type,
                relation.source_entity_id,
                relation.target_entity_id,
            )
            if key in relation_keys:
                raise ResearchRelationConflictError(
                    "duplicate relation type/source/target edge"
                )
            relation_keys.add(key)
            missing = {
                relation.source_entity_id, relation.target_entity_id
            } - entity_ids
            if missing:
                raise ResearchRelationConflictError(
                    "relation references unknown entity IDs: "
                    + ", ".join(sorted(missing))
                )

        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "entities", entities)
        object.__setattr__(self, "relations", relations)

    @property
    def by_entity_id(self) -> Mapping[str, ResearchEntity]:
        return MappingProxyType({item.entity_id: item for item in self.entities})

    @property
    def by_relation_id(self) -> Mapping[str, ResearchRelation]:
        return MappingProxyType({item.relation_id: item for item in self.relations})

    def summary_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESEARCH_RELATION_SCHEMA_VERSION,
            "kind": RESEARCH_RELATION_REGISTRY_KIND,
            "registry_version": RESEARCH_RELATION_REGISTRY_VERSION,
            "record_type": "summary",
            "project_id": self.project_id,
            "identity_version": RESEARCH_IDENTITY_VERSION,
            "entity_count": len(self.entities),
            "relation_count": len(self.relations),
        }

    def serialized_bytes(self) -> bytes:
        rows: list[dict[str, object]] = [self.summary_dict()]
        rows.extend(entity.as_dict() for entity in self.entities)
        rows.extend(relation.as_dict() for relation in self.relations)
        return b"".join(_canonical_json(row) + b"\n" for row in rows)


@dataclass(frozen=True)
class ResearchEntityIndexEntry:
    """One entity plus deterministic incoming/outgoing backlinks."""

    entity_id: str
    entity_type: ResearchEntityType
    title: str
    knowledge_path: str | None
    anchor: str | None
    incoming_relation_ids: tuple[str, ...]
    outgoing_relation_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "title": self.title,
            "knowledge_path": self.knowledge_path,
            "anchor": self.anchor,
            "incoming_relation_ids": list(self.incoming_relation_ids),
            "outgoing_relation_ids": list(self.outgoing_relation_ids),
        }


@dataclass(frozen=True)
class ResearchProjectIndex:
    """Read-only renderer input; it is not project ``index.md`` content."""

    project_id: str
    entities: tuple[ResearchEntityIndexEntry, ...]
    relations: tuple[ResearchRelation, ...]

    def as_dict(self) -> dict[str, object]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for entity in self.entities:
            grouped[entity.entity_type].append(entity.entity_id)
        groups = [
            {"entity_type": entity_type, "entity_ids": grouped[entity_type]}
            for entity_type in sorted(grouped)
        ]
        return {
            "schema_version": RESEARCH_RELATION_SCHEMA_VERSION,
            "kind": RESEARCH_PROJECT_INDEX_KIND,
            "index_version": RESEARCH_PROJECT_INDEX_VERSION,
            "project_id": self.project_id,
            "entity_count": len(self.entities),
            "relation_count": len(self.relations),
            "entity_groups": groups,
            "entities": [entity.as_dict() for entity in self.entities],
            "relations": [relation.as_dict() for relation in self.relations],
        }

    def serialized_bytes(self) -> bytes:
        return _canonical_json(self.as_dict()) + b"\n"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ResearchRelationError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ResearchRelationError(f"non-finite JSON constant is not supported: {value}")


def _decode_registry_line(raw: str, *, line_number: int) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ResearchRelationError(
            f"invalid research relation JSON at line {line_number}: {exc}"
        ) from exc


def _exact_fields(
    value: object, expected: frozenset[str], *, label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResearchRelationError(f"{label} must be a JSON object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ResearchRelationError(
            f"{label} fields are invalid; missing={missing}, unknown={unknown}"
        )
    return cast(dict[str, Any], value)


def _schema_version(value: object, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResearchRelationError(f"{label} schema_version must be integer 1")
    if value > RESEARCH_RELATION_SCHEMA_VERSION:
        raise UnsupportedResearchRelationSchemaVersionError(
            f"{label} schema_version {value} is newer than supported version 1"
        )
    if value != RESEARCH_RELATION_SCHEMA_VERSION:
        raise ResearchRelationError(f"{label} schema_version must equal 1")


def _fixed(value: object, expected: str, *, field_name: str, label: str) -> None:
    if value != expected:
        raise ResearchRelationError(
            f"{label} {field_name} must equal {expected!r}"
        )


def _non_negative_count(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResearchRelationError(f"{field_name} must be a non-negative integer")
    return value


def _parse_entity_row(value: object, *, project_id: str) -> ResearchEntity:
    row = _exact_fields(value, _ENTITY_FIELDS, label="research entity row")
    _schema_version(row["schema_version"], label="research entity row")
    _fixed(row["kind"], RESEARCH_ENTITY_KIND, field_name="kind", label="entity")
    _fixed(
        row["entity_version"], RESEARCH_ENTITY_VERSION,
        field_name="entity_version", label="entity",
    )
    _fixed(row["record_type"], "entity", field_name="record_type", label="entity")
    _fixed(
        row["identity_version"], RESEARCH_IDENTITY_VERSION,
        field_name="identity_version", label="entity",
    )
    if row["project_id"] != project_id:
        raise ResearchRelationConflictError(
            "entity row project_id does not match summary"
        )
    return ResearchEntity(
        project_id=project_id,
        entity_id=row["entity_id"],
        entity_type=row["entity_type"],
        identity_key=row["identity_key"],
        title=row["title"],
        knowledge_path=row["knowledge_path"],
        anchor=row["anchor"],
    )


def _parse_relation_row(value: object, *, project_id: str) -> ResearchRelation:
    row = _exact_fields(value, _RELATION_FIELDS, label="research relation row")
    _schema_version(row["schema_version"], label="research relation row")
    _fixed(row["kind"], RESEARCH_RELATION_KIND, field_name="kind", label="relation")
    _fixed(
        row["relation_version"], RESEARCH_RELATION_VERSION,
        field_name="relation_version", label="relation",
    )
    _fixed(
        row["record_type"], "relation", field_name="record_type", label="relation"
    )
    _fixed(
        row["identity_version"], RESEARCH_IDENTITY_VERSION,
        field_name="identity_version", label="relation",
    )
    if row["project_id"] != project_id:
        raise ResearchRelationConflictError(
            "relation row project_id does not match summary"
        )
    return ResearchRelation(
        project_id=project_id,
        relation_id=row["relation_id"],
        relation_type=row["relation_type"],
        source_entity_id=row["source_entity_id"],
        target_entity_id=row["target_entity_id"],
        evidence_ids=_evidence_ids(row["evidence_ids"]),
    )


def deserialize_research_relation_registry(
    payload: bytes, *, project_id: str | None = None,
) -> ResearchRelationRegistry:
    """Strictly parse canonical ``relations.jsonl`` bytes without filesystem I/O."""
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ResearchRelationError(
            "research relation registry must be strict UTF-8"
        ) from exc
    if not text:
        raise ResearchRelationError("research relation registry is empty")
    if not text.endswith("\n"):
        raise ResearchRelationError("research relation registry must end with LF")
    raw_lines = text[:-1].split("\n")
    if not raw_lines or any(not line for line in raw_lines):
        raise ResearchRelationError(
            "research relation registry must not contain blank rows"
        )
    rows = [
        _decode_registry_line(line, line_number=index)
        for index, line in enumerate(raw_lines, start=1)
    ]
    summary = _exact_fields(rows[0], _SUMMARY_FIELDS, label="research relation summary")
    _schema_version(summary["schema_version"], label="research relation summary")
    _fixed(
        summary["kind"], RESEARCH_RELATION_REGISTRY_KIND,
        field_name="kind", label="summary",
    )
    _fixed(
        summary["registry_version"], RESEARCH_RELATION_REGISTRY_VERSION,
        field_name="registry_version", label="summary",
    )
    _fixed(
        summary["record_type"], "summary", field_name="record_type", label="summary"
    )
    _fixed(
        summary["identity_version"], RESEARCH_IDENTITY_VERSION,
        field_name="identity_version", label="summary",
    )
    summary_project = _validated_project_id(summary["project_id"])
    if project_id is not None and _validated_project_id(project_id) != summary_project:
        raise ResearchRelationConflictError(
            "requested project_id does not match relation registry"
        )
    entity_count = _non_negative_count(
        summary["entity_count"], field_name="entity_count"
    )
    relation_count = _non_negative_count(
        summary["relation_count"], field_name="relation_count"
    )
    if len(rows) != 1 + entity_count + relation_count:
        raise ResearchRelationError("research relation registry row counts do not match")
    entities = tuple(
        _parse_entity_row(row, project_id=summary_project)
        for row in rows[1 : 1 + entity_count]
    )
    relations = tuple(
        _parse_relation_row(row, project_id=summary_project)
        for row in rows[1 + entity_count :]
    )
    if [item.entity_id for item in entities] != sorted(
        item.entity_id for item in entities
    ):
        raise ResearchRelationError("entity rows must use canonical entity_id order")
    if [item.relation_id for item in relations] != sorted(
        item.relation_id for item in relations
    ):
        raise ResearchRelationError("relation rows must use canonical relation_id order")
    registry = ResearchRelationRegistry(summary_project, entities, relations)
    if registry.serialized_bytes() != payload:
        raise ResearchRelationError(
            "research relation registry is valid but not canonically serialized"
        )
    return registry


def _knowledge_pages(
    project_id: str, values: Mapping[str, bytes],
) -> Mapping[str, KnowledgePage]:
    if not isinstance(values, Mapping):
        raise TypeError("knowledge_pages must map canonical paths to bytes")
    pages: dict[str, KnowledgePage] = {}
    for raw_path, payload in values.items():
        if not isinstance(raw_path, str):
            raise ResearchEntityBindingError("knowledge page paths must be strings")
        try:
            contract = artifact_contract_for_path(raw_path)
        except KnowledgeArtifactPathError as exc:
            raise ResearchEntityBindingError(str(exc)) from exc
        if contract.path != raw_path:
            raise ResearchEntityBindingError(
                "knowledge page mapping keys must already be canonical"
            )
        if contract.path in pages:
            raise ResearchEntityBindingError(
                f"duplicate knowledge page path: {contract.path}"
            )
        try:
            page = parse_knowledge_page(payload, path=contract.path)
        except KnowledgeArtifactError as exc:
            raise ResearchEntityBindingError(
                f"knowledge page {contract.path!r} is invalid: {exc}"
            ) from exc
        if page.frontmatter.schema_version != KNOWLEDGE_SCHEMA_VERSION:
            raise ResearchEntityBindingError(
                f"knowledge page {contract.path!r} must use current Schema v2"
            )
        if page.frontmatter.project_id != project_id:
            raise ResearchEntityBindingError(
                f"knowledge page {contract.path!r} belongs to another project"
            )
        pages[contract.path] = page
    return MappingProxyType(pages)


def validate_research_entity_bindings(
    registry: ResearchRelationRegistry,
    *,
    knowledge_pages: Mapping[str, bytes],
) -> Mapping[str, KnowledgePage]:
    """Bind page-backed entities to caller-supplied current Schema v2 bytes."""
    if not isinstance(registry, ResearchRelationRegistry):
        raise TypeError("registry must be a ResearchRelationRegistry")
    pages = _knowledge_pages(registry.project_id, knowledge_pages)
    for entity in registry.entities:
        if entity.knowledge_path is None:
            continue
        page = pages.get(entity.knowledge_path)
        if page is None:
            raise ResearchEntityBindingError(
                "entity page is missing from current knowledge set: "
                f"{entity.knowledge_path}"
            )
        expected_type, expected_role = (
            _DIRECT_PAGE_TYPES.get(entity.entity_type)
            or _EMBEDDED_PAGE_TYPES[entity.entity_type]
        )
        contract = artifact_contract_for_path(entity.knowledge_path)
        if (
            page.frontmatter.artifact_type != expected_type
            or contract.page_role != expected_role
        ):
            raise ResearchEntityBindingError(
                f"entity {entity.entity_id} does not match current page type/role"
            )
        if (
            entity.entity_type in _DIRECT_PAGE_TYPES
            and page.frontmatter.title != entity.title
        ):
            raise ResearchEntityBindingError(
                f"entity title is stale for page {entity.knowledge_path!r}"
            )
    return pages


def build_research_project_index(
    registry: ResearchRelationRegistry,
    *,
    knowledge_pages: Mapping[str, bytes],
    known_evidence_ids: Iterable[str] | None = None,
) -> ResearchProjectIndex:
    """Build deterministic groups and reverse backlinks after current-page checks."""
    validate_research_entity_bindings(registry, knowledge_pages=knowledge_pages)
    if known_evidence_ids is not None:
        known: set[str] = set()
        for evidence_id in known_evidence_ids:
            if (
                not isinstance(evidence_id, str)
                or _EVIDENCE_ID_PATTERN.fullmatch(evidence_id) is None
            ):
                raise ResearchRelationError(
                    "known_evidence_ids contains an invalid ID"
                )
            known.add(evidence_id)
        declared = {
            evidence_id
            for relation in registry.relations
            for evidence_id in relation.evidence_ids
        }
        missing = declared - known
        if missing:
            raise ResearchRelationConflictError(
                "relations reference unknown Evidence IDs: "
                + ", ".join(sorted(missing))
            )

    incoming: dict[str, list[str]] = defaultdict(list)
    outgoing: dict[str, list[str]] = defaultdict(list)
    for relation in registry.relations:
        outgoing[relation.source_entity_id].append(relation.relation_id)
        incoming[relation.target_entity_id].append(relation.relation_id)
    entries = tuple(
        ResearchEntityIndexEntry(
            entity_id=entity.entity_id,
            entity_type=entity.entity_type,
            title=entity.title,
            knowledge_path=entity.knowledge_path,
            anchor=entity.anchor,
            incoming_relation_ids=tuple(sorted(incoming[entity.entity_id])),
            outgoing_relation_ids=tuple(sorted(outgoing[entity.entity_id])),
        )
        for entity in sorted(
            registry.entities,
            key=lambda item: (item.entity_type, item.title, item.entity_id),
        )
    )
    return ResearchProjectIndex(
        project_id=registry.project_id,
        entities=entries,
        relations=registry.relations,
    )
