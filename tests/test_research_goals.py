from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.knowledge_artifacts import parse_knowledge_page
from tools.project_registry import register_project
from tools.research_goals import (
    GOAL_KIND,
    GOAL_MACHINE_FILENAME,
    GOAL_SCHEMA_VERSION,
    GOAL_VERSION,
    Goal,
    GoalNotFoundError,
    GoalSchemaError,
    GoalStorageError,
    GoalStore,
    Milestone,
    UnsupportedGoalSchemaVersionError,
    goal_from_markdown,
    goal_to_markdown,
    load_goal,
    parse_goal,
    serialize_goal,
    write_goal,
)

PROJECT_ID = "goal-study"
CREATED_AT = "2026-07-18T08:00:00Z"
UPDATED_AT = "2026-07-18T09:00:00Z"


def active_goal(*, project_id: str = PROJECT_ID) -> Goal:
    return Goal(
        project_id=project_id,
        goal="Reproduce the reference result",
        success_criteria=("Metric matches within tolerance",),
        deadline="2026-12-31",
        current_stage="baseline-reproduction",
        dependencies=("dataset-ready",),
        milestones=(
            Milestone(
                milestone_id="baseline",
                title="Baseline reproduced",
                success_criteria=("Run completes",),
                deadline="2026-09-30",
                current_stage="execution",
                status="active",
            ),
        ),
        status="active",
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )


