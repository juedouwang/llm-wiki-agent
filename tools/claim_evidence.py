#!/usr/bin/env python3
"""F-02B read-only Claim-to-Evidence registry and currentness validation.

This module consumes strict Knowledge Schema v2 frontmatter supplied by a caller.
It never opens or rewrites the Markdown page, registers Evidence, repairs source
bindings, infers stance, or exposes a CLI/MCP/Web surface.  Exact source bytes are
opened only through the existing deterministic Locator contract, with automatic
relocation recovery explicitly disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.evidence_registry import (
    Evidence,
    EvidenceRegistry,
    load_evidence_registry,
    validate_evidence,
)
from tools.knowledge_artifacts import (
    EvidenceRef,
    EvidenceStance,
    KnowledgeFrontmatter,
    KnowledgeFrontmatterError,
    artifact_contract_for_path,
    validate_knowledge_frontmatter,
)
from tools.project_layout import CURRENT_SCHEMA_VERSION
from tools.project_registry import ProjectRegistrationResult, load_registered_project
from tools.source_access import SourceAccessError, open_source
from tools.source_recovery import (
    SourceRecoveryError,
    SourceRelocationInspectionResult,
    inspect_source_relocation,
)
from tools.source_registry import SourceRegistry, load_source_registry


CLAIM_EVIDENCE_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
CLAIM_EVIDENCE_VALIDATION_KIND = "llmwiki-claim-evidence-validation"
CLAIM_EVIDENCE_SOURCE_KIND = "llmwiki-claim-source-binding-validation"
CLAIM_EVIDENCE_REFERENCE_KIND = "llmwiki-claim-evidence-reference-validation"
CLAIM_EVIDENCE_VALIDATION_VERSION = "claim-evidence-validation-v1"


@dataclass(frozen=True)
class ClaimSourceBindingValidation:
    """Registration result for one source ID declared by a Claim."""

    source_id: str
    valid: bool
    reason_code: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CLAIM_EVIDENCE_SCHEMA_VERSION,
            "kind": CLAIM_EVIDENCE_SOURCE_KIND,
            "validation_version": CLAIM_EVIDENCE_VALIDATION_VERSION,
            "source_id": self.source_id,
            "valid": self.valid,
            "reason_code": self.reason_code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ClaimEvidenceReferenceValidation:
    """Currentness result for one explicit directional Evidence reference."""

    evidence_id: str
    stance: EvidenceStance
    source_id: str | None
    current: bool
    reason_code: str
    detail: str
    observed_excerpt_hash: str | None = None
    relocation: SourceRelocationInspectionResult | None = None
    relocation_inspection_reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.relocation is not None and self.relocation.source_id != self.source_id:
            raise ValueError("relocation inspection belongs to another source")
        if self.relocation is not None and self.relocation_inspection_reason_code is not None:
            raise ValueError(
                "a relocation result and relocation inspection failure are mutually exclusive"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CLAIM_EVIDENCE_SCHEMA_VERSION,
            "kind": CLAIM_EVIDENCE_REFERENCE_KIND,
            "validation_version": CLAIM_EVIDENCE_VALIDATION_VERSION,
            "evidence_id": self.evidence_id,
            "stance": self.stance,
            "source_id": self.source_id,
            "current": self.current,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "observed_excerpt_hash": self.observed_excerpt_hash,
            "relocation": (
                self.relocation.as_dict() if self.relocation is not None else None
            ),
            "relocation_inspection_reason_code": (
                self.relocation_inspection_reason_code
            ),
        }


@dataclass(frozen=True)
class ClaimEvidenceValidationResult:
    """Deterministic closure over one Claim's declared sources and Evidence."""

    project_id: str
    path: str
    claim_status: str
    key_claim: bool
    valid: bool
    reason_codes: tuple[str, ...]
    source_bindings: tuple[ClaimSourceBindingValidation, ...]
    evidence_references: tuple[ClaimEvidenceReferenceValidation, ...]
    all_evidence_current: bool
    current_supporting_evidence_count: int
    verified_state_current: bool | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "source_bindings", tuple(self.source_bindings))
        object.__setattr__(self, "evidence_references", tuple(self.evidence_references))
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("claim validation reason_codes must be duplicate-free")
        if self.current_supporting_evidence_count < 0:
            raise ValueError("current supporting Evidence count must be non-negative")
        if self.claim_status == "verified" and self.verified_state_current is None:
            raise ValueError("verified Claims require an explicit currentness outcome")
        if self.claim_status != "verified" and self.verified_state_current is not None:
            raise ValueError("non-verified Claims must not claim verified-state currentness")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CLAIM_EVIDENCE_SCHEMA_VERSION,
            "kind": CLAIM_EVIDENCE_VALIDATION_KIND,
            "validation_version": CLAIM_EVIDENCE_VALIDATION_VERSION,
            "project_id": self.project_id,
            "path": self.path,
            "claim_status": self.claim_status,
            "key_claim": self.key_claim,
            "valid": self.valid,
            "reason_codes": list(self.reason_codes),
            "source_bindings": [item.as_dict() for item in self.source_bindings],
            "evidence_references": [
                item.as_dict() for item in self.evidence_references
            ],
            "all_evidence_current": self.all_evidence_current,
            "current_supporting_evidence_count": (
                self.current_supporting_evidence_count
            ),
            "verified_state_current": self.verified_state_current,
            "read_only": True,
        }


