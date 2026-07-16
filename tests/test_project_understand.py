from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.request
import webbrowser

from tools.project import main as project_main
from tools.project_registry import load_registered_project
from tools.project_runs import (
    PROJECT_RUN_RESULT_KIND,
    PROJECT_RUN_SCHEMA_VERSION,
    PROJECT_UNDERSTAND_STAGES,
)
from tools.research_core import ResearchCoreService


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "minimal_research_project"
DETERMINISTIC_PREFIX = ("register", "inventory", "classify")


class ProjectUnderstandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.knowledge_root = self.root / "knowledge"
        self.source = self.root / "minimal_research_project"
        shutil.copytree(FIXTURE_ROOT, self.source)

    def source_snapshot(
        self,
    ) -> tuple[
        dict[str, tuple[int, int]],
        dict[str, tuple[str, int, int, int]],
    ]:
        directories: dict[str, tuple[int, int]] = {}
        files: dict[str, tuple[str, int, int, int]] = {}
        for directory, dirnames, filenames in os.walk(self.source):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            base_stat = base.stat()
            relative_directory = (
                "." if base == self.source else base.relative_to(self.source).as_posix()
            )
            directories[relative_directory] = (
                base_stat.st_mtime_ns,
                stat.S_IMODE(base_stat.st_mode),
            )
            for filename in filenames:
                path = base / filename
                metadata = path.stat()
                files[path.relative_to(self.source).as_posix()] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    stat.S_IMODE(metadata.st_mode),
                )
        return directories, files

    def invoke_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = project_main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_deterministic_prefix(self, result: object) -> None:
        record = result.record
        stages = record["stages"]
        self.assertEqual(result.status, "paused")
        self.assertEqual(record["through_stage"], "classify")
        self.assertIsNone(record["active_stage"])
        self.assertEqual(
            [stage["stage_id"] for stage in stages],
            list(PROJECT_UNDERSTAND_STAGES),
        )
        self.assertEqual(
            [stage["status"] for stage in stages[: len(DETERMINISTIC_PREFIX)]],
            ["succeeded"] * len(DETERMINISTIC_PREFIX),
        )
        self.assertTrue(
            all(
                stage["status"] == "pending"
                for stage in stages[len(DETERMINISTIC_PREFIX) :]
            )
        )
        self.assertEqual(
            [len(stage["attempts"]) for stage in stages],
            [1, 1, 1] + [0] * (len(PROJECT_UNDERSTAND_STAGES) - 3),
        )
        self.assertTrue(
            all(
                stage["attempts"][0]["status"] == "succeeded"
                for stage in stages[: len(DETERMINISTIC_PREFIX)]
            )
        )
        self.assertEqual(record["errors"], [])
        self.assertEqual(
            record["usage"],
            {
                "providers": [],
                "models": [],
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_hits": 0,
                "estimated_cost": {"currency": "USD", "amount": 0.0},
            },
        )
        self.assertEqual(
            [artifact["relative_path"] for artifact in record["artifacts"]],
            [
                "project.yaml",
                "manifest.jsonl",
                "indexes/coverage-report.json",
            ],
        )

    def test_core_fresh_delegates_register_then_start_only_through_classify(
        self,
    ) -> None:
        service = ResearchCoreService(self.workspace)
        registration = Mock(project_id="delegated-study")
        expected_result = Mock(name="project-run-result")
        register_signature = inspect.signature(ResearchCoreService.register)
        start_signature = inspect.signature(ResearchCoreService.project_run_start)

        with (
            patch.object(
                ResearchCoreService,
                "register",
                autospec=True,
                return_value=registration,
            ) as register,
            patch.object(
                ResearchCoreService,
                "project_run_start",
                autospec=True,
                return_value=expected_result,
            ) as start,
            patch.object(
                ResearchCoreService,
                "project_run_resume",
                autospec=True,
            ) as resume,
        ):
            ordered = Mock()
            ordered.attach_mock(register, "register")
            ordered.attach_mock(start, "start")
            ordered.attach_mock(resume, "resume")
            result = service.project_understand(
                self.source,
                project_id="delegated-study",
                name="Delegated Study",
                knowledge_root=self.knowledge_root,
                final_goal="Understand the deterministic prefix",
                current_stage="inventory",
                important_question="Which files define the result?",
                deadline="2026-08-31",
                daily_available_hours=2.5,
            )

        self.assertIs(result, expected_result)
        self.assertEqual([item[0] for item in ordered.mock_calls], ["register", "start"])
        register_bound = register_signature.bind(
            *register.call_args.args,
            **register.call_args.kwargs,
        )
        register_bound.apply_defaults()
        self.assertEqual(
            register_bound.arguments,
            {
                "self": service,
                "project_root": self.source,
                "project_id": "delegated-study",
                "name": "Delegated Study",
                "knowledge_root": self.knowledge_root,
                "final_goal": "Understand the deterministic prefix",
                "current_stage": "inventory",
                "important_question": "Which files define the result?",
                "deadline": "2026-08-31",
                "daily_available_hours": 2.5,
            },
        )
        start_bound = start_signature.bind(*start.call_args.args, **start.call_args.kwargs)
        start_bound.apply_defaults()
        self.assertEqual(
            start_bound.arguments,
            {
                "self": service,
                "project_id": "delegated-study",
                "through_stage": "classify",
            },
        )
        resume.assert_not_called()

    def test_core_resume_delegates_register_then_resume_same_run_through_classify(
        self,
    ) -> None:
        service = ResearchCoreService(self.workspace)
        registration = Mock(project_id="resume-study")
        expected_result = Mock(name="resumed-project-run-result")
        resume_run_id = "run-20260716t000000000000z-0123456789ab"
        register_signature = inspect.signature(ResearchCoreService.register)
        resume_signature = inspect.signature(ResearchCoreService.project_run_resume)

        with (
            patch.object(
                ResearchCoreService,
                "register",
                autospec=True,
                return_value=registration,
            ) as register,
            patch.object(
                ResearchCoreService,
                "project_run_start",
                autospec=True,
            ) as start,
            patch.object(
                ResearchCoreService,
                "project_run_resume",
                autospec=True,
                return_value=expected_result,
            ) as resume,
        ):
            ordered = Mock()
            ordered.attach_mock(register, "register")
            ordered.attach_mock(start, "start")
            ordered.attach_mock(resume, "resume")
            result = service.project_understand(
                self.source,
                project_id="resume-study",
                resume_run_id=resume_run_id,
            )

        self.assertIs(result, expected_result)
        self.assertEqual([item[0] for item in ordered.mock_calls], ["register", "resume"])
        register_bound = register_signature.bind(
            *register.call_args.args,
            **register.call_args.kwargs,
        )
        register_bound.apply_defaults()
        self.assertEqual(register_bound.arguments["self"], service)
        self.assertEqual(register_bound.arguments["project_root"], self.source)
        self.assertEqual(register_bound.arguments["project_id"], "resume-study")
        self.assertTrue(
            all(
                register_bound.arguments[name] is None
                for name in (
                    "name",
                    "knowledge_root",
                    "final_goal",
                    "current_stage",
                    "important_question",
                    "deadline",
                    "daily_available_hours",
                )
            )
        )
        resume_bound = resume_signature.bind(
            *resume.call_args.args,
            **resume.call_args.kwargs,
        )
        resume_bound.apply_defaults()
        self.assertEqual(
            resume_bound.arguments,
            {
                "self": service,
                "project_id": "resume-study",
                "run_id": resume_run_id,
                "through_stage": "classify",
            },
        )
        start.assert_not_called()

    def test_cli_delegates_only_to_project_understand_with_registration_options(
        self,
    ) -> None:
        run_id = "run-20260716t000000000000z-0123456789ab"
        run_payload = {
            "schema_version": PROJECT_RUN_SCHEMA_VERSION,
            "kind": PROJECT_RUN_RESULT_KIND,
            "project_id": "cli-delegated-study",
            "run_id": run_id,
            "run_file": str(self.workspace / "run.json"),
            "run": {
                "project_id": "cli-delegated-study",
                "run_id": run_id,
                "status": "paused",
            },
        }
        result = Mock()
        result.as_dict.return_value = run_payload
        service = Mock(spec=ResearchCoreService)
        service.project_understand.return_value = result
        understand_signature = inspect.signature(ResearchCoreService.project_understand)

        with patch("tools.project.ResearchCoreService", return_value=service) as service_type:
            code, stdout, stderr = self.invoke_cli(
                [
                    "understand",
                    str(self.source),
                    "--project-id",
                    "cli-delegated-study",
                    "--name",
                    "CLI Delegated Study",
                    "--knowledge-root",
                    str(self.knowledge_root),
                    "--final-goal",
                    "Understand the CLI path",
                    "--current-stage",
                    "classification",
                    "--important-question",
                    "Does the CLI call only Core?",
                    "--deadline",
                    "2026-09-30",
                    "--daily-hours",
                    "3.25",
                    "--resume-run",
                    run_id,
                    "--workspace-root",
                    str(self.workspace),
                    "--json",
                ]
            )

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {"ok": True, **run_payload})
        service_type.assert_called_once_with(str(self.workspace))
        self.assertEqual(
            [item[0] for item in service.method_calls],
            ["project_understand"],
        )
        understand_bound = understand_signature.bind(
            service,
            *service.project_understand.call_args.args,
            **service.project_understand.call_args.kwargs,
        )
        understand_bound.apply_defaults()
        self.assertEqual(
            understand_bound.arguments,
            {
                "self": service,
                "project_root": str(self.source),
                "project_id": "cli-delegated-study",
                "name": "CLI Delegated Study",
                "knowledge_root": str(self.knowledge_root),
                "final_goal": "Understand the CLI path",
                "current_stage": "classification",
                "important_question": "Does the CLI call only Core?",
                "deadline": "2026-09-30",
                "daily_available_hours": 3.25,
                "resume_run_id": run_id,
            },
        )

    def test_actual_core_run_is_deterministic_persisted_read_only_and_resumable(
        self,
    ) -> None:
        before = self.source_snapshot()
        service = ResearchCoreService(self.workspace)
        options = {
            "project_id": "core-understand-study",
            "name": "Core Understand Study",
            "knowledge_root": self.knowledge_root,
            "final_goal": "Understand the fixture deterministically",
            "current_stage": "classification",
            "important_question": "What evidence is available locally?",
            "deadline": "2026-10-15",
            "daily_available_hours": 2.0,
        }

        with ExitStack() as stack:
            guards = (
                stack.enter_context(
                    patch(
                        "tools._utils.call_llm",
                        side_effect=AssertionError("E-08 attempted an LLM call"),
                    )
                ),
                stack.enter_context(
                    patch.object(
                        socket,
                        "create_connection",
                        side_effect=AssertionError("E-08 attempted network access"),
                    )
                ),
                stack.enter_context(
                    patch.object(
                        urllib.request,
                        "urlopen",
                        side_effect=AssertionError("E-08 attempted URL access"),
                    )
                ),
                stack.enter_context(
                    patch.object(
                        webbrowser,
                        "open",
                        side_effect=AssertionError("E-08 attempted browser behavior"),
                    )
                ),
            )
            first = service.project_understand(self.source, **options)
            self.assert_deterministic_prefix(first)

            registration = load_registered_project(
                self.workspace,
                "core-understand-study",
            )
            project_record_bytes = registration.project_file.read_bytes()
            run_files_before_resume = sorted(registration.layout.runs_dir.glob("*/run.json"))
            resumed = service.project_understand(
                self.source,
                **options,
                resume_run_id=first.run_id,
            )

        for guard in guards:
            guard.assert_not_called()

        self.assertEqual(resumed.run_id, first.run_id)
        self.assert_deterministic_prefix(resumed)
        self.assertEqual(
            [len(stage["attempts"]) for stage in resumed.record["stages"][:3]],
            [1, 1, 1],
        )
        self.assertEqual(
            sorted(registration.layout.runs_dir.glob("*/run.json")),
            run_files_before_resume,
        )
        self.assertEqual(registration.project_file.read_bytes(), project_record_bytes)
        self.assertEqual(
            first.run_file,
            registration.layout.runs_dir / first.run_id / "run.json",
        )
        self.assertEqual(
            json.loads(first.run_file.read_text(encoding="utf-8")),
            resumed.record,
        )
        self.assertTrue(registration.layout.manifest_file.is_file())
        coverage_file = registration.layout.indexes_dir / "coverage-report.json"
        self.assertTrue(coverage_file.is_file())
        coverage = json.loads(coverage_file.read_text(encoding="utf-8"))
        self.assertEqual(coverage["manifest"]["scan_generation"], 1)
        self.assertGreater(coverage["totals"]["file_count"], 0)
        self.assertEqual(registration.record["name"], "Core Understand Study")
        self.assertEqual(
            registration.record["onboarding"],
            {
                "final_goal": "Understand the fixture deterministically",
                "current_stage": "classification",
                "important_question": "What evidence is available locally?",
                "deadline": "2026-10-15",
                "daily_available_hours": 2.0,
            },
        )
        self.assertFalse(
            any(path.is_file() for path in registration.layout.knowledge_root.rglob("*"))
        )
        self.assertEqual(before, self.source_snapshot())
        self.assertFalse((self.source / ".llmwiki").exists())
        self.assertFalse((self.source / "wiki").exists())

    def test_repeated_fresh_actions_reuse_registration_but_create_new_runs(
        self,
    ) -> None:
        service = ResearchCoreService(self.workspace)
        options = {
            "project_id": "repeated-understand-study",
            "name": "Repeated Understand Study",
            "knowledge_root": self.knowledge_root,
        }

        first = service.project_understand(self.source, **options)
        registration = load_registered_project(
            self.workspace,
            "repeated-understand-study",
        )
        project_record_bytes = registration.project_file.read_bytes()
        second = service.project_understand(self.source, **options)

        self.assertNotEqual(first.run_id, second.run_id)
        self.assert_deterministic_prefix(first)
        self.assert_deterministic_prefix(second)
        self.assertEqual(registration.project_file.read_bytes(), project_record_bytes)
        self.assertEqual(
            sorted(path.parent.name for path in registration.layout.runs_dir.glob("*/run.json")),
            sorted([first.run_id, second.run_id]),
        )
        self.assertFalse(
            any(path.is_file() for path in registration.layout.knowledge_root.rglob("*"))
        )

    def test_actual_cli_run_emits_normal_persisted_project_run_result(self) -> None:
        before = self.source_snapshot()
        code, stdout, stderr = self.invoke_cli(
            [
                "understand",
                str(self.source),
                "--project-id",
                "cli-understand-study",
                "--name",
                "CLI Understand Study",
                "--knowledge-root",
                str(self.knowledge_root),
                "--goal",
                "Understand the fixture through classification",
                "--current-stage",
                "inventory",
                "--important-question",
                "Which research roles are present?",
                "--deadline",
                "2026-11-01",
                "--daily-hours",
                "1.5",
                "--workspace-root",
                str(self.workspace),
                "--json",
            ]
        )

        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(
            set(payload),
            {
                "ok",
                "schema_version",
                "kind",
                "project_id",
                "run_id",
                "run_file",
                "run",
            },
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema_version"], PROJECT_RUN_SCHEMA_VERSION)
        self.assertEqual(payload["kind"], PROJECT_RUN_RESULT_KIND)
        persisted = ResearchCoreService(self.workspace).project_run_status(
            payload["project_id"],
            payload["run_id"],
        )
        self.assertEqual(payload, {"ok": True, **persisted.as_dict()})
        self.assert_deterministic_prefix(persisted)

        registration = load_registered_project(
            self.workspace,
            "cli-understand-study",
        )
        self.assertTrue(registration.layout.manifest_file.is_file())
        self.assertTrue(
            (registration.layout.indexes_dir / "coverage-report.json").is_file()
        )
        self.assertEqual(before, self.source_snapshot())
        self.assertFalse((self.source / ".llmwiki").exists())
        self.assertFalse((self.source / "wiki").exists())

    def test_cli_invalid_resume_run_reports_stable_error_without_creating_a_run(
        self,
    ) -> None:
        before = self.source_snapshot()
        code, stdout, stderr = self.invoke_cli(
            [
                "understand",
                str(self.source),
                "--project-id",
                "invalid-resume-study",
                "--resume-run",
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
        registration = load_registered_project(
            self.workspace,
            "invalid-resume-study",
        )
        self.assertEqual(list(registration.layout.runs_dir.glob("*/run.json")), [])
        self.assertEqual(before, self.source_snapshot())
        self.assertFalse((self.source / ".llmwiki").exists())
        self.assertFalse((self.source / "wiki").exists())

    def test_cli_understand_rejects_open_and_later_stage_controls(self) -> None:
        with patch("tools.project.ResearchCoreService") as service_type:
            for unsupported in (["--open"], ["--through", "extract"]):
                with self.subTest(unsupported=unsupported):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with (
                        redirect_stdout(stdout),
                        redirect_stderr(stderr),
                        self.assertRaises(SystemExit) as raised,
                    ):
                        project_main(
                            [
                                "understand",
                                str(self.source),
                                *unsupported,
                                "--workspace-root",
                                str(self.workspace),
                            ]
                        )
                    self.assertEqual(raised.exception.code, 2)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn("unrecognized arguments", stderr.getvalue())
            service_type.assert_not_called()


if __name__ == "__main__":
    unittest.main()
