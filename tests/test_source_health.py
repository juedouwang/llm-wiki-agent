from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.evidence_registry import (
    excerpt_sha256,
    load_evidence_registry,
    register_evidence,
)
from tools.extraction_schema import LineRangeLocator
from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.source_health import (
    SOURCE_HEALTH_EVIDENCE_KIND,
    SOURCE_HEALTH_REPORT_KIND,
    SOURCE_HEALTH_SCHEMA_VERSION,
    SOURCE_HEALTH_SOURCE_KIND,
    SOURCE_HEALTH_VERSION,
    evaluate_source_health,
)
from tools.source_registry import load_source_registry, sync_source_registry


REPO_ROOT = Path(__file__).resolve().parent.parent


class SourceHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.project = self.root / "research-project"
        self.project.mkdir()
        self.source_path = self.project / "notes.txt"
        self.source_bytes = b"alpha\nbeta\ngamma\n"
        self.source_path.write_bytes(self.source_bytes)
        self.registration = register_project(
            self.workspace,
            self.project,
            project_id="source-health-study",
        )
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)

    def source(self):
        registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        return registry.by_source_id[next(iter(registry.by_source_id))]

    def register_line_evidence(
        self,
        start_line: int,
        end_line: int,
        excerpt: str,
    ):
        source = self.source()
        assert source.current_content_hash is not None
        return register_evidence(
            self.workspace,
            self.registration.project_id,
            source_id=source.source_id,
            content_hash=source.current_content_hash,
            locator=LineRangeLocator(start_line, end_line),
            excerpt=excerpt,
        ).evidence

    def source_snapshot(self) -> tuple[tuple[str, ...], dict[str, tuple[str, int]]]:
        directories: list[str] = []
        files: dict[str, tuple[str, int]] = {}
        for directory, dirnames, filenames in os.walk(self.project):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            for name in dirnames:
                path = base / name
                if not path.is_symlink():
                    directories.append(path.relative_to(self.project).as_posix())
            for name in filenames:
                path = base / name
                if not path.is_symlink():
                    files[path.relative_to(self.project).as_posix()] = (
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        path.stat().st_mtime_ns,
                    )
        return tuple(sorted(directories)), files

    def assert_reconciled(self, result) -> None:
        payload = result.as_dict()
        source_totals = payload["source_totals"]
        evidence_totals = payload["evidence_totals"]
        self.assertEqual(
            source_totals["registry_count"],
            source_totals["classified_count"],
        )
        self.assertEqual(
            source_totals["registry_count"],
            sum(source_totals["status_counts"].values()),
        )
        self.assertEqual(
            evidence_totals["registry_count"],
            evidence_totals["classified_count"],
        )
        self.assertEqual(
            evidence_totals["registry_count"],
            sum(evidence_totals["status_counts"].values()),
        )
        source_registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        self.assertEqual(source_totals["registry_count"], len(source_registry.records))
        if self.registration.layout.evidence_file.exists():
            evidence_registry = load_evidence_registry(
                self.workspace,
                self.registration.project_id,
            )
            expected_evidence_count = len(evidence_registry.records)
        else:
            expected_evidence_count = 0
        self.assertEqual(evidence_totals["registry_count"], expected_evidence_count)

    def test_valid_source_and_evidence_are_schema_versioned_and_cli_reconciles(self) -> None:
        evidence = self.register_line_evidence(1, 2, "alpha\nbeta\n")
        before = self.source_snapshot()

        result = evaluate_source_health(
            self.workspace,
            self.registration.project_id,
        )

        self.assertEqual(result.overall_status, "valid")
        self.assertEqual(result.source_status_counts["valid"], 1)
        self.assertEqual(result.evidence_status_counts["valid"], 1)
        self.assertEqual(result.recovery_write_count, 0)
        source_health = result.sources[0]
        evidence_health = result.evidence[0]
        self.assertEqual(source_health.status, "valid")
        self.assertEqual(source_health.reason_code, "source-current-path-valid")
        self.assertEqual(source_health.recovery.status, "not-needed")
        self.assertEqual(evidence_health.evidence_id, evidence.evidence_id)
        self.assertEqual(evidence_health.status, "valid")
        self.assertEqual(
            evidence_health.observed_excerpt_hash,
            excerpt_sha256("alpha\nbeta\n"),
        )
        payload = result.as_dict()
        self.assertEqual(payload["schema_version"], SOURCE_HEALTH_SCHEMA_VERSION)
        self.assertEqual(payload["kind"], SOURCE_HEALTH_REPORT_KIND)
        self.assertEqual(payload["health_version"], SOURCE_HEALTH_VERSION)
        self.assertEqual(payload["sources"][0]["kind"], SOURCE_HEALTH_SOURCE_KIND)
        self.assertEqual(
            payload["evidence"][0]["kind"],
            SOURCE_HEALTH_EVIDENCE_KIND,
        )
        self.assert_reconciled(result)
        self.assertEqual(before, self.source_snapshot())

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "source",
                "health",
                self.registration.project_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.root,
        )
        cli_payload = json.loads(completed.stdout)
        self.assertTrue(cli_payload["ok"])
        self.assertEqual(cli_payload["overall_status"], "valid")
        self.assertEqual(cli_payload["source_totals"]["registry_count"], 1)
        self.assertEqual(cli_payload["evidence_totals"]["registry_count"], 1)

    def test_absent_evidence_registry_is_an_explicit_zero_without_creation(self) -> None:
        self.assertFalse(self.registration.layout.evidence_file.exists())
        before = self.source_snapshot()

        result = evaluate_source_health(
            self.workspace,
            self.registration.project_id,
        )

        self.assertEqual(result.overall_status, "valid")
        self.assertEqual(result.evidence_registry_count, 0)
        self.assertEqual(result.evidence, ())
        self.assertFalse(self.registration.layout.evidence_file.exists())
        self.assert_reconciled(result)
        self.assertEqual(before, self.source_snapshot())

    def test_modified_source_and_its_evidence_are_stale(self) -> None:
        self.register_line_evidence(1, 1, "alpha\n")
        self.source_path.write_bytes(b"changed\n")
        before = self.source_snapshot()

        result = evaluate_source_health(
            self.workspace,
            self.registration.project_id,
        )

        self.assertEqual(result.overall_status, "stale")
        self.assertEqual(result.sources[0].status, "stale")
        self.assertEqual(
            result.sources[0].reason_code,
            "source-content-hash-mismatch",
        )
        self.assertEqual(result.evidence[0].status, "stale")
        self.assertEqual(
            result.evidence[0].reason_code,
            "source-content-hash-mismatch",
        )
        self.assertEqual(result.recovery_write_count, 0)
        self.assert_reconciled(result)
        self.assertEqual(before, self.source_snapshot())

    def test_deleted_source_and_its_evidence_are_missing(self) -> None:
        self.register_line_evidence(1, 1, "alpha\n")
        self.source_path.unlink()
        before = self.source_snapshot()

        result = evaluate_source_health(
            self.workspace,
            self.registration.project_id,
        )

        self.assertEqual(result.overall_status, "missing")
        self.assertEqual(result.sources[0].status, "missing")
        self.assertEqual(
            result.sources[0].reason_code,
            "source-current-path-missing",
        )
        self.assertEqual(result.evidence[0].status, "missing")
        self.assertEqual(
            result.evidence[0].reason_code,
            "source-current-path-missing",
        )
        self.assert_reconciled(result)
        self.assertEqual(before, self.source_snapshot())

    def test_unique_relocation_preserves_identity_and_revalidates_evidence(self) -> None:
        evidence = self.register_line_evidence(2, 2, "beta\n")
        source_id = self.source().source_id
        moved = self.project / "moved" / "notes.txt"
        moved.parent.mkdir()
        self.source_path.rename(moved)
        inventory_project(self.workspace, self.registration.project_id)
        before = self.source_snapshot()

        result = evaluate_source_health(
            self.workspace,
            self.registration.project_id,
        )

        self.assertEqual(result.overall_status, "valid")
        self.assertEqual(result.sources[0].source_id, source_id)
        self.assertEqual(result.sources[0].current_path, "moved/notes.txt")
        self.assertEqual(result.sources[0].status, "valid")
        self.assertEqual(
            result.sources[0].reason_code,
            "source-relocation-recovered",
        )
        self.assertEqual(result.sources[0].recovery.recovery_method, "content-hash")
        self.assertTrue(result.sources[0].recovery.wrote_registry)
        self.assertEqual(result.recovery_write_count, 1)
        self.assertEqual(result.evidence[0].evidence_id, evidence.evidence_id)
        self.assertEqual(result.evidence[0].status, "valid")
        current = self.source()
        self.assertEqual(current.source_id, source_id)
        self.assertEqual(current.current_path, "moved/notes.txt")
        self.assert_reconciled(result)
        self.assertEqual(before, self.source_snapshot())

    def test_multiple_equal_hash_relocations_are_ambiguous_and_never_bind(self) -> None:
        self.register_line_evidence(1, 1, "alpha\n")
        original_source = self.source()
        payload = self.source_path.read_bytes()
        self.source_path.unlink()
        copies = self.project / "copies"
        copies.mkdir()
        (copies / "a.txt").write_bytes(payload)
        (copies / "b.txt").write_bytes(payload)
        inventory_project(self.workspace, self.registration.project_id)
        registry_before = self.registration.layout.sources_file.read_bytes()
        before = self.source_snapshot()

        result = evaluate_source_health(
            self.workspace,
            self.registration.project_id,
        )

        self.assertEqual(result.overall_status, "ambiguous")
        self.assertEqual(result.sources[0].status, "ambiguous")
        self.assertEqual(
            result.sources[0].reason_code,
            "source-relocation-ambiguous",
        )
        self.assertEqual(
            result.sources[0].recovery.candidate_paths,
            ("copies/a.txt", "copies/b.txt"),
        )
        self.assertEqual(result.evidence[0].status, "ambiguous")
        self.assertEqual(result.recovery_write_count, 0)
        current = self.source()
        self.assertEqual(current.source_id, original_source.source_id)
        self.assertEqual(current.current_path, "notes.txt")
        self.assertEqual(
            registry_before,
            self.registration.layout.sources_file.read_bytes(),
        )
        self.assert_reconciled(result)
        self.assertEqual(before, self.source_snapshot())

    def test_recurring_content_does_not_make_old_evidence_current(self) -> None:
        old_evidence = self.register_line_evidence(1, 1, "alpha\n")
        self.source_path.write_bytes(b"delta\n")
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)
        self.source_path.write_bytes(self.source_bytes)
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)

        result = evaluate_source_health(
            self.workspace,
            self.registration.project_id,
        )

        self.assertEqual(result.sources[0].status, "valid")
        self.assertEqual(result.sources[0].current_version, 3)
        evidence_health = next(
            record
            for record in result.evidence
            if record.evidence_id == old_evidence.evidence_id
        )
        self.assertEqual(evidence_health.status, "stale")
        self.assertEqual(
            evidence_health.reason_code,
            "current-source-version-mismatch",
        )
        self.assertEqual(result.overall_status, "stale")
        self.assert_reconciled(result)

    def test_invalid_locator_and_wrong_excerpt_are_distinct_stale_evidence(self) -> None:
        invalid_locator = self.register_line_evidence(99, 99, "not present\n")
        wrong_excerpt = self.register_line_evidence(1, 1, "wrong\n")

        result = evaluate_source_health(
            self.workspace,
            self.registration.project_id,
        )

        records = {record.evidence_id: record for record in result.evidence}
        self.assertEqual(result.sources[0].status, "valid")
        self.assertEqual(records[invalid_locator.evidence_id].status, "stale")
        self.assertEqual(
            records[invalid_locator.evidence_id].reason_code,
            "source-locator-invalid",
        )
        self.assertEqual(records[wrong_excerpt.evidence_id].status, "stale")
        self.assertEqual(
            records[wrong_excerpt.evidence_id].reason_code,
            "excerpt-hash-mismatch",
        )
        self.assertEqual(result.evidence_status_counts["stale"], 2)
        self.assertEqual(result.overall_status, "stale")
        self.assert_reconciled(result)
