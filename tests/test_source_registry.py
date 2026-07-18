from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import source_registry as source_registry_module
from tools import stable_file_access as stable_file_access_module
from tools.stable_file_access import (
    StableFileCommitUnknownError,
    exclusive_stable_file_lock,
)
from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.source_registry import (
    SOURCE_ID_STRATEGY,
    SOURCE_REGISTRY_KIND,
    SOURCE_REGISTRY_SCHEMA_VERSION,
    SOURCE_REGISTRY_VERSION,
    SOURCE_VERSION_HASH_ALGORITHM,
    SourceRegistryCommitStateError,
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
        self.assertEqual(result.version_count, self.inventory.record_counts["file"])
        self.assertEqual(
            result.versions_added_count,
            self.inventory.record_counts["file"],
        )
        self.assertFalse(result.upgraded_registry)
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
            sorted(record.manifest_path for record in registry.records),
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
                "hash_algorithm": SOURCE_VERSION_HASH_ALGORITHM,
                "source_count": len(manifest_paths),
                "version_count": len(manifest_paths),
            },
        )
        source_rows = [row for row in rows if row.get("record_type") == "source"]
        version_rows = [row for row in rows if row.get("record_type") == "version"]
        self.assertEqual(len(source_rows), len(manifest_paths))
        self.assertEqual(len(version_rows), len(manifest_paths))
        forbidden = {
            "evidence",
            "evidence_id",
            "locator",
            "excerpt",
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
        self.assertEqual(second.versions_added_count, 0)
        self.assertEqual(second.version_count, len(manifest_paths))
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
        self.assertEqual(
            sum(int(payload["versions_added_count"]) for payload in payloads),
            self.inventory.record_counts["file"],
        )
        registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        self.assertEqual(len(registry.records), self.inventory.record_counts["file"])
        self.assertEqual(len(registry.by_path), len(registry.records))
        self.assertEqual(len(registry.by_source_id), len(registry.records))
        self.assertTrue(
            self.registration.layout.sources_file.with_name(
                "sources.jsonl.lock"
            ).is_file()
        )
        self.assertEqual(before_source, self.source_snapshot())

    def test_corrupt_conflicting_legacy_and_future_state_fails_closed(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        valid_rows = self.registry_rows()
        summary = valid_rows[0]
        sources = [row for row in valid_rows if row.get("record_type") == "source"]
        versions = [row for row in valid_rows if row.get("record_type") == "version"]
        self.assertGreaterEqual(len(sources), 2)

        invalid_payloads: dict[str, bytes] = {
            "invalid-json": b"{\n",
            "duplicate-json-key": (
                b'{"schema_version":1,"schema_version":1,"kind":"x"}\n'
            ),
        }

        legacy = dict(summary)
        legacy.pop("schema_version")
        invalid_payloads["legacy-missing-version"] = self.encode_rows(
            [legacy, *sources, *versions]
        )

        future = dict(summary)
        future["schema_version"] = SOURCE_REGISTRY_SCHEMA_VERSION + 1
        invalid_payloads["future-version"] = self.encode_rows(
            [future, *sources, *versions]
        )

        boolean_version = dict(summary)
        boolean_version["schema_version"] = True
        invalid_payloads["boolean-version"] = self.encode_rows(
            [boolean_version, *sources, *versions]
        )

        extra = dict(summary)
        extra["unexpected"] = "field"
        invalid_payloads["extra-field"] = self.encode_rows(
            [extra, *sources, *versions]
        )

        wrong_project = dict(summary)
        wrong_project["project_id"] = "different-project"
        invalid_payloads["wrong-project"] = self.encode_rows(
            [wrong_project, *sources, *versions]
        )

        wrong_strategy = dict(summary)
        wrong_strategy["identity_strategy"] = "path-derived-v1"
        invalid_payloads["wrong-strategy"] = self.encode_rows(
            [wrong_strategy, *sources, *versions]
        )

        count_mismatch = dict(summary)
        count_mismatch["source_count"] = len(sources) + 1
        invalid_payloads["count-mismatch"] = self.encode_rows(
            [count_mismatch, *sources, *versions]
        )

        duplicate_id = [dict(row) for row in sources]
        duplicate_id[1]["source_id"] = duplicate_id[0]["source_id"]
        invalid_payloads["duplicate-source-id"] = self.encode_rows(
            [summary, *duplicate_id, *versions]
        )

        duplicate_path = [dict(row) for row in sources]
        duplicate_path[1]["current_path"] = duplicate_path[0]["current_path"]
        duplicate_path[1]["path_history"] = [
            dict(duplicate_path[0]["path_history"][0])
        ]
        duplicate_versions = [dict(row) for row in versions]
        second_id = sources[1]["source_id"]
        for row in duplicate_versions:
            if row["source_id"] == second_id:
                row["manifest_path"] = duplicate_path[0]["current_path"]
        invalid_payloads["duplicate-path"] = self.encode_rows(
            [summary, *duplicate_path, *duplicate_versions]
        )

        invalid_payloads["unsorted-records"] = self.encode_rows(
            [summary, *reversed(sources), *versions]
        )

        future_assignment = [dict(row) for row in sources]
        future_assignment[0]["last_seen_scan_generation"] = (
            self.inventory.scan_generation + 1
        )
        future_assignment[0]["path_history"] = [
            {
                **future_assignment[0]["path_history"][0],
                "last_seen_scan_generation": self.inventory.scan_generation + 1,
            }
        ]
        invalid_payloads["future-assignment-generation"] = self.encode_rows(
            [summary, *future_assignment, *versions]
        )

        bad_source_id = [dict(row) for row in sources]
        bad_source_id[0]["source_id"] = "src-ABC"
        invalid_payloads["invalid-source-id"] = self.encode_rows(
            [summary, *bad_source_id, *versions]
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
        source["last_seen_scan_generation"] = self.inventory.scan_generation + 1
        source["path_history"] = [
            {
                **source["path_history"][0],
                "last_seen_scan_generation": self.inventory.scan_generation + 1,
            }
        ]
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

    def test_loader_rejects_descriptor_escape_before_read(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        outside = self.root / "outside-sources.jsonl"
        outside.write_bytes(self.registration.layout.sources_file.read_bytes())

        with patch(
            "tools.stable_file_access._descriptor_final_path",
            return_value=outside.resolve(),
        ), patch("tools.stable_file_access.os.read") as read_mock:
            with self.assertRaisesRegex(
                SourceRegistryError,
                "stable, non-redirected regular file",
            ):
                load_source_registry(
                    self.workspace,
                    self.registration.project_id,
                )

        read_mock.assert_not_called()

    def test_loader_rejects_redirected_registry(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        sources_file = self.registration.layout.sources_file
        outside = self.root / "outside-sources.jsonl"
        outside.write_bytes(sources_file.read_bytes())
        sources_file.unlink()
        try:
            os.symlink(outside, sources_file)
        except OSError as exc:
            sources_file.write_bytes(outside.read_bytes())
            self.skipTest(f"symbolic links unavailable: {exc}")

        with self.assertRaisesRegex(
            SourceRegistryError,
            "stable, non-redirected regular file",
        ):
            load_source_registry(
                self.workspace,
                self.registration.project_id,
            )

    def test_sync_rejects_registry_redirected_after_locked_load(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        sources_file = self.registration.layout.sources_file
        outside = self.root / "outside-sources.jsonl"
        sentinel = b"outside file must remain unchanged\n"
        outside.write_bytes(sentinel)
        real_load = source_registry_module._load_source_registry_file
        swapped = False

        def load_then_redirect(*args, **kwargs):
            nonlocal swapped
            registry = real_load(*args, **kwargs)
            if not swapped:
                original = sources_file.read_bytes()
                sources_file.unlink()
                try:
                    os.symlink(outside, sources_file)
                except OSError as exc:
                    sources_file.write_bytes(original)
                    self.skipTest(f"symbolic links unavailable: {exc}")
                swapped = True
            return registry

        with patch.object(
            source_registry_module,
            "_load_source_registry_file",
            side_effect=load_then_redirect,
        ):
            with self.assertRaisesRegex(
                SourceRegistryError,
                "securely write",
            ):
                sync_source_registry(
                    self.workspace,
                    self.registration.project_id,
                )

        self.assertEqual(outside.read_bytes(), sentinel)
        self.assertTrue(sources_file.is_symlink())

    def test_atomic_writer_pins_machine_root_during_source_registry_replace(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        before_registry = self.registration.layout.sources_file.read_bytes()
        (self.project / "added.txt").write_text(
            "new source\n",
            encoding="utf-8",
            newline="\n",
        )
        inventory_project(self.workspace, self.registration.project_id)

        machine_root = self.registration.layout.machine_root
        displaced = machine_root.with_name(f"{machine_root.name}-displaced")
        outside = self.root / "outside-machine-state"
        outside.mkdir()
        outside_registry = outside / "sources.jsonl"
        outside_lock = outside / "sources.jsonl.lock"
        registry_sentinel = b"outside source registry sentinel\n"
        lock_sentinel = b"outside source lock sentinel\n"
        outside_registry.write_bytes(registry_sentinel)
        outside_lock.write_bytes(lock_sentinel)
        real_write_all = stable_file_access_module._write_all
        attack_attempted = False
        attack_succeeded = False

        def write_while_replacing_root(descriptor: int, payload: bytes) -> None:
            nonlocal attack_attempted, attack_succeeded
            if not attack_attempted:
                attack_attempted = True
                try:
                    machine_root.rename(displaced)
                except OSError:
                    pass
                else:
                    try:
                        os.symlink(outside, machine_root, target_is_directory=True)
                    except OSError:
                        displaced.rename(machine_root)
                    else:
                        attack_succeeded = True
            real_write_all(descriptor, payload)

        error: SourceRegistryError | None = None
        result = None
        try:
            with patch(
                "tools.stable_file_access._write_all",
                side_effect=write_while_replacing_root,
            ):
                try:
                    result = sync_source_registry(
                        self.workspace,
                        self.registration.project_id,
                    )
                except SourceRegistryError as exc:
                    error = exc
        finally:
            if machine_root.is_symlink():
                machine_root.unlink()
            if displaced.exists():
                displaced.rename(machine_root)

        self.assertTrue(attack_attempted)
        self.assertEqual(outside_registry.read_bytes(), registry_sentinel)
        self.assertEqual(outside_lock.read_bytes(), lock_sentinel)
        if attack_succeeded:
            self.assertIsNotNone(error)
            self.assertEqual(
                self.registration.layout.sources_file.read_bytes(),
                before_registry,
            )
        else:
            self.assertIsNone(error)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertTrue(result.wrote_registry)
            self.assertIn(
                "added.txt",
                load_source_registry(
                    self.workspace,
                    self.registration.project_id,
                ).current_by_path,
            )

    def test_existing_lock_times_out_without_modifying_state(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        registry_before = self.registration.layout.sources_file.read_bytes()
        lock_file = self.registration.layout.sources_file.with_name(
            "sources.jsonl.lock"
        )
        ready = threading.Event()
        release = threading.Event()
        holder_error: list[BaseException] = []

        def hold_lock() -> None:
            try:
                with exclusive_stable_file_lock(
                    self.registration.layout.machine_root,
                    lock_file,
                    timeout_seconds=1.0,
                ):
                    ready.set()
                    release.wait(timeout=5.0)
            except BaseException as exc:  # pragma: no cover - surfaced below
                holder_error.append(exc)
                ready.set()

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        self.assertTrue(ready.wait(timeout=2.0), "lock holder did not start")
        try:
            self.assertEqual(holder_error, [])
            with self.assertRaises(SourceRegistryLockError):
                sync_source_registry(
                    self.workspace,
                    self.registration.project_id,
                    lock_timeout_seconds=0.02,
                )
        finally:
            release.set()
            holder.join(timeout=2.0)
        self.assertFalse(holder.is_alive())
        self.assertEqual(holder_error, [])
        self.assertEqual(self.registration.layout.sources_file.read_bytes(), registry_before)
        self.assertTrue(lock_file.is_file())

    def test_post_replace_unknown_is_reported_with_explicit_commit_state(self) -> None:
        sync_source_registry(self.workspace, self.registration.project_id)
        (self.project / "added.txt").write_text(
            "added after first registry commit\n",
            encoding="utf-8",
            newline="\n",
        )
        inventory_project(self.workspace, self.registration.project_id)
        real_write = source_registry_module.write_atomic_stable_file

        def commit_then_report_unknown(*args, **kwargs):
            result = real_write(*args, **kwargs)
            self.assertTrue(result.wrote)
            raise StableFileCommitUnknownError("forced post-replace uncertainty")

        with patch.object(
            source_registry_module,
            "write_atomic_stable_file",
            side_effect=commit_then_report_unknown,
        ):
            with self.assertRaises(SourceRegistryCommitStateError) as raised:
                sync_source_registry(self.workspace, self.registration.project_id)

        self.assertEqual(raised.exception.commit_state, "unknown")
        self.assertIn(
            "added.txt",
            load_source_registry(
                self.workspace,
                self.registration.project_id,
            ).current_by_path,
        )
        self.assertTrue(
            self.registration.layout.sources_file.with_name(
                "sources.jsonl.lock"
            ).is_file()
        )

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
        self.assertEqual(payload["version_count"], self.inventory.record_counts["file"])
        self.assertEqual(
            payload["versions_added_count"],
            self.inventory.record_counts["file"],
        )
        self.assertFalse(payload["upgraded_registry"])
        self.assertTrue(Path(payload["sources_file"]).is_file())


if __name__ == "__main__":
    unittest.main()
