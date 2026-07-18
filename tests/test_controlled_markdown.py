from __future__ import annotations

import dataclasses
import hashlib
import unittest
from unittest import mock

from tools.controlled_markdown import (
    CONTROLLED_MARKDOWN_PLAN_KIND,
    CONTROLLED_MARKDOWN_SCHEMA_VERSION,
    CONTROLLED_MARKDOWN_VERSION,
    MAX_KNOWLEDGE_PAGE_BYTES,
    ControlledMarkdownError,
    ControlledMarkdownOwnershipError,
    ControlledMarkdownRegionError,
    ControlledMarkdownRevisionError,
    parse_mixed_markdown_body,
    plan_controlled_markdown_update,
)
from tools.knowledge_artifacts import (
    KnowledgeFrontmatterError,
    parse_knowledge_page,
    serialize_knowledge_frontmatter,
)


PROJECT_ID = "study-0123456789ab"
SOURCE_ID = "src-" + "1" * 32
EVIDENCE_ID = "evd-" + "2" * 64
START = '<!-- llmwiki:user-region:start id="notes" -->\n'
END = '<!-- llmwiki:user-region:end id="notes" -->\n'


def frontmatter(
    *,
    artifact_type: str = "overview",
    title: str = "Study overview",
    status: str = "draft",
    ownership: str = "generated",
    source_ids: list[str] | None = None,
    evidence_refs: list[dict[str, str]] | None = None,
    generated_at: str = "2026-07-18T08:00:00Z",
    updated_at: str = "2026-07-18T08:00:00Z",
    last_verified_at: str | None = None,
    project_id: str = PROJECT_ID,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": "llmwiki-project-knowledge-page",
        "project_id": project_id,
        "artifact_type": artifact_type,
        "title": title,
        "status": status,
        "ownership": ownership,
        "source_ids": source_ids or [],
        "evidence_refs": evidence_refs or [],
        "generated_at": generated_at,
        "updated_at": updated_at,
        "last_verified_at": last_verified_at,
    }


def page_bytes(
    body: str,
    *,
    path: str = "overview.md",
    **changes: object,
) -> bytes:
    return (serialize_knowledge_frontmatter(frontmatter(**changes), path=path) + body).encode(
        "utf-8"
    )


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def mixed_body(content: str, *, generated: str = "# Summary\n\nGenerated.\n\n") -> str:
    return generated + START + content + END + "\nGenerated tail.\n"


class MixedBodyParserTests(unittest.TestCase):
    def test_parses_ordered_regions_and_preserves_exact_content(self) -> None:
        body = (
            "Generated A\n"
            + START
            + "Human\r\ntext\r\n"
            + END
            + "Generated B\n"
            + '<!-- llmwiki:user-region:start id="decision-2" -->\n'
            + "Accepted.\n"
            + '<!-- llmwiki:user-region:end id="decision-2" -->\n'
        )
        parsed = parse_mixed_markdown_body(body)
        self.assertEqual(parsed.region_ids, ("notes", "decision-2"))
        self.assertEqual(parsed.regions[0].content, "Human\r\ntext\r\n")
        self.assertEqual(parsed.render(), body)

    def test_rejects_missing_mismatched_duplicate_nested_and_orphan_markers(self) -> None:
        invalid = (
            START + "missing end\n",
            START + '<!-- llmwiki:user-region:end id="other" -->\n',
            START + END + START + END,
            START
            + '<!-- llmwiki:user-region:start id="nested" -->\n'
            + '<!-- llmwiki:user-region:end id="nested" -->\n'
            + END,
            END,
        )
        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(ControlledMarkdownRegionError):
                    parse_mixed_markdown_body(body)

    def test_rejects_no_regions_and_marker_like_invalid_lines(self) -> None:
        with self.assertRaisesRegex(ControlledMarkdownRegionError, "at least one"):
            parse_mixed_markdown_body("ordinary body\n")
        with self.assertRaisesRegex(ControlledMarkdownRegionError, "complete valid"):
            parse_mixed_markdown_body(
                '<!-- llmwiki:user-region:start id="UPPER" -->\n'
            )
        with self.assertRaisesRegex(ControlledMarkdownRegionError, "reserved user-region marker"):
            parse_mixed_markdown_body(START + "<!-- llmwiki:user-region:oops -->\n" + END)

    def test_only_literal_lf_or_crlf_delimit_markers(self) -> None:
        for separator in ("\u2028", "\u2029", "\x85", "\r"):
            body = "generated" + separator + START + END
            with self.subTest(separator=repr(separator)):
                with self.assertRaisesRegex(
                    ControlledMarkdownRegionError,
                    "complete valid marker line",
                ):
                    parse_mixed_markdown_body(body)

        content = "user line\u2028paragraph\u2029next\x85tail\rtext\r\n"
        body = (
            "generated\r\n"
            '<!-- llmwiki:user-region:start id="notes" -->\r\n'
            + content
            + '<!-- llmwiki:user-region:end id="notes" -->'
        )
        parsed = parse_mixed_markdown_body(body)
        self.assertEqual(parsed.regions[0].content, content)
        self.assertEqual(parsed.render(), body)


