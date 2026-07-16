from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from tools.extraction_schema import (
    ExtractedDocument,
    PdfPageLocator,
    serialize_extraction_result,
)
from tools.pdf_extractor import (
    PDF_BACKEND_NAME,
    PDF_BACKEND_VERSION,
    PDF_EXTRACTOR_NAME,
    PDF_EXTRACTOR_VERSION,
    PdfExtractionLimits,
    extract_pdf_bytes,
    extract_pdf_file,
)


class PdfExtractorTests(unittest.TestCase):
    @staticmethod
    def _pdf_literal(text: str) -> bytes:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return escaped.encode("latin-1")

    @classmethod
    def pdf_bytes(
        cls,
        pages: list[dict[str, object]],
        *,
        metadata: dict[str, str] | None = None,
        password: str | None = None,
    ) -> bytes:
        writer = PdfWriter()
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        font_reference = writer._add_object(font)
        for page_spec in pages:
            page = writer.add_blank_page(width=612, height=792)
            fonts = DictionaryObject({NameObject("/F1"): font_reference})
            resources = DictionaryObject({NameObject("/Font"): fonts})
            operators: list[bytes] = []
            text = page_spec.get("text")
            if text is not None:
                if not isinstance(text, str):
                    raise TypeError("fixture text must be a string")
                operators.append(
                    b"BT /F1 12 Tf 72 720 Td ("
                    + cls._pdf_literal(text)
                    + b") Tj ET"
                )
            if page_spec.get("image", False):
                image = DecodedStreamObject()
                image.set_data(b"\x80")
                image.update(
                    {
                        NameObject("/Type"): NameObject("/XObject"),
                        NameObject("/Subtype"): NameObject("/Image"),
                        NameObject("/Width"): NumberObject(1),
                        NameObject("/Height"): NumberObject(1),
                        NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                        NameObject("/BitsPerComponent"): NumberObject(8),
                    }
                )
                image_reference = writer._add_object(image)
                resources[NameObject("/XObject")] = DictionaryObject(
                    {NameObject("/Im0"): image_reference}
                )
                operators.append(b"q 200 0 0 200 72 500 cm /Im0 Do Q")
            page[NameObject("/Resources")] = resources
            if operators:
                content = DecodedStreamObject()
                content.set_data(b"\n".join(operators))
                page[NameObject("/Contents")] = writer._add_object(content)
        if metadata:
            writer.add_metadata(metadata)
        if password is not None:
            writer.encrypt(password)
        destination = io.BytesIO()
        writer.write(destination)
        return destination.getvalue()

    def require_document(self, result: object) -> ExtractedDocument:
        document = getattr(result, "document", None)
        self.assertIsInstance(document, ExtractedDocument)
        assert isinstance(document, ExtractedDocument)
        return document

    def test_pages_preserve_one_based_locators_text_order_and_metadata(self) -> None:
        data = self.pdf_bytes(
            [
                {"text": "First page contains enough research text."},
                {"text": "Second page contains a different result."},
            ],
            metadata={"/Title": "Trial paper", "/Author": "Researcher"},
        )

        result = extract_pdf_bytes(data, relative_path="papers/trial.pdf")

        self.assertEqual(result.status, "processed")
        document = self.require_document(result)
        self.assertEqual(document.extractor, PDF_EXTRACTOR_NAME)
        self.assertEqual(document.extractor_version, PDF_EXTRACTOR_VERSION)
        self.assertEqual(document.content_sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual([block.block_id for block in document.blocks], ["page-1", "page-2"])
        self.assertEqual(
            [block.locator for block in document.blocks],
            [PdfPageLocator(1), PdfPageLocator(2)],
        )
        self.assertEqual(
            [block.text for block in document.blocks],
            [
                "First page contains enough research text.",
                "Second page contains a different result.",
            ],
        )
        self.assertEqual(document.metadata["backend"], PDF_BACKEND_NAME)
        self.assertEqual(document.metadata["backend_version"], PDF_BACKEND_VERSION)
        self.assertEqual(document.metadata["page_count"], 2)
        self.assertEqual(document.metadata["basic_metadata"]["title"], "Trial paper")
        self.assertEqual(document.metadata["basic_metadata"]["author"], "Researcher")
        self.assertEqual(document.metadata["ocr_recommended_pages"], [])
        self.assertEqual(document.metadata["visual_review_recommended_pages"], [])
        self.assertEqual(document.metadata["assessment_counts"]["text"], 2)
        self.assertEqual(document.blocks[0].metadata["width_points"], 612.0)
        self.assertEqual(document.blocks[0].metadata["height_points"], 792.0)

    def test_scanned_low_text_and_no_text_pages_are_explicit_and_never_fabricated(self) -> None:
        data = self.pdf_bytes(
            [
                {"image": True},
                {"text": "x"},
                {},
            ]
        )

        result = extract_pdf_bytes(data, relative_path="papers/scanned.pdf")

        self.assertEqual(result.status, "partial")
        document = self.require_document(result)
        scanned, low_text, no_text = document.blocks
        self.assertEqual(scanned.text, "")
        self.assertEqual(scanned.metadata["assessment"], "likely_scanned")
        self.assertEqual(scanned.metadata["image_xobject_count"], 1)
        self.assertTrue(scanned.metadata["ocr_recommended"])
        self.assertEqual(low_text.text, "x")
        self.assertEqual(low_text.metadata["assessment"], "low_text")
        self.assertTrue(low_text.metadata["ocr_recommended"])
        self.assertEqual(no_text.text, "")
        self.assertEqual(no_text.metadata["assessment"], "no_text")
        self.assertTrue(no_text.metadata["visual_review_recommended"])
        self.assertEqual(document.metadata["ocr_recommended_pages"], [1, 2])
        self.assertEqual(document.metadata["visual_review_recommended_pages"], [3])
        self.assertEqual(
            document.metadata["partial_reasons"],
            ["page-requires-ocr", "page-requires-visual-review"],
        )

    def test_page_text_limit_is_partial_and_retains_full_text_hash(self) -> None:
        full_text = "abcdefghijklmnopqrstuvwxyz"
        data = self.pdf_bytes([{"text": full_text}])

        result = extract_pdf_bytes(
            data,
            relative_path="papers/large-page.pdf",
            limits=PdfExtractionLimits(
                max_page_characters=8,
                low_text_character_threshold=1,
            ),
        )

        self.assertEqual(result.status, "partial")
        document = self.require_document(result)
        block = document.blocks[0]
        self.assertEqual(block.text, "abcdefgh")
        self.assertTrue(block.truncated)
        self.assertEqual(block.truncation_reason_code, "page-character-limit")
        self.assertEqual(block.metadata["text_character_count"], len(full_text))
        self.assertEqual(
            block.metadata["text_sha256"],
            hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(document.metadata["truncated_pages"], [1])
        self.assertEqual(document.metadata["partial_reasons"], ["page-character-limit"])

    def test_metadata_limit_is_explicitly_partial(self) -> None:
        data = self.pdf_bytes(
            [{"text": "This page has enough extractable text for processing."}],
            metadata={"/Title": "0123456789"},
        )

        result = extract_pdf_bytes(
            data,
            relative_path="papers/metadata.pdf",
            limits=PdfExtractionLimits(max_metadata_characters=5),
        )

        self.assertEqual(result.status, "partial")
        document = self.require_document(result)
        self.assertEqual(document.metadata["basic_metadata"]["title"], "01234")
        self.assertEqual(document.metadata["metadata_truncated_fields"], ["/Title"])
        self.assertEqual(document.metadata["partial_reasons"], ["metadata-character-limit"])

    def test_source_byte_and_page_count_limits_fail_without_fabricated_document(self) -> None:
        data = self.pdf_bytes(
            [
                {"text": "First page contains enough extractable text."},
                {"text": "Second page contains enough extractable text."},
            ]
        )
        source_limited = extract_pdf_bytes(
            data,
            relative_path="papers/too-large.pdf",
            limits=PdfExtractionLimits(max_source_bytes=32),
        )
        page_limited = extract_pdf_bytes(
            data,
            relative_path="papers/too-many-pages.pdf",
            limits=PdfExtractionLimits(max_pages=1),
        )

        self.assertEqual(source_limited.status, "failed")
        self.assertEqual(source_limited.reason_code, "pdf-source-byte-limit")
        self.assertTrue(
            any(
                hashlib.sha256(data).hexdigest() in diagnostic
                for diagnostic in source_limited.diagnostics
            )
        )
        self.assertIsNone(source_limited.document)
        self.assertEqual(page_limited.status, "failed")
        self.assertEqual(page_limited.reason_code, "pdf-page-count-limit")
        self.assertIsNone(page_limited.document)

    def test_malformed_and_encrypted_pdfs_do_not_fabricate_text(self) -> None:
        malformed = extract_pdf_bytes(
            b"%PDF-1.7\nnot a real PDF",
            relative_path="papers/broken.pdf",
        )
        encrypted = extract_pdf_bytes(
            self.pdf_bytes(
                [{"text": "Secret page contains enough hidden text."}],
                password="secret",
            ),
            relative_path="papers/encrypted.pdf",
        )

        self.assertEqual(malformed.status, "failed")
        self.assertEqual(malformed.reason_code, "pdf-parse-failed")
        self.assertIsNone(malformed.document)
        self.assertEqual(encrypted.status, "unsupported")
        self.assertEqual(encrypted.reason_code, "encrypted-pdf-unsupported")
        self.assertIsNone(encrypted.document)

    def test_file_api_hashes_full_source_and_preserves_source_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "paper.pdf"
            data = self.pdf_bytes(
                [{"text": "A stable page contains enough extractable text."}]
            )
            source.write_bytes(data)
            fixed_ns = 1_700_000_000_123_456_700
            os.utime(source, ns=(fixed_ns, fixed_ns))
            before = source.stat()
            before_entries = sorted(item.name for item in root.iterdir())
            expected_hash = hashlib.sha256(data).hexdigest()

            result = extract_pdf_file(
                source,
                relative_path="paper.pdf",
                expected_sha256=expected_hash,
                limits=PdfExtractionLimits(read_chunk_bytes=7),
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
            source = Path(temporary_directory) / "paper.pdf"
            source.write_bytes(
                self.pdf_bytes(
                    [{"text": "This page contains enough extractable text."}]
                )
            )
            mismatch = extract_pdf_file(
                source,
                relative_path="paper.pdf",
                expected_sha256="0" * 64,
            )
            missing = extract_pdf_file(
                source.with_name("missing.pdf"),
                relative_path="missing.pdf",
            )

        unsupported = extract_pdf_file(
            "does-not-exist.ipynb",
            relative_path="does-not-exist.ipynb",
            format_value="notebook",
        )
        self.assertEqual(mismatch.reason_code, "content-hash-mismatch")
        self.assertEqual(missing.reason_code, "source-read-failed")
        self.assertEqual(unsupported.status, "unsupported")
        for result in (mismatch, missing, unsupported):
            self.assertIsNone(result.document)

    def test_repeated_extraction_serializes_byte_stably(self) -> None:
        data = self.pdf_bytes(
            [{"text": "Stable PDF page contains enough extractable text."}],
            metadata={"/Title": "Stable"},
        )
        first = extract_pdf_bytes(data, relative_path="papers/stable.pdf")
        second = extract_pdf_bytes(data, relative_path="papers/stable.pdf")

        self.assertEqual(
            serialize_extraction_result(first).encode("utf-8"),
            serialize_extraction_result(second).encode("utf-8"),
        )

    def test_limit_hash_and_data_arguments_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            PdfExtractionLimits(max_source_bytes=7)
        with self.assertRaises(ValueError):
            PdfExtractionLimits(max_pages=0)
        with self.assertRaises(TypeError):
            extract_pdf_bytes("not bytes", relative_path="paper.pdf")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            extract_pdf_file(
                "unused.pdf",
                relative_path="unused.pdf",
                expected_sha256="A" * 64,
            )


if __name__ == "__main__":
    unittest.main()
