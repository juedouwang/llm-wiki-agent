from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.knowledge_artifacts import parse_knowledge_page
from tools.project_registry import register_project
from tools.research_tasks import (
    TASK_KIND,
    TASK_MACHINE_FILENAME,
    TASK_SCHEMA_VERSION,
    TASK_VERSION,
    ResearchTask,
    TaskCollection,
    TaskNotFoundError,
    TaskSchemaError,
    TaskStorageError,
    TaskStore,
    UnsupportedTaskSchemaVersionError,
    load_tasks,
    parse_legacy_tasks,
    parse_task,
    parse_task_collection,
    serialize_task,
    serialize_task_collection,
    task_collection_from_markdown,
    task_collection_to_markdown,
    write_tasks,
)


PROJECT_ID = "task-study"
CREATED_AT = "2026-07-18T08:00:00Z"
UPDATED_AT = "2026-07-18T09:00:00Z"
EVIDENCE_ID = "evd-" + "a" * 64


def task(
    task_id: str = "reproduce-baseline",
    *,
    project_id: str = PROJECT_ID,
    status: str = "ready",
    dependencies: tuple[str, ...] = (),
    completion_refs: tuple[str, ...] = (),
) -> ResearchTask:
    return ResearchTask(
        project_id=project_id,
        task_id=task_id,
        title="Reproduce the baseline",
        why_now="The baseline is required before evaluating changes.",
        inputs=("README.md", "configs/baseline.yaml"),
        evidence=(EVIDENCE_ID,),
        allowed_paths=("runs/**", "wiki/projects/task-study/results/**"),
        denied_paths=(".env", "data/private/**"),
        dependencies=dependencies,
        dod=("A bounded baseline run completes.", "The metric is recorded."),
        verification=("Run the focused test suite.",),
        artifacts=("run:baseline", "results/baseline.md"),
        timebox_minutes=90,
        status=status,
        completion_refs=completion_refs,
        draft_reasons=("awaiting-user-confirmation",) if status == "draft" else (),
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )


def collection(*tasks: ResearchTask) -> TaskCollection:
    return TaskCollection(
        project_id=PROJECT_ID,
        tasks=tasks or (task(),),
        updated_at=UPDATED_AT,
    )


