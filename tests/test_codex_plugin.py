from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tools.extraction_schema import LineRangeLocator
from tools.research_mcp import (
    COVERAGE_TOOL,
    HOST_CONTEXT_TOOL,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    PLAN_TOOL,
    PROJECT_CONTEXT_TOOL,
    QUERY_TOOL,
    RECONCILE_TOOL,
    SOURCE_OPEN_TOOL,
)
from tools.project_registry import load_registered_project
from tools.research_tasks import load_tasks
from tools.source_registry import load_source_registry


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "llmwiki-research"
EXPECTED_TOOLS = (
    PROJECT_CONTEXT_TOOL,
    HOST_CONTEXT_TOOL,
    COVERAGE_TOOL,
    SOURCE_OPEN_TOOL,
    QUERY_TOOL,
    RECONCILE_TOOL,
    PLAN_TOOL,
)
EXPECTED_PLUGIN_FILES = {
    ".codex-plugin/plugin.json",
    ".mcp.json",
    "README.md",
    "hooks/hooks.json",
    "release/RELEASE_NOTES.md",
    "release/core-files.txt",
    "release/install-common.ps1",
    "release/install.ps1",
    "release/python-runtime-win-amd64.json",
    "release/requirements-win-amd64.txt",
    "release/rollback.ps1",
    "release/uninstall.ps1",
    "scripts/_bootstrap.py",
    "scripts/host_event.py",
    "scripts/launch-mcp.cmd",
    "scripts/launch_mcp.py",
    "scripts/llmwiki.cmd",
    "scripts/llmwiki.py",
    "scripts/research-cockpit.cmd",
    "scripts/research_cockpit.py",
    "skills/llmwiki-research/SKILL.md",
}


def _file_snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _source_snapshot(root: Path) -> dict[str, tuple[str, int, int, int]]:
    snapshot: dict[str, tuple[str, int, int, int]] = {}
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        base = Path(directory)
        for filename in filenames:
            path = base / filename
            stat = path.stat()
            snapshot[path.relative_to(root).as_posix()] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_mode,
            )
    return snapshot


