from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

from tools.claim_evidence import (
    ClaimEvidenceReferenceValidation,
    ClaimEvidenceValidationResult,
    ClaimSourceBindingValidation,
    claim_frontmatter_sha256,
    validate_claim_evidence,
)
from tools.claim_lifecycle import (
    CLAIM_CONFLICT_KIND,
    CLAIM_LIFECYCLE_SCHEMA_VERSION,
    CLAIM_TRANSITION_KIND,
    ClaimConflictCoexistenceError,
    ClaimConflictCoexistenceValidation,
    ClaimConflictVariant,
    ClaimLifecycleBindingError,
    ClaimLifecycleTransitionError,
    claim_conflict_id_for,
    validate_claim_conflict_coexistence,
    validate_claim_transition,
)
from tools.evidence_registry import register_evidence
from tools.extraction_schema import LineRangeLocator
from tools.knowledge_artifacts import (
    KNOWLEDGE_PAGE_KIND,
    KNOWLEDGE_SCHEMA_VERSION,
    KNOWLEDGE_STATUSES,
    artifact_contract_for_path,
    serialize_knowledge_frontmatter,
    validate_knowledge_frontmatter,
)
from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.research_relations import (
    ResearchEntity,
    ResearchRelationRegistry,
    create_research_entity,
    create_research_relation,
)
from tools.source_registry import load_source_registry, sync_source_registry

PROJECT_ID = "tiny-study-0123456789ab"
OTHER_PROJECT_ID = "other-study-abcdef012345"
SOURCE_ID = "src-" + "1" * 32
OTHER_SOURCE_ID = "src-" + "9" * 32
EVIDENCE_ID = "evd-" + "2" * 64
OTHER_EVIDENCE_ID = "evd-" + "8" * 64
UNKNOWN_ENTITY_ID = "ent-" + "f" * 64


def claim_frontmatter(
    entity: ResearchEntity,
    *,
    status: str = "draft",
    updated_at: str = "2026-07-18T01:00:00Z",
    generated_at: str = "2026-07-18T00:00:00Z",
    last_verified_at: str | None | object = ...,
    project_id: str = PROJECT_ID,
    title: str | None = None,
    source_ids: tuple[str, ...] = (SOURCE_ID,),
    evidence_id: str = EVIDENCE_ID,
    evidence_stance: str = "supporting",
    ownership: str = "generated",
) -> dict[str, object]:
    if last_verified_at is ...:
        last_verified_at = updated_at if status == "verified" else None
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "kind": KNOWLEDGE_PAGE_KIND,
        "project_id": project_id,
        "artifact_type": "claim",
        "title": entity.title if title is None else title,
        "status": status,
        "ownership": ownership,
        "source_ids": list(source_ids),
        "evidence_refs": [
            {"evidence_id": evidence_id, "stance": evidence_stance}
        ],
        "generated_at": generated_at,
        "updated_at": updated_at,
        "last_verified_at": last_verified_at,
    }


def artifact_frontmatter(
    path: str,
    title: str,
    *,
    status: str = "draft",
    project_id: str = PROJECT_ID,
) -> dict[str, object]:
    contract = artifact_contract_for_path(path)
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "kind": KNOWLEDGE_PAGE_KIND,
        "project_id": project_id,
        "artifact_type": contract.artifact_type,
        "title": title,
        "status": status,
        "ownership": "generated",
        "source_ids": [],
        "evidence_refs": [],
        "generated_at": "2026-07-18T00:00:00Z",
        "updated_at": "2026-07-18T01:00:00Z",
        "last_verified_at": None,
    }


def page_bytes(path: str, value: dict[str, object], body: str = "# Body\n") -> bytes:
    return (serialize_knowledge_frontmatter(value, path=path) + body).encode("utf-8")


def raw_page_bytes(value: dict[str, object], body: str = "# Body\n") -> bytes:
    rendered = yaml.safe_dump(
        value,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
        line_break="\n",
    )
    return f"---\n{rendered}---\n{body}".encode("utf-8")


def currentness_proof(
    *,
    path: str,
    value: dict[str, object],
    source_valid: bool = True,
    evidence_current: bool = True,
    project_id: str | None = None,
    key_claim: bool = True,
) -> ClaimEvidenceValidationResult:
    refs = value["evidence_refs"]
    source_ids = value["source_ids"]
    assert isinstance(refs, list)
    assert isinstance(source_ids, list)
    frontmatter = validate_knowledge_frontmatter(value, path=path)
    source_bindings = tuple(
        ClaimSourceBindingValidation(
            source_id=str(source_id),
            valid=source_valid,
            reason_code=(
                "claim-source-registered"
                if source_valid
                else "claim-source-not-registered"
            ),
            detail="fixture",
        )
        for source_id in source_ids
    )
    references = tuple(
        ClaimEvidenceReferenceValidation(
            evidence_id=str(reference["evidence_id"]),
            stance=str(reference["stance"]),  # type: ignore[arg-type]
            source_id=str(source_ids[0]) if source_ids else None,
            current=evidence_current,
            reason_code="evidence-current" if evidence_current else "evidence-stale",
            detail="fixture",
            observed_excerpt_hash="a" * 64 if evidence_current else None,
        )
        for reference in refs
    )
    all_evidence_current = all(item.current for item in references)
    supporting_count = sum(
        item.current and item.stance == "supporting" for item in references
    )
    status = str(value["status"])
    verified_state_current = None
    if status == "verified":
        verified_state_current = bool(
            source_valid
            and all_evidence_current
            and (not key_claim or supporting_count > 0)
            and value["last_verified_at"] == value["updated_at"]
        )
    valid = source_valid and all_evidence_current
    if verified_state_current is not None:
        valid = valid and verified_state_current

    reason_codes: list[str] = []
    if not source_valid:
        reason_codes.append("claim-source-not-registered")
    if not evidence_current:
        reason_codes.append("evidence-stale")
    if status == "verified":
        if value["last_verified_at"] != value["updated_at"]:
            reason_codes.append("verified-claim-modified-after-verification")
        if key_claim and supporting_count == 0:
            reason_codes.append("verified-claim-missing-current-support")
        if not all_evidence_current:
            reason_codes.append("verified-claim-declares-noncurrent-evidence")
    if valid:
        reason_codes.append("claim-evidence-current")

    return ClaimEvidenceValidationResult(
        project_id=project_id or str(value["project_id"]),
        path=path,
        claim_frontmatter_sha256=claim_frontmatter_sha256(frontmatter),
        claim_status=status,
        key_claim=key_claim,
        valid=valid,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        source_bindings=source_bindings,
        evidence_references=references,
        all_evidence_current=all_evidence_current,
        current_supporting_evidence_count=supporting_count,
        verified_state_current=verified_state_current,
    )



class ClaimLifecycleFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.alpha = create_research_entity(
            PROJECT_ID,
            entity_type="claim",
            identity_key="claim:alpha",
            title="Shared conclusion",
            knowledge_path="claims/alpha.md",
        )
        self.beta = create_research_entity(
            PROJECT_ID,
            entity_type="claim",
            identity_key="claim:beta",
            title="Shared conclusion",
            knowledge_path="claims/beta.md",
        )
        self.result_alpha = create_research_entity(
            PROJECT_ID,
            entity_type="result",
            identity_key="result:alpha",
            title="Alpha result",
            knowledge_path="results/alpha.md",
        )
        self.result_beta = create_research_entity(
            PROJECT_ID,
            entity_type="result",
            identity_key="result:beta",
            title="Beta result",
            knowledge_path="results/beta.md",
        )
        self.unmaterialized_claim = create_research_entity(
            PROJECT_ID,
            entity_type="claim",
            identity_key="claim:unmaterialized",
            title="Unmaterialized claim",
        )
        self.unmaterialized_result = create_research_entity(
            PROJECT_ID,
            entity_type="result",
            identity_key="result:unmaterialized",
            title="Unmaterialized result",
        )
        self.entities = (
            self.alpha,
            self.beta,
            self.result_alpha,
            self.result_beta,
            self.unmaterialized_claim,
            self.unmaterialized_result,
        )
        self.registry = ResearchRelationRegistry(PROJECT_ID, self.entities)

    def conflict_pages(
        self,
        *,
        claim_status: str = "conflicting",
        project_id: str = PROJECT_ID,
        evidence_stance: str = "supporting",
    ) -> dict[str, bytes]:
        return {
            "claims/alpha.md": page_bytes(
                "claims/alpha.md",
                claim_frontmatter(
                    self.alpha,
                    status=claim_status,
                    project_id=project_id,
                    evidence_stance=evidence_stance,
                ),
            ),
            "claims/beta.md": page_bytes(
                "claims/beta.md",
                claim_frontmatter(
                    self.beta,
                    status=claim_status,
                    project_id=project_id,
                    evidence_stance=evidence_stance,
                ),
            ),
            "results/alpha.md": page_bytes(
                "results/alpha.md",
                artifact_frontmatter(
                    "results/alpha.md", self.result_alpha.title, project_id=project_id
                ),
            ),
            "results/beta.md": page_bytes(
                "results/beta.md",
                artifact_frontmatter(
                    "results/beta.md", self.result_beta.title, project_id=project_id
                ),
            ),
        }

    def conflict_variants(self) -> tuple[ClaimConflictVariant, ClaimConflictVariant]:
        return (
            ClaimConflictVariant(
                self.alpha.entity_id, (self.result_alpha.entity_id,)
            ),
            ClaimConflictVariant(self.beta.entity_id, (self.result_beta.entity_id,)),
        )

    def conflict_proof_for(
        self,
        target: dict[str, object],
        *,
        identity_key: str = "experiment:transition",
        variants: tuple[ClaimConflictVariant, ...] | None = None,
        registry: ResearchRelationRegistry | None = None,
        knowledge_pages: dict[str, bytes] | None = None,
    ) -> ClaimConflictCoexistenceValidation:
        active_registry = self.registry if registry is None else registry
        pages = self.conflict_pages() if knowledge_pages is None else dict(knowledge_pages)
        pages["claims/alpha.md"] = page_bytes("claims/alpha.md", target)
        return validate_claim_conflict_coexistence(
            active_registry,
            identity_key=identity_key,
            variants=self.conflict_variants() if variants is None else variants,
            knowledge_pages=pages,
        )


