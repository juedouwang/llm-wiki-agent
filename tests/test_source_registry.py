from __future__ import annotations

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
    SOURCE_ID_STRATEGY,
    SOURCE_REGISTRY_KIND,
    SOURCE_REGISTRY_SCHEMA_VERSION,
    SOURCE_REGISTRY_VERSION,
    SourceRegistryError,
    SourceRegistryLockError,
    load_source_registry,
    sync_source_registry,
)


REPO_ROOT = Path(__file__).parent.parent


class SourceRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.project = self.root / "research-project"
        self.project.mkdir()
        (self.project / "README.md").write_text(
            "# Source Identity Study\n",
            encoding="utf-8",
            newline="\n",
        )
        source = self.project / "src"
        source.mkdir()
        (source / "model.py").write_text(
            "VALUE = 7\n",
            encoding="utf-8",
            newline="\n",
        )
        self.registration = register_project(
            self.workspace,
            self.project,
            project_id="source-identity-study",
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

    def test_sync_assigns_one_core_id_per_manifest_file_and_is_idempotent(self) -> None:
        before_source = self.source_snapshot()
        result = sync_source_registry(
            self.workspace,
            self.registration.project_id,
        )

        self.assertTrue(result.wrote_registry)
        self.assertEqual(result.assigned_count, self.inventory.record_counts["file"])
        self.assertEqual(result.existing_count, 0)
        registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        manifest_paths = sorted(
            row["path"]
            for row in (
                json.loads(line)
                for line in self.inventory.manifest_file.read_text(
                    encoding="utf-8"
                ).splitlines()
            )
            if row.get("record_type") == "file"
        )
        self.assertEqual(
            [record.manifest_path for record in registry.records],
            manifest_paths,
        )
        self.assertEqual(len(registry.by_source_id), len(manifest_paths))
        for record in registry.records:
            self.assertRegex(record.source_id, r"^src-[0-9a-f]{32}$")
            self.assertEqual(
                record.first_seen_scan_generation,
                self.inventory.scan_generation,
            )

        rows = self.registry_rows()
        self.assertEqual(
            rows[0],
            {
                "schema_version": SOURCE_REGISTRY_SCHEMA_VERSION,
                "kind": SOURCE_REGISTRY_KIND,
                "registry_version": SOURCE_REGISTRY_VERSION,
                "record_type": "summary",
                "project_id": self.registration.project_id,
                "identity_strategy": SOURCE_ID_STRATEGY,
                "source_count": len(manifest_paths),
            },
        )
        forbidden = {
            "content_hash",
            "content_sha256",
            "versions",
            "version",
            "aliases",
            "path_aliases",
            "evidence",
            "locator",
            "excerpt_hash",
            "extracted",
            "processing_status",
            "read_depth",
            "health",
            "mcp",
            "hook",
            "web",
            "llm",
        }
        for row in rows:
            self.assertFalse(forbidden.intersection(row))
        self.assertEqual(before_source, self.source_snapshot())

        first_bytes = result.sources_file.read_bytes()
        first_mtime = result.sources_file.stat().st_mtime_ns
        second = sync_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        self.assertFalse(second.wrote_registry)
        self.assertEqual(second.assigned_count, 0)
        self.assertEqual(second.existing_count, len(manifest_paths))
        self.assertEqual(result.sources_file.read_bytes(), first_bytes)
        self.assertEqual(result.sources_file.stat().st_mtime_ns, first_mtime)
        self.assertEqual(before_source, self.source_snapshot())

    def test_addition_preserves_old_ids_and_removed_paths_remain_registered(self) -> None:
        first = sync_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        original = load_source_registry(
            self.workspace,
            self.registration.project_id,
        ).by_path

        added = self.project / "notes.txt"
        added.write_text("new evidence\n", encoding="utf-8", newline="\n")
        self.inventory = inventory_project(
            self.workspace,
            self.registration.project_id,
        )
        second = sync_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        after_add = load_source_registry(
            self.workspace,
            self.registration.project_id,
        ).by_path

        self.assertEqual(second.assigned_count, 1)
        self.assertEqual(second.source_count, first.source_count + 1)
        self.assertEqual(
            {path: record.source_id for path, record in original.items()},
            {
                path: after_add[path].source_id
                for path in original
            },
        )
        self.assertIn("notes.txt", after_add)

        (self.project / "src" / "model.py").unlink()
        self.inventory = inventory_project(
            self.workspace,
            self.registration.project_id,
        )
        third = sync_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        after_remove = load_source_registry(
            self.workspace,
            self.registration.project_id,
        ).by_path

        self.assertEqual(third.assigned_count, 0)
        self.assertEqual(third.source_count, second.source_count)
        self.assertEqual(
            after_remove["src/model.py"].source_id,
            original["src/model.py"].source_id,
        )
        self.assertNotIn(
            "src/model.py",
            {
                row["path"]
                for row in (
                    json.loads(line)
                    for line in self.inventory.manifest_file.read_text(
                        encoding="utf-8"
                    ).splitlines()
                )
                if row.get("record_type") == "file"
            },
        )

    def test_concurrent_cli_writers_do_not_duplicate_assignments_or_touch_source(self) -> None:
        for number in range(20):
            (self.project / f"sample-{number:02}.txt").write_text(
                f"sample {number}\n",
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

        self.assertTrue(all(payload["ok"] for payload in payloads))
        self.assertEqual(
            sum(int(payload["assigned_count"]) for payload in payloads),
            self.inventory.record_counts["file"],
        )
        registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        self.assertEqual(len(registry.records), self.inventory.record_counts["file"])
        self.assertEqual(len(registry.by_path), len(registry.records))
        self.assertEqual(len(registry.by_source_id), len(registry.records))
        self.assertFalse(
            self.registration.layout.sources_file.with_name(
                "sources.jsonl.lock"
            ).exists()
        )
        self.assertEqual(before_source, self.source_snapshot())

    def test_corrupt_conflicting_legacy_and_future_state_fails_closed(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        valid_rows = self.registry_rows()
        summary = valid_rows[0]
        sources = valid_rows[1:]
        self.assertGreaterEqual(len(sources), 2)

        invalid_payloads: dict[str, bytes] = {
            "invalid-json": b"{\n",
            "duplicate-json-key": (
                b'{"schema_version":1,"schema_version":1,"kind":"x"}\n'
            ),
        }

        legacy = dict(summary)
        legacy.pop("schema_version")
        invalid_payloads["legacy-missing-version"] = self.encode_rows([legacy, *sources])

        future = dict(summary)
        future["schema_version"] = SOURCE_REGISTRY_SCHEMA_VERSION + 1
        invalid_payloads["future-version"] = self.encode_rows([future, *sources])

        boolean_version = dict(summary)
        boolean_version["schema_version"] = True
        invalid_payloads["boolean-version"] = self.encode_rows(
            [boolean_version, *sources]
        )

        extra = dict(summary)
        extra["unexpected"] = "field"
        invalid_payloads["extra-field"] = self.encode_rows([extra, *sources])

        wrong_project = dict(summary)
        wrong_project["project_id"] = "different-project"
        invalid_payloads["wrong-project"] = self.encode_rows(
            [wrong_project, *sources]
        )

        wrong_strategy = dict(summary)
        wrong_strategy["identity_strategy"] = "path-derived-v1"
        invalid_payloads["wrong-strategy"] = self.encode_rows(
            [wrong_strategy, *sources]
        )

        count_mismatch = dict(summary)
        count_mismatch["source_count"] = len(sources) + 1
        invalid_payloads["count-mismatch"] = self.encode_rows(
            [count_mismatch, *sources]
        )

        duplicate_id = [dict(row) for row in sources]
        duplicate_id[1]["source_id"] = duplicate_id[0]["source_id"]
        invalid_payloads["duplicate-source-id"] = self.encode_rows(
            [summary, *duplicate_id]
        )

        duplicate_path = [dict(row) for row in sources]
        duplicate_path[1]["manifest_path"] = duplicate_path[0]["manifest_path"]
        invalid_payloads["duplicate-path"] = self.encode_rows(
            [summary, *duplicate_path]
        )

        invalid_payloads["unsorted-records"] = self.encode_rows(
            [summary, *reversed(sources)]
        )

        future_assignment = [dict(row) for row in sources]
        future_assignment[0]["first_seen_scan_generation"] = (
            self.inventory.scan_generation + 1
        )
        invalid_payloads["future-assignment-generation"] = self.encode_rows(
            [summary, *future_assignment]
        )

        bad_source_id = [dict(row) for row in sources]
        bad_source_id[0]["source_id"] = "src-ABC"
        invalid_payloads["invalid-source-id"] = self.encode_rows(
            [summary, *bad_source_id]
        )

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

    def test_registry_rejects_manifest_generation_older_than_assignments(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        rows = self.registry_rows()
        source = dict(rows[1])
        source["first_seen_scan_generation"] = self.inventory.scan_generation + 1
        rows[1] = source
        payload = self.encode_rows(rows)
        self.registration.layout.sources_file.write_bytes(payload)

        with self.assertRaisesRegex(
            SourceRegistryError,
            "Manifest generation predates",
        ):
            sync_source_registry(
                self.workspace,
                self.registration.project_id,
            )
        self.assertEqual(self.registration.layout.sources_file.read_bytes(), payload)

    def test_existing_lock_times_out_without_modifying_state(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        registry_before = self.registration.layout.sources_file.read_bytes()
        lock_file = self.registration.layout.sources_file.with_name(
            "sources.jsonl.lock"
        )
        lock_payload = b"another-writer\n"
        lock_file.write_bytes(lock_payload)

        with self.assertRaises(SourceRegistryLockError):
            sync_source_registry(
                self.workspace,
                self.registration.project_id,
                lock_timeout_seconds=0.02,
            )
        self.assertEqual(self.registration.layout.sources_file.read_bytes(), registry_before)
        self.assertEqual(lock_file.read_bytes(), lock_payload)

    def test_source_sync_cli_json_is_parseable(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "source",
                "sync",
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

        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["project_id"], self.registration.project_id)
        self.assertEqual(payload["registry_version"], SOURCE_REGISTRY_VERSION)
        self.assertEqual(
            payload["manifest_file_count"],
            self.inventory.record_counts["file"],
        )
        self.assertTrue(Path(payload["sources_file"]).is_file())


if __name__ == "__main__":
    unittest.main()
