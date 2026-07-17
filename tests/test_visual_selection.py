from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from tools import reading_priority as priority_module
from tools import visual_selection as selection_module
from tools.file_state import FILE_STATE_KIND, FILE_STATE_SCHEMA_VERSION
from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.reading_priority import (
    generate_reading_priority,
    load_current_reading_priority,
)
from tools.scan_policy import ScanPolicyConfig
from tools.visual_selection import (
    VISUAL_CANDIDATE_FORMATS,
    VISUAL_SELECTION_DECISIONS,
    VISUAL_SELECTION_KIND,
    VISUAL_SELECTION_RECORD_KIND,
    VISUAL_SELECTION_SCHEMA_VERSION,
    VISUAL_SELECTION_VERSION,
    VisualSelectionCurrentnessError,
    VisualSelectionSchemaError,
    build_visual_selection,
    deserialize_visual_selection,
    serialize_visual_selection,
)


class VisualSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.project = self.root / "research-project"
        (self.project / "figures").mkdir(parents=True)
        (self.project / "papers").mkdir()
        (self.project / "README.md").write_text(
            """# Visual Study

visual-source-content-must-not-leak
![architecture](figures/architecture.png)
![result](figures/result.jpg)
![later](figures/z-deferred.gif)
![oversized](figures/oversized.tif)
""",
            encoding="utf-8",
            newline="\n",
        )
        (self.project / "figures" / "architecture.png").write_bytes(
            b"\x89PNG\r\n\x1a\narchitecture"
        )
        (self.project / "figures" / "result.jpg").write_bytes(
            b"\xff\xd8\xffresult-image\xff\xd9"
        )
        (self.project / "figures" / "z-deferred.gif").write_bytes(
            b"GIF89adeferred-image"
        )
        (self.project / "figures" / "oversized.tif").write_bytes(
            b"II*\x00" + b"x" * 1024
        )
        (self.project / "figures" / "already.bmp").write_bytes(
            b"BMalready-deep-read"
        )
        (self.project / "papers" / "scanned.pdf").write_bytes(
            b"%PDF-1.4\nscanned\n%%EOF\n"
        )
        (self.project / "figures" / "vector.svg").write_text(
            "<svg><text>not a C-05 raster candidate</text></svg>\n",
            encoding="utf-8",
            newline="\n",
        )
        self.registration = register_project(
            self.workspace,
            self.project,
            project_id="visual-study",
        )
        self.inventory = inventory_project(
            self.workspace,
            self.registration.project_id,
            policy_config=ScanPolicyConfig(
                max_content_file_bytes=512,
                max_raw_external_send_bytes=128,
                external_send_mode="local-only",
            ),
        )
        self._replace_file_states(
            {
                "figures/already.bmp",
                "papers/scanned.pdf",
            }
        )

    def _manifest_rows(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.inventory.manifest_file.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

    def _write_manifest_rows(self, rows: list[dict[str, object]]) -> None:
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

    def _replace_file_states(self, relative_paths: set[str]) -> None:
        rows = self._manifest_rows()
        found: set[str] = set()
        for row in rows[1:]:
            if row["record_type"] != "file" or row["path"] not in relative_paths:
                continue
            found.add(row["path"])
            row["file_state"] = {
                "schema_version": FILE_STATE_SCHEMA_VERSION,
                "kind": FILE_STATE_KIND,
                "processing_status": "processed",
                "read_depth": "deep_read",
                "reason_code": "deep-read-complete",
                "reason": "the source was already read deeply before C-05 selection",
            }
        self.assertEqual(found, relative_paths)

        statuses: Counter[str] = Counter()
        depths: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        for row in rows[1:]:
            if row["record_type"] != "file":
                continue
            state = row["file_state"]
            statuses[state["processing_status"]] += 1
            depths[state["read_depth"]] += 1
            reasons[state["reason_code"]] += 1
        summary = rows[0]["file_state_summary"]
        summary["processing_statuses"] = dict(sorted(statuses.items()))
        summary["read_depths"] = dict(sorted(depths.items()))
        summary["reasons"] = dict(sorted(reasons.items()))
        self._write_manifest_rows(rows)

    def _tamper_manifest_mtime(self) -> None:
        rows = self._manifest_rows()
        for row in rows[1:]:
            if row["record_type"] == "file":
                row["mtime_ns"] += 1
                break
        else:
            self.fail("fixture Manifest has no ordinary file row")
        self._write_manifest_rows(rows)

    @staticmethod
    def _file_bytes_snapshot(root: Path) -> dict[str, bytes]:
        if not root.exists():
            return {}
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def _source_snapshot(self) -> dict[str, tuple[str, int]]:
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

    def _generate_and_build(self) -> tuple[dict[str, object], object]:
        with patch.object(priority_module, "DEEP_READ_MAX_FILES", 2):
            priority = generate_reading_priority(
                self.workspace,
                self.registration.project_id,
            ).priority
            report = build_visual_selection(
                self.workspace,
                self.registration.project_id,
            )
        return priority, report

    def test_current_grounded_selection_preserves_all_visual_decisions(self) -> None:
        with patch.object(priority_module, "DEEP_READ_MAX_FILES", 2):
            priority = generate_reading_priority(
                self.workspace,
                self.registration.project_id,
            ).priority
            source_before = self._source_snapshot()
            curated_before = self._file_bytes_snapshot(
                self.registration.layout.knowledge_root
            )
            machine_before = self._file_bytes_snapshot(
                self.registration.layout.machine_root
            )
            with patch.object(
                selection_module,
                "load_current_reading_priority",
                wraps=load_current_reading_priority,
            ) as current_loader:
                report = build_visual_selection(
                    self.workspace,
                    self.registration.project_id,
                )

        self.assertEqual(current_loader.call_count, 2)
        self.assertFalse(hasattr(selection_module, "load_reading_priority"))
        self.assertEqual(
            VISUAL_CANDIDATE_FORMATS,
            ("png", "jpeg", "gif", "tiff", "bmp", "pdf"),
        )
        self.assertEqual(
            VISUAL_SELECTION_DECISIONS,
            ("selected", "deferred", "limited", "not-candidate"),
        )
        self.assertEqual(report.manifest.as_dict(), priority["manifest"])
        expected_priority_hash = hashlib.sha256(
            json.dumps(
                priority,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(report.priority_payload_sha256, expected_priority_hash)

        records = {record.path: record for record in report.records}
        self.assertEqual(
            set(records),
            {
                "figures/architecture.png",
                "figures/result.jpg",
                "figures/z-deferred.gif",
                "figures/oversized.tif",
                "figures/already.bmp",
                "papers/scanned.pdf",
            },
        )
        self.assertNotIn("figures/vector.svg", records)
        self.assertEqual(
            {record.format for record in report.records},
            set(VISUAL_CANDIDATE_FORMATS),
        )
        self.assertEqual(records["figures/architecture.png"].decision, "selected")
        self.assertEqual(records["figures/result.jpg"].decision, "selected")
        self.assertEqual(records["figures/z-deferred.gif"].decision, "deferred")
        self.assertEqual(records["figures/oversized.tif"].decision, "limited")
        self.assertEqual(records["figures/already.bmp"].decision, "not-candidate")
        self.assertEqual(records["papers/scanned.pdf"].decision, "not-candidate")
        self.assertEqual(
            records["figures/architecture.png"].referenced_by,
            ("README.md",),
        )
        self.assertEqual(
            records["figures/result.jpg"].referenced_by,
            ("README.md",),
        )
        self.assertEqual(
            [record.priority_rank for record in report.records],
            sorted(record.priority_rank for record in report.records),
        )
        self.assertEqual(
            {record.path for record in report.executable_records},
            {"figures/architecture.png", "figures/result.jpg"},
        )
        self.assertTrue(
            all(
                record.executable == (record.decision == "selected")
                for record in report.records
            )
        )
        self.assertTrue(
            all(
                not record.execution_authorized
                for record in report.records
                if record.decision != "selected"
            )
        )
        for path in ("figures/architecture.png", "figures/result.jpg"):
            self.assertEqual(records[path].local_content_access, "allowed")
            self.assertEqual(records[path].local_reason_code, "local-content-allowed")
            self.assertEqual(records[path].raw_external_send, "blocked")
            self.assertEqual(records[path].external_reason_code, "external-local-only")
        self.assertEqual(
            records["figures/oversized.tif"].local_content_access,
            "metadata_only",
        )
        self.assertEqual(
            records["figures/oversized.tif"].local_reason_code,
            "content-size-limit",
        )
        self.assertEqual(
            report.summary.as_dict(),
            {
                "candidate_count": 6,
                "selected_count": 2,
                "deferred_count": 1,
                "limited_count": 1,
                "not_candidate_count": 2,
                "selected_bytes": sum(
                    records[path].size_bytes
                    for path in (
                        "figures/architecture.png",
                        "figures/result.jpg",
                    )
                ),
            },
        )

        serialized = serialize_visual_selection(report)
        decoded = json.loads(serialized)
        self.assertEqual(
            set(decoded),
            {
                "schema_version",
                "kind",
                "version",
                "project_id",
                "manifest",
                "priority_payload_sha256",
                "summary",
                "records",
            },
        )
        self.assertEqual(decoded["schema_version"], VISUAL_SELECTION_SCHEMA_VERSION)
        self.assertEqual(decoded["kind"], VISUAL_SELECTION_KIND)
        self.assertEqual(decoded["version"], VISUAL_SELECTION_VERSION)
        expected_record_fields = {
            "schema_version",
            "kind",
            "path",
            "content_sha256",
            "size_bytes",
            "mtime_ns",
            "format",
            "research_role",
            "priority_rank",
            "deep_read_status",
            "decision",
            "local_content_access",
            "raw_external_send",
            "local_reason_code",
            "external_reason_code",
            "reason_codes",
            "referenced_by",
        }
        self.assertTrue(
            all(set(record) == expected_record_fields for record in decoded["records"])
        )
        self.assertTrue(
            all(
                record["schema_version"] == VISUAL_SELECTION_SCHEMA_VERSION
                and record["kind"] == VISUAL_SELECTION_RECORD_KIND
                for record in decoded["records"]
            )
        )
        self.assertNotIn(str(self.project.resolve()), serialized)
        self.assertNotIn("visual-source-content-must-not-leak", serialized)
        self.assertEqual(source_before, self._source_snapshot())
        self.assertEqual(
            curated_before,
            self._file_bytes_snapshot(self.registration.layout.knowledge_root),
        )
        self.assertEqual(
            machine_before,
            self._file_bytes_snapshot(self.registration.layout.machine_root),
        )

    def test_serialization_is_deterministic_and_strictly_round_trips(self) -> None:
        _, report = self._generate_and_build()
        first = serialize_visual_selection(report)
        second = serialize_visual_selection(report)
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))
        restored = deserialize_visual_selection(
            first,
            project_id=self.registration.project_id,
        )
        self.assertEqual(restored, report)
        self.assertEqual(serialize_visual_selection(restored), first)
        self.assertEqual(
            deserialize_visual_selection(first.encode("utf-8")),
            report,
        )

    def test_strict_report_parser_rejects_tampering_legacy_and_future_data(self) -> None:
        _, report = self._generate_and_build()
        base = report.as_dict()

        def reject(name: str, mutate: object) -> None:
            value = copy.deepcopy(base)
            mutate(value)
            with self.subTest(name=name), self.assertRaises(VisualSelectionSchemaError):
                deserialize_visual_selection(
                    json.dumps(value, ensure_ascii=False),
                    project_id=self.registration.project_id,
                )

        cases = (
            ("future-report", lambda value: value.__setitem__("schema_version", 2)),
            ("legacy-report", lambda value: value.pop("schema_version")),
            ("unknown-top-key", lambda value: value.__setitem__("extra", True)),
            ("wrong-version", lambda value: value.__setitem__("version", "visual-v2")),
            (
                "malformed-priority-hash",
                lambda value: value.__setitem__("priority_payload_sha256", "0" * 63),
            ),
            (
                "malformed-manifest-hash",
                lambda value: value["manifest"].__setitem__("content_sha256", "bad"),
            ),
            (
                "summary-count-tamper",
                lambda value: value["summary"].__setitem__("selected_count", 99),
            ),
            (
                "future-record",
                lambda value: value["records"][0].__setitem__("schema_version", 2),
            ),
            (
                "legacy-record",
                lambda value: value["records"][0].pop("schema_version"),
            ),
            (
                "unknown-record-key",
                lambda value: value["records"][0].__setitem__("source", "secret"),
            ),
            (
                "decision-mismatch",
                lambda value: value["records"][0].__setitem__(
                    "decision", "deferred"
                ),
            ),
            (
                "unknown-decision",
                lambda value: (
                    value["records"][0].__setitem__("decision", "execute"),
                    value["records"][0].__setitem__(
                        "deep_read_status", "execute"
                    ),
                ),
            ),
            (
                "unknown-format",
                lambda value: value["records"][0].__setitem__("format", "svg"),
            ),
            (
                "absolute-path",
                lambda value: value["records"][0].__setitem__(
                    "path", "/tmp/figure.png"
                ),
            ),
            (
                "windows-absolute-path",
                lambda value: value["records"][0].__setitem__(
                    "path", "C:/figures/figure.png"
                ),
            ),
            (
                "traversal-path",
                lambda value: value["records"][0].__setitem__(
                    "path", "../figure.png"
                ),
            ),
            (
                "negative-mtime",
                lambda value: value["records"][0].__setitem__("mtime_ns", -1),
            ),
            (
                "duplicate-path",
                lambda value: value["records"][1].__setitem__(
                    "path", value["records"][0]["path"]
                ),
            ),
            (
                "duplicate-rank",
                lambda value: value["records"][1].__setitem__(
                    "priority_rank", value["records"][0]["priority_rank"]
                ),
            ),
            ("unsorted-records", lambda value: value["records"].reverse()),
        )
        for name, mutate in cases:
            reject(name, mutate)

        duplicate_key_payload = serialize_visual_selection(report).replace(
            '"kind": "llmwiki-visual-selection",',
            '"kind": "duplicate",\n  "kind": "llmwiki-visual-selection",',
            1,
        )
        with self.assertRaises(VisualSelectionSchemaError):
            deserialize_visual_selection(duplicate_key_payload)

    def test_current_policy_and_manifest_tampering_fail_closed(self) -> None:
        with patch.object(priority_module, "DEEP_READ_MAX_FILES", 2):
            priority = generate_reading_priority(
                self.workspace,
                self.registration.project_id,
            ).priority

            ignore_file = self.project / ".llmwikiignore"
            ignore_file.write_text("figures/*.png\n", encoding="utf-8", newline="\n")
            with patch.object(
                selection_module,
                "load_current_reading_priority",
                return_value=priority,
            ) as replayed_loader:
                with self.assertRaises(VisualSelectionCurrentnessError):
                    build_visual_selection(
                        self.workspace,
                        self.registration.project_id,
                    )
            self.assertEqual(replayed_loader.call_count, 1)
            ignore_file.unlink()

            self._tamper_manifest_mtime()
            with patch.object(
                selection_module,
                "load_current_reading_priority",
                return_value=priority,
            ) as replayed_loader:
                with self.assertRaises(VisualSelectionCurrentnessError):
                    build_visual_selection(
                        self.workspace,
                        self.registration.project_id,
                    )
            self.assertEqual(replayed_loader.call_count, 1)

    def test_policy_change_between_authorization_passes_fails_closed(self) -> None:
        with patch.object(priority_module, "DEEP_READ_MAX_FILES", 2):
            generate_reading_priority(
                self.workspace,
                self.registration.project_id,
            )
            call_count = 0

            def load_with_mid_build_change(
                workspace_root: str | Path,
                project_id: str,
            ) -> dict[str, object]:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    (self.project / ".llmwikiignore").write_text(
                        "figures/*.png\n",
                        encoding="utf-8",
                        newline="\n",
                    )
                return load_current_reading_priority(workspace_root, project_id)

            with patch.object(
                selection_module,
                "load_current_reading_priority",
                side_effect=load_with_mid_build_change,
            ):
                with self.assertRaises(VisualSelectionCurrentnessError):
                    build_visual_selection(
                        self.workspace,
                        self.registration.project_id,
                    )
        self.assertEqual(call_count, 2)


class VisualSelectionLargeDatasetTests(unittest.TestCase):
    def test_large_training_image_group_remains_limited_and_non_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            project = root / "project"
            image_root = project / "data" / "train"
            image_root.mkdir(parents=True)
            for index in range(32):
                (image_root / f"image-{index:02}.png").write_bytes(
                    b"\x89PNG\r\n\x1a\n" + bytes([index])
                )
            registration = register_project(
                workspace,
                project,
                project_id="large-training-images",
            )
            inventory_project(workspace, registration.project_id)
            generate_reading_priority(workspace, registration.project_id)

            report = build_visual_selection(workspace, registration.project_id)

        self.assertEqual(report.summary.candidate_count, 32)
        self.assertEqual(report.summary.selected_count, 0)
        self.assertEqual(report.summary.deferred_count, 0)
        self.assertEqual(report.summary.limited_count, 32)
        self.assertEqual(report.summary.not_candidate_count, 0)
        self.assertEqual(report.summary.selected_bytes, 0)
        self.assertEqual(report.executable_records, ())
        self.assertTrue(
            all(record.decision == "limited" for record in report.records)
        )
        self.assertTrue(
            all(
                "large-dataset-limited" in record.reason_codes
                for record in report.records
            )
        )
        self.assertTrue(
            all(not record.execution_authorized for record in report.records)
        )


if __name__ == "__main__":
    unittest.main()
