from __future__ import annotations

import copy
import hashlib
import json
import unittest
from dataclasses import replace
from typing import Any

from tools.chunking import (
    CHUNKED_DOCUMENT_KIND,
    CHUNKING_SCHEMA_VERSION,
    ChunkingError,
    ChunkingLimits,
    ChunkingSchemaError,
    chunk_document,
    chunked_document_from_dict,
    deserialize_chunked_document,
    serialize_boundary_snapshot,
    serialize_chunked_document,
    validate_chunk_coverage,
)
from tools.extraction_schema import (
    EXTRACTION_SCHEMA_VERSION,
    LOCATOR_KIND,
    Block,
    ExtractedDocument,
    LineRangeLocator,
    Locator,
    NotebookCellLocator,
    PdfPageLocator,
    SectionLocator,
    SymbolLocator,
    TableRangeLocator,
)


HASH = "a" * 64


def document_with(*blocks: Block, path: str = "src/sample.py") -> ExtractedDocument:
    return ExtractedDocument(
        path=path,
        content_sha256=HASH,
        format="python",
        extractor="fixture-extractor",
        extractor_version="1",
        encoding="utf-8",
        blocks=blocks,
        metadata={"fixture": True},
    )


class UnsupportedLocator(Locator):
    locator_type = "byte_offset"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "kind": LOCATOR_KIND,
            "locator_type": self.locator_type,
            "offset": 1,
        }


