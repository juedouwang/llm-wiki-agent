#!/usr/bin/env python3
"""Deterministic in-memory Claim lifecycle and conflict coexistence contract (F-04A).

The host Agent supplies every semantic decision: the Claim identity, the desired
status, and explicit membership of a conflict set.  This module validates current
Knowledge Schema v2 frontmatter, immutable identity/timestamp invariants, and
coexistence of distinct Claim/Result entities.  It performs no filesystem I/O,
Source/Evidence access, semantic conflict inference, persistence, Markdown write,
status propagation, or public CLI/MCP/Web registration.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from tools.claim_evidence import (
    ClaimEvidenceResultIntegrityError,
    ClaimEvidenceValidationResult,
    validate_claim_evidence_result_integrity,
)
from tools.knowledge_artifacts import (
    KNOWLEDGE_SCHEMA_VERSION,
    KnowledgeArtifactError,
    KnowledgeFrontmatter,
    KnowledgeStatus,
    parse_knowledge_page,
    validate_knowledge_frontmatter,
)
from tools.project_layout import (
    CURRENT_SCHEMA_VERSION,
    InvalidProjectIdError,
    validate_project_id,
)
from tools.research_relations import ResearchEntity, ResearchRelationRegistry


CLAIM_LIFECYCLE_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
CLAIM_LIFECYCLE_VERSION = "claim-lifecycle-v1"
CLAIM_TRANSITION_KIND = "llmwiki-claim-lifecycle-transition-validation"
CLAIM_CONFLICT_KIND = "llmwiki-claim-conflict-coexistence-validation"
CLAIM_TRANSITION_IDENTITY_VERSION = "claim-transition-identity-v2"
CLAIM_CONFLICT_IDENTITY_VERSION = "claim-conflict-identity-v1"

ClaimTransitionKind = Literal["created", "status-changed", "reverified", "updated"]

_ENTITY_ID_PATTERN = re.compile(r"ent-[0-9a-f]{64}")
_TRANSITION_ID_PATTERN = re.compile(r"clt-[0-9a-f]{64}")
_CONFLICT_ID_PATTERN = re.compile(r"cfl-[0-9a-f]{64}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_IMMUTABLE_TRANSITION_FIELDS = frozenset(
    {"schema_version", "kind", "project_id", "artifact_type", "generated_at"}
)


class ClaimLifecycleError(KnowledgeArtifactError):
    """Base error for invalid F-04A lifecycle inputs or invariants."""


class ClaimLifecycleBindingError(ClaimLifecycleError):
    """Raised when a Claim/Result does not bind to the declared F-03 identity."""


class ClaimLifecycleTransitionError(ClaimLifecycleError):
    """Raised when a caller-declared lifecycle transition is structurally invalid."""


class ClaimConflictCoexistenceError(ClaimLifecycleError):
    """Raised when explicit conflicting variants cannot coexist as declared."""


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ClaimLifecycleError(
            "Claim lifecycle data must be canonical JSON-compatible"
        ) from exc


def _validated_project_id(value: object) -> str:
    if not isinstance(value, str):
        raise ClaimLifecycleError("project_id must be a string")
    try:
        return validate_project_id(value)
    except InvalidProjectIdError as exc:
        raise ClaimLifecycleError(str(exc)) from exc


def _bounded_text(value: object, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ClaimLifecycleError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ClaimLifecycleError(
            f"{field_name} must be non-empty without outer whitespace"
        )
    if len(value) > maximum:
        raise ClaimLifecycleError(
            f"{field_name} must contain at most {maximum} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ClaimLifecycleError(f"{field_name} must not contain control characters")
    return value


def _entity_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _ENTITY_ID_PATTERN.fullmatch(value) is None:
        raise ClaimLifecycleError(
            f"{field_name} must use 'ent-' plus 64 lowercase hexadecimal digits"
        )
    return value


def _frontmatter(value: object, *, path: str) -> KnowledgeFrontmatter:
    raw_value = value.as_dict() if isinstance(value, KnowledgeFrontmatter) else value
    try:
        frontmatter = validate_knowledge_frontmatter(raw_value, path=path)
    except KnowledgeArtifactError as exc:
        raise ClaimLifecycleBindingError(
            f"Claim frontmatter for {path!r} is invalid: {exc}"
        ) from exc
    if frontmatter.artifact_type != "claim":
        raise ClaimLifecycleBindingError(
            f"Claim lifecycle requires artifact_type 'claim' at {path!r}"
        )
    return frontmatter


def _fingerprint(frontmatter: KnowledgeFrontmatter) -> str:
    return hashlib.sha256(_canonical_json(frontmatter.as_dict())).hexdigest()


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)


def _claim_entity(
    registry: ResearchRelationRegistry,
    claim_entity_id: object,
) -> ResearchEntity:
    if not isinstance(registry, ResearchRelationRegistry):
        raise TypeError("registry must be a ResearchRelationRegistry")
    normalized_id = _entity_id(claim_entity_id, "claim_entity_id")
    entity = registry.by_entity_id.get(normalized_id)
    if entity is None:
        raise ClaimLifecycleBindingError(
            f"Claim lifecycle references unknown entity ID: {normalized_id}"
        )
    if entity.entity_type != "claim":
        raise ClaimLifecycleBindingError(
            f"entity {normalized_id} is not a Claim"
        )
    if entity.knowledge_path is None:
        raise ClaimLifecycleBindingError(
            f"Claim entity {normalized_id} has no materialized knowledge path"
        )
    return entity


def _bind_claim_frontmatter(
    registry: ResearchRelationRegistry,
    entity: ResearchEntity,
    value: object,
    *,
    require_current_title: bool,
) -> KnowledgeFrontmatter:
    assert entity.knowledge_path is not None
    frontmatter = _frontmatter(value, path=entity.knowledge_path)
    if frontmatter.project_id != registry.project_id:
        raise ClaimLifecycleBindingError(
            f"Claim page {entity.knowledge_path!r} belongs to another project"
        )
    if require_current_title and frontmatter.title != entity.title:
        raise ClaimLifecycleBindingError(
            f"proposed Claim title does not match entity {entity.entity_id}"
        )
    return frontmatter


def _transition_id(
    project_id: str,
    claim_entity_id: str,
    before_fingerprint: str | None,
    after_fingerprint: str,
    evidence_validation_sha256: str | None,
    conflict_validation_sha256: str | None,
) -> str:
    payload = {
        "identity_version": CLAIM_TRANSITION_IDENTITY_VERSION,
        "project_id": project_id,
        "claim_entity_id": claim_entity_id,
        "before_frontmatter_sha256": before_fingerprint,
        "after_frontmatter_sha256": after_fingerprint,
        "evidence_validation_sha256": evidence_validation_sha256,
        "conflict_validation_sha256": conflict_validation_sha256,
    }
    return f"clt-{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


@dataclass(frozen=True)
class ClaimTransitionValidation:
    """Read-only validation result for one explicit Claim lifecycle change."""

    project_id: str
    transition_id: str
    claim_entity_id: str
    knowledge_path: str
    transition_kind: ClaimTransitionKind
    from_status: KnowledgeStatus | None
    to_status: KnowledgeStatus
    from_updated_at: str | None
    to_updated_at: str
    from_last_verified_at: str | None
    to_last_verified_at: str | None
    before_frontmatter_sha256: str | None
    after_frontmatter_sha256: str
    changed_fields: tuple[str, ...]
    evidence_validation_sha256: str | None
    evidence_bindings_current: bool | None
    verified_state_current: bool | None
    conflict_validation_sha256: str | None
    conflict_id: str | None
    required_follow_up_validations: tuple[str, ...]

    def __post_init__(self) -> None:
        _validated_project_id(self.project_id)
        _entity_id(self.claim_entity_id, "claim_entity_id")
        _bounded_text(self.knowledge_path, "knowledge_path", maximum=1024)
        if _TRANSITION_ID_PATTERN.fullmatch(self.transition_id) is None:
            raise ValueError("transition_id must use 'clt-' plus 64 lowercase hex digits")
        for field_name, fingerprint in (
            ("before_frontmatter_sha256", self.before_frontmatter_sha256),
            ("after_frontmatter_sha256", self.after_frontmatter_sha256),
        ):
            if fingerprint is not None and _SHA256_PATTERN.fullmatch(fingerprint) is None:
                raise ValueError(f"{field_name} must be 64 lowercase hex digits or None")
        if self.after_frontmatter_sha256 is None:
            raise ValueError("after_frontmatter_sha256 is required")
        object.__setattr__(self, "changed_fields", tuple(self.changed_fields))
        object.__setattr__(
            self,
            "required_follow_up_validations",
            tuple(self.required_follow_up_validations),
        )
        if not self.changed_fields:
            raise ValueError("Claim transition must record at least one changed field")
        if tuple(sorted(set(self.changed_fields))) != self.changed_fields:
            raise ValueError("changed_fields must be duplicate-free canonical order")
        if (
            tuple(sorted(set(self.required_follow_up_validations)))
            != self.required_follow_up_validations
        ):
            raise ValueError(
                "required_follow_up_validations must be duplicate-free canonical order"
            )
        for field_name, fingerprint in (
            ("evidence_validation_sha256", self.evidence_validation_sha256),
            ("conflict_validation_sha256", self.conflict_validation_sha256),
        ):
            if fingerprint is not None and _SHA256_PATTERN.fullmatch(fingerprint) is None:
                raise ValueError(f"{field_name} must be 64 lowercase hex digits or None")
        if self.evidence_validation_sha256 is None:
            if (
                self.evidence_bindings_current is not None
                or self.verified_state_current is not None
            ):
                raise ValueError(
                    "Evidence currentness fields require a bound F-02B proof"
                )
        elif self.evidence_bindings_current is None:
            raise ValueError(
                "a bound F-02B proof requires an Evidence-binding outcome"
            )
        if self.to_status == "verified" and (
            self.evidence_validation_sha256 is None
            or self.evidence_bindings_current is not True
            or self.verified_state_current is not True
        ):
            raise ValueError(
                "verified transitions require a current bound F-02B proof"
            )
        if self.to_status != "verified" and self.verified_state_current is not None:
            raise ValueError(
                "non-verified transitions must not claim verified-state currentness"
            )
        if self.to_status == "conflicting":
            if (
                self.conflict_validation_sha256 is None
                or self.conflict_id is None
                or _CONFLICT_ID_PATTERN.fullmatch(self.conflict_id) is None
            ):
                raise ValueError(
                    "conflicting transitions require a bound conflict coexistence proof"
                )
        elif self.conflict_validation_sha256 is not None or self.conflict_id is not None:
            raise ValueError(
                "non-conflicting transitions must not bind a conflict coexistence proof"
            )
        if self.required_follow_up_validations:
            raise ValueError("successful transition validation must be complete")
        expected_transition_id = _transition_id(
            self.project_id,
            self.claim_entity_id,
            self.before_frontmatter_sha256,
            self.after_frontmatter_sha256,
            self.evidence_validation_sha256,
            self.conflict_validation_sha256,
        )
        if self.transition_id != expected_transition_id:
            raise ValueError("transition_id does not match the bound Claim/proof revisions")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CLAIM_LIFECYCLE_SCHEMA_VERSION,
            "kind": CLAIM_TRANSITION_KIND,
            "lifecycle_version": CLAIM_LIFECYCLE_VERSION,
            "project_id": self.project_id,
            "transition_id": self.transition_id,
            "claim_entity_id": self.claim_entity_id,
            "knowledge_path": self.knowledge_path,
            "transition_kind": self.transition_kind,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "from_updated_at": self.from_updated_at,
            "to_updated_at": self.to_updated_at,
            "from_last_verified_at": self.from_last_verified_at,
            "to_last_verified_at": self.to_last_verified_at,
            "before_frontmatter_sha256": self.before_frontmatter_sha256,
            "after_frontmatter_sha256": self.after_frontmatter_sha256,
            "changed_fields": list(self.changed_fields),
            "evidence_validation_sha256": self.evidence_validation_sha256,
            "evidence_bindings_current": self.evidence_bindings_current,
            "verified_state_current": self.verified_state_current,
            "conflict_validation_sha256": self.conflict_validation_sha256,
            "conflict_id": self.conflict_id,
            "required_follow_up_validations": list(
                self.required_follow_up_validations
            ),
            "validation_complete": True,
            "read_only": True,
            "status_persisted": False,
            "semantic_decision_made": False,
        }


@dataclass(frozen=True)
class _CurrentnessResultBinding:
    validation_sha256: str
    evidence_bindings_current: bool
    verified_state_current: bool | None


def _validate_currentness_result(
    value: ClaimEvidenceValidationResult | None,
    *,
    path: str,
    frontmatter: KnowledgeFrontmatter,
) -> _CurrentnessResultBinding | None:
    if value is None:
        return None
    if not isinstance(value, ClaimEvidenceValidationResult):
        raise TypeError("currentness must be a ClaimEvidenceValidationResult or None")
    try:
        validate_claim_evidence_result_integrity(
            value,
            path=path,
            frontmatter=frontmatter,
        )
    except ClaimEvidenceResultIntegrityError as exc:
        raise ClaimLifecycleTransitionError(
            f"F-02B currentness result failed integrity validation: {exc}"
        ) from exc
    bindings_current = all(item.valid for item in value.source_bindings)
    return _CurrentnessResultBinding(
        validation_sha256=hashlib.sha256(_canonical_json(value.as_dict())).hexdigest(),
        evidence_bindings_current=bindings_current and value.all_evidence_current,
        verified_state_current=value.verified_state_current,
    )


def validate_claim_transition(
    registry: ResearchRelationRegistry,
    *,
    claim_entity_id: object,
    before: object | None,
    after: object,
    currentness: ClaimEvidenceValidationResult | None = None,
    conflict_coexistence: ClaimConflictCoexistenceValidation | None = None,
) -> ClaimTransitionValidation:
    """Validate one host-declared Claim creation/update without persisting it.

    All pairs in the closed Knowledge Schema v2 status set are structurally
    composable. Core does not decide whether a transition is scientifically
    appropriate. It preserves Claim identity and monotonic modification time. A
    ``verified`` target requires a matching caller-supplied F-02B currentness
    result. A ``conflicting`` target requires and binds the separate coexistence
    proof returned by :func:`validate_claim_conflict_coexistence` before this
    function returns a complete validation record.
    """

    entity = _claim_entity(registry, claim_entity_id)
    after_frontmatter = _bind_claim_frontmatter(
        registry,
        entity,
        after,
        require_current_title=True,
    )
    after_dict = after_frontmatter.as_dict()
    after_fingerprint = _fingerprint(after_frontmatter)

    before_frontmatter: KnowledgeFrontmatter | None = None
    before_fingerprint: str | None = None
    if before is not None:
        before_frontmatter = _bind_claim_frontmatter(
            registry,
            entity,
            before,
            require_current_title=False,
        )
        before_dict = before_frontmatter.as_dict()
        changed_fields = tuple(
            sorted(
                field_name
                for field_name in after_dict
                if before_dict[field_name] != after_dict[field_name]
            )
        )
        if not changed_fields:
            raise ClaimLifecycleTransitionError(
                "Claim transition cannot be an identical no-op"
            )
        immutable_changes = _IMMUTABLE_TRANSITION_FIELDS.intersection(changed_fields)
        if immutable_changes:
            raise ClaimLifecycleTransitionError(
                "Claim transition cannot change immutable identity fields: "
                + ", ".join(sorted(immutable_changes))
            )
        if _timestamp(after_frontmatter.updated_at) <= _timestamp(
            before_frontmatter.updated_at
        ):
            raise ClaimLifecycleTransitionError(
                "Claim transition updated_at must advance strictly"
            )
        if (
            after_frontmatter.status != "verified"
            and after_frontmatter.last_verified_at
            != before_frontmatter.last_verified_at
        ):
            raise ClaimLifecycleTransitionError(
                "a non-verified transition must preserve last_verified_at "
                "verification history"
            )
        before_fingerprint = _fingerprint(before_frontmatter)
    else:
        before_dict = None
        changed_fields = tuple(sorted(after_dict))

    if (
        after_frontmatter.status == "verified"
        and after_frontmatter.last_verified_at != after_frontmatter.updated_at
    ):
        raise ClaimLifecycleTransitionError(
            "a transition targeting verified must set last_verified_at == updated_at"
        )

    if before_frontmatter is None:
        transition_kind: ClaimTransitionKind = "created"
    elif before_frontmatter.status != after_frontmatter.status:
        transition_kind = "status-changed"
    elif (
        after_frontmatter.status == "verified"
        and before_frontmatter.last_verified_at
        != after_frontmatter.last_verified_at
    ):
        transition_kind = "reverified"
    else:
        transition_kind = "updated"

    currentness_binding = _validate_currentness_result(
        currentness,
        path=entity.knowledge_path,
        frontmatter=after_frontmatter,
    )
    if after_frontmatter.status == "verified" and (
        currentness_binding is None
        or not currentness_binding.evidence_bindings_current
        or currentness_binding.verified_state_current is not True
    ):
        raise ClaimLifecycleTransitionError(
            "a transition targeting verified requires a matching current F-02B result"
        )

    if after_frontmatter.status != "conflicting" and conflict_coexistence is not None:
        raise ClaimLifecycleTransitionError(
            "non-conflicting transition must not bind a conflict coexistence proof"
        )
    conflict_binding = _validate_conflict_result(
        conflict_coexistence,
        registry=registry,
        entity=entity,
        after_fingerprint=after_fingerprint,
    )
    if after_frontmatter.status == "conflicting" and conflict_binding is None:
        raise ClaimLifecycleTransitionError(
            "a transition targeting conflicting requires a matching coexistence proof"
        )

    evidence_validation_sha256 = (
        currentness_binding.validation_sha256
        if currentness_binding is not None
        else None
    )
    conflict_validation_sha256 = (
        conflict_binding.validation_sha256 if conflict_binding is not None else None
    )
    assert entity.knowledge_path is not None
    return ClaimTransitionValidation(
        project_id=registry.project_id,
        transition_id=_transition_id(
            registry.project_id,
            entity.entity_id,
            before_fingerprint,
            after_fingerprint,
            evidence_validation_sha256,
            conflict_validation_sha256,
        ),
        claim_entity_id=entity.entity_id,
        knowledge_path=entity.knowledge_path,
        transition_kind=transition_kind,
        from_status=(before_frontmatter.status if before_frontmatter else None),
        to_status=after_frontmatter.status,
        from_updated_at=(before_frontmatter.updated_at if before_frontmatter else None),
        to_updated_at=after_frontmatter.updated_at,
        from_last_verified_at=(
            before_frontmatter.last_verified_at if before_frontmatter else None
        ),
        to_last_verified_at=after_frontmatter.last_verified_at,
        before_frontmatter_sha256=before_fingerprint,
        after_frontmatter_sha256=after_fingerprint,
        changed_fields=changed_fields,
        evidence_validation_sha256=evidence_validation_sha256,
        evidence_bindings_current=(
            currentness_binding.evidence_bindings_current
            if currentness_binding is not None
            else None
        ),
        verified_state_current=(
            currentness_binding.verified_state_current
            if currentness_binding is not None
            else None
        ),
        conflict_validation_sha256=conflict_validation_sha256,
        conflict_id=(conflict_binding.conflict_id if conflict_binding is not None else None),
        required_follow_up_validations=(),
    )


@dataclass(frozen=True)
class ClaimConflictVariant:
    """Caller-declared membership of one Claim variant and its Results."""

    claim_entity_id: str
    result_entity_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        claim_entity_id = _entity_id(self.claim_entity_id, "claim_entity_id")
        raw_results = self.result_entity_ids
        if isinstance(raw_results, (str, bytes)) or not isinstance(raw_results, Iterable):
            raise ClaimConflictCoexistenceError(
                "result_entity_ids must be an iterable of Result entity IDs"
            )
        normalized: list[str] = []
        seen: set[str] = set()
        for value in raw_results:
            result_id = _entity_id(value, "result_entity_ids entry")
            if result_id in seen:
                raise ClaimConflictCoexistenceError(
                    f"conflict variant contains duplicate Result entity ID: {result_id}"
                )
            seen.add(result_id)
            normalized.append(result_id)
        if not normalized:
            raise ClaimConflictCoexistenceError(
                "each conflicting Claim variant requires at least one Result entity"
            )
        object.__setattr__(self, "claim_entity_id", claim_entity_id)
        object.__setattr__(self, "result_entity_ids", tuple(sorted(normalized)))


@dataclass(frozen=True)
class ClaimConflictResultBinding:
    """One current Schema v2 Result page bound to a conflict variant."""

    result_entity_id: str
    knowledge_path: str
    frontmatter_sha256: str

    def __post_init__(self) -> None:
        _entity_id(self.result_entity_id, "result_entity_id")
        _bounded_text(self.knowledge_path, "knowledge_path", maximum=1024)
        if _SHA256_PATTERN.fullmatch(self.frontmatter_sha256) is None:
            raise ValueError("frontmatter_sha256 must be 64 lowercase hex digits")

    def as_dict(self) -> dict[str, str]:
        return {
            "result_entity_id": self.result_entity_id,
            "knowledge_path": self.knowledge_path,
            "frontmatter_sha256": self.frontmatter_sha256,
        }


@dataclass(frozen=True)
class ClaimConflictVariantBinding:
    """One conflicting Claim page and its explicitly assigned Result pages."""

    claim_entity_id: str
    knowledge_path: str
    frontmatter_sha256: str
    results: tuple[ClaimConflictResultBinding, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))
        _entity_id(self.claim_entity_id, "claim_entity_id")
        _bounded_text(self.knowledge_path, "knowledge_path", maximum=1024)
        if _SHA256_PATTERN.fullmatch(self.frontmatter_sha256) is None:
            raise ValueError("frontmatter_sha256 must be 64 lowercase hex digits")
        if not self.results:
            raise ValueError("a conflict variant binding requires at least one Result")
        result_ids = tuple(item.result_entity_id for item in self.results)
        if tuple(sorted(set(result_ids))) != result_ids:
            raise ValueError(
                "conflict Result bindings must be duplicate-free canonical order"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_entity_id": self.claim_entity_id,
            "knowledge_path": self.knowledge_path,
            "status": "conflicting",
            "frontmatter_sha256": self.frontmatter_sha256,
            "results": [item.as_dict() for item in self.results],
        }


def claim_conflict_id_for(project_id: object, identity_key: object) -> str:
    """Return a stable conflict identity from an explicit host-supplied key."""

    normalized_project = _validated_project_id(project_id)
    normalized_key = _bounded_text(identity_key, "identity_key", maximum=512)
    payload = {
        "identity_version": CLAIM_CONFLICT_IDENTITY_VERSION,
        "project_id": normalized_project,
        "identity_key": normalized_key,
    }
    return f"cfl-{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


@dataclass(frozen=True)
class ClaimConflictCoexistenceValidation:
    """Read-only proof that distinct conflicting Claims and Results coexist."""

    project_id: str
    conflict_id: str
    identity_key: str
    variants: tuple[ClaimConflictVariantBinding, ...]

    def __post_init__(self) -> None:
        _validated_project_id(self.project_id)
        normalized_key = _bounded_text(self.identity_key, "identity_key", maximum=512)
        if _CONFLICT_ID_PATTERN.fullmatch(self.conflict_id) is None:
            raise ValueError("conflict_id must use 'cfl-' plus 64 lowercase hex digits")
        if self.conflict_id != claim_conflict_id_for(self.project_id, normalized_key):
            raise ValueError("conflict_id does not match project_id and identity_key")
        object.__setattr__(self, "variants", tuple(self.variants))
        if len(self.variants) < 2:
            raise ValueError("conflict coexistence requires at least two variants")
        claim_ids = tuple(item.claim_entity_id for item in self.variants)
        if tuple(sorted(set(claim_ids))) != claim_ids:
            raise ValueError("conflict variants must be duplicate-free canonical order")
        result_ids = {
            result.result_entity_id
            for variant in self.variants
            for result in variant.results
        }
        if len(result_ids) < 2:
            raise ValueError(
                "conflict coexistence requires at least two distinct Results overall"
            )

    @property
    def claim_count(self) -> int:
        return len(self.variants)

    @property
    def result_count(self) -> int:
        return len(
            {
                result.result_entity_id
                for variant in self.variants
                for result in variant.results
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CLAIM_LIFECYCLE_SCHEMA_VERSION,
            "kind": CLAIM_CONFLICT_KIND,
            "lifecycle_version": CLAIM_LIFECYCLE_VERSION,
            "project_id": self.project_id,
            "conflict_id": self.conflict_id,
            "identity_key": self.identity_key,
            "status": "conflicting",
            "claim_count": self.claim_count,
            "result_count": self.result_count,
            "variants": [item.as_dict() for item in self.variants],
            "read_only": True,
            "status_persisted": False,
            "semantic_conflict_inferred": False,
        }


@dataclass(frozen=True)
class _ConflictResultBinding:
    validation_sha256: str
    conflict_id: str


def _validate_conflict_result(
    value: ClaimConflictCoexistenceValidation | None,
    *,
    registry: ResearchRelationRegistry,
    entity: ResearchEntity,
    after_fingerprint: str,
) -> _ConflictResultBinding | None:
    if value is None:
        return None
    if not isinstance(value, ClaimConflictCoexistenceValidation):
        raise TypeError(
            "conflict_coexistence must be a "
            "ClaimConflictCoexistenceValidation or None"
        )
    if value.project_id != registry.project_id:
        raise ClaimLifecycleTransitionError(
            "conflict coexistence proof belongs to another project"
        )
    if value.conflict_id != claim_conflict_id_for(
        value.project_id,
        value.identity_key,
    ):
        raise ClaimLifecycleTransitionError(
            "conflict coexistence proof has an inconsistent conflict identity"
        )
    target = next(
        (
            variant
            for variant in value.variants
            if variant.claim_entity_id == entity.entity_id
        ),
        None,
    )
    if target is None:
        raise ClaimLifecycleTransitionError(
            "conflict coexistence proof does not include the target Claim"
        )
    if (
        target.knowledge_path != entity.knowledge_path
        or target.frontmatter_sha256 != after_fingerprint
    ):
        raise ClaimLifecycleTransitionError(
            "conflict coexistence proof does not match the proposed Claim revision"
        )

    entities = registry.by_entity_id
    result_ids: set[str] = set()
    for variant in value.variants:
        claim_entity = entities.get(variant.claim_entity_id)
        if (
            claim_entity is None
            or claim_entity.entity_type != "claim"
            or claim_entity.knowledge_path != variant.knowledge_path
        ):
            raise ClaimLifecycleTransitionError(
                "conflict coexistence proof has a stale Claim entity binding"
            )
        for result in variant.results:
            result_entity = entities.get(result.result_entity_id)
            if (
                result_entity is None
                or result_entity.entity_type != "result"
                or result_entity.knowledge_path != result.knowledge_path
            ):
                raise ClaimLifecycleTransitionError(
                    "conflict coexistence proof has a stale Result binding"
                )
            result_ids.add(result.result_entity_id)
    if len(value.variants) < 2 or len(result_ids) < 2:
        raise ClaimLifecycleTransitionError(
            "conflict coexistence proof requires at least two Claims and Results"
        )
    return _ConflictResultBinding(
        validation_sha256=hashlib.sha256(
            _canonical_json(value.as_dict())
        ).hexdigest(),
        conflict_id=value.conflict_id,
    )


def _bound_entity_page(
    registry: ResearchRelationRegistry,
    entity: ResearchEntity,
    *,
    knowledge_pages: Mapping[str, bytes],
) -> KnowledgeFrontmatter:
    if entity.knowledge_path is None:
        raise ClaimLifecycleBindingError(
            f"entity {entity.entity_id} has no materialized knowledge path"
        )
    payload = knowledge_pages.get(entity.knowledge_path)
    if payload is None:
        raise ClaimLifecycleBindingError(
            f"entity page is missing from current knowledge set: {entity.knowledge_path}"
        )
    if not isinstance(payload, bytes):
        raise ClaimLifecycleBindingError(
            f"knowledge page {entity.knowledge_path!r} must be supplied as bytes"
        )
    try:
        page = parse_knowledge_page(payload, path=entity.knowledge_path)
    except KnowledgeArtifactError as exc:
        raise ClaimLifecycleBindingError(
            f"knowledge page {entity.knowledge_path!r} is invalid: {exc}"
        ) from exc
    frontmatter = page.frontmatter
    if frontmatter.schema_version != KNOWLEDGE_SCHEMA_VERSION:
        raise ClaimLifecycleBindingError(
            f"knowledge page {entity.knowledge_path!r} must use current Schema v2"
        )
    if frontmatter.project_id != registry.project_id:
        raise ClaimLifecycleBindingError(
            f"knowledge page {entity.knowledge_path!r} belongs to another project"
        )
    if frontmatter.artifact_type != entity.entity_type:
        raise ClaimLifecycleBindingError(
            f"entity {entity.entity_id} does not match current page type"
        )
    if frontmatter.title != entity.title:
        raise ClaimLifecycleBindingError(
            f"entity title is stale for page {entity.knowledge_path!r}"
        )
    return frontmatter


def _normalized_variants(value: object) -> tuple[ClaimConflictVariant, ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise ClaimConflictCoexistenceError(
            "variants must be an iterable of ClaimConflictVariant values"
        )
    variants = tuple(value)
    if any(not isinstance(item, ClaimConflictVariant) for item in variants):
        raise ClaimConflictCoexistenceError(
            "variants must contain only ClaimConflictVariant values"
        )
    if len(variants) < 2:
        raise ClaimConflictCoexistenceError(
            "conflict coexistence requires at least two Claim variants"
        )
    claim_ids = [item.claim_entity_id for item in variants]
    if len(set(claim_ids)) != len(claim_ids):
        raise ClaimConflictCoexistenceError(
            "conflict coexistence contains duplicate Claim entity IDs"
        )
    result_ids = {
        result_id
        for variant in variants
        for result_id in variant.result_entity_ids
    }
    if len(result_ids) < 2:
        raise ClaimConflictCoexistenceError(
            "conflict coexistence requires at least two distinct Result entities"
        )
    return tuple(sorted(variants, key=lambda item: item.claim_entity_id))


def validate_claim_conflict_coexistence(
    registry: ResearchRelationRegistry,
    *,
    identity_key: object,
    variants: object,
    knowledge_pages: Mapping[str, bytes],
) -> ClaimConflictCoexistenceValidation:
    """Validate explicit conflicting Claim variants without inferring their meaning.

    Each variant is a distinct F-03 Claim entity with a current Schema v2 detail
    page already marked ``conflicting`` by the host. Each variant references at least
    one Result entity, at least two distinct Results coexist overall, and a Result may
    be shared by multiple caller-declared interpretations. The function
    validates and fingerprints caller-supplied bytes only; it never changes a page,
    registry, relation, or status.
    """

    if not isinstance(registry, ResearchRelationRegistry):
        raise TypeError("registry must be a ResearchRelationRegistry")
    if not isinstance(knowledge_pages, Mapping):
        raise ClaimConflictCoexistenceError("knowledge_pages must be a mapping")
    normalized_key = _bounded_text(identity_key, "identity_key", maximum=512)
    normalized_variants = _normalized_variants(variants)
    entities = registry.by_entity_id
    bindings: list[ClaimConflictVariantBinding] = []

    for variant in normalized_variants:
        claim_entity = entities.get(variant.claim_entity_id)
        if claim_entity is None:
            raise ClaimLifecycleBindingError(
                f"conflict variant references unknown Claim entity: "
                f"{variant.claim_entity_id}"
            )
        if claim_entity.entity_type != "claim":
            raise ClaimLifecycleBindingError(
                f"conflict variant entity {claim_entity.entity_id} is not a Claim"
            )
        claim_frontmatter = _bound_entity_page(
            registry,
            claim_entity,
            knowledge_pages=knowledge_pages,
        )
        if claim_frontmatter.status != "conflicting":
            raise ClaimConflictCoexistenceError(
                f"Claim {claim_entity.entity_id} must explicitly use status 'conflicting'"
            )

        result_bindings: list[ClaimConflictResultBinding] = []
        for result_entity_id in variant.result_entity_ids:
            result_entity = entities.get(result_entity_id)
            if result_entity is None:
                raise ClaimLifecycleBindingError(
                    f"conflict variant references unknown Result entity: "
                    f"{result_entity_id}"
                )
            if result_entity.entity_type != "result":
                raise ClaimLifecycleBindingError(
                    f"conflict result entity {result_entity_id} is not a Result"
                )
            result_frontmatter = _bound_entity_page(
                registry,
                result_entity,
                knowledge_pages=knowledge_pages,
            )
            assert result_entity.knowledge_path is not None
            result_bindings.append(
                ClaimConflictResultBinding(
                    result_entity_id=result_entity.entity_id,
                    knowledge_path=result_entity.knowledge_path,
                    frontmatter_sha256=_fingerprint(result_frontmatter),
                )
            )

        assert claim_entity.knowledge_path is not None
        bindings.append(
            ClaimConflictVariantBinding(
                claim_entity_id=claim_entity.entity_id,
                knowledge_path=claim_entity.knowledge_path,
                frontmatter_sha256=_fingerprint(claim_frontmatter),
                results=tuple(
                    sorted(result_bindings, key=lambda item: item.result_entity_id)
                ),
            )
        )

    return ClaimConflictCoexistenceValidation(
        project_id=registry.project_id,
        conflict_id=claim_conflict_id_for(registry.project_id, normalized_key),
        identity_key=normalized_key,
        variants=tuple(bindings),
    )
