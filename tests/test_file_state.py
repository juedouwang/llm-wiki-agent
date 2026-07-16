from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.file_state import (
    FILE_STATE_KIND,
    FILE_STATE_SCHEMA_VERSION,
    PROCESSING_STATUSES,
    READ_DEPTHS,
    FileState,
    file_state_from_dict,
    inventory_file_state,
    state_is_compatible_with_inventory,
)
from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.scan_policy import ScanPolicyConfig


class FileStateSchemaTests(unittest.TestCase):
    def test_supported_enums_and_valid_combinations_round_trip(self) -> None:
        self.assertEqual(
            PROCESSING_STATUSES,
            ("discovered", "processed", "partial", "failed", "missing"),
        )
        self.assertEqual(
            READ_DEPTHS,
            (
                "deep_read",
                "normal_read",
                "sampled",
                "metadata_only",
                "ignored",
                "unsupported",
            ),
        )
        valid_pairs = {
            ("discovered", "sampled"),
            ("discovered", "metadata_only"),
            ("discovered", "ignored"),
            ("discovered", "unsupported"),
            ("processed", "deep_read"),
            ("processed", "normal_read"),
            ("processed", "sampled"),
            ("processed", "metadata_only"),
            ("processed", "ignored"),
            ("processed", "unsupported"),
            ("partial", "deep_read"),
            ("partial", "normal_read"),
            ("partial", "sampled"),
            ("partial", "metadata_only"),
            ("failed", "deep_read"),
            ("failed", "normal_read"),
            ("failed", "sampled"),
            ("failed", "metadata_only"),
            ("missing", "metadata_only"),
        }
        for processing_status, read_depth in valid_pairs:
            with self.subTest(
                processing_status=processing_status,
                read_depth=read_depth,
            ):
                state = FileState(
                    processing_status=processing_status,
                    read_depth=read_depth,
                    reason_code="test-reason",
                    reason="schema round-trip fixture",
                )
                self.assertEqual(file_state_from_dict(state.as_dict()), state)
                self.assertEqual(state.as_dict()["kind"], FILE_STATE_KIND)
                self.assertEqual(
                    state.as_dict()["schema_version"],
                    FILE_STATE_SCHEMA_VERSION,
                )

    def test_invalid_enums_combinations_and_records_fail_closed(self) -> None:
        invalid_constructors = (
            {"processing_status": "queued", "read_depth": "sampled"},
            {"processing_status": "discovered", "read_depth": "full"},
            {"processing_status": "discovered", "read_depth": "deep_read"},
            {"processing_status": "partial", "read_depth": "ignored"},
            {"processing_status": "failed", "read_depth": "unsupported"},
            {"processing_status": "missing", "read_depth": "sampled"},
        )
        for values in invalid_constructors:
            with self.subTest(values=values), self.assertRaises(ValueError):
                FileState(
                    **values,
                    reason_code="invalid-state",
                    reason="must be rejected",
                )

        valid = FileState(
            processing_status="discovered",
            read_depth="metadata_only",
            reason_code="inventory-only",
            reason="only metadata is available",
        ).as_dict()
        corruptions = {
            "future schema": lambda row: row.__setitem__(
                "schema_version", FILE_STATE_SCHEMA_VERSION + 1
            ),
            "wrong kind": lambda row: row.__setitem__("kind", "other"),
            "missing reason": lambda row: row.pop("reason"),
            "extra field": lambda row: row.__setitem__("extra", True),
            "bad reason code": lambda row: row.__setitem__("reason_code", "Bad code"),
            "blank reason": lambda row: row.__setitem__("reason", " "),
        }
        for name, corrupt in corruptions.items():
            with self.subTest(name=name):
                row = json.loads(json.dumps(valid))
                corrupt(row)
                with self.assertRaises(ValueError):
                    file_state_from_dict(row)

    def test_inventory_state_is_truthful_and_policy_aware(self) -> None:
        sampled = inventory_file_state("markdown")
        self.assertEqual(
            (sampled.processing_status, sampled.read_depth, sampled.reason_code),
            ("discovered", "sampled", "classification-sample"),
        )
        unsupported = inventory_file_state("unknown")
        self.assertEqual(unsupported.read_depth, "unsupported")
        sensitive = inventory_file_state(
            "dotenv",
            content_access_reason_code="sensitive-path",
            content_access_reason="credentials are protected",
        )
        self.assertEqual(sensitive.read_depth, "ignored")
        oversized = inventory_file_state(
            "csv",
            content_access_reason_code="content-size-limit",
            content_access_reason="file exceeds the local content limit",
        )
        self.assertEqual(oversized.read_depth, "metadata_only")

        self.assertTrue(state_is_compatible_with_inventory(sampled, sampled))
        processed = FileState(
            processing_status="processed",
            read_depth="normal_read",
            reason_code="text-extracted",
            reason="deterministic text extraction completed",
        )
        self.assertTrue(state_is_compatible_with_inventory(processed, sampled))
        self.assertFalse(state_is_compatible_with_inventory(sensitive, sampled))
        self.assertFalse(state_is_compatible_with_inventory(processed, sensitive))
        self.assertFalse(state_is_compatible_with_inventory(processed, oversized))
        missing = FileState(
            processing_status="missing",
            read_depth="metadata_only",
            reason_code="source-missing",
            reason="the previously inventoried file is not currently present",
        )
        self.assertFalse(state_is_compatible_with_inventory(missing, sampled))


class FileStateInventoryTests(unittest.TestCase):
    def test_manifest_assigns_every_file_auditable_two_axis_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            project = root / "study"
            project.mkdir()
            (project / "README.md").write_text("# Study\n", encoding="utf-8")
            (project / "opaque.bin").write_bytes(b"\x00\xff\x00\xfe")
            (project / ".env").write_text("TOKEN=x\n", encoding="utf-8")
            (project / "large.csv").write_bytes(b"x" * 128)
            source_before = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in project.iterdir()
            }
            registration = register_project(
                workspace,
                project,
                project_id="state-study",
            )

            result = inventory_project(
                workspace,
                registration.project_id,
                policy_config=ScanPolicyConfig(
                    max_content_file_bytes=64,
                    max_raw_external_send_bytes=64,
                ),
            )

            rows = [
                json.loads(line)
                for line in result.manifest_file.read_text(encoding="utf-8").splitlines()
            ]
            files = {
                row["path"]: row
                for row in rows
                if row["record_type"] == "file"
            }
            states = {
                name: file_state_from_dict(record["file_state"])
                for name, record in files.items()
            }
            self.assertEqual(states["README.md"].read_depth, "sampled")
            self.assertEqual(states["opaque.bin"].read_depth, "unsupported")
            self.assertEqual(states[".env"].read_depth, "ignored")
            self.assertEqual(states[".env"].reason_code, "sensitive-path")
            self.assertEqual(states["large.csv"].read_depth, "metadata_only")
            self.assertEqual(
                states["large.csv"].reason_code,
                "content-size-limit",
            )
            self.assertTrue(
                all(state.processing_status == "discovered" for state in states.values())
            )
            summary = rows[0]["file_state_summary"]
            self.assertEqual(summary["state_files"], len(files))
            self.assertEqual(sum(summary["processing_statuses"].values()), len(files))
            self.assertEqual(sum(summary["read_depths"].values()), len(files))
            self.assertEqual(sum(summary["reasons"].values()), len(files))
            source_after = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in project.iterdir()
            }
            self.assertEqual(source_before, source_after)
            self.assertFalse(any(name.startswith(".manifest") for name in os.listdir(project)))


if __name__ == "__main__":
    unittest.main()
