from __future__ import annotations

from collections import Counter
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request
import webbrowser

from tools import project_reconciliation as reconciliation_module
from tools.project import main as project_main
from tools.project_inventory import (
    ProjectInventoryError,
    inventory_project,
)
from tools.project_layout import CURRENT_SCHEMA_VERSION, UnsupportedSchemaVersionError
from tools.project_reconciliation import (
    RECONCILIATION_MODE,
    RECONCILIATION_RESULT_KIND,
    RECONCILIATION_SCHEMA_VERSION,
    RECONCILIATION_SOURCE_OF_TRUTH,
    RECONCILIATION_STATE_KIND,
    RECONCILIATION_VERSION,
    ProjectReconciliationResult,
    ReconciliationHintError,
    ReconciliationLockError,
    ReconciliationRunError,
    ReconciliationSnapshotError,
    ReconciliationState,
    ReconciliationStateError,
    _exclusive_reconciliation_lock,
    _write_reconciliation_state_atomic,
    load_reconciliation_state,
    reconcile_project,
)
from tools.project_registry import ProjectRegistrationResult
from tools.project_runs import ProjectRunResult
from tools.research_core import ResearchCoreService


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXED_TIME = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)


class ProjectReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.knowledge = self.root / "knowledge"
        self.service, self.registration, self.project = self.make_fixture(
            "reconciliation-study"
        )
        self.layout = self.registration.layout

    def make_fixture(
        self, project_id: str
    ) -> tuple[ResearchCoreService, ProjectRegistrationResult, Path]:
        project = self.root / f"source-{project_id}"
        (project / "src").mkdir(parents=True)
        (project / "config").mkdir()
        (project / "README.md").write_text(
            f"# {project_id}\n",
            encoding="utf-8",
            newline="\n",
        )
        (project / "src" / "model.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
            newline="\n",
        )
        (project / "config" / "train.yaml").write_text(
            "epochs: 2\n",
            encoding="utf-8",
            newline="\n",
        )
        service = ResearchCoreService(self.workspace)
        registration = service.register(
            project,
            project_id=project_id,
            knowledge_root=self.knowledge,
        )
        return service, registration, project

    @staticmethod
    def clock() -> datetime:
        return FIXED_TIME

    @staticmethod
    def canonical_json_line(value: object) -> bytes:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )

    @staticmethod
    def tree_snapshot(
        root: Path,
    ) -> tuple[tuple[str, ...], dict[str, tuple[str, int, int, int]]]:
        directories: list[str] = []
        files: dict[str, tuple[str, int, int, int]] = {}
        if not root.exists():
            return (), {}
        for directory, dirnames, filenames in os.walk(root):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            for dirname in dirnames:
                path = base / dirname
                if not path.is_symlink():
                    directories.append(path.relative_to(root).as_posix())
            for filename in filenames:
                path = base / filename
                if path.is_symlink():
                    continue
                metadata = path.stat()
                files[path.relative_to(root).as_posix()] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_mode,
                )
        return tuple(sorted(directories)), files

    @staticmethod
    def manifest_rows(path: Path) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_manifest_rows(
        self,
        path: Path,
        rows: list[dict[str, object]],
    ) -> None:
        path.write_bytes(b"".join(self.canonical_json_line(row) for row in rows))

    def mark_manifest_file_failed(self, path: Path, relative_path: str) -> None:
        rows = self.manifest_rows(path)
        for row in rows[1:]:
            if row.get("record_type") != "file" or row.get("path") != relative_path:
                continue
            file_state = row.get("file_state")
            if not isinstance(file_state, dict):
                self.fail(f"Manifest row {relative_path!r} has no file state")
            file_state.update(
                {
                    "processing_status": "failed",
                    "read_depth": "normal_read",
                    "reason_code": "deterministic-read-failed",
                    "reason": "deterministic extraction could not read the file",
                }
            )
            break
        else:
            self.fail(f"missing Manifest file row {relative_path!r}")

        status_counts: Counter[str] = Counter()
        depth_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        for row in rows[1:]:
            if row.get("record_type") != "file":
                continue
            file_state = row.get("file_state")
            if not isinstance(file_state, dict):
                self.fail("Manifest file row has no file state")
            status_counts[file_state["processing_status"]] += 1
            depth_counts[file_state["read_depth"]] += 1
            reason_counts[file_state["reason_code"]] += 1

        summary = rows[0].get("file_state_summary")
        if not isinstance(summary, dict):
            self.fail("Manifest summary has no file-state summary")
        summary["processing_statuses"] = dict(sorted(status_counts.items()))
        summary["read_depths"] = dict(sorted(depth_counts.items()))
        summary["reasons"] = dict(sorted(reason_counts.items()))
        self.write_manifest_rows(path, rows)

    @staticmethod
    def bind_run_to_current_artifacts(
        run: ProjectRunResult,
        *,
        manifest_file: Path,
        coverage_file: Path,
    ) -> ProjectRunResult:
        record = deepcopy(run.record)
        expected_hashes = {
            "manifest.jsonl": hashlib.sha256(manifest_file.read_bytes()).hexdigest(),
            "indexes/coverage-report.json": hashlib.sha256(
                coverage_file.read_bytes()
            ).hexdigest(),
        }
        updated: Counter[str] = Counter()
        artifact_ledgers = [record["artifacts"]]
        for stage in record["stages"]:
            artifact_ledgers.append(stage["artifacts"])
            if stage["attempts"]:
                artifact_ledgers.append(stage["attempts"][-1]["artifacts"])
        for artifacts in artifact_ledgers:
            for artifact in artifacts:
                relative_path = artifact.get("relative_path")
                if relative_path in expected_hashes:
                    artifact["content_hash"] = expected_hashes[relative_path]
                    updated[relative_path] += 1
        expected_updates = Counter({path: 3 for path in expected_hashes})
        if updated != expected_updates:
            raise AssertionError("run did not expose both reconciliation artifacts")
        return ProjectRunResult(run_file=run.run_file, record=record)

    @staticmethod
    def nested_strings(value: object) -> tuple[str, ...]:
        strings: list[str] = []
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                strings.extend(ProjectReconciliationTests.nested_strings(child))
        elif isinstance(value, (list, tuple)):
            for child in value:
                strings.extend(ProjectReconciliationTests.nested_strings(child))
        return tuple(strings)

    def submit(
        self,
        *,
        service: ResearchCoreService | None = None,
        registration: ProjectRegistrationResult | None = None,
        event_id: str = "codex-event-1",
        path: str = "src/model.py",
        operation: str = "modified",
    ) -> None:
        selected_service = service or self.service
        selected_registration = registration or self.registration
        selected_service.host_event_submit(
            selected_registration.project_id,
            event_id=event_id,
            producer="codex",
            occurred_at="2026-07-16T07:59:00Z",
            operation=operation,
            paths=[path],
            clock=self.clock,
        )

    def reconcile(
        self,
        *,
        service: ResearchCoreService | None = None,
        registration: ProjectRegistrationResult | None = None,
        dirty_paths: object = (),
    ) -> ProjectReconciliationResult:
        selected_service = service or self.service
        selected_registration = registration or self.registration
        return selected_service.project_reconcile(
            selected_registration.project_id,
            dirty_paths=dirty_paths,
            clock=self.clock,
        )

    def invoke_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = project_main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_schema_v1_state_and_result_are_exact_and_path_free(self) -> None:
        result = self.reconcile()
        payload = result.as_dict()

        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "kind",
                "reconciliation_version",
                "project_id",
                "status",
                "mode",
                "source_of_truth",
                "run",
                "manifest",
                "coverage",
                "hints",
                "acknowledgement",
            },
        )
        self.assertEqual(payload["schema_version"], RECONCILIATION_SCHEMA_VERSION)
        self.assertEqual(payload["kind"], RECONCILIATION_RESULT_KIND)
        self.assertEqual(payload["reconciliation_version"], RECONCILIATION_VERSION)
        self.assertEqual(payload["status"], "reconciled")
        self.assertEqual(payload["mode"], RECONCILIATION_MODE)
        self.assertEqual(payload["source_of_truth"], RECONCILIATION_SOURCE_OF_TRUTH)
        self.assertEqual(payload["run"]["status"], "paused")
        self.assertEqual(payload["run"]["through_stage"], "classify")
        self.assertEqual(
            payload["run"]["stages"],
            {
                "register": "succeeded",
                "inventory": "succeeded",
                "classify": "succeeded",
            },
        )
        self.assertEqual(payload["hints"]["status"], "absent")
        self.assertEqual(payload["acknowledgement"]["state_revision"], 1)

        state = json.loads(self.layout.reconciliation_state_file.read_text("utf-8"))
        self.assertEqual(
            set(state),
            {
                "schema_version",
                "kind",
                "reconciliation_version",
                "project_id",
                "revision",
                "acknowledged_through_sequence",
                "acknowledged_ledger_sha256",
                "last_success",
            },
        )
        self.assertEqual(state["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(state["kind"], RECONCILIATION_STATE_KIND)
        self.assertEqual(state["reconciliation_version"], RECONCILIATION_VERSION)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(
            set(state["last_success"]),
            {
                "run_id",
                "completed_at",
                "manifest_version",
                "manifest_scan_generation",
                "manifest_sha256",
                "coverage_report_version",
                "coverage_sha256",
                "coverage_failure_count",
                "snapshot_event_count",
                "snapshot_last_sequence",
                "snapshot_ledger_sha256",
                "pending_event_count",
                "pending_dirty_path_count",
                "hint_status",
                "explicit_dirty_path_count",
                "queued_dirty_path_count",
                "projection_rebuilt",
            },
        )
        self.assertEqual(state["last_success"]["completed_at"], "2026-07-16T08:00:00Z")

        serialized = json.dumps(payload, ensure_ascii=False)
        state_serialized = json.dumps(state, ensure_ascii=False)
        for forbidden in (
            str(self.workspace.resolve()),
            str(self.project.resolve()),
            "src/model.py",
            "config/train.yaml",
        ):
            self.assertNotIn(forbidden, serialized)
            self.assertNotIn(forbidden, state_serialized)

    def test_no_event_full_scan_preserves_source_curated_knowledge_and_locality(
        self,
    ) -> None:
        self.layout.knowledge_root.mkdir(parents=True, exist_ok=True)
        (self.layout.knowledge_root / "overview.md").write_text(
            "---\ntitle: Stable user note\n---\n\nDo not overwrite.\n",
            encoding="utf-8",
            newline="\n",
        )
        source_before = self.tree_snapshot(self.project)
        knowledge_before = self.tree_snapshot(self.layout.knowledge_root)
        ledger_before = (
            self.layout.events_file.read_bytes()
            if self.layout.events_file.exists()
            else None
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "tools._utils.call_llm",
                    side_effect=AssertionError("reconciliation attempted an LLM call"),
                )
            )
            stack.enter_context(
                patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError(
                        "reconciliation attempted network access"
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    urllib.request,
                    "urlopen",
                    side_effect=AssertionError("reconciliation attempted URL access"),
                )
            )
            stack.enter_context(
                patch.object(
                    webbrowser,
                    "open",
                    side_effect=AssertionError("reconciliation opened a browser"),
                )
            )
            stack.enter_context(
                patch(
                    "tools.research_core.submit_host_event",
                    side_effect=AssertionError("reconciliation invoked a Hook event"),
                )
            )
            result = self.reconcile()

        self.assertEqual(result.hint_status, "absent")
        self.assertEqual(result.snapshot_event_count, 0)
        self.assertEqual(result.newly_acknowledged_event_count, 0)
        self.assertEqual(self.tree_snapshot(self.project), source_before)
        self.assertEqual(
            self.tree_snapshot(self.layout.knowledge_root), knowledge_before
        )
        ledger_after = (
            self.layout.events_file.read_bytes()
            if self.layout.events_file.exists()
            else None
        )
        self.assertEqual(ledger_after, ledger_before)

    def test_queued_event_is_acknowledged_without_rewriting_ledger(self) -> None:
        self.submit()
        ledger_before = self.layout.events_file.read_bytes()

        first = self.reconcile()
        self.assertEqual(first.hint_status, "ledger-only")
        self.assertEqual(first.pending_event_count, 1)
        self.assertEqual(first.previous_acknowledged_through_sequence, 0)
        self.assertEqual(first.acknowledged_through_sequence, 1)
        self.assertEqual(first.newly_acknowledged_event_count, 1)
        self.assertEqual(first.remaining_event_count, 0)
        self.assertEqual(self.layout.events_file.read_bytes(), ledger_before)

        second = self.reconcile()
        self.assertEqual(second.hint_status, "absent")
        self.assertEqual(second.pending_event_count, 0)
        self.assertEqual(second.previous_acknowledged_through_sequence, 1)
        self.assertEqual(second.acknowledged_through_sequence, 1)
        self.assertEqual(second.newly_acknowledged_event_count, 0)
        self.assertEqual(second.state_revision, 2)
        self.assertEqual(self.layout.events_file.read_bytes(), ledger_before)

    def test_event_appended_during_scan_remains_pending(self) -> None:
        appended = False

        def run_factory(project_id: str, *, through_stage: str):
            nonlocal appended
            run = self.service.project_run_start(
                project_id,
                through_stage=through_stage,
            )
            self.submit(event_id="event-during-scan", path="config/train.yaml")
            appended = True
            return run

        result = reconcile_project(
            self.workspace,
            self.registration.project_id,
            run_factory=run_factory,
            coverage_factory=self.service.coverage,
            clock=self.clock,
        )

        self.assertTrue(appended)
        self.assertEqual(result.snapshot_event_count, 0)
        self.assertEqual(result.acknowledged_through_sequence, 0)
        self.assertEqual(result.newly_acknowledged_event_count, 0)
        self.assertEqual(result.remaining_event_count, 1)
        self.assertEqual(result.remaining_dirty_path_count, 1)
        state = load_reconciliation_state(
            self.layout.reconciliation_state_file,
            project_id=self.registration.project_id,
        )
        self.assertEqual(state.acknowledged_through_sequence, 0)

        follow_up = self.reconcile()
        self.assertEqual(follow_up.pending_event_count, 1)
        self.assertEqual(follow_up.acknowledged_through_sequence, 1)
        self.assertEqual(follow_up.remaining_event_count, 0)

    def test_inventory_and_classification_failures_preserve_checkpoint_and_events(
        self,
    ) -> None:
        self.submit(event_id="baseline")
        self.reconcile()
        self.submit(event_id="still-pending", path="config/train.yaml")
        state_before = self.layout.reconciliation_state_file.read_bytes()
        ledger_before = self.layout.events_file.read_bytes()

        with patch.object(
            ResearchCoreService,
            "scan",
            side_effect=RuntimeError("simulated inventory failure"),
        ):
            with self.assertRaises(ReconciliationRunError):
                self.reconcile()
        self.assertEqual(
            self.layout.reconciliation_state_file.read_bytes(), state_before
        )
        self.assertEqual(self.layout.events_file.read_bytes(), ledger_before)

        with patch.object(
            ResearchCoreService,
            "coverage",
            side_effect=RuntimeError("simulated classification failure"),
        ):
            with self.assertRaises(ReconciliationRunError):
                self.reconcile()
        self.assertEqual(
            self.layout.reconciliation_state_file.read_bytes(), state_before
        )
        self.assertEqual(self.layout.events_file.read_bytes(), ledger_before)

    def test_run_boundary_rejects_later_or_malformed_stage_ledgers(self) -> None:
        valid_run = self.service.project_run_start(
            self.registration.project_id,
            through_stage="classify",
        )
        corruptions = {
            "later-succeeded": lambda stages: stages[3].update(status="succeeded"),
            "duplicate-stage": lambda stages: stages[-1].update(
                stage_id="extract"
            ),
            "unexpected-stage": lambda stages: stages[-1].update(
                stage_id="unexpected"
            ),
            "missing-stage": lambda stages: stages.pop(),
        }
        for label, corrupt in corruptions.items():
            with self.subTest(corruption=label):
                record = deepcopy(valid_run.record)
                corrupt(record["stages"])
                fake_run = SimpleNamespace(record=record, status="paused")
                with self.assertRaises(ReconciliationRunError):
                    reconciliation_module._stage_statuses(fake_run)

        self.submit(event_id="pending-invalid-stage-ledger")
        ledger_before = self.layout.events_file.read_bytes()
        record = deepcopy(valid_run.record)
        record["stages"][3]["status"] = "succeeded"
        fake_run = SimpleNamespace(record=record, status="paused")
        with self.assertRaisesRegex(
            ReconciliationRunError,
            "must remain pending",
        ):
            reconcile_project(
                self.workspace,
                self.registration.project_id,
                run_factory=lambda *_args, **_kwargs: fake_run,
                coverage_factory=self.service.coverage,
                clock=self.clock,
            )
        self.assertFalse(self.layout.reconciliation_state_file.exists())
        self.assertEqual(self.layout.events_file.read_bytes(), ledger_before)

    def test_positive_coverage_failure_count_preserves_checkpoint_and_events(
        self,
    ) -> None:
        self.submit(event_id="baseline")
        self.reconcile()
        self.submit(event_id="still-pending", path="config/train.yaml")
        state_before = self.layout.reconciliation_state_file.read_bytes()
        ledger_before = self.layout.events_file.read_bytes()
        observed_failure_counts: list[int] = []

        def failed_run_factory(project_id: str, *, through_stage: str):
            run = self.service.project_run_start(
                project_id,
                through_stage=through_stage,
            )
            self.mark_manifest_file_failed(
                self.layout.manifest_file,
                "README.md",
            )
            coverage = self.service.coverage(project_id)
            observed_failure_counts.append(
                coverage.report["totals"]["failed_file_count"]
            )
            return self.bind_run_to_current_artifacts(
                run,
                manifest_file=self.layout.manifest_file,
                coverage_file=coverage.report_file,
            )

        with self.assertRaisesRegex(
            ReconciliationRunError,
            "coverage contains failed files",
        ):
            reconcile_project(
                self.workspace,
                self.registration.project_id,
                run_factory=failed_run_factory,
                coverage_factory=self.service.coverage,
                clock=self.clock,
            )

        self.assertEqual(observed_failure_counts, [1])
        self.assertEqual(
            self.layout.reconciliation_state_file.read_bytes(), state_before
        )
        self.assertEqual(self.layout.events_file.read_bytes(), ledger_before)
        state = load_reconciliation_state(
            self.layout.reconciliation_state_file,
            project_id=self.registration.project_id,
        )
        self.assertEqual(state.acknowledged_through_sequence, 1)

    def test_malformed_and_stale_current_queue_are_rebuilt(self) -> None:
        for suffix, corrupt in (
            ("malformed", b"{not-json"),
            ("stale", None),
        ):
            with self.subTest(queue=suffix):
                service, registration, _ = self.make_fixture(f"queue-{suffix}")
                self.submit(
                    service=service,
                    registration=registration,
                    event_id=f"event-{suffix}",
                )
                queue_file = registration.layout.dirty_paths_file
                if corrupt is not None:
                    queue_file.write_bytes(corrupt)
                else:
                    payload = json.loads(queue_file.read_text("utf-8"))
                    payload["ledger_event_count"] = 0
                    payload["ledger_sha256"] = "0" * 64
                    queue_file.write_text(
                        json.dumps(payload, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )

                result = self.reconcile(service=service, registration=registration)
                rebuilt = json.loads(queue_file.read_text("utf-8"))
                self.assertTrue(result.projection_rebuilt)
                self.assertEqual(rebuilt["schema_version"], 1)
                self.assertEqual(rebuilt["ledger_event_count"], 1)
                self.assertEqual(rebuilt["ledger_last_sequence"], 1)
                self.assertEqual(rebuilt["dirty_path_count"], 1)
                self.assertEqual(rebuilt["dirty_paths"][0]["path"], "src/model.py")

    def test_all_hint_statuses_are_reported_but_never_select_the_scan(self) -> None:
        cases = (
            ("absent", False, (), 0),
            ("ledger-only", True, (), 1),
            ("explicit-only", False, ("src/model.py",), 1),
            ("consistent", True, ("src/model.py",), 1),
            ("inconsistent", True, ("config/train.yaml",), 2),
        )
        for status, ledger, explicit, combined_count in cases:
            with self.subTest(status=status):
                service, registration, _ = self.make_fixture(f"hint-{status}")
                if ledger:
                    self.submit(
                        service=service,
                        registration=registration,
                        event_id=f"event-{status}",
                    )
                result = self.reconcile(
                    service=service,
                    registration=registration,
                    dirty_paths=explicit,
                )
                self.assertEqual(result.hint_status, status)
                self.assertEqual(result.combined_dirty_path_count, combined_count)
                self.assertEqual(result.run_status, "paused")
                self.assertEqual(
                    dict(result.stage_statuses),
                    {
                        "register": "succeeded",
                        "inventory": "succeeded",
                        "classify": "succeeded",
                    },
                )

    def test_missed_host_event_is_repaired_by_the_full_manifest_hash_scan(self) -> None:
        first = self.reconcile()
        first_rows = self.manifest_rows(self.layout.manifest_file)
        first_model = next(
            row for row in first_rows if row.get("path") == "src/model.py"
        )

        (self.project / "src" / "model.py").write_text(
            "VALUE = 2\n",
            encoding="utf-8",
            newline="\n",
        )
        second = self.reconcile()
        second_rows = self.manifest_rows(self.layout.manifest_file)
        second_model = next(
            row for row in second_rows if row.get("path") == "src/model.py"
        )

        self.assertEqual(first.hint_status, "absent")
        self.assertEqual(second.hint_status, "absent")
        self.assertEqual(second.snapshot_event_count, 0)
        self.assertGreater(
            second.manifest_scan_generation, first.manifest_scan_generation
        )
        self.assertNotEqual(
            first_model["content_sha256"],
            second_model["content_sha256"],
        )
        self.assertEqual(
            second_model["content_sha256"],
            hashlib.sha256(b"VALUE = 2\n").hexdigest(),
        )

    def test_prior_acknowledged_ledger_prefix_tampering_fails_closed(self) -> None:
        self.submit()
        self.reconcile()
        state_before = self.layout.reconciliation_state_file.read_bytes()
        row = json.loads(self.layout.events_file.read_text("utf-8"))
        row["operation"] = "created"
        tampered = self.canonical_json_line(row)
        self.layout.events_file.write_bytes(tampered)

        with self.assertRaises(ReconciliationStateError):
            self.reconcile()

        self.assertEqual(self.layout.events_file.read_bytes(), tampered)
        self.assertEqual(
            self.layout.reconciliation_state_file.read_bytes(), state_before
        )

    def test_legacy_and_future_checkpoint_schemas_fail_without_rewrite(self) -> None:
        cases: tuple[tuple[str, dict[str, object], type[BaseException]], ...] = (
            ("legacy", {"kind": "legacy-reconciliation"}, ReconciliationStateError),
            (
                "future",
                {"schema_version": CURRENT_SCHEMA_VERSION + 1},
                UnsupportedSchemaVersionError,
            ),
        )
        for suffix, payload, error_type in cases:
            with self.subTest(schema=suffix):
                service, registration, _ = self.make_fixture(f"state-{suffix}")
                state_file = registration.layout.reconciliation_state_file
                state_file.parent.mkdir(parents=True, exist_ok=True)
                state_file.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                before = state_file.read_bytes()

                with self.assertRaises(error_type):
                    self.reconcile(service=service, registration=registration)

                self.assertEqual(state_file.read_bytes(), before)
                self.assertFalse(registration.layout.manifest_file.exists())

    def test_state_strictly_rejects_duplicate_nonfinite_and_invalid_utf8(
        self,
    ) -> None:
        corruptions = (
            "duplicate-key",
            "nan",
            "positive-infinity",
            "negative-infinity",
            "invalid-utf8",
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                service, registration, _ = self.make_fixture(
                    f"strict-state-{corruption}"
                )
                self.reconcile(service=service, registration=registration)
                self.submit(
                    service=service,
                    registration=registration,
                    event_id=f"pending-{corruption}",
                )
                state_file = registration.layout.reconciliation_state_file
                valid = state_file.read_bytes()
                revision = b'  "revision": 1,\n'
                self.assertIn(revision, valid)
                if corruption == "duplicate-key":
                    corrupt = valid.replace(revision, revision + revision, 1)
                elif corruption == "nan":
                    corrupt = valid.replace(revision, b'  "revision": NaN,\n', 1)
                elif corruption == "positive-infinity":
                    corrupt = valid.replace(
                        revision,
                        b'  "revision": Infinity,\n',
                        1,
                    )
                elif corruption == "negative-infinity":
                    corrupt = valid.replace(
                        revision,
                        b'  "revision": -Infinity,\n',
                        1,
                    )
                else:
                    project_id = registration.project_id.encode("utf-8")
                    self.assertIn(project_id, valid)
                    corrupt = valid.replace(project_id, b"\xff" + project_id, 1)

                state_file.write_bytes(corrupt)
                ledger_before = registration.layout.events_file.read_bytes()
                with self.assertRaisesRegex(
                    ReconciliationStateError,
                    "malformed",
                ):
                    self.reconcile(service=service, registration=registration)

                self.assertEqual(state_file.read_bytes(), corrupt)
                self.assertEqual(
                    registration.layout.events_file.read_bytes(),
                    ledger_before,
                )

    def test_persisted_positive_coverage_failure_count_fails_closed(self) -> None:
        self.reconcile()
        self.submit(event_id="pending-invalid-last-success")
        state_file = self.layout.reconciliation_state_file
        document = json.loads(state_file.read_text(encoding="utf-8"))
        document["last_success"]["coverage_failure_count"] = 1
        invalid = (
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        state_file.write_bytes(invalid)
        ledger_before = self.layout.events_file.read_bytes()

        with self.assertRaisesRegex(
            ReconciliationStateError,
            "coverage_failure_count must be zero",
        ):
            self.reconcile()

        self.assertEqual(state_file.read_bytes(), invalid)
        self.assertEqual(self.layout.events_file.read_bytes(), ledger_before)

    def test_persisted_hint_counts_must_be_internally_consistent(self) -> None:
        self.reconcile()
        state_file = self.layout.reconciliation_state_file
        valid = json.loads(state_file.read_text(encoding="utf-8"))

        def mutate(**changes: object) -> bytes:
            document = deepcopy(valid)
            document["last_success"].update(changes)
            return (
                json.dumps(
                    document,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")

        cases = {
            "pending-queued-mismatch": (
                {"pending_dirty_path_count": 1},
                "pending and queued dirty-path counts must match",
            ),
            "explicit-limit": (
                {
                    "hint_status": "explicit-only",
                    "explicit_dirty_path_count": 257,
                },
                "explicit_dirty_path_count exceeds",
            ),
            "absent-with-explicit": (
                {"explicit_dirty_path_count": 1},
                "hint_status contradicts",
            ),
            "ledger-only-without-ledger-path": (
                {"hint_status": "ledger-only"},
                "hint_status contradicts",
            ),
            "explicit-only-without-explicit-path": (
                {"hint_status": "explicit-only"},
                "hint_status contradicts",
            ),
            "consistent-count-mismatch": (
                {
                    "pending_dirty_path_count": 1,
                    "queued_dirty_path_count": 1,
                    "explicit_dirty_path_count": 2,
                    "hint_status": "consistent",
                },
                "consistent hints must have matching path counts",
            ),
            "inconsistent-with-missing-ledger-path": (
                {
                    "explicit_dirty_path_count": 1,
                    "hint_status": "inconsistent",
                },
                "hint_status contradicts",
            ),
        }
        for label, (changes, message) in cases.items():
            with self.subTest(corruption=label):
                invalid = mutate(**changes)
                state_file.write_bytes(invalid)
                with self.assertRaisesRegex(ReconciliationStateError, message):
                    load_reconciliation_state(
                        state_file,
                        project_id=self.registration.project_id,
                    )
                self.assertEqual(state_file.read_bytes(), invalid)

    def test_atomic_state_write_failure_does_not_acknowledge_events(self) -> None:
        self.submit()
        run = self.service.project_run_start(
            self.registration.project_id,
            through_stage="classify",
        )
        coverage = self.service.coverage(self.registration.project_id)
        ledger_before = self.layout.events_file.read_bytes()

        with patch(
            "tools.project_reconciliation.os.replace",
            side_effect=OSError("simulated atomic replace failure"),
        ):
            with self.assertRaises(ReconciliationStateError):
                reconcile_project(
                    self.workspace,
                    self.registration.project_id,
                    run_factory=lambda *_args, **_kwargs: run,
                    coverage_factory=lambda _project_id: coverage,
                    clock=self.clock,
                )

        self.assertFalse(self.layout.reconciliation_state_file.exists())
        self.assertEqual(self.layout.events_file.read_bytes(), ledger_before)
        self.assertEqual(
            list(self.layout.indexes_dir.glob(".reconciliation-state.json.*.tmp")),
            [],
        )

        recovered = self.reconcile()
        self.assertEqual(recovered.newly_acknowledged_event_count, 1)
        self.assertEqual(recovered.acknowledged_through_sequence, 1)

    def test_reconciliation_lock_is_stable_persistent_and_advisory(self) -> None:
        lock = self.layout.reconciliation_lock_file
        self.assertEqual(lock, self.layout.machine_state_lock_file)
        self.assertFalse(lock.exists())
        started = threading.Event()
        finished = threading.Event()
        contender_errors: list[BaseException] = []

        def contender() -> None:
            started.set()
            try:
                with _exclusive_reconciliation_lock(
                    lock,
                    timeout_seconds=0.05,
                ):
                    contender_errors.append(
                        AssertionError("contended reconciliation lock was acquired")
                    )
            except BaseException as exc:
                contender_errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=contender)
        with _exclusive_reconciliation_lock(lock, timeout_seconds=1.0):
            held = os.stat(lock, follow_symlinks=False)
            thread.start()
            self.assertTrue(started.wait(timeout=1.0))
            self.assertTrue(finished.wait(timeout=2.0))

        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(contender_errors), 1)
        self.assertIsInstance(contender_errors[0], ReconciliationLockError)
        released = os.stat(lock, follow_symlinks=False)
        self.assertTrue(os.path.samestat(held, released))

        with _exclusive_reconciliation_lock(lock, timeout_seconds=1.0):
            reacquired = os.stat(lock, follow_symlinks=False)
            self.assertTrue(os.path.samestat(held, reacquired))

        final = os.stat(lock, follow_symlinks=False)
        self.assertTrue(os.path.samestat(held, final))
        self.assertFalse(self.layout.reconciliation_state_file.exists())

    def test_competing_inventory_writer_cannot_overwrite_reconciliation_artifacts(
        self,
    ) -> None:
        self.submit(event_id="pending")
        start_writer = threading.Event()
        writer_finished = threading.Event()
        writer_errors: list[BaseException] = []

        def competing_writer() -> None:
            try:
                if not start_writer.wait(timeout=5.0):
                    raise AssertionError(
                        "reconciliation never reached checkpoint commit"
                    )
                inventory_project(
                    self.workspace,
                    self.registration.project_id,
                    lock_timeout_seconds=0.05,
                )
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                writer_finished.set()

        original_assert = reconciliation_module._assert_artifact_snapshot_current

        def wait_for_competing_writer(registration, snapshot) -> None:
            start_writer.set()
            if not writer_finished.wait(timeout=5.0):
                raise AssertionError("competing inventory writer did not finish")
            original_assert(registration, snapshot)

        thread = threading.Thread(target=competing_writer)
        thread.start()
        try:
            with patch.object(
                reconciliation_module,
                "_assert_artifact_snapshot_current",
                side_effect=wait_for_competing_writer,
            ):
                result = self.reconcile()
        finally:
            start_writer.set()
            thread.join(timeout=5.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(writer_errors), 1)
        self.assertIsInstance(writer_errors[0], ProjectInventoryError)
        self.assertIn("timed out waiting", str(writer_errors[0]))
        self.assertEqual(result.acknowledged_through_sequence, 1)
        self.assertEqual(
            hashlib.sha256(self.layout.manifest_file.read_bytes()).hexdigest(),
            result.manifest_sha256,
        )
        coverage_file = self.layout.indexes_dir / "coverage-report.json"
        self.assertEqual(
            hashlib.sha256(coverage_file.read_bytes()).hexdigest(),
            result.coverage_sha256,
        )
        state = load_reconciliation_state(
            self.layout.reconciliation_state_file,
            project_id=self.registration.project_id,
        )
        self.assertEqual(state.last_success["manifest_sha256"], result.manifest_sha256)
        self.assertEqual(state.last_success["coverage_sha256"], result.coverage_sha256)

    def test_artifact_replacement_before_checkpoint_fails_without_acknowledgement(
        self,
    ) -> None:
        for artifact_name in ("manifest", "coverage"):
            with self.subTest(artifact=artifact_name):
                service, registration, _ = self.make_fixture(
                    f"artifact-replacement-{artifact_name}"
                )
                self.submit(
                    service=service,
                    registration=registration,
                    event_id=f"baseline-{artifact_name}",
                )
                self.reconcile(service=service, registration=registration)
                self.submit(
                    service=service,
                    registration=registration,
                    event_id=f"pending-{artifact_name}",
                    path="config/train.yaml",
                )
                state_before = (
                    registration.layout.reconciliation_state_file.read_bytes()
                )
                ledger_before = registration.layout.events_file.read_bytes()
                validated = threading.Event()
                replaced = threading.Event()
                replacement_errors: list[BaseException] = []
                artifact_file = (
                    registration.layout.manifest_file
                    if artifact_name == "manifest"
                    else registration.layout.indexes_dir / "coverage-report.json"
                )

                def replace_artifact() -> None:
                    try:
                        if not validated.wait(timeout=5.0):
                            raise AssertionError(
                                "reconciliation did not validate its artifacts"
                            )
                        original = artifact_file.read_bytes()
                        if artifact_name == "manifest":
                            replacement = b"".join(
                                (
                                    json.dumps(
                                        row,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    )
                                    + "\n"
                                ).encode("utf-8")
                                for row in self.manifest_rows(artifact_file)
                            )
                        else:
                            report = json.loads(original.decode("utf-8"))
                            replacement = (
                                json.dumps(
                                    report,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                    allow_nan=False,
                                )
                                + "\n"
                            ).encode("utf-8")
                        if replacement == original:
                            raise AssertionError("replacement bytes did not change")
                        temporary = artifact_file.with_name(
                            f".{artifact_file.name}.replacement"
                        )
                        temporary.write_bytes(replacement)
                        os.replace(temporary, artifact_file)
                    except BaseException as exc:
                        replacement_errors.append(exc)
                    finally:
                        replaced.set()

                original_validate = reconciliation_module._validate_coverage_artifacts

                def validate_then_replace(registration_value, coverage, run):
                    snapshot = original_validate(registration_value, coverage, run)
                    validated.set()
                    if not replaced.wait(timeout=5.0):
                        raise AssertionError("artifact replacement did not finish")
                    return snapshot

                thread = threading.Thread(target=replace_artifact)
                thread.start()
                try:
                    with patch.object(
                        reconciliation_module,
                        "_validate_coverage_artifacts",
                        side_effect=validate_then_replace,
                    ):
                        with self.assertRaisesRegex(
                            ReconciliationSnapshotError,
                            "artifacts changed before checkpoint commit",
                        ):
                            reconcile_project(
                                self.workspace,
                                registration.project_id,
                                run_factory=service.project_run_start,
                                coverage_factory=service.coverage,
                                clock=self.clock,
                            )
                finally:
                    validated.set()
                    thread.join(timeout=5.0)

                self.assertFalse(thread.is_alive())
                self.assertEqual(replacement_errors, [])
                self.assertEqual(
                    registration.layout.reconciliation_state_file.read_bytes(),
                    state_before,
                )
                self.assertEqual(
                    registration.layout.events_file.read_bytes(), ledger_before
                )
                state = load_reconciliation_state(
                    registration.layout.reconciliation_state_file,
                    project_id=registration.project_id,
                )
                self.assertEqual(state.acknowledged_through_sequence, 1)

    def test_explicit_dirty_paths_are_bounded_project_relative_hints(self) -> None:
        for invalid in (
            "../outside.txt",
            "/absolute.txt",
            ".git/config",
            "C:/absolute.txt",
        ):
            with self.subTest(path=invalid):
                with self.assertRaises(ReconciliationHintError):
                    self.reconcile(dirty_paths=[invalid])
        with self.assertRaises(ReconciliationHintError):
            self.reconcile(dirty_paths=[f"src/file-{index}.py" for index in range(257)])

        consumed: list[int] = []

        def duplicate_stream() -> object:
            for index in range(10_000):
                consumed.append(index)
                yield "src/model.py"

        with self.assertRaisesRegex(ReconciliationHintError, "supplied paths"):
            self.reconcile(dirty_paths=duplicate_stream())
        self.assertEqual(len(consumed), 257)
        self.assertFalse(self.layout.reconciliation_state_file.exists())

    def test_core_and_cli_json_contracts_match(self) -> None:
        result = self.reconcile(dirty_paths=["src/model.py"])
        with patch("tools.project.ResearchCoreService") as service_type:
            service_type.return_value.project_reconcile.return_value = result
            code, stdout, stderr = self.invoke_cli(
                [
                    "reconcile",
                    self.registration.project_id,
                    "--dirty-path",
                    "src/model.py",
                    "--workspace-root",
                    str(self.workspace),
                    "--json",
                ]
            )

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {"ok": True, **result.as_dict()})
        service_type.assert_called_once_with(str(self.workspace))
        service_type.return_value.project_reconcile.assert_called_once_with(
            project_id=self.registration.project_id,
            dirty_paths=["src/model.py"],
        )

    def test_direct_script_cli_reconciles_without_hooks(self) -> None:
        service, registration, project = self.make_fixture("direct-cli")
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "reconcile",
                registration.project_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["hints"]["status"], "absent")
        self.assertEqual(payload["run"]["status"], "paused")
        for value in self.nested_strings(payload):
            self.assertNotIn(str(project.resolve()), value)
        self.assertEqual(
            service.project_context(registration.project_id).project_id,
            registration.project_id,
        )

    def test_atomic_writer_keeps_previous_state_when_replace_fails(self) -> None:
        self.reconcile()
        previous_bytes = self.layout.reconciliation_state_file.read_bytes()
        previous = load_reconciliation_state(
            self.layout.reconciliation_state_file,
            project_id=self.registration.project_id,
        )
        updated = ReconciliationState(
            project_id=previous.project_id,
            revision=previous.revision + 1,
            acknowledged_through_sequence=previous.acknowledged_through_sequence,
            acknowledged_ledger_sha256=previous.acknowledged_ledger_sha256,
            last_success=previous.last_success,
        )

        with patch(
            "tools.project_reconciliation.os.replace",
            side_effect=OSError("simulated replace failure"),
        ):
            with self.assertRaises(ReconciliationStateError):
                _write_reconciliation_state_atomic(
                    self.layout.reconciliation_state_file,
                    updated,
                )

        self.assertEqual(
            self.layout.reconciliation_state_file.read_bytes(),
            previous_bytes,
        )
        self.assertEqual(
            list(self.layout.indexes_dir.glob(".reconciliation-state.json.*.tmp")),
            [],
        )


if __name__ == "__main__":
    unittest.main()