class ControlledMarkdownCreationTests(unittest.TestCase):
    def test_generator_creates_generated_page_with_canonical_output(self) -> None:
        proposed = page_bytes("# Overview\n", ownership="generated")
        plan = plan_controlled_markdown_update(
            path="overview.md",
            current=None,
            proposed=proposed,
            intent="regenerate",
            expected_current_sha256=None,
        )
        self.assertEqual(plan.schema_version, CONTROLLED_MARKDOWN_SCHEMA_VERSION)
        self.assertEqual(plan.kind, CONTROLLED_MARKDOWN_PLAN_KIND)
        self.assertEqual(plan.plan_version, CONTROLLED_MARKDOWN_VERSION)
        self.assertTrue(plan.created)
        self.assertEqual(plan.current_sha256, None)
        self.assertEqual(plan.proposed_sha256, sha(proposed))
        self.assertEqual(plan.output_sha256, sha(plan.output_bytes))
        self.assertTrue(plan.generated_body_changed)
        self.assertFalse(plan.user_body_changed)
        self.assertEqual(parse_knowledge_page(plan.output_bytes, path="overview.md").body, "# Overview\n")

    def test_user_creates_user_owned_page(self) -> None:
        proposed = page_bytes("# My goal\n", path="goals.md", artifact_type="goal", ownership="user")
        plan = plan_controlled_markdown_update(
            path="goals.md",
            current=None,
            proposed=proposed,
            intent="user-edit",
            expected_current_sha256=None,
        )
        self.assertTrue(plan.created)
        self.assertFalse(plan.generated_body_changed)
        self.assertTrue(plan.user_body_changed)

    def test_new_mixed_page_requires_generator_and_empty_user_regions(self) -> None:
        proposed = page_bytes(mixed_body("\n"), ownership="mixed")
        plan = plan_controlled_markdown_update(
            path="overview.md",
            current=None,
            proposed=proposed,
            intent="regenerate",
            expected_current_sha256=None,
        )
        self.assertEqual(plan.user_region_ids, ("notes",))
        self.assertEqual(plan.preserved_user_region_ids, ())
        with self.assertRaisesRegex(ControlledMarkdownOwnershipError, "new mixed"):
            plan_controlled_markdown_update(
                path="overview.md",
                current=None,
                proposed=proposed,
                intent="user-edit",
                expected_current_sha256=None,
            )
        populated = page_bytes(mixed_body("I approve.\n"), ownership="mixed")
        with self.assertRaisesRegex(ControlledMarkdownOwnershipError, "cannot create"):
            plan_controlled_markdown_update(
                path="overview.md",
                current=None,
                proposed=populated,
                intent="regenerate",
                expected_current_sha256=None,
            )

    def test_creation_rejects_a_non_null_revision_precondition(self) -> None:
        proposed = page_bytes("body", ownership="generated")
        with self.assertRaisesRegex(ControlledMarkdownRevisionError, "creation"):
            plan_controlled_markdown_update(
                path="overview.md",
                current=None,
                proposed=proposed,
                intent="regenerate",
                expected_current_sha256="0" * 64,
            )


