#!/usr/bin/env python3
"""Strict project-scoped Markdown knowledge contract for Schemas v1/v2.

F-01A and F-02A deliberately stop at an in-memory structural contract.  This
module does not open or write a project knowledge file, synthesize page content,
resolve Evidence registry state, check source health/currentness, or expose a
CLI/MCP/Web surface.  It gives later controlled writers deterministic current
Schema v2 frontmatter and project-relative path validation while retaining
strict, read-only parsing compatibility for Schema v1 pages.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal, cast

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode

from tools.project_layout import InvalidProjectIdError, validate_project_id


LEGACY_KNOWLEDGE_SCHEMA_VERSION = 1
KNOWLEDGE_SCHEMA_VERSION = 2
KNOWLEDGE_PAGE_KIND = "llmwiki-project-knowledge-page"

ArtifactType = Literal[
    "overview",
    "project_map",
    "reproduction",
    "architecture",
    "paper",
    "method",
    "dataset",
    "experiment",
    "result",
    "claim",
    "open_question",
    "project_status",
    "risk",
    "goal",
    "plan",
    "project_index",
    "metric",
    "decision",
    "source",
]
KnowledgeStatus = Literal["draft", "verified", "stale", "conflicting", "rejected"]
KnowledgeOwnership = Literal["generated", "mixed", "user"]
EvidenceStance = Literal["supporting", "opposing", "context"]
ArtifactPageRole = Literal[
    "project_index",
    "singleton",
    "collection_index",
    "detail",
    "backlog",
    "daily_plan",
]

ARTIFACT_TYPES: frozenset[str] = frozenset(
    {
        "overview",
        "project_map",
        "reproduction",
        "architecture",
        "paper",
        "method",
        "dataset",
        "experiment",
        "result",
        "claim",
        "open_question",
        "project_status",
        "risk",
        "goal",
        "plan",
        "project_index",
        "metric",
        "decision",
        "source",
    }
)
KNOWLEDGE_STATUSES: frozenset[str] = frozenset(
    {"draft", "verified", "stale", "conflicting", "rejected"}
)
KNOWLEDGE_OWNERSHIPS: frozenset[str] = frozenset({"generated", "mixed", "user"})
EVIDENCE_STANCES: frozenset[str] = frozenset({"supporting", "opposing", "context"})

_COMMON_FRONTMATTER_FIELDS = (
    "schema_version",
    "kind",
    "project_id",
    "artifact_type",
    "title",
    "status",
    "ownership",
    "source_ids",
)
_V1_FRONTMATTER_FIELDS = (
    *_COMMON_FRONTMATTER_FIELDS,
    "evidence_ids",
    "generated_at",
    "updated_at",
    "last_verified_at",
)
_V2_FRONTMATTER_FIELDS = (
    *_COMMON_FRONTMATTER_FIELDS,
    "evidence_refs",
    "generated_at",
    "updated_at",
    "last_verified_at",
)
_V1_FRONTMATTER_FIELD_SET = frozenset(_V1_FRONTMATTER_FIELDS)
_V2_FRONTMATTER_FIELD_SET = frozenset(_V2_FRONTMATTER_FIELDS)
_EVIDENCE_REF_FIELDS = frozenset({"evidence_id", "stance"})
_SOURCE_ID_PATTERN = re.compile(r"src-[0-9a-f]{32}")
_EVIDENCE_ID_PATTERN = re.compile(r"evd-[0-9a-f]{64}")
_RFC3339_PATTERN = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"T(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?P<fraction>\.[0-9]{1,6})?"
    r"(?P<offset>Z|[+-][0-9]{2}:[0-9]{2})"
)
_DETAIL_SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_WINDOWS_DRIVE_PATTERN = re.compile(r"[A-Za-z]:")

_SINGLETON_PATHS: dict[str, tuple[ArtifactType, ArtifactPageRole]] = {
    "index.md": ("project_index", "project_index"),
    "overview.md": ("overview", "singleton"),
    "project-map.md": ("project_map", "singleton"),
    "reproduction.md": ("reproduction", "singleton"),
    "architecture.md": ("architecture", "singleton"),
    "open-questions.md": ("open_question", "singleton"),
    "status.md": ("project_status", "singleton"),
    "risks.md": ("risk", "singleton"),
    "goals.md": ("goal", "singleton"),
    "plans/backlog.md": ("plan", "backlog"),
}
_COLLECTION_DIRECTORIES: dict[str, ArtifactType] = {
    "papers": "paper",
    "methods": "method",
    "datasets": "dataset",
    "experiments": "experiment",
    "results": "result",
    "claims": "claim",
}
_AUXILIARY_DIRECTORIES: dict[str, ArtifactType] = {
    "decisions": "decision",
    "sources": "source",
}


class KnowledgeArtifactError(ValueError):
    """Base error for invalid project knowledge artifacts."""


class KnowledgeFrontmatterError(KnowledgeArtifactError):
    """Raised when project-page YAML frontmatter violates its schema contract."""


class UnsupportedKnowledgeSchemaVersionError(KnowledgeFrontmatterError):
    """Raised when a knowledge page was written by a newer unsupported schema."""


class KnowledgeArtifactPathError(KnowledgeArtifactError):
    """Raised when a project-relative knowledge path is unsafe or noncanonical."""


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects aliases, duplicate keys, and merge keys."""

    yaml_implicit_resolvers = {
        leading: [
            (tag, expression)
            for tag, expression in resolvers
            if tag != "tag:yaml.org,2002:timestamp"
        ]
        for leading, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise KnowledgeFrontmatterError("YAML aliases are not supported")
        return super().compose_node(parent, index)


def _construct_strict_mapping(
    loader: _StrictSafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[str, Any]:
    if not isinstance(node, MappingNode):
        raise KnowledgeFrontmatterError("YAML mapping node is invalid")
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise KnowledgeFrontmatterError("YAML merge keys are not supported")
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise KnowledgeFrontmatterError("YAML mapping keys must be strings")
        if key == "<<":
            raise KnowledgeFrontmatterError("YAML merge keys are not supported")
        if key in result:
            raise KnowledgeFrontmatterError(f"duplicate YAML key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_strict_mapping,
)


@dataclass(frozen=True)
class ArtifactPathContract:
    """The deterministic semantic and structural role assigned by one path."""

    path: str
    artifact_type: ArtifactType
    page_role: ArtifactPageRole
    collection: str | None = None
    slug: str | None = None
    plan_date: str | None = None


@dataclass(frozen=True)
class EvidenceRef:
    """One strict Schema v2 directional reference to an Evidence record ID."""

    evidence_id: str
    stance: EvidenceStance

    def as_dict(self) -> dict[str, str]:
        """Return the canonical two-field mapping used by Schema v2."""

        return {"evidence_id": self.evidence_id, "stance": self.stance}


@dataclass(frozen=True)
class KnowledgeFrontmatter:
    """Validated Schema v2 or strict read-only Schema v1 frontmatter."""

    schema_version: int
    kind: str
    project_id: str
    artifact_type: ArtifactType
    title: str
    status: KnowledgeStatus
    ownership: KnowledgeOwnership
    source_ids: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...] | None
    generated_at: str
    updated_at: str
    last_verified_at: str | None
    _legacy_evidence_ids: tuple[str, ...] = field(default=(), repr=False)

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        """Return referenced IDs without inventing directional stance for v1."""

        if self.schema_version == LEGACY_KNOWLEDGE_SCHEMA_VERSION:
            return self._legacy_evidence_ids
        if self.evidence_refs is None:
            return ()
        return tuple(reference.evidence_id for reference in self.evidence_refs)

    def as_dict(self) -> dict[str, Any]:
        """Return the original schema's fields in canonical order without migration."""

        common: dict[str, Any] = {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "project_id": self.project_id,
            "artifact_type": self.artifact_type,
            "title": self.title,
            "status": self.status,
            "ownership": self.ownership,
            "source_ids": list(self.source_ids),
        }
        if self.schema_version == LEGACY_KNOWLEDGE_SCHEMA_VERSION:
            common["evidence_ids"] = list(self._legacy_evidence_ids)
        elif self.schema_version == KNOWLEDGE_SCHEMA_VERSION:
            common["evidence_refs"] = [
                reference.as_dict() for reference in self.evidence_refs or ()
            ]
        else:  # Defensive only; constructors in this module fail closed first.
            raise UnsupportedKnowledgeSchemaVersionError(
                f"knowledge schema_version {self.schema_version} is not supported"
            )
        common.update(
            {
                "generated_at": self.generated_at,
                "updated_at": self.updated_at,
                "last_verified_at": self.last_verified_at,
            }
        )
        return common


@dataclass(frozen=True)
class KnowledgePage:
    """One parsed page without any filesystem side effect."""

    frontmatter: KnowledgeFrontmatter
    body: str


@dataclass(frozen=True)
class _ValidatedCommonFrontmatter:
    project_id: str
    artifact_type: ArtifactType
    title: str
    status: KnowledgeStatus
    ownership: KnowledgeOwnership
    source_ids: tuple[str, ...]
    generated_at: str
    updated_at: str
    last_verified_at: str | None
    path_contract: ArtifactPathContract | None


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _non_empty_human_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise KnowledgeFrontmatterError(
            f"{field_name} must be a non-empty string without outer whitespace"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise KnowledgeFrontmatterError(f"{field_name} must not contain control characters")
    return value


def _stable_id_list(
    value: object,
    *,
    field_name: str,
    pattern: re.Pattern[str],
    expected_form: str,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise KnowledgeFrontmatterError(f"{field_name} must be a YAML sequence")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or pattern.fullmatch(item) is None:
            raise KnowledgeFrontmatterError(
                f"{field_name} entries must use {expected_form}"
            )
        if item in seen:
            raise KnowledgeFrontmatterError(f"{field_name} must not contain duplicates")
        seen.add(item)
        result.append(item)
    return tuple(result)


def _evidence_ref_list(value: object) -> tuple[EvidenceRef, ...]:
    if not isinstance(value, list):
        raise KnowledgeFrontmatterError("evidence_refs must be a YAML sequence")
    result: list[EvidenceRef] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise KnowledgeFrontmatterError(
                f"evidence_refs[{index}] must be a YAML mapping"
            )
        if not all(isinstance(key, str) for key in item):
            raise KnowledgeFrontmatterError(
                f"evidence_refs[{index}] mapping keys must be strings"
            )
        keys = frozenset(item.keys())
        missing = _EVIDENCE_REF_FIELDS - keys
        unknown = keys - _EVIDENCE_REF_FIELDS
        if missing or unknown:
            details: list[str] = []
            if missing:
                details.append(f"missing fields: {', '.join(sorted(missing))}")
            if unknown:
                details.append(f"unknown fields: {', '.join(sorted(unknown))}")
            raise KnowledgeFrontmatterError(
                f"evidence_refs[{index}] must contain exactly evidence_id and stance ("
                + "; ".join(details)
                + ")"
            )

        evidence_id = item["evidence_id"]
        if (
            not isinstance(evidence_id, str)
            or _EVIDENCE_ID_PATTERN.fullmatch(evidence_id) is None
        ):
            raise KnowledgeFrontmatterError(
                "evidence_refs evidence_id values must use 'evd-' plus 64 lowercase "
                "hexadecimal digits"
            )
        if evidence_id in seen_ids:
            raise KnowledgeFrontmatterError(
                "evidence_refs must not contain duplicate evidence_id values"
            )

        stance = item["stance"]
        if not isinstance(stance, str) or stance not in EVIDENCE_STANCES:
            raise KnowledgeFrontmatterError(
                "evidence_refs stance must be one of "
                + ", ".join(sorted(EVIDENCE_STANCES))
            )
        seen_ids.add(evidence_id)
        result.append(EvidenceRef(evidence_id=evidence_id, stance=stance))
    return tuple(result)


def _parse_rfc3339(value: object, field_name: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or _RFC3339_PATTERN.fullmatch(value) is None:
        raise KnowledgeFrontmatterError(
            f"{field_name} must be an RFC 3339 timestamp with an explicit timezone"
        )
    if value.endswith("-00:00"):
        raise KnowledgeFrontmatterError(
            f"{field_name} must use a known timezone offset, not -00:00"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise KnowledgeFrontmatterError(f"{field_name} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise KnowledgeFrontmatterError(f"{field_name} must include a timezone")
    utc_value = parsed.astimezone(timezone.utc)
    timespec = "microseconds" if utc_value.microsecond else "seconds"
    canonical = utc_value.isoformat(timespec=timespec).replace("+00:00", "Z")
    return canonical, utc_value


def _optional_rfc3339(value: object, field_name: str) -> tuple[str | None, datetime | None]:
    if value is None:
        return None, None
    canonical, parsed = _parse_rfc3339(value, field_name)
    return canonical, parsed


def _normalize_project_relative_path(path: object) -> str:
    if not isinstance(path, str) or not path or path.strip() != path:
        raise KnowledgeArtifactPathError(
            "knowledge path must be a non-empty project-relative POSIX string"
        )
    if "\\" in path:
        raise KnowledgeArtifactPathError("knowledge path must use POSIX '/' separators")
    if path.startswith("/") or _WINDOWS_DRIVE_PATTERN.match(path):
        raise KnowledgeArtifactPathError("knowledge path must be project-relative")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise KnowledgeArtifactPathError("knowledge path must not contain control characters")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise KnowledgeArtifactPathError(
            "knowledge path must not contain empty, '.' or '..' components"
        )
    if any(":" in part for part in parts):
        raise KnowledgeArtifactPathError("knowledge path components must not contain ':'")
    return path


def _detail_slug(filename: str) -> str:
    if not filename.endswith(".md"):
        raise KnowledgeArtifactPathError("knowledge detail pages must use the .md suffix")
    slug = filename[:-3]
    if slug == "index" or _DETAIL_SLUG_PATTERN.fullmatch(slug) is None:
        raise KnowledgeArtifactPathError(
            "detail page names must use a non-reserved lowercase kebab-case slug"
        )
    return slug


def artifact_contract_for_path(path: object) -> ArtifactPathContract:
    """Resolve one canonical project-relative Markdown path.

    The mapping is structural only.  It never guesses research semantics from a
    title, body, keyword, or arbitrary filename.
    """

    normalized = _normalize_project_relative_path(path)
    fixed = _SINGLETON_PATHS.get(normalized)
    if fixed is not None:
        artifact_type, page_role = fixed
        return ArtifactPathContract(
            path=normalized,
            artifact_type=artifact_type,
            page_role=page_role,
        )

    parts = normalized.split("/")
    if len(parts) == 2 and parts[0] in _COLLECTION_DIRECTORIES:
        collection, filename = parts
        artifact_type = _COLLECTION_DIRECTORIES[collection]
        if filename == "index.md":
            return ArtifactPathContract(
                path=normalized,
                artifact_type=artifact_type,
                page_role="collection_index",
                collection=collection,
            )
        slug = _detail_slug(filename)
        return ArtifactPathContract(
            path=normalized,
            artifact_type=artifact_type,
            page_role="detail",
            collection=collection,
            slug=slug,
        )

    if len(parts) == 2 and parts[0] in _AUXILIARY_DIRECTORIES:
        collection, filename = parts
        slug = _detail_slug(filename)
        return ArtifactPathContract(
            path=normalized,
            artifact_type=_AUXILIARY_DIRECTORIES[collection],
            page_role="detail",
            collection=collection,
            slug=slug,
        )

    if len(parts) == 3 and parts[:2] == ["plans", "daily"]:
        filename = parts[2]
        if not filename.endswith(".md"):
            raise KnowledgeArtifactPathError("daily plan pages must use the .md suffix")
        date_text = filename[:-3]
        try:
            parsed_date = date.fromisoformat(date_text)
        except ValueError as exc:
            raise KnowledgeArtifactPathError(
                "daily plan path must use plans/daily/YYYY-MM-DD.md"
            ) from exc
        if parsed_date.isoformat() != date_text:
            raise KnowledgeArtifactPathError(
                "daily plan path must use plans/daily/YYYY-MM-DD.md"
            )
        return ArtifactPathContract(
            path=normalized,
            artifact_type="plan",
            page_role="daily_plan",
            collection="plans/daily",
            plan_date=date_text,
        )

    raise KnowledgeArtifactPathError(
        f"path {normalized!r} is not a canonical F-01A project knowledge path"
    )


def _require_frontmatter_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise KnowledgeFrontmatterError("knowledge frontmatter must be a YAML mapping")
    if not all(isinstance(key, str) for key in value):
        raise KnowledgeFrontmatterError("knowledge frontmatter keys must be strings")
    return cast(Mapping[str, object], value)


def _read_schema_version(value: Mapping[str, object]) -> int:
    if "schema_version" not in value:
        raise KnowledgeFrontmatterError(
            "knowledge frontmatter is missing fields: schema_version"
        )
    version = value["schema_version"]
    if not _is_integer(version):
        raise KnowledgeFrontmatterError("schema_version must be integer 1 or 2")
    if version > KNOWLEDGE_SCHEMA_VERSION:
        raise UnsupportedKnowledgeSchemaVersionError(
            f"knowledge schema_version {version} is newer than supported "
            f"version {KNOWLEDGE_SCHEMA_VERSION}"
        )
    if version not in {LEGACY_KNOWLEDGE_SCHEMA_VERSION, KNOWLEDGE_SCHEMA_VERSION}:
        raise KnowledgeFrontmatterError(
            "knowledge schema_version is legacy or unsupported; migration must be explicit"
        )
    return version


def _require_exact_fields(
    value: Mapping[str, object],
    *,
    expected_fields: frozenset[str],
    schema_version: int,
) -> None:
    keys = frozenset(value.keys())
    missing = expected_fields - keys
    unknown = keys - expected_fields
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {', '.join(sorted(missing))}")
        if unknown:
            details.append(f"unknown fields: {', '.join(sorted(unknown))}")
        raise KnowledgeFrontmatterError(
            f"knowledge frontmatter must contain exactly the Schema v{schema_version} "
            "fields ("
            + "; ".join(details)
            + ")"
        )


def _validate_common_frontmatter(
    value: Mapping[str, object],
    *,
    path: object | None,
) -> _ValidatedCommonFrontmatter:
    if value["kind"] != KNOWLEDGE_PAGE_KIND:
        raise KnowledgeFrontmatterError(f"kind must be {KNOWLEDGE_PAGE_KIND!r}")

    try:
        project_id = validate_project_id(value["project_id"])
    except (InvalidProjectIdError, TypeError) as exc:
        raise KnowledgeFrontmatterError(str(exc)) from exc

    artifact_type_value = value["artifact_type"]
    if (
        not isinstance(artifact_type_value, str)
        or artifact_type_value not in ARTIFACT_TYPES
    ):
        raise KnowledgeFrontmatterError(
            f"artifact_type must be one of {', '.join(sorted(ARTIFACT_TYPES))}"
        )
    artifact_type = cast(ArtifactType, artifact_type_value)
    title = _non_empty_human_text(value["title"], "title")

    status_value = value["status"]
    if not isinstance(status_value, str) or status_value not in KNOWLEDGE_STATUSES:
        raise KnowledgeFrontmatterError(
            f"status must be one of {', '.join(sorted(KNOWLEDGE_STATUSES))}"
        )
    status = cast(KnowledgeStatus, status_value)

    ownership_value = value["ownership"]
    if (
        not isinstance(ownership_value, str)
        or ownership_value not in KNOWLEDGE_OWNERSHIPS
    ):
        raise KnowledgeFrontmatterError(
            f"ownership must be one of {', '.join(sorted(KNOWLEDGE_OWNERSHIPS))}"
        )
    ownership = cast(KnowledgeOwnership, ownership_value)

    source_ids = _stable_id_list(
        value["source_ids"],
        field_name="source_ids",
        pattern=_SOURCE_ID_PATTERN,
        expected_form="'src-' plus 32 lowercase hexadecimal digits",
    )

    generated_at, generated_time = _parse_rfc3339(value["generated_at"], "generated_at")
    updated_at, updated_time = _parse_rfc3339(value["updated_at"], "updated_at")
    last_verified_at, verified_time = _optional_rfc3339(
        value["last_verified_at"],
        "last_verified_at",
    )
    if generated_time > updated_time:
        raise KnowledgeFrontmatterError("generated_at must not be later than updated_at")
    if verified_time is not None and verified_time < generated_time:
        raise KnowledgeFrontmatterError(
            "last_verified_at must not be earlier than generated_at"
        )
    if verified_time is not None and verified_time > updated_time:
        raise KnowledgeFrontmatterError(
            "last_verified_at must not be later than updated_at"
        )
    if status == "verified" and verified_time is None:
        raise KnowledgeFrontmatterError(
            "verified pages must record a non-null last_verified_at"
        )

    path_contract: ArtifactPathContract | None = None
    if path is not None:
        path_contract = artifact_contract_for_path(path)
        if artifact_type != path_contract.artifact_type:
            raise KnowledgeFrontmatterError(
                f"artifact_type {artifact_type!r} does not match path "
                f"{path_contract.path!r} ({path_contract.artifact_type!r})"
            )

    return _ValidatedCommonFrontmatter(
        project_id=project_id,
        artifact_type=artifact_type,
        title=title,
        status=status,
        ownership=ownership,
        source_ids=source_ids,
        generated_at=generated_at,
        updated_at=updated_at,
        last_verified_at=last_verified_at,
        path_contract=path_contract,
    )


def _validate_v1_frontmatter(
    value: Mapping[str, object],
    *,
    path: object | None,
) -> KnowledgeFrontmatter:
    """Validate one legacy page without adding stance or migrating its fields."""

    _require_exact_fields(
        value,
        expected_fields=_V1_FRONTMATTER_FIELD_SET,
        schema_version=LEGACY_KNOWLEDGE_SCHEMA_VERSION,
    )
    common = _validate_common_frontmatter(value, path=path)
    evidence_ids = _stable_id_list(
        value["evidence_ids"],
        field_name="evidence_ids",
        pattern=_EVIDENCE_ID_PATTERN,
        expected_form="'evd-' plus 64 lowercase hexadecimal digits",
    )
    return KnowledgeFrontmatter(
        schema_version=LEGACY_KNOWLEDGE_SCHEMA_VERSION,
        kind=KNOWLEDGE_PAGE_KIND,
        project_id=common.project_id,
        artifact_type=common.artifact_type,
        title=common.title,
        status=common.status,
        ownership=common.ownership,
        source_ids=common.source_ids,
        evidence_refs=None,
        generated_at=common.generated_at,
        updated_at=common.updated_at,
        last_verified_at=common.last_verified_at,
        _legacy_evidence_ids=evidence_ids,
    )


def _is_key_claim(common: _ValidatedCommonFrontmatter) -> bool:
    if common.artifact_type != "claim":
        return False
    if common.path_contract is None:
        return True
    return common.path_contract.page_role == "detail"


def _validate_v2_frontmatter(
    value: Mapping[str, object],
    *,
    path: object | None,
) -> KnowledgeFrontmatter:
    _require_exact_fields(
        value,
        expected_fields=_V2_FRONTMATTER_FIELD_SET,
        schema_version=KNOWLEDGE_SCHEMA_VERSION,
    )
    common = _validate_common_frontmatter(value, path=path)
    evidence_refs = _evidence_ref_list(value["evidence_refs"])
    if (
        common.status == "verified"
        and _is_key_claim(common)
        and not any(reference.stance == "supporting" for reference in evidence_refs)
    ):
        raise KnowledgeFrontmatterError(
            "verified key claims must include at least one supporting evidence_ref"
        )

    return KnowledgeFrontmatter(
        schema_version=KNOWLEDGE_SCHEMA_VERSION,
        kind=KNOWLEDGE_PAGE_KIND,
        project_id=common.project_id,
        artifact_type=common.artifact_type,
        title=common.title,
        status=common.status,
        ownership=common.ownership,
        source_ids=common.source_ids,
        evidence_refs=evidence_refs,
        generated_at=common.generated_at,
        updated_at=common.updated_at,
        last_verified_at=common.last_verified_at,
    )


def validate_knowledge_frontmatter(
    value: object,
    *,
    path: object | None = None,
) -> KnowledgeFrontmatter:
    """Validate current Schema v2 frontmatter and optionally bind it to its path.

    Schema v1 is intentionally excluded from this current-schema validator.  It
    remains readable only through :func:`parse_knowledge_page`, so no caller can
    accidentally turn legacy ``evidence_ids`` into directional references.
    """

    mapping = _require_frontmatter_mapping(value)
    version = _read_schema_version(mapping)
    if version == LEGACY_KNOWLEDGE_SCHEMA_VERSION:
        raise KnowledgeFrontmatterError(
            "Schema v1 knowledge frontmatter is read-only compatibility; current "
            "validation requires schema_version 2 and explicit migration"
        )
    return _validate_v2_frontmatter(mapping, path=path)


def _decode_page(payload: object) -> str:
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes so strict UTF-8 can be enforced")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise KnowledgeFrontmatterError("knowledge page must be strict UTF-8") from exc
    if "\x00" in text:
        raise KnowledgeFrontmatterError("knowledge page must not contain NUL bytes")
    return text


def _split_frontmatter(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise KnowledgeFrontmatterError(
            "knowledge page must start with an exact '---' frontmatter delimiter"
        )
    if lines[0] not in {"---\n", "---\r\n"}:
        raise KnowledgeFrontmatterError("frontmatter delimiter must end with LF or CRLF")
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") != "---":
            continue
        if lines[index] not in {"---\n", "---\r\n", "---"}:
            raise KnowledgeFrontmatterError("frontmatter delimiter must be an exact line")
        yaml_text = "".join(lines[1:index])
        body = "".join(lines[index + 1 :])
        return yaml_text, body
    raise KnowledgeFrontmatterError("knowledge page is missing the closing '---' delimiter")


def _load_frontmatter(yaml_text: str) -> object:
    try:
        return yaml.load(yaml_text, Loader=_StrictSafeLoader)
    except KnowledgeFrontmatterError:
        raise
    except yaml.YAMLError as exc:
        raise KnowledgeFrontmatterError(f"invalid safe YAML frontmatter: {exc}") from exc


def parse_knowledge_page(payload: object, *, path: object) -> KnowledgePage:
    """Read strict Schema v1/v2 bytes and validate frontmatter against its path."""

    text = _decode_page(payload)
    yaml_text, body = _split_frontmatter(text)
    mapping = _require_frontmatter_mapping(_load_frontmatter(yaml_text))
    version = _read_schema_version(mapping)
    if version == LEGACY_KNOWLEDGE_SCHEMA_VERSION:
        frontmatter = _validate_v1_frontmatter(mapping, path=path)
    else:
        frontmatter = _validate_v2_frontmatter(mapping, path=path)
    return KnowledgePage(frontmatter=frontmatter, body=body)


def serialize_knowledge_frontmatter(
    value: object,
    *,
    path: object | None = None,
) -> str:
    """Return canonical current Schema v2 frontmatter without writing a file."""

    if isinstance(value, KnowledgeFrontmatter):
        if value.schema_version != KNOWLEDGE_SCHEMA_VERSION:
            raise KnowledgeFrontmatterError(
                "Schema v1 knowledge frontmatter is read-only compatibility and cannot "
                "be serialized; migration must be explicit"
            )
        raw_value: object = value.as_dict()
    else:
        raw_value = value
    frontmatter = validate_knowledge_frontmatter(raw_value, path=path)
    yaml_text = yaml.safe_dump(
        frontmatter.as_dict(),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
        line_break="\n",
    )
    return f"---\n{yaml_text}---\n"