def _append_reason(reason_codes: list[str], reason_code: str) -> None:
    if reason_code not in reason_codes:
        reason_codes.append(reason_code)


def _empty_evidence_registry(
    project_id: str,
    evidence_file: Path,
) -> EvidenceRegistry:
    return EvidenceRegistry(project_id, evidence_file)


def _load_registries(
    workspace_root: str | Path,
    project_id: str,
) -> tuple[ProjectRegistrationResult, SourceRegistry, EvidenceRegistry]:
    registration = load_registered_project(workspace_root, project_id)
    source_registry = load_source_registry(workspace_root, registration.project_id)
    evidence_file = registration.layout.evidence_file
    if evidence_file.exists() or evidence_file.is_symlink():
        evidence_registry = load_evidence_registry(
            workspace_root,
            registration.project_id,
        )
    else:
        evidence_registry = _empty_evidence_registry(
            registration.project_id,
            evidence_file,
        )
    return registration, source_registry, evidence_registry


def _source_bindings(
    source_ids: tuple[str, ...],
    source_registry: SourceRegistry,
) -> tuple[ClaimSourceBindingValidation, ...]:
    results: list[ClaimSourceBindingValidation] = []
    for source_id in source_ids:
        if source_id in source_registry.by_source_id:
            results.append(
                ClaimSourceBindingValidation(
                    source_id=source_id,
                    valid=True,
                    reason_code="claim-source-registered",
                    detail="Claim source_id is registered for the requested project.",
                )
            )
        else:
            results.append(
                ClaimSourceBindingValidation(
                    source_id=source_id,
                    valid=False,
                    reason_code="claim-source-not-registered",
                    detail="Claim source_id is not registered for the requested project.",
                )
            )
    return tuple(results)


def _missing_reference(reference: EvidenceRef) -> ClaimEvidenceReferenceValidation:
    return ClaimEvidenceReferenceValidation(
        evidence_id=reference.evidence_id,
        stance=reference.stance,
        source_id=None,
        current=False,
        reason_code="claim-evidence-not-registered",
        detail="Claim evidence_id is not present in the project Evidence registry.",
    )


def _invalid_reference_binding(
    reference: EvidenceRef,
    evidence: Evidence,
    *,
    reason_code: str,
    detail: str,
) -> ClaimEvidenceReferenceValidation:
    return ClaimEvidenceReferenceValidation(
        evidence_id=reference.evidence_id,
        stance=reference.stance,
        source_id=evidence.source_id,
        current=False,
        reason_code=reason_code,
        detail=detail,
    )


