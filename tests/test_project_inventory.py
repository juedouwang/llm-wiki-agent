from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from tools.project_inventory import (
    PROJECT_MANIFEST_KIND,
    PROJECT_MANIFEST_SCHEMA_VERSION,
    PROJECT_MANIFEST_VERSION,
    ProjectInventoryTraversalError,
    inventory_project,
)
from tools.project_registry import ProjectRecordError, register_project
from tools.scan_policy import ScanPolicy, ScanPolicyConfig, load_scan_policy


REPO_ROOT = Path(__file__).parent.parent


class ProjectInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.project = self.root / "research-project"
        self.project.mkdir()
        (self.project / "README.md").write_text(
            "# Inventory Study\n",
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

    def register(self, project_id: str = "inventory-study"):
        return register_project(
            self.workspace,
            self.project,
            project_id=project_id,
        )

    def manifest_rows(self, manifest_file: Path) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in manifest_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def source_snapshot(self) -> tuple[tuple[str, ...], dict[str, tuple[str, int]]]:
        directories: list[str] = []
        files: dict[str, tuple[str, int]] = {}
        for directory, dirnames, filenames in os.walk(self.project):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            for name in dirnames:
                path = base / name
                if path.is_symlink():
                    continue
                directories.append(path.relative_to(self.project).as_posix())
            for name in filenames:
                path = base / name
                if path.is_symlink():
                    continue
                files[path.relative_to(self.project).as_posix()] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mtime_ns,
                )
        return tuple(sorted(directories)), files

    def independent_in_scope_files(
        self,
        config: ScanPolicyConfig | None = None,
    ) -> list[str]:
        policy = load_scan_policy(self.project, config=config)
        found: list[str] = []
        pending: list[tuple[Path, str]] = [(self.project, "")]
        while pending:
            directory, relative_directory = pending.pop()
            with os.scandir(directory) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                path = directory / name
                relative = (
                    f"{relative_directory}/{name}"
                    if relative_directory
                    else name
                )
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    decision = policy.decide_path(relative, is_directory=True)
                    if decision.traverse:
                        pending.append((path, decision.path))
                elif stat.S_ISREG(metadata.st_mode):
                    decision = policy.decide_path(relative, is_directory=False)
                    if decision.included:
                        found.append(decision.path)
        return sorted(found)

    def make_symlink_or_skip(
        self,
        link: Path,
        target: Path,
        *,
        target_is_directory: bool,
    ) -> None:
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")

    def test_inventory_loads_registration_and_records_every_in_scope_file(
        self,
    ) -> None:
        data = self.project / "data"
        data.mkdir()
        (data / "metrics.csv").write_text("score\n0.9\n", encoding="utf-8")
        (data / "weights.unrecognized-format").write_bytes(b"opaque\x00payload")
        (self.project / "NO_EXTENSION").write_bytes(b"unknown")
        registration = self.register()

        result = inventory_project(self.workspace, registration.project_id)
        rows = self.manifest_rows(result.manifest_file)
        summary = rows[0]
        files = sorted(
            row["path"] for row in rows if row["record_type"] == "file"
        )

        self.assertEqual(result.project_root, self.project.resolve())
        self.assertEqual(files, self.independent_in_scope_files())
        self.assertIn("data/weights.unrecognized-format", files)
        self.assertIn("NO_EXTENSION", files)
        self.assertEqual(summary["record_counts"]["file"], len(files))
        self.assertEqual(summary["total_records"], len(rows) - 1)

        with self.assertRaises(ProjectRecordError):
            inventory_project(self.workspace, "not-registered")

    def test_ignore_reinclude_and_excluded_file_are_never_silent(self) -> None:
        (self.project / ".llmwikiignore").write_text(
            "data/\n!data/important.bin\n*.tmp\n",
            encoding="utf-8",
            newline="\n",
        )
        data = self.project / "data"
        raw = data / "raw"
        raw.mkdir(parents=True)
        (data / "important.bin").write_bytes(b"important")
        (raw / "discard.bin").write_bytes(b"discard")
        notes = self.project / "notes"
        notes.mkdir()
        (notes / "draft.tmp").write_text("draft", encoding="utf-8")
        registration = self.register()

        result = inventory_project(self.workspace, registration.project_id)
        rows = self.manifest_rows(result.manifest_file)
        by_path = {row.get("path"): row for row in rows[1:]}

        self.assertEqual(by_path["data/important.bin"]["record_type"], "file")
        self.assertEqual(by_path["data/raw"]["record_type"], "excluded_directory")
        self.assertTrue(by_path["data/raw"]["pruned"])
        self.assertNotIn("data/raw/discard.bin", by_path)
        self.assertEqual(by_path["notes/draft.tmp"]["record_type"], "excluded_file")
        self.assertEqual(
            by_path["notes/draft.tmp"]["effective_reason_code"],
            "ignore-exclude",
        )
        self.assertEqual(by_path[".llmwikiignore"]["record_type"], "file")

    def test_pruned_directory_records_reconcile_with_exclusion_summary(self) -> None:
        registration = self.register()
        for relative in (".git", "node_modules", "scratch"):
            directory = self.project / relative
            directory.mkdir()
            (directory / "hidden.dat").write_bytes(b"hidden")
        (self.project / "notes.tmp").write_text("temporary", encoding="utf-8")
        config = ScanPolicyConfig(exclude_patterns=("scratch/",))

        result = inventory_project(
            self.workspace,
            registration.project_id,
            policy_config=config,
        )
        rows = self.manifest_rows(result.manifest_file)
        summary = rows[0]
        excluded_directories = [
            row for row in rows if row["record_type"] == "excluded_directory"
        ]
        excluded_files = [
            row for row in rows if row["record_type"] == "excluded_file"
        ]

        self.assertEqual(
            {row["path"] for row in excluded_directories},
            {".git", "node_modules", "scratch"},
        )
        self.assertTrue(all(row["pruned"] for row in excluded_directories))
        self.assertEqual(
            sum(
                bucket["pruned_directories"]
                for bucket in summary["exclusion_summary"].values()
            ),
            len(excluded_directories),
        )
        self.assertEqual(
            sum(
                bucket["excluded_files"]
                for bucket in summary["exclusion_summary"].values()
            ),
            len(excluded_files),
        )
        self.assertEqual(
            sum(
                bucket["total_records"]
                for bucket in summary["exclusion_summary"].values()
            ),
            len(excluded_directories) + len(excluded_files),
        )
        paths = {row.get("path") for row in rows}
        self.assertFalse(any(path and path.endswith("hidden.dat") for path in paths))

    def test_manifest_rows_are_versioned_and_keep_later_task_fields_absent(self) -> None:
        (self.project / "artifact.ckpt").write_bytes(b"model")
        registration = self.register()

        result = inventory_project(self.workspace, registration.project_id)
        rows = self.manifest_rows(result.manifest_file)
        actual_counts = Counter(row["record_type"] for row in rows[1:])
        forbidden_keys = {
            "hash",
            "sha256",
            "content_hash",
            "size_bytes",
            "mtime",
            "mtime_ns",
            "format",
            "language",
            "role",
            "research_role",
            "processing_status",
            "read_depth",
            "content",
            "text",
        }

        def nested_keys(value: object) -> set[str]:
            if isinstance(value, dict):
                keys = set(value)
                for nested in value.values():
                    keys.update(nested_keys(nested))
                return keys
            if isinstance(value, list):
                keys: set[str] = set()
                for nested in value:
                    keys.update(nested_keys(nested))
                return keys
            return set()

        for row in rows:
            self.assertEqual(row["schema_version"], PROJECT_MANIFEST_SCHEMA_VERSION)
            self.assertEqual(row["kind"], PROJECT_MANIFEST_KIND)
            self.assertEqual(row["manifest_version"], PROJECT_MANIFEST_VERSION)
            self.assertTrue(forbidden_keys.isdisjoint(nested_keys(row)))

        self.assertEqual(rows[0]["record_type"], "summary")
        self.assertEqual(rows[0]["total_records"], len(rows) - 1)
        self.assertEqual(
            rows[0]["record_counts"],
            {
                record_type: actual_counts[record_type]
                for record_type in rows[0]["record_counts"]
            },
        )

    def test_inventory_does_not_read_source_file_content_or_write_source(self) -> None:
        (self.project / "opaque.bin").write_bytes(os.urandom(256))
        registration = self.register()
        before = self.source_snapshot()
        original_read_bytes = Path.read_bytes
        original_read_text = Path.read_text
        source_root = self.project.resolve()

        def is_source(candidate: Path) -> bool:
            try:
                candidate.expanduser().resolve().relative_to(source_root)
            except ValueError:
                return False
            return True

        def guarded_read_bytes(candidate: Path) -> bytes:
            if is_source(candidate):
                raise AssertionError(f"source content read: {candidate}")
            return original_read_bytes(candidate)

        def guarded_read_text(candidate: Path, *args, **kwargs) -> str:
            if is_source(candidate):
                raise AssertionError(f"source content read: {candidate}")
            return original_read_text(candidate, *args, **kwargs)

        with patch.object(Path, "read_bytes", guarded_read_bytes), patch.object(
            Path,
            "read_text",
            guarded_read_text,
        ):
            result = inventory_project(self.workspace, registration.project_id)

        self.assertTrue(result.manifest_file.is_file())
        self.assertEqual(before, self.source_snapshot())

    def test_repeated_unchanged_inventory_is_byte_stable(self) -> None:
        registration = self.register()

        first = inventory_project(self.workspace, registration.project_id)
        first_bytes = first.manifest_file.read_bytes()
        second = inventory_project(self.workspace, registration.project_id)

        self.assertEqual(first.manifest_file, second.manifest_file)
        self.assertEqual(first_bytes, second.manifest_file.read_bytes())

    def test_inventory_consumes_b02_symlink_decision_without_os_link_privilege(
        self,
    ) -> None:
        registration = self.register()
        target = self.project / "README.md"
        simulated_link = self.project / "simulated-link"
        simulated_link.write_text("placeholder", encoding="utf-8")
        original_lstat = Path.lstat
        captured: dict[str, object] = {}

        def symlink_lstat(candidate: Path) -> os.stat_result:
            metadata = original_lstat(candidate)
            if candidate == simulated_link:
                values = list(metadata)
                values[0] = stat.S_IFLNK | stat.S_IMODE(metadata.st_mode)
                return os.stat_result(values)
            return metadata

        def decide_with_b02(
            policy: ScanPolicy,
            link_path: str | Path,
            *,
            ancestor_realpaths=(),
            visited_realpaths=(),
        ):
            captured.update(
                {
                    "link_path": str(link_path),
                    "ancestor_realpaths": tuple(ancestor_realpaths),
                    "visited_realpaths": tuple(visited_realpaths),
                }
            )
            return policy.assess_symlink_target(
                link_path,
                target,
                ancestor_realpaths=ancestor_realpaths,
                visited_realpaths=visited_realpaths,
            )

        with patch.object(Path, "lstat", symlink_lstat), patch.object(
            ScanPolicy,
            "decide_symlink",
            autospec=True,
            side_effect=decide_with_b02,
        ) as decide_symlink:
            result = inventory_project(
                self.workspace,
                registration.project_id,
                policy_config=ScanPolicyConfig(follow_symlinks=True),
            )

        rows = self.manifest_rows(result.manifest_file)
        record = next(row for row in rows if row.get("path") == "simulated-link")
        decide_symlink.assert_called_once()
        self.assertEqual(captured["link_path"], "simulated-link")
        self.assertIn(self.project.resolve(), captured["ancestor_realpaths"])
        self.assertIn(self.project.resolve(), captured["visited_realpaths"])
        self.assertEqual(record["record_type"], "symlink")
        self.assertTrue(record["followed"])
        self.assertEqual(record["follow_reason_code"], "symlink-follow-safe")

    def test_default_symlink_policy_records_links_without_following(self) -> None:
        registration = self.register()
        target = self.project / "README.md"
        link = self.project / "readme-link"
        self.make_symlink_or_skip(link, target, target_is_directory=False)

        result = inventory_project(self.workspace, registration.project_id)
        rows = self.manifest_rows(result.manifest_file)
        record = next(row for row in rows if row.get("path") == "readme-link")

        self.assertEqual(record["record_type"], "symlink")
        self.assertFalse(record["followed"])
        self.assertEqual(record["follow_reason_code"], "symlink-follow-disabled")
        self.assertEqual(
            rows[0]["symlink_summary"],
            {"symlink-follow-disabled": 1},
        )

    def test_enabled_symlink_policy_uses_safe_outside_cycle_and_duplicate_guards(
        self,
    ) -> None:
        real = self.project / "real-data"
        real.mkdir()
        target_file = real / "value.dat"
        target_file.write_bytes(b"value")
        outside = self.root / "outside-data"
        outside.mkdir()
        registration = self.register()

        self.make_symlink_or_skip(
            self.project / "a-file-link",
            target_file,
            target_is_directory=False,
        )
        self.make_symlink_or_skip(
            self.project / "b-file-link",
            target_file,
            target_is_directory=False,
        )
        self.make_symlink_or_skip(
            self.project / "directory-alias",
            real,
            target_is_directory=True,
        )
        self.make_symlink_or_skip(
            self.project / "outside-link",
            outside,
            target_is_directory=True,
        )
        self.make_symlink_or_skip(
            self.project / "loop-link",
            self.project,
            target_is_directory=True,
        )

        result = inventory_project(
            self.workspace,
            registration.project_id,
            policy_config=ScanPolicyConfig(follow_symlinks=True),
        )
        rows = self.manifest_rows(result.manifest_file)
        links = {
            row["path"]: row for row in rows if row["record_type"] == "symlink"
        }

        self.assertTrue(links["a-file-link"]["followed"])
        self.assertEqual(
            links["a-file-link"]["follow_reason_code"],
            "symlink-follow-safe",
        )
        self.assertFalse(links["b-file-link"]["followed"])
        self.assertEqual(
            links["b-file-link"]["follow_reason_code"],
            "symlink-target-already-visited",
        )
        self.assertEqual(
            links["directory-alias"]["follow_reason_code"],
            "symlink-target-already-visited",
        )
        self.assertEqual(
            links["outside-link"]["follow_reason_code"],
            "symlink-target-outside-project",
        )
        self.assertEqual(
            links["loop-link"]["follow_reason_code"],
            "symlink-cycle",
        )

    def test_scan_failure_preserves_existing_manifest_and_removes_temporaries(
        self,
    ) -> None:
        registration = self.register()
        first = inventory_project(self.workspace, registration.project_id)
        previous = first.manifest_file.read_bytes()
        blocked = self.project / "blocked"
        blocked.mkdir()
        (blocked / "unseen.txt").write_text("unseen", encoding="utf-8")
        original_scandir = os.scandir

        def failing_scandir(path):
            if Path(path).resolve() == blocked.resolve():
                raise PermissionError("simulated directory denial")
            return original_scandir(path)

        with patch("tools.project_inventory.os.scandir", side_effect=failing_scandir):
            with self.assertRaises(ProjectInventoryTraversalError):
                inventory_project(self.workspace, registration.project_id)

        self.assertEqual(first.manifest_file.read_bytes(), previous)
        self.assertEqual(
            list(first.manifest_file.parent.glob(".manifest.jsonl.*")),
            [],
        )

    def test_inventory_cli_json_is_parseable_and_honors_explicit_rules(self) -> None:
        registration = self.register()
        (self.project / "draft.tmp").write_text("draft", encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "inventory",
                registration.project_id,
                "--workspace-root",
                str(self.workspace),
                "--exclude",
                "*.tmp",
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
        self.assertEqual(payload["project_id"], registration.project_id)
        manifest = Path(payload["manifest_file"])
        self.assertTrue(manifest.is_file())
        rows = self.manifest_rows(manifest)
        draft = next(row for row in rows if row.get("path") == "draft.tmp")
        self.assertEqual(draft["record_type"], "excluded_file")
        self.assertEqual(
            draft["effective_reason_code"],
            "explicit-exclude",
        )


if __name__ == "__main__":
    unittest.main()
