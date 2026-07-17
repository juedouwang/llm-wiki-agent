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

from tools.project_layout import (
    CURRENT_SCHEMA_VERSION,
    InvalidProjectIdError,
    ProjectLayout,
    load_versioned_json,
)
from tools.project_registry import (
    MANUAL_PROJECT_ID_STRATEGY,
    PROJECT_ID_STRATEGY,
    PROJECT_SCHEMA_KIND,
    ProjectConflictError,
    ProjectPathError,
    ProjectRecordError,
    ProjectRegistrationError,
    generate_project_id,
    register_project,
    sanitize_git_remote_url,
)


REPO_ROOT = Path(__file__).parent.parent

EXPECTED_KNOWLEDGE_DIRECTORY_PATHS = (
    "papers",
    "methods",
    "datasets",
    "experiments",
    "results",
    "claims",
    "plans",
    "plans/daily",
    "decisions",
    "sources",
)


class ProjectRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.projects = self.root / "research-projects"
        self.projects.mkdir()

    def make_project(self, relative_path: str = "tiny-study") -> Path:
        project = self.projects / Path(relative_path)
        project.mkdir(parents=True)
        (project / "README.md").write_text(
            "# Tiny Study\n\nResearch source.\n",
            encoding="utf-8",
            newline="\n",
        )
        source_dir = project / "src"
        source_dir.mkdir()
        (source_dir / "model.py").write_text(
            "VALUE = 7\n",
            encoding="utf-8",
            newline="\n",
        )
        return project

    def source_snapshot(self, project: Path) -> tuple[tuple[str, ...], dict[str, str]]:
        directories: list[str] = []
        files: dict[str, str] = {}
        for path in sorted(project.rglob("*")):
            relative = path.relative_to(project).as_posix()
            if path.is_dir():
                directories.append(relative)
            elif path.is_file():
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return tuple(directories), files

    def test_registration_writes_schema_v1_without_scanning_or_source_writes(self) -> None:
        project = self.make_project()
        before = self.source_snapshot(project)

        result = register_project(
            self.workspace,
            project / "." / "src" / "..",
            name="Tiny Research Study",
            final_goal="Reproduce the baseline",
            current_stage="setup",
            important_question="Does the baseline reproduce?",
            deadline="2026-12-31",
            daily_available_hours=2.5,
        )

        self.assertTrue(result.created)
        self.assertEqual(result.project_root, project.resolve())
        self.assertEqual(result.project_id, generate_project_id(project))
        self.assertRegex(result.project_id, r"^tiny-study-[0-9a-f]{12}$")
        self.assertEqual(before, self.source_snapshot(project))

        document = load_versioned_json(result.project_file, allow_legacy=False)
        record = document.data
        self.assertEqual(document.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(record["kind"], PROJECT_SCHEMA_KIND)
        self.assertEqual(record["identity_strategy"], PROJECT_ID_STRATEGY)
        self.assertEqual(record["project_id"], result.project_id)
        self.assertEqual(record["name"], "Tiny Research Study")
        self.assertEqual(record["root_path"], str(project.resolve()))
        self.assertTrue(record["registered_at"].endswith("Z"))
        self.assertEqual(
            record["storage"]["machine_root"],
            str(result.layout.machine_root),
        )
        self.assertEqual(
            record["storage"]["knowledge_root"],
            str(result.layout.knowledge_root),
        )
        self.assertEqual(
            record["onboarding"],
            {
                "final_goal": "Reproduce the baseline",
                "current_stage": "setup",
                "important_question": "Does the baseline reproduce?",
                "deadline": "2026-12-31",
                "daily_available_hours": 2.5,
            },
        )

        # B-01 creates identity and empty storage only; scanning starts at B-03.
        self.assertFalse(result.layout.manifest_file.exists())
        self.assertFalse(result.layout.sources_file.exists())
        self.assertFalse(result.layout.overview_file.exists())
        self.assertEqual(list(result.layout.extracted_dir.iterdir()), [])
        self.assertEqual(list(result.layout.indexes_dir.iterdir()), [])
        self.assertEqual(list(result.layout.runs_dir.iterdir()), [])
        self.assertEqual(
            tuple(
                path.relative_to(result.layout.knowledge_root).as_posix()
                for path in result.layout.knowledge_directories
            ),
            EXPECTED_KNOWLEDGE_DIRECTORY_PATHS,
        )
        for directory in result.layout.knowledge_directories:
            self.assertTrue(directory.is_dir(), directory)
        self.assertEqual(
            [
                path
                for path in result.layout.knowledge_root.rglob("*")
                if path.is_file()
            ],
            [],
        )

    def test_repeated_registration_is_idempotent_and_does_not_rewrite_record(self) -> None:
        project = self.make_project()
        first = register_project(self.workspace, project)
        original_bytes = first.project_file.read_bytes()
        fixed_time_ns = 1_700_000_000_000_000_000
        os.utime(first.project_file, ns=(fixed_time_ns, fixed_time_ns))

        second = register_project(self.workspace, project)

        self.assertFalse(second.created)
        self.assertEqual(second.project_id, first.project_id)
        self.assertEqual(second.record, first.record)
        self.assertEqual(second.project_file.read_bytes(), original_bytes)
        self.assertEqual(second.project_file.stat().st_mtime_ns, fixed_time_ns)

    def test_repeat_uses_persisted_knowledge_root_not_an_unsafe_default(self) -> None:
        project = self.workspace / "wiki"
        project.mkdir(parents=True)
        (project / "README.md").write_text(
            "# Workspace Wiki Project\n",
            encoding="utf-8",
            newline="\n",
        )
        custom_root = self.root / "external-knowledge"
        first = register_project(
            self.workspace,
            project,
            knowledge_root=custom_root,
        )

        second = register_project(self.workspace, project)

        self.assertFalse(second.created)
        self.assertEqual(second.project_id, first.project_id)
        self.assertEqual(second.layout.knowledge_projects_root, custom_root.resolve())
        self.assertFalse((project / "projects").exists())

    def test_custom_knowledge_root_is_external_and_does_not_create_default_root(self) -> None:
        project = self.make_project()
        knowledge_projects_root = self.root / "personal-knowledge" / "projects"

        result = register_project(
            self.workspace,
            project,
            knowledge_root=knowledge_projects_root,
        )

        self.assertEqual(
            result.layout.knowledge_root,
            knowledge_projects_root.resolve() / result.project_id,
        )
        self.assertTrue(result.layout.knowledge_root.is_dir())
        self.assertFalse((self.workspace / "wiki" / "projects").exists())
        self.assertEqual(
            result.record["storage"]["knowledge_projects_root"],
            str(knowledge_projects_root.resolve()),
        )
        for directory in result.layout.knowledge_directories:
            self.assertTrue(directory.is_dir(), directory)
        self.assertEqual(
            [
                path
                for path in result.layout.knowledge_root.rglob("*")
                if path.is_file()
            ],
            [],
        )
        self.assertEqual(self.source_snapshot(project)[0], ("src",))

    def test_same_directory_name_at_different_paths_gets_different_ids(self) -> None:
        first_project = self.make_project("group-a/shared-name")
        second_project = self.make_project("group-b/shared-name")

        first = register_project(self.workspace, first_project)
        second = register_project(self.workspace, second_project)

        self.assertNotEqual(first.project_id, second.project_id)
        self.assertTrue(first.project_id.startswith("shared-name-"))
        self.assertTrue(second.project_id.startswith("shared-name-"))

    def test_manual_id_is_validated_and_cannot_be_reused(self) -> None:
        first_project = self.make_project("first")
        second_project = self.make_project("second")
        first = register_project(
            self.workspace,
            first_project,
            project_id="manual-study",
        )
        self.assertEqual(first.project_id, "manual-study")
        self.assertEqual(
            first.record["identity_strategy"],
            MANUAL_PROJECT_ID_STRATEGY,
        )

        with self.assertRaises(ProjectConflictError):
            register_project(
                self.workspace,
                second_project,
                project_id="manual-study",
            )
        with self.assertRaises(InvalidProjectIdError):
            register_project(
                self.workspace,
                second_project,
                project_id="Unsafe ID",
            )
        with self.assertRaises(ProjectConflictError):
            register_project(
                self.workspace,
                first_project,
                project_id="different-id",
            )

    def test_empty_a03_layout_can_be_claimed_but_existing_data_cannot(self) -> None:
        project = self.make_project()
        empty_layout = ProjectLayout(self.workspace, "prepared-study")
        empty_layout.ensure_directories()

        registered = register_project(
            self.workspace,
            project,
            project_id="prepared-study",
        )
        self.assertTrue(registered.created)

        second_project = self.make_project("second-prepared")
        occupied_layout = ProjectLayout(self.workspace, "occupied-study")
        occupied_layout.ensure_directories()
        (occupied_layout.extracted_dir / "evidence.txt").write_text(
            "existing data\n",
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaises(ProjectConflictError):
            register_project(
                self.workspace,
                second_project,
                project_id="occupied-study",
            )

    def test_legacy_empty_knowledge_skeleton_is_claimed_and_completed(self) -> None:
        project = self.make_project()
        layout = ProjectLayout(self.workspace, "legacy-empty-study")
        for relative in ("sources", "papers", "experiments", "claims", "plans"):
            (layout.knowledge_root / relative).mkdir(parents=True, exist_ok=True)

        registered = register_project(
            self.workspace,
            project,
            project_id="legacy-empty-study",
        )

        self.assertTrue(registered.created)
        for directory in registered.layout.knowledge_directories:
            self.assertTrue(directory.is_dir(), directory)
        self.assertEqual(
            [
                path
                for path in registered.layout.knowledge_root.rglob("*")
                if path.is_file()
            ],
            [],
        )

    def test_unclaimed_knowledge_storage_with_unexpected_content_fails_closed(self) -> None:
        cases = (
            ("unknown-file", "README.md", False),
            ("unknown-directory", "notes", True),
            ("canonical-directory-content", "plans/draft.md", False),
        )
        for project_id, relative, is_directory in cases:
            with self.subTest(project_id=project_id, relative=relative):
                project = self.make_project(f"source-{project_id}")
                layout = ProjectLayout(self.workspace, project_id)
                target = layout.knowledge_root / relative
                if is_directory:
                    target.mkdir(parents=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(
                        "existing user content\n",
                        encoding="utf-8",
                        newline="\n",
                    )

                before = self.source_snapshot(project)
                with self.assertRaises(ProjectConflictError):
                    register_project(
                        self.workspace,
                        project,
                        project_id=project_id,
                    )
                self.assertEqual(before, self.source_snapshot(project))
                self.assertTrue(target.exists())

    def test_unclaimed_knowledge_storage_rejects_symbolic_link_entries(self) -> None:
        project = self.make_project()
        layout = ProjectLayout(self.workspace, "linked-knowledge-study")
        layout.knowledge_root.mkdir(parents=True)
        external = self.root / "external-plans"
        external.mkdir()
        try:
            os.symlink(
                external,
                layout.knowledge_root / "plans",
                target_is_directory=True,
            )
        except OSError as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")

        before = self.source_snapshot(project)
        with self.assertRaises(ProjectConflictError):
            register_project(
                self.workspace,
                project,
                project_id="linked-knowledge-study",
            )
        self.assertEqual(before, self.source_snapshot(project))
        self.assertEqual(list(external.iterdir()), [])

    def test_symlinked_output_root_cannot_redirect_writes_into_source(self) -> None:
        project = self.make_project()
        before = self.source_snapshot(project)
        machine_parent = self.workspace / ".llmwiki"
        machine_parent.mkdir(parents=True)
        try:
            os.symlink(
                project,
                machine_parent / "projects",
                target_is_directory=True,
            )
        except OSError as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")

        with self.assertRaises(ProjectPathError):
            register_project(self.workspace, project)
        self.assertEqual(before, self.source_snapshot(project))

    def test_missing_or_non_directory_project_paths_are_rejected(self) -> None:
        with self.assertRaises(ProjectPathError):
            register_project(self.workspace, self.root / "missing")

        source_file = self.root / "paper.pdf"
        source_file.write_bytes(b"not really a PDF")
        before = source_file.read_bytes()
        with self.assertRaises(ProjectPathError):
            register_project(self.workspace, source_file)
        self.assertEqual(source_file.read_bytes(), before)

    def test_source_and_output_roots_must_not_overlap(self) -> None:
        project = self.make_project()
        before = self.source_snapshot(project)
        cases = (
            {
                "workspace_root": project / "assistant",
                "knowledge_root": self.root / "knowledge-a",
            },
            {
                "workspace_root": self.workspace,
                "knowledge_root": project / "notes",
            },
            {
                "workspace_root": self.workspace,
                "knowledge_root": self.projects,
            },
            {
                "workspace_root": self.workspace,
                "knowledge_root": self.workspace / ".llmwiki",
            },
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ProjectPathError):
                    register_project(
                        case["workspace_root"],
                        project,
                        knowledge_root=case["knowledge_root"],
                    )
        self.assertEqual(before, self.source_snapshot(project))

    def test_reregistration_does_not_silently_update_onboarding(self) -> None:
        project = self.make_project()
        first = register_project(
            self.workspace,
            project,
            final_goal="Original goal",
            daily_available_hours=2,
        )
        original_bytes = first.project_file.read_bytes()

        same = register_project(
            self.workspace,
            project,
            final_goal="Original goal",
            daily_available_hours=2.0,
        )
        self.assertFalse(same.created)

        with self.assertRaises(ProjectConflictError):
            register_project(
                self.workspace,
                project,
                final_goal="Replacement goal",
            )
        self.assertEqual(first.project_file.read_bytes(), original_bytes)

    def test_reregistration_rejects_a_different_knowledge_root(self) -> None:
        project = self.make_project()
        first_root = self.root / "knowledge-a"
        second_root = self.root / "knowledge-b"
        register_project(self.workspace, project, knowledge_root=first_root)

        with self.assertRaises(ProjectConflictError):
            register_project(self.workspace, project, knowledge_root=second_root)

    def test_onboarding_values_are_validated_before_storage_is_created(self) -> None:
        project = self.make_project()
        invalid_options = (
            {"deadline": "31/12/2026"},
            {"daily_available_hours": 0},
            {"daily_available_hours": 25},
            {"final_goal": "   "},
        )
        for index, options in enumerate(invalid_options):
            isolated_workspace = self.root / f"workspace-{index}"
            with self.subTest(options=options):
                with self.assertRaises(ProjectRegistrationError):
                    register_project(isolated_workspace, project, **options)
                self.assertFalse(isolated_workspace.exists())

    def test_git_metadata_is_local_and_remote_credentials_are_removed(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("Git is not installed")
        project = self.make_project()

        def run_git(*args: str) -> str:
            completed = subprocess.run(
                ["git", "-C", str(project), *args],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            return completed.stdout.strip()

        run_git("init")
        run_git("config", "user.name", "Registration Test")
        run_git("config", "user.email", "registration@example.test")
        run_git("add", "README.md", "src/model.py")
        run_git("commit", "-m", "initial")
        run_git(
            "remote",
            "add",
            "origin",
            "https://researcher:secret-token@github.com/example/study.git?token=leak",
        )

        result = register_project(self.workspace, project)
        git = result.record["git"]

        self.assertTrue(git["available"])
        self.assertTrue(git["is_repository"])
        self.assertEqual(git["root"], str(project.resolve()))
        self.assertEqual(git["branch"], run_git("branch", "--show-current"))
        self.assertEqual(git["head_commit"], run_git("rev-parse", "HEAD"))
        self.assertEqual(
            git["origin_url"],
            "https://github.com/example/study.git",
        )
        self.assertNotIn("secret-token", result.project_file.read_text(encoding="utf-8"))

    def test_remote_url_sanitizer_preserves_normal_ssh_form(self) -> None:
        self.assertEqual(
            sanitize_git_remote_url(
                "https://user:token@example.com:8443/org/repo.git?access=secret#x"
            ),
            "https://example.com:8443/org/repo.git",
        )
        self.assertEqual(
            sanitize_git_remote_url("git@github.com:org/repo.git"),
            "git@github.com:org/repo.git",
        )
        self.assertIsNone(sanitize_git_remote_url("https://user:token@"))

    def test_cli_json_output_is_parseable_and_reports_created_state(self) -> None:
        project = self.make_project()
        workspace = self.root / "cli-workspace"
        knowledge = self.root / "cli-knowledge"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "register",
                str(project),
                "--workspace-root",
                str(workspace),
                "--knowledge-root",
                str(knowledge),
                "--goal",
                "Understand the project",
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
        self.assertTrue(payload["created"])
        self.assertEqual(payload["project_root"], str(project.resolve()))
        self.assertEqual(payload["record"]["onboarding"]["final_goal"], "Understand the project")
        self.assertEqual(
            payload["knowledge_root"],
            str(knowledge.resolve() / payload["project_id"]),
        )
        self.assertEqual(completed.stderr, "")

    def test_corrupt_or_future_records_fail_closed(self) -> None:
        project = self.make_project()
        cases = (
            "{not-json\n",
            json.dumps(
                {
                    "schema_version": CURRENT_SCHEMA_VERSION + 1,
                    "kind": PROJECT_SCHEMA_KIND,
                }
            ),
        )
        for index, content in enumerate(cases):
            workspace = self.root / f"unsafe-workspace-{index}"
            project_file = (
                workspace
                / ".llmwiki"
                / "projects"
                / "untrusted-record"
                / "project.yaml"
            )
            project_file.parent.mkdir(parents=True)
            project_file.write_text(content, encoding="utf-8", newline="\n")
            with self.subTest(index=index):
                with self.assertRaises(ProjectRecordError):
                    register_project(workspace, project)


if __name__ == "__main__":
    unittest.main()
