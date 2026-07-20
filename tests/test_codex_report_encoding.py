from __future__ import annotations

import codecs
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "llmwiki-research"
WRITER = PLUGIN_ROOT / "scripts" / "write_utf8_report.ps1"
SKILL = PLUGIN_ROOT / "skills" / "llmwiki-research" / "SKILL.md"
CHINESE_REPORT = (
    "# 项目理解报告\r\n\r\n"
    "这是中文编码验证。`ResearchCoreService`、`llmwiki_query` 和 `G-04` "
    "保持原英文标识。\r\n"
)
SECRET = "REPORT_SECRET_CANARY"


class CodexReportLanguageContractTests(unittest.TestCase):
    def test_skill_defaults_human_readable_reports_to_chinese_and_requires_readback(
        self,
    ) -> None:
        skill = SKILL.read_text(encoding="utf-8")

        self.assertIn(
            "Human-readable Markdown reports default to Simplified Chinese",
            skill,
        )
        self.assertRegex(
            skill, r"unless\s+the user explicitly requests another language"
        )
        self.assertIn("write_utf8_report.ps1", skill)
        self.assertIn("strictly decode the saved bytes as UTF-8", skill)
        self.assertIn("no suspicious\nrun of literal `?` characters", skill)
        self.assertIn("@'...中文...'@ | python -", skill)
        for preserved in (
            "code",
            "paths",
            "shell commands",
            "API and MCP",
            "Schema field names",
            "Git refs and hashes",
            "project IDs",
        ):
            self.assertIn(preserved, skill)

    def test_writer_source_is_strict_utf8_without_bom(self) -> None:
        raw = WRITER.read_bytes()

        self.assertFalse(raw.startswith(codecs.BOM_UTF8))
        source = raw.decode("utf-8", errors="strict")
        self.assertIn("UTF8Encoding($false, $true)", source)
        self.assertIn("report-text-contains-question-mark-run", source)
        self.assertIn("report-language-zh-cn-missing", source)
        self.assertIn("[System.IO.File]::Replace", source)
        self.assertNotIn("LLMWIKI_CORE_ROOT", source)


@unittest.skipUnless(os.name == "nt", "PowerShell report writer is Windows-only")
class CodexReportWriterWindowsTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if cls.powershell is None:
            raise unittest.SkipTest("Windows PowerShell is unavailable")

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory(
            prefix="llmwiki-report-encoding-"
        )
        self.root = Path(self.temp_directory.name)
        self.environment = os.environ.copy()
        self.environment.pop("LLMWIKI_CORE_ROOT", None)
        self.environment.pop("PYTHONPATH", None)
        self.environment.pop("PYTHONHOME", None)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def _run(
        self,
        path: Path,
        *,
        content: str | None = None,
        language: str | None = None,
        verify_only: bool = False,
        force: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            self.powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            os.fspath(WRITER),
            "-LiteralPath",
            os.fspath(path),
        ]
        if content is not None:
            command.extend(["-Content", content])
        if language is not None:
            command.extend(["-Language", language])
        if verify_only:
            command.append("-VerifyOnly")
        if force:
            command.append("-Force")
        return subprocess.run(
            command,
            cwd=self.root,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
        )

    def _success_payload(
        self, completed: subprocess.CompletedProcess[str]
    ) -> dict[str, object]:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    def _assert_bounded_failure(
        self,
        completed: subprocess.CompletedProcess[str],
        code: str,
        path: Path,
    ) -> None:
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr.strip(), f"error: {code}")
        self.assertNotIn(os.fspath(path), completed.stderr)
        self.assertNotIn(SECRET, completed.stderr)

    def test_writer_round_trips_chinese_and_verify_only_rechecks_exact_bytes(
        self,
    ) -> None:
        target = self.root / "nested" / "project-understanding.md"

        written = self._success_payload(
            self._run(target, content=CHINESE_REPORT, force=True)
        )
        raw = target.read_bytes()
        decoded = raw.decode("utf-8", errors="strict")

        self.assertFalse(raw.startswith(codecs.BOM_UTF8))
        self.assertEqual(decoded, CHINESE_REPORT)
        self.assertIn("项目理解报告", decoded)
        self.assertNotIn("????", decoded)
        self.assertEqual(written["schema_version"], 1)
        self.assertEqual(written["kind"], "llmwiki-report-encoding-validation")
        self.assertEqual(written["status"], "passed")
        self.assertEqual(written["encoding"], "utf-8")
        self.assertFalse(written["utf8_bom"])
        self.assertEqual(written["language"], "zh-CN")
        self.assertGreater(written["cjk_characters"], 0)
        self.assertEqual(written["literal_question_marks"], 0)
        self.assertEqual(written["replacement_characters"], 0)
        self.assertEqual(written["sha256"], hashlib.sha256(raw).hexdigest())

        verified = self._success_payload(self._run(target, verify_only=True))
        self.assertEqual(verified, written)

    def test_explicit_any_language_accepts_an_english_report(self) -> None:
        target = self.root / "english.md"

        written = self._success_payload(
            self._run(
                target,
                content="# English report\n\nRequested by the user.\n",
                language="any",
            )
        )

        self.assertEqual(written["language"], "any")
        self.assertEqual(written["cjk_characters"], 0)
        self._success_payload(self._run(target, verify_only=True, language="any"))

    def test_default_language_rejects_english_only_content_before_creation(
        self,
    ) -> None:
        target = self.root / "missing-chinese.md"

        failed = self._run(
            target,
            content=f"# English report\n\n{SECRET}\n",
        )

        self._assert_bounded_failure(
            failed,
            "report-language-zh-cn-missing",
            target,
        )
        self.assertFalse(target.exists())

    def test_verify_rejects_corruption_signals_without_echoing_content_or_path(
        self,
    ) -> None:
        fixtures = {
            "question-marks.md": (
                f"# ???? report\n{SECRET}\n".encode("ascii"),
                "report-text-contains-question-mark-run",
            ),
            "invalid-utf8.md": (
                b"# report\n\xff" + SECRET.encode("ascii"),
                "report-file-is-not-strict-utf8",
            ),
            "replacement.md": (
                f"# 中文报告\n\ufffd\n{SECRET}\n".encode("utf-8"),
                "report-text-contains-replacement-character",
            ),
            "bom.md": (
                codecs.BOM_UTF8 + f"# 中文报告\n{SECRET}\n".encode("utf-8"),
                "report-file-has-utf8-bom",
            ),
        }

        for filename, (raw, expected_code) in fixtures.items():
            with self.subTest(filename=filename):
                target = self.root / filename
                target.write_bytes(raw)
                failed = self._run(target, verify_only=True)
                self._assert_bounded_failure(failed, expected_code, target)

    def test_existing_report_requires_force_and_is_not_clobbered_on_rejection(
        self,
    ) -> None:
        target = self.root / "existing.md"
        self._success_payload(self._run(target, content=CHINESE_REPORT))
        original = target.read_bytes()

        failed = self._run(
            target,
            content=f"# 新报告\n\n{SECRET}\n",
        )

        self._assert_bounded_failure(failed, "report-output-exists", target)
        self.assertEqual(target.read_bytes(), original)
        self.assertNotIn(SECRET.encode("utf-8"), target.read_bytes())


if __name__ == "__main__":
    unittest.main()
