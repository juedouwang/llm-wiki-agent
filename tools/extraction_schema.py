#!/usr/bin/env python3
"""Versioned C-01 extraction contracts shared by deterministic extractors."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, ClassVar


EXTRACTION_SCHEMA_VERSION = 1
LOCATOR_KIND = "llmwiki-locator"
BLOCK_KIND = "llmwiki-extracted-block"
DOCUMENT_KIND = "llmwiki-extracted-document"
RESULT_KIND = "llmwiki-extraction-result"

BLOCK_TYPES = (
    "text",
    "code",
    "markdown",
    "table",
    "output_summary",
    "metadata",
)
EXTRACTION_STATUSES = (
    "processed",
    "partial",
    "failed",
    "unsupported",
)

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_STABLE_CODE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_FORMAT_CODE = re.compile(r"[a-z][a-z0-9]*(?:[._+-][a-z0-9]+)*")
_A1_CELL = re.compile(r"([A-Z]+)([1-9][0-9]*)")


class ExtractionSchemaError(ValueError):
    """Raised when extraction data is missing, malformed, or from the future."""


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtractionSchemaError(f"{field_name} must be a non-empty string")
    return value


def _stable_code(value: object, field_name: str) -> str:
    text = _non_empty_text(value, field_name)
    if not _STABLE_CODE.fullmatch(text):
        raise ExtractionSchemaError(
            f"{field_name} must be a stable kebab-case code"
        )
    return text


def _format_code(value: object) -> str:
    text = _non_empty_text(value, "format")
    if not _FORMAT_CODE.fullmatch(text):
        raise ExtractionSchemaError("format must be a stable lowercase code")
    return text


def _schema_object(
    value: object,
    *,
    kind: str,
    expected_fields: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExtractionSchemaError(f"{label} must be an object")
    if set(value) != expected_fields:
        raise ExtractionSchemaError(
            f"{label} must contain exactly the Schema v1 fields"
        )
    version = value.get("schema_version")
    if version != EXTRACTION_SCHEMA_VERSION:
        if _is_integer(version) and version > EXTRACTION_SCHEMA_VERSION:
            raise ExtractionSchemaError(
                f"{label} schema_version {version} is newer than supported "
                f"version {EXTRACTION_SCHEMA_VERSION}"
            )
        raise ExtractionSchemaError(
            f"{label} schema_version is missing, legacy, or unsupported"
        )
    if value.get("kind") != kind:
        raise ExtractionSchemaError(
            f"unexpected {label} kind {value.get('kind')!r}"
        )
    return value


def _validate_line_range(start_line: object, end_line: object) -> tuple[int, int]:
    if not _is_integer(start_line) or start_line < 1:
        raise ExtractionSchemaError("start_line must be a positive integer")
    if not _is_integer(end_line) or end_line < start_line:
        raise ExtractionSchemaError("end_line must be an integer >= start_line")
    return start_line, end_line


def _relative_path(value: object) -> str:
    path_text = _non_empty_text(value, "path")
    path = PurePosixPath(path_text)
    if (
        "\\" in path_text
        or path.is_absolute()
        or path_text != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or unicodedata.normalize("NFC", path_text) != path_text
    ):
        raise ExtractionSchemaError(
            "path must be normalized project-relative POSIX form"
        )
    return path_text


def _normalize_json_value(value: object, *, field_name: str) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if _is_integer(value):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExtractionSchemaError(f"{field_name} contains a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [
            _normalize_json_value(item, field_name=field_name) for item in value
        ]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExtractionSchemaError(
                    f"{field_name} object keys must be strings"
                )
            normalized[key] = _normalize_json_value(item, field_name=field_name)
        return normalized
    raise ExtractionSchemaError(f"{field_name} must contain only JSON values")


def _metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExtractionSchemaError("metadata must be an object")
    normalized = _normalize_json_value(value, field_name="metadata")
    assert isinstance(normalized, dict)
    return normalized


def _column_number(label: str) -> int:
    number = 0
    for character in label:
        number = number * 26 + (ord(character) - ord("A") + 1)
    return number


def _cell_position(value: object, field_name: str) -> tuple[str, int, int]:
    text = _non_empty_text(value, field_name)
    match = _A1_CELL.fullmatch(text)
    if match is None:
        raise ExtractionSchemaError(
            f"{field_name} must be an uppercase A1 cell reference"
        )
    return text, int(match.group(2)), _column_number(match.group(1))


class Locator:
    """Base class for one deterministic, source-reopenable location."""

    locator_type: ClassVar[str]

    def as_dict(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True)
class LineRangeLocator(Locator):
    """Inclusive, one-based line range in a text-like source."""

    locator_type: ClassVar[str] = "line_range"
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        _validate_line_range(self.start_line, self.end_line)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "kind": LOCATOR_KIND,
            "locator_type": self.locator_type,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


@dataclass(frozen=True)
class PdfPageLocator(Locator):
    """One-based PDF page location."""

    locator_type: ClassVar[str] = "pdf_page"
    page_number: int

    def __post_init__(self) -> None:
        if not _is_integer(self.page_number) or self.page_number < 1:
            raise ExtractionSchemaError("page_number must be a positive integer")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "kind": LOCATOR_KIND,
            "locator_type": self.locator_type,
            "page_number": self.page_number,
        }


@dataclass(frozen=True)
class ImageRegionLocator(Locator):
    """Pixel rectangle in one EXIF-normalized standalone raster frame."""

    locator_type: ClassVar[str] = "image_region"
    frame_index: int
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("frame_index", self.frame_index),
            ("x", self.x),
            ("y", self.y),
        ):
            if not _is_integer(value) or value < 0:
                raise ExtractionSchemaError(
                    f"{field_name} must be a non-negative integer"
                )
        for field_name, value in (("width", self.width), ("height", self.height)):
            if not _is_integer(value) or value < 1:
                raise ExtractionSchemaError(
                    f"{field_name} must be a positive integer"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "kind": LOCATOR_KIND,
            "locator_type": self.locator_type,
            "frame_index": self.frame_index,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class NotebookCellLocator(Locator):
    """Zero-based Notebook cell index with an optional persisted cell ID."""

    locator_type: ClassVar[str] = "notebook_cell"
    cell_index: int
    cell_id: str | None = None

    def __post_init__(self) -> None:
        if not _is_integer(self.cell_index) or self.cell_index < 0:
            raise ExtractionSchemaError(
                "cell_index must be a non-negative integer"
            )
        if self.cell_id is not None:
            _non_empty_text(self.cell_id, "cell_id")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "kind": LOCATOR_KIND,
            "locator_type": self.locator_type,
            "cell_index": self.cell_index,
            "cell_id": self.cell_id,
        }


@dataclass(frozen=True)
class TableRangeLocator(Locator):
    """Inclusive A1 cell range within one named sheet."""

    locator_type: ClassVar[str] = "table_range"
    sheet: str
    start_cell: str
    end_cell: str

    def __post_init__(self) -> None:
        _non_empty_text(self.sheet, "sheet")
        _start, start_row, start_column = _cell_position(
            self.start_cell,
            "start_cell",
        )
        _end, end_row, end_column = _cell_position(self.end_cell, "end_cell")
        if end_row < start_row or end_column < start_column:
            raise ExtractionSchemaError(
                "end_cell must not precede start_cell by row or column"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "kind": LOCATOR_KIND,
            "locator_type": self.locator_type,
            "sheet": self.sheet,
            "start_cell": self.start_cell,
            "end_cell": self.end_cell,
        }


@dataclass(frozen=True)
class SectionLocator(Locator):
    """Heading path plus inclusive source lines for a document section."""

    locator_type: ClassVar[str] = "section"
    heading_path: tuple[str, ...]
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        if not isinstance(self.heading_path, (list, tuple)):
            raise ExtractionSchemaError("heading_path must be an array")
        headings = tuple(self.heading_path)
        if not headings:
            raise ExtractionSchemaError("heading_path must contain at least one heading")
        for heading in headings:
            _non_empty_text(heading, "heading_path item")
        _validate_line_range(self.start_line, self.end_line)
        object.__setattr__(self, "heading_path", headings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "kind": LOCATOR_KIND,
            "locator_type": self.locator_type,
            "heading_path": list(self.heading_path),
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


@dataclass(frozen=True)
class SymbolLocator(Locator):
    """Named source symbol plus the inclusive lines that define it."""

    locator_type: ClassVar[str] = "symbol"
    symbol: str
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        _non_empty_text(self.symbol, "symbol")
        _validate_line_range(self.start_line, self.end_line)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "kind": LOCATOR_KIND,
            "locator_type": self.locator_type,
            "symbol": self.symbol,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


_LOCATOR_FIELDS = {
    "line_range": {"start_line", "end_line"},
    "pdf_page": {"page_number"},
    "image_region": {"frame_index", "x", "y", "width", "height"},
    "notebook_cell": {"cell_index", "cell_id"},
    "table_range": {"sheet", "start_cell", "end_cell"},
    "section": {"heading_path", "start_line", "end_line"},
    "symbol": {"symbol", "start_line", "end_line"},
}


def locator_from_dict(value: object) -> Locator:
    """Validate and deserialize one locator union member."""

    if not isinstance(value, dict):
        raise ExtractionSchemaError("locator must be an object")
    version = value.get("schema_version")
    if version != EXTRACTION_SCHEMA_VERSION:
        if _is_integer(version) and version > EXTRACTION_SCHEMA_VERSION:
            raise ExtractionSchemaError(
                f"locator schema_version {version} is newer than supported "
                f"version {EXTRACTION_SCHEMA_VERSION}"
            )
        raise ExtractionSchemaError(
            "locator schema_version is missing, legacy, or unsupported"
        )
    if value.get("kind") != LOCATOR_KIND:
        raise ExtractionSchemaError(
            f"unexpected locator kind {value.get('kind')!r}"
        )
    locator_type = value.get("locator_type")
    if not isinstance(locator_type, str) or locator_type not in _LOCATOR_FIELDS:
        raise ExtractionSchemaError(f"unsupported locator_type {locator_type!r}")
    record = _schema_object(
        value,
        kind=LOCATOR_KIND,
        expected_fields={
            "schema_version",
            "kind",
            "locator_type",
            *_LOCATOR_FIELDS[locator_type],
        },
        label="locator",
    )
    if locator_type == "line_range":
        return LineRangeLocator(record["start_line"], record["end_line"])
    if locator_type == "pdf_page":
        return PdfPageLocator(record["page_number"])
    if locator_type == "image_region":
        return ImageRegionLocator(
            record["frame_index"],
            record["x"],
            record["y"],
            record["width"],
            record["height"],
        )
    if locator_type == "notebook_cell":
        return NotebookCellLocator(record["cell_index"], record["cell_id"])
    if locator_type == "table_range":
        return TableRangeLocator(
            record["sheet"],
            record["start_cell"],
            record["end_cell"],
        )
    if locator_type == "section":
        heading_path = record["heading_path"]
        if not isinstance(heading_path, list):
            raise ExtractionSchemaError("heading_path must be an array")
        return SectionLocator(
            tuple(heading_path),
            record["start_line"],
            record["end_line"],
        )
    return SymbolLocator(
        record["symbol"],
        record["start_line"],
        record["end_line"],
    )


@dataclass(frozen=True)
class Block:
    """One independently locatable unit emitted by a deterministic extractor."""

    block_id: str
    block_type: str
    text: str
    locator: Locator
    metadata: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    truncation_reason_code: str | None = None
    truncation_reason: str | None = None

    def __post_init__(self) -> None:
        _non_empty_text(self.block_id, "block_id")
        if self.block_type not in BLOCK_TYPES:
            raise ExtractionSchemaError(
                f"unsupported block_type {self.block_type!r}"
            )
        if not isinstance(self.text, str):
            raise ExtractionSchemaError("block text must be a string")
        if not isinstance(self.locator, Locator):
            raise ExtractionSchemaError("block locator must be a Locator")
        object.__setattr__(self, "metadata", _metadata(self.metadata))
        if not isinstance(self.truncated, bool):
            raise ExtractionSchemaError("truncated must be a boolean")
        if self.truncated:
            _stable_code(
                self.truncation_reason_code,
                "truncation_reason_code",
            )
            _non_empty_text(self.truncation_reason, "truncation_reason")
        elif (
            self.truncation_reason_code is not None
            or self.truncation_reason is not None
        ):
            raise ExtractionSchemaError(
                "non-truncated blocks must not carry truncation reasons"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "kind": BLOCK_KIND,
            "block_id": self.block_id,
            "block_type": self.block_type,
            "text": self.text,
            "locator": self.locator.as_dict(),
            "metadata": _metadata(self.metadata),
            "truncated": self.truncated,
            "truncation_reason_code": self.truncation_reason_code,
            "truncation_reason": self.truncation_reason,
        }


def block_from_dict(value: object) -> Block:
    """Validate and deserialize one extracted block."""

    record = _schema_object(
        value,
        kind=BLOCK_KIND,
        expected_fields={
            "schema_version",
            "kind",
            "block_id",
            "block_type",
            "text",
            "locator",
            "metadata",
            "truncated",
            "truncation_reason_code",
            "truncation_reason",
        },
        label="block",
    )
    return Block(
        block_id=record["block_id"],
        block_type=record["block_type"],
        text=record["text"],
        locator=locator_from_dict(record["locator"]),
        metadata=record["metadata"],
        truncated=record["truncated"],
        truncation_reason_code=record["truncation_reason_code"],
        truncation_reason=record["truncation_reason"],
    )


@dataclass(frozen=True)
class ExtractedDocument:
    """Uniform deterministic extraction output for one Manifest file version."""

    path: str
    content_sha256: str
    format: str
    extractor: str
    extractor_version: str
    encoding: str | None
    blocks: tuple[Block, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _relative_path(self.path))
        if (
            not isinstance(self.content_sha256, str)
            or not _SHA256_PATTERN.fullmatch(self.content_sha256)
        ):
            raise ExtractionSchemaError(
                "content_sha256 must be 64 lowercase hexadecimal characters"
            )
        object.__setattr__(self, "format", _format_code(self.format))
        _non_empty_text(self.extractor, "extractor")
        _non_empty_text(self.extractor_version, "extractor_version")
        if self.encoding is not None:
            _non_empty_text(self.encoding, "encoding")
        blocks = tuple(self.blocks)
        if not all(isinstance(block, Block) for block in blocks):
            raise ExtractionSchemaError("blocks must contain only Block objects")
        block_ids = [block.block_id for block in blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ExtractionSchemaError("block_id values must be unique per document")
        object.__setattr__(self, "blocks", blocks)
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "kind": DOCUMENT_KIND,
            "path": self.path,
            "content_sha256": self.content_sha256,
            "format": self.format,
            "extractor": self.extractor,
            "extractor_version": self.extractor_version,
            "encoding": self.encoding,
            "blocks": [block.as_dict() for block in self.blocks],
            "metadata": _metadata(self.metadata),
        }


def document_from_dict(value: object) -> ExtractedDocument:
    """Validate and deserialize one extracted document."""

    record = _schema_object(
        value,
        kind=DOCUMENT_KIND,
        expected_fields={
            "schema_version",
            "kind",
            "path",
            "content_sha256",
            "format",
            "extractor",
            "extractor_version",
            "encoding",
            "blocks",
            "metadata",
        },
        label="extracted document",
    )
    raw_blocks = record["blocks"]
    if not isinstance(raw_blocks, list):
        raise ExtractionSchemaError("blocks must be an array")
    return ExtractedDocument(
        path=record["path"],
        content_sha256=record["content_sha256"],
        format=record["format"],
        extractor=record["extractor"],
        extractor_version=record["extractor_version"],
        encoding=record["encoding"],
        blocks=tuple(block_from_dict(block) for block in raw_blocks),
        metadata=record["metadata"],
    )


@dataclass(frozen=True)
class ExtractionResult:
    """Auditable extractor outcome without fabricated text on failure."""

    status: str
    reason_code: str
    reason: str
    document: ExtractedDocument | None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in EXTRACTION_STATUSES:
            raise ExtractionSchemaError(
                f"unsupported extraction status {self.status!r}"
            )
        _stable_code(self.reason_code, "reason_code")
        _non_empty_text(self.reason, "reason")
        if self.status in {"processed", "partial"}:
            if not isinstance(self.document, ExtractedDocument):
                raise ExtractionSchemaError(
                    f"{self.status} extraction requires a document"
                )
        elif self.document is not None:
            raise ExtractionSchemaError(
                f"{self.status} extraction must not fabricate a document"
            )
        if not isinstance(self.diagnostics, (list, tuple)):
            raise ExtractionSchemaError("diagnostics must be an array")
        diagnostics = tuple(self.diagnostics)
        for diagnostic in diagnostics:
            _non_empty_text(diagnostic, "diagnostic")
        object.__setattr__(self, "diagnostics", diagnostics)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "kind": RESULT_KIND,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "document": self.document.as_dict() if self.document is not None else None,
            "diagnostics": list(self.diagnostics),
        }


def extraction_result_from_dict(value: object) -> ExtractionResult:
    """Validate and deserialize one extractor result."""

    record = _schema_object(
        value,
        kind=RESULT_KIND,
        expected_fields={
            "schema_version",
            "kind",
            "status",
            "reason_code",
            "reason",
            "document",
            "diagnostics",
        },
        label="extraction result",
    )
    raw_document = record["document"]
    document = (
        document_from_dict(raw_document) if raw_document is not None else None
    )
    diagnostics = record["diagnostics"]
    if not isinstance(diagnostics, list):
        raise ExtractionSchemaError("diagnostics must be an array")
    return ExtractionResult(
        status=record["status"],
        reason_code=record["reason_code"],
        reason=record["reason"],
        document=document,
        diagnostics=tuple(diagnostics),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExtractionSchemaError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def serialize_extraction_result(result: ExtractionResult) -> str:
    """Serialize one result to stable UTF-8-compatible JSON text."""

    if not isinstance(result, ExtractionResult):
        raise ExtractionSchemaError("result must be an ExtractionResult")
    return (
        json.dumps(
            result.as_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )


def _reject_json_constant(value: str) -> Any:
    raise ExtractionSchemaError(
        f"non-finite JSON constant {value!r} is not supported"
    )


def deserialize_extraction_result(payload: str | bytes) -> ExtractionResult:
    """Deserialize stable JSON while rejecting duplicate keys and non-finite data."""

    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExtractionSchemaError(
                "extraction result JSON must be UTF-8"
            ) from exc
    elif isinstance(payload, str):
        text = payload
    else:
        raise ExtractionSchemaError("extraction result JSON must be str or bytes")
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ExtractionSchemaError(f"invalid extraction result JSON: {exc.msg}") from exc
    return extraction_result_from_dict(decoded)
