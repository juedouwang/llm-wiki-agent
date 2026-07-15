from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.project_layout import (
    CURRENT_SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
    WORKSPACE_SCHEMA_KIND,
    InvalidProjectIdError,
    LayoutError,
    ProjectLayout,
    SchemaVersionError,
    UnsupportedSchemaVersionError,
    WorkspaceLayout,
    load_versioned_json,
    resolve_project_layout,
    schema_version_of,
    validate_project_id,
    write_versioned_json,
)


REPO_ROOT = Path(__file__).parent.parent


class ProjectLayoutBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.workspace_root = Path(self.temp_dir.name) / "assistant-workspace"
        self.workspace_root.mkdir()

    def test_project_paths_separate_machine_state_from_human_knowledge(self) -> None:
        layout = ProjectLayout(self.workspace_root, "tiny-study")

        self.assertEqual(
            layout.machine_root,
            self.workspace_root.resolve() / ".llmwiki" / "projects" / "tiny-study",
        )
        self.assertEqual(
            layout.knowledge_root,
            self.workspace_root.resolve() / "wiki" / "projects" / "tiny-study",
        )
        self.assertEqual(layout.project_file.name, "project.yaml")
        self.assertEqual(layout.manifest_file.name, "manifest.jsonl")
        self.assertEqual(layout.sources_file.name, "sources.jsonl")
        self.assertEqual(
            {path.name for path in layout.machine_directories},
            {"extracted", "indexes", "runs"},
        )
        self.assertEqual(
            {path.name for path in layout.knowledge_directories},
            {"sources", "papers", "experiments", "claims", "plans"},
        )
        self.assertNotEqual(layout.machine_root, layout.knowledge_root)
        self.assertNotIn(layout.machine_root, layout.knowledge_root.parents)
        self.assertNotIn(layout.knowledge_root, layout.machine_root.parents)

    def test_custom_knowledge_projects_root_preserves_machine_workspace(self) -> None:
        custom_root = self.workspace_root.parent / "personal-knowledge" / "projects"
        layout = ProjectLayout(
            self.workspace_root,
            "tiny-study",
            custom_knowledge_projects_root=custom_root,
        )

        layout.ensure_directories()

        self.assertEqual(layout.knowledge_projects_root, custom_root.resolve())
        self.assertEqual(
            layout.knowledge_root,
            custom_root.resolve() / "tiny-study",
        )
        self.assertTrue(layout.machine_root.is_dir())
        self.assertTrue(layout.knowledge_root.is_dir())
        self.assertFalse(layout.workspace.knowledge_projects_root.exists())

        resolution = resolve_project_layout(
            self.workspace_root,
            "tiny-study",
            knowledge_projects_root=custom_root,
        )
        self.assertEqual(resolution.mode, "project")
        self.assertEqual(resolution.project.knowledge_root, layout.knowledge_root)

    def test_ensure_directories_creates_contract_without_fake_records_or_pages(self) -> None:
        layout = ProjectLayout(self.workspace_root, "tiny-study")
        document = layout.ensure_directories()

        self.assertEqual(document.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertFalse(document.is_legacy)
        self.assertEqual(document.data["kind"], WORKSPACE_SCHEMA_KIND)
        self.assertTrue(layout.machine_root.is_dir())
        self.assertTrue(layout.knowledge_root.is_dir())
        for directory in layout.machine_directories + layout.knowledge_directories:
            self.assertTrue(directory.is_dir(), directory)

        # A-03 defines storage only. B-01/B-03 and later tasks own these files.
        self.assertFalse(layout.project_file.exists())
        self.assertFalse(layout.manifest_file.exists())
        self.assertFalse(layout.sources_file.exists())
        self.assertFalse(layout.overview_file.exists())

        schema_text_before = layout.workspace.schema_file.read_text(encoding="utf-8")
        second_document = layout.ensure_directories()
        schema_text_after = layout.workspace.schema_file.read_text(encoding="utf-8")
        self.assertEqual(second_document.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(schema_text_after, schema_text_before)

    def test_project_id_validation_blocks_ambiguous_or_escaping_paths(self) -> None:
        self.assertEqual(validate_project_id("study-2026.v1"), "study-2026.v1")

        invalid_ids = (
            "",
            ".",
            "..",
            "../escape",
            "nested/project",
            r"nested\project",
            "C:drive",
            "UpperCase",
            "with space",
            "-leading-dash",
            "trailing.",
            "con",
            "nul.data",
            "x" * 65,
        )
        for project_id in invalid_ids:
            with self.subTest(project_id=project_id):
                with self.assertRaises(InvalidProjectIdError):
                    ProjectLayout(self.workspace_root, project_id)

    def test_versioned_json_reads_current_and_legacy_without_silent_migration(self) -> None:
        current_path = self.workspace_root / "current.json"
        write_versioned_json(current_path, {"kind": "test-record", "value": 7})
        current = load_versioned_json(current_path)
        self.assertEqual(current.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertFalse(current.is_legacy)
        self.assertEqual(current.data["value"], 7)

        legacy_path = self.workspace_root / "legacy.json"
        legacy_payload = {"version": 1, "value": "legacy"}
        legacy_path.write_text(
            json.dumps(legacy_payload, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        legacy_text_before = legacy_path.read_text(encoding="utf-8")
        legacy = load_versioned_json(legacy_path)
        self.assertEqual(legacy.schema_version, LEGACY_SCHEMA_VERSION)
        self.assertTrue(legacy.is_legacy)
        self.assertEqual(legacy.data, legacy_payload)
        self.assertEqual(legacy_path.read_text(encoding="utf-8"), legacy_text_before)

        with self.assertRaises(SchemaVersionError):
            load_versioned_json(legacy_path, allow_legacy=False)

    def test_schema_validation_fails_closed_for_invalid_or_future_versions(self) -> None:
        self.assertEqual(schema_version_of({"schema_version": 1}), 1)

        for invalid in (True, -1, "1", 1.0):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SchemaVersionError):
                    schema_version_of({"schema_version": invalid})

        with self.assertRaises(UnsupportedSchemaVersionError):
            schema_version_of({"schema_version": CURRENT_SCHEMA_VERSION + 1})
        for invalid in (LEGACY_SCHEMA_VERSION, True, 1.0, "1"):
            with self.subTest(writer_schema_version=invalid):
                with self.assertRaises(SchemaVersionError):
                    write_versioned_json(
                        self.workspace_root / "bad.json",
                        {"schema_version": invalid},
                    )

    def test_layout_resolution_prefers_v1_and_falls_back_to_existing_legacy(self) -> None:
        legacy_root = self.workspace_root / "tiny-study-wiki"
        legacy_manifest = legacy_root / "state" / "raw-md-manifest.json"
        legacy_manifest.parent.mkdir(parents=True)
        legacy_manifest.write_text("{}\n", encoding="utf-8", newline="\n")

        legacy_resolution = resolve_project_layout(
            self.workspace_root,
            "tiny-study",
            legacy_wiki_root=legacy_root,
        )
        self.assertEqual(legacy_resolution.mode, "legacy")
        self.assertEqual(legacy_resolution.manifest_candidates[0], legacy_manifest.resolve())
        self.assertEqual(
            legacy_resolution.manifest_candidates[1],
            legacy_resolution.project.manifest_file,
        )

        legacy_resolution.project.machine_root.mkdir(parents=True)
        current_resolution = resolve_project_layout(
            self.workspace_root,
            "tiny-study",
            legacy_wiki_root=legacy_root,
        )
        self.assertEqual(current_resolution.mode, "project")
        self.assertEqual(
            current_resolution.manifest_candidates,
            (
                current_resolution.project.manifest_file,
                legacy_manifest.resolve(),
            ),
        )

    def test_workspace_rejects_an_unrelated_schema_marker(self) -> None:
        workspace = WorkspaceLayout(self.workspace_root)
        write_versioned_json(workspace.schema_file, {"kind": "other-application"})

        with self.assertRaises(LayoutError):
            workspace.ensure()

    def test_committed_workspace_schema_matches_the_code_contract(self) -> None:
        document = load_versioned_json(REPO_ROOT / ".llmwiki" / "schema.json")
        self.assertEqual(document.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(document.data["kind"], WORKSPACE_SCHEMA_KIND)
        self.assertEqual(document.data["machine_state_root"], ".llmwiki/projects")
        self.assertEqual(document.data["knowledge_root"], "wiki/projects")


if __name__ == "__main__":
    unittest.main()