class GoalSchemaTests(unittest.TestCase):
    def test_active_goal_json_round_trip_is_deterministic(self) -> None:
        goal = active_goal()

        payload = serialize_goal(goal)
        parsed = parse_goal(payload, project_id=PROJECT_ID)

        self.assertEqual(parsed, goal)
        self.assertEqual(serialize_goal(parsed), payload)
        document = json.loads(payload)
        self.assertEqual(document["schema_version"], GOAL_SCHEMA_VERSION)
        self.assertEqual(document["kind"], GOAL_KIND)
        self.assertEqual(document["goal_version"], GOAL_VERSION)

    def test_draft_factory_records_missing_user_fields(self) -> None:
        goal = Goal.draft(
            PROJECT_ID,
            goal="Explore the project",
            created_at=CREATED_AT,
        )

        self.assertTrue(goal.is_draft)
        self.assertEqual(
            goal.draft_reasons,
            (
                "missing-success_criteria",
                "missing-deadline",
                "missing-current_stage",
            ),
        )
        self.assertEqual(goal.created_at, CREATED_AT)
        self.assertEqual(goal.updated_at, CREATED_AT)

    def test_complete_draft_stays_unconfirmed(self) -> None:
        goal = Goal.draft(
            PROJECT_ID,
            goal="Reproduce the result",
            success_criteria=("Result reproduced",),
            deadline="2026-12-31",
            current_stage="setup",
            created_at=CREATED_AT,
        )

        self.assertEqual(goal.status, "draft")
        self.assertEqual(goal.draft_reasons, ("awaiting-user-confirmation",))

    def test_non_draft_requires_goal_criteria_deadline_and_stage(self) -> None:
        with self.assertRaisesRegex(GoalSchemaError, "non-draft goal requires"):
            Goal(
                project_id=PROJECT_ID,
                goal="Reproduce",
                status="active",
                created_at=CREATED_AT,
                updated_at=CREATED_AT,
            )

    def test_non_draft_milestone_requires_acceptance_fields(self) -> None:
        with self.assertRaisesRegex(GoalSchemaError, "non-draft milestone requires"):
            Milestone(
                milestone_id="baseline",
                title="Baseline",
                status="active",
            )

    def test_duplicate_and_self_dependencies_are_rejected(self) -> None:
        with self.assertRaisesRegex(GoalSchemaError, "duplicate-free"):
            Goal.draft(
                PROJECT_ID,
                dependencies=("data", "data"),
                created_at=CREATED_AT,
            )
        with self.assertRaisesRegex(GoalSchemaError, "cannot depend on itself"):
            Milestone(
                milestone_id="baseline",
                title="Baseline",
                dependencies=("baseline",),
            )

    def test_duplicate_milestone_ids_are_rejected(self) -> None:
        item = Milestone(milestone_id="baseline", title="Baseline")
        with self.assertRaisesRegex(GoalSchemaError, "milestone IDs"):
            Goal.draft(
                PROJECT_ID,
                milestones=(item, item),
                created_at=CREATED_AT,
            )

    def test_strict_json_rejects_invalid_utf8_duplicate_keys_and_nonfinite(self) -> None:
        with self.assertRaisesRegex(GoalSchemaError, "strict UTF-8"):
            parse_goal(b"\xff")
        with self.assertRaisesRegex(GoalSchemaError, "duplicate JSON key"):
            parse_goal(b'{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(GoalSchemaError, "non-finite"):
            parse_goal(b'{"schema_version":NaN}')

    def test_unknown_missing_legacy_and_future_fields_fail_closed(self) -> None:
        document = active_goal().as_dict()
        unknown = deepcopy(document)
        unknown["extra"] = True
        with self.assertRaisesRegex(GoalSchemaError, "unknown extra"):
            parse_goal(unknown)

        missing = deepcopy(document)
        del missing["current_stage"]
        with self.assertRaisesRegex(GoalSchemaError, "missing current_stage"):
            parse_goal(missing)

        for version in (0, GOAL_SCHEMA_VERSION + 1):
            invalid = deepcopy(document)
            invalid["schema_version"] = version
            with self.subTest(version=version):
                with self.assertRaises(UnsupportedGoalSchemaVersionError):
                    parse_goal(invalid)

    def test_invalid_requested_project_id_is_wrapped(self) -> None:
        with self.assertRaisesRegex(GoalSchemaError, "requested project_id is invalid"):
            parse_goal(active_goal().as_dict(), project_id="../unsafe")

    def test_requested_project_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(GoalSchemaError, "does not match"):
            parse_goal(active_goal().as_dict(), project_id="another-study")

    def test_markdown_round_trip_uses_schema_v2_and_mixed_user_region(self) -> None:
        goal = active_goal()
        rendered = goal_to_markdown(
            goal,
            rendered_at=UPDATED_AT,
            user_region="User note remains exact.\n",
        )

        page = parse_knowledge_page(rendered.encode("utf-8"), path="goals.md")
        self.assertEqual(page.frontmatter.artifact_type, "goal")
        self.assertEqual(page.frontmatter.ownership, "mixed")
        self.assertEqual(page.frontmatter.status, "verified")
        self.assertIn("User note remains exact.\n", page.body)
        self.assertEqual(goal_from_markdown(rendered, project_id=PROJECT_ID), goal)

    def test_draft_markdown_is_visibly_draft(self) -> None:
        goal = Goal.draft(PROJECT_ID, created_at=CREATED_AT)
        rendered = goal_to_markdown(goal, rendered_at=CREATED_AT)
        page = parse_knowledge_page(rendered.encode("utf-8"), path="goals.md")

        self.assertEqual(page.frontmatter.status, "draft")
        self.assertIsNone(page.frontmatter.last_verified_at)
        self.assertIn("**DRAFT**", page.body)
        self.assertEqual(goal_from_markdown(rendered), goal)

    def test_markdown_rejects_project_mismatch_duplicate_block_and_reserved_region(self) -> None:
        rendered = goal_to_markdown(active_goal(), rendered_at=UPDATED_AT)
        with self.assertRaisesRegex(GoalSchemaError, "does not match"):
            goal_from_markdown(rendered, project_id="another-study")
        with self.assertRaisesRegex(GoalSchemaError, "exactly one"):
            goal_from_markdown(
                rendered.replace(
                    "<!-- llmwiki:goal-schema-v1:end -->",
                    "<!-- llmwiki:goal-schema-v1:end -->\n"
                    "<!-- llmwiki:goal-schema-v1:end -->",
                )
            )
        with self.assertRaisesRegex(GoalSchemaError, "reserved marker"):
            goal_to_markdown(
                active_goal(),
                user_region="<!-- llmwiki:user-region:start -->",
            )


class GoalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.workspace = root / "workspace"
        self.source = root / "source"
        self.source.mkdir()
        (self.source / "README.md").write_text(
            "# Source\n",
            encoding="utf-8",
            newline="\n",
        )
        self.registration = register_project(
            self.workspace,
            self.source,
            project_id=PROJECT_ID,
        )

    def test_store_writes_and_loads_only_registered_machine_artifact(self) -> None:
        goal = active_goal()
        before = (self.source / "README.md").read_bytes()

        path = write_goal(self.workspace, PROJECT_ID, goal)

        self.assertEqual(
            path,
            self.registration.layout.indexes_dir / GOAL_MACHINE_FILENAME,
        )
        self.assertEqual(load_goal(self.workspace, PROJECT_ID), goal)
        self.assertEqual((self.source / "README.md").read_bytes(), before)
        self.assertFalse((self.registration.layout.knowledge_root / "goals.md").exists())

    def test_store_loads_strict_markdown_without_writing_it(self) -> None:
        goal = active_goal()
        page = self.registration.layout.knowledge_root / "goals.md"
        page.write_text(
            goal_to_markdown(goal, rendered_at=UPDATED_AT),
            encoding="utf-8",
            newline="\n",
        )

        self.assertEqual(GoalStore(self.workspace, PROJECT_ID).load_markdown(), goal)

    def test_missing_artifacts_and_wrong_project_fail_closed(self) -> None:
        store = GoalStore(self.workspace, PROJECT_ID)
        with self.assertRaises(GoalNotFoundError):
            store.load()
        with self.assertRaises(GoalNotFoundError):
            store.load_markdown()
        with self.assertRaises(GoalStorageError):
            store.write(active_goal(project_id="another-study"))

    def test_unregistered_project_is_rejected(self) -> None:
        with self.assertRaises(GoalStorageError):
            GoalStore(self.workspace, "missing-study")


if __name__ == "__main__":
    unittest.main()
