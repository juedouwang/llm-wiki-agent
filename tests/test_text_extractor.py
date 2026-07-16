from __future__ import annotations

import codecs
import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.extraction_schema import (
    ExtractionResult,
    LineRangeLocator,
    serialize_extraction_result,
)
from tools.text_extractor import (
    SUPPORTED_TEXT_FORMATS,
    TextExtractionLimits,
    extract_text_bytes,
    extract_text_file,
)


class TextExtractorTests(unittest.TestCase):
    @staticmethod
    def reconstructed_text(result: ExtractionResult) -> str:
        document = result.document
        assert document is not None
        return "".join(block.text for block in document.blocks)

    def test_multilingual_source_is_reconstructed_with_one_based_line_blocks(
        self,
    ) -> None:
        fixtures = {
            "python": (
                "def greet(name):\n"
                "    return f\"\u4f60\u597d, {name} \u2014 caf\u00e9 \U0001f9ea\"\n"
                "print(greet(\"\u4e16\u754c\"))\n"
            ),
            "cpp": (
                "// \u6e29\u5ea6 \u03bc and \u03bb\n"
                "const char* label = u8\"\u03b3\u03b5\u03b9\u03ac\";\n"
                "return 0;\n"
            ),
            "r": (
                "values <- c(1, 2, 3)\n"
                "message(\"\u5206\u6790 \u2713\")\n"
                "mean(values)\n"
            ),
        }
        limits = TextExtractionLimits(max_block_lines=2)

        for format_value, text in fixtures.items():
            with self.subTest(format_value=format_value):
                result = extract_text_bytes(
                    text.encode("utf-8"),
                    relative_path=f"src/example.{format_value}",
                    format_value=format_value,
                    limits=limits,
                )
                self.assertEqual(result.status, "processed")
                self.assertIsNotNone(result.document)
                document = result.document
                assert document is not None
                self.assertEqual(document.encoding, "utf-8")
                self.assertEqual(self.reconstructed_text(result), text)
                self.assertEqual(
                    [block.locator for block in document.blocks],
                    [LineRangeLocator(1, 2), LineRangeLocator(3, 3)],
                )
                self.assertTrue(
                    all(block.block_type == "code" for block in document.blocks)
                )

    def test_common_encodings_are_detected_and_preserved(self) -> None:
        fixtures = (
            (
                "cp1252",
                b"caf\xe9 \x96 r\xe9sum\xe9\r\n",
                "caf\u00e9 \u2013 r\u00e9sum\u00e9\r\n",
            ),
            (
                "utf-16-le",
                codecs.BOM_UTF16_LE
                + "alpha\n\u4e2d\u6587\n".encode("utf-16-le"),
                "alpha\n\u4e2d\u6587\n",
            ),
            (
                "utf-16-be",
                codecs.BOM_UTF16_BE
                + "beta\n\u65e5\u672c\u8a9e\n".encode("utf-16-be"),
                "beta\n\u65e5\u672c\u8a9e\n",
            ),
            (
                "gb18030",
                "\u914d\u7f6e=\u503c\n\u6a21\u578b=\u6d4b\u8bd5\n".encode("gb18030"),
                "\u914d\u7f6e=\u503c\n\u6a21\u578b=\u6d4b\u8bd5\n",
            ),
        )

        for expected_encoding, data, expected_text in fixtures:
            with self.subTest(expected_encoding=expected_encoding):
                result = extract_text_bytes(
                    data,
                    relative_path="config/settings.ini",
                    format_value="ini",
                )
                self.assertEqual(result.status, "processed")
                self.assertIsNotNone(result.document)
                document = result.document
                assert document is not None
                self.assertEqual(document.encoding, expected_encoding)
                self.assertEqual(self.reconstructed_text(result), expected_text)

    def test_utf8_bom_is_removed_from_text_but_recorded_as_encoding(self) -> None:
        text = "# \u6807\u9898\nbody\n"
        result = extract_text_bytes(
            codecs.BOM_UTF8 + text.encode("utf-8"),
            relative_path="notes/readme.md",
            format_value="markdown",
        )

        self.assertEqual(result.status, "processed")
        assert result.document is not None
        self.assertEqual(result.document.encoding, "utf-8-sig")
        self.assertEqual(self.reconstructed_text(result), text)
        self.assertEqual(result.document.blocks[0].block_type, "markdown")

    def test_empty_file_is_a_valid_zero_block_utf8_document(self) -> None:
        result = extract_text_bytes(
            b"",
            relative_path="empty.txt",
            format_value="plain_text",
        )

        self.assertEqual(result.status, "processed")
        assert result.document is not None
        self.assertEqual(result.document.encoding, "utf-8")
        self.assertEqual(result.document.blocks, ())
        self.assertEqual(result.document.metadata["line_count"], 0)
        self.assertEqual(result.document.metadata["truncation_reasons"], [])

    def test_overlong_line_is_partial_with_explicit_reason_and_locator(self) -> None:
        result = extract_text_bytes(
            b"alpha\n0123456789\nomega\n",
            relative_path="data/long.log",
            format_value="log",
            limits=TextExtractionLimits(
                max_line_characters=5,
                max_block_lines=1,
            ),
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.reason_code, "deterministic-text-partial")
        assert result.document is not None
        self.assertEqual(
            [block.text for block in result.document.blocks],
            ["alpha\n", "01234\n", "omega\n"],
        )
        middle = result.document.blocks[1]
        self.assertEqual(middle.locator, LineRangeLocator(2, 2))
        self.assertTrue(middle.truncated)
        self.assertEqual(middle.truncation_reason_code, "line-character-limit")
        self.assertEqual(
            middle.metadata["truncation_reasons"],
            [
                {
                    "code": "line-character-limit",
                    "reason": (
                        "one or more source lines exceeded max_line_characters"
                    ),
                }
            ],
        )
        self.assertEqual(
            result.document.metadata["truncation_reasons"],
            ["line-character-limit"],
        )
        self.assertIn("limited to 5 characters", result.diagnostics[0])

    def test_source_byte_cutoff_handles_incomplete_utf8_without_fallback(self) -> None:
        data = "one\n\u4f60\u597d\nthree\n".encode("utf-8")
        limits = TextExtractionLimits(max_source_bytes=6, max_block_lines=1)
        result = extract_text_bytes(
            data,
            relative_path="notes/multibyte.txt",
            format_value="plain_text",
            limits=limits,
        )

        self.assertEqual(result.status, "partial")
        assert result.document is not None
        self.assertEqual(result.document.encoding, "utf-8")
        self.assertEqual(self.reconstructed_text(result), "one\n")
        self.assertEqual(result.document.blocks[0].locator, LineRangeLocator(1, 1))
        self.assertTrue(result.document.blocks[0].truncated)
        self.assertEqual(
            result.document.blocks[0].truncation_reason_code,
            "source-byte-limit",
        )
        self.assertEqual(result.document.metadata["extracted_prefix_bytes"], 6)
        self.assertEqual(result.document.metadata["omitted_tail_bytes"], 2)
        self.assertEqual(
            result.document.metadata["omitted_source_bytes"],
            len(data) - 6 + 2,
        )
        self.assertEqual(
            result.document.metadata["truncation_reasons"],
            ["source-byte-limit"],
        )
        self.assertTrue(
            any("incomplete trailing encoded bytes" in item for item in result.diagnostics)
        )

    def test_invalid_terminal_byte_is_not_discarded_as_incomplete_utf8(self) -> None:
        data = b"AAAA\xffmore"
        result = extract_text_bytes(
            data,
            relative_path="legacy.txt",
            format_value="plain_text",
            limits=TextExtractionLimits(max_source_bytes=5),
        )

        self.assertEqual(result.status, "partial")
        assert result.document is not None
        self.assertEqual(result.document.encoding, "cp1252")
        self.assertEqual(self.reconstructed_text(result), "AAAA\u00ff")
        self.assertEqual(result.document.metadata["omitted_tail_bytes"], 0)

    def test_binary_content_misclassified_as_text_fails_without_document(self) -> None:
        result = extract_text_bytes(
            b"header\x00payload\x01\x02\x03",
            relative_path="results/not-really.txt",
            format_value="plain_text",
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason_code, "binary-content-detected")
        self.assertIsNone(result.document)

    def test_svg_is_supported_but_notebook_and_pdf_are_not_c02_formats(self) -> None:
        self.assertIn("svg", SUPPORTED_TEXT_FORMATS)
        svg = extract_text_bytes(
            b'<svg><text x="1">ok</text></svg>\n',
            relative_path="figures/plot.svg",
            format_value="svg",
        )
        self.assertEqual(svg.status, "processed")
        assert svg.document is not None
        self.assertEqual(svg.document.format, "svg")
        self.assertEqual(svg.document.blocks[0].block_type, "text")

        for format_value in ("notebook", "pdf"):
            with self.subTest(format_value=format_value):
                unsupported = extract_text_bytes(
                    b"ignored",
                    relative_path=f"source.{format_value}",
                    format_value=format_value,
                )
                self.assertEqual(unsupported.status, "unsupported")
                self.assertEqual(
                    unsupported.reason_code,
                    "unsupported-text-format",
                )
                self.assertIsNone(unsupported.document)

    def test_file_extraction_hashes_without_writing_or_changing_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "src" / "model.py"
            source.parent.mkdir()
            data = "VALUE = \"\u4f60\u597d\"\n".encode("utf-8")
            source.write_bytes(data)
            before = source.stat()
            before_entries = sorted(
                item.relative_to(root).as_posix() for item in root.rglob("*")
            )
            expected_hash = hashlib.sha256(data).hexdigest()

            result = extract_text_file(
                source,
                relative_path="src/model.py",
                format_value="python",
                expected_sha256=expected_hash,
                limits=TextExtractionLimits(read_chunk_bytes=4),
            )

            after = source.stat()
            after_entries = sorted(
                item.relative_to(root).as_posix() for item in root.rglob("*")
            )
            self.assertEqual(result.status, "processed")
            assert result.document is not None
            self.assertEqual(result.document.content_sha256, expected_hash)
            self.assertEqual(result.document.path, "src/model.py")
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(after.st_size, before.st_size)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(after_entries, before_entries)

    def test_file_hash_mismatch_and_read_failure_do_not_fabricate_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "notes.txt"
            source.write_text("current\n", encoding="utf-8")
            mismatch = extract_text_file(
                source,
                relative_path="notes.txt",
                format_value="plain_text",
                expected_sha256="0" * 64,
            )
            self.assertEqual(mismatch.status, "failed")
            self.assertEqual(mismatch.reason_code, "content-hash-mismatch")
            self.assertIsNone(mismatch.document)

            missing = extract_text_file(
                source.with_name("missing.txt"),
                relative_path="missing.txt",
                format_value="plain_text",
            )
            self.assertEqual(missing.status, "failed")
            self.assertEqual(missing.reason_code, "source-read-failed")
            self.assertIsNone(missing.document)

    def test_unsupported_file_format_is_rejected_before_opening_source(self) -> None:
        result = extract_text_file(
            "does-not-exist.pdf",
            relative_path="does-not-exist.pdf",
            format_value="pdf",
        )
        self.assertEqual(result.status, "unsupported")
        self.assertIsNone(result.document)

    def test_encoding_hint_and_output_are_deterministic(self) -> None:
        data = "caf\u00e9\n".encode("latin-1")
        first = extract_text_bytes(
            data,
            relative_path="legacy/notes.txt",
            format_value="plain_text",
            encoding_hint="latin-1",
        )
        second = extract_text_bytes(
            data,
            relative_path="legacy/notes.txt",
            format_value="plain_text",
            encoding_hint="latin-1",
        )

        self.assertEqual(first.status, "processed")
        assert first.document is not None
        self.assertEqual(first.document.encoding, "latin-1")
        self.assertEqual(serialize_extraction_result(first), serialize_extraction_result(second))

        invalid_hint = extract_text_bytes(
            b"text",
            relative_path="notes.txt",
            format_value="plain_text",
            encoding_hint="auto",
        )
        self.assertEqual(invalid_hint.status, "failed")
        self.assertEqual(invalid_hint.reason_code, "text-decoding-failed")
        self.assertIsNone(invalid_hint.document)

    def test_limit_and_hash_arguments_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            TextExtractionLimits(max_source_bytes=3)
        with self.assertRaises(ValueError):
            TextExtractionLimits(max_block_lines=0)
        with self.assertRaises(ValueError):
            extract_text_file(
                "unused.txt",
                relative_path="unused.txt",
                format_value="plain_text",
                expected_sha256="A" * 64,
            )


if __name__ == "__main__":
    unittest.main()