class ClaimTransitionTests(ClaimLifecycleFixture):
    def test_creation_is_bound_to_current_schema_v2_claim_entity(self) -> None:
        target = claim_frontmatter(self.alpha)
        result = validate_claim_transition(
            self.registry,
            claim_entity_id=self.alpha.entity_id,
            before=None,
            after=target,
        )
        self.assertEqual(result.transition_kind, "created")
        self.assertIsNone(result.from_status)
        self.assertEqual(result.to_status, "draft")
        self.assertEqual(result.knowledge_path, "claims/alpha.md")
        self.assertRegex(result.transition_id, r"^clt-[0-9a-f]{64}$")
        self.assertEqual(result.as_dict()["kind"], CLAIM_TRANSITION_KIND)
        self.assertEqual(
            result.as_dict()["schema_version"], CLAIM_LIFECYCLE_SCHEMA_VERSION
        )
        self.assertTrue(result.as_dict()["read_only"])
        self.assertFalse(result.as_dict()["status_persisted"])
        self.assertFalse(result.as_dict()["semantic_decision_made"])

    def test_every_host_declared_status_pair_is_structurally_composable(self) -> None:
        statuses = tuple(sorted(KNOWLEDGE_STATUSES))
        self.assertEqual(
            set(statuses), {"draft", "verified", "stale", "conflicting", "rejected"}
        )
        for before_status in statuses:
            for after_status in statuses:
                with self.subTest(before=before_status, after=after_status):
                    before = claim_frontmatter(
                        self.alpha,
                        status=before_status,
                        updated_at="2026-07-18T01:00:00Z",
                    )
                    after = claim_frontmatter(
                        self.alpha,
                        status=after_status,
                        updated_at="2026-07-18T02:00:00Z",
                        last_verified_at=(
                            ...
                            if after_status == "verified"
                            else before["last_verified_at"]
                        ),
                    )
                    currentness = (
                        currentness_proof(path="claims/alpha.md", value=after)
                        if after_status == "verified"
                        else None
                    )
                    conflict = (
                        self.conflict_proof_for(
                            after,
                            identity_key=(
                                f"experiment:status-pair:{before_status}:{after_status}"
                            ),
                        )
                        if after_status == "conflicting"
                        else None
                    )
                    result = validate_claim_transition(
                        self.registry,
                        claim_entity_id=self.alpha.entity_id,
                        before=before,
                        after=after,
                        currentness=currentness,
                        conflict_coexistence=conflict,
                    )
                    self.assertEqual(result.from_status, before_status)
                    self.assertEqual(result.to_status, after_status)
                    expected_kind = (
                        "status-changed"
                        if before_status != after_status
                        else "reverified"
                        if after_status == "verified"
                        else "updated"
                    )
                    self.assertEqual(result.transition_kind, expected_kind)
                    self.assertEqual(result.required_follow_up_validations, ())
                    self.assertTrue(result.as_dict()["validation_complete"])
                    if after_status == "conflicting":
                        assert conflict is not None
                        self.assertEqual(result.conflict_id, conflict.conflict_id)
                        self.assertRegex(
                            result.conflict_validation_sha256 or "", r"^[0-9a-f]{64}$"
                        )
                    else:
                        self.assertIsNone(result.conflict_id)
                        self.assertIsNone(result.conflict_validation_sha256)

    def test_identity_fields_are_immutable_and_entity_binding_is_exact(self) -> None:
        before = claim_frontmatter(self.alpha)
        generated_at_changed = claim_frontmatter(
            self.alpha,
            updated_at="2026-07-18T02:00:00Z",
            generated_at="2026-07-18T00:30:00Z",
        )
        with self.assertRaisesRegex(
            ClaimLifecycleTransitionError, "immutable identity fields"
        ):
            validate_claim_transition(
                self.registry,
                claim_entity_id=self.alpha.entity_id,
                before=before,
                after=generated_at_changed,
            )

        renamed_alpha = create_research_entity(
            PROJECT_ID,
            entity_type="claim",
            identity_key=self.alpha.identity_key,
            title="Renamed conclusion",
            knowledge_path=self.alpha.knowledge_path,
        )
        self.assertEqual(renamed_alpha.entity_id, self.alpha.entity_id)
        renamed_registry = ResearchRelationRegistry(
            PROJECT_ID,
            (renamed_alpha,)
            + tuple(entity for entity in self.entities if entity != self.alpha),
        )
        renamed = claim_frontmatter(
            renamed_alpha,
            updated_at="2026-07-18T02:00:00Z",
        )
        result = validate_claim_transition(
            renamed_registry,
            claim_entity_id=renamed_alpha.entity_id,
            before=before,
            after=renamed,
        )
        self.assertEqual(result.claim_entity_id, self.alpha.entity_id)
        self.assertEqual(result.knowledge_path, self.alpha.knowledge_path)
        self.assertIn("title", result.changed_fields)

        cases = (
            claim_frontmatter(
                renamed_alpha,
                updated_at="2026-07-18T02:00:00Z",
                title="Title not current in F-03",
            ),
            claim_frontmatter(
                renamed_alpha,
                updated_at="2026-07-18T02:00:00Z",
                project_id=OTHER_PROJECT_ID,
            ),
            {
                **claim_frontmatter(
                    renamed_alpha, updated_at="2026-07-18T02:00:00Z"
                ),
                "artifact_type": "paper",
            },
        )
        for after in cases:
            with self.subTest(after=after):
                with self.assertRaises(ClaimLifecycleBindingError):
                    validate_claim_transition(
                        renamed_registry,
                        claim_entity_id=renamed_alpha.entity_id,
                        before=before,
                        after=after,
                    )

    def test_conflicting_target_requires_exact_coexistence_proof(self) -> None:
        before = claim_frontmatter(self.alpha)
        target = claim_frontmatter(
            self.alpha,
            status="conflicting",
            updated_at="2026-07-18T02:00:00Z",
        )

        with self.assertRaisesRegex(
            ClaimLifecycleTransitionError, "requires a matching coexistence proof"
        ):
            validate_claim_transition(
                self.registry,
                claim_entity_id=self.alpha.entity_id,
                before=before,
                after=target,
            )

        gamma = create_research_entity(
            PROJECT_ID,
            entity_type="claim",
            identity_key="claim:gamma",
            title="Third conclusion",
            knowledge_path="claims/gamma.md",
        )
        expanded_registry = ResearchRelationRegistry(
            PROJECT_ID,
            self.entities + (gamma,),
        )
        missing_target_pages = self.conflict_pages()
        missing_target_pages["claims/gamma.md"] = page_bytes(
            "claims/gamma.md",
            claim_frontmatter(gamma, status="conflicting"),
        )
        missing_target_proof = validate_claim_conflict_coexistence(
            expanded_registry,
            identity_key="experiment:missing-target",
            variants=(
                ClaimConflictVariant(
                    self.beta.entity_id, (self.result_alpha.entity_id,)
                ),
                ClaimConflictVariant(gamma.entity_id, (self.result_beta.entity_id,)),
            ),
            knowledge_pages=missing_target_pages,
        )
        with self.assertRaisesRegex(
            ClaimLifecycleTransitionError, "does not include the target Claim"
        ):
            validate_claim_transition(
                expanded_registry,
                claim_entity_id=self.alpha.entity_id,
                before=before,
                after=target,
                conflict_coexistence=missing_target_proof,
            )

        old_revision_proof = self.conflict_proof_for(
            target,
            identity_key="experiment:old-target-revision",
        )
        revised_target = claim_frontmatter(
            self.alpha,
            status="conflicting",
            updated_at="2026-07-18T03:00:00Z",
        )
        with self.assertRaisesRegex(
            ClaimLifecycleTransitionError, "does not match the proposed Claim revision"
        ):
            validate_claim_transition(
                self.registry,
                claim_entity_id=self.alpha.entity_id,
                before=before,
                after=revised_target,
                conflict_coexistence=old_revision_proof,
            )

    def test_conflict_proof_hash_is_part_of_transition_identity(self) -> None:
        before = claim_frontmatter(self.alpha)
        target = claim_frontmatter(
            self.alpha,
            status="conflicting",
            updated_at="2026-07-18T02:00:00Z",
        )
        first_proof = self.conflict_proof_for(
            target,
            identity_key="experiment:proof-identity-a",
        )
        second_proof = self.conflict_proof_for(
            target,
            identity_key="experiment:proof-identity-b",
        )
        first = validate_claim_transition(
            self.registry,
            claim_entity_id=self.alpha.entity_id,
            before=before,
            after=target,
            conflict_coexistence=first_proof,
        )
        second = validate_claim_transition(
            self.registry,
            claim_entity_id=self.alpha.entity_id,
            before=before,
            after=target,
            conflict_coexistence=second_proof,
        )

        self.assertNotEqual(first.conflict_id, second.conflict_id)
        self.assertNotEqual(
            first.conflict_validation_sha256,
            second.conflict_validation_sha256,
        )
        self.assertNotEqual(first.transition_id, second.transition_id)

    def test_noop_or_nonadvancing_updated_at_fails_closed(self) -> None:
        before = claim_frontmatter(self.alpha)
        with self.assertRaisesRegex(ClaimLifecycleTransitionError, "identical no-op"):
            validate_claim_transition(
                self.registry,
                claim_entity_id=self.alpha.entity_id,
                before=before,
                after=copy.deepcopy(before),
            )
        for timestamp in ("2026-07-18T01:00:00Z", "2026-07-18T00:30:00Z"):
            after = claim_frontmatter(
                self.alpha, status="stale", updated_at=timestamp
            )
            with self.subTest(timestamp=timestamp):
                with self.assertRaisesRegex(
                    ClaimLifecycleTransitionError, "must advance strictly"
                ):
                    validate_claim_transition(
                        self.registry,
                        claim_entity_id=self.alpha.entity_id,
                        before=before,
                        after=after,
                    )

    def test_schema_v1_and_future_schema_fail_closed(self) -> None:
        before = claim_frontmatter(self.alpha)
        legacy = claim_frontmatter(
            self.alpha, updated_at="2026-07-18T02:00:00Z"
        )
        legacy["schema_version"] = 1
        legacy["evidence_ids"] = [EVIDENCE_ID]
        del legacy["evidence_refs"]
        future = claim_frontmatter(
            self.alpha, updated_at="2026-07-18T02:00:00Z"
        )
        future["schema_version"] = 3
        for after in (legacy, future):
            with self.subTest(schema_version=after["schema_version"]):
                with self.assertRaises(ClaimLifecycleBindingError):
                    validate_claim_transition(
                        self.registry,
                        claim_entity_id=self.alpha.entity_id,
                        before=before,
                        after=after,
                    )

    def test_unknown_nonclaim_and_unmaterialized_entities_are_rejected(self) -> None:
        target = claim_frontmatter(self.alpha)
        for entity_id in (
            UNKNOWN_ENTITY_ID,
            self.result_alpha.entity_id,
            self.unmaterialized_claim.entity_id,
        ):
            with self.subTest(entity_id=entity_id):
                with self.assertRaises(ClaimLifecycleBindingError):
                    validate_claim_transition(
                        self.registry,
                        claim_entity_id=entity_id,
                        before=None,
                        after=target,
                    )

    def test_verified_target_requires_matching_current_f02b_result(self) -> None:
        before = claim_frontmatter(self.alpha)
        target = claim_frontmatter(
            self.alpha,
            status="verified",
            updated_at="2026-07-18T02:00:00Z",
        )
        proof = currentness_proof(path="claims/alpha.md", value=target)
        result = validate_claim_transition(
            self.registry,
            claim_entity_id=self.alpha.entity_id,
            before=before,
            after=target,
            currentness=proof,
        )
        self.assertTrue(result.evidence_bindings_current)
        self.assertTrue(result.verified_state_current)
        self.assertRegex(result.evidence_validation_sha256 or "", r"^[0-9a-f]{64}$")
        self.assertNotIn(
            "claim-evidence-currentness", result.required_follow_up_validations
        )

        with self.assertRaisesRegex(
            ClaimLifecycleTransitionError, "matching current F-02B result"
        ):
            validate_claim_transition(
                self.registry,
                claim_entity_id=self.alpha.entity_id,
                before=before,
                after=target,
            )

        stale = currentness_proof(
            path="claims/alpha.md",
            value=target,
            source_valid=False,
            evidence_current=False,
        )
        with self.assertRaisesRegex(
            ClaimLifecycleTransitionError, "matching current F-02B result"
        ):
            validate_claim_transition(
                self.registry,
                claim_entity_id=self.alpha.entity_id,
                before=before,
                after=target,
                currentness=stale,
            )

    def test_f02b_proof_cannot_be_replayed_after_claim_revision_changes(self) -> None:
        before = claim_frontmatter(self.alpha)
        target = claim_frontmatter(
            self.alpha,
            status="verified",
            updated_at="2026-07-18T02:00:00Z",
        )
        proof = currentness_proof(path="claims/alpha.md", value=target)

        changed_updated_at = claim_frontmatter(
            self.alpha,
            status="verified",
            updated_at="2026-07-18T03:00:00Z",
        )
        changed_ownership = dict(target)
        changed_ownership["ownership"] = "mixed"
        for revised_target in (changed_updated_at, changed_ownership):
            with self.subTest(revision=revised_target):
                with self.assertRaisesRegex(
                    ClaimLifecycleTransitionError, "exact Claim frontmatter revision"
                ):
                    validate_claim_transition(
                        self.registry,
                        claim_entity_id=self.alpha.entity_id,
                        before=before,
                        after=revised_target,
                        currentness=proof,
                    )

        inconsistent_reference = replace(
            proof.evidence_references[0],
            observed_excerpt_hash=None,
        )
        inconsistent_proof = replace(
            proof,
            evidence_references=(inconsistent_reference,),
        )
        with self.assertRaisesRegex(
            ClaimLifecycleTransitionError, "complete current Source/excerpt result"
        ):
            validate_claim_transition(
                self.registry,
                claim_entity_id=self.alpha.entity_id,
                before=before,
                after=target,
                currentness=inconsistent_proof,
            )

    def test_real_f02b_result_authorizes_verified_transition_for_utf8_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            project = root / "research-project"
            source_path = project / "notes" / "claim.txt"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(
                "alpha = 1\nbeta = 2\n",
                encoding="utf-8",
                newline="\n",
            )
            registration = register_project(
                workspace,
                project,
                project_id=PROJECT_ID,
            )
            inventory_project(workspace, registration.project_id)
            sync_source_registry(workspace, registration.project_id)
            source = load_source_registry(
                workspace,
                registration.project_id,
            ).current_by_path["notes/claim.txt"]
            evidence = register_evidence(
                workspace,
                registration.project_id,
                source_id=source.source_id,
                content_hash=source.current_content_hash,
                locator=LineRangeLocator(1, 1),
                excerpt="alpha = 1\n",
            ).evidence

            before = claim_frontmatter(
                self.alpha,
                source_ids=(source.source_id,),
                evidence_id=evidence.evidence_id,
            )
            target = claim_frontmatter(
                self.alpha,
                status="verified",
                updated_at="2026-07-18T02:00:00Z",
                source_ids=(source.source_id,),
                evidence_id=evidence.evidence_id,
            )
            proof = validate_claim_evidence(
                workspace,
                registration.project_id,
                path="claims/alpha.md",
                payload=target,
            )
            result = validate_claim_transition(
                self.registry,
                claim_entity_id=self.alpha.entity_id,
                before=before,
                after=target,
                currentness=proof,
            )

        self.assertTrue(proof.valid)
        self.assertTrue(proof.verified_state_current)
        self.assertEqual(result.to_status, "verified")
        self.assertTrue(result.evidence_bindings_current)
        self.assertTrue(result.verified_state_current)
        self.assertRegex(result.evidence_validation_sha256 or "", r"^[0-9a-f]{64}$")

    def test_verified_timestamp_must_match_target_update(self) -> None:
        before = claim_frontmatter(self.alpha)
        target = claim_frontmatter(
            self.alpha,
            status="verified",
            updated_at="2026-07-18T02:00:00Z",
            last_verified_at="2026-07-18T01:30:00Z",
        )
        proof = currentness_proof(path="claims/alpha.md", value=target)
        with self.assertRaisesRegex(
            ClaimLifecycleTransitionError, "last_verified_at == updated_at"
        ):
            validate_claim_transition(
                self.registry,
                claim_entity_id=self.alpha.entity_id,
                before=before,
                after=target,
                currentness=proof,
            )

    def test_nonverified_transition_preserves_verification_history(self) -> None:
        before = claim_frontmatter(
            self.alpha,
            status="verified",
            updated_at="2026-07-18T01:00:00Z",
        )
        preserved = claim_frontmatter(
            self.alpha,
            status="stale",
            updated_at="2026-07-18T02:00:00Z",
            last_verified_at="2026-07-18T01:00:00Z",
        )
        result = validate_claim_transition(
            self.registry,
            claim_entity_id=self.alpha.entity_id,
            before=before,
            after=preserved,
        )
        self.assertEqual(result.from_last_verified_at, "2026-07-18T01:00:00Z")
        self.assertEqual(result.to_last_verified_at, "2026-07-18T01:00:00Z")

        erased = dict(preserved)
        erased["last_verified_at"] = None
        invented = claim_frontmatter(
            self.alpha,
            status="stale",
            updated_at="2026-07-18T02:00:00Z",
            last_verified_at="2026-07-18T01:30:00Z",
        )
        for target in (erased, invented):
            with self.subTest(last_verified_at=target["last_verified_at"]):
                with self.assertRaisesRegex(
                    ClaimLifecycleTransitionError,
                    "must preserve last_verified_at verification history",
                ):
                    validate_claim_transition(
                        self.registry,
                        claim_entity_id=self.alpha.entity_id,
                        before=before,
                        after=target,
                    )

        never_verified = claim_frontmatter(self.alpha)
        invented_history = claim_frontmatter(
            self.alpha,
            status="stale",
            updated_at="2026-07-18T02:00:00Z",
            last_verified_at="2026-07-18T01:30:00Z",
        )
        with self.assertRaisesRegex(
            ClaimLifecycleTransitionError,
            "must preserve last_verified_at verification history",
        ):
            validate_claim_transition(
                self.registry,
                claim_entity_id=self.alpha.entity_id,
                before=never_verified,
                after=invented_history,
            )

    def test_mismatched_or_internally_inconsistent_currentness_is_rejected(self) -> None:
        before = claim_frontmatter(self.alpha)
        target = claim_frontmatter(
            self.alpha,
            status="verified",
            updated_at="2026-07-18T02:00:00Z",
        )
        valid = currentness_proof(path="claims/alpha.md", value=target)
        wrong_status = replace(
            valid,
            claim_status="stale",
            verified_state_current=None,
        )
        wrong_source = replace(
            valid,
            source_bindings=(
                ClaimSourceBindingValidation(
                    OTHER_SOURCE_ID, True, "claim-source-registered", "fixture"
                ),
            ),
        )
        wrong_evidence = replace(
            valid,
            evidence_references=(
                replace(valid.evidence_references[0], evidence_id=OTHER_EVIDENCE_ID),
            ),
        )
        cases = (
            replace(valid, project_id=OTHER_PROJECT_ID),
            replace(valid, path="claims/beta.md"),
            wrong_status,
            replace(valid, key_claim=False),
            wrong_source,
            wrong_evidence,
            replace(valid, all_evidence_current=False),
            replace(valid, current_supporting_evidence_count=0),
            replace(valid, verified_state_current=False, valid=False),
            replace(valid, valid=False),
        )
        for proof in cases:
            with self.subTest(proof=proof):
                with self.assertRaises(ClaimLifecycleTransitionError):
                    validate_claim_transition(
                        self.registry,
                        claim_entity_id=self.alpha.entity_id,
                        before=before,
                        after=target,
                        currentness=proof,
                    )

    def test_nonverified_currentness_remains_an_independent_dimension(self) -> None:
        before = claim_frontmatter(self.alpha)
        stale_target = claim_frontmatter(
            self.alpha,
            status="stale",
            updated_at="2026-07-18T02:00:00Z",
        )
        stale_proof = currentness_proof(
            path="claims/alpha.md", value=stale_target, evidence_current=False
        )
        result = validate_claim_transition(
            self.registry,
            claim_entity_id=self.alpha.entity_id,
            before=before,
            after=stale_target,
            currentness=stale_proof,
        )
        self.assertEqual(result.to_status, "stale")
        self.assertFalse(result.evidence_bindings_current)
        self.assertIsNone(result.verified_state_current)
        self.assertEqual(result.required_follow_up_validations, ())

    def test_transition_is_deterministic_and_preserves_inputs(self) -> None:
        before = claim_frontmatter(self.alpha)
        after = claim_frontmatter(
            self.alpha, status="rejected", updated_at="2026-07-18T02:00:00Z"
        )
        original_before = copy.deepcopy(before)
        original_after = copy.deepcopy(after)
        first = validate_claim_transition(
            self.registry,
            claim_entity_id=self.alpha.entity_id,
            before=before,
            after=after,
        )
        second = validate_claim_transition(
            self.registry,
            claim_entity_id=self.alpha.entity_id,
            before=before,
            after=after,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first.as_dict(), ensure_ascii=False, sort_keys=True),
            json.dumps(second.as_dict(), ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual(before, original_before)
        self.assertEqual(after, original_after)


class ClaimConflictTests(ClaimLifecycleFixture):
    def test_explicit_conflict_keeps_two_claims_and_two_results_without_winner(self) -> None:
        result = validate_claim_conflict_coexistence(
            self.registry,
            identity_key="experiment:opposite-results",
            variants=self.conflict_variants(),
            knowledge_pages=self.conflict_pages(),
        )
        rendered = result.as_dict()
        self.assertEqual(rendered["kind"], CLAIM_CONFLICT_KIND)
        self.assertEqual(rendered["status"], "conflicting")
        self.assertEqual(rendered["claim_count"], 2)
        self.assertEqual(rendered["result_count"], 2)
        self.assertEqual(
            {variant.claim_entity_id for variant in result.variants},
            {self.alpha.entity_id, self.beta.entity_id},
        )
        self.assertEqual(
            {
                self.registry.by_entity_id[item.claim_entity_id].title
                for item in result.variants
            },
            {"Shared conclusion"},
        )
        self.assertNotIn("winner_entity_id", rendered)
        self.assertNotIn("replacement_entity_id", rendered)
        self.assertTrue(rendered["read_only"])
        self.assertFalse(rendered["status_persisted"])
        self.assertFalse(rendered["semantic_conflict_inferred"])

    def test_conflict_identity_is_explicit_project_key_not_member_order(self) -> None:
        expected = claim_conflict_id_for(PROJECT_ID, "experiment:opposite-results")
        forward = validate_claim_conflict_coexistence(
            self.registry,
            identity_key="experiment:opposite-results",
            variants=self.conflict_variants(),
            knowledge_pages=self.conflict_pages(),
        )
        reverse = validate_claim_conflict_coexistence(
            self.registry,
            identity_key="experiment:opposite-results",
            variants=tuple(reversed(self.conflict_variants())),
            knowledge_pages=self.conflict_pages(),
        )
        self.assertEqual(forward.conflict_id, expected)
        self.assertEqual(forward, reverse)
        self.assertNotEqual(
            expected, claim_conflict_id_for(PROJECT_ID, "experiment:different")
        )
        self.assertNotEqual(
            expected,
            claim_conflict_id_for(OTHER_PROJECT_ID, "experiment:opposite-results"),
        )

    def test_conflict_membership_requires_distinct_claims_and_two_results(self) -> None:
        alpha = ClaimConflictVariant(
            self.alpha.entity_id, (self.result_alpha.entity_id,)
        )
        cases = (
            (alpha,),
            (alpha, alpha),
            (
                alpha,
                ClaimConflictVariant(
                    self.beta.entity_id, (self.result_alpha.entity_id,)
                ),
            ),
        )
        for variants in cases:
            with self.subTest(variants=variants):
                with self.assertRaises(ClaimConflictCoexistenceError):
                    validate_claim_conflict_coexistence(
                        self.registry,
                        identity_key="experiment:invalid",
                        variants=variants,
                        knowledge_pages=self.conflict_pages(),
                    )
        with self.assertRaises(ClaimConflictCoexistenceError):
            ClaimConflictVariant(
                self.alpha.entity_id,
                (self.result_alpha.entity_id, self.result_alpha.entity_id),
            )
        with self.assertRaises(ClaimConflictCoexistenceError):
            ClaimConflictVariant(self.alpha.entity_id, ())

    def test_result_entities_may_be_shared_by_competing_interpretations(self) -> None:
        variants = (
            ClaimConflictVariant(
                self.alpha.entity_id,
                (self.result_alpha.entity_id, self.result_beta.entity_id),
            ),
            ClaimConflictVariant(
                self.beta.entity_id,
                (self.result_alpha.entity_id, self.result_beta.entity_id),
            ),
        )
        target = claim_frontmatter(
            self.alpha,
            status="conflicting",
            updated_at="2026-07-18T02:00:00Z",
        )
        pages = self.conflict_pages()
        pages["claims/alpha.md"] = page_bytes("claims/alpha.md", target)
        result = validate_claim_conflict_coexistence(
            self.registry,
            identity_key="experiment:shared-results",
            variants=variants,
            knowledge_pages=pages,
        )
        self.assertEqual(result.claim_count, 2)
        self.assertEqual(result.result_count, 2)
        self.assertEqual(
            {
                binding.result_entity_id
                for variant in result.variants
                for binding in variant.results
            },
            {self.result_alpha.entity_id, self.result_beta.entity_id},
        )

        transition = validate_claim_transition(
            self.registry,
            claim_entity_id=self.alpha.entity_id,
            before=claim_frontmatter(self.alpha),
            after=target,
            conflict_coexistence=result,
        )
        self.assertEqual(transition.conflict_id, result.conflict_id)
        self.assertEqual(transition.required_follow_up_validations, ())

    def test_result_pages_need_not_carry_conflicting_status(self) -> None:
        pages = self.conflict_pages()
        parsed_result_statuses = {
            path: yaml.safe_load(payload.decode("utf-8").split("---", 2)[1])[
                "status"
            ]
            for path, payload in pages.items()
            if path.startswith("results/")
        }
        self.assertEqual(set(parsed_result_statuses.values()), {"draft"})
        result = validate_claim_conflict_coexistence(
            self.registry,
            identity_key="experiment:result-status-independent",
            variants=self.conflict_variants(),
            knowledge_pages=pages,
        )
        self.assertEqual(result.result_count, 2)

    def test_unknown_wrong_type_and_unmaterialized_entities_are_rejected(self) -> None:
        pages = self.conflict_pages()
        cases = (
            (
                ClaimConflictVariant(UNKNOWN_ENTITY_ID, (self.result_alpha.entity_id,)),
                ClaimConflictVariant(self.beta.entity_id, (self.result_beta.entity_id,)),
            ),
            (
                ClaimConflictVariant(
                    self.result_alpha.entity_id, (self.result_beta.entity_id,)
                ),
                ClaimConflictVariant(self.beta.entity_id, (self.result_alpha.entity_id,)),
            ),
            (
                ClaimConflictVariant(self.alpha.entity_id, (UNKNOWN_ENTITY_ID,)),
                ClaimConflictVariant(self.beta.entity_id, (self.result_beta.entity_id,)),
            ),
            (
                ClaimConflictVariant(self.alpha.entity_id, (self.beta.entity_id,)),
                ClaimConflictVariant(self.beta.entity_id, (self.result_beta.entity_id,)),
            ),
            (
                ClaimConflictVariant(
                    self.unmaterialized_claim.entity_id,
                    (self.result_alpha.entity_id,),
                ),
                ClaimConflictVariant(self.beta.entity_id, (self.result_beta.entity_id,)),
            ),
            (
                ClaimConflictVariant(
                    self.alpha.entity_id, (self.unmaterialized_result.entity_id,)
                ),
                ClaimConflictVariant(self.beta.entity_id, (self.result_beta.entity_id,)),
            ),
        )
        for variants in cases:
            with self.subTest(variants=variants):
                with self.assertRaises(ClaimLifecycleBindingError):
                    validate_claim_conflict_coexistence(
                        self.registry,
                        identity_key="experiment:bad-binding",
                        variants=variants,
                        knowledge_pages=pages,
                    )

    def test_page_schema_project_title_type_and_presence_bindings_fail_closed(self) -> None:
        base = self.conflict_pages()
        invalid_cases: list[dict[str, object]] = []

        missing = dict(base)
        del missing["claims/alpha.md"]
        invalid_cases.append(missing)

        nonbytes: dict[str, object] = dict(base)
        nonbytes["claims/alpha.md"] = "not bytes"
        invalid_cases.append(nonbytes)

        wrong_project = dict(base)
        wrong_project["claims/alpha.md"] = raw_page_bytes(
            claim_frontmatter(
                self.alpha, status="conflicting", project_id=OTHER_PROJECT_ID
            )
        )
        invalid_cases.append(wrong_project)

        wrong_title = dict(base)
        wrong_title["claims/alpha.md"] = page_bytes(
            "claims/alpha.md",
            claim_frontmatter(
                self.alpha, status="conflicting", title="Different title"
            ),
        )
        invalid_cases.append(wrong_title)

        wrong_type = dict(base)
        type_payload = claim_frontmatter(self.alpha, status="conflicting")
        type_payload["artifact_type"] = "paper"
        wrong_type["claims/alpha.md"] = raw_page_bytes(type_payload)
        invalid_cases.append(wrong_type)

        legacy = dict(base)
        v1 = claim_frontmatter(self.alpha, status="conflicting")
        v1["schema_version"] = 1
        v1["evidence_ids"] = [EVIDENCE_ID]
        del v1["evidence_refs"]
        legacy["claims/alpha.md"] = raw_page_bytes(v1)
        invalid_cases.append(legacy)

        future = dict(base)
        v3 = claim_frontmatter(self.alpha, status="conflicting")
        v3["schema_version"] = 3
        future["claims/alpha.md"] = raw_page_bytes(v3)
        invalid_cases.append(future)

        for knowledge_pages in invalid_cases:
            with self.subTest(paths=tuple(knowledge_pages)):
                with self.assertRaises(ClaimLifecycleBindingError):
                    validate_claim_conflict_coexistence(
                        self.registry,
                        identity_key="experiment:bad-page",
                        variants=self.conflict_variants(),
                        knowledge_pages=knowledge_pages,  # type: ignore[arg-type]
                    )

    def test_nonconflicting_pages_are_not_promoted_by_relation_or_opposing_evidence(self) -> None:
        relation = create_research_relation(
            PROJECT_ID,
            relation_type="conflicts-with",
            source_entity_id=self.alpha.entity_id,
            target_entity_id=self.beta.entity_id,
            evidence_ids=(EVIDENCE_ID,),
        )
        registry = ResearchRelationRegistry(PROJECT_ID, self.entities, (relation,))
        pages = self.conflict_pages(
            claim_status="draft", evidence_stance="opposing"
        )
        with self.assertRaisesRegex(
            ClaimConflictCoexistenceError, "explicitly use status 'conflicting'"
        ):
            validate_claim_conflict_coexistence(
                registry,
                identity_key="experiment:not-inferred",
                variants=self.conflict_variants(),
                knowledge_pages=pages,
            )

        before = claim_frontmatter(self.alpha, evidence_stance="opposing")
        after = claim_frontmatter(
            self.alpha,
            evidence_stance="opposing",
            updated_at="2026-07-18T02:00:00Z",
        )
        transition = validate_claim_transition(
            registry,
            claim_entity_id=self.alpha.entity_id,
            before=before,
            after=after,
        )
        self.assertEqual(transition.to_status, "draft")
        self.assertEqual(transition.required_follow_up_validations, ())

    def test_conflict_validation_is_deterministic_input_preserving_and_in_memory(self) -> None:
        pages = self.conflict_pages()
        variants = self.conflict_variants()
        original_pages = copy.deepcopy(pages)
        with (
            mock.patch("builtins.open", side_effect=AssertionError("filesystem open")),
            mock.patch.object(
                Path, "read_bytes", side_effect=AssertionError("filesystem read")
            ),
            mock.patch(
                "tools.claim_evidence.open_source",
                side_effect=AssertionError("Source access"),
            ),
        ):
            first = validate_claim_conflict_coexistence(
                self.registry,
                identity_key="experiment:in-memory",
                variants=variants,
                knowledge_pages=pages,
            )
            second = validate_claim_conflict_coexistence(
                self.registry,
                identity_key="experiment:in-memory",
                variants=tuple(reversed(variants)),
                knowledge_pages=pages,
            )
        self.assertEqual(first, second)
        self.assertEqual(pages, original_pages)
        self.assertEqual(
            json.dumps(first.as_dict(), ensure_ascii=False, sort_keys=True),
            json.dumps(second.as_dict(), ensure_ascii=False, sort_keys=True),
        )


if __name__ == "__main__":
    unittest.main()