class ControlledMarkdownGeneratedAndUserTests(unittest.TestCase):
    def test_generated_regeneration_replaces_body_and_reports_frontmatter_changes(self) -> None:
        current = page_bytes("old\n", ownership="generated")
        proposed = page_bytes(
            "new\n",
            ownership="generated",
            title="New title",
            updated_at="2026-07-18T08:01:00Z",
        )
        plan = plan_controlled_markdown_update(
            path="overview.md",
            current=current,
            proposed=proposed,
            intent="regenerate",
            expected_current_sha256=sha(current),
        )
        self.assertEqual(parse_knowledge_page(plan.output_bytes, path="overview.md").body, "new\n")
        self.assertEqual(plan.changed_frontmatter_fields, ("title", "updated_at"))
        self.assertTrue(plan.generated_body_changed)
        self.assertFalse(plan.user_body_changed)

    def test_user_edit_replaces_whole_user_owned_body(self) -> None:
        current = page_bytes("old user text\n", ownership="user")
        proposed = page_bytes(
            "new user text\n",
            ownership="user",
            updated_at="2026-07-18T08:01:00Z",
        )
        plan = plan_controlled_markdown_update(
            path="overview.md",
            current=current,
            proposed=proposed,
            intent="user-edit",
            expected_current_sha256=sha(current),
        )
        self.assertFalse(plan.generated_body_changed)
        self.assertTrue(plan.user_body_changed)
        self.assertEqual(parse_knowledge_page(plan.output_bytes, path="overview.md").body, "new user text\n")

    def test_intent_cannot_cross_generated_or_user_ownership(self) -> None:
        generated = page_bytes("body", ownership="generated")
        generated_next = page_bytes(
            "body 2", ownership="generated", updated_at="2026-07-18T08:01:00Z"
        )
        with self.assertRaises(ControlledMarkdownOwnershipError):
            plan_controlled_markdown_update(
                path="overview.md",
                current=generated,
                proposed=generated_next,
                intent="user-edit",
                expected_current_sha256=sha(generated),
            )
        user = page_bytes("body", ownership="user")
        user_next = page_bytes(
            "body 2", ownership="user", updated_at="2026-07-18T08:01:00Z"
        )
        with self.assertRaises(ControlledMarkdownOwnershipError):
            plan_controlled_markdown_update(
                path="overview.md",
                current=user,
                proposed=user_next,
                intent="regenerate",
                expected_current_sha256=sha(user),
            )

    def test_non_mixed_pages_reject_reserved_markers(self) -> None:
        for ownership, intent in (("generated", "regenerate"), ("user", "user-edit")):
            proposed = page_bytes(START + END, ownership=ownership)
            with self.subTest(ownership=ownership):
                with self.assertRaises(ControlledMarkdownRegionError):
                    plan_controlled_markdown_update(
                        path="overview.md",
                        current=None,
                        proposed=proposed,
                        intent=intent,
                        expected_current_sha256=None,
                    )


