from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tools.controlled_markdown_persistence as persistence
from tools.controlled_markdown import plan_controlled_markdown_update
from tools.controlled_markdown_persistence import (
    CONTROLLED_MARKDOWN_AUDIT_FILE,
    ControlledMarkdownAuditStateError,
    ControlledMarkdownAuditWriteError,
    ControlledMarkdownAuthorizationError,
    ControlledMarkdownAuthorizationMismatchError,
    ControlledMarkdownAuthorizationReuseError,
    ControlledMarkdownCommitAuditUnknownError,
    ControlledMarkdownCommitUnknownError,
    ControlledMarkdownWriteConflictError,
    ControlledMarkdownWriteFailedError,
    ControlledMarkdownWriteRejectedError,
    StableFileAccessError,
    StableFileCommitUnknownError,
    StableFileRevisionConflictError,
    StableFileWriteResult,
    TrustedHostSessionContext,
    _parse_audit_payload,
    bind_controlled_markdown_authorization,
    persist_controlled_markdown_update,
)
from tools.knowledge_artifacts import serialize_knowledge_frontmatter
from tools.project_registry import register_project


START = '<!-- llmwiki:user-region:start id="confirmation" -->\n'
END = '<!-- llmwiki:user-region:end id="confirmation" -->\n'


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def page_bytes(
    project_id: str,
    body: str,
    *,
    path: str = "overview.md",
    artifact_type: str = "overview",
    title: str = "Study overview",
    status: str = "draft",
    ownership: str = "generated",
    source_ids: list[str] | None = None,
    evidence_refs: list[dict[str, str]] | None = None,
    generated_at: str = "2026-07-18T08:00:00Z",
    updated_at: str = "2026-07-18T08:00:00Z",
    last_verified_at: str | None = None,
) -> bytes:
    frontmatter = {
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
    return (serialize_knowledge_frontmatter(frontmatter, path=path) + body).encode(
        "utf-8"
    )


def mixed_body(user_text: str, *, generated: str = "# Summary\n\nGenerated.\n\n") -> str:
    return generated + START + user_text + END + "\nGenerated tail.\n"


class ControlledMarkdownPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        self.source_file = self.source_root / "README.md"
        self.source_file.write_bytes(b"source sentinel\n")
        self.source_before = self.source_file.read_bytes()
        self.registration = register_project(self.workspace, self.source_root)
        self.project_id = self.registration.project_id
        self.registration_before = self.registration.project_file.read_bytes()
        self.target = self.registration.layout.knowledge_root / "overview.md"
        self.ledger = self.registration.layout.indexes_dir / CONTROLLED_MARKDOWN_AUDIT_FILE
        self.host_context = TrustedHostSessionContext(
            host_id="codex",
            actor_type="host-agent",
            actor_id="agent-1",
            session_id="session-1",
        )
        self.decision_number = 0

    def tearDown(self) -> None:
        self.assertEqual(self.source_file.read_bytes(), self.source_before)
        self.assertEqual(
            self.registration.project_file.read_bytes(),
            self.registration_before,
        )

    def make_plan_and_authorization(
        self,
        *,
        current: bytes | None,
        proposed: bytes,
        path: str = "overview.md",
        intent: str = "regenerate",
        expected_current_sha256: str | None = None,
    ):
        plan = plan_controlled_markdown_update(
            path=path,
            current=current,
            proposed=proposed,
            intent=intent,
            expected_current_sha256=expected_current_sha256,
        )
        self.decision_number += 1
        authorization = bind_controlled_markdown_authorization(
            plan,
            host_context=self.host_context,
            decision_id=f"decision-{self.decision_number}",
            authorized_at=f"2026-07-18T08:10:{self.decision_number:02d}Z",
        )
        return plan, authorization

    def persist(
        self,
        proposed: bytes,
        authorization,
        *,
        intent: str = "regenerate",
        expected_current_sha256: str | None = None,
        path: str = "overview.md",
    ):
        return persist_controlled_markdown_update(
            self.workspace,
            self.project_id,
            path=path,
            proposed=proposed,
            intent=intent,
            expected_current_sha256=expected_current_sha256,
            authorization=authorization,
        )

    def audit_records(self) -> list[dict[str, object]]:
        if not self.ledger.exists():
            return []
        return [json.loads(line) for line in self.ledger.read_text().splitlines()]

    def assert_body_free_audit(self, secret: bytes) -> None:
        payload = self.ledger.read_bytes()
        self.assertNotIn(secret, payload)
        self.assertNotIn(b"raw-proposal", payload)
        self.assertNotIn(b"yaml-body", payload)

    def test_creation_publishes_exact_output_and_prepared_committed_audit(self) -> None:
        secret = b"generated secret that must not enter audit"
        proposed = page_bytes(self.project_id, "# Generated\n" + secret.decode())
        plan, authorization = self.make_plan_and_authorization(
            current=None,
            proposed=proposed,
        )

        result = self.persist(proposed, authorization)

        self.assertEqual(result.outcome, "committed")
        self.assertTrue(result.created)
        self.assertIsNone(result.previous_sha256)
        self.assertEqual(result.output_sha256, sha(plan.output_bytes))
        self.assertEqual(self.target.read_bytes(), plan.output_bytes)
        self.assertEqual(result.output_byte_count, len(plan.output_bytes))
        self.assertEqual(result.audit_sequence, 2)
        records = self.audit_records()
        self.assertEqual([record["phase"] for record in records], ["prepared", "committed"])
        self.assertEqual([record["sequence"] for record in records], [1, 2])
        self.assertEqual(records[0]["transaction_id"], result.transaction_id)
        self.assertEqual(records[1]["transaction_id"], result.transaction_id)
        self.assertEqual(records[1]["observed_after_sha256"], result.output_sha256)
        self.assertEqual(records[0]["commit_state"], "not-attempted")
        self.assertEqual(records[1]["commit_state"], result.commit_state)
        self.assert_body_free_audit(secret)

    def test_existing_replacement_preserves_first_ledger_prefix(self) -> None:
        current = page_bytes(self.project_id, "old\n")
        self.target.write_bytes(current)
        first = page_bytes(
            self.project_id,
            "first\n",
            updated_at="2026-07-18T08:01:00Z",
        )
        first_plan, first_auth = self.make_plan_and_authorization(
            current=current,
            proposed=first,
            expected_current_sha256=sha(current),
        )
        self.persist(first, first_auth, expected_current_sha256=sha(current))
        prefix = self.ledger.read_bytes()

        second = page_bytes(
            self.project_id,
            "second\n",
            updated_at="2026-07-18T08:02:00Z",
        )
        second_plan, second_auth = self.make_plan_and_authorization(
            current=first_plan.output_bytes,
            proposed=second,
            expected_current_sha256=sha(first_plan.output_bytes),
        )
        result = self.persist(
            second,
            second_auth,
            expected_current_sha256=sha(first_plan.output_bytes),
        )

        self.assertEqual(self.target.read_bytes(), second_plan.output_bytes)
        self.assertEqual(result.previous_sha256, sha(first_plan.output_bytes))
        self.assertTrue(self.ledger.read_bytes().startswith(prefix))
        self.assertEqual(
            [record["sequence"] for record in self.audit_records()], [1, 2, 3, 4]
        )

    def test_mixed_regeneration_preserves_exact_user_region_and_is_body_free(self) -> None:
        current = page_bytes(
            self.project_id,
            mixed_body("User approved text\r\nwith exact bytes\r\n"),
            ownership="mixed",
        )
        self.target.write_bytes(current)
        proposed = page_bytes(
            self.project_id,
            mixed_body("discarded proposal\n", generated="# New generated\n\n"),
            ownership="mixed",
            updated_at="2026-07-18T08:01:00Z",
        )
        plan, authorization = self.make_plan_and_authorization(
            current=current,
            proposed=proposed,
            expected_current_sha256=sha(current),
        )

        self.persist(proposed, authorization, expected_current_sha256=sha(current))

        self.assertEqual(
            self.target.read_bytes(),
            plan.output_bytes,
        )
        self.assertIn(b"User approved text\r\nwith exact bytes\r\n", plan.output_bytes)
        self.assertNotIn(b"discarded proposal", plan.output_bytes)
        self.assert_body_free_audit(b"discarded proposal")
        self.assertEqual(self.audit_records()[0]["discarded_proposed_user_region_ids"], ["confirmation"])

    def test_user_edit_changes_only_declared_user_region(self) -> None:
        current = page_bytes(
            self.project_id,
            mixed_body("old user\n"),
            ownership="mixed",
        )
        self.target.write_bytes(current)
        proposed = page_bytes(
            self.project_id,
            mixed_body("new user\n"),
            ownership="mixed",
            updated_at="2026-07-18T08:01:00Z",
        )
        plan, authorization = self.make_plan_and_authorization(
            current=current,
            proposed=proposed,
            intent="user-edit",
            expected_current_sha256=sha(current),
        )

        self.persist(
            proposed,
            authorization,
            intent="user-edit",
            expected_current_sha256=sha(current),
        )

        self.assertEqual(self.target.read_bytes(), plan.output_bytes)
        self.assertTrue(plan.user_body_changed)
        self.assertFalse(plan.generated_body_changed)

    def test_authorization_mismatch_writes_nothing(self) -> None:
        proposed = page_bytes(self.project_id, "accepted\n")
        _plan, authorization = self.make_plan_and_authorization(
            current=None,
            proposed=proposed,
        )
        other = page_bytes(self.project_id, "different\n")

        with self.assertRaises(ControlledMarkdownAuthorizationMismatchError):
            self.persist(other, authorization)

        self.assertFalse(self.target.exists())
        self.assertFalse(self.ledger.exists())

    def test_mutated_authorization_integrity_is_rejected_before_project_access(self) -> None:
        proposed = page_bytes(self.project_id, "accepted\n")
        plan = plan_controlled_markdown_update(
            path="overview.md",
            current=None,
            proposed=proposed,
            intent="regenerate",
            expected_current_sha256=None,
        )

        mutations = (
            ("nested-host-context", "actor_type", "system"),
            ("decision-metadata", "decision_id", "tampered-decision"),
        )
        for label, field_name, value in mutations:
            with self.subTest(label=label):
                host_context = TrustedHostSessionContext(
                    host_id="codex",
                    actor_type="host-agent",
                    actor_id="agent-1",
                    session_id="session-1",
                )
                authorization = bind_controlled_markdown_authorization(
                    plan,
                    host_context=host_context,
                    decision_id="integrity-decision",
                    authorized_at="2026-07-18T08:11:00Z",
                )
                target = (
                    authorization.host_context
                    if label == "nested-host-context"
                    else authorization
                )
                object.__setattr__(target, field_name, value)

                with mock.patch.object(
                    persistence,
                    "load_registered_project",
                    side_effect=AssertionError("project access must not occur"),
                ):
                    with self.assertRaises(ControlledMarkdownAuthorizationError):
                        self.persist(proposed, authorization)

        self.assertFalse(self.target.exists())
        self.assertFalse(self.ledger.exists())

    def test_plan_id_is_not_a_capability_and_replay_is_rejected(self) -> None:
        proposed = page_bytes(self.project_id, "one\n")
        _plan, authorization = self.make_plan_and_authorization(
            current=None,
            proposed=proposed,
        )
        self.persist(proposed, authorization)
        before = self.ledger.read_bytes()

        with self.assertRaises(ControlledMarkdownAuthorizationReuseError):
            self.persist(proposed, authorization)

        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.target.read_bytes(), proposed)

    def test_stale_initial_revision_fails_before_prepared_and_does_not_write(self) -> None:
        original = page_bytes(self.project_id, "original\n")
        self.target.write_bytes(original)
        proposed = page_bytes(
            self.project_id,
            "new\n",
            updated_at="2026-07-18T08:01:00Z",
        )
        _plan, authorization = self.make_plan_and_authorization(
            current=original,
            proposed=proposed,
            expected_current_sha256=sha(original),
        )
        live = page_bytes(
            self.project_id,
            "changed\n",
            updated_at="2026-07-18T08:01:00Z",
        )
        self.target.write_bytes(live)

        with self.assertRaises(ControlledMarkdownWriteConflictError):
            self.persist(proposed, authorization, expected_current_sha256=sha(original))

        self.assertEqual(self.target.read_bytes(), live)
        self.assertFalse(self.ledger.exists())

    def test_cas_conflict_after_prepared_consumes_authorization_and_records_conflict(self) -> None:
        current = page_bytes(self.project_id, "old\n")
        self.target.write_bytes(current)
        proposed = page_bytes(
            self.project_id,
            "new\n",
            updated_at="2026-07-18T08:01:00Z",
        )
        plan, authorization = self.make_plan_and_authorization(
            current=current,
            proposed=proposed,
            expected_current_sha256=sha(current),
        )
        real_cas = persistence.compare_and_swap_atomic_stable_file

        def race(trusted_root, path, payload, *, expected_current_sha256, root_lease=None):
            if Path(path) == self.target:
                other = page_bytes(
                    self.project_id,
                    "outside writer\n",
                    updated_at="2026-07-18T08:02:00Z",
                )
                self.target.write_bytes(other)
                raise StableFileRevisionConflictError(
                    "simulated outside writer",
                    expected_current_sha256=expected_current_sha256,
                    observed_current_sha256=sha(other),
                )
            return real_cas(
                trusted_root,
                path,
                payload,
                expected_current_sha256=expected_current_sha256,
                root_lease=root_lease,
            )

        with mock.patch.object(persistence, "compare_and_swap_atomic_stable_file", side_effect=race):
            with self.assertRaises(ControlledMarkdownWriteConflictError) as raised:
                self.persist(proposed, authorization, expected_current_sha256=sha(current))

        self.assertEqual(raised.exception.transaction_id, self.audit_records()[1]["transaction_id"])
        self.assertEqual([record["phase"] for record in self.audit_records()], ["prepared", "conflict"])
        self.assertEqual(self.target.read_bytes(), page_bytes(self.project_id, "outside writer\n", updated_at="2026-07-18T08:02:00Z"))
        with self.assertRaises(ControlledMarkdownAuthorizationReuseError):
            self.persist(proposed, authorization, expected_current_sha256=sha(current))
        self.assertEqual(self.audit_records()[1]["reason_code"], "revision-conflict")
        self.assertEqual(self.audit_records()[1]["observed_after_sha256"], sha(self.target.read_bytes()))
        self.assertEqual(plan.output_sha256, sha(plan.output_bytes))

    def test_same_trusted_decision_cannot_bind_a_second_authorization(self) -> None:
        first = page_bytes(self.project_id, "first\n")
        _first_plan, first_authorization = self.make_plan_and_authorization(
            current=None,
            proposed=first,
        )
        self.persist(first, first_authorization)

        current = self.target.read_bytes()
        second = page_bytes(
            self.project_id,
            "second\n",
            updated_at="2026-07-18T08:01:00Z",
        )
        second_plan = plan_controlled_markdown_update(
            path="overview.md",
            current=current,
            proposed=second,
            intent="regenerate",
            expected_current_sha256=sha(current),
        )
        replayed_decision = bind_controlled_markdown_authorization(
            second_plan,
            host_context=self.host_context,
            decision_id=first_authorization.decision_id,
            authorized_at="2026-07-18T08:11:00Z",
        )

        with self.assertRaises(ControlledMarkdownAuthorizationReuseError):
            self.persist(
                second,
                replayed_decision,
                expected_current_sha256=sha(current),
            )
        self.assertEqual(self.target.read_bytes(), current)
        self.assertEqual([record["phase"] for record in self.audit_records()], [
            "prepared",
            "committed",
        ])

    def test_target_appearing_during_creation_is_not_overwritten(self) -> None:
        proposed = page_bytes(self.project_id, "planned\n")
        _plan, authorization = self.make_plan_and_authorization(
            current=None,
            proposed=proposed,
        )
        real_cas = persistence.compare_and_swap_atomic_stable_file

        def race(trusted_root, path, payload, *, expected_current_sha256, root_lease=None):
            if Path(path) == self.target:
                intruder = page_bytes(self.project_id, "appeared\n")
                self.target.write_bytes(intruder)
                raise StableFileRevisionConflictError(
                    "simulated appearing target",
                    expected_current_sha256=None,
                    observed_current_sha256=sha(intruder),
                )
            return real_cas(
                trusted_root,
                path,
                payload,
                expected_current_sha256=expected_current_sha256,
                root_lease=root_lease,
            )

        with mock.patch.object(persistence, "compare_and_swap_atomic_stable_file", side_effect=race):
            with self.assertRaises(ControlledMarkdownWriteConflictError):
                self.persist(proposed, authorization)
        self.assertEqual(self.target.read_bytes(), page_bytes(self.project_id, "appeared\n"))
        self.assertEqual([record["phase"] for record in self.audit_records()], ["prepared", "conflict"])

    def test_malformed_existing_audit_fails_closed_before_page_write(self) -> None:
        self.ledger.write_bytes(b"not-json\n")
        proposed = page_bytes(self.project_id, "new\n")
        _plan, authorization = self.make_plan_and_authorization(
            current=None,
            proposed=proposed,
        )

        with self.assertRaises(ControlledMarkdownAuditStateError):
            self.persist(proposed, authorization)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.ledger.read_bytes(), b"not-json\n")

    def test_audit_parser_rejects_duplicate_unknown_noncanonical_and_legacy_records(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        self.persist(proposed, self.make_plan_and_authorization(current=None, proposed=proposed)[1])
        valid = self.ledger.read_bytes().splitlines()[0]
        record = json.loads(valid)

        duplicate = b'{"schema_version":1,"schema_version":1}\n'
        with self.assertRaises(ControlledMarkdownAuditStateError):
            _parse_audit_payload(duplicate)

        unknown = dict(record)
        unknown["future_field"] = True
        with self.assertRaises(ControlledMarkdownAuditStateError):
            _parse_audit_payload(json.dumps(unknown, sort_keys=True, separators=(",", ":")).encode() + b"\n")

        noncanonical = json.dumps(record, sort_keys=True, separators=(", ", ": ")).encode() + b"\n"
        with self.assertRaises(ControlledMarkdownAuditStateError):
            _parse_audit_payload(noncanonical)

        legacy = dict(record)
        legacy["schema_version"] = 0
        with self.assertRaises(ControlledMarkdownAuditStateError):
            _parse_audit_payload(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode() + b"\n")

        future = dict(record)
        future["schema_version"] = 2
        with self.assertRaises(ControlledMarkdownAuditStateError):
            _parse_audit_payload(json.dumps(future, sort_keys=True, separators=(",", ":")).encode() + b"\n")

        with self.assertRaises(ControlledMarkdownAuditStateError):
            _parse_audit_payload(b'{"schema_version":1,"kind":"\xff"}\n')

    def test_audit_parser_rejects_consecutive_prepared_records(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        self.persist(
            proposed,
            self.make_plan_and_authorization(current=None, proposed=proposed)[1],
        )
        records = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        prepared = dict(records[0])
        prepared["sequence"] = 2
        prepared["transaction_id"] = "cmtx-" + "f" * 64
        prepared["authorization_id"] = "cmwa-" + "e" * 64
        prepared["decision_id"] = "new-decision"
        prepared["host_id"] = "other-host"
        prepared["actor_id"] = "other-actor"
        prepared["session_id"] = "other-session"
        first_line = json.dumps(records[0], sort_keys=True, separators=(",", ":")).encode() + b"\n"
        raw = first_line + (
            json.dumps(prepared, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
        with self.assertRaises(ControlledMarkdownAuditStateError):
            _parse_audit_payload(raw)

    def test_audit_record_bound_is_checked_before_append(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        self.persist(proposed, self.make_plan_and_authorization(current=None, proposed=proposed)[1])
        before = self.ledger.read_bytes()
        with mock.patch.object(persistence, "MAX_CONTROLLED_MARKDOWN_AUDIT_RECORDS", 2):
            next_page = page_bytes(
                self.project_id,
                "next\n",
                updated_at="2026-07-18T08:01:00Z",
            )
            current = self.target.read_bytes()
            _plan, authorization = self.make_plan_and_authorization(
                current=current,
                proposed=next_page,
                expected_current_sha256=sha(current),
            )
            with self.assertRaises(ControlledMarkdownAuditWriteError):
                self.persist(next_page, authorization, expected_current_sha256=sha(current))
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.target.read_bytes(), current)

    def test_prepared_reserves_terminal_record_capacity_before_publication(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        _plan, authorization = self.make_plan_and_authorization(
            current=None,
            proposed=proposed,
        )

        with mock.patch.object(
            persistence,
            "MAX_CONTROLLED_MARKDOWN_AUDIT_RECORDS",
            1,
        ):
            with self.assertRaises(ControlledMarkdownAuditWriteError):
                self.persist(proposed, authorization)

        self.assertFalse(self.target.exists())
        self.assertFalse(self.ledger.exists())

    def test_prepared_reserves_maximum_terminal_byte_capacity_before_publication(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        _plan, authorization = self.make_plan_and_authorization(
            current=None,
            proposed=proposed,
        )

        with mock.patch.object(
            persistence,
            "MAX_CONTROLLED_MARKDOWN_AUDIT_BYTES",
            persistence.MAX_CONTROLLED_MARKDOWN_AUDIT_RECORD_BYTES + 1,
        ):
            with self.assertRaises(ControlledMarkdownAuditWriteError):
                self.persist(proposed, authorization)

        self.assertFalse(self.target.exists())
        self.assertFalse(self.ledger.exists())

    def test_prepared_audit_failure_prevents_page_publication(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        _plan, authorization = self.make_plan_and_authorization(current=None, proposed=proposed)
        real_cas = persistence.compare_and_swap_atomic_stable_file

        def fail_prepared(trusted_root, path, payload, *, expected_current_sha256, root_lease=None):
            if Path(path) == self.ledger:
                raise StableFileAccessError("audit unavailable")
            return real_cas(
                trusted_root,
                path,
                payload,
                expected_current_sha256=expected_current_sha256,
                root_lease=root_lease,
            )

        with mock.patch.object(persistence, "compare_and_swap_atomic_stable_file", side_effect=fail_prepared):
            with self.assertRaises(ControlledMarkdownAuditWriteError):
                self.persist(proposed, authorization)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.ledger.exists())

    def test_page_write_failure_records_failed_and_does_not_claim_commit(self) -> None:
        current = page_bytes(self.project_id, "old\n")
        self.target.write_bytes(current)
        proposed = page_bytes(
            self.project_id,
            "new\n",
            updated_at="2026-07-18T08:01:00Z",
        )
        _plan, authorization = self.make_plan_and_authorization(
            current=current,
            proposed=proposed,
            expected_current_sha256=sha(current),
        )
        real_cas = persistence.compare_and_swap_atomic_stable_file

        def fail_page(trusted_root, path, payload, *, expected_current_sha256, root_lease=None):
            if Path(path) == self.target:
                raise StableFileAccessError("write failed")
            return real_cas(
                trusted_root,
                path,
                payload,
                expected_current_sha256=expected_current_sha256,
                root_lease=root_lease,
            )

        with mock.patch.object(persistence, "compare_and_swap_atomic_stable_file", side_effect=fail_page):
            with self.assertRaises(ControlledMarkdownWriteFailedError) as raised:
                self.persist(proposed, authorization, expected_current_sha256=sha(current))
        self.assertEqual(raised.exception.transaction_id, self.audit_records()[1]["transaction_id"])
        self.assertEqual([record["phase"] for record in self.audit_records()], ["prepared", "failed"])
        self.assertEqual(self.target.read_bytes(), current)
        self.assertEqual(self.audit_records()[1]["page_commit_state"] if "page_commit_state" in self.audit_records()[1] else "not-in-audit", "not-in-audit")

    def test_stable_commit_unknown_records_unknown_and_does_not_rollback(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        _plan, authorization = self.make_plan_and_authorization(current=None, proposed=proposed)
        real_cas = persistence.compare_and_swap_atomic_stable_file

        def unknown_page(trusted_root, path, payload, *, expected_current_sha256, root_lease=None):
            if Path(path) == self.target:
                self.target.write_bytes(payload)
                raise StableFileCommitUnknownError("unknown")
            return real_cas(
                trusted_root,
                path,
                payload,
                expected_current_sha256=expected_current_sha256,
                root_lease=root_lease,
            )

        with mock.patch.object(persistence, "compare_and_swap_atomic_stable_file", side_effect=unknown_page):
            with self.assertRaises(ControlledMarkdownCommitUnknownError):
                self.persist(proposed, authorization)
        self.assertEqual(self.target.read_bytes(), proposed)
        self.assertEqual([record["phase"] for record in self.audit_records()], ["prepared", "commit-unknown"])
        self.assertEqual(self.audit_records()[1]["commit_state"], "unknown")

    def test_page_durability_unknown_records_unknown_without_rollback(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        _plan, authorization = self.make_plan_and_authorization(
            current=None,
            proposed=proposed,
        )
        real_cas = persistence.compare_and_swap_atomic_stable_file

        def durability_unknown_page(
            trusted_root,
            path,
            payload,
            *,
            expected_current_sha256,
            root_lease=None,
        ):
            if Path(path) == self.target:
                self.target.write_bytes(payload)
                return StableFileWriteResult(
                    wrote=True,
                    commit_state="committed-durability-unknown",
                )
            return real_cas(
                trusted_root,
                path,
                payload,
                expected_current_sha256=expected_current_sha256,
                root_lease=root_lease,
            )

        with mock.patch.object(
            persistence,
            "compare_and_swap_atomic_stable_file",
            side_effect=durability_unknown_page,
        ):
            with self.assertRaises(ControlledMarkdownCommitUnknownError):
                self.persist(proposed, authorization)

        self.assertEqual(self.target.read_bytes(), proposed)
        records = self.audit_records()
        self.assertEqual(
            [record["phase"] for record in records],
            ["prepared", "commit-unknown"],
        )
        self.assertEqual(records[1]["reason_code"], "durability-unknown")

    def test_post_write_hash_mismatch_records_unknown(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        _plan, authorization = self.make_plan_and_authorization(current=None, proposed=proposed)
        real_cas = persistence.compare_and_swap_atomic_stable_file

        def wrong_output(trusted_root, path, payload, *, expected_current_sha256, root_lease=None):
            if Path(path) == self.target:
                wrong = page_bytes(self.project_id, "wrong\n")
                self.target.write_bytes(wrong)
                return StableFileWriteResult(wrote=True, commit_state="committed")
            return real_cas(
                trusted_root,
                path,
                payload,
                expected_current_sha256=expected_current_sha256,
                root_lease=root_lease,
            )

        with mock.patch.object(persistence, "compare_and_swap_atomic_stable_file", side_effect=wrong_output):
            with self.assertRaises(ControlledMarkdownCommitUnknownError):
                self.persist(proposed, authorization)
        self.assertNotEqual(self.target.read_bytes(), proposed)
        self.assertEqual([record["phase"] for record in self.audit_records()], ["prepared", "commit-unknown"])
        self.assertEqual(self.audit_records()[1]["reason_code"], "post-write-hash-mismatch")

    def test_terminal_audit_failure_reports_commit_audit_unknown_without_rollback(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        _plan, authorization = self.make_plan_and_authorization(current=None, proposed=proposed)
        real_cas = persistence.compare_and_swap_atomic_stable_file
        ledger_calls = 0

        def fail_terminal_audit(trusted_root, path, payload, *, expected_current_sha256, root_lease=None):
            nonlocal ledger_calls
            if Path(path) == self.ledger:
                ledger_calls += 1
                if ledger_calls == 2:
                    raise StableFileAccessError("terminal audit failed")
            return real_cas(
                trusted_root,
                path,
                payload,
                expected_current_sha256=expected_current_sha256,
                root_lease=root_lease,
            )

        with mock.patch.object(persistence, "compare_and_swap_atomic_stable_file", side_effect=fail_terminal_audit):
            with self.assertRaises(ControlledMarkdownCommitAuditUnknownError):
                self.persist(proposed, authorization)
        self.assertEqual(self.target.read_bytes(), proposed)
        self.assertEqual([record["phase"] for record in self.audit_records()], ["prepared"])

    def test_terminal_audit_read_failure_is_commit_audit_unknown(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        _plan, authorization = self.make_plan_and_authorization(
            current=None,
            proposed=proposed,
        )
        real_read = persistence._read_audit_state
        read_count = 0

        def fail_terminal_read(*args, **kwargs):
            nonlocal read_count
            read_count += 1
            if read_count == 4:
                raise ControlledMarkdownAuditStateError("terminal audit read failed")
            return real_read(*args, **kwargs)

        with mock.patch.object(
            persistence,
            "_read_audit_state",
            side_effect=fail_terminal_read,
        ):
            with self.assertRaises(ControlledMarkdownCommitAuditUnknownError) as raised:
                self.persist(proposed, authorization)

        self.assertEqual(self.target.read_bytes(), proposed)
        records = self.audit_records()
        self.assertEqual([record["phase"] for record in records], ["prepared"])
        self.assertEqual(raised.exception.transaction_id, records[0]["transaction_id"])

    def test_terminal_capacity_failure_after_publication_is_commit_audit_unknown(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        _plan, authorization = self.make_plan_and_authorization(
            current=None,
            proposed=proposed,
        )
        real_cas = persistence.compare_and_swap_atomic_stable_file

        def exhaust_after_page_publish(
            trusted_root,
            path,
            payload,
            *,
            expected_current_sha256,
            root_lease=None,
        ):
            result = real_cas(
                trusted_root,
                path,
                payload,
                expected_current_sha256=expected_current_sha256,
                root_lease=root_lease,
            )
            if Path(path) == self.target:
                persistence.MAX_CONTROLLED_MARKDOWN_AUDIT_RECORDS = 1
            return result

        with mock.patch.object(
            persistence,
            "MAX_CONTROLLED_MARKDOWN_AUDIT_RECORDS",
            2,
        ), mock.patch.object(
            persistence,
            "compare_and_swap_atomic_stable_file",
            side_effect=exhaust_after_page_publish,
        ):
            with self.assertRaises(ControlledMarkdownCommitAuditUnknownError):
                self.persist(proposed, authorization)

        self.assertEqual(self.target.read_bytes(), proposed)
        self.assertEqual(
            [record["phase"] for record in self.audit_records()],
            ["prepared"],
        )

    def test_registered_custom_knowledge_root_is_the_only_markdown_target(self) -> None:
        custom_workspace = self.root / "custom-workspace"
        custom_source = self.root / "custom-source"
        custom_source.mkdir()
        custom_source_file = custom_source / "README.md"
        custom_source_file.write_bytes(b"custom source sentinel\n")
        source_before = custom_source_file.read_bytes()
        custom_parent = self.root / "custom-knowledge"
        registration = register_project(
            custom_workspace,
            custom_source,
            knowledge_root=custom_parent,
        )
        project_id = registration.project_id
        target = registration.layout.knowledge_root / "overview.md"
        proposed = page_bytes(project_id, "custom root\n")
        plan = plan_controlled_markdown_update(
            path="overview.md",
            current=None,
            proposed=proposed,
            intent="regenerate",
            expected_current_sha256=None,
        )
        authorization = bind_controlled_markdown_authorization(
            plan,
            host_context=TrustedHostSessionContext(
                host_id="codex",
                actor_type="host-agent",
                actor_id="agent-custom",
                session_id="session-custom",
            ),
            decision_id="custom-root-decision",
            authorized_at="2026-07-18T08:12:00Z",
        )

        result = persist_controlled_markdown_update(
            custom_workspace,
            project_id,
            path="overview.md",
            proposed=proposed,
            intent="regenerate",
            expected_current_sha256=None,
            authorization=authorization,
        )

        self.assertEqual(result.outcome, "committed")
        self.assertEqual(target.read_bytes(), plan.output_bytes)
        self.assertEqual(registration.layout.knowledge_root.parent, custom_parent.resolve())
        default_target = (
            custom_workspace
            / "wiki"
            / "projects"
            / project_id
            / "overview.md"
        )
        self.assertFalse(default_target.exists())
        self.assertEqual(custom_source_file.read_bytes(), source_before)

    def test_invalid_paths_and_missing_parent_fail_without_creating_directories(self) -> None:
        proposed = page_bytes(self.project_id, "new\n")
        _plan, authorization = self.make_plan_and_authorization(current=None, proposed=proposed)
        with self.assertRaises(ControlledMarkdownWriteRejectedError):
            self.persist(proposed, authorization, path="../outside.md")
        self.assertFalse((self.registration.layout.knowledge_root / "outside.md").exists())

        papers = self.registration.layout.knowledge_root / "papers"
        papers.rmdir()
        paper = page_bytes(self.project_id, "paper\n", path="papers/paper.md", artifact_type="paper")
        _plan, authorization = self.make_plan_and_authorization(
            current=None,
            proposed=paper,
            path="papers/paper.md",
        )
        with self.assertRaises(ControlledMarkdownWriteRejectedError):
            self.persist(paper, authorization, path="papers/paper.md")
        self.assertFalse(papers.exists())
        self.assertFalse((papers / "paper.md").exists())

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlink support unavailable")
    def test_symlinked_knowledge_parent_fails_closed(self) -> None:
        papers = self.registration.layout.knowledge_root / "papers"
        outside = self.root / "outside"
        outside.mkdir()
        papers.rmdir()
        try:
            papers.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        proposed = page_bytes(self.project_id, "paper\n", path="papers/paper.md", artifact_type="paper")
        _plan, authorization = self.make_plan_and_authorization(
            current=None,
            proposed=proposed,
            path="papers/paper.md",
        )
        with self.assertRaises(ControlledMarkdownWriteRejectedError):
            self.persist(proposed, authorization, path="papers/paper.md")
        self.assertFalse((outside / "paper.md").exists())


if __name__ == "__main__":
    unittest.main()
