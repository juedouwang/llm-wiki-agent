from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
import urllib.request
import webbrowser

from tools.project import main as project_main
from tools.project_layout import UnsupportedSchemaVersionError
from tools.project_orchestrator import ProjectRunOrchestrator
from tools.project_registry import register_project
from tools.project_runs import (
    PROJECT_UNDERSTAND_STAGES,
    ProjectRunConflictError,
    ProjectRunError,
    ProjectRunStateError,
    StageOutcome,
    elapsed_ms,
    load_project_run,
    project_run_file,
    save_project_run,
    validate_project_run_record,
)
from tools.research_core import ResearchCoreService


_RUN_ID_RE = re.compile(r"^run-[0-9]{8}t[0-9]{12}z-[a-f0-9]{12}$")


class StepClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=100)
        return value


class ProjectRunOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.source = self.root / "source"
        (self.source / "src").mkdir(parents=True)
        (self.source / "src" / "model.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        (self.source / "README.md").write_text("# Study\n", encoding="utf-8")
        self.registration = register_project(
            self.workspace,
            self.source,
            project_id="run-study",
        )

    def source_snapshot(self) -> dict[str, tuple[str, int, int, int]]:
        snapshot: dict[str, tuple[str, int, int, int]] = {}
        for directory, dirnames, filenames in os.walk(self.source):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            for filename in filenames:
                path = base / filename
                stat = path.stat()
                snapshot[path.relative_to(self.source).as_posix()] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_mode,
                )
        return snapshot

    def invoke_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = project_main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def all_success_runners(calls: list[tuple[str, int]]):
        def succeed(context):
            calls.append((context.stage_id, context.attempt))
            return StageOutcome.succeeded()

        return {stage_id: succeed for stage_id in PROJECT_UNDERSTAND_STAGES}

    def test_unique_path_safe_run_ids_and_versioned_run_layout(self) -> None:
        first = ProjectRunOrchestrator(self.workspace).start(
            self.registration.project_id,
            through_stage="register",
        )
        second = ProjectRunOrchestrator(self.workspace).start(
            self.registration.project_id,
            through_stage="register",
        )

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertRegex(first.run_id, _RUN_ID_RE)
        self.assertEqual(
            first.run_file,
            self.registration.layout.runs_dir / first.run_id / "run.json",
        )
        self.assertTrue(first.run_file.is_file())
        self.assertFalse(first.run_file.with_name("run.json.tmp").exists())
        self.assertEqual(first.record["schema_version"], 1)
        self.assertEqual(first.record["run_version"], "project-run-v1")
        self.assertEqual(
            [stage["stage_id"] for stage in first.record["stages"]],
            list(PROJECT_UNDERSTAND_STAGES),
        )

    def test_missing_handlers_are_explicit_unavailable_and_partial(self) -> None:
        result = ProjectRunOrchestrator(self.workspace, clock=StepClock()).start(
            self.registration.project_id
        )

        self.assertEqual(result.status, "partial")
        self.assertTrue(
            all(stage["status"] == "unavailable" for stage in result.record["stages"])
        )
        self.assertEqual(len(result.record["errors"]), len(PROJECT_UNDERSTAND_STAGES))
        self.assertEqual(
            {error["reason_code"] for error in result.record["errors"]},
            {"stage-handler-unavailable"},
        )
        self.assertEqual(result.record["usage"]["providers"], [])
        self.assertEqual(result.record["usage"]["models"], [])
        self.assertEqual(result.record["usage"]["estimated_cost"]["amount"], 0.0)
        for stage in result.record["stages"]:
            attempt = stage["attempts"][0]
            self.assertIsNone(attempt["provider"])
            self.assertIsNone(attempt["model"])
            self.assertEqual(attempt["duration_ms"], 100)

    def test_failed_stage_resumes_without_repeating_successful_stages(self) -> None:
        calls: list[tuple[str, int]] = []
        failed_once = False

        def runner(context):
            nonlocal failed_once
            calls.append((context.stage_id, context.attempt))
            if context.stage_id == "inventory" and not failed_once:
                failed_once = True
                return StageOutcome.failed(
                    "fixture-failure",
                    "first inventory attempt failed",
                    provider="fixture-provider",
                    model="fixture-model",
                    input_tokens=4,
                    output_tokens=2,
                    cache_hits=1,
                    estimated_cost_usd=0.25,
                    artifacts=(
                        {
                            "artifact_type": "failure-log",
                            "artifact_id": "inventory-attempt-1",
                            "relative_path": "runs/failure.json",
                            "content_hash": None,
                        },
                    ),
                )
            return StageOutcome.succeeded(
                provider="fixture-provider",
                model="fixture-model",
                input_tokens=1,
                output_tokens=1,
                estimated_cost_usd=0.01,
            )

        clock = StepClock()
        runners = {stage_id: runner for stage_id in PROJECT_UNDERSTAND_STAGES}
        first = ProjectRunOrchestrator(
            self.workspace, runners=runners, clock=clock
        ).start(self.registration.project_id)

        self.assertEqual(first.status, "failed")
        self.assertEqual(first.record["stages"][0]["status"], "succeeded")
        self.assertEqual(first.record["stages"][1]["status"], "failed")
        self.assertTrue(
            all(stage["status"] == "pending" for stage in first.record["stages"][2:])
        )

        resumed = ProjectRunOrchestrator(
            self.workspace, runners=runners, clock=clock
        ).resume(self.registration.project_id, first.run_id)

        self.assertEqual(resumed.run_id, first.run_id)
        self.assertEqual(resumed.status, "succeeded")
        self.assertEqual(calls.count(("register", 1)), 1)
        self.assertEqual(
            [item for item in calls if item[0] == "inventory"],
            [("inventory", 1), ("inventory", 2)],
        )
        self.assertEqual(len(resumed.record["errors"]), 1)
        self.assertEqual(resumed.record["errors"][0]["reason_code"], "fixture-failure")
        inventory = resumed.record["stages"][1]
        self.assertEqual([item["status"] for item in inventory["attempts"]], ["failed", "succeeded"])
        self.assertIsNone(inventory["last_error"])
        self.assertIn("fixture-provider", resumed.record["usage"]["providers"])
        self.assertIn("fixture-model", resumed.record["usage"]["models"])
        self.assertEqual(resumed.record["usage"]["input_tokens"], 15)
        self.assertEqual(resumed.record["usage"]["output_tokens"], 13)
        self.assertEqual(resumed.record["usage"]["cache_hits"], 1)
        self.assertAlmostEqual(
            resumed.record["usage"]["estimated_cost"]["amount"], 0.36
        )
        self.assertEqual(resumed.record["artifacts"][0]["artifact_id"], "inventory-attempt-1")

    def test_keyboard_interrupt_leaves_checkpoint_and_resume_records_interruption(self) -> None:
        calls: list[tuple[str, int]] = []
        interrupted = False

        def runner(context):
            nonlocal interrupted
            calls.append((context.stage_id, context.attempt))
            if context.stage_id == "inventory" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return StageOutcome.succeeded()

        clock = StepClock()
        runners = {stage_id: runner for stage_id in PROJECT_UNDERSTAND_STAGES}
        orchestrator = ProjectRunOrchestrator(
            self.workspace, runners=runners, clock=clock
        )
        with self.assertRaises(KeyboardInterrupt):
            orchestrator.start(self.registration.project_id)

        runs = list(self.registration.layout.runs_dir.glob("*/run.json"))
        self.assertEqual(len(runs), 1)
        run_id = runs[0].parent.name
        checkpoint = load_project_run(
            self.workspace, self.registration.project_id, run_id
        )
        self.assertEqual(checkpoint.status, "running")
        self.assertEqual(checkpoint.record["active_stage"], "inventory")

        resumed = ProjectRunOrchestrator(
            self.workspace, runners=runners, clock=clock
        ).resume(self.registration.project_id, run_id)

        self.assertEqual(resumed.status, "succeeded")
        inventory = resumed.record["stages"][1]
        self.assertEqual([attempt["status"] for attempt in inventory["attempts"]], ["failed", "succeeded"])
        self.assertEqual(
            inventory["attempts"][0]["error"]["reason_code"],
            "stage-interrupted",
        )
        self.assertEqual(calls.count(("register", 1)), 1)
        self.assertEqual(calls.count(("inventory", 2)), 1)

    def test_through_stage_pauses_then_full_resume_skips_completed_work(self) -> None:
        calls: list[tuple[str, int]] = []
        runners = self.all_success_runners(calls)
        clock = StepClock()
        paused = ProjectRunOrchestrator(
            self.workspace, runners=runners, clock=clock
        ).start(self.registration.project_id, through_stage="classify")

        self.assertEqual(paused.status, "paused")
        self.assertEqual(paused.record["through_stage"], "classify")
        self.assertEqual(
            [stage["status"] for stage in paused.record["stages"][:3]],
            ["succeeded", "succeeded", "succeeded"],
        )
        self.assertTrue(
            all(stage["status"] == "pending" for stage in paused.record["stages"][3:])
        )

        completed = ProjectRunOrchestrator(
            self.workspace, runners=runners, clock=clock
        ).resume(self.registration.project_id, paused.run_id)
        self.assertEqual(completed.status, "succeeded")
        self.assertIsNone(completed.record["through_stage"])
        self.assertEqual(
            [stage for stage, attempt in calls if attempt == 1],
            list(PROJECT_UNDERSTAND_STAGES),
        )

    def test_unavailable_stage_is_retried_only_when_handler_is_installed(self) -> None:
        initial = ProjectRunOrchestrator(self.workspace).start(
            self.registration.project_id
        )
        revision = initial.record["revision"]
        unchanged = ProjectRunOrchestrator(self.workspace).resume(
            self.registration.project_id, initial.run_id
        )
        self.assertEqual(unchanged.record["revision"], revision)

        calls: list[tuple[str, int]] = []

        def extract(context):
            calls.append((context.stage_id, context.attempt))
            return StageOutcome.succeeded()

        retried = ProjectRunOrchestrator(
            self.workspace, runners={"extract": extract}
        ).resume(self.registration.project_id, initial.run_id)
        self.assertEqual(retried.status, "partial")
        self.assertEqual(calls, [("extract", 2)])
        extract_stage = retried.record["stages"][3]
        self.assertEqual([attempt["status"] for attempt in extract_stage["attempts"]], ["unavailable", "succeeded"])
        self.assertEqual(
            retried.record["stages"][0]["attempts"][0]["status"], "unavailable"
        )
        self.assertEqual(len(retried.record["stages"][0]["attempts"]), 1)

    def test_run_schema_is_closed_ledger_checked_and_future_versions_fail_closed(self) -> None:
        result = ProjectRunOrchestrator(self.workspace).start(
            self.registration.project_id,
            through_stage="register",
        )
        unknown = deepcopy(result.record)
        unknown["unexpected"] = True
        with self.assertRaises(ProjectRunStateError):
            validate_project_run_record(unknown)

        bad_ledger = deepcopy(result.record)
        bad_ledger["usage"]["input_tokens"] = 99
        with self.assertRaises(ProjectRunStateError):
            validate_project_run_record(bad_ledger)

        payload = json.loads(result.run_file.read_text(encoding="utf-8"))
        payload["schema_version"] = 2
        result.run_file.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(UnsupportedSchemaVersionError):
            load_project_run(self.workspace, self.registration.project_id, result.run_id)

    def test_legacy_run_is_read_only_and_path_traversal_is_rejected(self) -> None:
        result = ProjectRunOrchestrator(self.workspace).start(
            self.registration.project_id,
            through_stage="register",
        )
        payload = json.loads(result.run_file.read_text(encoding="utf-8"))
        del payload["schema_version"]
        original = json.dumps(payload, sort_keys=True)
        result.run_file.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ProjectRunStateError):
            load_project_run(self.workspace, self.registration.project_id, result.run_id)
        self.assertEqual(
            json.dumps(json.loads(result.run_file.read_text(encoding="utf-8")), sort_keys=True),
            original,
        )
        for invalid in ("../escape", "run.json", "RUN-20260716"):
            with self.subTest(run_id=invalid), self.assertRaises(ProjectRunError):
                project_run_file(self.workspace, self.registration.project_id, invalid)

    def test_optimistic_revision_rejects_stale_writer(self) -> None:
        result = ProjectRunOrchestrator(self.workspace).start(
            self.registration.project_id,
            through_stage="register",
        )
        first_revision = result.record["revision"]
        saved = save_project_run(
            self.workspace,
            self.registration.project_id,
            result.run_id,
            result.record,
            expected_revision=first_revision,
        )
        self.assertEqual(saved.record["revision"], first_revision + 1)
        with self.assertRaises(ProjectRunConflictError):
            save_project_run(
                self.workspace,
                self.registration.project_id,
                result.run_id,
                result.record,
                expected_revision=first_revision,
            )

    def test_core_default_stages_delegate_and_leave_source_read_only(self) -> None:
        before = self.source_snapshot()
        service = ResearchCoreService(self.workspace)
        result = service.project_run_start(self.registration.project_id)
        after = self.source_snapshot()

        self.assertEqual(before, after)
        self.assertFalse((self.source / ".llmwiki").exists())
        self.assertFalse((self.source / "wiki").exists())
        self.assertEqual(result.status, "partial")
        self.assertEqual(
            [stage["status"] for stage in result.record["stages"][:3]],
            ["succeeded", "succeeded", "succeeded"],
        )
        self.assertTrue(
            all(stage["status"] == "unavailable" for stage in result.record["stages"][3:])
        )
        self.assertTrue(self.registration.layout.manifest_file.is_file())
        self.assertTrue(
            (self.registration.layout.indexes_dir / "coverage-report.json").is_file()
        )
        self.assertTrue(result.run_file.is_relative_to(self.registration.layout.runs_dir))
        restarted = ResearchCoreService(self.workspace)
        self.assertEqual(
            restarted.project_run_status(
                self.registration.project_id, result.run_id
            ).record,
            result.record,
        )

    def test_cli_delegates_start_resume_and_show_to_core(self) -> None:
        result = ProjectRunOrchestrator(self.workspace).start(
            self.registration.project_id,
            through_stage="register",
        )
        service = Mock()
        service.project_run_start.return_value = result
        service.project_run_resume.return_value = result
        service.project_run_status.return_value = result
        with patch("tools.project.ResearchCoreService", return_value=service):
            code, stdout, stderr = self.invoke_cli(
                [
                    "run",
                    "start",
                    self.registration.project_id,
                    "--through",
                    "classify",
                    "--workspace-root",
                    str(self.workspace),
                    "--json",
                ]
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertEqual(json.loads(stdout)["run_id"], result.run_id)
            service.project_run_start.assert_called_once_with(
                project_id=self.registration.project_id,
                through_stage="classify",
            )

            code, _, stderr = self.invoke_cli(
                [
                    "run",
                    "resume",
                    self.registration.project_id,
                    result.run_id,
                    "--workspace-root",
                    str(self.workspace),
                    "--json",
                ]
            )
            self.assertEqual((code, stderr), (0, ""))
            service.project_run_resume.assert_called_once_with(
                project_id=self.registration.project_id,
                run_id=result.run_id,
                through_stage=None,
            )

            code, _, stderr = self.invoke_cli(
                [
                    "run",
                    "show",
                    self.registration.project_id,
                    result.run_id,
                    "--workspace-root",
                    str(self.workspace),
                    "--json",
                ]
            )
            self.assertEqual((code, stderr), (0, ""))
            service.project_run_status.assert_called_once_with(
                project_id=self.registration.project_id,
                run_id=result.run_id,
            )

    def test_actual_cli_and_core_observe_the_same_persisted_run(self) -> None:
        code, stdout, stderr = self.invoke_cli(
            [
                "run",
                "start",
                self.registration.project_id,
                "--through",
                "classify",
                "--workspace-root",
                str(self.workspace),
                "--json",
            ]
        )
        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        core_result = ResearchCoreService(self.workspace).project_run_status(
            self.registration.project_id, payload["run_id"]
        )
        self.assertEqual(payload["run"], core_result.record)

        code, shown, stderr = self.invoke_cli(
            [
                "run",
                "show",
                self.registration.project_id,
                payload["run_id"],
                "--workspace-root",
                str(self.workspace),
                "--json",
            ]
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(shown)["run"], core_result.record)

    def test_cli_reports_invalid_run_id_with_stable_reason_code(self) -> None:
        code, stdout, stderr = self.invoke_cli(
            [
                "run",
                "show",
                self.registration.project_id,
                "../escape",
                "--workspace-root",
                str(self.workspace),
                "--json",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason_code"], "project-run-invalid")

    def test_deterministic_core_run_has_no_llm_network_web_or_hook_behavior(self) -> None:
        with ExitStack() as stack:
            guards = (
                stack.enter_context(
                    patch(
                        "tools._utils.call_llm",
                        side_effect=AssertionError("E-01 attempted an LLM call"),
                    )
                ),
                stack.enter_context(
                    patch.object(
                        socket,
                        "create_connection",
                        side_effect=AssertionError("E-01 attempted network access"),
                    )
                ),
                stack.enter_context(
                    patch.object(
                        urllib.request,
                        "urlopen",
                        side_effect=AssertionError("E-01 attempted URL access"),
                    )
                ),
                stack.enter_context(
                    patch.object(
                        webbrowser,
                        "open",
                        side_effect=AssertionError("E-01 attempted Web behavior"),
                    )
                ),
            )
            result = ResearchCoreService(self.workspace).project_run_start(
                self.registration.project_id,
                through_stage="classify",
            )
            self.assertEqual(result.status, "paused")
        for guard in guards:
            guard.assert_not_called()

    def test_report_durations_match_canonical_timestamps(self) -> None:
        calls: list[tuple[str, int]] = []
        result = ProjectRunOrchestrator(
            self.workspace,
            runners=self.all_success_runners(calls),
            clock=StepClock(),
        ).start(self.registration.project_id)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(
            result.record["duration_ms"],
            elapsed_ms(result.record["started_at"], result.record["completed_at"]),
        )
        for stage in result.record["stages"]:
            for attempt in stage["attempts"]:
                self.assertEqual(
                    attempt["duration_ms"],
                    elapsed_ms(attempt["started_at"], attempt["completed_at"]),
                )


if __name__ == "__main__":
    unittest.main()

