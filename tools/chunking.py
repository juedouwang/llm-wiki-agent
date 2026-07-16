#!/usr/bin/env python3
"""Deterministic C-08 chunking along reopenable C-01 locator boundaries."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from tools.extraction_schema import (
    BLOCK_TYPES,
    Block,
    ExtractedDocument,
    ExtractionSchemaError,
    LineRangeLocator,
    Locator,
    NotebookCellLocator,
    PdfPageLocator,
    SectionLocator,
    SymbolLocator,
    TableRangeLocator,
    locator_from_dict,
)


CHUNKING_SCHEMA_VERSION = 1
CHUNK_KIND = "llmwiki-locator-chunk"
CHUNKED_DOCUMENT_KIND = "llmwiki-chunked-document"
BOUNDARY_SNAPSHOT_KIND = "llmwiki-chunk-boundary-snapshot"
CHUNKER_NAME = "deterministic-locator-chunker"
CHUNKER_VERSION = "1"

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_STABLE_CODE_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_FORMAT_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:[._+-][a-z0-9]+)*")
_LINE_LOCATOR_TYPES = (LineRangeLocator, SectionLocator, SymbolLocator)
_ATOMIC_LOCATOR_TYPES = (PdfPageLocator, NotebookCellLocator, TableRangeLocator)


class ChunkingError(ValueError):
    """Raised when exact, locator-preserving chunking cannot be proven."""


class ChunkingSchemaError(ChunkingError):
    """Raised when persisted C-08 records fail closed validation."""


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChunkingSchemaError(f"{field_name} must be a non-empty string")
    return value


def _stable_code(value: object, field_name: str) -> str:
    text = _non_empty_text(value, field_name)
    if _STABLE_CODE_PATTERN.fullmatch(text) is None:
        raise ChunkingSchemaError(
            f"{field_name} must be a stable kebab-case code"
        )
    return text


def _format_code(value: object) -> str:
    text = _non_empty_text(value, "format")
    if _FORMAT_PATTERN.fullmatch(text) is None:
        raise ChunkingSchemaError("format must be a stable lowercase code")
    return text


def _sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ChunkingSchemaError(
            f"{field_name} must be 64 lowercase hexadecimal characters"
        )
    return value


def _relative_path(value: object) -> str:
    text = _non_empty_text(value, "path")
    path = PurePosixPath(text)
    if (
        "\\" in text
        or path.is_absolute()
        or path.as_posix() != text
        or any(part in {"", ".", ".."} for part in path.parts)
        or unicodedata.normalize("NFC", text) != text
    ):
        raise ChunkingSchemaError(
            "path must be normalized project-relative POSIX form"
        )
    return text


def _normalize_json_value(value: object, *, field_name: str) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if _is_integer(value):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ChunkingSchemaError(
                f"{field_name} contains a non-finite number"
            )
        return value
    if isinstance(value, (list, tuple)):
        return [
            _normalize_json_value(item, field_name=field_name) for item in value
        ]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ChunkingSchemaError(
                    f"{field_name} object keys must be strings"
                )
            normalized[key] = _normalize_json_value(item, field_name=field_name)
        return normalized
    raise ChunkingSchemaError(f"{field_name} must contain only JSON values")


def _metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ChunkingSchemaError("source_block_metadata must be an object")
    normalized = _normalize_json_value(value, field_name="source_block_metadata")
    assert isinstance(normalized, dict)
    return normalized


def _schema_object(
    value: object,
    *,
    kind: str,
    expected_fields: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ChunkingSchemaError(f"{label} must be an object")
    if set(value) != expected_fields:
        raise ChunkingSchemaError(
            f"{label} must contain exactly the Schema v1 fields"
        )
    version = value.get("schema_version")
    if not _is_integer(version) or version != CHUNKING_SCHEMA_VERSION:
        if _is_integer(version) and version > CHUNKING_SCHEMA_VERSION:
            raise ChunkingSchemaError(
                f"{label} schema_version {version} is newer than supported "
                f"version {CHUNKING_SCHEMA_VERSION}"
            )
        raise ChunkingSchemaError(
            f"{label} schema_version is missing, legacy, or unsupported"
        )
    if value.get("kind") != kind:
        raise ChunkingSchemaError(f"unexpected {label} kind {value.get('kind')!r}")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ChunkingSchemaError("record is not stable JSON data") from exc
    return (text + "\n").encode("utf-8")


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extracted_document_sha256(document: ExtractedDocument) -> str:
    """Hash the exact stable C-01 document record consumed by the chunker."""

    if not isinstance(document, ExtractedDocument):
        raise ChunkingSchemaError("document must be an ExtractedDocument")
    return hashlib.sha256(_canonical_json_bytes(document.as_dict())).hexdigest()


@dataclass(frozen=True)
class ChunkingLimits:
    """Bound chunks by UTF-8 bytes without inventing ungrounded coordinates."""

    max_chunk_utf8_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if (
            not _is_integer(self.max_chunk_utf8_bytes)
            or self.max_chunk_utf8_bytes < 1
        ):
            raise ValueError("max_chunk_utf8_bytes must be a positive integer")


def _chunk_id(source_block_id: str, chunk_index: int) -> str:
    return f"{source_block_id}#chunk-{chunk_index + 1}"


def _line_bounds(locator: Locator) -> tuple[int, int]:
    if isinstance(locator, _LINE_LOCATOR_TYPES):
        return locator.start_line, locator.end_line
    raise ChunkingError(
        f"locator {locator.locator_type!r} does not expose line boundaries"
    )


def _adjust_line_locator(
    locator: Locator,
    *,
    start_line: int,
    end_line: int,
) -> Locator:
    if isinstance(locator, LineRangeLocator):
        return LineRangeLocator(start_line, end_line)
    if isinstance(locator, SectionLocator):
        return SectionLocator(locator.heading_path, start_line, end_line)
    if isinstance(locator, SymbolLocator):
        return SymbolLocator(locator.symbol, start_line, end_line)
    raise ChunkingError(
        f"locator {locator.locator_type!r} cannot represent a line subdivision"
    )


def _split_source_lines(text: str, locator: Locator) -> list[str]:
    lines = text.splitlines(keepends=True)
    if text and not lines:
        lines = [text]
    start_line, end_line = _line_bounds(locator)
    expected = end_line - start_line + 1
    if len(lines) != expected:
        raise ChunkingError(
            "source block text line count does not match its locator span: "
            f"expected {expected}, found {len(lines)}"
        )
    return lines


@dataclass(frozen=True)
class Chunk:
    """One byte-bounded excerpt with an exact source locator."""

    chunk_id: str
    source_block_index: int
    source_block_id: str
    chunk_index: int
    block_type: str
    text: str
    text_sha256: str
    locator: Locator
    source_block_locator: Locator
    source_block_text_sha256: str
    source_block_utf8_bytes: int
    start_utf8_byte: int
    end_utf8_byte: int
    source_block_metadata: dict[str, Any] = field(default_factory=dict)
    source_block_truncated: bool = False
    source_block_truncation_reason_code: str | None = None
    source_block_truncation_reason: str | None = None

    def __post_init__(self) -> None:
        _non_empty_text(self.source_block_id, "source_block_id")
        if not _is_integer(self.source_block_index) or self.source_block_index < 0:
            raise ChunkingSchemaError(
                "source_block_index must be a non-negative integer"
            )
        if not _is_integer(self.chunk_index) or self.chunk_index < 0:
            raise ChunkingSchemaError("chunk_index must be a non-negative integer")
        expected_chunk_id = _chunk_id(self.source_block_id, self.chunk_index)
        if self.chunk_id != expected_chunk_id:
            raise ChunkingSchemaError(
                f"chunk_id must be deterministic value {expected_chunk_id!r}"
            )
        if self.block_type not in BLOCK_TYPES:
            raise ChunkingSchemaError(
                f"unsupported block_type {self.block_type!r}"
            )
        if not isinstance(self.text, str):
            raise ChunkingSchemaError("chunk text must be a string")
        if self.text_sha256 != _text_sha256(self.text):
            raise ChunkingSchemaError("text_sha256 does not match chunk text")
        if not isinstance(self.locator, Locator):
            raise ChunkingSchemaError("locator must be a Locator")
        if not isinstance(self.source_block_locator, Locator):
            raise ChunkingSchemaError("source_block_locator must be a Locator")
        _sha256(self.source_block_text_sha256, "source_block_text_sha256")
        if (
            not _is_integer(self.source_block_utf8_bytes)
            or self.source_block_utf8_bytes < 0
        ):
            raise ChunkingSchemaError(
                "source_block_utf8_bytes must be a non-negative integer"
            )
        for field_name, value in (
            ("start_utf8_byte", self.start_utf8_byte),
            ("end_utf8_byte", self.end_utf8_byte),
        ):
            if not _is_integer(value) or value < 0:
                raise ChunkingSchemaError(
                    f"{field_name} must be a non-negative integer"
                )
        if self.end_utf8_byte < self.start_utf8_byte:
            raise ChunkingSchemaError(
                "end_utf8_byte must be greater than or equal to start_utf8_byte"
            )
        if self.end_utf8_byte > self.source_block_utf8_bytes:
            raise ChunkingSchemaError(
                "chunk byte boundary exceeds source_block_utf8_bytes"
            )
        if self.end_utf8_byte - self.start_utf8_byte != len(
            self.text.encode("utf-8")
        ):
            raise ChunkingSchemaError(
                "chunk byte span does not match its UTF-8 text length"
            )
        object.__setattr__(
            self,
            "source_block_metadata",
            _metadata(self.source_block_metadata),
        )
        if not isinstance(self.source_block_truncated, bool):
            raise ChunkingSchemaError("source_block_truncated must be a boolean")
        if self.source_block_truncated:
            _stable_code(
                self.source_block_truncation_reason_code,
                "source_block_truncation_reason_code",
            )
            _non_empty_text(
                self.source_block_truncation_reason,
                "source_block_truncation_reason",
            )
        elif (
            self.source_block_truncation_reason_code is not None
            or self.source_block_truncation_reason is not None
        ):
            raise ChunkingSchemaError(
                "non-truncated source blocks must not carry truncation reasons"
            )
        if isinstance(self.locator, _LINE_LOCATOR_TYPES):
            _split_source_lines(self.text, self.locator)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CHUNKING_SCHEMA_VERSION,
            "kind": CHUNK_KIND,
            "chunk_id": self.chunk_id,
            "source_block_index": self.source_block_index,
            "source_block_id": self.source_block_id,
            "chunk_index": self.chunk_index,
            "block_type": self.block_type,
            "text": self.text,
            "text_sha256": self.text_sha256,
            "locator": self.locator.as_dict(),
            "source_block_locator": self.source_block_locator.as_dict(),
            "source_block_text_sha256": self.source_block_text_sha256,
            "source_block_utf8_bytes": self.source_block_utf8_bytes,
            "start_utf8_byte": self.start_utf8_byte,
            "end_utf8_byte": self.end_utf8_byte,
            "source_block_metadata": _metadata(self.source_block_metadata),
            "source_block_truncated": self.source_block_truncated,
            "source_block_truncation_reason_code": (
                self.source_block_truncation_reason_code
            ),
            "source_block_truncation_reason": self.source_block_truncation_reason,
        }


def _locator_from_dict(value: object) -> Locator:
    if isinstance(value, dict) and not _is_integer(value.get("schema_version")):
        raise ChunkingSchemaError(
            "invalid nested locator: schema_version must be an integer"
        )
    try:
        return locator_from_dict(value)
    except ExtractionSchemaError as exc:
        raise ChunkingSchemaError(f"invalid nested locator: {exc}") from exc


def chunk_from_dict(value: object) -> Chunk:
    """Strictly deserialize one C-08 chunk."""

    record = _schema_object(
        value,
        kind=CHUNK_KIND,
        expected_fields={
            "schema_version",
            "kind",
            "chunk_id",
            "source_block_index",
            "source_block_id",
            "chunk_index",
            "block_type",
            "text",
            "text_sha256",
            "locator",
            "source_block_locator",
            "source_block_text_sha256",
            "source_block_utf8_bytes",
            "start_utf8_byte",
            "end_utf8_byte",
            "source_block_metadata",
            "source_block_truncated",
            "source_block_truncation_reason_code",
            "source_block_truncation_reason",
        },
        label="chunk",
    )
    return Chunk(
        chunk_id=record["chunk_id"],
        source_block_index=record["source_block_index"],
        source_block_id=record["source_block_id"],
        chunk_index=record["chunk_index"],
        block_type=record["block_type"],
        text=record["text"],
        text_sha256=record["text_sha256"],
        locator=_locator_from_dict(record["locator"]),
        source_block_locator=_locator_from_dict(record["source_block_locator"]),
        source_block_text_sha256=record["source_block_text_sha256"],
        source_block_utf8_bytes=record["source_block_utf8_bytes"],
        start_utf8_byte=record["start_utf8_byte"],
        end_utf8_byte=record["end_utf8_byte"],
        source_block_metadata=record["source_block_metadata"],
        source_block_truncated=record["source_block_truncated"],
        source_block_truncation_reason_code=record[
            "source_block_truncation_reason_code"
        ],
        source_block_truncation_reason=record["source_block_truncation_reason"],
    )


def _same_line_context(locator: Locator, source_locator: Locator) -> bool:
    if type(locator) is not type(source_locator):
        return False
    if isinstance(locator, SectionLocator):
        assert isinstance(source_locator, SectionLocator)
        return locator.heading_path == source_locator.heading_path
    if isinstance(locator, SymbolLocator):
        assert isinstance(source_locator, SymbolLocator)
        return locator.symbol == source_locator.symbol
    return isinstance(locator, LineRangeLocator)


def _validate_chunk_group(
    chunks: list[Chunk],
    *,
    max_chunk_utf8_bytes: int,
) -> None:
    first = chunks[0]
    expected_start = 0
    combined_text: list[str] = []
    for expected_index, chunk in enumerate(chunks):
        if chunk.chunk_index != expected_index:
            raise ChunkingSchemaError(
                "chunk_index values must start at zero and be contiguous per block"
            )
        if chunk.start_utf8_byte != expected_start:
            raise ChunkingSchemaError(
                "chunk byte boundaries must be contiguous without overlap or omission"
            )
        if len(chunk.text.encode("utf-8")) > max_chunk_utf8_bytes:
            raise ChunkingSchemaError("chunk exceeds max_chunk_utf8_bytes")
        for field_name in (
            "source_block_id",
            "block_type",
            "source_block_locator",
            "source_block_text_sha256",
            "source_block_utf8_bytes",
            "source_block_metadata",
            "source_block_truncated",
            "source_block_truncation_reason_code",
            "source_block_truncation_reason",
        ):
            if getattr(chunk, field_name) != getattr(first, field_name):
                raise ChunkingSchemaError(
                    f"{field_name} must be identical across a source block"
                )
        expected_start = chunk.end_utf8_byte
        combined_text.append(chunk.text)
    if expected_start != first.source_block_utf8_bytes:
        raise ChunkingSchemaError(
            "chunks do not cover the complete source block byte range"
        )
    if _text_sha256("".join(combined_text)) != first.source_block_text_sha256:
        raise ChunkingSchemaError(
            "ordered chunk text does not match source_block_text_sha256"
        )

    source_locator = first.source_block_locator
    if isinstance(source_locator, _ATOMIC_LOCATOR_TYPES):
        if len(chunks) != 1 or chunks[0].locator != source_locator:
            raise ChunkingSchemaError(
                "page, Notebook cell, and table-range locators are atomic"
            )
        return
    if not isinstance(source_locator, _LINE_LOCATOR_TYPES):
        raise ChunkingSchemaError(
            f"unsupported source locator {source_locator.locator_type!r}"
        )
    source_start, source_end = _line_bounds(source_locator)
    expected_line = source_start
    for chunk in chunks:
        if not _same_line_context(chunk.locator, source_locator):
            raise ChunkingSchemaError(
                "line chunk must retain its source locator type and context"
            )
        start_line, end_line = _line_bounds(chunk.locator)
        if start_line != expected_line:
            raise ChunkingSchemaError(
                "line locators must be contiguous without overlap or omission"
            )
        expected_line = end_line + 1
    if expected_line != source_end + 1:
        raise ChunkingSchemaError(
            "line locators do not cover the complete source locator"
        )


@dataclass(frozen=True)
class ChunkedDocument:
    """Strict C-08 output tied to one exact extracted-document record."""

    path: str
    content_sha256: str
    format: str
    source_extractor: str
    source_extractor_version: str
    source_document_sha256: str
    max_chunk_utf8_bytes: int
    source_block_count: int
    chunks: tuple[Chunk, ...]
    chunker: str = CHUNKER_NAME
    chunker_version: str = CHUNKER_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _relative_path(self.path))
        _sha256(self.content_sha256, "content_sha256")
        object.__setattr__(self, "format", _format_code(self.format))
        _non_empty_text(self.source_extractor, "source_extractor")
        _non_empty_text(self.source_extractor_version, "source_extractor_version")
        _sha256(self.source_document_sha256, "source_document_sha256")
        if (
            not _is_integer(self.max_chunk_utf8_bytes)
            or self.max_chunk_utf8_bytes < 1
        ):
            raise ChunkingSchemaError(
                "max_chunk_utf8_bytes must be a positive integer"
            )
        if not _is_integer(self.source_block_count) or self.source_block_count < 0:
            raise ChunkingSchemaError(
                "source_block_count must be a non-negative integer"
            )
        _non_empty_text(self.chunker, "chunker")
        _non_empty_text(self.chunker_version, "chunker_version")
        chunks = tuple(self.chunks)
        if not all(isinstance(chunk, Chunk) for chunk in chunks):
            raise ChunkingSchemaError("chunks must contain only Chunk objects")
        object.__setattr__(self, "chunks", chunks)
        if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
            raise ChunkingSchemaError("chunk_id values must be unique")
        if self.source_block_count == 0:
            if chunks:
                raise ChunkingSchemaError(
                    "a zero-block source document must not contain chunks"
                )
            return
        if not chunks:
            raise ChunkingSchemaError("every source block must produce a chunk")

        grouped: dict[int, list[Chunk]] = {}
        previous_key: tuple[int, int] | None = None
        for chunk in chunks:
            key = (chunk.source_block_index, chunk.chunk_index)
            if previous_key is not None and key <= previous_key:
                raise ChunkingSchemaError(
                    "chunks must be ordered by source_block_index then chunk_index"
                )
            previous_key = key
            grouped.setdefault(chunk.source_block_index, []).append(chunk)
        if set(grouped) != set(range(self.source_block_count)):
            raise ChunkingSchemaError(
                "source block indexes must be contiguous and complete"
            )
        for group in grouped.values():
            _validate_chunk_group(
                group,
                max_chunk_utf8_bytes=self.max_chunk_utf8_bytes,
            )

    def boundary_snapshot_dict(self) -> dict[str, Any]:
        """Return the canonical boundary-only snapshot used by regression tests."""

        return {
            "schema_version": CHUNKING_SCHEMA_VERSION,
            "kind": BOUNDARY_SNAPSHOT_KIND,
            "path": self.path,
            "content_sha256": self.content_sha256,
            "source_document_sha256": self.source_document_sha256,
            "max_chunk_utf8_bytes": self.max_chunk_utf8_bytes,
            "boundaries": [
                {
                    "chunk_id": chunk.chunk_id,
                    "source_block_index": chunk.source_block_index,
                    "source_block_id": chunk.source_block_id,
                    "chunk_index": chunk.chunk_index,
                    "start_utf8_byte": chunk.start_utf8_byte,
                    "end_utf8_byte": chunk.end_utf8_byte,
                    "locator": chunk.locator.as_dict(),
                    "text_sha256": chunk.text_sha256,
                }
                for chunk in self.chunks
            ],
        }

    @property
    def boundary_snapshot_sha256(self) -> str:
        return hashlib.sha256(serialize_boundary_snapshot(self)).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CHUNKING_SCHEMA_VERSION,
            "kind": CHUNKED_DOCUMENT_KIND,
            "path": self.path,
            "content_sha256": self.content_sha256,
            "format": self.format,
            "source_extractor": self.source_extractor,
            "source_extractor_version": self.source_extractor_version,
            "source_document_sha256": self.source_document_sha256,
            "max_chunk_utf8_bytes": self.max_chunk_utf8_bytes,
            "source_block_count": self.source_block_count,
            "chunks": [chunk.as_dict() for chunk in self.chunks],
            "chunker": self.chunker,
            "chunker_version": self.chunker_version,
            "boundary_snapshot_sha256": self.boundary_snapshot_sha256,
        }


def _make_chunk(
    *,
    block: Block,
    source_block_index: int,
    chunk_index: int,
    text: str,
    locator: Locator,
    start_utf8_byte: int,
) -> Chunk:
    end_utf8_byte = start_utf8_byte + len(text.encode("utf-8"))
    return Chunk(
        chunk_id=_chunk_id(block.block_id, chunk_index),
        source_block_index=source_block_index,
        source_block_id=block.block_id,
        chunk_index=chunk_index,
        block_type=block.block_type,
        text=text,
        text_sha256=_text_sha256(text),
        locator=locator,
        source_block_locator=block.locator,
        source_block_text_sha256=_text_sha256(block.text),
        source_block_utf8_bytes=len(block.text.encode("utf-8")),
        start_utf8_byte=start_utf8_byte,
        end_utf8_byte=end_utf8_byte,
        source_block_metadata=block.metadata,
        source_block_truncated=block.truncated,
        source_block_truncation_reason_code=block.truncation_reason_code,
        source_block_truncation_reason=block.truncation_reason,
    )


def _chunk_block(
    block: Block,
    *,
    source_block_index: int,
    max_chunk_utf8_bytes: int,
) -> list[Chunk]:
    if isinstance(block.locator, _ATOMIC_LOCATOR_TYPES):
        if len(block.text.encode("utf-8")) > max_chunk_utf8_bytes:
            raise ChunkingError(
                f"source block {block.block_id!r} uses atomic "
                f"{block.locator.locator_type!r} coordinates and exceeds "
                "max_chunk_utf8_bytes"
            )
        return [
            _make_chunk(
                block=block,
                source_block_index=source_block_index,
                chunk_index=0,
                text=block.text,
                locator=block.locator,
                start_utf8_byte=0,
            )
        ]
    if not isinstance(block.locator, _LINE_LOCATOR_TYPES):
        raise ChunkingError(
            f"source block {block.block_id!r} has unsupported locator "
            f"{block.locator.locator_type!r}"
        )

    lines = _split_source_lines(block.text, block.locator)
    groups: list[tuple[int, int, str]] = []
    group_start = 0
    group_lines: list[str] = []
    group_bytes = 0
    for line_index, line in enumerate(lines):
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > max_chunk_utf8_bytes:
            source_line = _line_bounds(block.locator)[0] + line_index
            raise ChunkingError(
                f"source line {source_line} in block {block.block_id!r} exceeds "
                "max_chunk_utf8_bytes and cannot be cut without an "
                "unrepresentable locator"
            )
        if group_lines and group_bytes + line_bytes > max_chunk_utf8_bytes:
            groups.append(
                (
                    group_start,
                    line_index - 1,
                    "".join(group_lines),
                )
            )
            group_start = line_index
            group_lines = []
            group_bytes = 0
        group_lines.append(line)
        group_bytes += line_bytes
    if group_lines:
        groups.append((group_start, len(lines) - 1, "".join(group_lines)))

    chunks: list[Chunk] = []
    source_start_line, _source_end_line = _line_bounds(block.locator)
    start_utf8_byte = 0
    for chunk_index, (start_offset, end_offset, text) in enumerate(groups):
        locator = _adjust_line_locator(
            block.locator,
            start_line=source_start_line + start_offset,
            end_line=source_start_line + end_offset,
        )
        chunk = _make_chunk(
            block=block,
            source_block_index=source_block_index,
            chunk_index=chunk_index,
            text=text,
            locator=locator,
            start_utf8_byte=start_utf8_byte,
        )
        chunks.append(chunk)
        start_utf8_byte = chunk.end_utf8_byte
    return chunks


def chunk_document(
    document: ExtractedDocument,
    *,
    limits: ChunkingLimits | None = None,
) -> ChunkedDocument:
    """Chunk one extracted document without omission, overlap, or fake locators."""

    if not isinstance(document, ExtractedDocument):
        raise ChunkingSchemaError("document must be an ExtractedDocument")
    resolved_limits = limits if limits is not None else ChunkingLimits()
    if not isinstance(resolved_limits, ChunkingLimits):
        raise ChunkingSchemaError("limits must be ChunkingLimits")
    chunks: list[Chunk] = []
    for block_index, block in enumerate(document.blocks):
        chunks.extend(
            _chunk_block(
                block,
                source_block_index=block_index,
                max_chunk_utf8_bytes=resolved_limits.max_chunk_utf8_bytes,
            )
        )
    result = ChunkedDocument(
        path=document.path,
        content_sha256=document.content_sha256,
        format=document.format,
        source_extractor=document.extractor,
        source_extractor_version=document.extractor_version,
        source_document_sha256=extracted_document_sha256(document),
        max_chunk_utf8_bytes=resolved_limits.max_chunk_utf8_bytes,
        source_block_count=len(document.blocks),
        chunks=tuple(chunks),
    )
    validate_chunk_coverage(document, result)
    return result


def validate_chunk_coverage(
    document: ExtractedDocument,
    chunked: ChunkedDocument,
) -> None:
    """Prove ordered chunks exactly reproduce every source Block and locator."""

    if not isinstance(document, ExtractedDocument):
        raise ChunkingSchemaError("document must be an ExtractedDocument")
    if not isinstance(chunked, ChunkedDocument):
        raise ChunkingSchemaError("chunked must be a ChunkedDocument")
    expected_header = (
        document.path,
        document.content_sha256,
        document.format,
        document.extractor,
        document.extractor_version,
        extracted_document_sha256(document),
        len(document.blocks),
    )
    actual_header = (
        chunked.path,
        chunked.content_sha256,
        chunked.format,
        chunked.source_extractor,
        chunked.source_extractor_version,
        chunked.source_document_sha256,
        chunked.source_block_count,
    )
    if actual_header != expected_header:
        raise ChunkingError(
            "chunked document does not identify the supplied extracted document"
        )

    grouped: dict[int, list[Chunk]] = {}
    for chunk in chunked.chunks:
        grouped.setdefault(chunk.source_block_index, []).append(chunk)
    for block_index, block in enumerate(document.blocks):
        chunks = grouped[block_index]
        first = chunks[0]
        if "".join(chunk.text for chunk in chunks) != block.text:
            raise ChunkingError(
                f"chunks do not reproduce source block {block.block_id!r} exactly"
            )
        expected_block_fields = (
            block.block_id,
            block.block_type,
            block.locator,
            block.metadata,
            block.truncated,
            block.truncation_reason_code,
            block.truncation_reason,
            _text_sha256(block.text),
            len(block.text.encode("utf-8")),
        )
        actual_block_fields = (
            first.source_block_id,
            first.block_type,
            first.source_block_locator,
            first.source_block_metadata,
            first.source_block_truncated,
            first.source_block_truncation_reason_code,
            first.source_block_truncation_reason,
            first.source_block_text_sha256,
            first.source_block_utf8_bytes,
        )
        if actual_block_fields != expected_block_fields:
            raise ChunkingError(
                f"chunks do not preserve source block {block.block_id!r} metadata"
            )


def serialize_boundary_snapshot(chunked: ChunkedDocument) -> bytes:
    """Serialize only deterministic chunk boundaries as canonical UTF-8 bytes."""

    if not isinstance(chunked, ChunkedDocument):
        raise ChunkingSchemaError("chunked must be a ChunkedDocument")
    return _canonical_json_bytes(chunked.boundary_snapshot_dict())


def serialize_chunked_document(chunked: ChunkedDocument) -> str:
    """Serialize one strict C-08 document using stable human-readable JSON."""

    if not isinstance(chunked, ChunkedDocument):
        raise ChunkingSchemaError("chunked must be a ChunkedDocument")
    return (
        json.dumps(
            chunked.as_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )


def chunked_document_from_dict(value: object) -> ChunkedDocument:
    """Strictly deserialize one C-08 chunked document."""

    record = _schema_object(
        value,
        kind=CHUNKED_DOCUMENT_KIND,
        expected_fields={
            "schema_version",
            "kind",
            "path",
            "content_sha256",
            "format",
            "source_extractor",
            "source_extractor_version",
            "source_document_sha256",
            "max_chunk_utf8_bytes",
            "source_block_count",
            "chunks",
            "chunker",
            "chunker_version",
            "boundary_snapshot_sha256",
        },
        label="chunked document",
    )
    raw_chunks = record["chunks"]
    if not isinstance(raw_chunks, list):
        raise ChunkingSchemaError("chunks must be an array")
    result = ChunkedDocument(
        path=record["path"],
        content_sha256=record["content_sha256"],
        format=record["format"],
        source_extractor=record["source_extractor"],
        source_extractor_version=record["source_extractor_version"],
        source_document_sha256=record["source_document_sha256"],
        max_chunk_utf8_bytes=record["max_chunk_utf8_bytes"],
        source_block_count=record["source_block_count"],
        chunks=tuple(chunk_from_dict(chunk) for chunk in raw_chunks),
        chunker=record["chunker"],
        chunker_version=record["chunker_version"],
    )
    if record["boundary_snapshot_sha256"] != result.boundary_snapshot_sha256:
        raise ChunkingSchemaError("boundary_snapshot_sha256 does not match chunks")
    return result


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChunkingSchemaError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ChunkingSchemaError(
        f"non-finite JSON constant {value!r} is not supported"
    )


def deserialize_chunked_document(payload: str | bytes) -> ChunkedDocument:
    """Deserialize C-08 JSON, rejecting duplicate keys and non-finite values."""

    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ChunkingSchemaError(
                "chunked document JSON must be UTF-8"
            ) from exc
    elif isinstance(payload, str):
        text = payload
    else:
        raise ChunkingSchemaError("chunked document JSON must be str or bytes")
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ChunkingSchemaError(
            f"invalid chunked document JSON: {exc.msg}"
        ) from exc
    return chunked_document_from_dict(decoded)