class CodexPluginStructureTests(unittest.TestCase):
    def test_package_structure_is_valid_portable_and_honest(self) -> None:
        observed = {
            path.relative_to(PLUGIN_ROOT).as_posix()
            for path in PLUGIN_ROOT.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertEqual(observed, EXPECTED_PLUGIN_FILES)

        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["name"], "llmwiki-research")
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertNotIn("hooks", manifest)
        self.assertIn(
            "Query and C-07 remain unavailable",
            manifest["interface"]["longDescription"],
        )

        mcp_config = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(set(mcp_config), {"llmwiki-research-core"})
        server = mcp_config["llmwiki-research-core"]
        self.assertEqual(server["cwd"], ".")
        self.assertEqual(server["command"], "runtime/python/python.exe")
        self.assertEqual(server["args"], ["-I", "-B", "scripts/launch_mcp.py"])
        self.assertFalse(Path(server["cwd"]).is_absolute())
        for argument in server["args"]:
            self.assertFalse(Path(argument).is_absolute(), argument)

        hooks = json.loads(
            (PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(hooks), {"description", "hooks"})
        self.assertEqual(set(hooks["hooks"]), {"PostToolUse"})
        groups = hooks["hooks"]["PostToolUse"]
        self.assertEqual(len(groups), 1)
        handlers = groups[0]["hooks"]
        self.assertEqual(len(handlers), 1)
        handler = handlers[0]
        self.assertEqual(handler["type"], "command")
        self.assertIn("${PLUGIN_ROOT}/runtime/python/python.exe", handler["command"])
        self.assertIn("${PLUGIN_ROOT}/scripts/host_event.py", handler["command"])
        self.assertTrue(handler.get("async", False))

        skill = (PLUGIN_ROOT / "skills" / "llmwiki-research" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertTrue(skill.startswith("---\n"))
        self.assertIn("name: llmwiki-research", skill)
        self.assertIn("llmwiki_reconcile", skill)
        self.assertIn("capability-unavailable", skill)
        self.assertIn("Hooks", skill)

        forbidden_patterns = (
            re.compile(r"\bTODO\b", re.IGNORECASE),
            re.compile(r"[A-Za-z]:[\\/](?:Users|GitHub)[\\/]", re.IGNORECASE),
            re.compile(re.escape(str(REPO_ROOT)), re.IGNORECASE),
        )
        for relative_path in sorted(observed):
            path = PLUGIN_ROOT / relative_path
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                self.assertIsNone(
                    pattern.search(text), f"{relative_path}: {pattern.pattern}"
                )

        hook_script = (PLUGIN_ROOT / "scripts" / "host_event.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "project_reconcile",
            "project_understand",
            ".scan(",
            "coverage_view",
            "wiki/projects",
            "reconciliation-state.json",
        ):
            self.assertNotIn(forbidden, hook_script)

    def test_repo_local_launchers_find_core_without_an_absolute_checkout_path(
        self,
    ) -> None:
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment.pop("LLMWIKI_CORE_ROOT", None)
        environment.pop("LLMWIKI_WORKSPACE_ROOT", None)
        completed = subprocess.run(
            [sys.executable, "-B", "scripts/llmwiki.py", "--help"],
            cwd=PLUGIN_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Manage external research projects", completed.stdout)
        self.assertEqual(completed.stderr, "")

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
    def test_codex_cli_installs_and_discovers_package_from_clean_marketplace(
        self,
    ) -> None:
        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("Codex CLI is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marketplace = root / "marketplace"
            marketplace_file = marketplace / ".agents" / "plugins" / "marketplace.json"
            marketplace_file.parent.mkdir(parents=True)
            installed_source = marketplace / "plugins" / "llmwiki-research"
            installed_source.parent.mkdir(parents=True)
            shutil.copytree(PLUGIN_ROOT, installed_source)
            marketplace_file.write_text(
                json.dumps(
                    {
                        "name": "j05-test",
                        "interface": {"displayName": "J-05 Test"},
                        "plugins": [
                            {
                                "name": "llmwiki-research",
                                "source": {
                                    "source": "local",
                                    "path": "./plugins/llmwiki-research",
                                },
                                "policy": {
                                    "installation": "AVAILABLE",
                                    "authentication": "ON_INSTALL",
                                },
                                "category": "Productivity",
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            codex_home = root / "codex-home"
            codex_home.mkdir()
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(codex_home)

            added = subprocess.run(
                [codex, "plugin", "marketplace", "add", str(marketplace), "--json"],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertEqual(json.loads(added.stdout)["marketplaceName"], "j05-test")

            available = subprocess.run(
                [codex, "plugin", "list", "--available", "--json"],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(available.returncode, 0, available.stderr)
            available_payload = json.loads(available.stdout)
            self.assertEqual(
                [item["pluginId"] for item in available_payload["available"]],
                ["llmwiki-research@j05-test"],
            )

            added_plugin = subprocess.run(
                [codex, "plugin", "add", "llmwiki-research@j05-test", "--json"],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(added_plugin.returncode, 0, added_plugin.stderr)
            installation = json.loads(added_plugin.stdout)
            self.assertEqual(installation["pluginId"], "llmwiki-research@j05-test")
            installed_path = Path(installation["installedPath"])
            self.assertTrue(
                (installed_path / ".codex-plugin" / "plugin.json").is_file()
            )
            self.assertTrue(
                (installed_path / "skills" / "llmwiki-research" / "SKILL.md").is_file()
            )
            self.assertTrue((installed_path / ".mcp.json").is_file())
            self.assertTrue((installed_path / "hooks" / "hooks.json").is_file())

            listed = subprocess.run(
                [codex, "plugin", "list", "--json"],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            installed = json.loads(listed.stdout)["installed"]
            self.assertEqual(len(installed), 1)
            self.assertEqual(installed[0]["pluginId"], "llmwiki-research@j05-test")
            self.assertTrue(installed[0]["enabled"])


class CodexPluginBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.portable_plugin = self.root / "installed-plugin" / "llmwiki-research"
        self.portable_plugin.parent.mkdir(parents=True)
        shutil.copytree(PLUGIN_ROOT, self.portable_plugin)
        self.workspace = self.root / "assistant-workspace"
        self.knowledge = self.root / "knowledge"
        self.project = self.root / "research-project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "README.md").write_text(
            "# Fixture\n\nSee `src/model.py`.\n", encoding="utf-8"
        )
        self.source_path = self.project / "src" / "model.py"
        self.source_path.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PYTHONUTF8": "1",
                "LLMWIKI_CORE_ROOT": str(REPO_ROOT),
                "LLMWIKI_WORKSPACE_ROOT": str(self.workspace),
            }
        )

    def run_plugin_cli(self, *arguments: str) -> tuple[int, str, str]:
        completed = subprocess.run(
            [sys.executable, "-B", "scripts/llmwiki.py", *arguments],
            cwd=self.portable_plugin,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.returncode, completed.stdout, completed.stderr

    def register(self, project_id: str = "j-05-fixture") -> dict[str, Any]:
        returncode, stdout, stderr = self.run_plugin_cli(
            "register",
            str(self.project),
            "--project-id",
            project_id,
            "--knowledge-root",
            str(self.knowledge),
            "--json",
        )
        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["project_id"], project_id)
        return payload

    @asynccontextmanager
    async def mcp_session(
        self,
        *,
        plugin_root: Path | None = None,
    ) -> AsyncIterator[ClientSession]:
        selected = plugin_root or self.portable_plugin
        stderr_file = self.root / f"mcp-{hash(selected)}.stderr"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-B", "scripts/launch_mcp.py"],
            cwd=str(selected),
            env=self.environment,
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

    async def test_clean_fixture_registers_and_delegates_real_core_operations(
        self,
    ) -> None:
        registration = self.register()
        project_id = registration["project_id"]
        source_before = _source_snapshot(self.project)
        curated_root = self.knowledge / project_id
        curated_before = _file_snapshot(curated_root)

        async with self.mcp_session() as session:
            initialized = await session.initialize()
            self.assertEqual(initialized.serverInfo.name, MCP_SERVER_NAME)
            self.assertEqual(initialized.serverInfo.version, MCP_SERVER_VERSION)

            listed = await session.list_tools()
            self.assertEqual(tuple(tool.name for tool in listed.tools), EXPECTED_TOOLS)

            context = await session.call_tool(
                PROJECT_CONTEXT_TOOL,
                {"project_id": project_id},
            )
            self.assertFalse(context.isError)
            self.assertEqual(self.payload(context)["result"]["project_id"], project_id)

            reconciled = await session.call_tool(
                RECONCILE_TOOL,
                {"project_id": project_id},
            )
            self.assertFalse(reconciled.isError)
            reconcile_payload = self.payload(reconciled)["result"]
            self.assertEqual(reconcile_payload["project_id"], project_id)
            self.assertEqual(reconcile_payload["run"]["through_stage"], "classify")
            self.assertEqual(reconcile_payload["coverage"]["failed_file_count"], 0)

            coverage = await session.call_tool(
                COVERAGE_TOOL, {"project_id": project_id}
            )
            self.assertFalse(coverage.isError)
            self.assertEqual(self.payload(coverage)["result"]["project_id"], project_id)

            host_context = await session.call_tool(
                HOST_CONTEXT_TOOL,
                {"project_id": project_id, "max_bytes": 4096},
            )
            self.assertFalse(host_context.isError)
            self.assertEqual(
                self.payload(host_context)["result"]["project_id"], project_id
            )

            query = await session.call_tool(
                QUERY_TOOL,
                {"project_id": project_id, "question": "What changed?"},
            )
            self.assertTrue(query.isError)
            query_error = self.payload(query)["error"]
            self.assertEqual(query_error["code"], "capability-unavailable")
            self.assertEqual(query_error["details"]["available_after"], "G-04")

            plan = await session.call_tool(
                PLAN_TOOL,
                {"project_id": project_id, "objective": "Finish the study"},
            )
            self.assertFalse(plan.isError)
            plan_result = self.payload(plan)["result"]
            self.assertEqual(plan_result["project_id"], project_id)
            self.assertEqual(plan_result["goal"]["status"], "draft")
            self.assertEqual(plan_result["plan"]["status"], "draft")
            self.assertTrue(plan_result["tasks"]["task_ids"])
            self.assertEqual(
                plan_result["tasks"]["statuses"],
                ["draft"] * len(plan_result["tasks"]["task_ids"]),
            )

        tasks = load_tasks(self.workspace, project_id)
        self.assertTrue(tasks.tasks)
        self.assertTrue(all(not task.executable for task in tasks.tasks))
        registration = load_registered_project(self.workspace, project_id)
        for relative_path in (
            "indexes/goals.json",
            "indexes/tasks.json",
            "indexes/project-state.json",
            "indexes/initial-plan.json",
        ):
            self.assertTrue(
                (registration.layout.machine_root / relative_path).is_file(),
                relative_path,
            )
        self.assertEqual(_source_snapshot(self.project), source_before)
        self.assertEqual(_file_snapshot(curated_root), curated_before)

        returncode, stdout, stderr = self.run_plugin_cli(
            "source", "sync", project_id, "--json"
        )
        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertTrue(json.loads(stdout)["ok"])
        source = load_source_registry(self.workspace, project_id).current_by_path[
            "src/model.py"
        ]
        locator = LineRangeLocator(1, 1).as_dict()
        async with self.mcp_session() as session:
            await session.initialize()
            opened = await session.call_tool(
                SOURCE_OPEN_TOOL,
                {
                    "project_id": project_id,
                    "target_id": source.source_id,
                    "locator": locator,
                },
            )
            self.assertFalse(opened.isError)
            self.assertEqual(
                self.payload(opened)["result"]["excerpt"],
                self.source_path.read_bytes()
                .splitlines(keepends=True)[0]
                .decode("utf-8"),
            )

    async def test_explicit_reconcile_survives_absent_or_malformed_hooks(self) -> None:
        project_id = self.register()["project_id"]
        variants: list[Path] = []
        for name in ("absent", "malformed"):
            variant = self.root / name / "llmwiki-research"
            variant.parent.mkdir(parents=True)
            shutil.copytree(self.portable_plugin, variant)
            hook_file = variant / "hooks" / "hooks.json"
            if name == "absent":
                hook_file.unlink()
            else:
                hook_file.write_text("{not-json", encoding="utf-8")
            variants.append(variant)

        for variant in variants:
            async with self.mcp_session(plugin_root=variant) as session:
                await session.initialize()
                reconciled = await session.call_tool(
                    RECONCILE_TOOL,
                    {"project_id": project_id},
                )
                self.assertFalse(reconciled.isError, str(reconciled))
                self.assertEqual(
                    self.payload(reconciled)["result"]["coverage"]["failed_file_count"],
                    0,
                )

    def test_optional_hook_only_submits_h04_events_and_fails_open(self) -> None:
        project_id = self.register()["project_id"]
        machine_root = self.workspace / ".llmwiki" / "projects" / project_id
        curated_root = self.knowledge / project_id
        source_before = _source_snapshot(self.project)
        curated_before = _file_snapshot(curated_root)
        registration_before = (machine_root / "project.yaml").read_bytes()

        hook_environment = self.environment.copy()
        hook_environment["LLMWIKI_PROJECT_ID"] = project_id
        payload = {
            "session_id": "session-j05",
            "turn_id": "turn-j05",
            "cwd": str(self.project),
            "hook_event_name": "PostToolUse",
            "tool_name": "write_file",
            "tool_input": {"file_path": str(self.source_path)},
            "tool_response": {"ok": True},
            "tool_use_id": "tool-j05",
        }
        completed = subprocess.run(
            [sys.executable, "-B", "scripts/host_event.py"],
            cwd=self.portable_plugin,
            env=hook_environment,
            input=json.dumps(payload),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

        ledger = machine_root / "events.jsonl"
        queue = machine_root / "indexes" / "dirty-paths.json"
        self.assertTrue(ledger.is_file())
        self.assertTrue(queue.is_file())
        event = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(event["producer"], "codex-plugin")
        self.assertEqual(event["project_id"], project_id)
        self.assertEqual(event["paths"], ["src/model.py"])

        nested_payload = dict(payload)
        nested_payload["cwd"] = str(self.project / "src")
        nested_payload["tool_input"] = {"path": "model.py"}
        nested_payload["tool_use_id"] = "tool-j05-nested"
        nested = subprocess.run(
            [sys.executable, "-B", "scripts/host_event.py"],
            cwd=self.portable_plugin,
            env=hook_environment,
            input=json.dumps(nested_payload),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(nested.returncode, 0)
        self.assertEqual(nested.stdout, "")
        self.assertEqual(nested.stderr, "")
        nested_events = [
            json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(nested_events), 2)
        self.assertEqual(nested_events[1]["paths"], ["src/model.py"])

        outside = self.root / "outside-project"
        outside.mkdir()
        outside_payload = dict(payload)
        outside_payload["cwd"] = str(outside)
        outside_payload["tool_input"] = {"path": "src/model.py"}
        outside_payload["tool_use_id"] = "tool-j05-outside"
        outside_result = subprocess.run(
            [sys.executable, "-B", "scripts/host_event.py"],
            cwd=self.portable_plugin,
            env=hook_environment,
            input=json.dumps(outside_payload),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(outside_result.returncode, 0)
        self.assertEqual(outside_result.stdout, "")
        self.assertEqual(outside_result.stderr, "")
        self.assertEqual(
            len(ledger.read_text(encoding="utf-8").splitlines()),
            2,
        )

        self.assertEqual(_source_snapshot(self.project), source_before)
        self.assertEqual(_file_snapshot(curated_root), curated_before)
        self.assertEqual(
            (machine_root / "project.yaml").read_bytes(), registration_before
        )
        for forbidden in (
            "manifest.jsonl",
            "sources.jsonl",
            "evidence.jsonl",
            "indexes/coverage-report.json",
            "indexes/reconciliation-state.json",
        ):
            self.assertFalse((machine_root / forbidden).exists(), forbidden)
        self.assertTrue((machine_root / "runs").is_dir())
        self.assertEqual(list((machine_root / "runs").iterdir()), [])

        before_lines = ledger.read_text(encoding="utf-8").splitlines()
        malformed = subprocess.run(
            [sys.executable, "-B", "scripts/host_event.py"],
            cwd=self.portable_plugin,
            env=hook_environment,
            input="{not-json",
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(malformed.returncode, 0)
        self.assertEqual(malformed.stdout, "")
        self.assertEqual(malformed.stderr, "")

        disabled_environment = self.environment.copy()
        unavailable = subprocess.run(
            [sys.executable, "-B", "scripts/host_event.py"],
            cwd=self.portable_plugin,
            env=disabled_environment,
            input=json.dumps(payload),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(unavailable.returncode, 0)
        self.assertEqual(unavailable.stdout, "")
        self.assertEqual(unavailable.stderr, "")
        self.assertEqual(ledger.read_text(encoding="utf-8").splitlines(), before_lines)

    def test_copied_plugin_fails_closed_when_core_cannot_be_located(self) -> None:
        environment = self.environment.copy()
        environment.pop("LLMWIKI_CORE_ROOT", None)
        environment.pop("LLMWIKI_WORKSPACE_ROOT", None)
        isolated = self.root / "isolated"
        isolated.mkdir()
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.portable_plugin / "scripts" / "llmwiki.py"),
                "--help",
            ],
            cwd=isolated,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertIn("reinstall the Plugin package", completed.stderr)
        self.assertNotIn("LLMWIKI_CORE_ROOT", completed.stderr)
        self.assertNotIn(str(REPO_ROOT), completed.stderr)


if __name__ == "__main__":
    unittest.main()