class ChunkingTests(unittest.TestCase):
    def line_document(self) -> ExtractedDocument:
        return document_with(
            Block(
                block_id="lines-10-12",
                block_type="code",
                text="a\nβ\ncc\n",
                locator=LineRangeLocator(10, 12),
                metadata={"language": "python"},
            )
        )

    def test_line_chunks_cover_exact_text_at_complete_utf8_line_boundaries(self) -> None:
        source = self.line_document()
        result = chunk_document(
            source,
            limits=ChunkingLimits(max_chunk_utf8_bytes=4),
        )

        self.assertEqual([chunk.text for chunk in result.chunks], ["a\n", "β\n", "cc\n"])
        self.assertEqual(
            [chunk.locator for chunk in result.chunks],
            [
                LineRangeLocator(10, 10),
                LineRangeLocator(11, 11),
                LineRangeLocator(12, 12),
            ],
        )
        self.assertEqual(
            [
                (chunk.start_utf8_byte, chunk.end_utf8_byte)
                for chunk in result.chunks
            ],
            [(0, 2), (2, 5), (5, 8)],
        )
        self.assertEqual("".join(chunk.text for chunk in result.chunks), source.blocks[0].text)
        validate_chunk_coverage(source, result)

    def test_mixed_newlines_are_preserved_at_exact_line_boundaries(self) -> None:
        source = document_with(
            Block(
                block_id="mixed-newlines",
                block_type="text",
                text="a\r\nb\rc\n",
                locator=LineRangeLocator(7, 9),
            )
        )

        result = chunk_document(source, limits=ChunkingLimits(3))

        self.assertEqual([chunk.text for chunk in result.chunks], ["a\r\n", "b\r", "c\n"])
        self.assertEqual(
            [chunk.locator for chunk in result.chunks],
            [
                LineRangeLocator(7, 7),
                LineRangeLocator(8, 8),
                LineRangeLocator(9, 9),
            ],
        )
        self.assertEqual("".join(chunk.text for chunk in result.chunks), source.blocks[0].text)
        validate_chunk_coverage(source, result)

    def test_section_and_symbol_chunks_retain_context_and_truncation(self) -> None:
        source = document_with(
            Block(
                block_id="section-method",
                block_type="markdown",
                text="A\nB\nC\n",
                locator=SectionLocator(("Methods", "Training"), 20, 22),
                metadata={"heading_level": 2},
                truncated=True,
                truncation_reason_code="source-byte-limit",
                truncation_reason="source content exceeded the extraction limit",
            ),
            Block(
                block_id="symbol-train",
                block_type="code",
                text="x\ny\n",
                locator=SymbolLocator("train", 50, 51),
                metadata={"language": "python"},
            ),
        )

        result = chunk_document(
            source,
            limits=ChunkingLimits(max_chunk_utf8_bytes=2),
        )

        section_chunks = result.chunks[:3]
        symbol_chunks = result.chunks[3:]
        self.assertEqual(
            [chunk.locator for chunk in section_chunks],
            [
                SectionLocator(("Methods", "Training"), 20, 20),
                SectionLocator(("Methods", "Training"), 21, 21),
                SectionLocator(("Methods", "Training"), 22, 22),
            ],
        )
        self.assertEqual(
            [chunk.locator for chunk in symbol_chunks],
            [SymbolLocator("train", 50, 50), SymbolLocator("train", 51, 51)],
        )
        for chunk in section_chunks:
            self.assertEqual(chunk.source_block_metadata, {"heading_level": 2})
            self.assertTrue(chunk.source_block_truncated)
            self.assertEqual(
                chunk.source_block_truncation_reason_code,
                "source-byte-limit",
            )
        validate_chunk_coverage(source, result)

    def test_page_cell_and_table_ranges_are_atomic_and_preserve_empty_text(self) -> None:
        locators = (
            PdfPageLocator(2),
            NotebookCellLocator(3, "cell-3"),
            TableRangeLocator("Data", "A1", "B2"),
        )
        source = document_with(
            *(
                Block(
                    block_id=f"atomic-{index}",
                    block_type="table" if index == 2 else "text",
                    text="" if index < 2 else "x",
                    locator=locator,
                )
                for index, locator in enumerate(locators)
            )
        )

        result = chunk_document(
            source,
            limits=ChunkingLimits(max_chunk_utf8_bytes=1),
        )

        self.assertEqual(len(result.chunks), 3)
        self.assertEqual([chunk.locator for chunk in result.chunks], list(locators))
        self.assertEqual([chunk.text for chunk in result.chunks], ["", "", "x"])
        self.assertEqual(
            [(chunk.start_utf8_byte, chunk.end_utf8_byte) for chunk in result.chunks],
            [(0, 0), (0, 0), (0, 1)],
        )
        validate_chunk_coverage(source, result)

    def test_oversized_atomic_locators_are_rejected_not_character_sliced(self) -> None:
        for locator in (
            PdfPageLocator(1),
            NotebookCellLocator(0, None),
            TableRangeLocator("Data", "A1", "C3"),
        ):
            with self.subTest(locator=locator.locator_type):
                source = document_with(
                    Block(
                        block_id="atomic",
                        block_type="text",
                        text="oversized",
                        locator=locator,
                    )
                )
                with self.assertRaisesRegex(ChunkingError, "atomic"):
                    chunk_document(
                        source,
                        limits=ChunkingLimits(max_chunk_utf8_bytes=4),
                    )

    def test_unrepresentable_line_cut_and_locator_mismatch_are_rejected(self) -> None:
        oversized_line = document_with(
            Block(
                block_id="long-line",
                block_type="text",
                text="abcdef\n",
                locator=LineRangeLocator(1, 1),
            )
        )
        mismatch = document_with(
            Block(
                block_id="bad-span",
                block_type="text",
                text="one line\n",
                locator=LineRangeLocator(1, 2),
            )
        )
        unsupported = document_with(
            Block(
                block_id="byte-offset",
                block_type="text",
                text="x",
                locator=UnsupportedLocator(),
            )
        )

        with self.assertRaisesRegex(ChunkingError, "unrepresentable locator"):
            chunk_document(
                oversized_line,
                limits=ChunkingLimits(max_chunk_utf8_bytes=4),
            )
        with self.assertRaisesRegex(ChunkingError, "line count"):
            chunk_document(mismatch)
        with self.assertRaisesRegex(ChunkingError, "unsupported locator"):
            chunk_document(unsupported)

    def test_empty_line_block_is_rejected_without_silent_omission(self) -> None:
        source = document_with(
            Block(
                block_id="empty-line",
                block_type="text",
                text="",
                locator=LineRangeLocator(1, 1),
            )
        )

        with self.assertRaisesRegex(ChunkingError, "line count"):
            chunk_document(source)

    def test_empty_extracted_document_produces_a_valid_empty_chunk_set(self) -> None:
        source = document_with()
        result = chunk_document(source, limits=ChunkingLimits(7))

        self.assertEqual(result.source_block_count, 0)
        self.assertEqual(result.chunks, ())
        validate_chunk_coverage(source, result)

    def test_boundary_snapshot_and_serialization_are_byte_stable(self) -> None:
        first = chunk_document(
            self.line_document(),
            limits=ChunkingLimits(max_chunk_utf8_bytes=4),
        )
        second = chunk_document(
            self.line_document(),
            limits=ChunkingLimits(max_chunk_utf8_bytes=4),
        )

        snapshot = serialize_boundary_snapshot(first)
        self.assertEqual(snapshot, serialize_boundary_snapshot(second))
        self.assertEqual(first.boundary_snapshot_sha256, second.boundary_snapshot_sha256)
        self.assertEqual(
            hashlib.sha256(snapshot).hexdigest(),
            first.boundary_snapshot_sha256,
        )
        self.assertEqual(
            first.boundary_snapshot_sha256,
            "d225fc8714fd0917dc87cdf729b0d077525a98a79ea5f1559e2da5241bed8f63",
        )
        payload = serialize_chunked_document(first)
        self.assertEqual(payload, serialize_chunked_document(second))
        self.assertEqual(deserialize_chunked_document(payload), first)
        self.assertEqual(deserialize_chunked_document(payload.encode("utf-8")), first)

    def test_schema_rejects_missing_future_extra_and_tampered_boundary_hash(self) -> None:
        result = chunk_document(self.line_document(), limits=ChunkingLimits(4))
        valid = result.as_dict()
        cases: dict[str, dict[str, Any]] = {
            "missing version": {key: value for key, value in valid.items() if key != "schema_version"},
            "future version": {
                **valid,
                "schema_version": CHUNKING_SCHEMA_VERSION + 1,
            },
            "extra field": {**valid, "unexpected": True},
            "wrong kind": {**valid, "kind": "other"},
            "tampered snapshot": {
                **valid,
                "boundary_snapshot_sha256": "0" * 64,
            },
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ChunkingSchemaError):
                    chunked_document_from_dict(payload)

    def test_schema_versions_reject_booleans_at_every_record_level(self) -> None:
        result = chunk_document(self.line_document(), limits=ChunkingLimits(4))
        valid = result.as_dict()

        boolean_document = copy.deepcopy(valid)
        boolean_document["schema_version"] = True
        boolean_chunk = copy.deepcopy(valid)
        boolean_chunk["chunks"][0]["schema_version"] = True
        boolean_locator = copy.deepcopy(valid)
        boolean_locator["chunks"][0]["locator"]["schema_version"] = True
        boolean_source_locator = copy.deepcopy(valid)
        boolean_source_locator["chunks"][0]["source_block_locator"][
            "schema_version"
        ] = True

        for name, payload in {
            "document": boolean_document,
            "chunk": boolean_chunk,
            "locator": boolean_locator,
            "source locator": boolean_source_locator,
        }.items():
            with self.subTest(name=name):
                with self.assertRaises(ChunkingSchemaError):
                    chunked_document_from_dict(payload)

    def test_nested_schema_and_ordering_fail_closed(self) -> None:
        result = chunk_document(self.line_document(), limits=ChunkingLimits(4))
        valid = result.as_dict()

        extra_chunk = copy.deepcopy(valid)
        extra_chunk["chunks"][0]["unexpected"] = True
        reversed_chunks = copy.deepcopy(valid)
        reversed_chunks["chunks"].reverse()
        bad_hash = copy.deepcopy(valid)
        bad_hash["chunks"][0]["text_sha256"] = "0" * 64
        bad_offset = copy.deepcopy(valid)
        bad_offset["chunks"][1]["start_utf8_byte"] = 1

        for name, payload in {
            "extra chunk field": extra_chunk,
            "reversed order": reversed_chunks,
            "bad text hash": bad_hash,
            "overlap": bad_offset,
        }.items():
            with self.subTest(name=name):
                with self.assertRaises(ChunkingSchemaError):
                    chunked_document_from_dict(payload)

    def test_json_loader_rejects_duplicates_nonfinite_and_invalid_utf8(self) -> None:
        duplicate = '{"schema_version":1,"schema_version":1}'
        nonfinite = '{"schema_version":NaN}'
        for payload in (duplicate, nonfinite, b"\xff"):
            with self.subTest(payload=payload):
                with self.assertRaises(ChunkingSchemaError):
                    deserialize_chunked_document(payload)

    def test_coverage_validation_rejects_a_different_extracted_document(self) -> None:
        source = self.line_document()
        result = chunk_document(source, limits=ChunkingLimits(4))
        changed = document_with(
            replace(source.blocks[0], metadata={"language": "changed"})
        )

        with self.assertRaisesRegex(ChunkingError, "does not identify"):
            validate_chunk_coverage(changed, result)

    def test_limits_reject_booleans_and_non_positive_values(self) -> None:
        for value in (0, -1, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ChunkingLimits(value)

    def test_chunked_document_kind_and_version_are_explicit(self) -> None:
        result = chunk_document(self.line_document(), limits=ChunkingLimits(4))
        payload = json.loads(serialize_chunked_document(result))

        self.assertEqual(payload["schema_version"], CHUNKING_SCHEMA_VERSION)
        self.assertEqual(payload["kind"], CHUNKED_DOCUMENT_KIND)
        for chunk in payload["chunks"]:
            self.assertEqual(chunk["schema_version"], CHUNKING_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
