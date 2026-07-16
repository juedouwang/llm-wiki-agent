from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.source_registry import (
    LEGACY_SOURCE_REGISTRY_VERSION,
    SOURCE_ID_STRATEGY,
    SOURCE_REGISTRY_KIND,
    SOURCE_REGISTRY_SCHEMA_VERSION,
    SOURCE_REGISTRY_VERSION,
    SOURCE_VERSION_HASH_ALGORITHM,
    SourceRegistryError,
    get_source_history,
    load_source_registry,
    sync_source_registry,
)


REPO_ROOT = Path(__file__).parent.parent


class SourceVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.project = self.root / "research-project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "README.md").write_text(
            "# Versioned Sources\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.project / "src" / "model.py").write_text(
            "VALUE = 'A'\n",
            encoding="utf-8",
            newline="\n",
        )
        self.registration = register_project(
            self.workspace,
            self.project,
            project_id="source-version-study",
        )
        self.inventory = inventory_project(
            self.workspace,
            self.registration.project_id,
        )

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

    def manifest_files(self) -> dict[str, dict[str, object]]:
        return {
            row["path"]: row
            for row in (
                json.loads(line)
                for line in self.inventory.manifest_file.read_text(
                    encoding="utf-8"
                ).splitlines()
            )
            if row.get("record_type") == "file"
        }

    def registry_rows(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.registration.layout.sources_file.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

    @staticmethod
    def encode_rows(rows: list[dict[str, object]]) -> bytes:
        return b"".join(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
            for row in rows
        )

    def test_valid_v1_registry_upgrades_explicitly_and_preserves_ids(self) -> None:
        before_source = self.source_snapshot()
        manifest = self.manifest_files()
        assignments = {
            path: f"src-{index:032x}"
            for index, path in enumerate(sorted(manifest), start=1)
        }
        rows: list[dict[str, object]] = [
            {
                "schema_version": SOURCE_REGISTRY_SCHEMA_VERSION,
                "kind": SOURCE_REGISTRY_KIND,
                "registry_version": LEGACY_SOURCE_REGISTRY_VERSION,
                "record_type": "summary",
                "project_id": self.registration.project_id,
                "identity_strategy": SOURCE_ID_STRATEGY,
                "source_count": len(assignments),
            },
            *[
                {
                    "schema_version": SOURCE_REGISTRY_SCHEMA_VERSION,
                    "kind": SOURCE_REGISTRY_KIND,
                    "registry_version": LEGACY_SOURCE_REGISTRY_VERSION,
                    "record_type": "source",
                    "project_id": self.registration.project_id,
                    "source_id": assignments[path],
                    "manifest_path": path,
                    "first_seen_scan_generation": self.inventory.scan_generation,
                }
                for path in sorted(assignments)
            ],
        ]
        self.registration.layout.sources_file.write_bytes(self.encode_rows(rows))

        result = sync_source_registry(
            self.workspace,
            self.registration.project_id,
        )

        self.assertTrue(result.upgraded_registry)
        self.assertTrue(result.wrote_registry)
        self.assertEqual(result.assigned_count, 0)
        self.assertEqual(result.versions_added_count, len(assignments))
        registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        self.assertEqual(
            {path: record.source_id for path, record in registry.by_path.items()},
            assignments,
        )
        for path, source_id in assignments.items():
            record = registry.by_source_id[source_id]
            self.assertEqual(record.current_path, path)
            self.assertEqual(record.current_version, 1)
            self.assertEqual(len(record.versions), 1)
            self.assertEqual(
                record.versions[0].content_hash,
                manifest[path]["content_sha256"],
            )
        self.assertEqual(self.registry_rows()[0]["registry_version"], SOURCE_REGISTRY_VERSION)
        self.assertEqual(before_source, self.source_snapshot())

    def test_content_transitions_append_versions_and_history_is_deterministic(self) -> None:
        before_source = self.source_snapshot()
        first = sync_source_registry(self.workspace, self.registration.project_id)
        first_registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        source_id = first_registry.by_path["src/model.py"].source_id
        digest_a = hashlib.sha256(b"VALUE = 'A'\n").hexdigest()
        self.assertEqual(first.version_count, self.inventory.record_counts["file"])
        self.assertEqual(
            first_registry.by_source_id[source_id].versions[0].content_hash,
            digest_a,
        )

        previous_generation = self.inventory.scan_generation
        self.inventory = inventory_project(
            self.workspace,
            self.registration.project_id,
        )
        self.assertGreater(self.inventory.scan_generation, previous_generation)
        unchanged = sync_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        unchanged_registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        unchanged_record = unchanged_registry.by_source_id[source_id]
        self.assertEqual(unchanged.versions_added_count, 0)
        self.assertEqual(unchanged_record.current_version, 1)
        self.assertEqual(len(unchanged_record.versions), 1)
        same_generation = sync_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        self.assertFalse(same_generation.wrote_registry)

        model = self.project / "src" / "model.py"
        model.write_text("VALUE = 'B'\n", encoding="utf-8", newline="\n")
        self.inventory = inventory_project(
            self.workspace,
            self.registration.project_id,
        )
        changed = sync_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        changed_record = load_source_registry(
            self.workspace,
            self.registration.project_id,
        ).by_source_id[source_id]
        digest_b = hashlib.sha256(b"VALUE = 'B'\n").hexdigest()
        self.assertEqual(changed.assigned_count, 0)
        self.assertEqual(changed.versions_added_count, 1)
        self.assertEqual(changed_record.current_version, 2)
        self.assertEqual(
            [item.content_hash for item in changed_record.versions],
            [digest_a, digest_b],
        )

        model.write_text("VALUE = 'A'\n", encoding="utf-8", newline="\n")
        self.inventory = inventory_project(
            self.workspace,
            self.registration.project_id,
        )
        reverted = sync_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        reverted_record = load_source_registry(
            self.workspace,
            self.registration.project_id,
        ).by_source_id[source_id]
        self.assertEqual(reverted.versions_added_count, 1)
        self.assertEqual(reverted_record.current_version, 3)
        self.assertEqual(
            [item.content_hash for item in reverted_record.versions],
            [digest_a, digest_b, digest_a],
        )
        self.assertEqual(
            [item.version for item in reverted_record.versions],
            [1, 2, 3],
        )

        api_payload = get_source_history(
            self.workspace,
            self.registration.project_id,
            source_id,
        ).as_dict()
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "source",
                "history",
                self.registration.project_id,
                source_id,
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
        self.assertTrue(cli_payload.pop("ok"))
        self.assertEqual(cli_payload, api_payload)
        self.assertEqual(
            [item["version"] for item in cli_payload["source"]["versions"]],
            [1, 2, 3],
        )
        self.assertEqual(before_source[0], self.source_snapshot()[0])

    def test_concurrent_changed_generation_appends_exactly_one_version(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        source_id = registry.by_path["README.md"].source_id
        (self.project / "README.md").write_text(
            "# Versioned Sources\n\nchanged\n",
            encoding="utf-8",
            newline="\n",
        )
        self.inventory = inventory_project(
            self.workspace,
            self.registration.project_id,
        )
        before_source = self.source_snapshot()
        command = [
            sys.executable,
            "-B",
            str(REPO_ROOT / "tools" / "project.py"),
            "source",
            "sync",
            self.registration.project_id,
            "--workspace-root",
            str(self.workspace),
            "--json",
        ]
        processes = [
            subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                cwd=self.root,
            )
            for _ in range(8)
        ]
        payloads: list[dict[str, object]] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            payloads.append(json.loads(stdout))

        self.assertEqual(
            sum(int(payload["versions_added_count"]) for payload in payloads),
            1,
        )
        record = load_source_registry(
            self.workspace,
            self.registration.project_id,
        ).by_source_id[source_id]
        self.assertEqual(record.current_version, 2)
        self.assertEqual(len(record.versions), 2)
        self.assertEqual(before_source, self.source_snapshot())

    def test_missing_path_keeps_history_and_equal_hash_move_gets_new_id(self) -> None:
        first = sync_source_registry(self.workspace, self.registration.project_id)
        original = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        old_record = original.by_path["src/model.py"]
        old_hash = old_record.current_content_hash
        (self.project / "src" / "model.py").rename(
            self.project / "src" / "renamed.py"
        )
        self.inventory = inventory_project(
            self.workspace,
            self.registration.project_id,
        )

        second = sync_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        moved = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        retained = moved.by_source_id[old_record.source_id]
        replacement = moved.by_path["src/renamed.py"]

        self.assertEqual(second.source_count, first.source_count + 1)
        self.assertEqual(second.assigned_count, 1)
        self.assertEqual(retained.current_path, "src/model.py")
        self.assertEqual(retained.path_history, old_record.path_history)
        self.assertEqual(retained.versions, old_record.versions)
        self.assertNotEqual(replacement.source_id, retained.source_id)
        self.assertEqual(replacement.current_content_hash, old_hash)
        self.assertEqual(
            sorted(item.path for item in replacement.path_history),
            ["src/renamed.py"],
        )

    def test_corrupt_version_and_path_state_fails_closed(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        (self.project / "src" / "model.py").write_text(
            "VALUE = 'B'\n",
            encoding="utf-8",
            newline="\n",
        )
        self.inventory = inventory_project(
            self.workspace,
            self.registration.project_id,
        )
        sync_source_registry(self.workspace, self.registration.project_id)
        valid_rows = self.registry_rows()
        before_source = self.source_snapshot()
        summary = valid_rows[0]
        source_rows = [row for row in valid_rows if row["record_type"] == "source"]
        version_rows = [row for row in valid_rows if row["record_type"] == "version"]
        model_source = next(
            row for row in source_rows if row["current_path"] == "src/model.py"
        )
        model_id = model_source["source_id"]
        model_versions = [
            row for row in version_rows if row["source_id"] == model_id
        ]
        self.assertEqual(len(model_versions), 2)

        def mutated() -> list[dict[str, object]]:
            return copy.deepcopy(valid_rows)

        invalid_payloads: dict[str, bytes] = {}

        for label, value in (
            ("nested-path-missing-schema", None),
            ("nested-path-legacy-schema", 0),
            ("nested-path-boolean-schema", True),
            ("nested-path-future-schema", SOURCE_REGISTRY_SCHEMA_VERSION + 1),
        ):
            rows = mutated()
            source = next(row for row in rows if row.get("record_type") == "source")
            nested = source["path_history"][0]
            if value is None:
                nested.pop("schema_version")
            else:
                nested["schema_version"] = value
            invalid_payloads[label] = self.encode_rows(rows)

        rows = mutated()
        version = next(row for row in rows if row.get("record_type") == "version")
        version["content_hash"] = "A" * 64
        invalid_payloads["bad-content-hash"] = self.encode_rows(rows)

        rows = mutated()
        first_model_version = next(
            row
            for row in rows
            if row.get("record_type") == "version"
            and row["source_id"] == model_id
            and row["version"] == 1
        )
        first_model_version["version"] = 3
        invalid_payloads["noncontiguous-versions"] = self.encode_rows(rows)

        rows = mutated()
        source = next(
            row
            for row in rows
            if row.get("record_type") == "source" and row["source_id"] == model_id
        )
        source["current_version"] = 1
        invalid_payloads["current-version-mismatch"] = self.encode_rows(rows)

        rows = mutated()
        rows[0]["version_count"] = int(summary["version_count"]) + 1
        invalid_payloads["version-count-mismatch"] = self.encode_rows(rows)

        rows = mutated()
        version = next(row for row in rows if row.get("record_type") == "version")
        version["source_id"] = "src-ffffffffffffffffffffffffffffffff"
        invalid_payloads["unknown-source-reference"] = self.encode_rows(rows)

        rows = [summary, *reversed(source_rows), *version_rows]
        invalid_payloads["bad-source-order"] = self.encode_rows(rows)

        rows = [summary, *source_rows, *reversed(version_rows)]
        invalid_payloads["bad-version-order"] = self.encode_rows(rows)

        rows = mutated()
        future_generation = self.inventory.scan_generation + 1
        source = next(
            row
            for row in rows
            if row.get("record_type") == "source" and row["source_id"] == model_id
        )
        source["last_seen_scan_generation"] = future_generation
        source["path_history"][0]["last_seen_scan_generation"] = future_generation
        latest_version = next(
            row
            for row in rows
            if row.get("record_type") == "version"
            and row["source_id"] == model_id
            and row["version"] == 2
        )
        latest_version["observed_scan_generation"] = future_generation
        invalid_payloads["future-observation-generation"] = self.encode_rows(rows)

        sources_file = self.registration.layout.sources_file
        for label, payload in invalid_payloads.items():
            with self.subTest(label=label):
                sources_file.write_bytes(payload)
                with self.assertRaises(SourceRegistryError):
                    sync_source_registry(
                        self.workspace,
                        self.registration.project_id,
                    )
                self.assertEqual(sources_file.read_bytes(), payload)
                self.assertEqual(
                    list(sources_file.parent.glob(".sources.jsonl.*.tmp")),
                    [],
                )
                self.assertEqual(before_source, self.source_snapshot())

    def test_history_rejects_unknown_source_without_writing(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        before = self.registration.layout.sources_file.read_bytes()

        with self.assertRaisesRegex(SourceRegistryError, "not registered"):
            get_source_history(
                self.workspace,
                self.registration.project_id,
                "src-ffffffffffffffffffffffffffffffff",
            )

        self.assertEqual(self.registration.layout.sources_file.read_bytes(), before)
        self.assertEqual(SOURCE_VERSION_HASH_ALGORITHM, "sha256")


if __name__ == "__main__":
    unittest.main()
