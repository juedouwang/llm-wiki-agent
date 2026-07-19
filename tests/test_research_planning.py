from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.research_goals import Goal, GoalStore
from tools.research_planning import (
    INITIAL_PLAN_KIND,
    INITIAL_PLAN_SCHEMA_VERSION,
    INITIAL_PLAN_STATUS,
    INITIAL_PLAN_VERSION,
    InitialPlan,
    InitialPlanningError,
    UnsupportedInitialPlanSchemaVersionError,
    generate_initial_plan,
    load_initial_plan,
    parse_initial_plan,
    serialize_initial_plan,
)
from tools.research_state import generate_project_state
from tools.research_tasks import ResearchTask, TaskCollection, TaskStore


PROJECT_ID = "initial-plan-study"
TIMESTAMP = "2026-07-19T01:02:03Z"


def plan() -> InitialPlan:
    return InitialPlan(
        project_id=PROJECT_ID,
        plan_date="2026-07-19",
        generated_at=TIMESTAMP,
        status="draft",
        goal_id="primary",
        state_artifact_id="state-" + "a" * 64,
        task_ids=("review-goal",),
        why_now="Review the initial state before execution.",
        timebox_minutes=30,
        inputs=("indexes/project-state.json",),
        outputs=("plans/daily/2026-07-19.md",),
        verification=("Require explicit user confirmation.",),
        blockers=("Goal remains DRAFT.",),
    )


def draft_task(project_id: str, *, updated_at: str = TIMESTAMP) -> ResearchTask:
    return ResearchTask(
        project_id=project_id,
        task_id="review-goal",
        title="Review the goal",
        why_now="A user must confirm the initial direction.",
        inputs=("machine:indexes/goals.json",),
        evidence=(),
        allowed_paths=("README*",),
        denied_paths=(".git/**",),
        dependencies=(),
        dod=("The goal is reviewed.",),
        verification=("Capture explicit user confirmation.",),
        artifacts=("user-confirmation:research-goal",),
        timebox_minutes=30,
        status="draft",
        draft_reasons=("awaiting-user-confirmation",),
        created_at=TIMESTAMP,
        updated_at=updated_at,
    )


class InitialPlanSchemaTests(unittest.TestCase):
    def test_canonical_round_trip(self) -> None:
        value = plan()
        payload = serialize_initial_plan(value)

        self.assertEqual(parse_initial_plan(payload, project_id=PROJECT_ID), value)
        self.assertEqual(serialize_initial_plan(parse_initial_plan(payload)), payload)
        document = json.loads(payload)
        self.assertEqual(document["schema_version"], INITIAL_PLAN_SCHEMA_VERSION)
        self.assertEqual(document["kind"], INITIAL_PLAN_KIND)
        self.assertEqual(document["plan_version"], INITIAL_PLAN_VERSION)
        self.assertEqual(document["status"], INITIAL_PLAN_STATUS)

    def test_rejects_unknown_future_noncanonical_and_local_paths(self) -> None:
        document = plan().as_dict()
        with self.assertRaises(InitialPlanningError):
            parse_initial_plan({**document, "extra": True})
        with self.assertRaises(UnsupportedInitialPlanSchemaVersionError):
            parse_initial_plan({**document, "schema_version": 2})
        with self.assertRaises(InitialPlanningError):
            parse_initial_plan(json.dumps(document))
        with self.assertRaises(InitialPlanningError):
            parse_initial_plan({**document, "outputs": [r"C:\\private\\plan.md"]})


class InitialPlanningStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.workspace = root / "workspace"
        self.source = root / "source"
        self.knowledge_projects = root / "knowledge-projects"
        self.source.mkdir()
        (self.source / "README.md").write_text("# Study\n", encoding="utf-8")
        self.registration = register_project(
            self.workspace,
            self.source,
            project_id=PROJECT_ID,
            knowledge_root=self.knowledge_projects,
            final_goal="Reproduce the study",
            current_stage="orientation",
            daily_available_hours=1.0,
        )
        inventory_project(self.workspace, PROJECT_ID)

    def knowledge_files(self) -> tuple[str, ...]:
        root = self.registration.layout.knowledge_root
        return tuple(
            sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            )
        )

    def test_generate_creates_draft_bundle_without_markdown(self) -> None:
        before_knowledge = self.knowledge_files()

        result = generate_initial_plan(
            self.workspace,
            PROJECT_ID,
            generated_at=TIMESTAMP,
        )

        self.assertTrue(result.goal_created)
        self.assertTrue(result.tasks_created)
        self.assertTrue(result.state_created)
        self.assertTrue(result.state_rebuilt)
        self.assertEqual(result.goal.goal, "Reproduce the study")
        self.assertEqual(result.goal.status, "draft")
        self.assertEqual(result.goal.success_criteria, ())
        self.assertEqual({task.status for task in result.tasks.tasks}, {"draft"})
        self.assertTrue(all(not task.executable for task in result.tasks.tasks))
        self.assertEqual(result.plan.status, "draft")
        self.assertEqual(load_initial_plan(self.workspace, PROJECT_ID), result.plan)
        self.assertEqual(self.knowledge_files(), before_knowledge)

        payload = json.dumps(result.as_dict(), sort_keys=True)
        self.assertNotIn(str(self.workspace), payload)
        self.assertNotIn(str(self.source), payload)
        self.assertNotIn(str(self.registration.layout.machine_root), payload)

    def test_explicit_objective_precedes_onboarding_when_goal_is_missing(self) -> None:
        result = generate_initial_plan(
            self.workspace,
            PROJECT_ID,
            objective="Test the alternative objective",
            generated_at=TIMESTAMP,
        )
        self.assertEqual(result.goal.goal, "Test the alternative objective")
        self.assertNotIn("important_question", result.goal.draft_reasons)

    def test_existing_goal_and_tasks_remain_byte_for_byte_unchanged(self) -> None:
        goal = Goal.draft(
            PROJECT_ID,
            goal="User-authored objective",
            current_stage="analysis",
            created_at=TIMESTAMP,
        )
        tasks = TaskCollection(
            project_id=PROJECT_ID,
            tasks=(draft_task(PROJECT_ID),),
            updated_at=TIMESTAMP,
        )
        GoalStore(self.workspace, PROJECT_ID).write(goal)
        TaskStore(self.workspace, PROJECT_ID).write(tasks)
        generate_project_state(
            self.workspace,
            PROJECT_ID,
            generated_at=TIMESTAMP,
        )
        goal_path = self.registration.layout.goals_file
        task_path = self.registration.layout.tasks_file
        before_goal = goal_path.read_bytes()
        before_tasks = task_path.read_bytes()

        result = generate_initial_plan(
            self.workspace,
            PROJECT_ID,
            objective="Must not overwrite the user Goal",
            generated_at="2026-07-19T02:00:00Z",
        )

        self.assertFalse(result.goal_created)
        self.assertFalse(result.tasks_created)
        self.assertEqual(goal_path.read_bytes(), before_goal)
        self.assertEqual(task_path.read_bytes(), before_tasks)
        self.assertEqual(result.goal.goal, "User-authored objective")

    def test_stale_state_fails_closed_instead_of_silent_rebuild(self) -> None:
        generate_initial_plan(
            self.workspace,
            PROJECT_ID,
            generated_at=TIMESTAMP,
        )
        changed_goal = Goal.draft(
            PROJECT_ID,
            goal="Changed after the state snapshot",
            current_stage="analysis",
            created_at=TIMESTAMP,
            updated_at="2026-07-19T03:00:00Z",
        )
        GoalStore(self.workspace, PROJECT_ID).write(changed_goal)

        with self.assertRaisesRegex(InitialPlanningError, "stale"):
            generate_initial_plan(
                self.workspace,
                PROJECT_ID,
                generated_at="2026-07-19T04:00:00Z",
            )

    def test_load_rejects_stale_or_noncanonical_plan(self) -> None:
        result = generate_initial_plan(
            self.workspace,
            PROJECT_ID,
            generated_at=TIMESTAMP,
        )
        path = self.registration.layout.initial_plan_file
        document = result.plan.as_dict()
        path.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaises(InitialPlanningError):
            load_initial_plan(self.workspace, PROJECT_ID)


if __name__ == "__main__":
    unittest.main()