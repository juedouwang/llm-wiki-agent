from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from tools.chunking import ChunkingLimits, chunk_document, validate_chunk_coverage
from tools.coverage_report import generate_coverage_report
from tools.evidence_registry import load_evidence_registry, register_evidence
from tools.extraction_schema import (
    LineRangeLocator,
    NotebookCellLocator,
    PdfPageLocator,
)
from tools.file_classification import classification_from_dict
from tools.file_state import file_state_from_dict
from tools.notebook_extractor import extract_notebook_file
from tools.pdf_extractor import extract_pdf_file
from tools.project_inventory import (
    PROJECT_MANIFEST_VERSION,
    inventory_project,
    load_project_manifest,
)
from tools.project_registry import register_project
from tools.source_access import open_evidence
from tools.source_health import evaluate_source_health
from tools.source_recovery import recover_source
from tools.source_registry import load_source_registry, sync_source_registry
from tools.text_extractor import extract_text_file


PROJECT_ID = "r1-basic-chain-study"
CODE_PATH = "src/model.py"
MOVED_CODE_PATH = "archive/model.py"
NOTEBOOK_PATH = "notebooks/analysis.ipynb"
PDF_PATH = "papers/study.pdf"
OPAQUE_PATH = "artifacts/blob.weird"
INITIAL_FILE_PATHS = tuple(sorted((CODE_PATH, NOTEBOOK_PATH, PDF_PATH, OPAQUE_PATH)))
MOVED_FILE_PATHS = tuple(
    sorted((MOVED_CODE_PATH, NOTEBOOK_PATH, PDF_PATH, OPAQUE_PATH))
)
_SOURCE_ID_PATTERN = re.compile(r"src-[0-9a-f]{32}")