class TaskProtocolTests(unittest.TestCase):
    def test_current_collection_round_trip_is_deterministic(self) -> None:
        value = collection()
        payload = serialize_task_collection(value)

        self.assertEqual(parse_task_collection(payload, project_id=PROJECT_ID), value)
        self.assertEqual(serialize_task_collection(parse_task_collection(payload)), payload)
        document = json.loads(payload)
        self.assertEqual(document["schema_version"], TASK_SCHEMA_VERSION)
        self.assertEqual(document["kind"], TASK_KIND)
        self.assertEqual(document["task_version"], TASK_VERSION)

    def test_individual_task_round_trip_has_project_envelope(self) -> None:
        value = task()
        payload = serialize_task(value)

        self.assertEqual(parse_task(payload, project_id=PROJECT_ID), value)
        self.assertEqual(json.loads(payload)["project_id"], PROJECT_ID)

    def test_title_only_todo_and_missing_dod_are_not_current_tasks(self) -> None:
        with self.assertRaisesRegex(TaskSchemaError, "why_now"):
            ResearchTask(
                project_id=PROJECT_ID,
                task_id="title-only",
                title="Just do it",
                why_now=None,
                inputs=(),
                evidence=(),
                allowed_paths=(),
                denied_paths=(),
                dependencies=(),
                dod=(),
                verification=(),
                artifacts=(),
                status="draft",
                draft_reasons=("awaiting-user-confirmation",),
                created_at=CREATED_AT,
                updated_at=UPDATED_AT,
            )
        raw = collection().as_dict()
        raw["tasks"][0]["dod"] = []  # type: ignore[index]
        with self.assertRaisesRegex(TaskSchemaError, "dod"):
            parse_task_collection(raw)

    def test_path_scope_rejects_absolute_windows_parent_and_backslash(self) -> None:
        for unsafe in ("/tmp/out", "C:/out", "../out", "a/../out", "a\\out", "."):
            with self.subTest(path=unsafe):
                values = task().as_dict()
                values["allowed_paths"] = [unsafe]
                with self.assertRaisesRegex(TaskSchemaError, "project-relative|cannot"):
                    ResearchTask(project_id=PROJECT_ID, **values)  # type: ignore[arg-type]

    def test_evidence_must_use_canonical_evidence_ids(self) -> None:
        values = task().as_dict()
        values["evidence"] = ["README.md"]
        with self.assertRaisesRegex(TaskSchemaError, "64 lowercase"):
            ResearchTask(project_id=PROJECT_ID, **values)  # type: ignore[arg-type]

    def test_completed_task_requires_controlled_completion_refs(self) -> None:
        with self.assertRaisesRegex(TaskSchemaError, "completion_refs"):
            task(status="completed")
        with self.assertRaisesRegex(TaskSchemaError, "controlled evidence namespace"):
            task(status="completed", completion_refs=("looks-good",))
        completed = task(
            status="completed",
            completion_refs=("test:pytest-focused", "artifact:results/baseline.md"),
        )
        self.assertEqual(completed.status, "completed")

    def test_dependencies_must_exist_be_unique_and_acyclic(self) -> None:
        with self.assertRaisesRegex(TaskSchemaError, "unknown dependencies"):
            collection(task(dependencies=("missing-task",)))
        with self.assertRaisesRegex(TaskSchemaError, "duplicate-free"):
            task(dependencies=("data-ready", "data-ready"))
        first = task("first", dependencies=("second",))
        second = task("second", dependencies=("first",))
        with self.assertRaisesRegex(TaskSchemaError, "cycle"):
            collection(first, second)

    def test_duplicate_ids_and_self_dependency_are_rejected(self) -> None:
        with self.assertRaisesRegex(TaskSchemaError, "task_id values"):
            collection(task(), task())
        with self.assertRaisesRegex(TaskSchemaError, "cannot depend on itself"):
            task(dependencies=("reproduce-baseline",))

    def test_unknown_duplicate_and_future_json_fail_closed(self) -> None:
        raw = collection().as_dict()
        raw["unknown"] = True
        with self.assertRaisesRegex(TaskSchemaError, "fields must be exact"):
            parse_task_collection(raw)
        duplicate = serialize_task_collection(collection()).replace(
            '"schema_version": 1,',
            '"schema_version": 1,\n  "schema_version": 1,',
        )
        with self.assertRaises(TaskSchemaError):
            parse_task_collection(duplicate)
        for version in (0, 2):
            raw = collection().as_dict()
            raw["schema_version"] = version
            with self.assertRaises(UnsupportedTaskSchemaVersionError):
                parse_task_collection(raw)

    def test_project_mismatch_and_invalid_requested_id_fail_closed(self) -> None:
        with self.assertRaisesRegex(TaskSchemaError, "does not match"):
            parse_task_collection(
                serialize_task_collection(collection()), project_id="another-study"
            )
        with self.assertRaisesRegex(TaskSchemaError, "requested project_id is invalid"):
            parse_task_collection(collection().as_dict(), project_id="../unsafe")

    def test_legacy_aliases_are_draft_read_only_views(self) -> None:
        legacy = parse_legacy_tasks(
            {
                "todos": [
                    {
                        "id": "old-task",
                        "title": "Old completed todo",
                        "status": "done",
                        "definition_of_done": ["Old acceptance text"],
                        "expected_artifacts": ["results/old.md"],
                        "forbidden_paths": [".env"],
                        "timebox": 30,
                    }
                ]
            },
            project_id=PROJECT_ID,
        )
        old = legacy.tasks[0]
        self.assertTrue(legacy.is_legacy)
        self.assertTrue(old.is_legacy)
        self.assertFalse(old.executable)
        self.assertEqual(old.status, "draft")
        self.assertEqual(old.draft_reasons, ("legacy-completion-unverified",))
        self.assertEqual(old.dod, ("Old acceptance text",))
        self.assertEqual(old.artifacts, ("results/old.md",))
        self.assertEqual(old.denied_paths, (".env",))
        with self.assertRaisesRegex(TaskSchemaError, "legacy"):
            serialize_task_collection(legacy)

    def test_title_only_legacy_todo_is_not_promoted(self) -> None:
        legacy = parse_legacy_tasks(
            [{"title": "Unstructured note"}], project_id=PROJECT_ID
        )
        old = legacy.tasks[0]
        self.assertEqual(old.dod, ())
        self.assertEqual(old.allowed_paths, ())
        self.assertEqual(old.status, "draft")
        self.assertFalse(old.executable)

    def test_markdown_round_trip_uses_schema_v2_and_preserves_user_region(self) -> None:
        value = collection()
        rendered = task_collection_to_markdown(
            value,
            rendered_at=UPDATED_AT,
            user_region="User-owned prioritization stays exact.\n",
        )

        page = parse_knowledge_page(rendered.encode("utf-8"), path="plans/backlog.md")
        self.assertEqual(page.frontmatter.artifact_type, "plan")
        self.assertEqual(page.frontmatter.ownership, "mixed")
        self.assertEqual(page.frontmatter.status, "verified")
        self.assertIn("User-owned prioritization stays exact.\n", page.body)
        self.assertEqual(
            task_collection_from_markdown(rendered, project_id=PROJECT_ID), value
        )

    def test_draft_markdown_is_visibly_draft_and_reserved_markers_fail(self) -> None:
        draft = collection(task(status="draft"))
        rendered = task_collection_to_markdown(draft, rendered_at=UPDATED_AT)
        page = parse_knowledge_page(rendered.encode("utf-8"), path="plans/backlog.md")
        self.assertEqual(page.frontmatter.status, "draft")
        self.assertIsNone(page.frontmatter.last_verified_at)
        with self.assertRaisesRegex(TaskSchemaError, "reserved"):
            task_collection_to_markdown(
                draft,
                user_region="<!-- llmwiki:task-protocol-v1:start -->",
            )

    def test_markdown_rejects_duplicate_block_and_project_mismatch(self) -> None:
        rendered = task_collection_to_markdown(collection(), rendered_at=UPDATED_AT)
        with self.assertRaisesRegex(TaskSchemaError, "does not match"):
            task_collection_from_markdown(rendered, project_id="another-study")
        with self.assertRaisesRegex(TaskSchemaError, "exactly once"):
            task_collection_from_markdown(
                rendered.replace(
                    "<!-- llmwiki:task-protocol-v1:end -->",
                    "<!-- llmwiki:task-protocol-v1:end -->\n"
                    "<!-- llmwiki:task-protocol-v1:end -->",
                )
            )


class TaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.workspace = root / "workspace"
        self.source = root / "source"
        self.source.mkdir()
        (self.source / "README.md").write_text(
            "# Source\n", encoding="utf-8", newline="\n"
        )
        self.registration = register_project(
            self.workspace,
            self.source,
            project_id=PROJECT_ID,
        )

    def test_store_writes_only_registered_machine_artifact(self) -> None:
        value = collection()
        before = (self.source / "README.md").read_bytes()

        path = write_tasks(self.workspace, PROJECT_ID, value)

        self.assertEqual(
            path,
            self.registration.layout.indexes_dir / TASK_MACHINE_FILENAME,
        )
        self.assertEqual(load_tasks(self.workspace, PROJECT_ID), value)
        self.assertEqual((self.source / "README.md").read_bytes(), before)
        self.assertFalse(
            (self.registration.layout.knowledge_root / "plans" / "backlog.md").exists()
        )

    def test_store_loads_markdown_without_writing_it(self) -> None:
        value = collection()
        page = self.registration.layout.knowledge_root / "plans" / "backlog.md"
        page.write_text(
            task_collection_to_markdown(value, rendered_at=UPDATED_AT),
            encoding="utf-8",
            newline="\n",
        )
        self.assertEqual(TaskStore(self.workspace, PROJECT_ID).load_markdown(), value)

    def test_missing_wrong_project_and_legacy_write_fail_closed(self) -> None:
        store = TaskStore(self.workspace, PROJECT_ID)
        with self.assertRaises(TaskNotFoundError):
            store.load()
        with self.assertRaises(TaskNotFoundError):
            store.load_markdown()
        with self.assertRaises(TaskStorageError):
            store.write(
                TaskCollection(
                    project_id="another-study",
                    tasks=(task(project_id="another-study"),),
                    updated_at=UPDATED_AT,
                )
            )
        legacy = parse_legacy_tasks([{"title": "Old"}], project_id=PROJECT_ID)
        with self.assertRaises(TaskStorageError):
            store.write(legacy)

    def test_unregistered_project_is_rejected(self) -> None:
        with self.assertRaises(TaskStorageError):
            TaskStore(self.workspace, "missing-study")


if __name__ == "__main__":
    unittest.main()
