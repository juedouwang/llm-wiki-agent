from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import struct
import tempfile
import unittest
import warnings
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

from openpyxl import Workbook

from tools.extraction_schema import (
    ExtractedDocument,
    ParagraphLocator,
    SlideLocator,
    TableRangeLocator,
    deserialize_extraction_result,
    serialize_extraction_result,
)
from tools.office_tabular_extractor import (
    OfficeTabularExtractionLimits,
    OfficeTabularFormatError,
    OfficeTabularLocatorError,
    extract_office_tabular_bytes,
    extract_office_tabular_file,
    reopen_office_locator_bytes,
)


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
SLIDE_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
)


class OfficeTabularExtractorTests(unittest.TestCase):
    @staticmethod
    def require_document(result: object) -> ExtractedDocument:
        document = getattr(result, "document", None)
        if not isinstance(document, ExtractedDocument):
            raise AssertionError(f"expected an extracted document, got {result!r}")
        return document

    @staticmethod
    def zip_bytes(
        members: list[tuple[str | zipfile.ZipInfo, str | bytes]],
    ) -> bytes:
        destination = io.BytesIO()
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in members:
                archive.writestr(
                    name,
                    payload.encode("utf-8") if isinstance(payload, str) else payload,
                )
        return destination.getvalue()

    @classmethod
    def rewrite_zip(cls, data: bytes, replacements: dict[str, bytes]) -> bytes:
        destination = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data), "r") as source:
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as target:
                for info in source.infolist():
                    payload = replacements.get(info.filename, source.read(info))
                    target.writestr(info, payload)
        return destination.getvalue()

    @staticmethod
    def mark_first_member_encrypted(data: bytes) -> bytes:
        mutated = bytearray(data)
        for signature, relative_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            offset = mutated.find(signature)
            if offset < 0:
                raise AssertionError(f"missing ZIP signature {signature!r}")
            flag_offset = offset + relative_offset
            flags = struct.unpack_from("<H", mutated, flag_offset)[0] | 0x1
            struct.pack_into("<H", mutated, flag_offset, flags)
        return bytes(mutated)

    @staticmethod
    def xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
        workbook = Workbook()
        workbook.iso_dates = True
        default = workbook.active
        first_name = next(iter(sheets))
        default.title = first_name
        worksheets = {first_name: default}
        for sheet_name in list(sheets)[1:]:
            worksheets[sheet_name] = workbook.create_sheet(sheet_name)
        for sheet_name, rows in sheets.items():
            worksheet = worksheets[sheet_name]
            for row in rows:
                worksheet.append(row)
        destination = io.BytesIO()
        workbook.save(destination)
        workbook.close()
        return destination.getvalue()

    @classmethod
    def docx_bytes(
        cls,
        body_xml: str,
        *,
        document_xml: bytes | None = None,
        extra_members: list[tuple[str | zipfile.ZipInfo, str | bytes]] | None = None,
    ) -> bytes:
        payload = document_xml
        if payload is None:
            payload = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{W_NS}"><w:body>{body_xml}</w:body>'
                "</w:document>"
            ).encode("utf-8")
        members: list[tuple[str | zipfile.ZipInfo, str | bytes]] = [
            ("word/document.xml", payload)
        ]
        members.extend(extra_members or [])
        return cls.zip_bytes(members)

    @staticmethod
    def docx_paragraph(
        text: str = "",
        *,
        style: str | None = None,
        inner_xml: str | None = None,
    ) -> str:
        style_xml = (
            f'<w:pPr><w:pStyle w:val="{escape(style)}"/></w:pPr>'
            if style is not None
            else ""
        )
        run_xml = (
            inner_xml
            if inner_xml is not None
            else (f"<w:r><w:t>{escape(text)}</w:t></w:r>" if text else "")
        )
        return f"<w:p>{style_xml}{run_xml}</w:p>"

    @classmethod
    def pptx_bytes(
        cls,
        *,
        order: list[str],
        relationships: list[tuple[str, str, str | None, str | None]],
        slides: dict[str, list[str]],
        presentation_xml: bytes | None = None,
        relationships_xml: bytes | None = None,
    ) -> bytes:
        if presentation_xml is None:
            ids = "".join(
                f'<p:sldId id="{256 + index}" r:id="{escape(rid)}"/>'
                for index, rid in enumerate(order)
            )
            presentation_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<p:presentation xmlns:p="{P_NS}" xmlns:r="{R_NS}">'
                f"<p:sldIdLst>{ids}</p:sldIdLst></p:presentation>"
            ).encode("utf-8")
        if relationships_xml is None:
            relation_nodes = []
            for rid, target, target_mode, relation_type in relationships:
                attributes = [
                    f'Id="{escape(rid)}"',
                    f'Target="{escape(target)}"',
                    f'Type="{escape(relation_type or SLIDE_REL_TYPE)}"',
                ]
                if target_mode is not None:
                    attributes.append(f'TargetMode="{escape(target_mode)}"')
                relation_nodes.append(f"<Relationship {' '.join(attributes)}/>")
            relationships_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<Relationships xmlns="{REL_NS}">'
                f"{''.join(relation_nodes)}</Relationships>"
            ).encode("utf-8")
        members: list[tuple[str | zipfile.ZipInfo, str | bytes]] = [
            ("ppt/presentation.xml", presentation_xml),
            ("ppt/_rels/presentation.xml.rels", relationships_xml),
        ]
        for target, paragraphs in slides.items():
            paragraph_xml = "".join(
                f"<a:p><a:r><a:t>{escape(text)}</a:t></a:r></a:p>"
                for text in paragraphs
            )
            slide_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}">'
                f"<p:cSld><p:spTree><p:sp><p:txBody>{paragraph_xml}"
                "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
            )
            members.append((f"ppt/{target}", slide_xml))
        return cls.zip_bytes(members)

    @classmethod
    def simple_pptx(cls) -> bytes:
        return cls.pptx_bytes(
            order=["rId2", "rId1"],
            relationships=[
                ("rId1", "slides/slide1.xml", None, SLIDE_REL_TYPE),
                ("rId2", "slides/slide2.xml", None, SLIDE_REL_TYPE),
            ],
            slides={
                "slides/slide1.xml": ["filename one"],
                "slides/slide2.xml": ["filename two", "second paragraph"],
            },
        )

    def test_api_rejects_unsupported_formats_without_fabricating_document(self) -> None:
        for format_value in ("pdf", "CSV", "", None, 7):
            with self.subTest(format_value=format_value):
                result = extract_office_tabular_bytes(
                    b"a,b\n1,2\n",
                    relative_path="tables/data.csv",
                    format_value=format_value,  # type: ignore[arg-type]
                )
                self.assertEqual(result.status, "unsupported")
                self.assertEqual(
                    result.reason_code, "office-tabular-format-unsupported"
                )
                self.assertIsNone(result.document)

    def test_api_requires_bytes_and_real_limits_instances(self) -> None:
        for value in ("a,b", bytearray(b"a,b"), memoryview(b"a,b"), None):
            with self.subTest(data_type=type(value).__name__):
                with self.assertRaisesRegex(TypeError, "data must be bytes"):
                    extract_office_tabular_bytes(
                        value,  # type: ignore[arg-type]
                        relative_path="tables/data.csv",
                        format_value="csv",
                    )
        for limits in (False, 0, {}, "limits"):
            with self.subTest(limits=limits):
                with self.assertRaisesRegex(
                    TypeError, "limits must be OfficeTabularExtractionLimits"
                ):
                    extract_office_tabular_bytes(
                        b"a,b\n",
                        relative_path="tables/data.csv",
                        format_value="csv",
                        limits=limits,  # type: ignore[arg-type]
                    )
                with self.assertRaisesRegex(
                    TypeError, "limits must be OfficeTabularExtractionLimits"
                ):
                    reopen_office_locator_bytes(
                        b"a,b\n",
                        relative_path="tables/data.csv",
                        format_value="csv",
                        locator=TableRangeLocator("CSV", "A1", "A1"),
                        limits=limits,  # type: ignore[arg-type]
                    )

    def test_every_limit_rejects_boolean_and_zero_values(self) -> None:
        for field_name in OfficeTabularExtractionLimits.__dataclass_fields__:
            with self.subTest(field_name=field_name, value=False):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    OfficeTabularExtractionLimits(**{field_name: False})
            with self.subTest(field_name=field_name, value=0):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    OfficeTabularExtractionLimits(**{field_name: 0})

    def test_file_hash_precondition_and_source_immutability(self) -> None:
        source_bytes = "name,value\n猫,42\n".encode()
        expected = hashlib.sha256(source_bytes).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.csv"
            source.write_bytes(source_bytes)
            original_stat = source.stat()

            result = extract_office_tabular_file(
                source,
                relative_path="tables/source.csv",
                format_value="csv",
                expected_sha256=expected,
            )
            mismatch = extract_office_tabular_file(
                source,
                relative_path="tables/source.csv",
                format_value="csv",
                expected_sha256="0" * 64,
            )

            self.assertEqual(result.status, "processed")
            self.assertEqual(
                self.require_document(result).content_sha256,
                expected,
            )
            self.assertEqual(mismatch.status, "failed")
            self.assertEqual(mismatch.reason_code, "content-hash-mismatch")
            self.assertIsNone(mismatch.document)
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(source.stat().st_mtime_ns, original_stat.st_mtime_ns)
            self.assertEqual(
                sorted(path.relative_to(root).as_posix() for path in root.rglob("*")),
                ["source.csv"],
            )

        for invalid in ("ABC", "A" * 64, "0" * 63, 1):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "expected_sha256"):
                    extract_office_tabular_file(
                        "missing.csv",
                        relative_path="tables/missing.csv",
                        format_value="csv",
                        expected_sha256=invalid,  # type: ignore[arg-type]
                    )

    def test_schema_serialization_round_trip_preserves_new_locators(self) -> None:
        docx_result = extract_office_tabular_bytes(
            self.docx_bytes(self.docx_paragraph("hello")),
            relative_path="notes/sample.docx",
            format_value="docx",
        )
        pptx_result = extract_office_tabular_bytes(
            self.simple_pptx(),
            relative_path="slides/sample.pptx",
            format_value="pptx",
        )
        for result in (docx_result, pptx_result):
            with self.subTest(format=self.require_document(result).format):
                serialized = serialize_extraction_result(result)
                restored = deserialize_extraction_result(serialized)
                self.assertEqual(restored, result)
                self.assertEqual(
                    json.loads(serialize_extraction_result(restored)),
                    json.loads(serialized),
                )

    def test_csv_unicode_quotes_logical_sheet_and_canonical_matrix(self) -> None:
        data = 'name,note\n"猫","comma, and\nnewline"\n'.encode()

        result = extract_office_tabular_bytes(
            data,
            relative_path="tables/unicode.csv",
            format_value="csv",
        )

        self.assertEqual(result.status, "processed")
        document = self.require_document(result)
        self.assertEqual(document.encoding, "utf-8")
        self.assertEqual(document.metadata["sheet_names"], ["CSV"])
        self.assertEqual(len(document.blocks), 1)
        block = document.blocks[0]
        self.assertEqual(block.text, '[["name","note"],["猫","comma, and\\nnewline"]]')
        self.assertEqual(block.locator, TableRangeLocator("CSV", "A1", "B2"))
        self.assertEqual(block.metadata["matrix_format"], "table-json-matrix-v1")
        self.assertEqual(block.metadata["row_count"], 2)
        self.assertEqual(block.metadata["column_count"], 2)

    def test_tsv_quotes_tabs_newlines_and_uses_reserved_sheet(self) -> None:
        data = 'left\tright\n"embedded\ttab"\t"two\nlines"\n'.encode()

        result = extract_office_tabular_bytes(
            data,
            relative_path="tables/data.tsv",
            format_value="tsv",
        )

        document = self.require_document(result)
        self.assertEqual(result.status, "processed")
        self.assertEqual(document.metadata["sheet_names"], ["TSV"])
        self.assertEqual(
            json.loads(document.blocks[0].text),
            [["left", "right"], ["embedded\ttab", "two\nlines"]],
        )
        self.assertEqual(
            document.blocks[0].locator,
            TableRangeLocator("TSV", "A1", "B2"),
        )

    def test_delimited_decoding_supports_bom_utf16_and_cp1252(self) -> None:
        cases = [
            (b"\xef\xbb\xbf" + "café\n".encode(), "utf-8-sig", "café"),
            ("café\n".encode("utf-16"), "utf-16-le", "café"),
            ("café\n".encode("cp1252"), "cp1252", "café"),
        ]
        for data, encoding, expected in cases:
            with self.subTest(encoding=encoding):
                result = extract_office_tabular_bytes(
                    data,
                    relative_path="tables/data.csv",
                    format_value="csv",
                )
                document = self.require_document(result)
                self.assertEqual(document.encoding, encoding)
                self.assertEqual(json.loads(document.blocks[0].text), [[expected]])

    def test_csv_preserves_explicit_empty_fields_and_pads_absent_cells(self) -> None:
        result = extract_office_tabular_bytes(
            b"a,b,c\nx,,\ny\n",
            relative_path="tables/sparse.csv",
            format_value="csv",
        )

        document = self.require_document(result)
        self.assertEqual(
            json.loads(document.blocks[0].text),
            [["a", "b", "c"], ["x", "", ""], ["y", None, None]],
        )
        self.assertEqual(
            document.blocks[0].locator,
            TableRangeLocator("CSV", "A1", "C3"),
        )

    def test_delimited_content_limits_are_explicitly_partial(self) -> None:
        cases = [
            (
                OfficeTabularExtractionLimits(max_rows=1),
                "row-limit",
                b"a,b\nc,d\n",
            ),
            (
                OfficeTabularExtractionLimits(max_columns=1),
                "column-limit",
                b"a,b\n",
            ),
            (
                OfficeTabularExtractionLimits(max_cells=2),
                "cell-limit",
                b"a,b\nc,d\n",
            ),
            (
                OfficeTabularExtractionLimits(max_cell_characters=3),
                "cell-character-limit",
                b"abcdef\n",
            ),
        ]
        for limits, expected_reason, data in cases:
            with self.subTest(reason=expected_reason):
                result = extract_office_tabular_bytes(
                    data,
                    relative_path="tables/limited.csv",
                    format_value="csv",
                    limits=limits,
                )
                self.assertEqual(result.status, "partial")
                document = self.require_document(result)
                self.assertIn(expected_reason, document.metadata["partial_reasons"])
                self.assertTrue(result.diagnostics)
                if expected_reason == "cell-character-limit":
                    self.assertEqual(document.blocks[0].text, '[["abc"]]')
                    self.assertTrue(document.blocks[0].truncated)

    def test_delimited_block_grouping_keeps_exact_range_locators(self) -> None:
        result = extract_office_tabular_bytes(
            b"a,b\nc,d\ne,f\n",
            relative_path="tables/groups.csv",
            format_value="csv",
            limits=OfficeTabularExtractionLimits(max_rows_per_block=2),
        )

        document = self.require_document(result)
        self.assertEqual(result.status, "processed")
        self.assertEqual(
            [block.locator for block in document.blocks],
            [
                TableRangeLocator("CSV", "A1", "B2"),
                TableRangeLocator("CSV", "A3", "B3"),
            ],
        )
        self.assertEqual(json.loads(document.blocks[1].text), [["e", "f"]])

    def test_delimited_malformed_quotes_and_nul_fail_without_document(self) -> None:
        cases = [
            (b'"unterminated', "delimited-parse-failed"),
            (b"a,\x00b\n", "delimited-binary-contradiction"),
        ]
        for data, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                result = extract_office_tabular_bytes(
                    data,
                    relative_path="tables/broken.csv",
                    format_value="csv",
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.reason_code, reason_code)
                self.assertIsNone(result.document)

    def test_delimited_reopening_supports_arbitrary_exact_ranges(self) -> None:
        data = b"r1c1,r1c2,r1c3\nr2c1,,r2c3\nr3c1\n"

        text = reopen_office_locator_bytes(
            data,
            relative_path="tables/source.csv",
            format_value="csv",
            locator=TableRangeLocator("CSV", "B2", "C3"),
        )

        self.assertEqual(json.loads(text), [["", "r2c3"], [None, None]])

    def test_delimited_reopening_rejects_wrong_sheet_bounds_and_limits(self) -> None:
        data = b"aa,b\nc,d\ne,f\n"
        cases = [
            (
                TableRangeLocator("TSV", "A1", "A1"),
                OfficeTabularExtractionLimits(),
                "delimited-sheet-missing",
            ),
            (
                TableRangeLocator("CSV", "A1", "C1"),
                OfficeTabularExtractionLimits(),
                "delimited-range-out-of-bounds",
            ),
            (
                TableRangeLocator("CSV", "A1", "B2"),
                OfficeTabularExtractionLimits(max_cells=3),
                "table-range-cell-limit",
            ),
        ]
        for locator, limits, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                with self.assertRaises(OfficeTabularLocatorError) as raised:
                    reopen_office_locator_bytes(
                        data,
                        relative_path="tables/source.csv",
                        format_value="csv",
                        locator=locator,
                        limits=limits,
                    )
                self.assertEqual(raised.exception.reason_code, reason_code)

        for limits, reason_code in (
            (OfficeTabularExtractionLimits(max_rows=2), "delimited-source-limit"),
            (OfficeTabularExtractionLimits(max_cell_characters=1), "cell-character-limit"),
        ):
            with self.subTest(reason_code=reason_code):
                with self.assertRaises(OfficeTabularFormatError) as raised:
                    reopen_office_locator_bytes(
                        data,
                        relative_path="tables/source.csv",
                        format_value="csv",
                        locator=TableRangeLocator("CSV", "A1", "A1"),
                        limits=limits,
                    )
                self.assertEqual(raised.exception.reason_code, reason_code)


    def test_xlsx_preserves_sheets_formulas_and_typed_values(self) -> None:
        data = self.xlsx_bytes(
            {
                "Alpha": [
                    [
                        "text",
                        7,
                        True,
                        "=SUM(B1,1)",
                        datetime(2025, 1, 2, 3, 4, 5),
                        date(2025, 1, 3),
                        time(6, 7, 8),
                        timedelta(days=1, seconds=2),
                    ]
                ],
                "Beta": [["second sheet"]],
            }
        )

        result = extract_office_tabular_bytes(
            data,
            relative_path="tables/typed.xlsx",
            format_value="xlsx",
        )

        self.assertEqual(result.status, "processed")
        document = self.require_document(result)
        self.assertEqual(document.metadata["sheet_names"], ["Alpha", "Beta"])
        self.assertEqual(document.metadata["formula_mode"], "formula-preserving")
        self.assertEqual(
            [block.locator for block in document.blocks],
            [
                TableRangeLocator("Alpha", "A1", "H1"),
                TableRangeLocator("Beta", "A1", "A1"),
            ],
        )
        matrix = json.loads(document.blocks[0].text)
        self.assertEqual(matrix[0][:4], ["text", 7, True, "=SUM(B1,1)"])
        self.assertEqual(
            matrix[0][4],
            {"type": "datetime", "value": "2025-01-02T03:04:05"},
        )
        self.assertEqual(
            matrix[0][5],
            {"type": "date", "value": "2025-01-03"},
        )
        self.assertEqual(matrix[0][6], {"type": "time", "value": "06:07:08"})
        self.assertEqual(
            matrix[0][7],
            {"type": "timedelta", "microseconds": 86_402_000_000},
        )
        self.assertEqual(
            json.loads(document.blocks[1].text),
            [["second sheet"]],
        )

    def test_xlsx_sheet_row_column_cell_and_cell_text_limits_are_partial(self) -> None:
        data = self.xlsx_bytes(
            {
                "First": [["abcdef", "b"], ["c", "d"]],
                "Second": [["e"]],
            }
        )
        cases = [
            (OfficeTabularExtractionLimits(max_sheets=1), "sheet-limit"),
            (OfficeTabularExtractionLimits(max_rows=1), "row-limit"),
            (OfficeTabularExtractionLimits(max_columns=1), "column-limit"),
            (OfficeTabularExtractionLimits(max_cells=1), "cell-limit"),
            (
                OfficeTabularExtractionLimits(max_cell_characters=3),
                "cell-character-limit",
            ),
        ]
        for limits, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                result = extract_office_tabular_bytes(
                    data,
                    relative_path="tables/limited.xlsx",
                    format_value="xlsx",
                    limits=limits,
                )
                self.assertEqual(result.status, "partial")
                document = self.require_document(result)
                self.assertIn(reason_code, document.metadata["partial_reasons"])
                self.assertTrue(document.blocks)
                if reason_code == "cell-character-limit":
                    self.assertEqual(json.loads(document.blocks[0].text)[0][0], "abc")
                    self.assertTrue(document.blocks[0].truncated)

    def test_xlsx_low_cell_budget_retains_a_bounded_rectangle(self) -> None:
        data = self.xlsx_bytes(
            {"Data": [["a", "b", "c"], ["d", "e", "f"]]}
        )

        result = extract_office_tabular_bytes(
            data,
            relative_path="tables/budget.xlsx",
            format_value="xlsx",
            limits=OfficeTabularExtractionLimits(max_cells=2),
        )

        self.assertEqual(result.status, "partial")
        document = self.require_document(result)
        self.assertEqual(json.loads(document.blocks[0].text), [["a", "b"]])
        self.assertEqual(
            document.blocks[0].locator,
            TableRangeLocator("Data", "A1", "B1"),
        )
        self.assertEqual(document.metadata["scanned_cells"], 2)
        self.assertIn("cell-limit", document.metadata["partial_reasons"])

    def test_xlsx_malformed_zip_and_xml_fail_closed(self) -> None:
        malformed = extract_office_tabular_bytes(
            b"not a zip archive",
            relative_path="tables/broken.xlsx",
            format_value="xlsx",
        )
        self.assertEqual(malformed.status, "failed")
        self.assertEqual(malformed.reason_code, "ooxml-archive-invalid")
        self.assertIsNone(malformed.document)

        data = self.xlsx_bytes({"Sheet": [["value"]]})
        broken_xml = self.rewrite_zip(
            data,
            {"xl/workbook.xml": b"<workbook><broken></workbook>"},
        )
        result = extract_office_tabular_bytes(
            broken_xml,
            relative_path="tables/broken-xml.xlsx",
            format_value="xlsx",
        )
        self.assertEqual(result.status, "failed")
        self.assertIn(
            result.reason_code,
            {"ooxml-xml-invalid", "xlsx-parse-failed"},
        )
        self.assertIsNone(result.document)

    def test_xlsx_archive_resource_limits_fail_closed(self) -> None:
        data = self.xlsx_bytes({"Sheet": [["value"]]})
        cases = [
            (OfficeTabularExtractionLimits(max_archive_entries=1), "ooxml-entry-limit"),
            (
                OfficeTabularExtractionLimits(max_archive_member_bytes=16),
                "ooxml-member-byte-limit",
            ),
            (
                OfficeTabularExtractionLimits(max_archive_uncompressed_bytes=16),
                "ooxml-total-byte-limit",
            ),
        ]
        for limits, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                result = extract_office_tabular_bytes(
                    data,
                    relative_path="tables/limited.xlsx",
                    format_value="xlsx",
                    limits=limits,
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.reason_code, reason_code)
                self.assertIsNone(result.document)

    def test_ooxml_rejects_unsafe_duplicate_encrypted_and_nonregular_members(self) -> None:
        base = self.xlsx_bytes({"Sheet": [["value"]]})
        with zipfile.ZipFile(io.BytesIO(base), "r") as archive:
            base_members = [
                (info.filename, archive.read(info)) for info in archive.infolist()
            ]
        unsafe = self.zip_bytes(base_members + [("../escape.xml", b"<x/>")])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            duplicate = self.zip_bytes(base_members + [base_members[0]])
        case_collision = self.zip_bytes(
            base_members + [(base_members[0][0].upper(), base_members[0][1])]
        )
        encrypted = self.mark_first_member_encrypted(base)
        symlink = zipfile.ZipInfo("unsafe-link")
        symlink.create_system = 3
        symlink.external_attr = 0o120777 << 16
        nonregular = self.zip_bytes(base_members + [(symlink, b"target")])

        cases = [
            (unsafe, "ooxml-member-path-unsafe"),
            (duplicate, "ooxml-member-duplicate"),
            (case_collision, "ooxml-member-duplicate"),
            (encrypted, "ooxml-member-encrypted"),
            (nonregular, "ooxml-member-not-regular"),
        ]
        for data, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                result = extract_office_tabular_bytes(
                    data,
                    relative_path="tables/unsafe.xlsx",
                    format_value="xlsx",
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.reason_code, reason_code)
                self.assertIsNone(result.document)

    def test_ooxml_rejects_dtd_and_entity_declarations_including_utf16(self) -> None:
        base = self.xlsx_bytes({"Sheet": [["value"]]})
        payloads = [
            b'<?xml version="1.0"?><!DOCTYPE workbook [<!ENTITY x "boom">]>'
            b"<workbook>&x;</workbook>",
            (
                '<?xml version="1.0" encoding="UTF-16"?>'
                '<!DOCTYPE workbook [<!ENTITY x "boom">]><workbook>&x;</workbook>'
            ).encode("utf-16"),
        ]
        for payload in payloads:
            with self.subTest(encoding=payload[:8]):
                data = self.rewrite_zip(base, {"xl/workbook.xml": payload})
                result = extract_office_tabular_bytes(
                    data,
                    relative_path="tables/dtd.xlsx",
                    format_value="xlsx",
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(
                    result.reason_code,
                    "ooxml-xml-declaration-unsafe",
                )
                self.assertIsNone(result.document)

    def test_xlsx_reopening_supports_arbitrary_exact_range_and_formulas(self) -> None:
        data = self.xlsx_bytes(
            {"Data": [["a", 2, "=B1*2"], ["c", 4, "=B2*2"]]}
        )

        text = reopen_office_locator_bytes(
            data,
            relative_path="tables/data.xlsx",
            format_value="xlsx",
            locator=TableRangeLocator("Data", "B1", "C2"),
        )

        self.assertEqual(json.loads(text), [[2, "=B1*2"], [4, "=B2*2"]])

    def test_xlsx_reopening_rejects_missing_bounds_and_source_limits(self) -> None:
        data = self.xlsx_bytes({"Data": [["abcdef", "b"], ["c", "d"]]})
        locator_cases = [
            (TableRangeLocator("Missing", "A1", "A1"), "xlsx-sheet-missing"),
            (TableRangeLocator("Data", "A1", "C1"), "xlsx-range-out-of-bounds"),
        ]
        for locator, reason_code in locator_cases:
            with self.subTest(reason_code=reason_code):
                with self.assertRaises(OfficeTabularLocatorError) as raised:
                    reopen_office_locator_bytes(
                        data,
                        relative_path="tables/data.xlsx",
                        format_value="xlsx",
                        locator=locator,
                    )
                self.assertEqual(raised.exception.reason_code, reason_code)

        format_cases = [
            (
                OfficeTabularExtractionLimits(max_source_bytes=len(data) - 1),
                "office-tabular-source-byte-limit",
            ),
            (OfficeTabularExtractionLimits(max_cells=1), "xlsx-cell-limit"),
            (
                OfficeTabularExtractionLimits(max_cell_characters=3),
                "cell-character-limit",
            ),
        ]
        for limits, reason_code in format_cases:
            with self.subTest(reason_code=reason_code):
                with self.assertRaises(OfficeTabularFormatError) as raised:
                    reopen_office_locator_bytes(
                        data,
                        relative_path="tables/data.xlsx",
                        format_value="xlsx",
                        locator=TableRangeLocator("Data", "A1", "A1"),
                        limits=limits,
                    )
                self.assertEqual(raised.exception.reason_code, reason_code)

    def test_docx_main_body_paragraph_order_controls_empty_text_and_style(self) -> None:
        body = "".join(
            [
                self.docx_paragraph("First", style="Heading1"),
                (
                    "<w:tbl><w:tr><w:tc>"
                    f"{self.docx_paragraph('Inside table')}"
                    "</w:tc></w:tr></w:tbl>"
                ),
                self.docx_paragraph(),
                self.docx_paragraph(
                    inner_xml=(
                        "<w:r><w:t>A</w:t><w:tab/><w:t>B</w:t>"
                        "<w:br/><w:t>C</w:t></w:r>"
                    )
                ),
            ]
        )
        data = self.docx_bytes(body)

        result = extract_office_tabular_bytes(
            data,
            relative_path="notes/main.docx",
            format_value="docx",
        )

        self.assertEqual(result.status, "processed")
        document = self.require_document(result)
        self.assertEqual(
            [block.text for block in document.blocks],
            ["First", "Inside table", "", "A\tB\nC"],
        )
        self.assertEqual(
            [block.locator for block in document.blocks],
            [ParagraphLocator(0), ParagraphLocator(1), ParagraphLocator(2), ParagraphLocator(3)],
        )
        self.assertEqual(document.blocks[0].metadata["style"], "Heading1")
        self.assertNotIn("style", document.blocks[1].metadata)
        self.assertEqual(document.metadata["paragraph_count"], 4)
        self.assertEqual(
            document.metadata["paragraph_coordinate_space"],
            "word-document-body-w-p-order",
        )

    def test_docx_paragraph_and_block_character_limits_are_partial(self) -> None:
        data = self.docx_bytes(
            self.docx_paragraph("abcdef")
            + self.docx_paragraph("second")
            + self.docx_paragraph("third")
        )
        paragraph_limited = extract_office_tabular_bytes(
            data,
            relative_path="notes/limited.docx",
            format_value="docx",
            limits=OfficeTabularExtractionLimits(max_paragraphs=2),
        )
        character_limited = extract_office_tabular_bytes(
            data,
            relative_path="notes/limited.docx",
            format_value="docx",
            limits=OfficeTabularExtractionLimits(max_block_characters=3),
        )

        self.assertEqual(paragraph_limited.status, "partial")
        paragraph_document = self.require_document(paragraph_limited)
        self.assertEqual(len(paragraph_document.blocks), 2)
        self.assertIn("paragraph-limit", paragraph_document.metadata["partial_reasons"])
        self.assertEqual(character_limited.status, "partial")
        character_document = self.require_document(character_limited)
        self.assertEqual(character_document.blocks[0].text, "abc")
        self.assertTrue(character_document.blocks[0].truncated)
        self.assertEqual(
            character_document.blocks[0].truncation_reason_code,
            "block-character-limit",
        )
        self.assertIn(
            "block-character-limit",
            character_document.metadata["partial_reasons"],
        )

    def test_docx_reopening_returns_exact_current_paragraph(self) -> None:
        data = self.docx_bytes(
            self.docx_paragraph("first")
            + self.docx_paragraph(
                inner_xml="<w:r><w:t>A</w:t><w:tab/><w:t>B</w:t></w:r>"
            )
        )

        text = reopen_office_locator_bytes(
            data,
            relative_path="notes/main.docx",
            format_value="docx",
            locator=ParagraphLocator(1),
        )

        self.assertEqual(text, "A\tB")
        with self.assertRaises(OfficeTabularLocatorError) as raised:
            reopen_office_locator_bytes(
                data,
                relative_path="notes/main.docx",
                format_value="docx",
                locator=ParagraphLocator(2),
            )
        self.assertEqual(raised.exception.reason_code, "docx-paragraph-out-of-bounds")

    def test_docx_reopening_fails_instead_of_returning_truncated_content(self) -> None:
        data = self.docx_bytes(
            self.docx_paragraph("abcdef") + self.docx_paragraph("second")
        )
        cases = [
            (OfficeTabularExtractionLimits(max_paragraphs=1), "docx-paragraph-limit"),
            (
                OfficeTabularExtractionLimits(max_block_characters=3),
                "block-character-limit",
            ),
        ]
        for limits, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                with self.assertRaises(OfficeTabularFormatError) as raised:
                    reopen_office_locator_bytes(
                        data,
                        relative_path="notes/main.docx",
                        format_value="docx",
                        locator=ParagraphLocator(0),
                        limits=limits,
                    )
                self.assertEqual(raised.exception.reason_code, reason_code)

    def test_docx_missing_malformed_body_and_dtd_fail_closed(self) -> None:
        cases = [
            (
                self.zip_bytes([("word/other.xml", "<x/>")]),
                "ooxml-required-part-missing",
            ),
            (
                self.docx_bytes("", document_xml=b"<w:document>"),
                "ooxml-xml-invalid",
            ),
            (
                self.docx_bytes(
                    "",
                    document_xml=(
                        f'<w:document xmlns:w="{W_NS}"></w:document>'
                    ).encode(),
                ),
                "docx-body-missing",
            ),
            (
                self.docx_bytes(
                    "",
                    document_xml=(
                        '<?xml version="1.0"?><!DOCTYPE w:document '
                        '[<!ENTITY x "boom">]>'
                        f'<w:document xmlns:w="{W_NS}"><w:body>&x;</w:body>'
                        "</w:document>"
                    ).encode(),
                ),
                "ooxml-xml-declaration-unsafe",
            ),
        ]
        for data, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                result = extract_office_tabular_bytes(
                    data,
                    relative_path="notes/broken.docx",
                    format_value="docx",
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.reason_code, reason_code)
                self.assertIsNone(result.document)

    def test_pptx_uses_relationship_order_and_preserves_paragraph_boundaries(self) -> None:
        result = extract_office_tabular_bytes(
            self.simple_pptx(),
            relative_path="slides/deck.pptx",
            format_value="pptx",
        )

        self.assertEqual(result.status, "processed")
        document = self.require_document(result)
        self.assertEqual(
            [block.text for block in document.blocks],
            ["filename two\nsecond paragraph", "filename one"],
        )
        self.assertEqual(
            [block.locator for block in document.blocks],
            [SlideLocator(1), SlideLocator(2)],
        )
        self.assertEqual(document.blocks[0].metadata["relationship_id"], "rId2")
        self.assertEqual(
            document.blocks[0].metadata["part_name"],
            "ppt/slides/slide2.xml",
        )
        self.assertEqual(
            document.metadata["slide_coordinate_space"],
            "presentation-relationship-order",
        )

    def test_pptx_empty_slide_and_content_limits_are_explicit(self) -> None:
        data = self.pptx_bytes(
            order=["rId1", "rId2"],
            relationships=[
                ("rId1", "slides/slide1.xml", None, SLIDE_REL_TYPE),
                ("rId2", "slides/slide2.xml", None, SLIDE_REL_TYPE),
            ],
            slides={
                "slides/slide1.xml": [],
                "slides/slide2.xml": ["abcdef"],
            },
        )
        full = extract_office_tabular_bytes(
            data,
            relative_path="slides/deck.pptx",
            format_value="pptx",
        )
        slide_limited = extract_office_tabular_bytes(
            data,
            relative_path="slides/deck.pptx",
            format_value="pptx",
            limits=OfficeTabularExtractionLimits(max_slides=1),
        )
        character_limited = extract_office_tabular_bytes(
            data,
            relative_path="slides/deck.pptx",
            format_value="pptx",
            limits=OfficeTabularExtractionLimits(max_block_characters=3),
        )

        self.assertEqual([block.text for block in self.require_document(full).blocks], ["", "abcdef"])
        self.assertEqual(slide_limited.status, "partial")
        self.assertIn(
            "slide-limit",
            self.require_document(slide_limited).metadata["partial_reasons"],
        )
        self.assertEqual(character_limited.status, "partial")
        limited_document = self.require_document(character_limited)
        self.assertEqual(limited_document.blocks[1].text, "abc")
        self.assertTrue(limited_document.blocks[1].truncated)
        self.assertEqual(
            limited_document.blocks[1].truncation_reason_code,
            "block-character-limit",
        )

    def test_pptx_reopening_returns_exact_current_slide(self) -> None:
        data = self.simple_pptx()

        text = reopen_office_locator_bytes(
            data,
            relative_path="slides/deck.pptx",
            format_value="pptx",
            locator=SlideLocator(1),
        )

        self.assertEqual(text, "filename two\nsecond paragraph")
        with self.assertRaises(OfficeTabularLocatorError) as raised:
            reopen_office_locator_bytes(
                data,
                relative_path="slides/deck.pptx",
                format_value="pptx",
                locator=SlideLocator(3),
            )
        self.assertEqual(raised.exception.reason_code, "pptx-slide-out-of-bounds")

    def test_pptx_reopening_fails_instead_of_returning_truncated_content(self) -> None:
        data = self.simple_pptx()
        cases = [
            (OfficeTabularExtractionLimits(max_slides=1), "pptx-slide-limit"),
            (
                OfficeTabularExtractionLimits(max_block_characters=3),
                "block-character-limit",
            ),
        ]
        for limits, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                with self.assertRaises(OfficeTabularFormatError) as raised:
                    reopen_office_locator_bytes(
                        data,
                        relative_path="slides/deck.pptx",
                        format_value="pptx",
                        locator=SlideLocator(1),
                        limits=limits,
                    )
                self.assertEqual(raised.exception.reason_code, reason_code)

    def test_pptx_external_unsafe_missing_and_invalid_relationships_fail_closed(self) -> None:
        cases = [
            (
                self.pptx_bytes(
                    order=["rId1"],
                    relationships=[
                        (
                            "rId1",
                            "https://example.invalid/slide.xml",
                            "External",
                            SLIDE_REL_TYPE,
                        )
                    ],
                    slides={},
                ),
                "pptx-slide-target-external",
            ),
            (
                self.pptx_bytes(
                    order=["rId1"],
                    relationships=[
                        ("rId1", "../../escape.xml", None, SLIDE_REL_TYPE)
                    ],
                    slides={},
                ),
                "pptx-slide-target-unsafe",
            ),
            (
                self.pptx_bytes(order=["rIdMissing"], relationships=[], slides={}),
                "pptx-slide-relationship-missing",
            ),
            (
                self.pptx_bytes(
                    order=["rId1"],
                    relationships=[
                        ("rId1", "slides/missing.xml", None, SLIDE_REL_TYPE)
                    ],
                    slides={},
                ),
                "ooxml-required-part-missing",
            ),
            (
                self.pptx_bytes(
                    order=["rId1"],
                    relationships=[
                        ("rId1", "slides/slide1.xml", None, "urn:not-a-slide")
                    ],
                    slides={"slides/slide1.xml": ["text"]},
                ),
                "pptx-slide-relationship-invalid",
            ),
        ]
        for data, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                result = extract_office_tabular_bytes(
                    data,
                    relative_path="slides/broken.pptx",
                    format_value="pptx",
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.reason_code, reason_code)
                self.assertIsNone(result.document)

    def test_pptx_malformed_archive_and_xml_fail_closed(self) -> None:
        malformed_archive = extract_office_tabular_bytes(
            b"not a package",
            relative_path="slides/broken.pptx",
            format_value="pptx",
        )
        self.assertEqual(malformed_archive.status, "failed")
        self.assertEqual(malformed_archive.reason_code, "ooxml-archive-invalid")

        malformed_xml = self.pptx_bytes(
            order=[],
            relationships=[],
            slides={},
            presentation_xml=b"<p:presentation>",
        )
        result = extract_office_tabular_bytes(
            malformed_xml,
            relative_path="slides/broken.pptx",
            format_value="pptx",
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason_code, "ooxml-xml-invalid")
        self.assertIsNone(result.document)

    def test_reopening_rejects_wrong_locator_classes(self) -> None:
        cases = [
            (b"a,b\n", "csv", ParagraphLocator(0)),
            (self.xlsx_bytes({"Sheet": [["x"]]}), "xlsx", SlideLocator(1)),
            (
                self.docx_bytes(self.docx_paragraph("x")),
                "docx",
                TableRangeLocator("Sheet", "A1", "A1"),
            ),
            (self.simple_pptx(), "pptx", ParagraphLocator(0)),
        ]
        for data, format_value, locator in cases:
            with self.subTest(format_value=format_value):
                with self.assertRaises(OfficeTabularLocatorError) as raised:
                    reopen_office_locator_bytes(
                        data,
                        relative_path=f"source.{format_value}",
                        format_value=format_value,
                        locator=locator,
                    )
                self.assertEqual(
                    raised.exception.reason_code,
                    "office-tabular-locator-type-mismatch",
                )

    def test_reopening_separates_unsupported_and_malformed_format_errors(self) -> None:
        with self.assertRaises(OfficeTabularFormatError) as unsupported:
            reopen_office_locator_bytes(
                b"data",
                relative_path="papers/source.pdf",
                format_value="pdf",
                locator=ParagraphLocator(0),
            )
        self.assertEqual(
            unsupported.exception.reason_code,
            "office-tabular-format-unsupported",
        )

        with self.assertRaises(OfficeTabularFormatError) as malformed:
            reopen_office_locator_bytes(
                b"not a docx",
                relative_path="notes/source.docx",
                format_value="docx",
                locator=ParagraphLocator(0),
            )
        self.assertEqual(malformed.exception.reason_code, "ooxml-archive-invalid")

    def test_extraction_performs_no_network_calls_or_hidden_persistence(self) -> None:
        fixtures = [
            (b"a,b\n1,2\n", "csv", "tables/source.csv"),
            (
                self.xlsx_bytes({"Sheet": [["x"]]}),
                "xlsx",
                "tables/source.xlsx",
            ),
            (
                self.docx_bytes(self.docx_paragraph("x")),
                "docx",
                "notes/source.docx",
            ),
            (self.simple_pptx(), "pptx", "slides/source.pptx"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker.txt"
            marker.write_text("unchanged", encoding="utf-8")
            original_cwd = Path.cwd()
            os.chdir(root)
            try:
                with patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("network access attempted"),
                ):
                    for data, format_value, relative_path in fixtures:
                        with self.subTest(format_value=format_value):
                            result = extract_office_tabular_bytes(
                                data,
                                relative_path=relative_path,
                                format_value=format_value,
                            )
                            self.assertIn(result.status, {"processed", "partial"})
            finally:
                os.chdir(original_cwd)
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(
                sorted(path.relative_to(root).as_posix() for path in root.rglob("*")),
                ["marker.txt"],
            )
            self.assertFalse((root / ".llmwiki").exists())
            self.assertFalse((root / "wiki").exists())


if __name__ == "__main__":
    unittest.main()