class ControlledMarkdownMixedTests(unittest.TestCase):
    def test_regeneration_preserves_exact_user_content_and_uses_new_generated_text(self) -> None:
        current_content = "Human confirmed\r\nline two\r\n"
        current = page_bytes(mixed_body(current_content, generated="Old generated\n"), ownership="mixed")
        proposed = page_bytes(
            mixed_body("generator must not overwrite this\n", generated="New generated\n"),
            ownership="mixed",
            updated_at="2026-07-18T08:01:00Z",
        )
        plan = plan_controlled_markdown_update(
            path="overview.md",
            current=current,
            proposed=proposed,
            intent="regenerate",
            expected_current_sha256=sha(current),
        )
        body = parse_knowledge_page(plan.output_bytes, path="overview.md").body
        self.assertIn("New generated\n", body)
        self.assertNotIn("generator must not overwrite this", body)
        self.assertIn(current_content, body)
        self.assertEqual(plan.user_region_ids, ("notes",))
        self.assertEqual(plan.preserved_user_region_ids, ("notes",))
        self.assertEqual(plan.changed_user_region_ids, ())
        self.assertEqual(plan.discarded_proposed_user_region_ids, ("notes",))
        self.assertTrue(plan.generated_body_changed)
        self.assertFalse(plan.user_body_changed)

    def test_user_edit_changes_only_region_content(self) -> None:
        current = page_bytes(mixed_body("old user\n"), ownership="mixed")
        proposed = page_bytes(
            mixed_body("new user\n"),
            ownership="mixed",
            updated_at="2026-07-18T08:01:00Z",
        )
        plan = plan_controlled_markdown_update(
            path="overview.md",
            current=current,
            proposed=proposed,
            intent="user-edit",
            expected_current_sha256=sha(current),
        )
        self.assertIn("new user\n", parse_knowledge_page(plan.output_bytes, path="overview.md").body)
        self.assertFalse(plan.generated_body_changed)
        self.assertTrue(plan.user_body_changed)
        self.assertEqual(plan.preserved_user_region_ids, ())
        self.assertEqual(plan.changed_user_region_ids, ("notes",))
        self.assertEqual(plan.discarded_proposed_user_region_ids, ())

    def test_user_edit_rejects_generated_text_or_marker_change(self) -> None:
        current = page_bytes(mixed_body("old user\n"), ownership="mixed")
        changed_generated = page_bytes(
            mixed_body("new user\n", generated="changed generated\n"),
            ownership="mixed",
            updated_at="2026-07-18T08:01:00Z",
        )
        with self.assertRaisesRegex(ControlledMarkdownOwnershipError, "generated-owned"):
            plan_controlled_markdown_update(
                path="overview.md",
                current=current,
                proposed=changed_generated,
                intent="user-edit",
                expected_current_sha256=sha(current),
            )

    def test_ordinary_update_rejects_added_removed_or_reordered_regions(self) -> None:
        second_start = '<!-- llmwiki:user-region:start id="decision" -->\n'
        second_end = '<!-- llmwiki:user-region:end id="decision" -->\n'
        current_body = mixed_body("note\n") + second_start + "yes\n" + second_end
        current = page_bytes(current_body, ownership="mixed")
        proposals = (
            mixed_body("note\n"),
            second_start + "yes\n" + second_end + mixed_body("note\n"),
        )
        for body in proposals:
            proposed = page_bytes(
                body,
                ownership="mixed",
                updated_at="2026-07-18T08:01:00Z",
            )
            with self.subTest(body=body):
                with self.assertRaisesRegex(ControlledMarkdownRegionError, "IDs and order"):
                    plan_controlled_markdown_update(
                        path="overview.md",
                        current=current,
                        proposed=proposed,
                        intent="regenerate",
                        expected_current_sha256=sha(current),
                    )