def _relocation_after_access_failure(
    workspace_root: str | Path,
    project_id: str,
    evidence: Evidence,
) -> tuple[SourceRelocationInspectionResult | None, str | None]:
    try:
        inspection = inspect_source_relocation(
            workspace_root,
            project_id,
            evidence.source_id,
        )
    except SourceRecoveryError as exc:
        return None, exc.reason_code
    if inspection.current:
        return None, None
    return inspection, None


def _validate_reference(
    workspace_root: str | Path,
    project_id: str,
    reference: EvidenceRef,
    evidence: Evidence,
    *,
    claim_source_ids: frozenset[str],
    source_registry: SourceRegistry,
) -> ClaimEvidenceReferenceValidation:
    if evidence.project_id != project_id:
        return _invalid_reference_binding(
            reference,
            evidence,
            reason_code="evidence-project-mismatch",
            detail="Evidence belongs to a different project.",
        )
    if evidence.source_id not in claim_source_ids:
        return _invalid_reference_binding(
            reference,
            evidence,
            reason_code="evidence-source-not-declared",
            detail="Evidence source_id is not declared in Claim source_ids.",
        )

    registry_validation = validate_evidence(evidence, source_registry)
    if not registry_validation.valid:
        return _invalid_reference_binding(
            reference,
            evidence,
            reason_code=registry_validation.reason_code,
            detail=registry_validation.reason,
        )

    try:
        opened = open_source(
            workspace_root,
            project_id,
            source_id=evidence.source_id,
            locator=evidence.locator,
            expected_content_hash=evidence.content_hash,
            expected_excerpt_hash=evidence.excerpt_hash,
            evidence_id=evidence.evidence_id,
            evidence_source_version=evidence.source_version,
            recover_relocation=False,
        )
    except SourceAccessError as exc:
        relocation, relocation_inspection_reason_code = (
            _relocation_after_access_failure(
                workspace_root,
                project_id,
                evidence,
            )
        )
        reason_code = exc.reason_code
        detail = str(exc)
        if relocation_inspection_reason_code is not None:
            detail = (
                f"{detail} Read-only relocation inspection failed closed with "
                f"{relocation_inspection_reason_code}."
            )
        if relocation is not None:
            if relocation.relocatable:
                reason_code = "source-relocation-unrecorded"
                detail = (
                    "A unique exact-hash relocation candidate exists, but the current "
                    "source registry binding was not changed by validation."
                )
            elif relocation.ambiguous:
                reason_code = relocation.reason_code
                detail = relocation.detail
        return ClaimEvidenceReferenceValidation(
            evidence_id=reference.evidence_id,
            stance=reference.stance,
            source_id=evidence.source_id,
            current=False,
            reason_code=reason_code,
            detail=detail,
            relocation=relocation,
            relocation_inspection_reason_code=relocation_inspection_reason_code,
        )

    final_validation = validate_evidence(
        evidence,
        source_registry,
        observed_content_hash=opened.source.content_hash,
        excerpt=opened.excerpt,
    )
    if not final_validation.valid:
        return _invalid_reference_binding(
            reference,
            evidence,
            reason_code=final_validation.reason_code,
            detail=final_validation.reason,
        )
    return ClaimEvidenceReferenceValidation(
        evidence_id=reference.evidence_id,
        stance=reference.stance,
        source_id=evidence.source_id,
        current=True,
        reason_code="evidence-current",
        detail=(
            "Evidence project, source version, content hash, Locator, and excerpt "
            "match current registered source bytes."
        ),
        observed_excerpt_hash=opened.excerpt_hash,
    )


def _project_mismatch_result(
    *,
    requested_project_id: str,
    path: str,
    frontmatter: KnowledgeFrontmatter,
    key_claim: bool,
) -> ClaimEvidenceValidationResult:
    return ClaimEvidenceValidationResult(
        project_id=requested_project_id,
        path=path,
        claim_status=frontmatter.status,
        key_claim=key_claim,
        valid=False,
        reason_codes=("claim-project-mismatch",),
        source_bindings=(),
        evidence_references=(),
        all_evidence_current=False,
        current_supporting_evidence_count=0,
        verified_state_current=False if frontmatter.status == "verified" else None,
    )


