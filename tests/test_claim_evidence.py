from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tools.claim_evidence import (
    CLAIM_EVIDENCE_VALIDATION_KIND,
    CLAIM_EVIDENCE_VALIDATION_VERSION,
    ClaimEvidenceResultIntegrityError,
    claim_frontmatter_sha256,
    validate_claim_evidence,
    validate_claim_evidence_result_integrity,
)
from tools.evidence_registry import register_evidence
from tools.extraction_schema import LineRangeLocator
from tools.knowledge_artifacts import (
    KNOWLEDGE_PAGE_KIND,
    KNOWLEDGE_SCHEMA_VERSION,
    KnowledgeFrontmatterError,
    UnsupportedKnowledgeSchemaVersionError,
    validate_knowledge_frontmatter,
)
from tools.project_inventory import inventory_project
from tools.project_layout import CURRENT_SCHEMA_VERSION
from tools.project_registry import register_project
from tools.source_recovery import SourceRecoveryError
from tools.source_registry import load_source_registry, sync_source_registry


class ClaimEvidenceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.project = self.root / "research-project"
        self.primary_path = self.project / "docs" / "primary.txt"
        self.secondary_path = self.project / "docs" / "secondary.txt"
        self.primary_path.parent.mkdir(parents=True)
        self.primary_path.write_text(
            "alpha = 1\nbeta = 2\ngamma = alpha + beta\n",
            encoding="utf-8",
            newline="\n",
        )
        self.secondary_path.write_text(
            "delta = 4\nepsilon = 5\n",
            encoding="utf-8",
            newline="\n",
        )
        self.registration = register_project(
            self.workspace,
            self.project,
            project_id="claim-evidence-study",
        )
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)
        self.refresh_sources()

    def refresh_sources(self) -> None:
        registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        self.primary = registry.current_by_path["docs/primary.txt"]
        self.secondary = registry.current_by_path["docs/secondary.txt"]

    def register_text_evidence(
        self,
        *,
        source_id: str | None = None,
        content_hash: str | None = None,
        line: int = 1,
        excerpt: str = "alpha = 1\n",
    ):
        source = self.primary if source_id is None else load_source_registry(
            self.workspace,
            self.registration.project_id,
        ).by_source_id[source_id]
        return register_evidence(
            self.workspace,
            self.registration.project_id,
            source_id=source.source_id,
            content_hash=content_hash or source.current_content_hash,
            locator=LineRangeLocator(line, line),
            excerpt=excerpt,
        ).evidence

    def payload(
        self,
        *,
        source_ids: list[str] | None = None,
        evidence_refs: list[dict[str, str]] | None = None,
        status: str = "verified",
        project_id: str | None = None,
        updated_at: str = "2026-07-18T06:30:00Z",
        last_verified_at: str | None = "2026-07-18T06:30:00Z",
        schema_version: int = KNOWLEDGE_SCHEMA_VERSION,
    ) -> dict[str, object]:
        return {
            "schema_version": schema_version,
            "kind": KNOWLEDGE_PAGE_KIND,
            "project_id": project_id or self.registration.project_id,
            "artifact_type": "claim",
            "title": "Primary claim",
            "status": status,
            "ownership": "generated",
            "source_ids": source_ids if source_ids is not None else [self.primary.source_id],
            "evidence_refs": evidence_refs if evidence_refs is not None else [],
            "generated_at": "2026-07-18T06:00:00Z",
            "updated_at": updated_at,
            "last_verified_at": last_verified_at,
        }

    @staticmethod
    def ref(evidence_id: str, stance: str = "supporting") -> dict[str, str]:
        return {"evidence_id": evidence_id, "stance": stance}

    def validate(self, payload: dict[str, object], *, path: str = "claims/primary.md"):
        return validate_claim_evidence(
            self.workspace,
            self.registration.project_id,
            path=path,
            payload=payload,
        )

    @staticmethod
    def tree_snapshot(root: Path) -> dict[str, tuple[str, int]]:
        snapshot: dict[str, tuple[str, int]] = {}
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name != ".git")
            for filename in sorted(filenames):
                path = Path(directory) / filename
                if path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                snapshot[relative] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mtime_ns,
                )
        return snapshot

    def project_snapshot(self) -> dict[str, tuple[str, int]]:
        return self.tree_snapshot(self.project)

    def workspace_snapshot(self) -> dict[str, tuple[str, int]]:
        return self.tree_snapshot(self.workspace)

    def test_verified_claim_with_current_directional_evidence_is_valid(self) -> None:
        supporting = self.register_text_evidence()
        opposing = self.register_text_evidence(line=2, excerpt="beta = 2\n")
        context = self.register_text_evidence(
            source_id=self.secondary.source_id,
            line=1,
            excerpt="delta = 4\n",
        )
        claim_payload = self.payload(
            source_ids=[self.primary.source_id, self.secondary.source_id],
            evidence_refs=[
                self.ref(supporting.evidence_id),
                self.ref(opposing.evidence_id, "opposing"),
                self.ref(context.evidence_id, "context"),
            ],
        )
        result = self.validate(claim_payload)
        frontmatter = validate_knowledge_frontmatter(
            claim_payload,
            path="claims/primary.md",
        )

        self.assertTrue(result.valid)
        self.assertTrue(result.all_evidence_current)
        self.assertTrue(result.verified_state_current)
        self.assertEqual(result.current_supporting_evidence_count, 1)
        self.assertEqual(result.reason_codes, ("claim-evidence-current",))
        self.assertTrue(all(item.current for item in result.evidence_references))
        self.assertEqual(
            result.claim_frontmatter_sha256,
            claim_frontmatter_sha256(frontmatter),
        )
        payload = result.as_dict()
        self.assertEqual(payload["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(payload["kind"], CLAIM_EVIDENCE_VALIDATION_KIND)
        self.assertEqual(CLAIM_EVIDENCE_VALIDATION_VERSION, "claim-evidence-validation-v2")
        self.assertEqual(
            payload["validation_version"],
            CLAIM_EVIDENCE_VALIDATION_VERSION,
        )
        self.assertEqual(
            payload["claim_frontmatter_sha256"],
            result.claim_frontmatter_sha256,
        )
        self.assertTrue(payload["read_only"])
        validate_claim_evidence_result_integrity(
            result,
            path="claims/primary.md",
            frontmatter=frontmatter,
        )

    def test_result_integrity_binds_exact_claim_revision_and_current_fields(self) -> None:
        evidence = self.register_text_evidence()
        claim_payload = self.payload(
            evidence_refs=[self.ref(evidence.evidence_id)],
        )
        result = self.validate(claim_payload)
        frontmatter = validate_knowledge_frontmatter(
            claim_payload,
            path="claims/primary.md",
        )
        with patch(
            "tools.claim_evidence.open_source",
            side_effect=AssertionError("integrity validation must not reopen Source"),
        ):
            validate_claim_evidence_result_integrity(
                result,
                path="claims/primary.md",
                frontmatter=frontmatter,
            )

        invalid_frontmatter = replace(frontmatter, title="")
        with self.assertRaisesRegex(
            ClaimEvidenceResultIntegrityError,
            "strict normalized Knowledge Schema v2 frontmatter",
        ):
            validate_claim_evidence_result_integrity(
                result,
                path="claims/primary.md",
                frontmatter=invalid_frontmatter,
            )

        changed_updated_at = dict(claim_payload)
        changed_updated_at["updated_at"] = "2026-07-18T07:00:00Z"
        changed_updated_at["last_verified_at"] = "2026-07-18T07:00:00Z"
        changed_ownership = dict(claim_payload)
        changed_ownership["ownership"] = "mixed"
        for changed in (changed_updated_at, changed_ownership):
            revised_frontmatter = validate_knowledge_frontmatter(
                changed,
                path="claims/primary.md",
            )
            with self.subTest(revision=changed):
                with self.assertRaisesRegex(
                    ClaimEvidenceResultIntegrityError,
                    "exact Claim frontmatter revision",
                ):
                    validate_claim_evidence_result_integrity(
                        result,
                        path="claims/primary.md",
                        frontmatter=revised_frontmatter,
                    )

        inconsistent_result = replace(
            result,
            evidence_references=(
                replace(
                    result.evidence_references[0],
                    observed_excerpt_hash=None,
                ),
            ),
        )
        with self.assertRaisesRegex(
            ClaimEvidenceResultIntegrityError,
            "complete current Source/excerpt result",
        ):
            validate_claim_evidence_result_integrity(
                inconsistent_result,
                path="claims/primary.md",
                frontmatter=frontmatter,
            )

    def test_missing_evidence_and_unregistered_claim_source_fail_closed(self) -> None:
        missing_id = "evd-" + "f" * 64
        unknown_source = "src-" + "f" * 32
        result = self.validate(
            self.payload(
                source_ids=[self.primary.source_id, unknown_source],
                evidence_refs=[self.ref(missing_id)],
            )
        )

        self.assertFalse(result.valid)
        self.assertFalse(result.verified_state_current)
        self.assertIn("claim-source-not-registered", result.reason_codes)
        self.assertIn("claim-evidence-not-registered", result.reason_codes)
        self.assertIn(
            "verified-claim-missing-current-support",
            result.reason_codes,
        )
        self.assertIn(
            "verified-claim-declares-noncurrent-evidence",
            result.reason_codes,
        )

    def test_evidence_must_belong_to_a_claim_declared_source(self) -> None:
        evidence = self.register_text_evidence()
        result = self.validate(
            self.payload(
                source_ids=[self.secondary.source_id],
                evidence_refs=[self.ref(evidence.evidence_id)],
            )
        )

        self.assertFalse(result.valid)
        self.assertEqual(
            result.evidence_references[0].reason_code,
            "evidence-source-not-declared",
        )

    def test_claim_project_binding_is_checked_before_evidence_currentness(self) -> None:
        evidence = self.register_text_evidence()
        result = self.validate(
            self.payload(
                project_id="different-claim-study",
                evidence_refs=[self.ref(evidence.evidence_id)],
            )
        )

        self.assertFalse(result.valid)
        self.assertEqual(result.reason_codes, ("claim-project-mismatch",))
        self.assertEqual(result.source_bindings, ())
        self.assertEqual(result.evidence_references, ())

    def test_stale_source_version_invalidates_evidence(self) -> None:
        evidence = self.register_text_evidence()
        self.primary_path.write_text(
            "alpha = 10\nbeta = 20\n",
            encoding="utf-8",
            newline="\n",
        )
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)

        result = self.validate(
            self.payload(evidence_refs=[self.ref(evidence.evidence_id)])
        )

        self.assertFalse(result.valid)
        self.assertEqual(
            result.evidence_references[0].reason_code,
            "current-content-hash-mismatch",
        )
        self.assertFalse(result.verified_state_current)

    def test_locator_and_exact_excerpt_are_reopened(self) -> None:
        invalid_locator = self.register_text_evidence(
            line=99,
            excerpt="not present\n",
        )
        locator_result = self.validate(
            self.payload(evidence_refs=[self.ref(invalid_locator.evidence_id)])
        )
        self.assertEqual(
            locator_result.evidence_references[0].reason_code,
            "source-locator-invalid",
        )

        wrong_excerpt = self.register_text_evidence(
            line=2,
            excerpt="wrong excerpt\n",
        )
        excerpt_result = self.validate(
            self.payload(evidence_refs=[self.ref(wrong_excerpt.evidence_id)])
        )
        self.assertEqual(
            excerpt_result.evidence_references[0].reason_code,
            "source-excerpt-hash-mismatch",
        )

    def test_verified_claim_needs_current_supporting_evidence(self) -> None:
        stale_support = self.register_text_evidence()
        self.primary_path.write_text(
            "alpha = 9\nbeta = 2\ngamma = 11\n",
            encoding="utf-8",
            newline="\n",
        )
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)

        result = self.validate(
            self.payload(evidence_refs=[self.ref(stale_support.evidence_id)])
        )

        self.assertEqual(result.current_supporting_evidence_count, 0)
        self.assertIn(
            "verified-claim-missing-current-support",
            result.reason_codes,
        )
        self.assertFalse(result.verified_state_current)

    def test_any_stale_declared_evidence_invalidates_verified_claim(self) -> None:
        current_support = self.register_text_evidence()
        stale_context = self.register_text_evidence(
            source_id=self.secondary.source_id,
            line=1,
            excerpt="delta = 4\n",
        )
        self.secondary_path.write_text(
            "delta = 40\nepsilon = 50\n",
            encoding="utf-8",
            newline="\n",
        )
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)

        result = self.validate(
            self.payload(
                source_ids=[self.primary.source_id, self.secondary.source_id],
                evidence_refs=[
                    self.ref(current_support.evidence_id),
                    self.ref(stale_context.evidence_id, "context"),
                ],
            )
        )

        self.assertEqual(result.current_supporting_evidence_count, 1)
        self.assertFalse(result.all_evidence_current)
        self.assertIn(
            "verified-claim-declares-noncurrent-evidence",
            result.reason_codes,
        )
        self.assertFalse(result.verified_state_current)

    def test_modification_after_verification_invalidates_verified_state(self) -> None:
        evidence = self.register_text_evidence()
        result = self.validate(
            self.payload(
                evidence_refs=[self.ref(evidence.evidence_id)],
                updated_at="2026-07-18T06:30:00Z",
                last_verified_at="2026-07-18T06:15:00Z",
            )
        )

        self.assertTrue(result.all_evidence_current)
        self.assertFalse(result.valid)
        self.assertFalse(result.verified_state_current)
        self.assertIn(
            "verified-claim-modified-after-verification",
            result.reason_codes,
        )

    def test_verified_claim_collection_index_does_not_invent_a_key_claim(self) -> None:
        result = self.validate(
            self.payload(evidence_refs=[]),
            path="claims/index.md",
        )

        self.assertTrue(result.valid)
        self.assertFalse(result.key_claim)
        self.assertTrue(result.verified_state_current)
        self.assertEqual(result.current_supporting_evidence_count, 0)
        self.assertNotIn(
            "verified-claim-missing-current-support",
            result.reason_codes,
        )

    def test_non_claim_artifacts_are_rejected(self) -> None:
        payload = self.payload(status="draft", last_verified_at=None)
        payload["artifact_type"] = "paper"

        with self.assertRaisesRegex(
            KnowledgeFrontmatterError,
            "requires artifact_type 'claim'",
        ):
            self.validate(payload, path="papers/primary.md")

    def test_non_verified_claim_reports_currentness_without_verified_state(self) -> None:
        evidence = self.register_text_evidence()
        self.primary_path.write_text(
            "alpha = 10\nbeta = 20\n",
            encoding="utf-8",
            newline="\n",
        )
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)

        result = self.validate(
            self.payload(
                status="stale",
                last_verified_at=None,
                evidence_refs=[self.ref(evidence.evidence_id)],
            )
        )

        self.assertFalse(result.valid)
        self.assertFalse(result.all_evidence_current)
        self.assertIsNone(result.verified_state_current)
        self.assertNotIn(
            "verified-claim-declares-noncurrent-evidence",
            result.reason_codes,
        )

    def test_actual_source_bytes_are_checked_even_before_registry_resync(self) -> None:
        evidence = self.register_text_evidence()
        sources_file = self.registration.layout.sources_file
        sources_before = sources_file.read_bytes()
        sources_mtime = sources_file.stat().st_mtime_ns
        self.primary_path.write_text(
            "alpha = 99\nbeta = 2\n",
            encoding="utf-8",
            newline="\n",
        )

        result = self.validate(
            self.payload(evidence_refs=[self.ref(evidence.evidence_id)])
        )

        reference = result.evidence_references[0]
        self.assertFalse(reference.current)
        self.assertEqual(reference.reason_code, "source-content-hash-mismatch")
        self.assertIsNotNone(reference.relocation)
        self.assertEqual(reference.relocation.status, "unresolved")
        self.assertEqual(sources_file.read_bytes(), sources_before)
        self.assertEqual(sources_file.stat().st_mtime_ns, sources_mtime)

    def test_a_to_b_to_a_does_not_revive_old_evidence(self) -> None:
        evidence = self.register_text_evidence()
        original = self.primary_path.read_text(encoding="utf-8")
        self.primary_path.write_text(
            "alpha = 7\nbeta = 8\n",
            encoding="utf-8",
            newline="\n",
        )
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)
        self.primary_path.write_text(
            original,
            encoding="utf-8",
            newline="\n",
        )
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)

        result = self.validate(
            self.payload(evidence_refs=[self.ref(evidence.evidence_id)])
        )

        reference = result.evidence_references[0]
        self.assertFalse(reference.current)
        self.assertEqual(reference.reason_code, "current-source-version-mismatch")

    def test_ambiguous_and_unresolved_relocations_are_report_only(self) -> None:
        evidence = self.register_text_evidence()
        sources_file = self.registration.layout.sources_file
        sources_before = sources_file.read_bytes()
        sources_mtime = sources_file.stat().st_mtime_ns
        candidate_a = self.project / "moved" / "candidate-a.txt"
        candidate_b = self.project / "moved" / "candidate-b.txt"
        candidate_a.parent.mkdir(parents=True)
        source_bytes = self.primary_path.read_bytes()
        self.primary_path.unlink()
        candidate_a.write_bytes(source_bytes)
        candidate_b.write_bytes(source_bytes)
        inventory_project(self.workspace, self.registration.project_id)

        ambiguous = self.validate(
            self.payload(evidence_refs=[self.ref(evidence.evidence_id)])
        ).evidence_references[0]

        self.assertEqual(ambiguous.reason_code, "source-relocation-ambiguous")
        self.assertIsNotNone(ambiguous.relocation)
        self.assertEqual(ambiguous.relocation.status, "ambiguous")
        self.assertEqual(
            ambiguous.relocation.candidate_paths,
            ("moved/candidate-a.txt", "moved/candidate-b.txt"),
        )
        self.assertEqual(sources_file.read_bytes(), sources_before)
        self.assertEqual(sources_file.stat().st_mtime_ns, sources_mtime)

        candidate_a.unlink()
        candidate_b.unlink()
        inventory_project(self.workspace, self.registration.project_id)
        unresolved = self.validate(
            self.payload(evidence_refs=[self.ref(evidence.evidence_id)])
        ).evidence_references[0]

        self.assertEqual(unresolved.reason_code, "source-current-path-missing")
        self.assertIsNotNone(unresolved.relocation)
        self.assertEqual(unresolved.relocation.status, "unresolved")
        self.assertEqual(sources_file.read_bytes(), sources_before)
        self.assertEqual(sources_file.stat().st_mtime_ns, sources_mtime)

    def test_successful_validation_does_not_modify_workspace_or_source(self) -> None:
        evidence = self.register_text_evidence()
        workspace_before = self.workspace_snapshot()
        project_before = self.project_snapshot()

        result = self.validate(
            self.payload(evidence_refs=[self.ref(evidence.evidence_id)])
        )

        self.assertTrue(result.valid)
        self.assertEqual(self.workspace_snapshot(), workspace_before)
        self.assertEqual(self.project_snapshot(), project_before)

    def test_relocation_inspection_failure_is_reported_and_read_only(self) -> None:
        evidence = self.register_text_evidence()
        moved = self.project / "moved" / "primary.txt"
        moved.parent.mkdir(parents=True)
        self.primary_path.replace(moved)
        workspace_before = self.workspace_snapshot()
        project_before = self.project_snapshot()

        with patch(
            "tools.claim_evidence.inspect_source_relocation",
            side_effect=SourceRecoveryError("inspection unavailable"),
        ):
            result = self.validate(
                self.payload(evidence_refs=[self.ref(evidence.evidence_id)])
            )

        reference = result.evidence_references[0]
        self.assertFalse(reference.current)
        self.assertEqual(reference.reason_code, "source-current-path-missing")
        self.assertIsNone(reference.relocation)
        self.assertEqual(
            reference.relocation_inspection_reason_code,
            "source-relocation-recovery-failed",
        )
        self.assertIn("failed closed", reference.detail)
        self.assertEqual(self.workspace_snapshot(), workspace_before)
        self.assertEqual(self.project_snapshot(), project_before)

    def test_schema_v1_is_read_only_and_future_versions_fail_closed(self) -> None:
        legacy = self.payload(schema_version=1)
        legacy.pop("evidence_refs")
        legacy["evidence_ids"] = []
        with self.assertRaisesRegex(KnowledgeFrontmatterError, "read-only"):
            self.validate(legacy)

        with self.assertRaises(UnsupportedKnowledgeSchemaVersionError):
            self.validate(self.payload(schema_version=3))

    def test_relocation_is_reported_without_rewriting_any_registry(self) -> None:
        evidence = self.register_text_evidence()
        moved = self.project / "moved" / "primary.txt"
        moved.parent.mkdir(parents=True)
        self.primary_path.replace(moved)
        inventory_project(self.workspace, self.registration.project_id)

        sources_file = self.registration.layout.sources_file
        evidence_file = self.registration.layout.evidence_file
        sources_before = sources_file.read_bytes()
        sources_mtime = sources_file.stat().st_mtime_ns
        evidence_before = evidence_file.read_bytes()
        evidence_mtime = evidence_file.stat().st_mtime_ns
        project_before = self.project_snapshot()
        knowledge_before = tuple(
            sorted(
                path.relative_to(self.registration.layout.knowledge_root).as_posix()
                for path in self.registration.layout.knowledge_root.rglob("*")
            )
        )

        result = self.validate(
            self.payload(evidence_refs=[self.ref(evidence.evidence_id)])
        )

        reference = result.evidence_references[0]
        self.assertFalse(reference.current)
        self.assertEqual(reference.reason_code, "source-relocation-unrecorded")
        self.assertIsNotNone(reference.relocation)
        self.assertEqual(reference.relocation.status, "relocatable")
        self.assertEqual(reference.relocation.candidate_paths, ("moved/primary.txt",))
        self.assertFalse(reference.relocation.as_dict()["registry_write_performed"])
        self.assertEqual(sources_file.read_bytes(), sources_before)
        self.assertEqual(sources_file.stat().st_mtime_ns, sources_mtime)
        self.assertEqual(evidence_file.read_bytes(), evidence_before)
        self.assertEqual(evidence_file.stat().st_mtime_ns, evidence_mtime)
        self.assertEqual(self.project_snapshot(), project_before)
        self.assertEqual(
            tuple(
                sorted(
                    path.relative_to(self.registration.layout.knowledge_root).as_posix()
                    for path in self.registration.layout.knowledge_root.rglob("*")
                )
            ),
            knowledge_before,
        )
        persisted = load_source_registry(
            self.workspace,
            self.registration.project_id,
        ).by_source_id[self.primary.source_id]
        self.assertEqual(persisted.current_path, "docs/primary.txt")
        self.assertEqual(
            moved.read_text(encoding="utf-8"),
            "alpha = 1\nbeta = 2\ngamma = alpha + beta\n",
        )


if __name__ == "__main__":
    unittest.main()
