from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import Mock, patch

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tools.coverage_report import COVERAGE_REPORT_FILENAME
from tools.extraction_schema import LineRangeLocator
from tools.project_registry import ProjectNotRegisteredError
from tools.research_core import ResearchCoreService
from tools.research_mcp import (
    COVERAGE_TOOL,
    HOST_CONTEXT_TOOL,
    MCP_ENVELOPE_SCHEMA_VERSION,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    PLAN_TOOL,
    PROJECT_CONTEXT_TOOL,
    QUERY_TOOL,
    RECONCILE_TOOL,
    SOURCE_OPEN_TOOL,
    ResearchMCPAdapter,
)
from tools.source_registry import load_source_registry, sync_source_registry


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = (
    PROJECT_CONTEXT_TOOL,
    HOST_CONTEXT_TOOL,
    COVERAGE_TOOL,
    SOURCE_OPEN_TOOL,
    QUERY_TOOL,
    RECONCILE_TOOL,
    PLAN_TOOL,
)


class ResearchMCPServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.knowledge = self.root / "knowledge"
        self.project = self.root / "research-project"
        (self.project / "src").mkdir(parents=True)
        self.secret = "G07_RAW_SECRET_8bb9bde8"
        self.source_path = self.project / "src" / "model.py"
        self.source_path.write_text(
            f"alpha = 1\nbeta = 2\nsecret = {self.secret!r}\n",
            encoding="utf-8",
        )
        self.sensitive_secret = "G07_ENV_SECRET_73db43e0"
        self.sensitive_path = self.project / ".env"
        self.sensitive_path.write_text(
            f"TOKEN={self.sensitive_secret}\n",
            encoding="utf-8",
        )
        self.service = ResearchCoreService(self.workspace)
        self.registration = self.service.register(
            self.project,
            project_id="g-07-fixture",
            knowledge_root=self.knowledge,
            final_goal="Verify the MCP transport boundary",
        )
        self.inventory = self.service.scan(self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)
        registry = load_source_registry(self.workspace, self.registration.project_id)
        self.source = registry.current_by_path["src/model.py"]
        self.sensitive_source = registry.current_by_path[".env"]

    @staticmethod
    def nested_keys(value: object) -> set[str]:
        keys: set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                keys.add(str(key))
                keys.update(ResearchMCPServerTests.nested_keys(child))
        elif isinstance(value, list):
            for child in value:
                keys.update(ResearchMCPServerTests.nested_keys(child))
        return keys

    @staticmethod
    def nested_strings(value: object) -> tuple[str, ...]:
        strings: list[str] = []
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                strings.extend(ResearchMCPServerTests.nested_strings(child))
        elif isinstance(value, list):
            for child in value:
                strings.extend(ResearchMCPServerTests.nested_strings(child))
        return tuple(strings)

    def assert_host_safe(self, payload: dict[str, Any]) -> None:
        forbidden_keys = {
            "absolute_path",
            "knowledge_root",
            "machine_root",
            "manifest_file",
            "origin_url",
            "project_file",
            "project_root",
            "report_file",
            "root",
            "root_path",
            "storage",
        }
        self.assertTrue(
            self.nested_keys(payload).isdisjoint(forbidden_keys),
            self.nested_keys(payload) & forbidden_keys,
        )
        absolute_roots = (
            str(self.workspace.resolve()),
            str(self.project.resolve()),
            str(self.knowledge.resolve()),
        )
        for value in self.nested_strings(payload):
            for absolute_root in absolute_roots:
                self.assertNotIn(absolute_root, value)

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

    def workspace_snapshot(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        if not self.workspace.exists():
            return snapshot
        for path in sorted(self.workspace.rglob("*")):
            if path.is_file():
                snapshot[path.relative_to(self.workspace).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return snapshot

    @asynccontextmanager
    async def mcp_session(
        self,
        *,
        direct_script: bool = False,
    ) -> AsyncIterator[ClientSession]:
        stderr_file = self.root / "mcp-server.stderr"
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        entry = (
            [str(REPO_ROOT / "tools" / "research_mcp.py")]
            if direct_script
            else ["-m", "tools.research_mcp"]
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-B",
                *entry,
                "--workspace-root",
                str(self.workspace),
            ],
            cwd=str(REPO_ROOT),
            env=environment,
        )
        with stderr_file.open("w", encoding="utf-8") as errlog:
            async with stdio_client(parameters, errlog=errlog) as streams:
                async with ClientSession(*streams) as session:
                    yield session
        self.assertEqual(stderr_file.read_text(encoding="utf-8"), "")

    @staticmethod
    def payload(result: Any) -> dict[str, Any]:
        payload = result.structuredContent
        if not isinstance(payload, dict):
            raise AssertionError(f"missing structured MCP payload: {result!r}")
        return payload

    def invoke_cli(self, argv: list[str]) -> dict[str, Any]:
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "tools.project", *argv],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    async def test_stdio_client_lists_seven_honest_tool_contracts(self) -> None:
        self.assertEqual(MCP_SERVER_VERSION, "0.3.0")
        async with self.mcp_session() as session:
            initialized = await session.initialize()
            self.assertEqual(initialized.serverInfo.name, MCP_SERVER_NAME)
            self.assertEqual(initialized.serverInfo.version, "0.3.0")
            self.assertIsNotNone(initialized.capabilities.tools)

            listed = await session.list_tools()
            self.assertEqual(len(listed.tools), 7)
            self.assertEqual(tuple(tool.name for tool in listed.tools), EXPECTED_TOOLS)
            by_name = {tool.name: tool for tool in listed.tools}
            for name in EXPECTED_TOOLS:
                tool = by_name[name]
                self.assertFalse(tool.inputSchema["additionalProperties"])
                self.assertEqual(
                    tool.outputSchema["properties"]["schema_version"]["const"],
                    MCP_ENVELOPE_SCHEMA_VERSION,
                )
            self.assertFalse(by_name[COVERAGE_TOOL].annotations.readOnlyHint)
            self.assertFalse(by_name[HOST_CONTEXT_TOOL].annotations.readOnlyHint)
            self.assertTrue(by_name[PROJECT_CONTEXT_TOOL].annotations.readOnlyHint)
            self.assertTrue(by_name[SOURCE_OPEN_TOOL].annotations.readOnlyHint)

            reconcile_tool = by_name[RECONCILE_TOOL]
            self.assertIn("H-07", reconcile_tool.description)
            self.assertIn("full scan", reconcile_tool.description)
            self.assertNotIn("capability-unavailable", reconcile_tool.description)
            self.assertFalse(reconcile_tool.annotations.readOnlyHint)
            self.assertFalse(reconcile_tool.annotations.destructiveHint)
            self.assertTrue(reconcile_tool.annotations.idempotentHint)
            self.assertFalse(reconcile_tool.annotations.openWorldHint)
            self.assertEqual(reconcile_tool.inputSchema["required"], ["project_id"])
            self.assertEqual(
                set(reconcile_tool.inputSchema["properties"]),
                {"project_id", "dirty_paths"},
            )
            dirty_paths_schema = reconcile_tool.inputSchema["properties"]["dirty_paths"]
            self.assertEqual(dirty_paths_schema["maxItems"], 256)
            self.assertTrue(dirty_paths_schema["uniqueItems"])
            path_pattern = dirty_paths_schema["items"]["pattern"]
            self.assertRegex("src/model.py", path_pattern)
            for unsafe_path in (
                "../outside.py",
                "/absolute.py",
                "C:/absolute.py",
                r"src\model.py",
            ):
                self.assertNotRegex(unsafe_path, path_pattern)

            reconcile_output = reconcile_tool.outputSchema
            self.assertEqual(
                reconcile_output["properties"]["capability"]["const"],
                "reconcile",
            )
            reconcile_schema = reconcile_output["properties"]["result"]
            self.assertFalse(reconcile_schema["additionalProperties"])
            self.assertEqual(
                set(reconcile_schema["required"]),
                {
                    "schema_version",
                    "kind",
                    "reconciliation_version",
                    "project_id",
                    "status",
                    "mode",
                    "source_of_truth",
                    "run",
                    "manifest",
                    "coverage",
                    "hints",
                    "acknowledgement",
                },
            )
            reconcile_properties = reconcile_schema["properties"]
            self.assertEqual(
                {
                    key: reconcile_properties[key]["const"]
                    for key in (
                        "schema_version",
                        "kind",
                        "reconciliation_version",
                        "status",
                        "mode",
                        "source_of_truth",
                    )
                },
                {
                    "schema_version": 1,
                    "kind": "llmwiki-project-reconciliation-result",
                    "reconciliation_version": "project-reconciliation-v1",
                    "status": "reconciled",
                    "mode": "full-scan",
                    "source_of_truth": "manifest-and-hash",
                },
            )
            run_properties = reconcile_properties["run"]["properties"]
            self.assertEqual(run_properties["status"]["const"], "paused")
            self.assertEqual(run_properties["through_stage"]["const"], "classify")
            self.assertEqual(
                run_properties["stages"]["properties"],
                {
                    "register": {"const": "succeeded"},
                    "inventory": {"const": "succeeded"},
                    "classify": {"const": "succeeded"},
                },
            )
            self.assertTrue(
                self.nested_keys(reconcile_schema).isdisjoint(
                    {
                        "absolute_path",
                        "dirty_paths",
                        "path",
                        "paths",
                        "project_root",
                        "storage",
                    }
                )
            )

            for name, milestone in (
                (QUERY_TOOL, "G-04"),
                (PLAN_TOOL, "I-04"),
            ):
                self.assertIn("capability-unavailable", by_name[name].description)
                self.assertIn(milestone, by_name[name].description)

    async def test_direct_script_entrypoint_initializes_the_same_server(self) -> None:
        async with self.mcp_session(direct_script=True) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
        self.assertEqual(initialized.serverInfo.name, MCP_SERVER_NAME)
        self.assertEqual(initialized.serverInfo.version, "0.3.0")
        self.assertEqual(tuple(tool.name for tool in listed.tools), EXPECTED_TOOLS)

    async def test_real_tools_match_core_and_cli_semantics_without_path_leaks(
        self,
    ) -> None:
        before = self.source_snapshot()
        context_direct = self.service.project_context(
            self.registration.project_id
        ).as_dict()
        context_pack_direct = self.service.host_context_pack(
            self.registration.project_id
        ).as_dict()
        coverage_direct = self.service.coverage_view(
            self.registration.project_id
        ).as_dict()
        locator = LineRangeLocator(2, 2)
        opened_direct = self.service.source_open_view(
            self.registration.project_id,
            self.source.source_id,
            locator=locator,
            expected_content_hash=self.source.current_content_hash,
        ).as_dict()

        async with self.mcp_session() as session:
            await session.initialize()
            context_call = await session.call_tool(
                PROJECT_CONTEXT_TOOL,
                {"project_id": self.registration.project_id},
            )
            context_pack_call = await session.call_tool(
                HOST_CONTEXT_TOOL,
                {"project_id": self.registration.project_id},
            )
            coverage_call = await session.call_tool(
                COVERAGE_TOOL,
                {"project_id": self.registration.project_id},
            )
            source_call = await session.call_tool(
                SOURCE_OPEN_TOOL,
                {
                    "project_id": self.registration.project_id,
                    "target_id": self.source.source_id,
                    "locator": locator.as_dict(),
                    "expected_content_hash": self.source.current_content_hash,
                },
            )

        for result, capability, expected in (
            (context_call, "project-context", context_direct),
            (context_pack_call, "host-context", context_pack_direct),
            (coverage_call, "coverage", coverage_direct),
            (source_call, "source-open", opened_direct),
        ):
            self.assertFalse(result.isError)
            payload = self.payload(result)
            self.assertEqual(
                payload,
                {
                    "schema_version": MCP_ENVELOPE_SCHEMA_VERSION,
                    "ok": True,
                    "capability": capability,
                    "result": expected,
                },
            )
            self.assertEqual(set(payload) & {"result", "error"}, {"result"})
            self.assert_host_safe(payload["result"])

        context_cli = self.invoke_cli(
            [
                "context",
                self.registration.project_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ]
        )
        context_pack_cli = self.invoke_cli(
            [
                "context-pack",
                self.registration.project_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ]
        )
        coverage_cli = self.invoke_cli(
            [
                "coverage",
                self.registration.project_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ]
        )
        source_cli = self.invoke_cli(
            [
                "source",
                "open",
                self.registration.project_id,
                self.source.source_id,
                "--locator-json",
                json.dumps(locator.as_dict()),
                "--expected-content-hash",
                self.source.current_content_hash,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ]
        )
        self.assertEqual(
            self.payload(context_call)["result"],
            {key: value for key, value in context_cli.items() if key != "ok"},
        )
        self.assertEqual(
            self.payload(context_pack_call)["result"],
            {key: value for key, value in context_pack_cli.items() if key != "ok"},
        )
        coverage_result = self.payload(coverage_call)["result"]
        self.assertEqual(coverage_result["project_id"], coverage_cli["project_id"])
        self.assertEqual(coverage_result["report"], coverage_cli["report"])
        self.assertTrue(coverage_result["report_persisted"])

        source_result = self.payload(source_call)["result"]
        for field in (
            "locator",
            "excerpt",
            "excerpt_hash",
            "excerpt_encoding",
            "excerpt_format",
            "content_hash_verified",
            "excerpt_hash_verified",
            "evidence_id",
            "evidence_source_version",
        ):
            self.assertEqual(source_result[field], source_cli[field])
        for field in (
            "project_id",
            "source_id",
            "current_path",
            "current_version",
            "content_hash",
        ):
            self.assertEqual(
                source_result["source"][field], source_cli["source"][field]
            )

        self.assertEqual(self.source_snapshot(), before)
        self.assertFalse((self.project / ".llmwiki").exists())
        self.assertFalse((self.project / "wiki").exists())

    async def test_real_mcp_reconcile_full_scan_is_path_safe_and_source_immutable(
        self,
    ) -> None:
        unhinted_path = self.project / "src" / "unhinted.py"
        unhinted_path.write_text("gamma = 3\n", encoding="utf-8")
        source_before = self.source_snapshot()
        workspace_before = self.workspace_snapshot()
        checkpoint_file = self.registration.layout.reconciliation_state_file
        self.assertFalse(checkpoint_file.exists())
        explicit_hint = f"private/{self.secret}.txt"

        async with self.mcp_session() as session:
            await session.initialize()
            result = await session.call_tool(
                RECONCILE_TOOL,
                {
                    "project_id": self.registration.project_id,
                    "dirty_paths": [explicit_hint],
                },
            )

        self.assertFalse(result.isError)
        payload = self.payload(result)
        self.assertEqual(
            set(payload) & {"result", "error"},
            {"result"},
        )
        self.assertEqual(payload["capability"], "reconcile")
        reconciliation = payload["result"]
        self.assertEqual(reconciliation["schema_version"], 1)
        self.assertEqual(
            reconciliation["kind"],
            "llmwiki-project-reconciliation-result",
        )
        self.assertEqual(
            reconciliation["reconciliation_version"],
            "project-reconciliation-v1",
        )
        self.assertEqual(reconciliation["status"], "reconciled")
        self.assertEqual(reconciliation["mode"], "full-scan")
        self.assertEqual(reconciliation["source_of_truth"], "manifest-and-hash")
        self.assertEqual(reconciliation["run"]["status"], "paused")
        self.assertEqual(reconciliation["run"]["through_stage"], "classify")
        self.assertEqual(
            reconciliation["run"]["stages"],
            {
                "register": "succeeded",
                "inventory": "succeeded",
                "classify": "succeeded",
            },
        )
        self.assertEqual(
            reconciliation["manifest"]["scan_generation"],
            self.inventory.scan_generation + 1,
        )
        self.assertEqual(reconciliation["hints"]["status"], "explicit-only")
        self.assertEqual(reconciliation["hints"]["queued_dirty_path_count"], 0)
        self.assertEqual(reconciliation["hints"]["explicit_dirty_path_count"], 1)
        self.assertEqual(reconciliation["hints"]["combined_dirty_path_count"], 1)
        self.assertEqual(
            reconciliation["acknowledgement"]["state_revision"],
            1,
        )
        self.assert_host_safe(reconciliation)

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(explicit_hint, serialized)
        self.assertNotIn(self.secret, serialized)
        self.assertNotIn("src/unhinted.py", serialized)

        manifest_rows = [
            json.loads(line)
            for line in self.registration.layout.manifest_file.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        self.assertIn(
            "src/unhinted.py",
            {row.get("path") for row in manifest_rows},
        )
        self.assertTrue(checkpoint_file.is_file())
        checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        self.assertEqual(
            checkpoint["revision"],
            reconciliation["acknowledgement"]["state_revision"],
        )
        self.assertEqual(
            checkpoint["acknowledged_through_sequence"],
            reconciliation["acknowledgement"]["current_through_sequence"],
        )
        self.assertEqual(
            checkpoint["last_success"]["manifest_scan_generation"],
            reconciliation["manifest"]["scan_generation"],
        )
        self.assertEqual(checkpoint["last_success"]["hint_status"], "explicit-only")

        self.assertNotEqual(self.workspace_snapshot(), workspace_before)
        self.assertEqual(self.source_snapshot(), source_before)
        self.assertFalse((self.project / ".llmwiki").exists())
        self.assertFalse((self.project / "wiki").exists())

    async def test_transport_errors_have_stable_codes_and_fail_closed(self) -> None:
        async with self.mcp_session() as session:
            await session.initialize()
            calls = (
                (
                    PROJECT_CONTEXT_TOOL,
                    {"project_id": "../escape"},
                    "project-id-invalid",
                ),
                (
                    PROJECT_CONTEXT_TOOL,
                    {"project_id": "not-registered"},
                    "project-not-registered",
                ),
                (
                    HOST_CONTEXT_TOOL,
                    {
                        "project_id": self.registration.project_id,
                        "max_bytes": 512,
                    },
                    "context-budget-too-small",
                ),
                (
                    SOURCE_OPEN_TOOL,
                    {
                        "project_id": self.registration.project_id,
                        "target_id": "unknown-target",
                    },
                    "source-not-registered",
                ),
                (
                    SOURCE_OPEN_TOOL,
                    {
                        "project_id": self.registration.project_id,
                        "target_id": self.source.source_id,
                        "locator": {
                            **LineRangeLocator(1, 1).as_dict(),
                            "start_line": 2,
                            "end_line": 1,
                        },
                    },
                    "source-locator-invalid",
                ),
                (
                    PROJECT_CONTEXT_TOOL,
                    {"project_id": self.registration.project_id, "extra": True},
                    "invalid-arguments",
                ),
                (
                    "llmwiki_unknown",
                    {"project_id": self.registration.project_id},
                    "mcp-tool-not-found",
                ),
            )
            for name, arguments, expected_code in calls:
                with self.subTest(name=name, expected_code=expected_code):
                    result = await session.call_tool(name, arguments)
                    payload = self.payload(result)
                    self.assertTrue(result.isError)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["schema_version"], 1)
                    self.assertEqual(payload["error"]["code"], expected_code)
                    self.assertFalse(payload["error"]["retryable"])
                    self.assertNotIn("result", payload)

    async def test_reconcile_errors_have_stable_redacted_codes(self) -> None:
        checkpoint_file = self.registration.layout.reconciliation_state_file
        checkpoint_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "llmwiki-reconciliation-state",
                    "reconciliation_version": "project-reconciliation-v1",
                    "project_id": self.registration.project_id,
                    "revision": 0,
                    "acknowledged_through_sequence": 0,
                    "acknowledged_ledger_sha256": hashlib.sha256(b"").hexdigest(),
                    "last_success": None,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        checkpoint_before = checkpoint_file.read_bytes()
        source_before = self.source_snapshot()
        workspace_before = self.workspace_snapshot()
        invalid_hint = f".llmwiki/private/{self.secret}.json"
        calls = (
            (
                {"project_id": "valid-but-absent"},
                "project-not-registered",
            ),
            (
                {
                    "project_id": self.registration.project_id,
                    "dirty_paths": [invalid_hint],
                },
                "reconciliation-hint-invalid",
            ),
            (
                {"project_id": self.registration.project_id},
                "reconciliation-state-invalid",
            ),
        )

        async with self.mcp_session() as session:
            await session.initialize()
            for arguments, expected_code in calls:
                with self.subTest(expected_code=expected_code):
                    result = await session.call_tool(RECONCILE_TOOL, arguments)
                    payload = self.payload(result)
                    self.assertTrue(result.isError)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["capability"], "reconcile")
                    self.assertEqual(payload["error"]["code"], expected_code)
                    self.assertFalse(payload["error"]["retryable"])
                    self.assertEqual(
                        set(payload) & {"result", "error"},
                        {"error"},
                    )
                    self.assertNotIn(
                        self.secret,
                        json.dumps(payload, ensure_ascii=False),
                    )

        self.assertEqual(checkpoint_file.read_bytes(), checkpoint_before)
        self.assertEqual(self.workspace_snapshot(), workspace_before)
        self.assertEqual(self.source_snapshot(), source_before)

    async def test_published_json_schemas_reject_malformed_arguments(self) -> None:
        invalid_calls = (
            (HOST_CONTEXT_TOOL, {"project_id": "p", "max_bytes": True}),
            (HOST_CONTEXT_TOOL, {"project_id": "p", "max_bytes": 511}),
            (QUERY_TOOL, {"project_id": "p", "question": "q", "mode": []}),
            (
                RECONCILE_TOOL,
                {"project_id": "p", "dirty_paths": ["a.py", "a.py"]},
            ),
            (
                RECONCILE_TOOL,
                {"project_id": "p", "dirty_paths": ["../outside.py"]},
            ),
            (
                RECONCILE_TOOL,
                {"project_id": "p", "dirty_paths": ["C:/outside.py"]},
            ),
            (
                RECONCILE_TOOL,
                {"project_id": "p", "dirty_paths": [r"src\model.py"]},
            ),
            (
                SOURCE_OPEN_TOOL,
                {
                    "project_id": self.registration.project_id,
                    "target_id": self.source.source_id,
                    "locator": {},
                },
            ),
            (
                SOURCE_OPEN_TOOL,
                {
                    "project_id": self.registration.project_id,
                    "target_id": self.source.source_id,
                    "locator": LineRangeLocator(1, 1).as_dict(),
                    "expected_content_hash": "A" * 64,
                },
            ),
            (
                SOURCE_OPEN_TOOL,
                {
                    "project_id": self.registration.project_id,
                    "target_id": self.source.source_id,
                    "locator": LineRangeLocator(1, 1).as_dict(),
                    "expected_excerpt_hash": "0" * 63,
                },
            ),
            (PROJECT_CONTEXT_TOOL, {}),
            (
                PROJECT_CONTEXT_TOOL,
                {"project_id": self.registration.project_id, "extra": True},
            ),
        )

        async with self.mcp_session() as session:
            await session.initialize()
            for name, arguments in invalid_calls:
                with self.subTest(name=name, arguments=arguments):
                    result = await session.call_tool(name, arguments)
                    payload = self.payload(result)
                    self.assertTrue(result.isError)
                    self.assertEqual(payload["error"]["code"], "invalid-arguments")
                    self.assertEqual(set(payload) & {"result", "error"}, {"error"})

    def test_malformed_core_output_fails_closed_to_valid_internal_error(self) -> None:
        adapter = ResearchMCPAdapter(self.workspace)
        malformed = Mock()
        malformed.as_dict.return_value = {
            **self.service.project_context(self.registration.project_id).as_dict(),
            "absolute_path": str(self.project),
            "secret": self.sensitive_secret,
        }

        with patch.object(
            ResearchCoreService,
            "project_context",
            return_value=malformed,
        ):
            result = adapter.call_tool(
                PROJECT_CONTEXT_TOOL,
                {"project_id": self.registration.project_id},
            )

        payload = self.payload(result)
        self.assertTrue(result.isError)
        self.assertEqual(payload["error"]["code"], "internal-error")
        self.assertEqual(set(payload) & {"result", "error"}, {"error"})
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.project), serialized)
        self.assertNotIn(self.sensitive_secret, serialized)

    def test_nested_host_output_injection_fails_closed(self) -> None:
        adapter = ResearchMCPAdapter(self.workspace)
        coverage_payload = self.service.coverage_view(
            self.registration.project_id
        ).as_dict()
        coverage_payload["report"]["manifest"]["manifest_file"] = str(
            self.project / "secret-manifest.jsonl"
        )
        source_payload = self.service.source_open_view(
            self.registration.project_id,
            self.source.source_id,
            locator=LineRangeLocator(1, 1),
        ).as_dict()
        source_payload["locator"]["absolute_path"] = str(self.project / "source.py")

        cases = (
            (COVERAGE_TOOL, "coverage_view", coverage_payload),
            (SOURCE_OPEN_TOOL, "source_open_view", source_payload),
        )
        for tool_name, method_name, malformed_payload in cases:
            malformed = Mock()
            malformed.as_dict.return_value = malformed_payload
            with (
                self.subTest(tool=tool_name),
                patch.object(
                    ResearchCoreService,
                    method_name,
                    return_value=malformed,
                ),
            ):
                arguments = {"project_id": self.registration.project_id}
                if tool_name == SOURCE_OPEN_TOOL:
                    arguments.update(
                        {
                            "target_id": self.source.source_id,
                            "locator": LineRangeLocator(1, 1).as_dict(),
                        }
                    )
                result = adapter.call_tool(tool_name, arguments)
                payload = self.payload(result)
                self.assertTrue(result.isError)
                self.assertEqual(payload["error"]["code"], "internal-error")
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(str(self.project), serialized)

    def test_typed_project_not_registered_error_maps_through_wrappers(self) -> None:
        adapter = ResearchMCPAdapter(self.workspace)
        cause = ProjectNotRegisteredError("wording intentionally unrelated to mapping")
        wrapped = RuntimeError("opaque wrapper")
        wrapped.__cause__ = cause

        with patch.object(
            ResearchCoreService,
            "project_context",
            side_effect=wrapped,
        ):
            result = adapter.call_tool(
                PROJECT_CONTEXT_TOOL,
                {"project_id": "valid-but-absent"},
            )

        payload = self.payload(result)
        self.assertTrue(result.isError)
        self.assertEqual(payload["error"]["code"], "project-not-registered")
        self.assertNotIn("wording intentionally", json.dumps(payload))

    def test_unregistered_source_open_preserves_typed_error_mapping(self) -> None:
        adapter = ResearchMCPAdapter(self.workspace)
        result = adapter.call_tool(
            SOURCE_OPEN_TOOL,
            {
                "project_id": "valid-but-absent",
                "target_id": "src-" + "0" * 32,
                "locator": LineRangeLocator(1, 1).as_dict(),
            },
        )
        payload = self.payload(result)
        self.assertTrue(result.isError)
        self.assertEqual(payload["error"]["code"], "project-not-registered")

    async def test_coverage_persists_machine_report_without_source_writes(self) -> None:
        report_file = self.registration.layout.indexes_dir / COVERAGE_REPORT_FILENAME
        self.assertFalse(report_file.exists())
        before = self.source_snapshot()

        async with self.mcp_session() as session:
            await session.initialize()
            listed = await session.list_tools()
            coverage_tool = next(
                tool for tool in listed.tools if tool.name == COVERAGE_TOOL
            )
            result = await session.call_tool(
                COVERAGE_TOOL,
                {"project_id": self.registration.project_id},
            )

        self.assertFalse(result.isError)
        self.assertFalse(coverage_tool.annotations.readOnlyHint)
        self.assertTrue(report_file.is_file())
        persisted = json.loads(report_file.read_text(encoding="utf-8"))
        self.assertEqual(self.payload(result)["result"]["report"], persisted)
        self.assertEqual(self.source_snapshot(), before)

    async def test_manifest_policy_denies_sensitive_source_but_allows_local_only(
        self,
    ) -> None:
        self.assertEqual(
            self.inventory.policy["config"]["external_send_mode"],
            "local-only",
        )
        before = self.source_snapshot()

        async with self.mcp_session() as session:
            await session.initialize()
            denied = await session.call_tool(
                SOURCE_OPEN_TOOL,
                {
                    "project_id": self.registration.project_id,
                    "target_id": self.sensitive_source.source_id,
                    "locator": LineRangeLocator(1, 1).as_dict(),
                    "expected_content_hash": (
                        self.sensitive_source.current_content_hash
                    ),
                },
            )
            ordinary = await session.call_tool(
                SOURCE_OPEN_TOOL,
                {
                    "project_id": self.registration.project_id,
                    "target_id": self.source.source_id,
                    "locator": LineRangeLocator(1, 1).as_dict(),
                    "expected_content_hash": self.source.current_content_hash,
                },
            )

        denied_payload = self.payload(denied)
        self.assertTrue(denied.isError)
        self.assertEqual(
            denied_payload["error"]["code"],
            "source-content-policy-denied",
        )
        self.assertNotIn(
            self.sensitive_secret,
            json.dumps(denied_payload, ensure_ascii=False),
        )
        self.assertFalse(ordinary.isError)
        self.assertEqual(self.payload(ordinary)["result"]["excerpt"], "alpha = 1\r\n")
        self.assertEqual(self.source_snapshot(), before)

    async def test_future_project_schema_maps_to_unsupported_and_is_not_read(
        self,
    ) -> None:
        project_file = self.registration.project_file
        record = json.loads(project_file.read_text(encoding="utf-8"))
        record["schema_version"] = 999
        project_file.write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        async with self.mcp_session() as session:
            await session.initialize()
            result = await session.call_tool(
                PROJECT_CONTEXT_TOOL,
                {"project_id": self.registration.project_id},
            )
        self.assertTrue(result.isError)
        self.assertEqual(
            self.payload(result)["error"]["code"],
            "schema-version-unsupported",
        )

    async def test_future_manifest_schema_blocks_source_open_before_content_read(
        self,
    ) -> None:
        manifest_file = self.registration.layout.manifest_file
        records = [
            json.loads(line)
            for line in manifest_file.read_text(encoding="utf-8").splitlines()
        ]
        records[0]["schema_version"] = 999
        manifest_file.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )

        async with self.mcp_session() as session:
            await session.initialize()
            result = await session.call_tool(
                SOURCE_OPEN_TOOL,
                {
                    "project_id": self.registration.project_id,
                    "target_id": self.source.source_id,
                    "locator": LineRangeLocator(3, 3).as_dict(),
                    "expected_content_hash": self.source.current_content_hash,
                },
            )

        payload = self.payload(result)
        self.assertTrue(result.isError)
        self.assertEqual(payload["error"]["code"], "schema-version-unsupported")
        self.assertNotIn(self.secret, json.dumps(payload, ensure_ascii=False))

    async def test_query_and_plan_are_unavailable_without_echoing_inputs(self) -> None:
        before_source = self.source_snapshot()
        before_workspace = self.workspace_snapshot()
        sensitive_inputs = (
            (
                QUERY_TOOL,
                {
                    "project_id": self.registration.project_id,
                    "question": f"reveal {self.secret}",
                    "mode": "verified",
                },
                "G-04",
            ),
            (
                PLAN_TOOL,
                {
                    "project_id": self.registration.project_id,
                    "objective": f"private objective {self.secret}",
                },
                "I-04",
            ),
        )

        async with self.mcp_session() as session:
            await session.initialize()
            for name, arguments, milestone in sensitive_inputs:
                result = await session.call_tool(name, arguments)
                payload = self.payload(result)
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertTrue(result.isError)
                self.assertEqual(payload["error"]["code"], "capability-unavailable")
                self.assertEqual(
                    payload["error"]["details"],
                    {"available_after": milestone, "status": "not-implemented"},
                )
                self.assertNotIn(self.secret, serialized)

        self.assertEqual(self.source_snapshot(), before_source)
        self.assertEqual(self.workspace_snapshot(), before_workspace)

    async def test_raw_content_appears_only_after_explicit_source_open(self) -> None:
        async with self.mcp_session() as session:
            await session.initialize()
            ordinary_results = (
                await session.call_tool(
                    PROJECT_CONTEXT_TOOL,
                    {"project_id": self.registration.project_id},
                ),
                await session.call_tool(
                    HOST_CONTEXT_TOOL,
                    {"project_id": self.registration.project_id},
                ),
                await session.call_tool(
                    COVERAGE_TOOL,
                    {"project_id": self.registration.project_id},
                ),
                await session.call_tool(
                    QUERY_TOOL,
                    {
                        "project_id": self.registration.project_id,
                        "question": "What is the secret?",
                    },
                ),
            )
            for result in ordinary_results:
                self.assertNotIn(
                    self.secret,
                    json.dumps(self.payload(result), ensure_ascii=False),
                )

            explicit = await session.call_tool(
                SOURCE_OPEN_TOOL,
                {
                    "project_id": self.registration.project_id,
                    "target_id": self.source.source_id,
                    "locator": LineRangeLocator(3, 3).as_dict(),
                    "expected_content_hash": self.source.current_content_hash,
                },
            )
        self.assertFalse(explicit.isError)
        self.assertIn(self.secret, self.payload(explicit)["result"]["excerpt"])

    def test_adapter_delegates_real_operations_without_filesystem_workflows(
        self,
    ) -> None:
        adapter = ResearchMCPAdapter(self.workspace)
        context = Mock()
        context.as_dict.return_value = self.service.project_context(
            self.registration.project_id
        ).as_dict()
        host_context = Mock()
        host_context.as_dict.return_value = self.service.host_context_pack(
            self.registration.project_id
        ).as_dict()
        coverage = Mock()
        coverage.as_dict.return_value = self.service.coverage_view(
            self.registration.project_id
        ).as_dict()
        opened = Mock()
        opened.as_dict.return_value = self.service.source_open_view(
            self.registration.project_id,
            self.source.source_id,
            locator=LineRangeLocator(1, 1),
        ).as_dict()
        reconciled = Mock()
        reconciled.as_dict.return_value = self.service.project_reconcile(
            self.registration.project_id,
            dirty_paths=("src/model.py",),
        ).as_dict()
        locator = LineRangeLocator(1, 1).as_dict()

        with (
            patch.object(
                ResearchCoreService,
                "project_context",
                return_value=context,
            ) as project_context,
            patch.object(
                ResearchCoreService,
                "host_context_pack",
                return_value=host_context,
            ) as host_context_call,
            patch.object(
                ResearchCoreService,
                "coverage_view",
                return_value=coverage,
            ) as coverage_call,
            patch.object(
                ResearchCoreService,
                "source_open_view",
                return_value=opened,
            ) as source_open,
            patch.object(
                ResearchCoreService,
                "project_reconcile",
                return_value=reconciled,
            ) as project_reconcile,
        ):
            context_result = adapter.call_tool(
                PROJECT_CONTEXT_TOOL, {"project_id": "p"}
            )
            host_context_result = adapter.call_tool(
                HOST_CONTEXT_TOOL,
                {"project_id": "p", "max_bytes": 4096},
            )
            coverage_result = adapter.call_tool(COVERAGE_TOOL, {"project_id": "p"})
            source_result = adapter.call_tool(
                SOURCE_OPEN_TOOL,
                {
                    "project_id": "p",
                    "target_id": "src-" + "0" * 32,
                    "locator": locator,
                },
            )
            reconcile_result = adapter.call_tool(
                RECONCILE_TOOL,
                {
                    "project_id": "p",
                    "dirty_paths": ["src/model.py"],
                },
            )

        self.assertFalse(context_result.isError)
        self.assertFalse(host_context_result.isError)
        self.assertFalse(coverage_result.isError)
        self.assertFalse(source_result.isError)
        self.assertFalse(reconcile_result.isError)
        project_context.assert_called_once_with("p")
        host_context_call.assert_called_once_with("p", max_bytes=4096)
        coverage_call.assert_called_once_with("p")
        source_open.assert_called_once_with(
            "p",
            "src-" + "0" * 32,
            locator=locator,
            expected_content_hash=None,
            expected_excerpt_hash=None,
        )
        project_reconcile.assert_called_once_with(
            "p",
            dirty_paths=("src/model.py",),
        )

        tree = ast.parse(
            (REPO_ROOT / "tools" / "research_mcp.py").read_text(encoding="utf-8-sig")
        )
        forbidden_calls = {
            "open",
            "read_bytes",
            "read_text",
            "write_bytes",
            "write_text",
            "mkdir",
            "replace",
            "unlink",
            "walk",
            "scandir",
        }
        observed: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                observed.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                observed.add(node.func.attr)
        self.assertTrue(
            observed.isdisjoint(forbidden_calls), observed & forbidden_calls
        )


if __name__ == "__main__":
    unittest.main()
