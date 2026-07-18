from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import source_recovery as source_recovery_module
from tools import source_registry as source_registry_module
from tools import stable_file_access as stable_file_access_module
from tools.evidence_registry import register_evidence
from tools.extraction_schema import LineRangeLocator
from tools.project_inventory import inventory_project
from tools.project_layout import CURRENT_SCHEMA_VERSION
from tools.project_registry import register_project
from tools.scan_policy import ScanPolicyConfig
from tools.source_access import (
    SourceRelocationAmbiguousError,
    locate_source,
    open_evidence,
    open_source,
)
from tools.source_recovery import (
    SOURCE_RECOVERY_ATTEMPT_KIND,
    SOURCE_RECOVERY_KIND,
    SOURCE_RECOVERY_VERSION,
    SOURCE_RELOCATION_INSPECTION_KIND,
    SOURCE_RELOCATION_INSPECTION_VERSION,
    SourceRecoveryError,
    inspect_source_relocation,
    recover_source,
)
from tools.source_registry import (
    SourceRegistryConflictError,
    load_source_registry,
    record_source_relocation,
    sync_source_registry,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


class SourceRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.project = self.root / "research-project"
        self.original = self.project / "docs" / "original.txt"
        self.original.parent.mkdir(parents=True)
        self.source_bytes = b"alpha = 1\nbeta = 2\ngamma = alpha + beta\n"
        self.original.write_bytes(self.source_bytes)
        (self.project / "notes.txt").write_text(
            "unrelated project note\n",
            encoding="utf-8",
            newline="\n",
        )
        self.registration = register_project(
            self.workspace,
            self.project,
            project_id="source-recovery-study",
        )
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)
        self.source = self.source_for("docs/original.txt")

    def source_for(self, relative_path: str):
        registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        return registry.current_by_path[relative_path]

    def current_source(self):
        registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )
        return registry.by_source_id[self.source.source_id]

    def move_original(self, relative_path: str) -> Path:
        destination = self.project.joinpath(*relative_path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.original.replace(destination)
        return destination

    def project_snapshot(self) -> dict[str, tuple[str, int]]:
        snapshot: dict[str, tuple[str, int]] = {}
        for directory, dirnames, filenames in os.walk(self.project):
            dirnames[:] = sorted(name for name in dirnames if name != ".git")
            for filename in sorted(filenames):
                path = Path(directory) / filename
                if path.is_symlink():
                    continue
                relative = path.relative_to(self.project).as_posix()
                snapshot[relative] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mtime_ns,
                )
        return snapshot

    def run_recover_cli(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "source",
                "recover",
                self.registration.project_id,
                self.source.source_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.root,
        )

    def test_current_valid_path_is_not_needed_and_does_not_write_registry(self) -> None:
        sources_file = self.registration.layout.sources_file
        before_registry = sources_file.read_bytes()
        before_mtime = sources_file.stat().st_mtime_ns
        before_source = self.project_snapshot()

        result = recover_source(
            self.workspace,
            self.registration.project_id,
            self.source.source_id,
        )

        self.assertEqual(result.status, "not-needed")
        self.assertEqual(result.reason_code, "source-current-path-valid")
        self.assertIsNone(result.recovery_method)
        self.assertEqual(result.attempts, ())
        self.assertFalse(result.wrote_registry)
        self.assertEqual(sources_file.read_bytes(), before_registry)
        self.assertEqual(sources_file.stat().st_mtime_ns, before_mtime)
        self.assertEqual(self.project_snapshot(), before_source)

        with mock.patch(
            "tools.source_access.recover_source",
            side_effect=AssertionError("valid locate must not invoke recovery"),
        ):
            location = locate_source(
                self.workspace,
                self.registration.project_id,
                self.source.source_id,
            )
        self.assertEqual(location.current_path, "docs/original.txt")

    def test_read_only_relocation_inspection_never_binds_a_candidate(self) -> None:
        sources_file = self.registration.layout.sources_file
        before_registry = sources_file.read_bytes()
        before_mtime = sources_file.stat().st_mtime_ns

        current = inspect_source_relocation(
            self.workspace,
            self.registration.project_id,
            self.source.source_id,
        )

        self.assertEqual(current.status, "current")
        self.assertEqual(current.reason_code, "source-current-path-valid")
        self.assertFalse(current.as_dict()["registry_write_performed"])
        self.assertEqual(sources_file.read_bytes(), before_registry)
        self.assertEqual(sources_file.stat().st_mtime_ns, before_mtime)

        moved = self.move_original("moved/current.txt")
        inventory_project(self.workspace, self.registration.project_id)
        source_before_inspection = self.project_snapshot()
        inspection = inspect_source_relocation(
            self.workspace,
            self.registration.project_id,
            self.source.source_id,
        )

        self.assertEqual(inspection.status, "relocatable")
        self.assertEqual(inspection.recovery_method, "content-hash")
        self.assertEqual(inspection.candidate_paths, ("moved/current.txt",))
        payload = inspection.as_dict()
        self.assertEqual(payload["kind"], SOURCE_RELOCATION_INSPECTION_KIND)
        self.assertEqual(
            payload["inspection_version"],
            SOURCE_RELOCATION_INSPECTION_VERSION,
        )
        self.assertFalse(payload["registry_write_performed"])
        self.assertEqual(sources_file.read_bytes(), before_registry)
        self.assertEqual(sources_file.stat().st_mtime_ns, before_mtime)
        self.assertEqual(self.project_snapshot(), source_before_inspection)
        self.assertEqual(moved.read_bytes(), self.source_bytes)
        self.assertEqual(self.current_source().current_path, "docs/original.txt")

    def test_recovery_fails_closed_if_source_version_changes_after_inspection(self) -> None:
        self.move_original("moved/candidate.txt")
        inventory_project(self.workspace, self.registration.project_id)
        real_inspect = inspect_source_relocation

        def inspect_then_advance(*args, **kwargs):
            inspection = real_inspect(*args, **kwargs)
            self.original.parent.mkdir(parents=True, exist_ok=True)
            self.original.write_text(
                "replacement version\n",
                encoding="utf-8",
                newline="\n",
            )
            inventory_project(self.workspace, self.registration.project_id)
            sync_source_registry(self.workspace, self.registration.project_id)
            return inspection

        with mock.patch(
            "tools.source_recovery.inspect_source_relocation",
            side_effect=inspect_then_advance,
        ):
            with self.assertRaisesRegex(
                SourceRecoveryError,
                "source registry changed during relocation recovery inspection",
            ):
                recover_source(
                    self.workspace,
                    self.registration.project_id,
                    self.source.source_id,
                )

        current = self.current_source()
        self.assertEqual(current.current_path, "docs/original.txt")
        self.assertEqual(current.current_version, 2)
        self.assertEqual(
            self.original.read_text(encoding="utf-8"),
            "replacement version\n",
        )

    def test_unique_manifest_hash_move_preserves_identity_versions_and_source(self) -> None:
        moved = self.move_original("moved/current.txt")
        inventory_project(self.workspace, self.registration.project_id)
        before_recovery_source = self.project_snapshot()
        versions_before = self.source.versions
        generation_before = self.source.last_seen_scan_generation

        result = recover_source(
            self.workspace,
            self.registration.project_id,
            self.source.source_id,
        )

        self.assertEqual(result.status, "recovered")
        self.assertEqual(result.recovery_method, "content-hash")
        self.assertEqual(result.previous_path, "docs/original.txt")
        self.assertEqual(result.current_path, "moved/current.txt")
        self.assertEqual(result.candidate_paths, ("moved/current.txt",))
        self.assertTrue(result.wrote_registry)
        recovered = self.current_source()
        self.assertEqual(recovered.source_id, self.source.source_id)
        self.assertEqual(recovered.current_path, "moved/current.txt")
        self.assertEqual(recovered.versions, versions_before)
        self.assertEqual(recovered.last_seen_scan_generation, generation_before)
        self.assertEqual(
            {item.path for item in recovered.path_history},
            {"docs/original.txt", "moved/current.txt"},
        )
        self.assertEqual(moved.read_bytes(), self.source_bytes)
        self.assertEqual(self.project_snapshot(), before_recovery_source)

    def test_historical_path_alias_has_priority_over_manifest_and_git(self) -> None:
        moved = self.move_original("moved/current.txt")
        inventory_project(self.workspace, self.registration.project_id)
        first = recover_source(
            self.workspace,
            self.registration.project_id,
            self.source.source_id,
        )
        self.assertEqual(first.recovery_method, "content-hash")

        self.original.parent.mkdir(parents=True, exist_ok=True)
        moved.replace(self.original)
        before_registry_versions = self.current_source().versions
        second = recover_source(
            self.workspace,
            self.registration.project_id,
            self.source.source_id,
        )

        self.assertEqual(second.status, "recovered")
        self.assertEqual(second.recovery_method, "path-alias")
        self.assertEqual(second.current_path, "docs/original.txt")
        self.assertEqual(len(second.attempts), 1)
        recovered = self.current_source()
        self.assertEqual(recovered.versions, before_registry_versions)
        self.assertEqual(len(recovered.path_history), 2)

    def test_local_git_history_recovers_move_with_stale_manifest(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("Git is unavailable")
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        subprocess.run(
            ["git", "config", "user.email", "tests@example.invalid"],
            cwd=self.project,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "LLM Wiki Tests"],
            cwd=self.project,
            check=True,
        )
        subprocess.run(
            ["git", "config", "core.autocrlf", "false"],
            cwd=self.project,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=self.project, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial source"],
            cwd=self.project,
            check=True,
        )
        destination = self.project / "renamed.txt"
        subprocess.run(
            ["git", "mv", "docs/original.txt", "renamed.txt"],
            cwd=self.project,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "rename source"],
            cwd=self.project,
            check=True,
        )

        result = recover_source(
            self.workspace,
            self.registration.project_id,
            self.source.source_id,
        )

        self.assertEqual(result.status, "recovered")
        self.assertEqual(result.recovery_method, "git-history")
        self.assertEqual(result.current_path, "renamed.txt")
        self.assertEqual(len(result.attempts), 3)
        self.assertEqual(destination.read_bytes(), self.source_bytes)
        self.assertEqual(self.current_source().source_id, self.source.source_id)

    def test_duplicate_manifest_hash_candidates_are_ambiguous_without_binding(self) -> None:
        first = self.move_original("duplicates/first.txt")
        second = self.project / "duplicates" / "second.txt"
        shutil.copy2(first, second)
        inventory_project(self.workspace, self.registration.project_id)
        sources_file = self.registration.layout.sources_file
        before_registry = sources_file.read_bytes()

        result = recover_source(
            self.workspace,
            self.registration.project_id,
            self.source.source_id,
        )

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.reason_code, "source-relocation-ambiguous")
        self.assertEqual(result.recovery_method, "content-hash")
        self.assertEqual(
            result.candidate_paths,
            ("duplicates/first.txt", "duplicates/second.txt"),
        )
        self.assertFalse(result.wrote_registry)
        self.assertEqual(sources_file.read_bytes(), before_registry)
        self.assertEqual(self.current_source().current_path, "docs/original.txt")
        with self.assertRaises(SourceRelocationAmbiguousError):
            locate_source(
                self.workspace,
                self.registration.project_id,
                self.source.source_id,
            )

    def test_changed_moved_bytes_are_unresolved_without_registry_write(self) -> None:
        moved = self.move_original("moved/changed.txt")
        moved.write_bytes(b"changed bytes\n")
        inventory_project(self.workspace, self.registration.project_id)
        sources_file = self.registration.layout.sources_file
        before_registry = sources_file.read_bytes()

        result = recover_source(
            self.workspace,
            self.registration.project_id,
            self.source.source_id,
        )

        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.reason_code, "source-relocation-not-found")
        self.assertEqual(
            result.current_failure_reason_code,
            "source-current-path-missing",
        )
        self.assertFalse(result.wrote_registry)
        self.assertEqual(sources_file.read_bytes(), before_registry)

    def test_outside_symlink_does_not_rebind_source(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_bytes(self.source_bytes)
        self.original.unlink()
        try:
            os.symlink(outside, self.original)
        except OSError as exc:
            self.original.write_bytes(self.source_bytes)
            self.skipTest(f"symbolic links unavailable: {exc}")
        sources_file = self.registration.layout.sources_file
        before_registry = sources_file.read_bytes()

        result = recover_source(
            self.workspace,
            self.registration.project_id,
            self.source.source_id,
        )

        self.assertEqual(result.status, "unresolved")
        self.assertEqual(
            result.current_failure_reason_code,
            "source-path-outside-project",
        )
        self.assertFalse(result.wrote_registry)
        self.assertEqual(sources_file.read_bytes(), before_registry)

    def test_blocked_equal_priority_symlink_candidate_fails_closed(self) -> None:
        real = self.move_original("candidates/real.txt")
        link = self.project / "candidates" / "linked.txt"
        try:
            os.symlink(real.name, link)
        except OSError as exc:
            self.original.parent.mkdir(parents=True, exist_ok=True)
            real.replace(self.original)
            self.skipTest(f"symbolic links unavailable: {exc}")
        inventory_project(
            self.workspace,
            self.registration.project_id,
            policy_config=ScanPolicyConfig(follow_symlinks=True),
        )
        sources_file = self.registration.layout.sources_file
        before_registry = sources_file.read_bytes()

        result = recover_source(
            self.workspace,
            self.registration.project_id,
            self.source.source_id,
        )

        self.assertEqual(result.status, "unresolved")
        self.assertEqual(
            result.reason_code,
            "source-relocation-candidate-unreadable",
        )
        self.assertEqual(result.recovery_method, "content-hash")
        self.assertIn("candidates/linked.txt", result.attempts[-1].blocked_paths)
        self.assertEqual(sources_file.read_bytes(), before_registry)

    def test_inspection_binds_read_to_registered_lexical_path(self) -> None:
        internal = self.project / "docs" / "internal-target.txt"
        outside = self.root / "outside-race.txt"
        internal.write_bytes(self.source_bytes)
        outside.write_bytes(self.source_bytes)
        self.original.unlink()
        try:
            os.symlink(internal.name, self.original)
        except OSError as exc:
            self.original.write_bytes(self.source_bytes)
            self.skipTest(f"symbolic links unavailable: {exc}")
        sources_file = self.registration.layout.sources_file
        before_registry = sources_file.read_bytes()
        real_resolve = source_recovery_module._resolve_observation_path
        redirected = False

        def resolve_then_redirect(
            project_root: Path,
            path: str,
            *,
            reject_reparse_points: bool,
        ):
            nonlocal redirected
            result = real_resolve(
                project_root,
                path,
                reject_reparse_points=reject_reparse_points,
            )
            if not redirected and path == "docs/original.txt":
                self.original.unlink()
                try:
                    os.symlink(outside, self.original)
                except OSError as exc:
                    self.skipTest(f"symbolic-link retargeting unavailable: {exc}")
                redirected = True
            return result

        with mock.patch.object(
            source_recovery_module,
            "_resolve_observation_path",
            side_effect=resolve_then_redirect,
        ):
            result = inspect_source_relocation(
                self.workspace,
                self.registration.project_id,
                self.source.source_id,
            )

        self.assertNotEqual(result.status, "current")
        self.assertEqual(
            result.current_failure_reason_code,
            "source-path-outside-project",
        )
        self.assertEqual(sources_file.read_bytes(), before_registry)

    def test_mutating_relocation_rejects_descriptor_escape_before_read(self) -> None:
        moved = self.move_original("moved/descriptor-race.txt")
        outside = self.root / "outside.txt"
        outside.write_bytes(self.source_bytes)
        sources_file = self.registration.layout.sources_file
        before_registry = sources_file.read_bytes()
        assert self.source.current_content_hash is not None
        assert self.source.current_version is not None

        real_descriptor_path = stable_file_access_module._descriptor_final_path
        moved_path = moved.resolve()

        def redirect_candidate_descriptor(descriptor: int):
            actual = real_descriptor_path(descriptor)
            if actual == moved_path:
                return outside.resolve()
            return actual

        with mock.patch(
            "tools.stable_file_access._descriptor_final_path",
            side_effect=redirect_candidate_descriptor,
        ):
            with self.assertRaises(SourceRegistryConflictError):
                record_source_relocation(
                    self.workspace,
                    self.registration.project_id,
                    source_id=self.source.source_id,
                    recovered_path=moved.relative_to(self.project).as_posix(),
                    expected_content_hash=self.source.current_content_hash,
                    expected_current_path=self.source.current_path,
                    expected_current_version=self.source.current_version,
                )

        self.assertEqual(sources_file.read_bytes(), before_registry)
        self.assertEqual(self.current_source().current_path, self.source.current_path)

    def test_relocation_restores_exact_registry_preimage_after_candidate_failure(self) -> None:
        moved = self.move_original("moved/commit-race.txt")
        sources_file = self.registration.layout.sources_file
        canonical_rows = [
            json.loads(line)
            for line in sources_file.read_text(encoding="utf-8").splitlines()
        ]
        before_registry = (
            "\n".join(
                json.dumps(row, ensure_ascii=False, separators=(", ", ": "))
                for row in canonical_rows
            )
            + "\n"
        ).encode("utf-8")
        sources_file.write_bytes(before_registry)
        self.assertNotEqual(
            before_registry,
            load_source_registry(
                self.workspace,
                self.registration.project_id,
            ).serialized_bytes(),
        )
        real_revalidate = stable_file_access_module.StableFileLease.revalidate
        candidate_revalidations = 0
        assert self.source.current_content_hash is not None
        assert self.source.current_version is not None

        def fail_candidate_after_commit(lease):
            nonlocal candidate_revalidations
            if lease.lexical_path == moved.resolve():
                candidate_revalidations += 1
                if candidate_revalidations == 2:
                    raise stable_file_access_module.StableFileChangedError(
                        "forced candidate currentness failure"
                    )
            return real_revalidate(lease)

        with mock.patch.object(
            stable_file_access_module.StableFileLease,
            "revalidate",
            side_effect=fail_candidate_after_commit,
            autospec=True,
        ):
            with self.assertRaisesRegex(
                SourceRegistryConflictError,
                "exact CAS rollback restored and verified",
            ):
                record_source_relocation(
                    self.workspace,
                    self.registration.project_id,
                    source_id=self.source.source_id,
                    recovered_path=moved.relative_to(self.project).as_posix(),
                    expected_content_hash=self.source.current_content_hash,
                    expected_current_path=self.source.current_path,
                    expected_current_version=self.source.current_version,
                )

        self.assertEqual(candidate_revalidations, 2)
        self.assertEqual(sources_file.read_bytes(), before_registry)
        self.assertEqual(self.current_source().current_path, self.source.current_path)
        self.assertEqual(moved.read_bytes(), self.source_bytes)

    def test_relocation_cas_mismatch_refuses_to_overwrite_foreign_registry(self) -> None:
        moved = self.move_original("moved/cas-mismatch.txt")
        sources_file = self.registration.layout.sources_file
        foreign_payload = b'{"foreign":"writer-owned-bytes"}\n'
        real_write = source_registry_module._write_registry_atomic
        real_revalidate = stable_file_access_module.StableFileLease.revalidate
        candidate_revalidations = 0
        assert self.source.current_content_hash is not None
        assert self.source.current_version is not None

        def write_then_interfere(
            registry,
            *,
            trusted_root: Path,
            root_lease,
        ):
            result = real_write(
                registry,
                trusted_root=trusted_root,
                root_lease=root_lease,
            )
            sources_file.write_bytes(foreign_payload)
            return result

        def fail_candidate_after_commit(lease):
            nonlocal candidate_revalidations
            if lease.lexical_path == moved.resolve():
                candidate_revalidations += 1
                if candidate_revalidations == 2:
                    raise stable_file_access_module.StableFileChangedError(
                        "forced candidate currentness failure"
                    )
            return real_revalidate(lease)

        with mock.patch.object(
            source_registry_module,
            "_write_registry_atomic",
            side_effect=write_then_interfere,
        ), mock.patch.object(
            stable_file_access_module.StableFileLease,
            "revalidate",
            side_effect=fail_candidate_after_commit,
            autospec=True,
        ):
            with self.assertRaisesRegex(
                SourceRegistryConflictError,
                "CAS rollback was refused",
            ):
                record_source_relocation(
                    self.workspace,
                    self.registration.project_id,
                    source_id=self.source.source_id,
                    recovered_path=moved.relative_to(self.project).as_posix(),
                    expected_content_hash=self.source.current_content_hash,
                    expected_current_path=self.source.current_path,
                    expected_current_version=self.source.current_version,
                )

        self.assertEqual(candidate_revalidations, 2)
        self.assertEqual(sources_file.read_bytes(), foreign_payload)
        self.assertEqual(moved.read_bytes(), self.source_bytes)

    def test_concurrent_cli_recovery_has_one_writer_and_no_duplicate_history(self) -> None:
        moved = self.move_original("moved/concurrent.txt")
        inventory_project(self.workspace, self.registration.project_id)
        command = [
            sys.executable,
            "-B",
            str(REPO_ROOT / "tools" / "project.py"),
            "source",
            "recover",
            self.registration.project_id,
            self.source.source_id,
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
            for _ in range(5)
        ]
        payloads: list[dict[str, object]] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            payloads.append(json.loads(stdout))

        self.assertTrue(all(payload["ok"] for payload in payloads))
        self.assertEqual(
            sum(bool(payload["wrote_registry"]) for payload in payloads),
            1,
        )
        self.assertTrue(
            all(payload["current_path"] == "moved/concurrent.txt" for payload in payloads)
        )
        self.assertTrue(
            all(payload["status"] in {"recovered", "not-needed"} for payload in payloads)
        )
        recovered = self.current_source()
        self.assertEqual(recovered.current_path, "moved/concurrent.txt")
        self.assertEqual(
            [item.path for item in recovered.path_history].count("moved/concurrent.txt"),
            1,
        )
        self.assertEqual(moved.read_bytes(), self.source_bytes)

    def test_cli_json_has_versioned_structured_result_for_all_outcomes(self) -> None:
        moved = self.move_original("moved/cli.txt")
        inventory_project(self.workspace, self.registration.project_id)

        completed = self.run_recover_cli()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(payload["kind"], SOURCE_RECOVERY_KIND)
        self.assertEqual(payload["recovery_version"], SOURCE_RECOVERY_VERSION)
        self.assertEqual(payload["status"], "recovered")
        self.assertEqual(payload["recovery_method"], "content-hash")
        self.assertEqual(payload["current_path"], "moved/cli.txt")
        self.assertTrue(payload["wrote_registry"])
        self.assertEqual(payload["attempts"][0]["kind"], SOURCE_RECOVERY_ATTEMPT_KIND)
        self.assertEqual(moved.read_bytes(), self.source_bytes)

    def test_locate_recovers_missing_path_and_open_recovers_hash_mismatch(self) -> None:
        moved = self.move_original("moved/located.txt")
        inventory_project(self.workspace, self.registration.project_id)
        location = locate_source(
            self.workspace,
            self.registration.project_id,
            self.source.source_id,
        )
        self.assertEqual(location.current_path, "moved/located.txt")
        self.assertEqual(location.absolute_path, moved.resolve())

        candidate = self.project / "moved" / "opened.txt"
        candidate.write_bytes(self.source_bytes)
        moved.write_bytes(b"replacement content\n")
        inventory_project(self.workspace, self.registration.project_id)
        reopened = open_source(
            self.workspace,
            self.registration.project_id,
            source_id=self.source.source_id,
            locator=LineRangeLocator(2, 2),
            expected_content_hash=hashlib.sha256(self.source_bytes).hexdigest(),
        )
        self.assertEqual(reopened.source.current_path, "moved/opened.txt")
        self.assertEqual(reopened.excerpt, "beta = 2\n")

    def test_evidence_reopens_after_relocation_without_weakening_identity_checks(self) -> None:
        evidence = register_evidence(
            self.workspace,
            self.registration.project_id,
            source_id=self.source.source_id,
            content_hash=self.source.current_content_hash,
            locator=LineRangeLocator(1, 2),
            excerpt="alpha = 1\nbeta = 2\n",
        ).evidence
        moved = self.move_original("moved/evidence.txt")
        inventory_project(self.workspace, self.registration.project_id)

        reopened = open_evidence(
            self.workspace,
            self.registration.project_id,
            evidence.evidence_id,
        )

        self.assertEqual(reopened.source.source_id, self.source.source_id)
        self.assertEqual(reopened.source.current_path, "moved/evidence.txt")
        self.assertEqual(reopened.evidence_source_version, 1)
        self.assertTrue(reopened.excerpt_hash_verified)
        self.assertEqual(reopened.excerpt, "alpha = 1\nbeta = 2\n")
        self.assertEqual(moved.read_bytes(), self.source_bytes)


if __name__ == "__main__":
    unittest.main()
