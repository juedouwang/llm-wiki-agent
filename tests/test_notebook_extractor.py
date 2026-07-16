from __future__ import annotations

import codecs
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.extraction_schema import (
    ExtractedDocument,
    NotebookCellLocator,
    serialize_extraction_result,
)
from tools.notebook_extractor import (
    NOTEBOOK_EXTRACTOR_NAME,
    NOTEBOOK_EXTRACTOR_VERSION,
    NotebookExtractionLimits,
    extract_notebook_bytes,
    extract_notebook_file,
)


class NotebookExtractorTests(unittest.TestCase):
    @staticmethod
    def notebook_bytes(cells: list[dict[str, object]], **metadata: object) -> bytes:
        notebook = {
            "cells": cells,
            "metadata": metadata,
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        return json.dumps(
            notebook,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")

    def require_document(self, result: object) -> ExtractedDocument:
        document = getattr(result, "document", None)
        self.assertIsInstance(document, ExtractedDocument)
        assert isinstance(document, ExtractedDocument)
        return document

    def test_cells_preserve_identity_type_execution_and_output_summary(self) -> None:
        image_payload = "A" * 200
        data = self.notebook_bytes(
            [
                {
                    "cell_type": "markdown",
                    "id": "intro-cell",
                    "metadata": {"tags": ["overview"]},
                    "source": ["# \u0422\u0435\u0441\u0442\n", "Context\n"],
                    "attachments": {},
                },
                {
                    "cell_type": "code",
                    "id": "run-cell",
                    "metadata": {"tags": ["experiment"]},
                    "execution_count": 7,
                    "source": "print(\'\u7ed3\u679c\')\n",
                    "outputs": [
                        {
                            "output_type": "stream",
                            "name": "stdout",
                            "text": ["\u7ed3\u679c\n"],
                        },
                        {
                            "output_type": "display_data",
                            "data": {
                                "image/png": image_payload,
                                "image/svg+xml": "<svg>secret-image</svg>",
                                "text/plain": "<Figure size 10x10>",
                            },
                            "metadata": {"isolated": True},
                        },
                    ],
                },
                {
                    "cell_type": "raw",
                    "metadata": {},
                    "source": "raw notes\n",
                },
            ],
            kernelspec={"name": "python3"},
        )

        result = extract_notebook_bytes(data, relative_path="notebooks/run.ipynb")

        self.assertEqual(result.status, "processed")
        document = self.require_document(result)
        self.assertEqual(document.extractor, NOTEBOOK_EXTRACTOR_NAME)
        self.assertEqual(document.extractor_version, NOTEBOOK_EXTRACTOR_VERSION)
        self.assertEqual(document.encoding, "utf-8")
        self.assertEqual(
            [block.block_id for block in document.blocks],
            [
                "cell-0-source",
                "cell-1-source",
                "cell-1-outputs",
                "cell-2-source",
            ],
        )
        self.assertEqual(
            [block.block_type for block in document.blocks],
            ["markdown", "code", "output_summary", "text"],
        )
        self.assertEqual(
            [block.locator for block in document.blocks],
            [
                NotebookCellLocator(0, "intro-cell"),
                NotebookCellLocator(1, "run-cell"),
                NotebookCellLocator(1, "run-cell"),
                NotebookCellLocator(2, None),
            ],
        )
        self.assertEqual(document.blocks[0].text, "# \u0422\u0435\u0441\u0442\nContext\n")
        self.assertEqual(document.blocks[1].metadata["execution_count"], 7)
        output_summary = json.loads(document.blocks[2].text)
        self.assertEqual(output_summary[0]["name"], "stdout")
        image_record = output_summary[1]["data"]["items"]["image/png"]
        self.assertTrue(image_record["payload_omitted"])
        self.assertEqual(image_record["character_count"], len(image_payload))
        self.assertNotIn(image_payload, document.blocks[2].text)
        svg_record = output_summary[1]["data"]["items"]["image/svg+xml"]
        self.assertTrue(svg_record["payload_omitted"])
        self.assertNotIn("secret-image", document.blocks[2].text)
        self.assertEqual(document.metadata["cell_count"], 3)
        self.assertEqual(
            document.metadata["cell_type_counts"],
            {"code": 1, "markdown": 1, "raw": 1},
        )
        self.assertEqual(document.metadata["cells_with_ids"], 2)
        self.assertEqual(document.metadata["output_summary_block_count"], 1)

    def test_modifying_one_cell_changes_only_its_source_block(self) -> None:
        cells = [
            {
                "cell_type": "markdown",
                "id": "a",
                "metadata": {},
                "source": "alpha\n",
            },
            {
                "cell_type": "code",
                "id": "b",
                "metadata": {},
                "execution_count": None,
                "source": "value = 1\n",
                "outputs": [],
            },
            {
                "cell_type": "raw",
                "id": "c",
                "metadata": {},
                "source": "omega\n",
            },
        ]
        changed_cells = [dict(cell) for cell in cells]
        changed_cells[1] = {**changed_cells[1], "source": "value = 2\n"}

        first = self.require_document(
            extract_notebook_bytes(
                self.notebook_bytes(cells),
                relative_path="trial.ipynb",
            )
        )
        second = self.require_document(
            extract_notebook_bytes(
                self.notebook_bytes(changed_cells),
                relative_path="trial.ipynb",
            )
        )
        first_blocks = {block.block_id: block.as_dict() for block in first.blocks}
        second_blocks = {block.block_id: block.as_dict() for block in second.blocks}

        self.assertEqual(first_blocks["cell-0-source"], second_blocks["cell-0-source"])
        self.assertNotEqual(first_blocks["cell-1-source"], second_blocks["cell-1-source"])
        self.assertEqual(first_blocks["cell-2-source"], second_blocks["cell-2-source"])

    def test_large_cell_and_text_output_are_partial_with_stable_reasons(self) -> None:
        data = self.notebook_bytes(
            [
                {
                    "cell_type": "code",
                    "id": "large",
                    "metadata": {},
                    "execution_count": 3,
                    "source": "0123456789",
                    "outputs": [
                        {
                            "output_type": "stream",
                            "name": "stdout",
                            "text": "abcdefghijklmnopqrstuvwxyz",
                        }
                    ],
                }
            ]
        )
        result = extract_notebook_bytes(
            data,
            relative_path="large.ipynb",
            limits=NotebookExtractionLimits(
                max_cell_characters=5,
                max_output_text_characters=6,
            ),
        )

        self.assertEqual(result.status, "partial")
        document = self.require_document(result)
        source, output = document.blocks
        self.assertEqual(source.text, "01234")
        self.assertTrue(source.truncated)
        self.assertEqual(source.truncation_reason_code, "cell-character-limit")
        self.assertTrue(output.truncated)
        self.assertEqual(output.truncation_reason_code, "output-text-character-limit")
        summary = json.loads(output.text)
        self.assertEqual(summary[0]["text"]["preview"], "abcdef")
        self.assertEqual(summary[0]["text"]["character_count"], 26)
        self.assertEqual(
            document.metadata["truncation_reasons"],
            ["cell-character-limit", "output-text-character-limit"],
        )

    def test_output_count_and_summary_limits_remain_explicit(self) -> None:
        outputs = [
            {"output_type": "stream", "name": "stdout", "text": f"item-{index}"}
            for index in range(4)
        ]
        result = extract_notebook_bytes(
            self.notebook_bytes(
                [
                    {
                        "cell_type": "code",
                        "id": "many",
                        "metadata": {},
                        "execution_count": 1,
                        "source": "pass\n",
                        "outputs": outputs,
                    }
                ]
            ),
            relative_path="many.ipynb",
            limits=NotebookExtractionLimits(
                max_outputs_per_cell=2,
                max_output_summary_characters=40,
            ),
        )

        self.assertEqual(result.status, "partial")
        output = self.require_document(result).blocks[1]
        self.assertEqual(len(output.text), 40)
        self.assertEqual(output.truncation_reason_code, "multiple-output-limits")
        self.assertEqual(
            [item["code"] for item in output.metadata["truncation_reasons"]],
            ["output-count-limit", "output-summary-character-limit"],
        )
        self.assertEqual(output.metadata["output_count"], 4)
        self.assertEqual(output.metadata["included_output_count"], 2)

    def test_utf8_bom_is_supported_and_recorded(self) -> None:
        data = codecs.BOM_UTF8 + self.notebook_bytes(
            [
                {
                    "cell_type": "markdown",
                    "id": "bom",
                    "metadata": {},
                    "source": "title\n",
                }
            ]
        )
        result = extract_notebook_bytes(data, relative_path="bom.ipynb")

        self.assertEqual(result.status, "processed")
        document = self.require_document(result)
        self.assertEqual(document.encoding, "utf-8-sig")
        self.assertEqual(document.blocks[0].text, "title\n")

    def test_malformed_duplicate_and_invalid_structure_fail_without_document(self) -> None:
        cases = {
            "malformed": b'{"cells": [}',
            "duplicate": (
                b'{"cells": [], "cells": [], "metadata": {}, '
                b'"nbformat": 4, "nbformat_minor": 5}'
            ),
            "bad cell": self.notebook_bytes(
                [
                    {
                        "cell_type": "heading",
                        "metadata": {},
                        "source": "legacy",
                    }
                ]
            ),
            "duplicate cell id": self.notebook_bytes(
                [
                    {
                        "cell_type": "raw",
                        "id": "same",
                        "metadata": {},
                        "source": "a",
                    },
                    {
                        "cell_type": "raw",
                        "id": "same",
                        "metadata": {},
                        "source": "b",
                    },
                ]
            ),
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                result = extract_notebook_bytes(data, relative_path="bad.ipynb")
                self.assertEqual(result.status, "failed")
                self.assertIsNone(result.document)
                self.assertIn(
                    result.reason_code,
                    {"notebook-json-invalid", "notebook-structure-invalid"},
                )

    def test_unsupported_notebook_version_fails_closed(self) -> None:
        data = json.dumps(
            {
                "cells": [],
                "metadata": {},
                "nbformat": 5,
                "nbformat_minor": 0,
            }
        ).encode("utf-8")
        result = extract_notebook_bytes(data, relative_path="future.ipynb")

        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.reason_code, "unsupported-notebook-version")
        self.assertIsNone(result.document)

    def test_source_byte_limit_fails_without_parsing_a_prefix(self) -> None:
        data = self.notebook_bytes(
            [
                {
                    "cell_type": "raw",
                    "metadata": {},
                    "source": "x" * 100,
                }
            ]
        )
        result = extract_notebook_bytes(
            data,
            relative_path="oversized.ipynb",
            limits=NotebookExtractionLimits(max_source_bytes=32),
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason_code, "notebook-source-byte-limit")
        self.assertIsNone(result.document)

    def test_file_api_is_read_only_and_verifies_full_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "experiment.ipynb"
            data = self.notebook_bytes(
                [
                    {
                        "cell_type": "code",
                        "id": "run",
                        "metadata": {},
                        "execution_count": None,
                        "source": "value = 1\n",
                        "outputs": [],
                    }
                ]
            )
            source.write_bytes(data)
            fixed_ns = 1_700_000_000_123_456_700
            os.utime(source, ns=(fixed_ns, fixed_ns))
            before = source.stat()
            before_entries = sorted(item.name for item in root.iterdir())
            expected_hash = hashlib.sha256(data).hexdigest()

            result = extract_notebook_file(
                source,
                relative_path="experiment.ipynb",
                expected_sha256=expected_hash,
                limits=NotebookExtractionLimits(read_chunk_bytes=7),
            )

            after = source.stat()
            self.assertEqual(result.status, "processed")
            document = self.require_document(result)
            self.assertEqual(document.content_sha256, expected_hash)
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(after.st_size, before.st_size)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(sorted(item.name for item in root.iterdir()), before_entries)

    def test_hash_mismatch_read_failure_and_unsupported_format_do_not_fabricate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "trial.ipynb"
            source.write_bytes(self.notebook_bytes([]))
            mismatch = extract_notebook_file(
                source,
                relative_path="trial.ipynb",
                expected_sha256="0" * 64,
            )
            missing = extract_notebook_file(
                source.with_name("missing.ipynb"),
                relative_path="missing.ipynb",
            )

        unsupported = extract_notebook_file(
            "does-not-exist.pdf",
            relative_path="does-not-exist.pdf",
            format_value="pdf",
        )
        self.assertEqual(mismatch.reason_code, "content-hash-mismatch")
        self.assertEqual(missing.reason_code, "source-read-failed")
        self.assertEqual(unsupported.status, "unsupported")
        for result in (mismatch, missing, unsupported):
            self.assertIsNone(result.document)

    def test_invalid_output_structure_fails_without_partial_document(self) -> None:
        data = self.notebook_bytes(
            [
                {
                    "cell_type": "code",
                    "id": "bad-output",
                    "metadata": {},
                    "execution_count": 1,
                    "source": "pass\n",
                    "outputs": [{"output_type": "mystery"}],
                }
            ]
        )
        result = extract_notebook_bytes(data, relative_path="bad-output.ipynb")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason_code, "notebook-structure-invalid")
        self.assertIsNone(result.document)

    def test_repeated_extraction_serializes_byte_stably(self) -> None:
        data = self.notebook_bytes(
            [
                {
                    "cell_type": "markdown",
                    "id": "stable",
                    "metadata": {"tags": ["x"]},
                    "source": "stable\n",
                }
            ],
            language_info={"name": "python"},
        )
        first = extract_notebook_bytes(data, relative_path="stable.ipynb")
        second = extract_notebook_bytes(data, relative_path="stable.ipynb")

        self.assertEqual(
            serialize_extraction_result(first).encode("utf-8"),
            serialize_extraction_result(second).encode("utf-8"),
        )

    def test_limit_and_hash_arguments_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            NotebookExtractionLimits(max_source_bytes=3)
        with self.assertRaises(ValueError):
            NotebookExtractionLimits(max_outputs_per_cell=0)
        with self.assertRaises(ValueError):
            extract_notebook_file(
                "unused.ipynb",
                relative_path="unused.ipynb",
                expected_sha256="A" * 64,
            )


if __name__ == "__main__":
    unittest.main()
