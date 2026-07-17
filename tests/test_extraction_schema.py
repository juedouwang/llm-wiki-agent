from __future__ import annotations

import json
import unittest

from tools.extraction_schema import (
    BLOCK_KIND,
    DOCUMENT_KIND,
    EXTRACTION_SCHEMA_VERSION,
    LOCATOR_KIND,
    RESULT_KIND,
    Block,
    ExtractedDocument,
    ExtractionResult,
    ExtractionSchemaError,
    ImageRegionLocator,
    LineRangeLocator,
    Locator,
    NotebookCellLocator,
    PdfPageLocator,
    SectionLocator,
    SymbolLocator,
    TableRangeLocator,
    block_from_dict,
    deserialize_extraction_result,
    document_from_dict,
    extraction_result_from_dict,
    locator_from_dict,
    serialize_extraction_result,
)


HASH = "a" * 64


class ExtractionSchemaTests(unittest.TestCase):
    def locator_fixtures(self) -> list[Locator]:
        return [
            LineRangeLocator(start_line=1, end_line=4),
            PdfPageLocator(page_number=3),
            ImageRegionLocator(
                frame_index=0,
                x=12,
                y=34,
                width=640,
                height=480,
            ),
            NotebookCellLocator(cell_index=2, cell_id="cell-abc"),
            TableRangeLocator(sheet="Results", start_cell="A1", end_cell="C9"),
            SectionLocator(
                heading_path=("Methods", "Training"),
                start_line=20,
                end_line=42,
            ),
            SymbolLocator(symbol="train_model", start_line=44, end_line=90),
        ]

    def sample_document(self) -> ExtractedDocument:
        return ExtractedDocument(
            path="src/model.py",
            content_sha256=HASH,
            format="python",
            extractor="deterministic-text",
            extractor_version="1",
            encoding="utf-8",
            blocks=(
                Block(
                    block_id="lines-1-2",
                    block_type="code",
                    text="VALUE = 7\n",
                    locator=LineRangeLocator(1, 2),
                    metadata={"language": "python", "confidence": 1.0},
                ),
            ),
            metadata={"newline": "lf"},
        )

    def test_all_locator_variants_round_trip_with_versioned_shape(self) -> None:
        for locator in self.locator_fixtures():
            with self.subTest(locator_type=locator.locator_type):
                payload = locator.as_dict()
                self.assertEqual(payload["schema_version"], EXTRACTION_SCHEMA_VERSION)
                self.assertEqual(payload["kind"], LOCATOR_KIND)
                self.assertEqual(locator_from_dict(payload), locator)

    def test_image_region_locator_has_strict_schema_v1_shape(self) -> None:
        locator = ImageRegionLocator(
            frame_index=2,
            x=10,
            y=20,
            width=30,
            height=40,
        )
        payload = locator.as_dict()

        self.assertEqual(
            payload,
            {
                "schema_version": EXTRACTION_SCHEMA_VERSION,
                "kind": LOCATOR_KIND,
                "locator_type": "image_region",
                "frame_index": 2,
                "x": 10,
                "y": 20,
                "width": 30,
                "height": 40,
            },
        )
        self.assertEqual(locator_from_dict(payload), locator)

        invalid_payloads = {
            "legacy missing version": {
                key: value
                for key, value in payload.items()
                if key != "schema_version"
            },
            "future version": {
                **payload,
                "schema_version": EXTRACTION_SCHEMA_VERSION + 1,
            },
            "extra field": {**payload, "rotation": 90},
            "missing field": {
                key: value for key, value in payload.items() if key != "width"
            },
        }
        for name, invalid in invalid_payloads.items():
            with self.subTest(name=name):
                with self.assertRaises(ExtractionSchemaError):
                    locator_from_dict(invalid)

    def test_invalid_locator_coordinates_fail_closed(self) -> None:
        factories = {
            "zero line": lambda: LineRangeLocator(0, 1),
            "reversed line": lambda: LineRangeLocator(4, 3),
            "boolean line": lambda: LineRangeLocator(True, 2),
            "zero page": lambda: PdfPageLocator(0),
            "negative image frame": lambda: ImageRegionLocator(-1, 0, 0, 1, 1),
            "boolean image frame": lambda: ImageRegionLocator(True, 0, 0, 1, 1),
            "negative image x": lambda: ImageRegionLocator(0, -1, 0, 1, 1),
            "negative image y": lambda: ImageRegionLocator(0, 0, -1, 1, 1),
            "zero image width": lambda: ImageRegionLocator(0, 0, 0, 0, 1),
            "zero image height": lambda: ImageRegionLocator(0, 0, 0, 1, 0),
            "boolean image width": lambda: ImageRegionLocator(0, 0, 0, True, 1),
            "negative cell": lambda: NotebookCellLocator(-1),
            "blank cell id": lambda: NotebookCellLocator(0, " "),
            "lowercase A1": lambda: TableRangeLocator("Data", "a1", "B2"),
            "reversed A1": lambda: TableRangeLocator("Data", "C3", "B4"),
            "empty heading": lambda: SectionLocator((), 1, 2),
            "string heading path": lambda: SectionLocator("Methods", 1, 2),
            "blank symbol": lambda: SymbolLocator("", 1, 2),
        }
        for name, factory in factories.items():
            with self.subTest(name=name):
                with self.assertRaises(ExtractionSchemaError):
                    factory()

    def test_locator_rejects_unknown_extra_and_future_schema(self) -> None:
        valid = LineRangeLocator(1, 2).as_dict()
        cases = {
            "unknown type": {**valid, "locator_type": "byte_offset"},
            "extra field": {**valid, "offset": 0},
            "future schema": {
                **valid,
                "schema_version": EXTRACTION_SCHEMA_VERSION + 1,
            },
            "wrong kind": {**valid, "kind": "other"},
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ExtractionSchemaError):
                    locator_from_dict(payload)

    def test_block_round_trip_and_truncation_contract(self) -> None:
        block = Block(
            block_id="cell-3-output",
            block_type="output_summary",
            text="image/png output omitted",
            locator=NotebookCellLocator(3, "cell-3"),
            metadata={"mime_types": ["image/png"], "output_count": 1},
            truncated=True,
            truncation_reason_code="output-byte-limit",
            truncation_reason="output exceeded the deterministic byte limit",
        )

        payload = block.as_dict()
        self.assertEqual(payload["kind"], BLOCK_KIND)
        self.assertEqual(block_from_dict(payload), block)
        with self.assertRaises(ExtractionSchemaError):
            Block(
                block_id="bad",
                block_type="text",
                text="x",
                locator=LineRangeLocator(1, 1),
                truncated=True,
            )
        with self.assertRaises(ExtractionSchemaError):
            Block(
                block_id="bad",
                block_type="text",
                text="x",
                locator=LineRangeLocator(1, 1),
                truncation_reason_code="not-truncated",
                truncation_reason="not allowed",
            )

    def test_document_round_trip_and_unique_block_ids(self) -> None:
        document = self.sample_document()
        payload = document.as_dict()

        self.assertEqual(payload["kind"], DOCUMENT_KIND)
        self.assertEqual(document_from_dict(payload), document)
        duplicate = document.blocks[0]
        with self.assertRaisesRegex(ExtractionSchemaError, "unique"):
            ExtractedDocument(
                path=document.path,
                content_sha256=document.content_sha256,
                format=document.format,
                extractor=document.extractor,
                extractor_version=document.extractor_version,
                encoding=document.encoding,
                blocks=(duplicate, duplicate),
            )

    def test_document_rejects_unsafe_paths_hashes_and_non_json_metadata(self) -> None:
        base = self.sample_document()
        cases = {
            "parent path": {"path": "../source.py"},
            "absolute path": {"path": "/source.py"},
            "backslash path": {"path": "src\\source.py"},
            "bad hash": {"content_sha256": "ABC"},
            "bad format": {"format": "Python Source"},
            "nan metadata": {"metadata": {"value": float("nan")}},
            "object metadata": {"metadata": {"value": object()}},
        }
        values = {
            "path": base.path,
            "content_sha256": base.content_sha256,
            "format": base.format,
            "extractor": base.extractor,
            "extractor_version": base.extractor_version,
            "encoding": base.encoding,
            "blocks": base.blocks,
            "metadata": base.metadata,
        }
        for name, update in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ExtractionSchemaError):
                    ExtractedDocument(**{**values, **update})

    def test_result_status_document_combinations_are_explicit(self) -> None:
        document = self.sample_document()
        processed = ExtractionResult(
            status="processed",
            reason_code="deterministic-extraction-complete",
            reason="all supported source content was extracted",
            document=document,
        )
        failed = ExtractionResult(
            status="failed",
            reason_code="source-read-failed",
            reason="the source could not be read",
            document=None,
            diagnostics=("permission denied",),
        )

        self.assertEqual(processed.as_dict()["kind"], RESULT_KIND)
        self.assertEqual(extraction_result_from_dict(processed.as_dict()), processed)
        self.assertEqual(extraction_result_from_dict(failed.as_dict()), failed)
        invalid = [
            lambda: ExtractionResult(
                "processed",
                "missing-document",
                "no document",
                None,
            ),
            lambda: ExtractionResult(
                "failed",
                "fabricated-document",
                "document not allowed",
                document,
            ),
            lambda: ExtractionResult(
                "unknown",
                "unknown-status",
                "unsupported status",
                None,
            ),
            lambda: ExtractionResult(
                "failed",
                "bad code",
                "invalid reason code",
                None,
            ),
        ]
        for factory in invalid:
            with self.assertRaises(ExtractionSchemaError):
                factory()

    def test_stable_json_round_trip_preserves_unicode_and_locator(self) -> None:
        document = self.sample_document()
        unicode_block = Block(
            block_id="section-1",
            block_type="markdown",
            text="\u65b9\u6cd5\u4e0e\u7ed3\u679c\n",
            locator=SectionLocator(("\u65b9\u6cd5",), 3, 5),
        )
        document = ExtractedDocument(
            path=document.path,
            content_sha256=document.content_sha256,
            format=document.format,
            extractor=document.extractor,
            extractor_version=document.extractor_version,
            encoding=document.encoding,
            blocks=(document.blocks[0], unicode_block),
        )
        result = ExtractionResult(
            status="partial",
            reason_code="bounded-extraction",
            reason="some content was deterministically truncated",
            document=document,
            diagnostics=("one block reached its configured limit",),
        )

        serialized = serialize_extraction_result(result)
        restored = deserialize_extraction_result(serialized.encode("utf-8"))

        self.assertEqual(restored, result)
        self.assertEqual(serialize_extraction_result(restored), serialized)
        self.assertTrue(serialized.endswith("\n"))
        self.assertIn("\u65b9\u6cd5", serialized)
        self.assertEqual(json.loads(serialized), result.as_dict())

    def test_nested_future_versions_and_duplicate_json_keys_fail_closed(self) -> None:
        result = ExtractionResult(
            status="processed",
            reason_code="deterministic-extraction-complete",
            reason="complete",
            document=self.sample_document(),
        )
        cases = []
        top_future = result.as_dict()
        top_future["schema_version"] = EXTRACTION_SCHEMA_VERSION + 1
        cases.append(json.dumps(top_future))
        nested_future = result.as_dict()
        nested_future["document"]["blocks"][0]["locator"]["schema_version"] = (
            EXTRACTION_SCHEMA_VERSION + 1
        )
        cases.append(json.dumps(nested_future))
        cases.append(
            '{"schema_version":1,"schema_version":1,"kind":"x"}'
        )
        cases.append("{\"value\": NaN}")

        for payload in cases:
            with self.subTest(payload=payload[:50]):
                with self.assertRaises(ExtractionSchemaError):
                    deserialize_extraction_result(payload)

    def test_exact_schema_fields_reject_missing_or_extra_data(self) -> None:
        document = self.sample_document()
        payloads = [
            {**document.blocks[0].as_dict(), "extra": True},
            {key: value for key, value in document.as_dict().items() if key != "format"},
            {**ExtractionResult(
                "failed",
                "read-failed",
                "failed",
                None,
            ).as_dict(), "extra": True},
        ]
        readers = [
            block_from_dict,
            document_from_dict,
            extraction_result_from_dict,
        ]
        for reader, payload in zip(readers, payloads, strict=True):
            with self.assertRaises(ExtractionSchemaError):
                reader(payload)


if __name__ == "__main__":
    unittest.main()
