from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from tools import coverage_report as coverage_report_module
from tools.advisory_lock import AdvisoryFileLock, AdvisoryLockTimeoutError
from tools.coverage_report import (
    COVERAGE_REPORT_KIND,
    COVERAGE_REPORT_SCHEMA_VERSION,
    COVERAGE_REPORT_VERSION,
    CoverageReportError,
    generate_coverage_report,
)
from tools.file_state import FILE_STATE_KIND, FILE_STATE_SCHEMA_VERSION
from tools.project_inventory import (
    CLASSIFICATION_PROJECT_MANIFEST_VERSION,
    PROJECT_MANIFEST_VERSION,
    ProjectManifestError,
    inventory_project,
)
from tools.project_layout import LayoutError, UnsupportedSchemaVersionError
from tools.project_registry import register_project


REPO_ROOT = Path(__file__).parent.parent


class CoverageReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.project = self.root / "research-project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "\u5b9e\u9a8c").mkdir()
        (self.project / "README.md").write_text(
            "# Coverage Study\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.project / "src" / "model.py").write_text(
            "VALUE = 7\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.project / "\u5b9e\u9a8c" / "\u5931\u8d25.txt").write_text(
            "measurement\n",
            encoding="utf-8",
            newline="\n",
        )
        self.registration = register_project(
            self.workspace,
            self.project,
            project_id="coverage-study",
        )
        self.inventory = inventory_project(
            self.workspace,
            self.registration.project_id,
        )

    def source_snapshot(self) -> dict[str, tuple[str, int]]:
        snapshot: dict[str, tuple[str, int]] = {}
        for directory, dirnames, filenames in os.walk(self.project):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            for name in filenames:
                path = base / name
                snapshot[path.relative_to(self.project).as_posix()] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mtime_ns,
                )
        return snapshot

    def manifest_rows(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.inventory.manifest_file.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

    def write_manifest_rows(self, rows: list[dict[str, object]]) -> None:
        self.inventory.manifest_file.write_text(
            "".join(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for row in rows
            ),
            encoding="utf-8",
            newline="\n",
        )

    def mark_failed(self, relative_path: str) -> None:
        rows = self.manifest_rows()
        for row in rows[1:]:
            if row["record_type"] == "file" and row["path"] == relative_path:
                row["file_state"] = {
                    "schema_version": FILE_STATE_SCHEMA_VERSION,
                    "kind": FILE_STATE_KIND,
                    "processing_status": "failed",
                    "read_depth": "normal_read",
                    "reason_code": "deterministic-read-failed",
                    "reason": "deterministic extraction could not read the file",
                }
                break
        else:
            self.fail(f"missing Manifest file row {relative_path!r}")

        status_counts: Counter[str] = Counter()
        depth_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        for row in rows[1:]:
            if row["record_type"] != "file":
                continue
            state = row["file_state"]
            status_counts[state["processing_status"]] += 1
            depth_counts[state["read_depth"]] += 1
            reason_counts[state["reason_code"]] += 1
        summary = rows[0]["file_state_summary"]
        summary["processing_statuses"] = dict(sorted(status_counts.items()))
        summary["read_depths"] = dict(sorted(depth_counts.items()))
        summary["reasons"] = dict(sorted(reason_counts.items()))
        self.write_manifest_rows(rows)

    def assert_machine_state_lock_contended(self, lock_file: Path) -> None:
        started = threading.Event()
        errors: list[BaseException] = []

        def contend() -> None:
            started.set()
            try:
                with AdvisoryFileLock(lock_file, timeout_seconds=0.05):
                    errors.append(AssertionError("contender acquired the shared lock"))
            except BaseException as exc:
                errors.append(exc)

        contender = threading.Thread(target=contend, name="coverage-lock-contender")
        contender.start()
        self.assertTrue(started.wait(timeout=1.0))
        contender.join(timeout=2.0)
        self.assertFalse(contender.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AdvisoryLockTimeoutError)

    def test_coverage_holds_shared_lock_across_manifest_read_and_report_write(
        self,
    ) -> None:
        phases = (
            ("load_project_manifest", "manifest-read"),
            ("_write_atomic_json", "report-write"),
        )

        for target_name, phase in phases:
            with self.subTest(phase=phase):
                entered = threading.Event()
                release = threading.Event()
                results: list[object] = []
                errors: list[BaseException] = []
                original = getattr(coverage_report_module, target_name)

                def blocked_phase(
                    *args: object,
                    _original: object = original,
                    **kwargs: object,
                ) -> object:
                    entered.set()
                    if not release.wait(timeout=5.0):
                        raise AssertionError(f"coverage {phase} was not released")
                    return _original(*args, **kwargs)  # type: ignore[operator]

                def run_coverage() -> None:
                    try:
                        results.append(
                            generate_coverage_report(
                                self.workspace,
                                self.registration.project_id,
                            )
                        )
                    except BaseException as exc:
                        errors.append(exc)

                with patch.object(
                    coverage_report_module,
                    target_name,
                    side_effect=blocked_phase,
                ):
                    worker = threading.Thread(
                        target=run_coverage,
                        name=f"coverage-{phase}",
                    )
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(timeout=2.0))
                        self.assert_machine_state_lock_contended(
                            self.registration.layout.machine_state_lock_file
                        )
                    finally:
                        release.set()
                        worker.join(timeout=5.0)

                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(len(results), 1)

    def test_coverage_is_reentrant_under_same_thread_machine_state_lock(self) -> None:
        with AdvisoryFileLock(
            self.registration.layout.machine_state_lock_file,
            timeout_seconds=0.2,
        ):
            result = generate_coverage_report(
                self.workspace,
                self.registration.project_id,
                lock_timeout_seconds=0.05,
            )

        self.assertTrue(result.report_file.is_file())
        self.assertEqual(
            result.report["manifest"]["scan_generation"],
            self.inventory.scan_generation,
        )

    def test_coverage_contender_timeout_uses_coverage_domain_error(self) -> None:
        started = threading.Event()
        errors: list[BaseException] = []

        def contend() -> None:
            started.set()
            try:
                generate_coverage_report(
                    self.workspace,
                    self.registration.project_id,
                    lock_timeout_seconds=0.05,
                )
            except BaseException as exc:
                errors.append(exc)

        contender = threading.Thread(target=contend, name="coverage-domain-contender")
        with AdvisoryFileLock(self.registration.layout.machine_state_lock_file):
            contender.start()
            self.assertTrue(started.wait(timeout=1.0))
            contender.join(timeout=2.0)
            self.assertFalse(contender.is_alive())

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CoverageReportError)
        self.assertIsInstance(errors[0].__cause__, AdvisoryLockTimeoutError)
        self.assertIn("machine-state lock", str(errors[0]))
        self.assertFalse(
            (
                self.registration.layout.indexes_dir
                / coverage_report_module.COVERAGE_REPORT_FILENAME
            ).exists()
        )

    def test_report_reconciles_every_axis_and_is_byte_stable(self) -> None:
        source_before = self.source_snapshot()

        first = generate_coverage_report(
            self.workspace,
            self.registration.project_id,
        )
        first_bytes = first.report_file.read_bytes()
        second = generate_coverage_report(
            self.workspace,
            self.registration.project_id,
        )

        self.assertEqual(first.report, second.report)
        self.assertEqual(first_bytes, second.report_file.read_bytes())
        self.assertEqual(source_before, self.source_snapshot())
        report = first.report
        self.assertEqual(report["schema_version"], COVERAGE_REPORT_SCHEMA_VERSION)
        self.assertEqual(report["kind"], COVERAGE_REPORT_KIND)
        self.assertEqual(report["report_version"], COVERAGE_REPORT_VERSION)
        self.assertEqual(
            report["manifest"]["manifest_version"],
            PROJECT_MANIFEST_VERSION,
        )
        self.assertEqual(report["totals"]["file_count"], 3)
        self.assertEqual(report["totals"]["failed_file_count"], 0)
        self.assertEqual(report["failures"], [])

        expected = {
            "file_count": report["totals"]["file_count"],
            "byte_count": report["totals"]["byte_count"],
        }
        self.assertEqual(report["reconciliation"]["manifest"], expected)
        for axis in (
            "research_role",
            "processing_status",
            "read_depth",
            "reason",
        ):
            self.assertEqual(report["reconciliation"]["axes"][axis], expected)
            self.assertEqual(
                sum(
                    bucket["file_count"]
                    for bucket in report["coverage"][axis].values()
                ),
                expected["file_count"],
            )
            self.assertEqual(
                sum(
                    bucket["byte_count"]
                    for bucket in report["coverage"][axis].values()
                ),
                expected["byte_count"],
            )

        serialized = (
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        self.assertEqual(first_bytes, serialized)
        self.assertTrue(
            first.report_file.is_relative_to(self.registration.layout.machine_root)
        )
        self.assertFalse(
            first.report_file.is_relative_to(self.registration.layout.knowledge_root)
        )

    def test_failed_rows_preserve_project_relative_paths_and_reasons(self) -> None:
        relative_path = "\u5b9e\u9a8c/\u5931\u8d25.txt"
        self.mark_failed(relative_path)

        result = generate_coverage_report(
            self.workspace,
            self.registration.project_id,
        )

        self.assertEqual(result.report["totals"]["failed_file_count"], 1)
        self.assertEqual(
            result.report["failures"],
            [
                {
                    "path": relative_path,
                    "byte_count": len("measurement\n".encode()),
                    "research_role": "documentation",
                    "processing_status": "failed",
                    "read_depth": "normal_read",
                    "reason_code": "deterministic-read-failed",
                    "reason": "deterministic extraction could not read the file",
                }
            ],
        )
        reason_bucket = result.report["coverage"]["reason"][
            "deterministic-read-failed"
        ]
        self.assertEqual(reason_bucket["file_count"], 1)
        self.assertEqual(
            reason_bucket["details"],
            ["deterministic extraction could not read the file"],
        )

    def test_corrupt_or_future_manifest_fails_closed_and_preserves_report(self) -> None:
        valid = generate_coverage_report(
            self.workspace,
            self.registration.project_id,
        )
        previous_report = valid.report_file.read_bytes()
        rows = self.manifest_rows()
        rows[1]["file_state"]["schema_version"] = FILE_STATE_SCHEMA_VERSION + 1
        self.write_manifest_rows(rows)
        corrupt_manifest = self.inventory.manifest_file.read_bytes()

        with self.assertRaises(ProjectManifestError):
            generate_coverage_report(
                self.workspace,
                self.registration.project_id,
            )

        self.assertEqual(self.inventory.manifest_file.read_bytes(), corrupt_manifest)
        self.assertEqual(valid.report_file.read_bytes(), previous_report)

    def test_legacy_manifest_artifact_is_validated_but_rejected(self) -> None:
        rows = self.manifest_rows()
        for row in rows:
            row["manifest_version"] = CLASSIFICATION_PROJECT_MANIFEST_VERSION
            if row["record_type"] == "file":
                del row["file_state"]
        del rows[0]["file_state_summary"]
        self.write_manifest_rows(rows)
        before = self.inventory.manifest_file.read_bytes()

        with self.assertRaisesRegex(
            ProjectManifestError,
            "operation requires manifest_version",
        ):
            generate_coverage_report(
                self.workspace,
                self.registration.project_id,
            )

        self.assertEqual(self.inventory.manifest_file.read_bytes(), before)
        self.assertFalse(
            (
                self.registration.layout.indexes_dir
                / "coverage-report.json"
            ).exists()
        )

    def test_existing_report_schema_fails_closed_without_rewrite(self) -> None:
        valid = generate_coverage_report(
            self.workspace,
            self.registration.project_id,
        )
        report_file = valid.report_file

        future = {
            "schema_version": COVERAGE_REPORT_SCHEMA_VERSION + 1,
            "kind": COVERAGE_REPORT_KIND,
            "report_version": COVERAGE_REPORT_VERSION,
            "project_id": self.registration.project_id,
        }
        future_bytes = (json.dumps(future) + "\n").encode("utf-8")
        report_file.write_bytes(future_bytes)
        with self.assertRaises(UnsupportedSchemaVersionError):
            generate_coverage_report(self.workspace, self.registration.project_id)
        self.assertEqual(report_file.read_bytes(), future_bytes)

        malformed_cases = (
            b'{"schema_version":1,"schema_version":1}\n',
            b'{"schema_version":1,"value":NaN}\n',
            b'{"schema_version":1,"value":"\xff"}\n',
            b'{"kind":"legacy-coverage"}\n',
        )
        for payload in malformed_cases:
            with self.subTest(payload=payload):
                report_file.write_bytes(payload)
                with self.assertRaises(LayoutError):
                    generate_coverage_report(
                        self.workspace,
                        self.registration.project_id,
                    )
                self.assertEqual(report_file.read_bytes(), payload)

        wrong_current = {
            "schema_version": COVERAGE_REPORT_SCHEMA_VERSION,
            "kind": "other-record",
            "report_version": COVERAGE_REPORT_VERSION,
            "project_id": self.registration.project_id,
        }
        wrong_bytes = (json.dumps(wrong_current) + "\n").encode("utf-8")
        report_file.write_bytes(wrong_bytes)
        with self.assertRaises(CoverageReportError):
            generate_coverage_report(self.workspace, self.registration.project_id)
        self.assertEqual(report_file.read_bytes(), wrong_bytes)

    def test_project_cli_emits_json_and_writes_only_machine_state(self) -> None:
        source_before = self.source_snapshot()
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "coverage",
                self.registration.project_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["project_id"], self.registration.project_id)
        self.assertEqual(payload["report"]["kind"], COVERAGE_REPORT_KIND)
        self.assertTrue(Path(payload["report_file"]).is_file())
        self.assertEqual(source_before, self.source_snapshot())


if __name__ == "__main__":
    unittest.main()