class ControlledMarkdownRevisionAndSchemaTests(unittest.TestCase):
    def test_existing_update_requires_exact_lowercase_sha256(self) -> None:
        current = page_bytes("old", ownership="generated")
        proposed = page_bytes(
            "new", ownership="generated", updated_at="2026-07-18T08:01:00Z"
        )
        for expected in (None, "0" * 64, sha(current).upper(), "abc"):
            with self.subTest(expected=expected):
                with self.assertRaises(ControlledMarkdownRevisionError):
                    plan_controlled_markdown_update(
                        path="overview.md",
                        current=current,
                        proposed=proposed,
                        intent="regenerate",
                        expected_current_sha256=expected,
                    )

    def test_existing_update_preserves_project_type_ownership_and_generated_time(self) -> None:
        current = page_bytes("old", ownership="generated")
        changes = (
            {"project_id": "study-aaaaaaaaaaaa"},
            {"artifact_type": "goal"},
            {"ownership": "user"},
            {"generated_at": "2026-07-18T07:59:00Z"},
        )
        for change in changes:
            proposed_args = {
                "ownership": "generated",
                "updated_at": "2026-07-18T08:01:00Z",
                **change,
            }
            if change.get("artifact_type") == "goal":
                # A path/type mismatch is rejected even before immutable-field comparison.
                with self.assertRaises(KnowledgeFrontmatterError):
                    page_bytes("new", **proposed_args)
                continue
            proposed = page_bytes("new", **proposed_args)
            with self.subTest(change=change):
                with self.assertRaises(ControlledMarkdownOwnershipError):
                    plan_controlled_markdown_update(
                        path="overview.md",
                        current=current,
                        proposed=proposed,
                        intent="regenerate",
                        expected_current_sha256=sha(current),
                    )

    def test_updated_at_must_advance_strictly(self) -> None:
        current = page_bytes(
            "old",
            ownership="generated",
            generated_at="2026-07-18T07:00:00Z",
        )
        for value in ("2026-07-18T08:00:00Z", "2026-07-18T07:59:59Z"):
            proposed = page_bytes(
                "new",
                ownership="generated",
                generated_at="2026-07-18T07:00:00Z",
                updated_at=value,
            )
            with self.subTest(value=value):
                with self.assertRaisesRegex(ControlledMarkdownRevisionError, "advance"):
                    plan_controlled_markdown_update(
                        path="overview.md",
                        current=current,
                        proposed=proposed,
                        intent="regenerate",
                        expected_current_sha256=sha(current),
                    )

    def test_schema_v1_and_future_schema_fail_closed(self) -> None:
        legacy = b"""---\nschema_version: 1\nkind: llmwiki-project-knowledge-page\nproject_id: study-0123456789ab\nartifact_type: overview\ntitle: Legacy\nstatus: draft\nownership: generated\nsource_ids: []\nevidence_ids: []\ngenerated_at: '2026-07-18T08:00:00Z'\nupdated_at: '2026-07-18T08:00:00Z'\nlast_verified_at: null\n---\nbody\n"""
        current_v2 = page_bytes("body", ownership="generated")
        proposed_v2 = page_bytes(
            "new", ownership="generated", updated_at="2026-07-18T08:01:00Z"
        )
        with self.assertRaisesRegex(ControlledMarkdownError, "Schema v1"):
            plan_controlled_markdown_update(
                path="overview.md",
                current=legacy,
                proposed=proposed_v2,
                intent="regenerate",
                expected_current_sha256=sha(legacy),
            )
        with self.assertRaisesRegex(ControlledMarkdownError, "Schema v1"):
            plan_controlled_markdown_update(
                path="overview.md",
                current=None,
                proposed=legacy,
                intent="regenerate",
                expected_current_sha256=None,
            )
        future = proposed_v2.replace(b"schema_version: 2", b"schema_version: 3", 1)
        with self.assertRaises(ControlledMarkdownError) as captured:
            plan_controlled_markdown_update(
                path="overview.md",
                current=None,
                proposed=future,
                intent="regenerate",
                expected_current_sha256=None,
            )
        self.assertEqual(captured.exception.as_dict()["reason_code"], "invalid-controlled-markdown")
        self.assertNotIn("schema_version 3", repr(captured.exception.as_dict()))
        # Keep the v2 current fixture used so accidental test simplification is visible.
        self.assertEqual(parse_knowledge_page(current_v2, path="overview.md").frontmatter.schema_version, 2)

    def test_wrong_path_invalid_utf8_invalid_intent_and_oversize_fail_closed(self) -> None:
        proposed = page_bytes("body", ownership="generated")
        with self.assertRaises(ControlledMarkdownError):
            plan_controlled_markdown_update(
                path="goals.md",
                current=None,
                proposed=proposed,
                intent="regenerate",
                expected_current_sha256=None,
            )
        with self.assertRaises(ControlledMarkdownError) as captured_utf8:
            plan_controlled_markdown_update(
                path="overview.md",
                current=None,
                proposed=b"\xff",
                intent="regenerate",
                expected_current_sha256=None,
            )
        utf8_audit = captured_utf8.exception.as_dict()
        self.assertEqual(utf8_audit["reason_code"], "invalid-controlled-markdown")
        self.assertNotIn("\xff", repr(utf8_audit))
        with self.assertRaisesRegex(ControlledMarkdownError, "intent"):
            plan_controlled_markdown_update(
                path="overview.md",
                current=None,
                proposed=proposed,
                intent="replace-all",
                expected_current_sha256=None,
            )
        with self.assertRaisesRegex(ControlledMarkdownError, "at most"):
            plan_controlled_markdown_update(
                path="overview.md",
                current=None,
                proposed=b"x" * (MAX_KNOWLEDGE_PAGE_BYTES + 1),
                intent="regenerate",
                expected_current_sha256=None,
            )