def validate_claim_evidence(
    workspace_root: str | Path,
    project_id: str,
    *,
    path: object,
    payload: object,
) -> ClaimEvidenceValidationResult:
    """Validate one strict Schema v2 Claim against current local registries/bytes.

    Structural schema/path errors fail closed through ``knowledge_artifacts``.
    Binding and currentness failures are returned as explicit result records.  The
    function is read-only: relocation candidates are inspected, never persisted.
    """

    contract = artifact_contract_for_path(path)
    raw_payload = (
        payload.as_dict() if isinstance(payload, KnowledgeFrontmatter) else payload
    )
    frontmatter = validate_knowledge_frontmatter(raw_payload, path=contract.path)
    if frontmatter.artifact_type != "claim":
        raise KnowledgeFrontmatterError(
            "claim Evidence validation requires artifact_type 'claim'"
        )
    key_claim = contract.page_role == "detail"

    registration, source_registry, evidence_registry = _load_registries(
        workspace_root,
        project_id,
    )
    if frontmatter.project_id != registration.project_id:
        return _project_mismatch_result(
            requested_project_id=registration.project_id,
            path=contract.path,
            frontmatter=frontmatter,
            key_claim=key_claim,
        )

    bindings = _source_bindings(frontmatter.source_ids, source_registry)
    references = frontmatter.evidence_refs or ()
    evidence_results: list[ClaimEvidenceReferenceValidation] = []
    claim_source_ids = frozenset(frontmatter.source_ids)
    for reference in references:
        evidence = evidence_registry.by_evidence_id.get(reference.evidence_id)
        if evidence is None:
            evidence_results.append(_missing_reference(reference))
            continue
        evidence_results.append(
            _validate_reference(
                workspace_root,
                registration.project_id,
                reference,
                evidence,
                claim_source_ids=claim_source_ids,
                source_registry=source_registry,
            )
        )

    binding_valid = all(item.valid for item in bindings)
    all_evidence_current = all(item.current for item in evidence_results)
    supporting_count = sum(
        item.current and item.stance == "supporting" for item in evidence_results
    )
    reason_codes: list[str] = []
    for binding in bindings:
        if not binding.valid:
            _append_reason(reason_codes, binding.reason_code)
    for reference in evidence_results:
        if not reference.current:
            _append_reason(reason_codes, reference.reason_code)

    verified_state_current: bool | None = None
    timestamp_current = frontmatter.last_verified_at == frontmatter.updated_at
    if frontmatter.status == "verified":
        if not timestamp_current:
            _append_reason(
                reason_codes,
                "verified-claim-modified-after-verification",
            )
        if key_claim and supporting_count == 0:
            _append_reason(
                reason_codes,
                "verified-claim-missing-current-support",
            )
        if not all_evidence_current:
            _append_reason(
                reason_codes,
                "verified-claim-declares-noncurrent-evidence",
            )
        verified_state_current = (
            binding_valid
            and all_evidence_current
            and (not key_claim or supporting_count > 0)
            and timestamp_current
        )

    valid = binding_valid and all_evidence_current
    if verified_state_current is not None:
        valid = valid and verified_state_current
    if valid:
        reason_codes.append("claim-evidence-current")

    return ClaimEvidenceValidationResult(
        project_id=registration.project_id,
        path=contract.path,
        claim_status=frontmatter.status,
        key_claim=key_claim,
        valid=valid,
        reason_codes=tuple(reason_codes),
        source_bindings=bindings,
        evidence_references=tuple(evidence_results),
        all_evidence_current=all_evidence_current,
        current_supporting_evidence_count=supporting_count,
        verified_state_current=verified_state_current,
    )
