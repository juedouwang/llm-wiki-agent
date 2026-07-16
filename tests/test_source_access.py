from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from openpyxl import Workbook
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from tools.evidence_registry import excerpt_sha256, register_evidence
from tools.extraction_schema import (
    EXTRACTION_SCHEMA_VERSION,
    LineRangeLocator,
    NotebookCellLocator,
    PdfPageLocator,
    TableRangeLocator,
)
from tools.pdf_extractor import extract_pdf_bytes
from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.source_access import (
    SOURCE_ACCESS_VERSION,
    SOURCE_LOCATION_KIND,
    SOURCE_OPEN_KIND,
    TABLE_EXCERPT_FORMAT,
    SourceBoundaryError,
    SourceContentMismatchError,
    SourceContentPolicyDeniedError,
    SourceExcerptMismatchError,
    SourceFormatError,
    SourceLocatorError,
    SourceMissingError,
    SourceNotFoundError,
    SourceVersionMismatchError,
    deserialize_locator,
    locate_source,
    open_evidence,
    open_source,
)
from tools.source_registry import load_source_registry, sync_source_registry


REPO_ROOT = Path(__file__).resolve().parent.parent


class SourceAccessTests(unittest.TestCase):
    @staticmethod
    def _pdf_literal(text: str) -> bytes:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return escaped.encode("latin-1")

    @classmethod
    def pdf_bytes(cls, pages: list[str]) -> bytes:
        writer = PdfWriter()
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        font_reference = writer._add_object(font)
        for text in pages:
            page = writer.add_blank_page(width=612, height=792)
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): font_reference}
                    )
                }
            )
            content = DecodedStreamObject()
            content.set_data(
                b"BT /F1 12 Tf 72 720 Td ("
                + cls._pdf_literal(text)
                + b") Tj ET"
            )
            page[NameObject("/Contents")] = writer._add_object(content)
        destination = io.BytesIO()
        writer.write(destination)
        return destination.getvalue()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.project = self.root / "research-project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "papers").mkdir()
        (self.project / "notebooks").mkdir()
        (self.project / "tables").mkdir()
        (self.project / "broken").mkdir()

        self.code_path = self.project / "src" / "model.py"
        self.code_bytes = b"alpha = 1\r\nbeta = 2\r\ngamma = alpha + beta\r\n"
        self.code_path.write_bytes(self.code_bytes)
        self.sensitive_secret = "SOURCE_ACCESS_ENV_SECRET_5f07a2c1"
        self.sensitive_path = self.project / ".env"
        self.sensitive_path.write_text(
            f"TOKEN={self.sensitive_secret}\n",
            encoding="utf-8",
        )

        self.pdf_path = self.project / "papers" / "study.pdf"
        self.pdf_path.write_bytes(
            self.pdf_bytes(
                [
                    "First page contains exact source evidence.",
                    "Second page records a different result.",
                ]
            )
        )

        self.notebook_path = self.project / "notebooks" / "analysis.ipynb"
        self.notebook_path.write_text(
            json.dumps(
                {
                    "nbformat": 4,
                    "nbformat_minor": 5,
                    "metadata": {},
                    "cells": [
                        {
                            "cell_type": "markdown",
                            "id": "intro-cell",
                            "metadata": {},
                            "source": ["# Trial\n", "Context\n"],
                        },
                        {
                            "cell_type": "code",
                            "id": "result-cell",
                            "metadata": {},
                            "execution_count": 1,
                            "source": ["score = 0.9\n", "print(score)\n"],
                            "outputs": [],
                        },
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
            newline="\n",
        )

        self.workbook_path = self.project / "tables" / "results.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Results"
        sheet["A1"] = "metric"
        sheet["B1"] = "value"
        sheet["A2"] = "accuracy"
        sheet["B2"] = 0.875
        sheet["A3"] = datetime(2026, 7, 16, 12, 30, 45)
        sheet["B3"] = "=B2*100"
        sheet["A4"] = timedelta(days=1, seconds=2, microseconds=3)
        sheet["B4"] = True
        workbook.save(self.workbook_path)
        workbook.close()

        (self.project / "broken" / "bad.pdf").write_bytes(b"not a PDF")
        (self.project / "broken" / "bad.ipynb").write_bytes(b"{not-json")
        (self.project / "broken" / "surrogate.ipynb").write_bytes(
            b'{"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":['
            b'{"cell_type":"markdown","metadata":{},"source":["\\ud800"]}]}'
        )
        (self.project / "broken" / "bad.xlsx").write_bytes(b"not an OOXML workbook")

        self.registration = register_project(
            self.workspace,
            self.project,
            project_id="source-access-study",
        )
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)
        self.registry = load_source_registry(
            self.workspace,
            self.registration.project_id,
        )

    def source_for(self, relative_path: str):
        return self.registry.current_by_path[relative_path]

    def source_snapshot(self) -> tuple[tuple[str, ...], dict[str, tuple[str, int]]]:
        directories: list[str] = []
        files: dict[str, tuple[str, int]] = {}
        for directory, dirnames, filenames in os.walk(self.project):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            for name in dirnames:
                path = base / name
                if not path.is_symlink():
                    directories.append(path.relative_to(self.project).as_posix())
            for name in filenames:
                path = base / name
                if not path.is_symlink():
                    files[path.relative_to(self.project).as_posix()] = (
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        path.stat().st_mtime_ns,
                    )
        return tuple(sorted(directories)), files

    def test_locate_returns_current_path_version_and_hash_without_source_writes(self) -> None:
        source = self.source_for("src/model.py")
        before = self.source_snapshot()

        result = locate_source(
            self.workspace,
            self.registration.project_id,
            source.source_id,
        )

        self.assertEqual(result.project_id, self.registration.project_id)
        self.assertEqual(result.source_id, source.source_id)
        self.assertEqual(result.current_path, "src/model.py")
        self.assertEqual(result.absolute_path, self.code_path.resolve())
        self.assertEqual(result.current_version, 1)
        self.assertEqual(result.content_hash, hashlib.sha256(self.code_bytes).hexdigest())
        payload = result.as_dict()
        self.assertEqual(payload["kind"], SOURCE_LOCATION_KIND)
        self.assertEqual(payload["access_version"], SOURCE_ACCESS_VERSION)
        self.assertEqual(before, self.source_snapshot())

    def test_code_lines_reopen_with_exact_crlf_and_optional_hash_verification(self) -> None:
        source = self.source_for("src/model.py")
        excerpt = "beta = 2\r\ngamma = alpha + beta\r\n"

        result = open_source(
            self.workspace,
            self.registration.project_id,
            source_id=source.source_id,
            locator=LineRangeLocator(2, 3),
            expected_content_hash=source.current_content_hash,
            expected_excerpt_hash=excerpt_sha256(excerpt),
        )

        self.assertEqual(result.excerpt, excerpt)
        self.assertEqual(result.excerpt_hash, excerpt_sha256(excerpt))
        self.assertEqual(result.excerpt_format, "text-line-range")
        self.assertTrue(result.excerpt_hash_verified)
        payload = result.as_dict()
        self.assertEqual(payload["kind"], SOURCE_OPEN_KIND)
        self.assertTrue(payload["content_hash_verified"])
        self.assertEqual(payload["excerpt_encoding"], "utf-8")

        with self.assertRaises(SourceExcerptMismatchError):
            open_source(
                self.workspace,
                self.registration.project_id,
                source_id=source.source_id,
                locator=LineRangeLocator(1, 1),
                expected_excerpt_hash="not-a-sha256",
            )

    def test_pdf_page_notebook_cell_and_table_range_reopen_exactly(self) -> None:
        pdf_source = self.source_for("papers/study.pdf")
        pdf_expected = extract_pdf_bytes(
            self.pdf_path.read_bytes(),
            relative_path="papers/study.pdf",
        ).document
        assert pdf_expected is not None
        page_text = pdf_expected.blocks[1].text
        pdf_result = open_source(
            self.workspace,
            self.registration.project_id,
            source_id=pdf_source.source_id,
            locator=PdfPageLocator(2),
        )
        self.assertEqual(pdf_result.excerpt, page_text)
        self.assertEqual(pdf_result.excerpt_format, "pdf-extracted-page-text")

        notebook_source = self.source_for("notebooks/analysis.ipynb")
        notebook_result = open_source(
            self.workspace,
            self.registration.project_id,
            source_id=notebook_source.source_id,
            locator=NotebookCellLocator(1, "result-cell"),
        )
        self.assertEqual(notebook_result.excerpt, "score = 0.9\nprint(score)\n")
        self.assertEqual(notebook_result.excerpt_format, "notebook-cell-source")

        table_source = self.source_for("tables/results.xlsx")
        table_result = open_source(
            self.workspace,
            self.registration.project_id,
            source_id=table_source.source_id,
            locator=TableRangeLocator("Results", "A1", "B4"),
        )
        self.assertEqual(table_result.excerpt_format, TABLE_EXCERPT_FORMAT)
        self.assertEqual(
            json.loads(table_result.excerpt),
            [
                ["metric", "value"],
                ["accuracy", 0.875],
                [
                    {"type": "datetime", "value": "2026-07-16T12:30:45"},
                    "=B2*100",
                ],
                [{"microseconds": 86402000000, "type": "timedelta"}, True],
            ],
        )
        self.assertEqual(
            table_result.excerpt,
            json.dumps(
                json.loads(table_result.excerpt),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def test_open_evidence_verifies_content_and_excerpt_hashes(self) -> None:
        source = self.source_for("src/model.py")
        excerpt = "alpha = 1\r\nbeta = 2\r\n"
        registration = register_evidence(
            self.workspace,
            self.registration.project_id,
            source_id=source.source_id,
            content_hash=source.current_content_hash,
            locator=LineRangeLocator(1, 2),
            excerpt=excerpt,
        )
        before = self.source_snapshot()

        result = open_evidence(
            self.workspace,
            self.registration.project_id,
            registration.evidence.evidence_id,
        )

        self.assertEqual(result.excerpt, excerpt)
        self.assertEqual(result.evidence_id, registration.evidence.evidence_id)
        self.assertEqual(result.evidence_source_version, 1)
        self.assertTrue(result.excerpt_hash_verified)
        self.assertEqual(before, self.source_snapshot())

    def test_open_evidence_rejects_recurrent_content_from_newer_source_version(self) -> None:
        source = self.source_for("src/model.py")
        old = register_evidence(
            self.workspace,
            self.registration.project_id,
            source_id=source.source_id,
            content_hash=source.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt="alpha = 1\r\n",
        ).evidence

        self.code_path.write_bytes(b"changed = True\n")
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)
        self.code_path.write_bytes(self.code_bytes)
        inventory_project(self.workspace, self.registration.project_id)
        sync_source_registry(self.workspace, self.registration.project_id)

        current = load_source_registry(
            self.workspace,
            self.registration.project_id,
        ).current_by_path["src/model.py"]
        self.assertEqual(current.current_version, 3)
        self.assertEqual(current.current_content_hash, old.content_hash)
        with self.assertRaises(SourceVersionMismatchError) as raised:
            open_evidence(
                self.workspace,
                self.registration.project_id,
                old.evidence_id,
            )
        self.assertEqual(
            raised.exception.reason_code,
            "current-source-version-mismatch",
        )

        current_evidence = register_evidence(
            self.workspace,
            self.registration.project_id,
            source_id=current.source_id,
            content_hash=current.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt="alpha = 1\r\n",
        ).evidence
        self.assertEqual(current_evidence.source_version, 3)
        self.assertNotEqual(current_evidence.evidence_id, old.evidence_id)
        reopened = open_evidence(
            self.workspace,
            self.registration.project_id,
            current_evidence.evidence_id,
        )
        self.assertEqual(reopened.excerpt, "alpha = 1\r\n")

    def test_wrong_persisted_excerpt_changed_bytes_and_deleted_source_fail_explicitly(self) -> None:
        source = self.source_for("src/model.py")
        wrong = register_evidence(
            self.workspace,
            self.registration.project_id,
            source_id=source.source_id,
            content_hash=source.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt="not the source excerpt\n",
        )
        with self.assertRaises(SourceExcerptMismatchError):
            open_evidence(
                self.workspace,
                self.registration.project_id,
                wrong.evidence.evidence_id,
            )

        self.code_path.write_bytes(b"changed = True\n")
        with self.assertRaises(SourceContentMismatchError):
            open_source(
                self.workspace,
                self.registration.project_id,
                source_id=source.source_id,
                locator=LineRangeLocator(1, 1),
            )

        self.code_path.unlink()
        with self.assertRaises(SourceMissingError):
            locate_source(
                self.workspace,
                self.registration.project_id,
                source.source_id,
            )

    def test_locator_bounds_cell_id_sheet_and_malformed_sources_fail_closed(self) -> None:
        cases = [
            ("src/model.py", LineRangeLocator(4, 4), SourceLocatorError),
            ("papers/study.pdf", PdfPageLocator(3), SourceLocatorError),
            (
                "notebooks/analysis.ipynb",
                NotebookCellLocator(2),
                SourceLocatorError,
            ),
            (
                "notebooks/analysis.ipynb",
                NotebookCellLocator(1, "wrong-id"),
                SourceLocatorError,
            ),
            (
                "tables/results.xlsx",
                TableRangeLocator("Missing", "A1", "B2"),
                SourceLocatorError,
            ),
            (
                "tables/results.xlsx",
                TableRangeLocator("Results", "A1", "XFD7"),
                SourceLocatorError,
            ),
            (
                "tables/results.xlsx",
                TableRangeLocator("Results", "XFE1", "XFE1"),
                SourceLocatorError,
            ),
            ("broken/bad.pdf", PdfPageLocator(1), SourceFormatError),
            (
                "broken/bad.ipynb",
                NotebookCellLocator(0),
                SourceFormatError,
            ),
            (
                "broken/surrogate.ipynb",
                NotebookCellLocator(0),
                SourceFormatError,
            ),
            (
                "broken/bad.xlsx",
                TableRangeLocator("Sheet", "A1", "A1"),
                SourceFormatError,
            ),
        ]
        for relative_path, locator, error_type in cases:
            with self.subTest(path=relative_path, locator=locator):
                source = self.source_for(relative_path)
                with self.assertRaises(error_type):
                    open_source(
                        self.workspace,
                        self.registration.project_id,
                        source_id=source.source_id,
                        locator=locator,
                    )

    def test_locator_json_is_strict_and_future_versions_fail_closed(self) -> None:
        valid = LineRangeLocator(1, 1).as_dict()
        self.assertEqual(
            deserialize_locator(json.dumps(valid)),
            LineRangeLocator(1, 1),
        )
        bad_payloads = [
            "{}",
            '{"schema_version":1,"schema_version":1}',
            json.dumps({**valid, "schema_version": True}),
            json.dumps({**valid, "schema_version": EXTRACTION_SCHEMA_VERSION + 1}),
            '{"schema_version":1,"kind":NaN}',
            b"\xff",
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(SourceLocatorError):
                    deserialize_locator(payload)

    def test_unknown_source_and_evidence_ids_fail_explicitly(self) -> None:
        with self.assertRaises(SourceNotFoundError):
            locate_source(
                self.workspace,
                self.registration.project_id,
                "src-00000000000000000000000000000000",
            )
        source = self.source_for("src/model.py")
        register_evidence(
            self.workspace,
            self.registration.project_id,
            source_id=source.source_id,
            content_hash=source.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt="alpha = 1\r\n",
        )
        with self.assertRaises(SourceNotFoundError):
            open_evidence(
                self.workspace,
                self.registration.project_id,
                "evd-" + "0" * 64,
            )

    def test_manifest_content_policy_is_explicit_and_host_local_only_is_allowed(
        self,
    ) -> None:
        sensitive = self.source_for(".env")
        ordinary = self.source_for("src/model.py")
        before = self.source_snapshot()

        with self.assertRaises(SourceContentPolicyDeniedError) as denied:
            open_source(
                self.workspace,
                self.registration.project_id,
                source_id=sensitive.source_id,
                locator=LineRangeLocator(1, 1),
                expected_content_hash=sensitive.current_content_hash,
                enforce_content_policy=True,
            )
        self.assertNotIn(self.sensitive_secret, str(denied.exception))

        opened = open_source(
            self.workspace,
            self.registration.project_id,
            source_id=ordinary.source_id,
            locator=LineRangeLocator(1, 1),
            expected_content_hash=ordinary.current_content_hash,
            enforce_content_policy=True,
        )
        self.assertEqual(opened.excerpt, "alpha = 1\r\n")
        self.assertEqual(self.source_snapshot(), before)

    def test_manifest_content_policy_denies_all_restricted_states(self) -> None:
        source = self.source_for("src/model.py")
        manifest_file = self.registration.layout.manifest_file
        original_text = manifest_file.read_text(encoding="utf-8")
        records = [json.loads(line) for line in original_text.splitlines()]
        file_record = next(
            record for record in records if record.get("path") == "src/model.py"
        )
        states = (
            ("metadata_only", "content-size-limit"),
            ("metadata_only", "outside-scan-boundary"),
            ("ignored", "explicit-ignore"),
        )
        try:
            for read_depth, reason_code in states:
                with self.subTest(
                    read_depth=read_depth,
                    reason_code=reason_code,
                ):
                    file_record["file_state"] = {
                        "schema_version": 1,
                        "kind": "llmwiki-file-state",
                        "processing_status": "discovered",
                        "read_depth": read_depth,
                        "reason_code": reason_code,
                        "reason": "test-only current Manifest content restriction",
                    }
                    persisted_states = [
                        record["file_state"]
                        for record in records
                        if isinstance(record.get("file_state"), dict)
                    ]
                    summary = records[0]["file_state_summary"]
                    summary["processing_statuses"] = dict(
                        sorted(
                            Counter(
                                state["processing_status"]
                                for state in persisted_states
                            ).items()
                        )
                    )
                    summary["read_depths"] = dict(
                        sorted(
                            Counter(
                                state["read_depth"] for state in persisted_states
                            ).items()
                        )
                    )
                    summary["reasons"] = dict(
                        sorted(
                            Counter(
                                state["reason_code"] for state in persisted_states
                            ).items()
                        )
                    )
                    manifest_file.write_text(
                        "".join(
                            json.dumps(record, ensure_ascii=False, sort_keys=True)
                            + "\n"
                            for record in records
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(SourceContentPolicyDeniedError):
                        open_source(
                            self.workspace,
                            self.registration.project_id,
                            source_id=source.source_id,
                            locator=LineRangeLocator(1, 1),
                            expected_content_hash=source.current_content_hash,
                            enforce_content_policy=True,
                        )
        finally:
            manifest_file.write_text(original_text, encoding="utf-8")

    def test_symlink_escape_is_rejected_before_source_content_is_opened(self) -> None:
        source = self.source_for("src/model.py")
        outside = self.root / "outside.py"
        outside.write_bytes(self.code_bytes)
        self.code_path.unlink()
        try:
            os.symlink(outside, self.code_path)
        except OSError as exc:
            self.code_path.write_bytes(self.code_bytes)
            self.skipTest(f"symbolic links unavailable: {exc}")

        with self.assertRaises(SourceBoundaryError):
            locate_source(
                self.workspace,
                self.registration.project_id,
                source.source_id,
            )

    def test_cli_json_locate_open_and_error_results_are_machine_readable(self) -> None:
        source = self.source_for("src/model.py")
        locate_completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "source",
                "locate",
                self.registration.project_id,
                source.source_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.root,
        )
        locate_payload = json.loads(locate_completed.stdout)
        self.assertTrue(locate_payload["ok"])
        self.assertEqual(locate_payload["source_id"], source.source_id)
        self.assertEqual(locate_payload["current_path"], "src/model.py")

        locator_json = json.dumps(LineRangeLocator(2, 2).as_dict())
        open_completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "source",
                "open",
                self.registration.project_id,
                source.source_id,
                "--locator-json",
                locator_json,
                "--expected-content-hash",
                source.current_content_hash,
                "--expected-excerpt-hash",
                excerpt_sha256("beta = 2\r\n"),
                "--workspace-root",
                str(self.workspace),
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.root,
        )
        open_payload = json.loads(open_completed.stdout)
        self.assertTrue(open_payload["ok"])
        self.assertEqual(open_payload["excerpt"], "beta = 2\r\n")
        self.assertTrue(open_payload["excerpt_hash_verified"])

        evidence = register_evidence(
            self.workspace,
            self.registration.project_id,
            source_id=source.source_id,
            content_hash=source.current_content_hash,
            locator=LineRangeLocator(1, 1),
            excerpt="alpha = 1\r\n",
        ).evidence
        evidence_completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "source",
                "open",
                self.registration.project_id,
                evidence.evidence_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.root,
        )
        evidence_payload = json.loads(evidence_completed.stdout)
        self.assertEqual(evidence_payload["evidence_id"], evidence.evidence_id)
        self.assertTrue(evidence_payload["excerpt_hash_verified"])

        bad_pdf = self.source_for("broken/bad.pdf")
        malformed_completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "source",
                "open",
                self.registration.project_id,
                bad_pdf.source_id,
                "--locator-json",
                json.dumps(PdfPageLocator(1).as_dict()),
                "--workspace-root",
                str(self.workspace),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.root,
        )
        self.assertEqual(malformed_completed.returncode, 2)
        self.assertEqual(malformed_completed.stdout, "")
        malformed_payload = json.loads(malformed_completed.stderr)
        self.assertEqual(
            malformed_payload["reason_code"],
            "source-locator-format-unsupported",
        )

        error_completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "source",
                "open",
                self.registration.project_id,
                source.source_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.root,
        )
        self.assertEqual(error_completed.returncode, 2)
        error_payload = json.loads(error_completed.stderr)
        self.assertFalse(error_payload["ok"])
        self.assertEqual(error_payload["reason_code"], "source-locator-invalid")


if __name__ == "__main__":
    unittest.main()
