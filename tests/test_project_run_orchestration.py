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
import threading
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
import urllib.request
import webbrowser

from tools import project_runs
from tools.advisory_lock import AdvisoryFileLock, AdvisoryLockTimeoutError
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
    create_project_run,
    elapsed_ms,
    load_project_run,
    project_run_file,
    save_project_run,
    validate_project_run_record,
)
from tools.research_core import ResearchCoreService


_RUN_ID_RE = re.compile(r"^run-[0-9]{8}t[0-9]{12}z-[a-f0-9]{12}$")
_SYNC_TIMEOUT_SECONDS = 5.0


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

    def test_concurrent_direct_saves_serialize_stale_revision_writers(self) -> None:
        created = create_project_run(
            self.workspace,
            self.registration.project_id,
            run_id="run-20260716t010000000000z-111111111111",
            created_at="2026-07-16T01:00:00.000000Z",
        )
        expected_revision = created.record["revision"]
        acquire_barrier = threading.Barrier(2)
        write_entered = threading.Event()
        release_write = threading.Event()
        outcomes: list[object] = []
        outcomes_lock = threading.Lock()
        real_acquire = project_runs._acquire_machine_state_lock
        real_write = project_runs.write_versioned_json

        def synchronized_acquire(
            workspace_root: str | Path,
            project_id: str,
            *,
            timeout_seconds: float,
        ) -> AdvisoryFileLock:
            acquire_barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
            return real_acquire(
                workspace_root,
                project_id,
                timeout_seconds=timeout_seconds,
            )

        def blocking_write(path: Path, payload: dict[str, object]) -> Path:
            write_entered.set()
            if not release_write.wait(timeout=_SYNC_TIMEOUT_SECONDS):
                raise AssertionError("test did not release the first run writer")
            return real_write(path, payload)

        def save_stale_copy() -> None:
            try:
                outcome: object = save_project_run(
                    self.workspace,
                    self.registration.project_id,
                    created.run_id,
                    created.record,
                    expected_revision=expected_revision,
                )
            except BaseException as exc:
                outcome = exc
            with outcomes_lock:
                outcomes.append(outcome)

        with (
            patch.object(
                project_runs,
                "_acquire_machine_state_lock",
                side_effect=synchronized_acquire,
            ),
            patch.object(
                project_runs,
                "write_versioned_json",
                side_effect=blocking_write,
            ) as write_run,
        ):
            threads = [
                threading.Thread(target=save_stale_copy, name=f"run-save-{index}")
                for index in range(2)
            ]
            for thread in threads:
                thread.start()

            writer_reached_checkpoint = write_entered.wait(
                timeout=_SYNC_TIMEOUT_SECONDS
            )
            release_write.set()
            for thread in threads:
                thread.join(timeout=_SYNC_TIMEOUT_SECONDS)

        self.assertTrue(writer_reached_checkpoint)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(outcomes), 2)
        successes = [
            outcome for outcome in outcomes if not isinstance(outcome, BaseException)
        ]
        failures = [
            outcome for outcome in outcomes if isinstance(outcome, BaseException)
        ]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(len(failures), 1, outcomes)
        self.assertIsInstance(failures[0], ProjectRunConflictError)
        self.assertEqual(write_run.call_count, 1)

        persisted = load_project_run(
            self.workspace,
            self.registration.project_id,
            created.run_id,
        )
        on_disk = json.loads(created.run_file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, persisted.record)
        self.assertEqual(on_disk, successes[0].record)
        self.assertEqual(on_disk["revision"], expected_revision + 1)
        validate_project_run_record(on_disk)
        self.assertFalse(created.run_file.with_name("run.json.tmp").exists())

    def test_direct_mutators_are_reentrant_under_orchestrator_outer_lock(
        self,
    ) -> None:
        main_run_id = "run-20260716t020000000000z-222222222222"
        nested_run_id = "run-20260716t020000000001z-333333333333"
        caller_thread = threading.get_ident()
        nested_results: list[object] = []
        nested_threads: list[int] = []

        def exercise_direct_mutators(_context):
            nested_threads.append(threading.get_ident())
            created = create_project_run(
                self.workspace,
                self.registration.project_id,
                run_id=nested_run_id,
                created_at="2026-07-16T02:00:00.000000Z",
                lock_timeout_seconds=0.05,
            )
            saved = save_project_run(
                self.workspace,
                self.registration.project_id,
                nested_run_id,
                created.record,
                expected_revision=created.record["revision"],
                lock_timeout_seconds=0.05,
            )
            nested_results.extend((created, saved))
            return StageOutcome.succeeded()

        result = ProjectRunOrchestrator(
            self.workspace,
            runners={"register": exercise_direct_mutators},
            clock=StepClock(),
            run_id_factory=lambda _moment: main_run_id,
            lock_timeout_seconds=0.05,
        ).start(
            self.registration.project_id,
            through_stage="register",
        )

        self.assertEqual(result.status, "paused")
        self.assertEqual(nested_threads, [caller_thread])
        self.assertEqual(len(nested_results), 2)
        nested = load_project_run(
            self.workspace,
            self.registration.project_id,
            nested_run_id,
        )
        self.assertEqual(nested.record["revision"], 2)
        self.assertEqual(nested.record, nested_results[-1].record)
        self.assertFalse(nested.run_file.with_name("run.json.tmp").exists())

    def test_direct_save_timeout_is_project_scoped(self) -> None:
        other_source = self.root / "other-source"
        other_source.mkdir()
        (other_source / "README.md").write_text("# Other study\n", encoding="utf-8")
        other_registration = register_project(
            self.workspace,
            other_source,
            project_id="run-study-other",
        )
        same_project_run = create_project_run(
            self.workspace,
            self.registration.project_id,
            run_id="run-20260716t030000000000z-444444444444",
            created_at="2026-07-16T03:00:00.000000Z",
        )
        other_project_run = create_project_run(
            self.workspace,
            other_registration.project_id,
            run_id="run-20260716t030000000001z-555555555555",
            created_at="2026-07-16T03:00:00.000001Z",
        )
        start_barrier = threading.Barrier(3)
        same_done = threading.Event()
        other_done = threading.Event()
        outcomes: dict[str, object] = {}
        outcomes_lock = threading.Lock()

        def save_contender(
            label: str,
            project_id: str,
            run_id: str,
            record: dict[str, object],
            timeout_seconds: float,
            done: threading.Event,
        ) -> None:
            try:
                start_barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
                outcome: object = save_project_run(
                    self.workspace,
                    project_id,
                    run_id,
                    record,
                    expected_revision=record["revision"],
                    lock_timeout_seconds=timeout_seconds,
                )
            except BaseException as exc:
                outcome = exc
            with outcomes_lock:
                outcomes[label] = outcome
            done.set()

        same_thread = threading.Thread(
            target=save_contender,
            args=(
                "same",
                self.registration.project_id,
                same_project_run.run_id,
                same_project_run.record,
                0.1,
                same_done,
            ),
            name="same-project-save",
        )
        other_thread = threading.Thread(
            target=save_contender,
            args=(
                "other",
                other_registration.project_id,
                other_project_run.run_id,
                other_project_run.record,
                _SYNC_TIMEOUT_SECONDS,
                other_done,
            ),
            name="other-project-save",
        )
        held_lock = AdvisoryFileLock(
            self.registration.layout.machine_state_lock_file,
            timeout_seconds=_SYNC_TIMEOUT_SECONDS,
        ).acquire()
        same_finished = False
        other_finished = False
        try:
            same_thread.start()
            other_thread.start()
            start_barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
            same_finished = same_done.wait(timeout=_SYNC_TIMEOUT_SECONDS)
            other_finished = other_done.wait(timeout=_SYNC_TIMEOUT_SECONDS)
        finally:
            held_lock.release()
            same_thread.join(timeout=_SYNC_TIMEOUT_SECONDS)
            other_thread.join(timeout=_SYNC_TIMEOUT_SECONDS)

        self.assertTrue(same_finished)
        self.assertTrue(other_finished)
        self.assertFalse(same_thread.is_alive())
        self.assertFalse(other_thread.is_alive())
        same_outcome = outcomes["same"]
        other_outcome = outcomes["other"]
        self.assertIsInstance(same_outcome, ProjectRunError)
        self.assertIsInstance(same_outcome.__cause__, AdvisoryLockTimeoutError)
        self.assertNotIsInstance(other_outcome, BaseException)
        self.assertEqual(other_outcome.record["revision"], 2)

        same_persisted = load_project_run(
            self.workspace,
            self.registration.project_id,
            same_project_run.run_id,
        )
        other_persisted = load_project_run(
            self.workspace,
            other_registration.project_id,
            other_project_run.run_id,
        )
        self.assertEqual(same_persisted.record["revision"], 1)
        self.assertEqual(other_persisted.record, other_outcome.record)
        self.assertFalse(
            same_project_run.run_file.with_name("run.json.tmp").exists()
        )
        self.assertFalse(
            other_project_run.run_file.with_name("run.json.tmp").exists()
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

