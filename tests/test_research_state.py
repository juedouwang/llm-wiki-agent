from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from tools.evidence_registry import register_evidence
from tools.extraction_schema import LineRangeLocator
from tools.knowledge_artifacts import (
    KNOWLEDGE_PAGE_KIND,
    KNOWLEDGE_SCHEMA_VERSION,
    serialize_knowledge_frontmatter,
)
from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.project_runs import create_project_run
from tools.research_state import (
    PROJECT_STATE_KIND,
    PROJECT_STATE_SCHEMA_VERSION,
    PROJECT_STATE_VERSION,
    ProjectStateError,
    ProjectStateNotFoundError,
    ProjectStateStore,
    generate_project_state,
    load_project_state,
    parse_project_state,
    serialize_project_state,
)
from tools.source_registry import load_source_registry, sync_source_registry


class ResearchStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.source = root / "research-project"
        self.source.mkdir()
        self.files = {
            "README.md": "# Research project\n",
            "train.py": "secret-source-content = True\n",
            "results/metrics.csv": "metric,value\naccuracy,0.9\n",
        }
        for relative, text in self.files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        self.registration = register_project(self.workspace, self.source)
        self.project_id = self.registration.project_id
        inventory_project(self.workspace, self.project_id)
        self.source_snapshot = {
            path.relative_to(self.source).as_posix(): path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }

    def assert_source_unchanged(self) -> None:
        actual = {
            path.relative_to(self.source).as_posix(): path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual, self.source_snapshot)

    def page(self, relative: str, artifact_type: str, title: str, status: str = "draft") -> None:
        path = self.registration.layout.knowledge_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        frontmatter = {
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "kind": KNOWLEDGE_PAGE_KIND,
            "project_id": self.project_id,
            "artifact_type": artifact_type,
            "title": title,
            "status": status,
            "ownership": "generated",
            "source_ids": [],
            "evidence_refs": [],
            "generated_at": "2026-07-18T00:00:00Z",
            "updated_at": "2026-07-19T00:00:00Z",
            "last_verified_at": None,
        }
        path.write_text(
            serialize_knowledge_frontmatter(frontmatter, path=relative)
            + "# Curated body\n",
            encoding="utf-8",
            newline="\n",
        )

    def test_round_trip_and_fixed_timestamp_are_canonical(self) -> None:
        first = generate_project_state(
            self.workspace,
            self.project_id,
            generated_at="2026-07-19T00:00:00Z",
        )
        raw = first.project_state_file.read_bytes()
        parsed = parse_project_state(raw, project_id=self.project_id)
        self.assertEqual(parsed, first.state)
        self.assertEqual(raw.decode("utf-8"), serialize_project_state(parsed))
        second = generate_project_state(
            self.workspace,
            self.project_id,
            generated_at="2026-07-19T00:00:00Z",
        )
        self.assertEqual(raw, second.project_state_file.read_bytes())
        self.assertEqual(first.state, second.state)
        self.assert_source_unchanged()

    def test_manifest_binding_is_exact_and_stale_loader_fails_closed(self) -> None:
        result = generate_project_state(
            self.workspace, self.project_id, generated_at="2026-07-19T00:00:00Z"
        )
        store = ProjectStateStore(self.workspace, self.project_id)
        self.assertEqual(store.load(), result.state)
        self.assertEqual(result.state.manifest["sha256"], hashlib.sha256(
            self.registration.layout.manifest_file.read_bytes()
        ).hexdigest())
        (self.source / "new.md").write_text("new source\n", encoding="utf-8")
        inventory_project(self.workspace, self.project_id)
        with self.assertRaises(ProjectStateError):
            load_project_state(self.workspace, self.project_id)
        with self.assertRaises(ProjectStateError):
            store.load()
        with self.assertRaises(ProjectStateError):
            store.write(result.state)
        refreshed = generate_project_state(
            self.workspace, self.project_id, generated_at="2026-07-19T00:00:00Z"
        )
        self.assertNotEqual(result.state.manifest, refreshed.state.manifest)
        # The new source file is an intentional fixture mutation; verify that
        # the state workflow did not rewrite any pre-existing source bytes.
        for relative, original in self.files.items():
            self.assertEqual((self.source / relative).read_bytes(), original.encode("utf-8"))

    def test_strict_parser_rejects_unknown_future_duplicate_and_noncanonical(self) -> None:
        state = generate_project_state(
            self.workspace, self.project_id, generated_at="2026-07-19T00:00:00Z"
        ).state.as_dict()
        unknown = dict(state)
        unknown["unknown"] = True
        with self.assertRaises(ProjectStateError):
            parse_project_state(unknown)
        future = dict(state)
        future["schema_version"] = PROJECT_STATE_SCHEMA_VERSION + 1
        with self.assertRaises(ProjectStateError):
            parse_project_state(future)
        noncanonical = json.dumps(state, ensure_ascii=False, indent=4).encode("utf-8")
        with self.assertRaises(ProjectStateError):
            parse_project_state(noncanonical)
        duplicate = (
            b'{"schema_version":1,"schema_version":1}'
        )
        with self.assertRaises(ProjectStateError):
            parse_project_state(duplicate)
        self.assertEqual(state["kind"], PROJECT_STATE_KIND)
        self.assertEqual(state["state_version"], PROJECT_STATE_VERSION)

    def test_missing_optional_artifacts_are_explicit_gaps(self) -> None:
        result = generate_project_state(
            self.workspace, self.project_id, generated_at="2026-07-19T00:00:00Z"
        )
        gaps = {(item["code"], item["artifact"]) for item in result.state.gaps}
        self.assertIn(("missing-goal", "goal"), gaps)
        self.assertIn(("missing-tasks", "tasks"), gaps)
        self.assertIn(("missing-evidence", "evidence"), gaps)
        self.assertEqual(result.state.inputs["goal"]["status"], "missing")
        self.assertEqual(result.state.inputs["runs"]["status"], "missing")

    def test_knowledge_experiments_results_and_stale_pages_are_summarized(self) -> None:
        self.page("experiments/index.md", "experiment", "Experiments")
        self.page("experiments/baseline.md", "experiment", "Baseline")
        self.page("results/index.md", "result", "Results")
        self.page("results/accuracy.md", "result", "Accuracy", status="stale")
        self.page("open-questions.md", "open_question", "Open Questions")
        result = generate_project_state(
            self.workspace, self.project_id, generated_at="2026-07-19T00:00:00Z"
        )
        self.assertEqual(result.state.experiments["total"], 2)
        self.assertEqual(result.state.results["total"], 2)
        self.assertEqual(result.state.open_questions["total"], 1)
        stale_paths = {item["path"] for item in result.state.stale_knowledge["items"]}
        self.assertIn("results/accuracy.md", stale_paths)
        self.assertEqual(result.state.recent_changes["items"][0]["updated_at"], "2026-07-19T00:00:00Z")

    def test_project_run_microseconds_are_projected_to_state_seconds(self) -> None:
        run_id = "run-20260719t035048473037z-111111111111"
        create_project_run(
            self.workspace,
            self.project_id,
            run_id=run_id,
            created_at="2026-07-19T03:50:48.473037Z",
        )
        result = generate_project_state(
            self.workspace,
            self.project_id,
            generated_at="2026-07-19T04:00:00Z",
        )
        run_item = next(
            item
            for item in result.state.recent_changes["items"]
            if item["id"] == "run:" + run_id
        )
        self.assertEqual(run_item["updated_at"], "2026-07-19T03:50:48Z")
        self.assertEqual(result.state.inputs["runs"]["status"], "available")
        self.assert_source_unchanged()

    def test_source_and_evidence_currentness_is_reported_without_source_reads(self) -> None:
        sync_source_registry(self.workspace, self.project_id)
        source = load_source_registry(self.workspace, self.project_id).current_by_path["train.py"]
        register_evidence(
            self.workspace,
            self.project_id,
            source_id=source.source_id,
            content_hash=source.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt="secret-source-content = True\n",
        )
        result = generate_project_state(
            self.workspace, self.project_id, generated_at="2026-07-19T00:00:00Z"
        )
        self.assertEqual(result.state.inputs["sources"]["status"], "available")
        self.assertEqual(result.state.inputs["evidence"]["item_count"], 1)
        self.assertEqual(result.state.stale_evidence["total"], 0)
        encoded = json.dumps(result.state.as_dict(), ensure_ascii=False)
        self.assertNotIn(str(self.source.resolve()), encoded)
        self.assertNotIn("secret-source-content", encoded)
        self.assert_source_unchanged()

    def test_lock_and_atomic_publication_leave_no_temp_files(self) -> None:
        result = generate_project_state(
            self.workspace, self.project_id, generated_at="2026-07-19T00:00:00Z"
        )
        self.assertTrue(self.registration.layout.machine_state_lock_file.exists())
        leftovers = list(self.registration.layout.indexes_dir.glob(f".{result.project_state_file.name}.*.tmp"))
        self.assertEqual(leftovers, [])
        self.assertTrue(result.project_state_file.is_file())

    def test_no_source_project_reads_are_performed_by_generation(self) -> None:
        generate_project_state(
            self.workspace, self.project_id, generated_at="2026-07-19T00:00:00Z"
        )
        self.assert_source_unchanged()

    def test_missing_state_is_distinct(self) -> None:
        with self.assertRaises(ProjectStateNotFoundError):
            load_project_state(self.workspace, self.project_id)


if __name__ == "__main__":
    unittest.main()
