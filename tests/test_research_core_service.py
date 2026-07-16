from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tools.evidence_registry import register_evidence
from tools.extraction_schema import LineRangeLocator
from tools.project import main as project_main
from tools.project_reconciliation import ProjectReconciliationResult
from tools.research_core import (
    HostCoverageResult,
    HostSourceOpenResult,
    ResearchCoreService,
)
from tools.scan_policy import ScanPolicyConfig, ScanPolicyError
from tools.source_access import (
    SourceLocatorError,
    SourceNotFoundError,
)
from tools.source_registry import load_source_registry, sync_source_registry


REPO_ROOT = Path(__file__).resolve().parent.parent


class ResearchCoreServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.knowledge = self.root / "knowledge"
        self.project = self.root / "research-project"
        (self.project / "src").mkdir(parents=True)
        self.source_path = self.project / "src" / "model.py"
        self.source_bytes = b"alpha = 1\r\nbeta = 2\r\n"
        self.source_path.write_bytes(self.source_bytes)
        (self.project / "notes.tmp").write_text("ignored", encoding="utf-8")
        self.service = ResearchCoreService(self.workspace)

    def source_snapshot(self) -> dict[str, tuple[str, int, int, int]]:
        snapshot: dict[str, tuple[str, int, int, int]] = {}
        for directory, dirnames, filenames in os.walk(self.project):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            for name in filenames:
                path = base / name
                relative = path.relative_to(self.project).as_posix()
                metadata = path.stat()
                snapshot[relative] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_mode,
                )
        return snapshot

    def prepare_results(self):
        registration = self.service.register(
            self.project,
            project_id="g-01-fixture",
            knowledge_root=self.knowledge,
            final_goal="Verify the service boundary",
        )
        inventory = self.service.scan(
            registration.project_id,
            exclude_patterns=("*.tmp",),
        )
        coverage = self.service.coverage(registration.project_id)
        sync_source_registry(self.workspace, registration.project_id)
        registry = load_source_registry(self.workspace, registration.project_id)
        source = registry.current_by_path["src/model.py"]
        locator = LineRangeLocator(2, 2)
        opened = self.service.source_open(
            registration.project_id,
            source.source_id,
            locator=locator,
            expected_content_hash=source.current_content_hash,
        )
        return registration, inventory, coverage, source, locator, opened

    def invoke_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = project_main(argv)
        return return_code, stdout.getvalue(), stderr.getvalue()

    def invoke_cli_process(
        self,
        argv: list[str],
        *,
        module_entry: bool = False,
    ) -> tuple[int, str, str]:
        entry = (
            ["-m", "tools.project"]
            if module_entry
            else [str(REPO_ROOT / "tools" / "project.py")]
        )
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            [sys.executable, "-B", *entry, *argv],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        return completed.returncode, completed.stdout, completed.stderr

    def test_direct_service_exposes_the_g01_operations_without_source_writes(self) -> None:
        before = self.source_snapshot()
        registration, inventory, coverage, source, locator, opened = (
            self.prepare_results()
        )

        self.assertEqual(self.service.workspace_root, self.workspace.resolve())
        self.assertTrue(registration.created)
        self.assertEqual(registration.project_id, "g-01-fixture")
        self.assertEqual(inventory.record_counts["file"], 1)
        self.assertEqual(inventory.record_counts["excluded_file"], 1)
        self.assertEqual(coverage.report["totals"]["file_count"], 1)
        self.assertEqual(opened.source.source_id, source.source_id)
        self.assertEqual(opened.locator, locator)
        self.assertEqual(opened.excerpt, "beta = 2\r\n")

        evidence = register_evidence(
            self.workspace,
            registration.project_id,
            source_id=source.source_id,
            content_hash=source.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt="alpha = 1\r\n",
        ).evidence
        reopened = self.service.source_open(
            registration.project_id,
            evidence.evidence_id,
        )
        self.assertEqual(reopened.evidence_id, evidence.evidence_id)
        self.assertEqual(reopened.excerpt, "alpha = 1\r\n")
        self.assertTrue(reopened.excerpt_hash_verified)

        context_pack = self.service.host_context_pack(registration.project_id)
        self.assertEqual(context_pack.payload["project_id"], registration.project_id)
        self.assertEqual(
            [item["evidence_id"] for item in context_pack.payload["evidence_refs"]],
            [evidence.evidence_id],
        )
        self.assertLessEqual(context_pack.used_bytes, context_pack.max_bytes)

        self.assertEqual(self.source_snapshot(), before)
        self.assertFalse((self.project / ".llmwiki").exists())
        self.assertFalse((self.project / "wiki").exists())

    def test_host_views_reject_nested_path_or_locator_injection(self) -> None:
        registration, _inventory, _coverage, source, locator, _opened = (
            self.prepare_results()
        )
        coverage_view = self.service.coverage_view(registration.project_id)
        bad_report = json.loads(json.dumps(coverage_view.report))
        bad_report["manifest"]["manifest_file"] = str(
            self.project / "secret-manifest.jsonl"
        )
        with self.assertRaises(ValueError):
            HostCoverageResult(
                project_id=registration.project_id,
                report=bad_report,
            ).as_dict()

        source_view = self.service.source_open_view(
            registration.project_id,
            source.source_id,
            locator=locator,
        )

        source_path = self.source_path

        class InjectedLocator:
            def as_dict(self):
                return {
                    **locator.as_dict(),
                    "absolute_path": str(source_path),
                }

        injected_open = replace(source_view.opened, locator=InjectedLocator())
        with self.assertRaises(ValueError):
            HostSourceOpenResult(injected_open).as_dict()

        absolute_location = replace(
            source_view.opened.source,
            current_path=str(self.source_path),
        )
        injected_open = replace(source_view.opened, source=absolute_location)
        with self.assertRaises(ValueError):
            HostSourceOpenResult(injected_open).as_dict()

    def test_scan_accepts_complete_policy_without_narrowing_core_capability(
        self,
    ) -> None:
        registration = self.service.register(
            self.project,
            project_id="g-01-policy",
            knowledge_root=self.knowledge,
        )
        policy = ScanPolicyConfig(
            exclude_patterns=("*.tmp",),
            sensitive_patterns=("private/**",),
            external_exclude_patterns=("private/**",),
            max_content_file_bytes=2_000_000,
            max_raw_external_send_bytes=100_000,
            follow_symlinks=False,
            external_send_mode="local-only",
            case_sensitive=True,
        )
        inventory = self.service.scan(
            registration.project_id,
            policy_config=policy,
        )
        self.assertEqual(inventory.policy["config"], policy.as_dict())

        with self.assertRaises(ScanPolicyError):
            self.service.scan(
                registration.project_id,
                policy_config=policy,
                exclude_patterns=("other/**",),
            )

    def test_project_reconcile_delegates_path_bearing_core_to_path_free_boundary(
        self,
    ) -> None:
        project_id = "h-07-delegation"
        empty_ledger_sha = hashlib.sha256(b"").hexdigest()
        expected = ProjectReconciliationResult(
            project_id=project_id,
            run_id="run-h-07",
            run_status="paused",
            stage_statuses={
                "register": "succeeded",
                "inventory": "succeeded",
                "classify": "succeeded",
            },
            manifest_version="project-inventory-v4",
            manifest_scan_generation=1,
            manifest_sha256="a" * 64,
            coverage_report_version="coverage-report-v1",
            coverage_sha256="b" * 64,
            coverage_failure_count=0,
            hint_status="explicit-only",
            snapshot_event_count=0,
            snapshot_last_sequence=0,
            snapshot_ledger_sha256=empty_ledger_sha,
            pending_event_count=0,
            queued_dirty_path_count=0,
            explicit_dirty_path_count=2,
            combined_dirty_path_count=2,
            projection_rebuilt=False,
            previous_acknowledged_through_sequence=0,
            acknowledged_through_sequence=0,
            newly_acknowledged_event_count=0,
            remaining_event_count=0,
            remaining_dirty_path_count=0,
            state_revision=1,
            acknowledged_ledger_sha256=empty_ledger_sha,
        )
        dirty_paths = (Path("src/model.py"), "README.md")
        clock = Mock(name="reconciliation_clock")

        with patch(
            "tools.research_core.reconcile_project",
            return_value=expected,
        ) as delegate:
            actual = self.service.project_reconcile(
                project_id,
                dirty_paths=dirty_paths,
                clock=clock,
                lock_timeout_seconds=1.25,
            )

        self.assertIs(actual, expected)
        delegate.assert_called_once_with(
            self.workspace.resolve(),
            project_id,
            dirty_paths=dirty_paths,
            run_factory=self.service.project_run_start,
            coverage_factory=self.service.coverage,
            clock=clock,
            lock_timeout_seconds=1.25,
        )
        call = delegate.call_args
        self.assertIs(
            call.kwargs["run_factory"].__func__,
            ResearchCoreService.project_run_start,
        )
        self.assertIs(
            call.kwargs["coverage_factory"].__func__,
            ResearchCoreService.coverage,
        )
        self.assertIsNot(
            call.kwargs["coverage_factory"].__func__,
            ResearchCoreService.coverage_view,
        )

        serialized = json.dumps(actual.as_dict(), ensure_ascii=False, sort_keys=True)
        for forbidden in (
            str(self.workspace.resolve()),
            str(self.project.resolve()),
            "src/model.py",
            "README.md",
            "run_file",
            "manifest_file",
            "report_file",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_real_script_and_module_cli_match_direct_service_results(self) -> None:
        registration = self.service.register(
            self.project,
            project_id="g-01-real-parity",
            knowledge_root=self.knowledge,
            final_goal="Verify real CLI parity",
        )

        direct_registration = self.service.register(
            self.project,
            project_id=registration.project_id,
            knowledge_root=self.knowledge,
            final_goal="Verify real CLI parity",
        )
        code, stdout, stderr = self.invoke_cli_process(
            [
                "register",
                str(self.project),
                "--workspace-root",
                str(self.workspace),
                "--project-id",
                registration.project_id,
                "--knowledge-root",
                str(self.knowledge),
                "--goal",
                "Verify real CLI parity",
                "--json",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {"ok": True, **direct_registration.as_dict()},
        )

        scan_baseline = self.root / "scan-baseline"
        shutil.copytree(registration.layout.machine_root, scan_baseline)
        direct_inventory = self.service.scan(
            registration.project_id,
            exclude_patterns=("*.tmp",),
        ).as_dict()
        direct_scan_state = self.root / "direct-scan-state"
        registration.layout.machine_root.rename(direct_scan_state)
        shutil.copytree(scan_baseline, registration.layout.machine_root)

        code, stdout, stderr = self.invoke_cli_process(
            [
                "inventory",
                registration.project_id,
                "--workspace-root",
                str(self.workspace),
                "--exclude",
                "*.tmp",
                "--json",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {"ok": True, **direct_inventory},
        )

        direct_coverage = self.service.coverage(registration.project_id)
        code, stdout, stderr = self.invoke_cli_process(
            [
                "coverage",
                registration.project_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {"ok": True, **direct_coverage.as_dict()},
        )

        sync_source_registry(self.workspace, registration.project_id)
        registry = load_source_registry(self.workspace, registration.project_id)
        source = registry.current_by_path["src/model.py"]
        locator = LineRangeLocator(2, 2)
        direct_opened = self.service.source_open(
            registration.project_id,
            source.source_id,
            locator=locator,
            expected_content_hash=source.current_content_hash,
        )
        code, stdout, stderr = self.invoke_cli_process(
            [
                "source",
                "open",
                registration.project_id,
                source.source_id,
                "--locator-json",
                json.dumps(locator.as_dict()),
                "--expected-content-hash",
                source.current_content_hash,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {"ok": True, **direct_opened.as_dict()},
        )

        evidence = register_evidence(
            self.workspace,
            registration.project_id,
            source_id=source.source_id,
            content_hash=source.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt="alpha = 1\r\n",
        ).evidence
        direct_context_pack = self.service.host_context_pack(
            registration.project_id,
            max_bytes=8192,
        )
        code, stdout, stderr = self.invoke_cli_process(
            [
                "context-pack",
                registration.project_id,
                "--workspace-root",
                str(self.workspace),
                "--max-bytes",
                "8192",
                "--json",
            ],
            module_entry=True,
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {"ok": True, **direct_context_pack.as_dict()},
        )

        direct_evidence = self.service.source_open(
            registration.project_id,
            evidence.evidence_id,
        )
        code, stdout, stderr = self.invoke_cli_process(
            [
                "source",
                "open",
                registration.project_id,
                evidence.evidence_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ],
            module_entry=True,
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {"ok": True, **direct_evidence.as_dict()},
        )

    def test_cli_delegates_and_serializes_exact_service_results(self) -> None:
        registration, inventory, coverage, source, locator, opened = (
            self.prepare_results()
        )
        fake_service = Mock(spec=ResearchCoreService)
        fake_service.register.return_value = registration
        fake_service.scan.return_value = inventory
        context_pack = self.service.host_context_pack(registration.project_id)
        fake_service.coverage.return_value = coverage
        fake_service.host_context_pack.return_value = context_pack
        fake_service.source_open.return_value = opened

        with patch("tools.project.ResearchCoreService", return_value=fake_service):
            return_code, stdout, stderr = self.invoke_cli(
                [
                    "register",
                    str(self.project),
                    "--workspace-root",
                    str(self.workspace),
                    "--project-id",
                    registration.project_id,
                    "--knowledge-root",
                    str(self.knowledge),
                    "--goal",
                    "Verify the service boundary",
                    "--json",
                ]
            )
            self.assertEqual(return_code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(
                json.loads(stdout),
                {"ok": True, **registration.as_dict()},
            )
            fake_service.register.assert_called_once_with(
                project_root=str(self.project),
                project_id=registration.project_id,
                name=None,
                knowledge_root=str(self.knowledge),
                final_goal="Verify the service boundary",
                current_stage=None,
                important_question=None,
                deadline=None,
                daily_available_hours=None,
            )

            return_code, stdout, stderr = self.invoke_cli(
                [
                    "inventory",
                    registration.project_id,
                    "--workspace-root",
                    str(self.workspace),
                    "--exclude",
                    "*.tmp",
                    "--follow-symlinks",
                    "--json",
                ]
            )
            self.assertEqual(return_code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(
                json.loads(stdout),
                {"ok": True, **inventory.as_dict()},
            )
            fake_service.scan.assert_called_once_with(
                project_id=registration.project_id,
                include_patterns=[],
                exclude_patterns=["*.tmp"],
                follow_symlinks=True,
            )

            return_code, stdout, stderr = self.invoke_cli(
                [
                    "context-pack",
                    registration.project_id,
                    "--workspace-root",
                    str(self.workspace),
                    "--json",
                ]
            )
            self.assertEqual(return_code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(
                json.loads(stdout),
                {"ok": True, **context_pack.as_dict()},
            )
            fake_service.host_context_pack.assert_called_once_with(
                project_id=registration.project_id,
                max_bytes=32768,
            )

            return_code, stdout, stderr = self.invoke_cli(
                [
                    "coverage",
                    registration.project_id,
                    "--workspace-root",
                    str(self.workspace),
                    "--json",
                ]
            )
            self.assertEqual(return_code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(
                json.loads(stdout),
                {"ok": True, **coverage.as_dict()},
            )
            fake_service.coverage.assert_called_once_with(
                project_id=registration.project_id,
            )

            return_code, stdout, stderr = self.invoke_cli(
                [
                    "source",
                    "open",
                    registration.project_id,
                    source.source_id,
                    "--locator-json",
                    json.dumps(locator.as_dict()),
                    "--expected-content-hash",
                    source.current_content_hash,
                    "--workspace-root",
                    str(self.workspace),
                    "--json",
                ]
            )
            self.assertEqual(return_code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(
                json.loads(stdout),
                {"ok": True, **opened.as_dict()},
            )
            fake_service.source_open.assert_called_once_with(
                project_id=registration.project_id,
                target_id=source.source_id,
                locator=locator,
                expected_content_hash=source.current_content_hash,
                expected_excerpt_hash=None,
            )

    def test_source_open_validation_is_service_neutral_and_fails_closed(self) -> None:
        registration, _, _, source, _, _ = self.prepare_results()
        with self.assertRaises(SourceLocatorError) as missing:
            self.service.source_open(registration.project_id, source.source_id)
        self.assertEqual(missing.exception.reason_code, "source-locator-invalid")

        evidence = register_evidence(
            self.workspace,
            registration.project_id,
            source_id=source.source_id,
            content_hash=source.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt="alpha = 1\r\n",
        ).evidence
        with self.assertRaises(SourceLocatorError):
            self.service.source_open(
                registration.project_id,
                evidence.evidence_id,
                locator=LineRangeLocator(1, 1),
            )
        with self.assertRaises(SourceNotFoundError):
            self.service.source_open(registration.project_id, "unknown-target")
        with self.assertRaises(SourceNotFoundError) as malformed:
            self.service.source_open(
                registration.project_id,
                None,  # type: ignore[arg-type]
            )
        self.assertEqual(malformed.exception.reason_code, "source-not-registered")

    def test_cli_preserves_source_open_error_priority_and_reason_codes(self) -> None:
        registration, _, _, source, _, _ = self.prepare_results()
        evidence = register_evidence(
            self.workspace,
            registration.project_id,
            source_id=source.source_id,
            content_hash=source.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt="alpha = 1\r\n",
        ).evidence

        cases = (
            (evidence.evidence_id, SourceLocatorError, "source-locator-invalid"),
            ("unknown-target", SourceNotFoundError, "source-not-registered"),
        )
        for target_id, exception_type, reason_code in cases:
            with self.subTest(target_id=target_id):
                with self.assertRaises(exception_type) as direct:
                    self.service.source_open(
                        registration.project_id,
                        target_id,
                        locator={},
                    )
                code, stdout, stderr = self.invoke_cli_process(
                    [
                        "source",
                        "open",
                        registration.project_id,
                        target_id,
                        "--locator-json",
                        "{",
                        "--workspace-root",
                        str(self.workspace),
                        "--json",
                    ]
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    json.loads(stderr),
                    {
                        "ok": False,
                        "error": str(direct.exception),
                        "reason_code": reason_code,
                    },
                )

    def test_service_chain_has_no_llm_network_or_web_side_effects(self) -> None:
        with ExitStack() as stack:
            guards = (
                stack.enter_context(
                    patch(
                        "tools._utils.call_llm",
                        side_effect=AssertionError("G-01 attempted an LLM call"),
                    )
                ),
                stack.enter_context(
                    patch(
                        "socket.create_connection",
                        side_effect=AssertionError("G-01 attempted network access"),
                    )
                ),
                stack.enter_context(
                    patch(
                        "urllib.request.urlopen",
                        side_effect=AssertionError("G-01 attempted URL access"),
                    )
                ),
                stack.enter_context(
                    patch(
                        "webbrowser.open",
                        side_effect=AssertionError("G-01 attempted Web UI behavior"),
                    )
                ),
            )
            registration, _, _, source, _, _ = self.prepare_results()
            evidence = register_evidence(
                self.workspace,
                registration.project_id,
                source_id=source.source_id,
                content_hash=source.current_content_hash,
                locator=LineRangeLocator(1, 1),
                excerpt="alpha = 1\r\n",
            ).evidence
            self.service.source_open(registration.project_id, evidence.evidence_id)
            self.service.host_context_pack(registration.project_id)

        for guard in guards:
            guard.assert_not_called()

    def test_service_dependency_closure_has_no_cli_or_host_adapter_imports(
        self,
    ) -> None:
        tools_dir = REPO_ROOT / "tools"
        pending = ["research_core"]
        visited: set[str] = set()
        imported: set[str] = set()

        while pending:
            module_name = pending.pop()
            if module_name in visited:
                continue
            visited.add(module_name)
            module_path = tools_dir / f"{module_name}.py"
            tree = ast.parse(module_path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                candidates: list[str] = []
                if isinstance(node, ast.Import):
                    candidates.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    candidates.append(node.module)
                for candidate in candidates:
                    imported.add(candidate)
                    local_name = candidate.removeprefix("tools.").split(".", 1)[0]
                    if (tools_dir / f"{local_name}.py").is_file():
                        pending.append(local_name)

        forbidden = {
            "_utils",
            "argparse",
            "project",
            "tools._utils",
            "tools.project",
            "webbrowser",
        }
        self.assertTrue(imported.isdisjoint(forbidden), imported & forbidden)
        host_markers = ("codex", "claude", "mcp", "hook")
        host_imports = {
            name
            for name in imported
            if any(marker in name.casefold() for marker in host_markers)
        }
        self.assertEqual(host_imports, set())


if __name__ == "__main__":
    unittest.main()
