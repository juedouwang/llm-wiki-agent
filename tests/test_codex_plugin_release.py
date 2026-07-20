from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from jsonschema.validators import validator_for
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tools.build_codex_plugin_release import (
    BASENAME,
    VERSION,
    default_cache_dir,
    prune_target_console_launchers,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = REPO_ROOT / "tools" / "build_codex_plugin_release.py"
PLUGIN_TEMPLATE = REPO_ROOT / "plugins" / "llmwiki-research"
RUN_RELEASE_TESTS = os.environ.get("LLMWIKI_RUN_RELEASE_TESTS") == "1"
EXPECTED_TOOLS = (
    "llmwiki_project_context",
    "llmwiki_host_context",
    "llmwiki_coverage",
    "llmwiki_source_open",
    "llmwiki_query",
    "llmwiki_reconcile",
    "llmwiki_plan",
)
PLUGIN_ID = "llmwiki-research@llmwiki-research-release"
SECRET_INPUT = "J05B_SECRET_INPUT_CANARY"
SECRET_BINARY = b"J05B_SECRET_BINARY_CANARY"
CHINESE_REPORT = (
    "# \u9879\u76ee\u7406\u89e3\u62a5\u544a\r\n\r\n"
    "\u8fd9\u662f\u5e72\u51c0\u53d1\u884c\u5305\u4e2d\u7684\u4e2d\u6587 UTF-8 \u56de\u8bfb\u9a8c\u8bc1\u3002"
    "`ResearchCoreService` \u548c `llmwiki_query` \u4fdd\u6301\u7cbe\u786e\u82f1\u6587\u6807\u8bc6\u3002\r\n"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(root: Path) -> dict[str, tuple[str, int, int, int]]:
    snapshot: dict[str, tuple[str, int, int, int]] = {}
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        base = Path(directory)
        for filename in filenames:
            path = base / filename
            stat = path.stat()
            snapshot[path.relative_to(root).as_posix()] = (
                _sha256(path),
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_mode,
            )
    return snapshot


def _resolve_codex_executable() -> Path | None:
    configured = os.environ.get("LLMWIKI_CODEX_EXE", "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve(strict=False)
        return candidate if candidate.is_file() else None
    direct = shutil.which("codex.exe")
    if direct:
        return Path(direct).resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates = list(
            (Path(local_app_data) / "OpenAI" / "Codex" / "bin").glob("**/codex.exe")
        )
        candidates = [path for path in candidates if path.is_file()]
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime_ns).resolve()
    return None


def _check_schema(schema: dict[str, Any]) -> Any:
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    return validator_class(schema)


def _structured(result: Any) -> dict[str, Any]:
    payload = result.structuredContent
    if not isinstance(payload, dict):
        raise AssertionError(f"missing structured MCP payload: {result!r}")
    return payload


class CodexPluginReleaseContractTests(unittest.TestCase):
    def test_prune_target_console_launchers_is_deterministic_and_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="j5c-prune-") as temporary:
            site_packages = Path(temporary)
            launcher = site_packages / "bin" / "mcp.exe"
            launcher.parent.mkdir()
            launcher.write_bytes(b"nondeterministic-launcher")
            record = site_packages / "mcp-1.0.dist-info" / "RECORD"
            record.parent.mkdir()
            record.write_text(
                "../../bin/mcp.exe,sha256=unstable,27\n"
                "mcp/__init__.py,sha256=stable,12\n"
                "mcp-1.0.dist-info/RECORD,,\n",
                encoding="utf-8",
                newline="\n",
            )

            prune_target_console_launchers(site_packages)
            first = record.read_bytes()
            prune_target_console_launchers(site_packages)

            self.assertFalse((site_packages / "bin").exists())
            self.assertEqual(record.read_bytes(), first)
            self.assertEqual(
                first.decode("utf-8"),
                "mcp/__init__.py,sha256=stable,12\nmcp-1.0.dist-info/RECORD,,\n",
            )

    def test_release_inputs_are_locked_and_versioned(self) -> None:
        manifest = json.loads(
            (PLUGIN_TEMPLATE / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        runtime = json.loads(
            (PLUGIN_TEMPLATE / "release" / "python-runtime-win-amd64.json").read_text(
                encoding="utf-8"
            )
        )
        requirements = (
            PLUGIN_TEMPLATE / "release" / "requirements-win-amd64.txt"
        ).read_text(encoding="utf-8-sig")
        core_files = [
            line
            for line in (PLUGIN_TEMPLATE / "release" / "core-files.txt")
            .read_text(encoding="utf-8-sig")
            .splitlines()
            if line and not line.startswith("#")
        ]

        self.assertEqual(manifest["version"], VERSION)
        self.assertEqual(runtime["schema_version"], 1)
        self.assertEqual(runtime["platform"], "windows")
        self.assertEqual(runtime["architecture"], "x86_64")
        self.assertRegex(runtime["sha256"], r"^[0-9a-f]{64}$")
        requirement_lines = [
            line
            for line in requirements.splitlines()
            if line and not line.startswith("#")
        ]
        self.assertGreaterEqual(len(requirement_lines), 30)
        self.assertTrue(
            all("--hash=sha256:" in line for line in requirement_lines),
            "every runtime wheel must be hash locked",
        )
        self.assertEqual(len(core_files), len(set(core_files)))
        self.assertIn("tools/research_core.py", core_files)
        self.assertIn("tools/research_mcp.py", core_files)
        self.assertIn("tools/research_cockpit.py", core_files)
        self.assertTrue(
            (PLUGIN_TEMPLATE / "scripts" / "write_utf8_report.ps1").is_file()
        )
        release_notes = (PLUGIN_TEMPLATE / "release" / "RELEASE_NOTES.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Codex Plugin 0.2.1", release_notes)
        self.assertIn("write_utf8_report.ps1", release_notes)
        install_common = (PLUGIN_TEMPLATE / "release" / "install-common.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('"plugin", "add"', install_common)
        self.assertNotIn('"plugin", "install"', install_common)


@unittest.skipUnless(
    RUN_RELEASE_TESTS and os.name == "nt",
    "set LLMWIKI_RUN_RELEASE_TESTS=1 on Windows to run the real release acceptance",
)
class CodexPluginReleaseAcceptanceTests(unittest.TestCase):
    maxDiff = None

    def _run(
        self,
        command: list[str | os.PathLike[str]],
        *,
        cwd: Path | None = None,
        timeout: int = 180,
        expect_success: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [os.fspath(part) for part in command],
            cwd=cwd,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if expect_success and completed.returncode != 0:
            self.fail(
                f"command failed ({completed.returncode}): {command!r}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        return completed

    def _powershell(self, script: Path, *arguments: str) -> dict[str, Any]:
        completed = self._run(
            [
                self.powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script,
                *arguments,
            ],
            cwd=self.clean_root,
            timeout=300,
        )
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        return payload

    def _powershell_failure(
        self, script: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        completed = self._run(
            [
                self.powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script,
                *arguments,
            ],
            cwd=self.clean_root,
            timeout=300,
            expect_success=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        return completed

    def _report_writer_command(
        self,
        writer: Path,
        target: Path,
        *,
        content: str | None = None,
        verify_only: bool = False,
        force: bool = False,
        expect_success: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command: list[str | os.PathLike[str]] = [
            self.powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            writer,
            "-LiteralPath",
            target,
        ]
        if content is not None:
            command.extend(["-Content", content])
        if verify_only:
            command.append("-VerifyOnly")
        if force:
            command.append("-Force")
        return self._run(
            command,
            cwd=self.clean_root,
            timeout=60,
            expect_success=expect_success,
        )

    def _report_writer_json(
        self,
        writer: Path,
        target: Path,
        *,
        content: str | None = None,
        verify_only: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        completed = self._report_writer_command(
            writer,
            target,
            content=content,
            verify_only=verify_only,
            force=force,
        )
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "passed")
        return payload

    def _codex_json(self, *arguments: str) -> Any:
        completed = self._run(
            [self.codex, *arguments], cwd=self.clean_root, timeout=120
        )
        return json.loads(completed.stdout)

    def _embedded_cli(self, plugin_root: Path, *arguments: str) -> dict[str, Any]:
        completed = self._run(
            [
                plugin_root / "runtime" / "python" / "python.exe",
                "-I",
                "-B",
                "scripts/llmwiki.py",
                *arguments,
            ],
            cwd=plugin_root,
            timeout=300,
        )
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    def _cockpit_snapshot(self, plugin_root: Path, project_id: str) -> dict[str, Any]:
        completed = self._run(
            [
                plugin_root / "runtime" / "python" / "python.exe",
                "-I",
                "-B",
                "scripts/research_cockpit.py",
                "snapshot",
                "--project-id",
                project_id,
            ],
            cwd=plugin_root,
            timeout=120,
        )
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    async def _exercise_mcp_before_source_sync(
        self,
        plugin_root: Path,
        project_id: str,
    ) -> None:
        stderr_path = self.clean_root / "mcp-before-sync.stderr"
        parameters = StdioServerParameters(
            command=str(plugin_root / "runtime" / "python" / "python.exe"),
            args=["-I", "-B", "scripts/launch_mcp.py"],
            cwd=str(plugin_root),
            env=self.environment,
        )
        with stderr_path.open("w", encoding="utf-8") as errlog:
            async with stdio_client(parameters, errlog=errlog) as streams:
                async with ClientSession(*streams) as session:
                    initialized = await session.initialize()
                    self.assertEqual(
                        initialized.serverInfo.name, "llmwiki-research-core"
                    )
                    self.assertEqual(initialized.serverInfo.version, "0.3.0")
                    listed = await session.list_tools()
                    self.assertEqual(
                        tuple(tool.name for tool in listed.tools), EXPECTED_TOOLS
                    )
                    output_validators: dict[str, Any] = {}
                    for tool in listed.tools:
                        self.assertIsInstance(tool.inputSchema, dict)
                        self.assertIsInstance(tool.outputSchema, dict)
                        _check_schema(tool.inputSchema)
                        output_validators[tool.name] = _check_schema(tool.outputSchema)

                    async def call(name: str, arguments: dict[str, Any]) -> Any:
                        result = await session.call_tool(name, arguments)
                        output_validators[name].validate(_structured(result))
                        return result

                    context = await call(
                        "llmwiki_project_context", {"project_id": project_id}
                    )
                    self.assertFalse(context.isError)
                    self.assertEqual(
                        _structured(context)["result"]["project_id"], project_id
                    )

                    reconciled = await call(
                        "llmwiki_reconcile", {"project_id": project_id}
                    )
                    self.assertFalse(reconciled.isError)
                    reconcile_result = _structured(reconciled)["result"]
                    self.assertEqual(reconcile_result["mode"], "full-scan")
                    self.assertEqual(
                        reconcile_result["run"]["through_stage"], "classify"
                    )
                    self.assertEqual(
                        reconcile_result["coverage"]["failed_file_count"], 0
                    )
                    self.assertEqual(reconcile_result["hints"]["status"], "absent")

                    coverage = await call(
                        "llmwiki_coverage", {"project_id": project_id}
                    )
                    self.assertFalse(coverage.isError)
                    self.assertEqual(
                        _structured(coverage)["result"]["project_id"], project_id
                    )

                    host_context = await call(
                        "llmwiki_host_context",
                        {"project_id": project_id, "max_bytes": 4096},
                    )
                    self.assertFalse(host_context.isError)
                    self.assertEqual(
                        _structured(host_context)["result"]["project_id"], project_id
                    )

                    plan = await call(
                        "llmwiki_plan",
                        {"project_id": project_id, "objective": "Finish the study"},
                    )
                    self.assertFalse(plan.isError)
                    plan_result = _structured(plan)["result"]
                    self.assertEqual(plan_result["goal"]["status"], "draft")
                    self.assertEqual(plan_result["plan"]["status"], "draft")
                    self.assertTrue(plan_result["tasks"]["task_ids"])
                    self.assertEqual(
                        plan_result["tasks"]["statuses"],
                        ["draft"] * len(plan_result["tasks"]["task_ids"]),
                    )

                    query = await call(
                        "llmwiki_query",
                        {
                            "project_id": project_id,
                            "question": f"What changed? {SECRET_INPUT}",
                        },
                    )
                    self.assertTrue(query.isError)
                    query_error = _structured(query)["error"]
                    self.assertEqual(query_error["code"], "capability-unavailable")
                    self.assertEqual(
                        query_error["details"],
                        {"available_after": "G-04", "status": "not-implemented"},
                    )

                    invalid = await call(
                        "llmwiki_project_context",
                        {"project_id": f"../../{SECRET_INPUT}"},
                    )
                    self.assertTrue(invalid.isError)
                    invalid_payload = _structured(invalid)
                    self.assertEqual(
                        invalid_payload["error"]["code"], "project-id-invalid"
                    )
                    serialized_errors = json.dumps(
                        {
                            "query": _structured(query),
                            "invalid": invalid_payload,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    for forbidden in (
                        SECRET_INPUT,
                        str(REPO_ROOT),
                        str(self.clean_root),
                    ):
                        self.assertNotIn(forbidden, serialized_errors)
        stderr = stderr_path.read_text(encoding="utf-8")
        self.assertEqual(stderr, "")

    async def _exercise_source_open(
        self,
        plugin_root: Path,
        project_id: str,
        source_id: str,
    ) -> None:
        stderr_path = self.clean_root / "mcp-source-open.stderr"
        parameters = StdioServerParameters(
            command=str(plugin_root / "runtime" / "python" / "python.exe"),
            args=["-I", "-B", "scripts/launch_mcp.py"],
            cwd=str(plugin_root),
            env=self.environment,
        )
        with stderr_path.open("w", encoding="utf-8") as errlog:
            async with stdio_client(parameters, errlog=errlog) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    schema = next(
                        tool.outputSchema
                        for tool in listed.tools
                        if tool.name == "llmwiki_source_open"
                    )
                    validator = _check_schema(schema)
                    opened = await session.call_tool(
                        "llmwiki_source_open",
                        {
                            "project_id": project_id,
                            "target_id": source_id,
                            "locator": {
                                "schema_version": 1,
                                "kind": "llmwiki-locator",
                                "locator_type": "line_range",
                                "start_line": 1,
                                "end_line": 1,
                            },
                        },
                    )
                    payload = _structured(opened)
                    validator.validate(payload)
                    self.assertFalse(opened.isError)
                    self.assertEqual(payload["result"]["excerpt"], "alpha = 1\n")
                    self.assertTrue(payload["result"]["content_hash_verified"])
        self.assertEqual(stderr_path.read_text(encoding="utf-8"), "")

    def test_real_release_installs_runs_moves_rolls_back_and_uninstalls_cleanly(
        self,
    ) -> None:
        self.codex = _resolve_codex_executable()
        if self.codex is None:
            self.skipTest("Codex executable is unavailable")
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("Windows PowerShell is unavailable")
        self.powershell = Path(powershell).resolve()

        profile_parent = (
            Path(os.environ.get("LLMWIKI_RELEASE_TEST_ROOT", str(Path.home())))
            .expanduser()
            .resolve()
        )
        profile_parent.mkdir(parents=True, exist_ok=True)
        # Keep the profile shallow enough for embedded wheels on Windows hosts
        # where cleanup still traverses legacy MAX_PATH-sensitive paths.
        with tempfile.TemporaryDirectory(
            prefix="j5c-", dir=profile_parent
        ) as temporary:
            self.clean_root = Path(temporary).resolve()
            try:
                self.clean_root.relative_to(REPO_ROOT)
            except ValueError:
                pass
            else:
                self.fail("clean profile must not be inside the source checkout")

            build_output = self.clean_root / "b"
            cache_dir = (
                Path(
                    os.environ.get(
                        "LLMWIKI_RELEASE_CACHE_DIR", os.fspath(default_cache_dir())
                    )
                )
                .expanduser()
                .resolve()
            )
            build_command: list[str | os.PathLike[str]] = [
                sys.executable,
                "-B",
                BUILD_SCRIPT,
                "--output-dir",
                build_output,
                "--cache-dir",
                cache_dir,
                "--json",
            ]
            if os.environ.get("LLMWIKI_RELEASE_OFFLINE") == "1":
                build_command.append("--offline")
            build_completed = subprocess.run(
                [os.fspath(part) for part in build_command],
                cwd=REPO_ROOT,
                env=os.environ.copy(),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
            self.assertEqual(
                build_completed.returncode,
                0,
                f"{build_completed.stdout}\n{build_completed.stderr}",
            )
            build_result = json.loads(build_completed.stdout)
            release_dir = Path(build_result["release_directory"])
            archive = Path(build_result["archive"])
            self.assertEqual(release_dir.name, BASENAME)
            self.assertTrue(archive.is_file())
            self.assertEqual(_sha256(archive), build_result["sha256"])
            sidecar = archive.with_suffix(archive.suffix + ".sha256")
            self.assertEqual(
                sidecar.read_text(encoding="ascii").split()[0],
                build_result["sha256"],
            )

            actual_files = {
                path.relative_to(release_dir).as_posix()
                for path in release_dir.rglob("*")
                if path.is_file()
            }
            listed_files = set(
                (release_dir / "FILELIST.txt").read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(listed_files, actual_files)
            self.assertFalse(
                any(
                    "/runtime/python/Lib/site-packages/bin/" in f"/{relative}"
                    for relative in listed_files
                ),
                "unused pip console launchers make release builds non-reproducible",
            )
            checksum_records: dict[str, str] = {}
            for line in (
                (release_dir / "SHA256SUMS").read_text(encoding="ascii").splitlines()
            ):
                digest, relative = line.split("  ", 1)
                self.assertNotIn(relative, checksum_records)
                checksum_records[relative] = digest
            self.assertEqual(set(checksum_records), actual_files - {"SHA256SUMS"})
            for relative, digest in checksum_records.items():
                self.assertEqual(_sha256(release_dir / relative), digest, relative)

            release_metadata = json.loads(
                (release_dir / "release.json").read_text(encoding="utf-8")
            )
            self.assertEqual(release_metadata["version"], VERSION)
            self.assertEqual(
                release_metadata["default_workspace"],
                "%LOCALAPPDATA%\\LLMWiki\\workspace",
            )
            bundled_build = json.loads(
                (
                    release_dir
                    / "plugins"
                    / "llmwiki-research"
                    / "runtime"
                    / "core"
                    / "BUILD.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                bundled_build["c07"],
                {"processing_status": "not_started", "status": "deferred"},
            )

            home = self.clean_root / "home"
            local_app_data = self.clean_root / "localappdata"
            app_data = self.clean_root / "appdata"
            codex_home = self.clean_root / "codex-home"
            temp_root = self.clean_root / "temp"
            canary_root = self.clean_root / "pythonpath-canary"
            for directory in (
                home,
                local_app_data,
                app_data,
                codex_home,
                temp_root,
                canary_root,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            import_marker = self.clean_root / "PYTHONPATH_WAS_IMPORTED"
            (canary_root / "sitecustomize.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(import_marker)!r}).write_text('imported')\n",
                encoding="utf-8",
            )
            self.environment = os.environ.copy()
            for variable in (
                "LLMWIKI_CORE_ROOT",
                "LLMWIKI_WORKSPACE_ROOT",
                "LLMWIKI_PROJECT_ID",
            ):
                self.environment.pop(variable, None)
            self.environment.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "LOCALAPPDATA": str(local_app_data),
                    "APPDATA": str(app_data),
                    "HOME": str(home),
                    "USERPROFILE": str(home),
                    "TEMP": str(temp_root),
                    "TMP": str(temp_root),
                    "PYTHONPATH": str(canary_root),
                    "PYTHONHOME": str(canary_root),
                    "PYTHONUTF8": "1",
                    "PSModuleAnalysisCachePath": str(
                        temp_root / "PowerShell" / "ModuleAnalysisCache"
                    ),
                }
            )
            self.assertNotIn("LLMWIKI_CORE_ROOT", self.environment)
            self.assertNotIn("LLMWIKI_WORKSPACE_ROOT", self.environment)

            unmanaged_root = self.clean_root / f"unmanaged-{SECRET_INPUT}"
            unmanaged_root.mkdir()
            (unmanaged_root / "user-file.txt").write_text("preserve", encoding="utf-8")
            failed_install = self._powershell_failure(
                release_dir / "install.ps1",
                "-InstallRoot",
                str(unmanaged_root),
                "-CodexCommand",
                str(self.codex),
            )
            self.assertEqual(
                failed_install.stderr,
                "error: LLM Wiki Research Plugin installation failed.\n",
            )
            self.assertEqual(
                (unmanaged_root / "user-file.txt").read_text(encoding="utf-8"),
                "preserve",
            )
            for forbidden in (SECRET_INPUT, str(REPO_ROOT), str(self.clean_root)):
                self.assertNotIn(forbidden, failed_install.stderr)

            install = self._powershell(
                release_dir / "install.ps1", "-CodexCommand", str(self.codex)
            )
            self.assertEqual(install["plugin_id"], PLUGIN_ID)
            self.assertEqual(install["version"], VERSION)
            default_workspace = local_app_data / "LLMWiki" / "workspace"
            self.assertEqual(Path(install["default_workspace"]), default_workspace)
            managed_base = (
                local_app_data / "LLMWiki" / "CodexPlugins" / "llmwiki-research"
            )
            self.assertTrue(
                (managed_base / ".llmwiki-codex-plugin-install.json").is_file()
            )

            plugin_list = self._codex_json("plugin", "list", "--json")
            self.assertEqual(
                [entry["pluginId"] for entry in plugin_list["installed"]],
                [PLUGIN_ID],
            )
            self.assertTrue(plugin_list["installed"][0]["enabled"])
            installed_plugin = Path(install["codex"]["installedPath"]).resolve()
            report_writer = installed_plugin / "scripts" / "write_utf8_report.ps1"
            self.assertTrue(report_writer.is_file())
            report_path = self.clean_root / "outputs" / "project-understanding.md"
            report_metadata = self._report_writer_json(
                report_writer,
                report_path,
                content=CHINESE_REPORT,
                force=True,
            )
            report_bytes = report_path.read_bytes()
            report_text = report_bytes.decode("utf-8", errors="strict")
            self.assertFalse(report_bytes.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(report_text, CHINESE_REPORT)
            self.assertGreater(report_metadata["cjk_characters"], 0)
            self.assertEqual(report_metadata["literal_question_marks"], 0)
            self.assertEqual(report_metadata["sha256"], _sha256(report_path))
            self.assertEqual(
                self._report_writer_json(report_writer, report_path, verify_only=True),
                report_metadata,
            )
            rejected_report_path = (
                self.clean_root / f"rejected-{SECRET_INPUT}" / "report.md"
            )
            rejected_report = self._report_writer_command(
                report_writer,
                rejected_report_path,
                content=f"# English report\n\n{SECRET_INPUT}\n",
                expect_success=False,
            )
            self.assertEqual(rejected_report.returncode, 2)
            self.assertEqual(rejected_report.stdout, "")
            self.assertEqual(
                rejected_report.stderr,
                "error: report-language-zh-cn-missing\n",
            )
            self.assertNotIn(str(rejected_report_path), rejected_report.stderr)
            self.assertNotIn(SECRET_INPUT, rejected_report.stderr)
            self.assertFalse(rejected_report_path.exists())
            installed_skill = (
                installed_plugin / "skills" / "llmwiki-research" / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertIn("name: llmwiki-research", installed_skill)
            self.assertIn("llmwiki_reconcile", installed_skill)

            prompt_input = self._codex_json(
                "debug", "prompt-input", "J-05B clean Skill discovery probe"
            )
            skill_blocks = [
                part["text"]
                for message in prompt_input
                for part in message.get("content", [])
                if isinstance(part, dict)
                and isinstance(part.get("text"), str)
                and "<skills_instructions>" in part["text"]
            ]
            self.assertEqual(len(skill_blocks), 1)
            self.assertIn("- llmwiki-research:llmwiki-research:", skill_blocks[0])
            self.assertIn(
                "skills/llmwiki-research/SKILL.md",
                skill_blocks[0].replace("\\", "/"),
            )

            mcp_list = self._codex_json("mcp", "list", "--json")
            self.assertEqual(
                [entry["name"] for entry in mcp_list], ["llmwiki-research-core"]
            )
            transport = mcp_list[0]["transport"]
            self.assertEqual(transport["command"], "runtime/python/python.exe")
            self.assertEqual(transport["args"], ["-I", "-B", "scripts/launch_mcp.py"])
            self.assertEqual(Path(transport["cwd"]).resolve(), installed_plugin)

            moved_release = self.clean_root / "moved-release" / release_dir.name
            moved_release.parent.mkdir(parents=True)
            release_dir.rename(moved_release)
            self.assertFalse(release_dir.exists())
            force_install = self._powershell(
                moved_release / "install.ps1",
                "-Force",
                "-CodexCommand",
                str(self.codex),
            )
            self.assertEqual(force_install["version"], VERSION)
            installed_plugin = Path(force_install["codex"]["installedPath"]).resolve()

            relocated_plugin = self.clean_root / "relocated-plugin" / "llmwiki-research"
            relocated_plugin.parent.mkdir(parents=True)
            shutil.copytree(installed_plugin, relocated_plugin)
            (relocated_plugin / "hooks" / "hooks.json").unlink()
            self.assertFalse((relocated_plugin / "hooks" / "hooks.json").exists())
            relocated_writer = relocated_plugin / "scripts" / "write_utf8_report.ps1"
            self.assertTrue(relocated_writer.is_file())
            self.assertEqual(
                self._report_writer_json(
                    relocated_writer, report_path, verify_only=True
                ),
                report_metadata,
            )
            relocated_report = self.clean_root / "outputs" / "relocated-report.md"
            relocated_metadata = self._report_writer_json(
                relocated_writer,
                relocated_report,
                content=CHINESE_REPORT,
                force=True,
            )
            relocated_bytes = relocated_report.read_bytes()
            self.assertEqual(relocated_metadata["sha256"], _sha256(relocated_report))
            self.assertEqual(
                relocated_bytes.decode("utf-8", errors="strict"), CHINESE_REPORT
            )

            project = self.clean_root / "research-project"
            (project / "src").mkdir(parents=True)
            (project / "data").mkdir()
            (project / "README.md").write_bytes(
                b"# Clean release fixture\n\nSee src/model.py.\n"
            )
            (project / "src" / "model.py").write_bytes(b"alpha = 1\nbeta = 2\n")
            (project / "data" / "secret.npy").write_bytes(b"\\x93NUMPY" + SECRET_BINARY)
            source_before = _snapshot(project)
            knowledge_parent = self.clean_root / "knowledge"
            project_id = "j05b-clean-release"
            registration = self._embedded_cli(
                relocated_plugin,
                "register",
                str(project),
                "--project-id",
                project_id,
                "--knowledge-root",
                str(knowledge_parent),
                "--json",
            )
            self.assertEqual(registration["project_id"], project_id)
            self.assertEqual(
                Path(registration["record"]["storage"]["workspace_root"]),
                default_workspace,
            )
            understood = self._embedded_cli(
                relocated_plugin, "understand", str(project), "--json"
            )
            self.assertTrue(understood["ok"])
            self.assertEqual(understood["project_id"], project_id)
            self.assertEqual(understood["run"]["status"], "succeeded")
            self.assertEqual(understood["run"]["usage"]["input_tokens"], 0)
            self.assertEqual(understood["run"]["usage"]["output_tokens"], 0)
            curated_root = knowledge_parent / project_id
            curated_before = _snapshot(curated_root)

            asyncio.run(
                self._exercise_mcp_before_source_sync(relocated_plugin, project_id)
            )
            source_sync = self._embedded_cli(
                relocated_plugin, "source", "sync", project_id, "--json"
            )
            self.assertTrue(source_sync["ok"])
            registry_path = (
                default_workspace
                / ".llmwiki"
                / "projects"
                / project_id
                / "sources.jsonl"
            )
            registry = [
                json.loads(line)
                for line in registry_path.read_text(encoding="utf-8").splitlines()
            ]
            source_id = next(
                record["source_id"]
                for record in registry
                if record.get("record_type") == "source"
                and record.get("current_path") == "src/model.py"
            )
            asyncio.run(
                self._exercise_source_open(relocated_plugin, project_id, source_id)
            )

            cockpit = self._cockpit_snapshot(relocated_plugin, project_id)
            capabilities = cockpit["capabilities"]
            self.assertEqual(
                capabilities["query"],
                {
                    "available": False,
                    "error": "capability-unavailable",
                    "available_after": "G-04",
                },
            )
            self.assertEqual(capabilities["c07"], "deferred/not_started")
            self.assertFalse(capabilities["source_bytes_reopened"])
            self.assertFalse(capabilities["plan"]["execution_authorized"])
            self.assertEqual(capabilities["external_send"], "local-only")

            machine_root = default_workspace / ".llmwiki" / "projects" / project_id
            self.assertEqual(_snapshot(project), source_before)
            self.assertEqual(_snapshot(curated_root), curated_before)
            self.assertEqual(list((machine_root / "extracted").iterdir()), [])
            for path in machine_root.rglob("*"):
                if path.is_file():
                    data = path.read_bytes()
                    self.assertNotIn(SECRET_BINARY, data, path)
                    self.assertNotIn(SECRET_INPUT.encode("utf-8"), data, path)
            self.assertFalse(import_marker.exists())

            uninstalled = self._powershell(
                moved_release / "uninstall.ps1", "-CodexCommand", str(self.codex)
            )
            self.assertTrue(uninstalled["package_cache_retained"])
            self.assertTrue(uninstalled["workspace_retained"])
            self.assertEqual(
                self._codex_json("plugin", "list", "--json")["installed"], []
            )
            self.assertTrue(managed_base.is_dir())
            self.assertTrue(default_workspace.is_dir())

            rollback = self._powershell(
                moved_release / "rollback.ps1",
                "-Version",
                VERSION,
                "-CodexCommand",
                str(self.codex),
            )
            self.assertEqual(rollback["version"], VERSION)
            self.assertEqual(
                [
                    entry["pluginId"]
                    for entry in self._codex_json("plugin", "list", "--json")[
                        "installed"
                    ]
                ],
                [PLUGIN_ID],
            )

            purged = self._powershell(
                moved_release / "uninstall.ps1",
                "-PurgePackages",
                "-CodexCommand",
                str(self.codex),
            )
            self.assertFalse(purged["package_cache_retained"])
            self.assertTrue(purged["workspace_retained"])
            self.assertFalse(managed_base.exists())
            self.assertTrue(default_workspace.is_dir())
            self.assertEqual(
                self._codex_json("plugin", "list", "--json")["installed"], []
            )
            self.assertEqual(self._codex_json("mcp", "list", "--json"), [])
            self.assertFalse(import_marker.exists())


if __name__ == "__main__":
    unittest.main()