class R1EndToEndAcceptanceTests(unittest.TestCase):
    @staticmethod
    def _pdf_literal(text: str) -> bytes:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return escaped.encode("latin-1")

    @classmethod
    def _pdf_bytes(cls, pages: tuple[str, ...]) -> bytes:
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

    @classmethod
    def _write_fixture(cls, project: Path) -> None:
        for relative_directory in (
            "archive",
            "artifacts",
            "notebooks",
            "papers",
            "src",
        ):
            (project / relative_directory).mkdir(parents=True, exist_ok=True)

        (project / CODE_PATH).write_bytes(
            b"alpha = 1\nbeta = 2\ngamma = alpha + beta\n"
        )
        (project / NOTEBOOK_PATH).write_text(
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
        (project / PDF_PATH).write_bytes(
            cls._pdf_bytes(
                (
                    "First page contains exact source evidence for the R1 acceptance chain.",
                    "Second page records a deterministic result with a page locator.",
                )
            )
        )
        (project / OPAQUE_PATH).write_bytes(b"\x00\x01\x02opaque")

    @staticmethod
    def _independent_regular_files(project: Path) -> tuple[str, ...]:
        found: list[str] = []
        pending = [project]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                sorted_entries = sorted(entries, key=lambda entry: entry.name)
            for entry in sorted_entries:
                path = Path(entry.path)
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(metadata.st_mode):
                    found.append(path.relative_to(project).as_posix())
        return tuple(sorted(found))

    @staticmethod
    def _source_snapshot(
        project: Path,
    ) -> tuple[tuple[str, ...], dict[str, tuple[str, int, int, int]]]:
        directories: list[str] = []
        files: dict[str, tuple[str, int, int, int]] = {}
        for directory, dirnames, filenames in os.walk(project):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            for name in dirnames:
                path = base / name
                if not path.is_symlink():
                    directories.append(path.relative_to(project).as_posix())
            for name in filenames:
                path = base / name
                metadata = path.lstat()
                if stat.S_ISREG(metadata.st_mode):
                    files[path.relative_to(project).as_posix()] = (
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        stat.S_IMODE(metadata.st_mode),
                    )
        return tuple(sorted(directories)), files

    def _assert_only_controlled_rename(
        self,
        before: tuple[tuple[str, ...], dict[str, tuple[str, int, int, int]]],
        after: tuple[tuple[str, ...], dict[str, tuple[str, int, int, int]]],
    ) -> None:
        before_directories, before_files = before
        after_directories, after_files = after
        self.assertEqual(after_directories, before_directories)
        self.assertEqual(set(before_files) - set(after_files), {CODE_PATH})
        self.assertEqual(set(after_files) - set(before_files), {MOVED_CODE_PATH})
        for path in sorted(set(before_files) & set(after_files)):
            self.assertEqual(after_files[path], before_files[path], path)
        self.assertEqual(after_files[MOVED_CODE_PATH], before_files[CODE_PATH])

    def _load_manifest(self, registration, inventory):
        return load_project_manifest(
            inventory.manifest_file,
            project_id=registration.project_id,
            project_root=registration.project_root,
            required_manifest_version=PROJECT_MANIFEST_VERSION,
        )

    def _assert_manifest_reconciles(
        self,
        project: Path,
        manifest,
        expected_paths: tuple[str, ...],
    ) -> dict[str, dict[str, object]]:
        independent_paths = self._independent_regular_files(project)
        records_by_path = {record["path"]: record for record in manifest.file_records}
        self.assertEqual(independent_paths, expected_paths)
        self.assertEqual(tuple(sorted(records_by_path)), independent_paths)
        self.assertEqual(manifest.summary["record_counts"]["file"], len(expected_paths))
        self.assertEqual(len(manifest.file_records), len(expected_paths))

        expected_state = {
            CODE_PATH: ("python", "discovered", "sampled", "classification-sample"),
            MOVED_CODE_PATH: (
                "python",
                "discovered",
                "sampled",
                "classification-sample",
            ),
            NOTEBOOK_PATH: (
                "notebook",
                "discovered",
                "sampled",
                "classification-sample",
            ),
            PDF_PATH: ("pdf", "discovered", "sampled", "classification-sample"),
            OPAQUE_PATH: (
                "unknown",
                "discovered",
                "unsupported",
                "unsupported-format",
            ),
        }
        for path, record in records_by_path.items():
            classification = classification_from_dict(record["classification"])
            state = file_state_from_dict(record["file_state"])
            expected = expected_state[path]
            self.assertEqual(classification.format, expected[0], path)
            self.assertEqual(state.processing_status, expected[1], path)
            self.assertEqual(state.read_depth, expected[2], path)
            self.assertEqual(state.reason_code, expected[3], path)
            self.assertTrue(state.reason.strip(), path)
            self.assertRegex(record["content_sha256"], r"[0-9a-f]{64}")

        state_summary = manifest.summary["file_state_summary"]
        self.assertEqual(state_summary["state_files"], len(expected_paths))
        for axis in ("processing_statuses", "read_depths", "reasons"):
            self.assertEqual(sum(state_summary[axis].values()), len(expected_paths))
        return records_by_path

    def _exercise_chain(self, root: Path) -> dict[str, object]:
        workspace = root / "assistant-workspace"
        project = root / "research-project"
        project.mkdir()
        self._write_fixture(project)
        initial_snapshot = self._source_snapshot(project)
        self.assertEqual(self._independent_regular_files(project), INITIAL_FILE_PATHS)

        registration = register_project(
            workspace,
            project,
            project_id=PROJECT_ID,
        )
        inventory = inventory_project(workspace, registration.project_id)
        manifest = self._load_manifest(registration, inventory)
        records_by_path = self._assert_manifest_reconciles(
            project,
            manifest,
            INITIAL_FILE_PATHS,
        )

        coverage = generate_coverage_report(workspace, registration.project_id)
        coverage_bytes = coverage.report_file.read_bytes()
        repeated_coverage = generate_coverage_report(workspace, registration.project_id)
        self.assertEqual(repeated_coverage.report_file.read_bytes(), coverage_bytes)
        self.assertEqual(coverage.report["totals"]["file_count"], len(INITIAL_FILE_PATHS))
        self.assertEqual(coverage.report["totals"]["failed_file_count"], 0)
        for axis in (
            "research_role",
            "processing_status",
            "read_depth",
            "reason",
        ):
            self.assertEqual(
                coverage.report["reconciliation"]["axes"][axis]["file_count"],
                len(INITIAL_FILE_PATHS),
            )

        first_sync = sync_source_registry(workspace, registration.project_id)
        self.assertEqual(first_sync.assigned_count, len(INITIAL_FILE_PATHS))
        self.assertEqual(first_sync.versions_added_count, len(INITIAL_FILE_PATHS))
        source_registry = load_source_registry(workspace, registration.project_id)
        source_ids_by_path = {
            path: record.source_id
            for path, record in source_registry.current_by_path.items()
        }
        self.assertEqual(tuple(sorted(source_ids_by_path)), INITIAL_FILE_PATHS)
        self.assertEqual(len(set(source_ids_by_path.values())), len(INITIAL_FILE_PATHS))
        for source_id in source_ids_by_path.values():
            self.assertRegex(source_id, _SOURCE_ID_PATTERN)

        source_registry_bytes = source_registry.sources_file.read_bytes()
        second_sync = sync_source_registry(workspace, registration.project_id)
        self.assertFalse(second_sync.wrote_registry)
        self.assertEqual(second_sync.assigned_count, 0)
        self.assertEqual(second_sync.versions_added_count, 0)
        self.assertEqual(source_registry.sources_file.read_bytes(), source_registry_bytes)
        stable_registry = load_source_registry(workspace, registration.project_id)
        self.assertEqual(
            {
                path: record.source_id
                for path, record in stable_registry.current_by_path.items()
            },
            source_ids_by_path,
        )

        code_result = extract_text_file(
            project / CODE_PATH,
            relative_path=CODE_PATH,
            format_value="python",
            expected_sha256=records_by_path[CODE_PATH]["content_sha256"],
        )
        notebook_result = extract_notebook_file(
            project / NOTEBOOK_PATH,
            relative_path=NOTEBOOK_PATH,
            expected_sha256=records_by_path[NOTEBOOK_PATH]["content_sha256"],
        )
        pdf_result = extract_pdf_file(
            project / PDF_PATH,
            relative_path=PDF_PATH,
            expected_sha256=records_by_path[PDF_PATH]["content_sha256"],
        )
        extraction_results = {
            CODE_PATH: code_result,
            NOTEBOOK_PATH: notebook_result,
            PDF_PATH: pdf_result,
        }
        for path, result in extraction_results.items():
            self.assertEqual(result.status, "processed", path)
            self.assertIsNotNone(result.document, path)
            self.assertEqual(result.document.path, path)
            self.assertEqual(
                result.document.content_sha256,
                records_by_path[path]["content_sha256"],
            )

        assert code_result.document is not None
        code_chunked = chunk_document(
            code_result.document,
            limits=ChunkingLimits(max_chunk_utf8_bytes=32),
        )
        validate_chunk_coverage(code_result.document, code_chunked)
        self.assertEqual(
            tuple(chunk.locator for chunk in code_chunked.chunks),
            (LineRangeLocator(1, 2), LineRangeLocator(3, 3)),
        )
        code_chunk = code_chunked.chunks[1]
        self.assertEqual(code_chunk.source_block_locator, LineRangeLocator(1, 3))
        self.assertEqual(code_chunk.text, "gamma = alpha + beta\n")

        assert notebook_result.document is not None
        notebook_chunked = chunk_document(
            notebook_result.document,
            limits=ChunkingLimits(max_chunk_utf8_bytes=256),
        )
        validate_chunk_coverage(notebook_result.document, notebook_chunked)
        self.assertEqual(
            tuple(chunk.locator for chunk in notebook_chunked.chunks),
            (
                NotebookCellLocator(0, "intro-cell"),
                NotebookCellLocator(1, "result-cell"),
            ),
        )
        notebook_chunk = notebook_chunked.chunks[1]
        self.assertEqual(notebook_chunk.locator, notebook_chunk.source_block_locator)
        self.assertEqual(notebook_chunk.text, "score = 0.9\nprint(score)\n")

        assert pdf_result.document is not None
        pdf_chunked = chunk_document(
            pdf_result.document,
            limits=ChunkingLimits(max_chunk_utf8_bytes=256),
        )
        validate_chunk_coverage(pdf_result.document, pdf_chunked)
        self.assertEqual(
            tuple(chunk.locator for chunk in pdf_chunked.chunks),
            (PdfPageLocator(1), PdfPageLocator(2)),
        )
        pdf_chunk = pdf_chunked.chunks[1]
        self.assertEqual(pdf_chunk.locator, pdf_chunk.source_block_locator)
        self.assertEqual(
            pdf_chunk.text,
            "Second page records a deterministic result with a page locator.",
        )

        selected_chunks = {
            CODE_PATH: code_chunk,
            NOTEBOOK_PATH: notebook_chunk,
            PDF_PATH: pdf_chunk,
        }
        evidence_by_path = {}
        for path, chunk in selected_chunks.items():
            source = stable_registry.current_by_path[path]
            assert source.current_content_hash is not None
            evidence_by_path[path] = register_evidence(
                workspace,
                registration.project_id,
                source_id=source.source_id,
                source_version=source.current_version,
                content_hash=source.current_content_hash,
                locator=chunk.locator,
                excerpt=chunk.text,
            ).evidence

        evidence_registry = load_evidence_registry(workspace, registration.project_id)
        self.assertEqual(len(evidence_registry.records), len(selected_chunks))
        opened_formats: dict[str, str] = {}
        for path, evidence in evidence_by_path.items():
            opened = open_evidence(
                workspace,
                registration.project_id,
                evidence.evidence_id,
            )
            self.assertEqual(opened.source.source_id, source_ids_by_path[path])
            self.assertEqual(opened.source.current_path, path)
            self.assertEqual(opened.locator, selected_chunks[path].locator)
            self.assertEqual(opened.excerpt, selected_chunks[path].text)
            self.assertTrue(opened.excerpt_hash_verified)
            opened_formats[path] = opened.excerpt_format

        self.assertEqual(self._source_snapshot(project), initial_snapshot)

        (project / CODE_PATH).rename(project / MOVED_CODE_PATH)
        after_controlled_rename = self._source_snapshot(project)
        self._assert_only_controlled_rename(initial_snapshot, after_controlled_rename)

        moved_inventory = inventory_project(workspace, registration.project_id)
        moved_manifest = self._load_manifest(registration, moved_inventory)
        self._assert_manifest_reconciles(project, moved_manifest, MOVED_FILE_PATHS)
        self.assertEqual(moved_inventory.scan_generation, inventory.scan_generation + 1)

        code_source_id = source_ids_by_path[CODE_PATH]
        recovery = recover_source(
            workspace,
            registration.project_id,
            code_source_id,
        )
        self.assertEqual(recovery.status, "recovered")
        self.assertEqual(recovery.recovery_method, "content-hash")
        self.assertEqual(recovery.previous_path, CODE_PATH)
        self.assertEqual(recovery.current_path, MOVED_CODE_PATH)
        self.assertEqual(recovery.candidate_paths, (MOVED_CODE_PATH,))
        self.assertTrue(recovery.wrote_registry)

        recovered_registry = load_source_registry(workspace, registration.project_id)
        recovered_code = recovered_registry.by_source_id[code_source_id]
        self.assertEqual(recovered_code.source_id, code_source_id)
        self.assertEqual(recovered_code.current_path, MOVED_CODE_PATH)
        self.assertEqual(recovered_code.current_version, 1)
        self.assertEqual(
            tuple(path_record.path for path_record in recovered_code.path_history),
            tuple(sorted((CODE_PATH, MOVED_CODE_PATH))),
        )
        self.assertEqual(
            recovered_registry.current_by_path[MOVED_CODE_PATH].source_id,
            code_source_id,
        )
        for path in (NOTEBOOK_PATH, PDF_PATH, OPAQUE_PATH):
            self.assertEqual(
                recovered_registry.current_by_path[path].source_id,
                source_ids_by_path[path],
            )

        moved_code_evidence = evidence_by_path[CODE_PATH]
        reopened_after_move = open_evidence(
            workspace,
            registration.project_id,
            moved_code_evidence.evidence_id,
        )
        self.assertEqual(reopened_after_move.source.source_id, code_source_id)
        self.assertEqual(reopened_after_move.source.current_path, MOVED_CODE_PATH)
        self.assertEqual(reopened_after_move.excerpt, code_chunk.text)
        self.assertTrue(reopened_after_move.excerpt_hash_verified)

        health = evaluate_source_health(workspace, registration.project_id)
        self.assertEqual(health.overall_status, "valid")
        self.assertEqual(health.source_registry_count, len(INITIAL_FILE_PATHS))
        self.assertEqual(health.evidence_registry_count, len(selected_chunks))
        self.assertEqual(health.source_status_counts["valid"], len(INITIAL_FILE_PATHS))
        self.assertEqual(health.evidence_status_counts["valid"], len(selected_chunks))
        self.assertEqual(health.recovery_write_count, 0)
        self.assertEqual(
            next(record for record in health.sources if record.source_id == code_source_id).current_path,
            MOVED_CODE_PATH,
        )
        self.assertEqual(
            next(
                record
                for record in health.evidence
                if record.evidence_id == moved_code_evidence.evidence_id
            ).status,
            "valid",
        )
        repeated_health = evaluate_source_health(workspace, registration.project_id)
        self.assertEqual(repeated_health.as_dict(), health.as_dict())

        self._assert_only_controlled_rename(
            initial_snapshot,
            self._source_snapshot(project),
        )
        self.assertFalse((project / ".llmwiki").exists())
        self.assertFalse((project / "wiki").exists())

        return {
            "initial_paths": INITIAL_FILE_PATHS,
            "moved_paths": MOVED_FILE_PATHS,
            "file_states": tuple(
                sorted(
                    (
                        path,
                        file_state_from_dict(record["file_state"]).processing_status,
                        file_state_from_dict(record["file_state"]).read_depth,
                        file_state_from_dict(record["file_state"]).reason_code,
                    )
                    for path, record in records_by_path.items()
                )
            ),
            "locator_chain": {
                CODE_PATH: tuple(chunk.locator.as_dict() for chunk in code_chunked.chunks),
                NOTEBOOK_PATH: tuple(
                    chunk.locator.as_dict() for chunk in notebook_chunked.chunks
                ),
                PDF_PATH: tuple(chunk.locator.as_dict() for chunk in pdf_chunked.chunks),
            },
            "opened_formats": tuple(sorted(opened_formats.items())),
            "source_count": len(source_ids_by_path),
            "evidence_count": len(evidence_by_path),
            "recovery": (
                recovery.status,
                recovery.recovery_method,
                recovery.previous_path,
                recovery.current_path,
            ),
            "health": (
                health.overall_status,
                tuple(sorted(health.source_status_counts.items())),
                tuple(sorted(health.evidence_status_counts.items())),
            ),
        }

    def test_r1_basic_chain_is_repeatable_local_only_and_source_read_only(self) -> None:
        summaries: list[dict[str, object]] = []
        for run_number in range(2):
            with tempfile.TemporaryDirectory(
                prefix=f"llmwiki-r1-e2e-{run_number + 1}-"
            ) as temp_dir:
                with ExitStack() as stack:
                    guards = (
                        stack.enter_context(
                            patch(
                                "tools._utils.call_llm",
                                side_effect=AssertionError(
                                    "R1 deterministic acceptance attempted an LLM call"
                                ),
                            )
                        ),
                        stack.enter_context(
                            patch(
                                "socket.create_connection",
                                side_effect=AssertionError(
                                    "R1 deterministic acceptance attempted network access"
                                ),
                            )
                        ),
                        stack.enter_context(
                            patch(
                                "urllib.request.urlopen",
                                side_effect=AssertionError(
                                    "R1 deterministic acceptance attempted URL access"
                                ),
                            )
                        ),
                        stack.enter_context(
                            patch(
                                "webbrowser.open",
                                side_effect=AssertionError(
                                    "R1 deterministic acceptance attempted Web UI behavior"
                                ),
                            )
                        ),
                    )
                    summaries.append(self._exercise_chain(Path(temp_dir)))
                for guard in guards:
                    guard.assert_not_called()

        self.assertEqual(summaries[0], summaries[1])