class ControlledMarkdownResultTests(unittest.TestCase):
    def test_plan_is_deterministic_and_serialized_view_contains_no_body(self) -> None:
        current = page_bytes(mixed_body("private user note\n"), ownership="mixed")
        proposed = page_bytes(
            mixed_body("ignored\n", generated="refreshed\n"),
            ownership="mixed",
            updated_at="2026-07-18T08:01:00Z",
        )
        first = plan_controlled_markdown_update(
            path="overview.md",
            current=current,
            proposed=proposed,
            intent="regenerate",
            expected_current_sha256=sha(current),
        )
        second = plan_controlled_markdown_update(
            path="overview.md",
            current=current,
            proposed=proposed,
            intent="regenerate",
            expected_current_sha256=sha(current),
        )
        self.assertEqual(first, second)
        payload = first.as_dict()
        self.assertEqual(payload["plan_id"], first.plan_id)
        self.assertEqual(payload["discarded_proposed_user_region_ids"], ["notes"])
        self.assertEqual(payload["output_byte_count"], len(first.output_bytes))
        self.assertNotIn("output_bytes", payload)
        self.assertNotIn("private user note", repr(payload))


    def test_expected_conflicts_have_body_free_stable_audit_classification(self) -> None:
        current = page_bytes("private current body", ownership="generated")
        proposed = page_bytes(
            "private proposed body",
            ownership="generated",
            updated_at="2026-07-18T08:01:00Z",
        )
        with self.assertRaises(ControlledMarkdownRevisionError) as captured:
            plan_controlled_markdown_update(
                path="overview.md",
                current=current,
                proposed=proposed,
                intent="regenerate",
                expected_current_sha256="0" * 64,
            )
        audit = captured.exception.as_dict()
        self.assertEqual(audit["outcome"], "rejected")
        self.assertEqual(audit["reason_code"], "revision-conflict")
        self.assertNotIn("private current body", repr(audit))
        self.assertNotIn("private proposed body", repr(audit))

    def test_result_rejects_output_or_identity_tampering(self) -> None:
        proposed = page_bytes("body", ownership="generated")
        plan = plan_controlled_markdown_update(
            path="overview.md",
            current=None,
            proposed=proposed,
            intent="regenerate",
            expected_current_sha256=None,
        )
        with self.assertRaisesRegex(ControlledMarkdownError, "output_bytes"):
            dataclasses.replace(plan, output_bytes=plan.output_bytes + b"x")
        with self.assertRaisesRegex(ControlledMarkdownError, "plan_id"):
            dataclasses.replace(plan, plan_id="cmp-" + "0" * 64)

    def test_plan_metadata_is_exactly_typed_and_not_mutably_shared(self) -> None:
        proposed = page_bytes("body", ownership="generated")
        plan = plan_controlled_markdown_update(
            path="overview.md",
            current=None,
            proposed=proposed,
            intent="regenerate",
            expected_current_sha256=None,
        )
        invalid = (
            {"schema_version": True},
            {"user_region_ids": []},
            {"changed_frontmatter_fields": ["title"]},
            {"changed_frontmatter_fields": ("not-a-field",)},
            {"changed_frontmatter_fields": ("title", "title")},
            {"changed_frontmatter_fields": ("updated_at", "title")},
            {"generated_body_changed": 1},
            {"validation_complete": 1},
            {"output_bytes": bytearray(plan.output_bytes)},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(ControlledMarkdownError):
                    dataclasses.replace(plan, **changes)

        metadata = plan.as_dict()
        metadata["changed_frontmatter_fields"].append("not-a-field")
        self.assertNotIn("not-a-field", plan.changed_frontmatter_fields)
        self.assertNotIn("not-a-field", plan.as_dict()["changed_frontmatter_fields"])

    def test_structural_rejections_have_body_free_audit_views(self) -> None:
        secret = "private body that must not enter an audit"
        with self.assertRaises(ControlledMarkdownError) as captured:
            plan_controlled_markdown_update(
                path="overview.md",
                current=None,
                proposed=secret.encode("utf-8"),
                intent="regenerate",
                expected_current_sha256=None,
            )
        audit = captured.exception.as_dict()
        self.assertEqual(audit["outcome"], "rejected")
        self.assertEqual(audit["reason_code"], "invalid-controlled-markdown")
        self.assertNotIn(secret, repr(audit))

    def test_ownership_and_region_rejections_have_typed_body_free_audit_views(self) -> None:
        secret = "private ownership body"
        current = page_bytes(secret, ownership="generated")
        proposed = page_bytes(
            secret + " changed",
            ownership="generated",
            updated_at="2026-07-18T08:01:00Z",
        )
        with self.assertRaises(ControlledMarkdownOwnershipError) as ownership_error:
            plan_controlled_markdown_update(
                path="overview.md",
                current=current,
                proposed=proposed,
                intent="user-edit",
                expected_current_sha256=sha(current),
            )
        ownership_audit = ownership_error.exception.as_dict()
        self.assertEqual(ownership_audit["reason_code"], "ownership-denied")
        self.assertNotIn(secret, repr(ownership_audit))

        current_mixed = page_bytes(mixed_body("old user\n"), ownership="mixed")
        duplicate_regions = page_bytes(
            START + secret + END + START + END,
            ownership="mixed",
            updated_at="2026-07-18T08:01:00Z",
        )
        with self.assertRaises(ControlledMarkdownRegionError) as region_error:
            plan_controlled_markdown_update(
                path="overview.md",
                current=current_mixed,
                proposed=duplicate_regions,
                intent="regenerate",
                expected_current_sha256=sha(current_mixed),
            )
        region_audit = region_error.exception.as_dict()
        self.assertEqual(region_audit["reason_code"], "protected-region-conflict")
        self.assertNotIn(secret, repr(region_audit))

    def test_verified_claim_remains_structural_only_not_evidence_currentness(self) -> None:
        proposed = page_bytes(
            "Claim body\n",
            path="claims/example.md",
            artifact_type="claim",
            status="verified",
            ownership="generated",
            source_ids=[SOURCE_ID],
            evidence_refs=[{"evidence_id": EVIDENCE_ID, "stance": "supporting"}],
            last_verified_at="2026-07-18T08:00:00Z",
        )
        plan = plan_controlled_markdown_update(
            path="claims/example.md",
            current=None,
            proposed=proposed,
            intent="regenerate",
            expected_current_sha256=None,
        )
        self.assertEqual(plan.artifact_type, "claim")
        self.assertTrue(plan.validation_complete)
        # F-05A deliberately does not claim Source/Evidence currentness.
        self.assertNotIn("verified_state_current", plan.as_dict())

    def test_planner_performs_no_filesystem_io(self) -> None:
        proposed = page_bytes("body", ownership="generated")
        with mock.patch("builtins.open", side_effect=AssertionError("unexpected I/O")):
            plan = plan_controlled_markdown_update(
                path="overview.md",
                current=None,
                proposed=proposed,
                intent="regenerate",
                expected_current_sha256=None,
            )
        self.assertTrue(plan.validation_complete)


if __name__ == "__main__":
    unittest.main()
