#!/usr/bin/env python3
"""Bounded deterministic C-06 Office and tabular extraction."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import posixpath
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlsplit
from xml.etree import ElementTree

if __package__:
    from .extraction_schema import (
        Block,
        ExtractedDocument,
        ExtractionResult,
        Locator,
        ParagraphLocator,
        SlideLocator,
        TableRangeLocator,
    )
else:
    from extraction_schema import (  # type: ignore[no-redef]
        Block,
        ExtractedDocument,
        ExtractionResult,
        Locator,
        ParagraphLocator,
        SlideLocator,
        TableRangeLocator,
    )


OFFICE_TABULAR_EXTRACTOR_NAME = "deterministic-office-tabular"
OFFICE_TABULAR_EXTRACTOR_VERSION = "1"
SUPPORTED_OFFICE_TABULAR_FORMATS = frozenset(
    {"csv", "tsv", "xlsx", "docx", "pptx"}
)
TABLE_MATRIX_FORMAT = "table-json-matrix-v1"
DELIMITED_SHEETS = {"csv": "CSV", "tsv": "TSV"}

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_A1_CELL_PATTERN = re.compile(r"([A-Z]+)([1-9][0-9]*)")
_MAX_XLSX_ROW = 1_048_576
_MAX_XLSX_COLUMN = 16_384
_XML_FORBIDDEN_MARKERS = (b"<!doctype", b"<!entity")
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_SLIDE_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
)


class OfficeTabularExtractionError(ValueError):
    """Base error for safe Office/tabular parsing and reopening."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


class OfficeTabularFormatError(OfficeTabularExtractionError):
    """Raised when source bytes cannot be parsed safely as the requested format."""


class OfficeTabularLocatorError(OfficeTabularExtractionError):
    """Raised when a valid source does not contain the requested location."""


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class OfficeTabularExtractionLimits:
    """Deterministic resource bounds shared by extraction and exact reopening."""

    max_source_bytes: int = 64 * 1024 * 1024
    read_chunk_bytes: int = 1024 * 1024
    max_decoded_characters: int = 64 * 1024 * 1024
    max_rows: int = 100_000
    max_columns: int = 4_096
    max_cells: int = 1_000_000
    max_rows_per_block: int = 1_000
    max_cell_characters: int = 100_000
    max_sheets: int = 128
    max_paragraphs: int = 100_000
    max_slides: int = 10_000
    max_block_characters: int = 500_000
    max_archive_entries: int = 10_000
    max_archive_member_bytes: int = 64 * 1024 * 1024
    max_archive_uncompressed_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        for field_name, value in self.__dict__.items():
            if not _is_integer(value) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.max_columns > _MAX_XLSX_COLUMN:
            raise ValueError(
                f"max_columns must not exceed the XLSX limit {_MAX_XLSX_COLUMN}"
            )
        if self.max_rows > _MAX_XLSX_ROW:
            raise ValueError(f"max_rows must not exceed the XLSX limit {_MAX_XLSX_ROW}")


@dataclass(frozen=True)
class _ReadResult:
    data: bytes
    content_sha256: str
    source_size_bytes: int


@dataclass(frozen=True)
class _ArchiveStats:
    entry_count: int
    uncompressed_bytes: int


@dataclass(frozen=True)
class _BuildResult:
    blocks: tuple[Block, ...]
    encoding: str | None
    metadata: dict[str, Any]
    partial_reasons: tuple[str, ...]


@dataclass(frozen=True)
class _TableRows:
    rows: tuple[tuple[object, ...], ...]
    cell_truncated_rows: frozenset[int]
    partial_reasons: tuple[str, ...]
    encoding: str | None
    metadata: dict[str, Any]


def _failed_result(reason_code: str, reason: str, *diagnostics: str) -> ExtractionResult:
    return ExtractionResult(
        status="failed",
        reason_code=reason_code,
        reason=reason,
        document=None,
        diagnostics=tuple(diagnostics),
    )


def _unsupported_result(format_value: object) -> ExtractionResult:
    return ExtractionResult(
        status="unsupported",
        reason_code="office-tabular-format-unsupported",
        reason="the requested format is outside the C-06 Office/tabular family",
        document=None,
        diagnostics=(f"format was {format_value!r}",),
    )


def _validate_expected_sha256(value: str | None) -> None:
    if value is not None and (
        not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(
            "expected_sha256 must be 64 lowercase hexadecimal characters"
        )


def _read_and_hash(path: Path, limits: OfficeTabularExtractionLimits) -> _ReadResult:
    digest = hashlib.sha256()
    retained = bytearray()
    source_size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(limits.read_chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
            source_size += len(chunk)
            if len(retained) < limits.max_source_bytes:
                remaining = limits.max_source_bytes - len(retained)
                retained.extend(chunk[:remaining])
    return _ReadResult(bytes(retained), digest.hexdigest(), source_size)


def _column_number(name: str) -> int:
    result = 0
    for character in name:
        result = result * 26 + (ord(character) - ord("A") + 1)
    return result


def _column_name(number: int) -> str:
    if not _is_integer(number) or number < 1 or number > _MAX_XLSX_COLUMN:
        raise OfficeTabularLocatorError(
            "table-column-out-of-bounds",
            f"column number {number!r} is outside XLSX A1 bounds",
        )
    characters: list[str] = []
    current = number
    while current:
        current, remainder = divmod(current - 1, 26)
        characters.append(chr(ord("A") + remainder))
    return "".join(reversed(characters))


def _cell_coordinates(value: str) -> tuple[int, int]:
    match = _A1_CELL_PATTERN.fullmatch(value)
    if match is None:
        raise OfficeTabularLocatorError(
            "table-cell-invalid", f"invalid uppercase A1 cell reference {value!r}"
        )
    column = _column_number(match.group(1))
    row = int(match.group(2))
    if row > _MAX_XLSX_ROW or column > _MAX_XLSX_COLUMN:
        raise OfficeTabularLocatorError(
            "table-cell-out-of-bounds",
            f"cell {value!r} exceeds XLSX worksheet bounds",
        )
    return row, column


def _locator_bounds(locator: TableRangeLocator) -> tuple[int, int, int, int]:
    start_row, start_column = _cell_coordinates(locator.start_cell)
    end_row, end_column = _cell_coordinates(locator.end_cell)
    return start_row, start_column, end_row, end_column


def _canonical_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OfficeTabularFormatError(
                "table-non-finite-number", "table cell contains a non-finite number"
            )
        return value
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"type": "time", "value": value.isoformat()}
    if isinstance(value, timedelta):
        return {
            "type": "timedelta",
            "microseconds": (
                value.days * 86_400_000_000
                + value.seconds * 1_000_000
                + value.microseconds
            ),
        }
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise OfficeTabularFormatError(
                "table-non-finite-number", "table cell contains a non-finite number"
            )
        return {"type": "decimal", "value": str(value)}
    raise OfficeTabularFormatError(
        "table-cell-type-unsupported",
        f"table cell has unsupported value type {type(value).__name__}",
    )


def _canonical_matrix(rows: Iterable[Iterable[object]]) -> str:
    matrix = [[_canonical_json_value(value) for value in row] for row in rows]
    try:
        return json.dumps(
            matrix,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise OfficeTabularFormatError(
            "table-matrix-serialization-failed",
            "table matrix cannot be serialized canonically",
        ) from exc


def _decode_delimited(data: bytes, limits: OfficeTabularExtractionLimits) -> tuple[str, str]:
    attempts: list[tuple[str, str]]
    if data.startswith(b"\xef\xbb\xbf"):
        attempts = [("utf-8-sig", "utf-8-sig")]
    elif data.startswith(b"\xff\xfe"):
        attempts = [("utf-16", "utf-16-le")]
    elif data.startswith(b"\xfe\xff"):
        attempts = [("utf-16", "utf-16-be")]
    else:
        attempts = [("utf-8", "utf-8"), ("cp1252", "cp1252")]
    last_error: UnicodeError | None = None
    for codec, label in attempts:
        try:
            text = data.decode(codec, errors="strict")
        except UnicodeError as exc:
            last_error = exc
            continue
        if len(text) > limits.max_decoded_characters:
            raise OfficeTabularFormatError(
                "delimited-character-limit",
                "decoded delimited text exceeds the deterministic character limit",
            )
        if "\x00" in text:
            raise OfficeTabularFormatError(
                "delimited-binary-contradiction",
                "decoded delimited text contains NUL bytes",
            )
        return text, label
    raise OfficeTabularFormatError(
        "delimited-decode-failed",
        f"delimited text could not be decoded strictly: {last_error}",
    )



def _validate_archive(
    data: bytes, limits: OfficeTabularExtractionLimits
) -> _ArchiveStats:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise OfficeTabularFormatError(
            "ooxml-archive-invalid", f"OOXML ZIP archive could not be opened: {exc}"
        ) from exc
    try:
        infos = archive.infolist()
        if len(infos) > limits.max_archive_entries:
            raise OfficeTabularFormatError(
                "ooxml-entry-limit",
                "OOXML archive exceeds the deterministic member-count limit",
            )
        seen: set[str] = set()
        total = 0
        for info in infos:
            name = info.filename
            is_directory = info.is_dir()
            normalized_name = name[:-1] if is_directory and name.endswith("/") else name
            path = PurePosixPath(normalized_name)
            parts = normalized_name.split("/")
            folded = normalized_name.casefold()
            if (
                not normalized_name
                or "\\" in name
                or "\x00" in name
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise OfficeTabularFormatError(
                    "ooxml-member-path-unsafe",
                    f"OOXML archive contains an unsafe member path {name!r}",
                )
            if folded in seen:
                raise OfficeTabularFormatError(
                    "ooxml-member-duplicate",
                    f"OOXML archive contains a duplicate member path {name!r}",
                )
            seen.add(folded)
            if info.flag_bits & 0x1:
                raise OfficeTabularFormatError(
                    "ooxml-member-encrypted",
                    f"OOXML archive member is encrypted: {name!r}",
                )
            if info.create_system == 3:
                file_type = (info.external_attr >> 16) & 0o170000
                if file_type not in {0, 0o040000, 0o100000}:
                    raise OfficeTabularFormatError(
                        "ooxml-member-not-regular",
                        f"OOXML archive member is not a regular file: {name!r}",
                    )
            if info.is_dir():
                continue
            if info.file_size < 0 or info.file_size > limits.max_archive_member_bytes:
                raise OfficeTabularFormatError(
                    "ooxml-member-byte-limit",
                    f"OOXML member {name!r} exceeds the deterministic byte limit",
                )
            total += info.file_size
            if total > limits.max_archive_uncompressed_bytes:
                raise OfficeTabularFormatError(
                    "ooxml-total-byte-limit",
                    "OOXML archive exceeds the total uncompressed-byte limit",
                )
            if folded.endswith((".xml", ".rels")):
                payload = _read_archive_member(archive, name, limits)
                if _xml_has_forbidden_declaration(payload):
                    raise OfficeTabularFormatError(
                        "ooxml-xml-declaration-unsafe",
                        f"OOXML part {name!r} contains a DTD or entity declaration",
                    )
        return _ArchiveStats(len(infos), total)
    finally:
        archive.close()


def _open_archive(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise OfficeTabularFormatError(
            "ooxml-archive-invalid", f"OOXML ZIP archive could not be opened: {exc}"
        ) from exc


def _read_archive_member(
    archive: zipfile.ZipFile,
    name: str,
    limits: OfficeTabularExtractionLimits,
) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise OfficeTabularFormatError(
            "ooxml-required-part-missing", f"required OOXML part is missing: {name}"
        ) from exc
    if info.is_dir() or info.file_size > limits.max_archive_member_bytes:
        raise OfficeTabularFormatError(
            "ooxml-member-byte-limit", f"OOXML part cannot be read safely: {name}"
        )
    try:
        with archive.open(info, "r") as source:
            payload = source.read(limits.max_archive_member_bytes + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OfficeTabularFormatError(
            "ooxml-member-read-failed", f"OOXML part {name!r} could not be read: {exc}"
        ) from exc
    if len(payload) > limits.max_archive_member_bytes or len(payload) != info.file_size:
        raise OfficeTabularFormatError(
            "ooxml-member-size-mismatch",
            f"OOXML part {name!r} did not match its bounded archive size",
        )
    return payload


def _xml_has_forbidden_declaration(payload: bytes) -> bool:
    lowered = payload.lower()
    without_nuls = lowered.replace(b"\x00", b"")
    return any(
        marker in lowered or marker in without_nuls
        for marker in _XML_FORBIDDEN_MARKERS
    )


def _parse_xml(payload: bytes, *, part_name: str) -> ElementTree.Element:
    if _xml_has_forbidden_declaration(payload):
        raise OfficeTabularFormatError(
            "ooxml-xml-declaration-unsafe",
            f"OOXML part {part_name!r} contains a DTD or entity declaration",
        )
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise OfficeTabularFormatError(
            "ooxml-xml-invalid", f"OOXML part {part_name!r} is malformed: {exc}"
        ) from exc


def _block_truncation(reasons: set[str]) -> tuple[bool, str | None, str | None]:
    if not reasons:
        return False, None, None
    if len(reasons) == 1:
        reason_code = next(iter(reasons))
        explanations = {
            "cell-character-limit": "one or more table cells reached the character limit",
            "block-character-limit": "the located paragraph or slide reached the character limit",
        }
        return True, reason_code, explanations[reason_code]
    return (
        True,
        "multiple-content-limits",
        "multiple deterministic content limits were applied to the located block",
    )


def _table_blocks(
    rows: tuple[tuple[object, ...], ...],
    *,
    sheet: str,
    sheet_index: int,
    cell_truncated_rows: frozenset[int],
    limits: OfficeTabularExtractionLimits,
) -> tuple[Block, ...]:
    if not rows:
        return ()
    width = max((len(row) for row in rows), default=0)
    if width < 1:
        return ()
    padded = tuple(row + (None,) * (width - len(row)) for row in rows)
    blocks: list[Block] = []
    for offset in range(0, len(padded), limits.max_rows_per_block):
        group = padded[offset : offset + limits.max_rows_per_block]
        start_row = offset + 1
        end_row = offset + len(group)
        reasons: set[str] = set()
        if any(index in cell_truncated_rows for index in range(offset, end_row)):
            reasons.add("cell-character-limit")
        truncated, reason_code, reason = _block_truncation(reasons)
        blocks.append(
            Block(
                block_id=f"sheet-{sheet_index}-rows-{start_row}-{end_row}",
                block_type="table",
                text=_canonical_matrix(group),
                locator=TableRangeLocator(
                    sheet=sheet,
                    start_cell=f"A{start_row}",
                    end_cell=f"{_column_name(width)}{end_row}",
                ),
                metadata={
                    "matrix_format": TABLE_MATRIX_FORMAT,
                    "sheet": sheet,
                    "sheet_index": sheet_index,
                    "start_row": start_row,
                    "end_row": end_row,
                    "row_count": len(group),
                    "column_count": width,
                },
                truncated=truncated,
                truncation_reason_code=reason_code,
                truncation_reason=reason,
            )
        )
    return tuple(blocks)


def _parse_delimited_rows(
    data: bytes,
    *,
    format_value: str,
    limits: OfficeTabularExtractionLimits,
    truncate_cells: bool,
) -> _TableRows:
    text, encoding = _decode_delimited(data, limits)
    delimiter = "," if format_value == "csv" else "\t"
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    rows: list[tuple[object, ...]] = []
    truncated_rows: set[int] = set()
    partial_reasons: set[str] = set()
    retained_cells = 0
    try:
        for row_index, raw_row in enumerate(reader):
            if row_index >= limits.max_rows:
                partial_reasons.add("row-limit")
                break
            row: list[object] = list(raw_row) if raw_row else [None]
            if len(row) > limits.max_columns:
                row = row[: limits.max_columns]
                partial_reasons.add("column-limit")
            remaining_cells = limits.max_cells - retained_cells
            if remaining_cells <= 0:
                partial_reasons.add("cell-limit")
                break
            stop_after_row = False
            if len(row) > remaining_cells:
                row = row[:remaining_cells]
                partial_reasons.add("cell-limit")
                stop_after_row = True
            normalized: list[object] = []
            for value in row:
                if isinstance(value, str) and len(value) > limits.max_cell_characters:
                    if not truncate_cells:
                        raise OfficeTabularFormatError(
                            "cell-character-limit",
                            "requested delimited table range contains an over-limit cell",
                        )
                    value = value[: limits.max_cell_characters]
                    truncated_rows.add(row_index)
                    partial_reasons.add("cell-character-limit")
                normalized.append(value)
            rows.append(tuple(normalized))
            retained_cells += len(normalized)
            if stop_after_row:
                break
    except csv.Error as exc:
        raise OfficeTabularFormatError(
            "delimited-parse-failed", f"delimited source could not be parsed strictly: {exc}"
        ) from exc
    return _TableRows(
        rows=tuple(rows),
        cell_truncated_rows=frozenset(truncated_rows),
        partial_reasons=tuple(sorted(partial_reasons)),
        encoding=encoding,
        metadata={
            "delimiter": delimiter,
            "retained_rows": len(rows),
            "retained_cells": retained_cells,
        },
    )


def _extract_delimited(
    data: bytes,
    *,
    format_value: str,
    limits: OfficeTabularExtractionLimits,
) -> _BuildResult:
    table = _parse_delimited_rows(
        data,
        format_value=format_value,
        limits=limits,
        truncate_cells=True,
    )
    sheet = DELIMITED_SHEETS[format_value]
    blocks = _table_blocks(
        table.rows,
        sheet=sheet,
        sheet_index=1,
        cell_truncated_rows=table.cell_truncated_rows,
        limits=limits,
    )
    metadata = {
        **table.metadata,
        "sheet_count": 1,
        "sheet_names": [sheet],
        "table_matrix_format": TABLE_MATRIX_FORMAT,
    }
    return _BuildResult(
        blocks=blocks,
        encoding=table.encoding,
        metadata=metadata,
        partial_reasons=table.partial_reasons,
    )



def _normalize_xlsx_cell(
    value: object,
    *,
    limits: OfficeTabularExtractionLimits,
    truncate: bool,
) -> tuple[object, bool]:
    # Validate the openpyxl value now, but retain the original typed value until
    # the matrix is serialized. This keeps tagged date/time values canonical and
    # avoids normalizing the same cell twice.
    _canonical_json_value(value)
    if isinstance(value, str) and len(value) > limits.max_cell_characters:
        if not truncate:
            raise OfficeTabularFormatError(
                "cell-character-limit",
                "requested spreadsheet range contains an over-limit cell",
            )
        return value[: limits.max_cell_characters], True
    return value, False


def _load_xlsx_workbook(data: bytes):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise OfficeTabularFormatError(
            "xlsx-runtime-unavailable",
            "openpyxl is required for deterministic XLSX extraction",
        ) from exc
    try:
        return load_workbook(
            io.BytesIO(data),
            read_only=True,
            data_only=False,
            keep_links=False,
        )
    except Exception as exc:
        raise OfficeTabularFormatError(
            "xlsx-parse-failed", f"XLSX workbook could not be opened safely: {exc}"
        ) from exc


def _trim_empty_matrix(rows: list[list[object]]) -> tuple[tuple[object, ...], ...]:
    while rows and all(value is None for value in rows[-1]):
        rows.pop()
    if not rows:
        return ()
    width = 0
    for row in rows:
        for column_index, value in enumerate(row, start=1):
            if value is not None:
                width = max(width, column_index)
    if width == 0:
        return ()
    return tuple(tuple(row[:width]) for row in rows)


def _extract_xlsx(
    data: bytes,
    *,
    limits: OfficeTabularExtractionLimits,
) -> _BuildResult:
    archive_stats = _validate_archive(data, limits)
    workbook = _load_xlsx_workbook(data)
    blocks: list[Block] = []
    partial_reasons: set[str] = set()
    sheet_names = list(workbook.sheetnames)
    retained_rows = 0
    retained_cells = 0
    scanned_rows = 0
    scanned_cells = 0
    try:
        selected_names = sheet_names[: limits.max_sheets]
        if len(sheet_names) > len(selected_names):
            partial_reasons.add("sheet-limit")
        for sheet_index, sheet_name in enumerate(selected_names, start=1):
            if scanned_rows >= limits.max_rows or scanned_cells >= limits.max_cells:
                if scanned_rows >= limits.max_rows:
                    partial_reasons.add("row-limit")
                if scanned_cells >= limits.max_cells:
                    partial_reasons.add("cell-limit")
                break
            worksheet = workbook[sheet_name]
            max_row = int(worksheet.max_row or 0)
            max_column = int(worksheet.max_column or 0)
            if max_row < 1 or max_column < 1:
                continue
            row_budget = limits.max_rows - scanned_rows
            cell_budget = limits.max_cells - scanned_cells
            configured_columns = min(max_column, limits.max_columns)
            selected_columns = min(configured_columns, cell_budget)
            if max_column > configured_columns:
                partial_reasons.add("column-limit")
            if configured_columns > selected_columns:
                partial_reasons.add("cell-limit")
            if selected_columns < 1:
                partial_reasons.add("cell-limit")
                break
            rows_by_cells = cell_budget // selected_columns
            selected_rows = min(max_row, row_budget, rows_by_cells)
            if selected_rows < max_row:
                if row_budget <= min(max_row, rows_by_cells):
                    partial_reasons.add("row-limit")
                if rows_by_cells <= min(max_row, row_budget):
                    partial_reasons.add("cell-limit")
            if selected_rows < 1:
                partial_reasons.add("cell-limit")
                break
            matrix: list[list[object]] = []
            truncated_rows: set[int] = set()
            try:
                iterator = worksheet.iter_rows(
                    min_row=1,
                    max_row=selected_rows,
                    min_col=1,
                    max_col=selected_columns,
                )
                for row_index, cells in enumerate(iterator):
                    row_values: list[object] = []
                    for cell in cells:
                        value, was_truncated = _normalize_xlsx_cell(
                            cell.value,
                            limits=limits,
                            truncate=True,
                        )
                        row_values.append(value)
                        if was_truncated:
                            truncated_rows.add(row_index)
                            partial_reasons.add("cell-character-limit")
                    matrix.append(row_values)
            except OfficeTabularExtractionError:
                raise
            except Exception as exc:
                raise OfficeTabularFormatError(
                    "xlsx-cell-read-failed",
                    f"worksheet {sheet_name!r} could not be read safely: {exc}",
                ) from exc
            scanned_rows += selected_rows
            scanned_cells += selected_rows * selected_columns
            retained = _trim_empty_matrix(matrix)
            if retained:
                blocks.extend(
                    _table_blocks(
                        retained,
                        sheet=sheet_name,
                        sheet_index=sheet_index,
                        cell_truncated_rows=frozenset(truncated_rows),
                        limits=limits,
                    )
                )
                retained_rows += len(retained)
                retained_cells += len(retained) * len(retained[0])
            if selected_rows < max_row:
                break
    finally:
        workbook.close()
    return _BuildResult(
        blocks=tuple(blocks),
        encoding=None,
        metadata={
            "archive_entry_count": archive_stats.entry_count,
            "archive_uncompressed_bytes": archive_stats.uncompressed_bytes,
            "sheet_count": len(sheet_names),
            "sheet_names": sheet_names,
            "scanned_rows": scanned_rows,
            "scanned_cells": scanned_cells,
            "retained_rows": retained_rows,
            "retained_cells": retained_cells,
            "table_matrix_format": TABLE_MATRIX_FORMAT,
            "formula_mode": "formula-preserving",
        },
        partial_reasons=tuple(sorted(partial_reasons)),
    )


def _docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    text_tag = f"{{{_W_NS}}}t"
    tab_tag = f"{{{_W_NS}}}tab"
    break_tags = {f"{{{_W_NS}}}br", f"{{{_W_NS}}}cr"}
    for node in paragraph.iter():
        if node.tag == text_tag:
            parts.append(node.text or "")
        elif node.tag == tab_tag:
            parts.append("\t")
        elif node.tag in break_tags:
            parts.append("\n")
    return "".join(parts)


def _docx_paragraphs(
    data: bytes,
    *,
    limits: OfficeTabularExtractionLimits,
) -> tuple[list[ElementTree.Element], _ArchiveStats]:
    archive_stats = _validate_archive(data, limits)
    archive = _open_archive(data)
    try:
        document_xml = _read_archive_member(
            archive, "word/document.xml", limits
        )
    finally:
        archive.close()
    root = _parse_xml(document_xml, part_name="word/document.xml")
    body = root.find(f"{{{_W_NS}}}body")
    if body is None:
        raise OfficeTabularFormatError(
            "docx-body-missing", "DOCX main document does not contain a w:body"
        )
    return list(body.iter(f"{{{_W_NS}}}p")), archive_stats


def _docx_style(paragraph: ElementTree.Element) -> str | None:
    style = paragraph.find(
        f"{{{_W_NS}}}pPr/{{{_W_NS}}}pStyle"
    )
    if style is None:
        return None
    value = style.attrib.get(f"{{{_W_NS}}}val")
    return value if value else None


def _extract_docx(
    data: bytes,
    *,
    limits: OfficeTabularExtractionLimits,
) -> _BuildResult:
    paragraphs, archive_stats = _docx_paragraphs(data, limits=limits)
    selected = paragraphs[: limits.max_paragraphs]
    partial_reasons: set[str] = set()
    if len(paragraphs) > len(selected):
        partial_reasons.add("paragraph-limit")
    blocks: list[Block] = []
    for paragraph_index, paragraph in enumerate(selected):
        text = _docx_paragraph_text(paragraph)
        truncated = False
        if len(text) > limits.max_block_characters:
            text = text[: limits.max_block_characters]
            truncated = True
            partial_reasons.add("block-character-limit")
        style = _docx_style(paragraph)
        metadata: dict[str, Any] = {"paragraph_index": paragraph_index}
        if style is not None:
            metadata["style"] = style
        blocks.append(
            Block(
                block_id=f"paragraph-{paragraph_index}",
                block_type="text",
                text=text,
                locator=ParagraphLocator(paragraph_index),
                metadata=metadata,
                truncated=truncated,
                truncation_reason_code=(
                    "block-character-limit" if truncated else None
                ),
                truncation_reason=(
                    "the DOCX paragraph reached the deterministic character limit"
                    if truncated
                    else None
                ),
            )
        )
    return _BuildResult(
        blocks=tuple(blocks),
        encoding=None,
        metadata={
            "archive_entry_count": archive_stats.entry_count,
            "archive_uncompressed_bytes": archive_stats.uncompressed_bytes,
            "paragraph_count": len(paragraphs),
            "retained_paragraphs": len(selected),
            "paragraph_coordinate_space": "word-document-body-w-p-order",
        },
        partial_reasons=tuple(sorted(partial_reasons)),
    )



def _resolve_ooxml_target(base_part: str, target: str) -> str:
    split = urlsplit(target)
    if (
        not target
        or "\\" in target
        or split.scheme
        or split.netloc
        or split.query
        or split.fragment
        or target.startswith("/")
    ):
        raise OfficeTabularFormatError(
            "pptx-slide-target-unsafe",
            f"PPTX slide relationship target is unsafe: {target!r}",
        )
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))
    if resolved.startswith("../") or resolved in {".", ".."}:
        raise OfficeTabularFormatError(
            "pptx-slide-target-unsafe",
            f"PPTX slide relationship escapes the package root: {target!r}",
        )
    return resolved


def _pptx_slide_parts(
    data: bytes,
    *,
    limits: OfficeTabularExtractionLimits,
) -> tuple[list[tuple[str, str]], _ArchiveStats, dict[str, bytes]]:
    archive_stats = _validate_archive(data, limits)
    archive = _open_archive(data)
    try:
        presentation_name = "ppt/presentation.xml"
        relationships_name = "ppt/_rels/presentation.xml.rels"
        presentation_xml = _read_archive_member(
            archive, presentation_name, limits
        )
        relationships_xml = _read_archive_member(
            archive, relationships_name, limits
        )
        presentation = _parse_xml(
            presentation_xml, part_name=presentation_name
        )
        relationships = _parse_xml(
            relationships_xml, part_name=relationships_name
        )
        relationship_map: dict[str, tuple[str, str | None, str | None]] = {}
        for relationship in relationships.findall(f"{{{_REL_NS}}}Relationship"):
            relationship_id = relationship.attrib.get("Id")
            target = relationship.attrib.get("Target")
            if not relationship_id or target is None:
                raise OfficeTabularFormatError(
                    "pptx-relationship-invalid",
                    "PPTX presentation relationship is missing Id or Target",
                )
            if relationship_id in relationship_map:
                raise OfficeTabularFormatError(
                    "pptx-relationship-duplicate",
                    f"PPTX relationship ID is duplicated: {relationship_id!r}",
                )
            relationship_map[relationship_id] = (
                target,
                relationship.attrib.get("TargetMode"),
                relationship.attrib.get("Type"),
            )
        slide_ids = presentation.findall(
            f".//{{{_P_NS}}}sldIdLst/{{{_P_NS}}}sldId"
        )
        slide_parts: list[tuple[str, str]] = []
        payloads: dict[str, bytes] = {}
        for slide_id in slide_ids:
            relationship_id = slide_id.attrib.get(f"{{{_R_NS}}}id")
            if not relationship_id or relationship_id not in relationship_map:
                raise OfficeTabularFormatError(
                    "pptx-slide-relationship-missing",
                    "PPTX slide order references a missing relationship",
                )
            target, target_mode, relationship_type = relationship_map[relationship_id]
            if target_mode not in {None, "", "Internal"}:
                raise OfficeTabularFormatError(
                    "pptx-slide-target-external",
                    "PPTX slide relationship uses an external target",
                )
            if relationship_type != _SLIDE_REL_TYPE:
                raise OfficeTabularFormatError(
                    "pptx-slide-relationship-invalid",
                    "PPTX slide order relationship is not an internal slide part",
                )
            part_name = _resolve_ooxml_target(presentation_name, target)
            if any(existing == part_name for _rid, existing in slide_parts):
                raise OfficeTabularFormatError(
                    "pptx-slide-part-duplicate",
                    f"PPTX slide part is referenced more than once: {part_name!r}",
                )
            payloads[part_name] = _read_archive_member(archive, part_name, limits)
            slide_parts.append((relationship_id, part_name))
        return slide_parts, archive_stats, payloads
    finally:
        archive.close()


def _drawing_paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    text_tag = f"{{{_A_NS}}}t"
    break_tag = f"{{{_A_NS}}}br"
    tab_tag = f"{{{_A_NS}}}tab"
    for node in paragraph.iter():
        if node.tag == text_tag:
            parts.append(node.text or "")
        elif node.tag == break_tag:
            parts.append("\n")
        elif node.tag == tab_tag:
            parts.append("\t")
    return "".join(parts)


def _pptx_slide_text(payload: bytes, *, part_name: str) -> str:
    root = _parse_xml(payload, part_name=part_name)
    paragraphs = list(root.iter(f"{{{_A_NS}}}p"))
    return "\n".join(_drawing_paragraph_text(paragraph) for paragraph in paragraphs)


def _extract_pptx(
    data: bytes,
    *,
    limits: OfficeTabularExtractionLimits,
) -> _BuildResult:
    slide_parts, archive_stats, payloads = _pptx_slide_parts(data, limits=limits)
    selected = slide_parts[: limits.max_slides]
    partial_reasons: set[str] = set()
    if len(slide_parts) > len(selected):
        partial_reasons.add("slide-limit")
    blocks: list[Block] = []
    for slide_number, (relationship_id, part_name) in enumerate(selected, start=1):
        text = _pptx_slide_text(payloads[part_name], part_name=part_name)
        truncated = False
        if len(text) > limits.max_block_characters:
            text = text[: limits.max_block_characters]
            truncated = True
            partial_reasons.add("block-character-limit")
        blocks.append(
            Block(
                block_id=f"slide-{slide_number}",
                block_type="text",
                text=text,
                locator=SlideLocator(slide_number),
                metadata={
                    "slide_number": slide_number,
                    "relationship_id": relationship_id,
                    "part_name": part_name,
                },
                truncated=truncated,
                truncation_reason_code=(
                    "block-character-limit" if truncated else None
                ),
                truncation_reason=(
                    "the PPTX slide reached the deterministic character limit"
                    if truncated
                    else None
                ),
            )
        )
    return _BuildResult(
        blocks=tuple(blocks),
        encoding=None,
        metadata={
            "archive_entry_count": archive_stats.entry_count,
            "archive_uncompressed_bytes": archive_stats.uncompressed_bytes,
            "slide_count": len(slide_parts),
            "retained_slides": len(selected),
            "slide_coordinate_space": "presentation-relationship-order",
        },
        partial_reasons=tuple(sorted(partial_reasons)),
    )


def _build_document(
    data: bytes,
    *,
    relative_path: str,
    format_value: str,
    content_sha256: str,
    source_size_bytes: int,
    limits: OfficeTabularExtractionLimits,
) -> ExtractionResult:
    try:
        if format_value in DELIMITED_SHEETS:
            build = _extract_delimited(
                data, format_value=format_value, limits=limits
            )
        elif format_value == "xlsx":
            build = _extract_xlsx(data, limits=limits)
        elif format_value == "docx":
            build = _extract_docx(data, limits=limits)
        else:
            build = _extract_pptx(data, limits=limits)
    except OfficeTabularExtractionError as exc:
        return _failed_result(exc.reason_code, exc.message)
    except Exception as exc:
        return _failed_result(
            "office-tabular-unexpected-failure",
            "Office/tabular extraction failed closed",
            f"unexpected exception type: {type(exc).__name__}",
        )
    metadata = {
        "source_size_bytes": source_size_bytes,
        "partial_reasons": list(build.partial_reasons),
        **build.metadata,
    }
    document = ExtractedDocument(
        path=relative_path,
        content_sha256=content_sha256,
        format=format_value,
        extractor=OFFICE_TABULAR_EXTRACTOR_NAME,
        extractor_version=OFFICE_TABULAR_EXTRACTOR_VERSION,
        encoding=build.encoding,
        blocks=build.blocks,
        metadata=metadata,
    )
    partial = bool(build.partial_reasons)
    return ExtractionResult(
        status="partial" if partial else "processed",
        reason_code=(
            "deterministic-office-tabular-partial"
            if partial
            else "deterministic-office-tabular-extracted"
        ),
        reason=(
            "Office/tabular content was extracted with explicit deterministic limits"
            if partial
            else "all supported Office/tabular content was deterministically extracted"
        ),
        document=document,
        diagnostics=tuple(
            f"deterministic limit applied: {reason}"
            for reason in build.partial_reasons
        ),
    )


def _extract_verified_bytes(
    data: bytes,
    *,
    relative_path: str,
    format_value: str,
    content_sha256: str,
    source_size_bytes: int,
    limits: OfficeTabularExtractionLimits,
) -> ExtractionResult:
    if source_size_bytes > limits.max_source_bytes:
        return _failed_result(
            "office-tabular-source-byte-limit",
            "the source exceeds the deterministic Office/tabular byte limit",
            f"source has {source_size_bytes} bytes; limit is {limits.max_source_bytes}",
            f"content SHA-256 is {content_sha256}",
        )
    return _build_document(
        data,
        relative_path=relative_path,
        format_value=format_value,
        content_sha256=content_sha256,
        source_size_bytes=source_size_bytes,
        limits=limits,
    )


def extract_office_tabular_bytes(
    data: bytes,
    *,
    relative_path: str,
    format_value: str,
    limits: OfficeTabularExtractionLimits | None = None,
) -> ExtractionResult:
    """Extract one immutable in-memory Office/tabular source snapshot."""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if (
        not isinstance(format_value, str)
        or format_value not in SUPPORTED_OFFICE_TABULAR_FORMATS
    ):
        return _unsupported_result(format_value)
    chosen_limits = (
        OfficeTabularExtractionLimits() if limits is None else limits
    )
    if not isinstance(chosen_limits, OfficeTabularExtractionLimits):
        raise TypeError("limits must be OfficeTabularExtractionLimits")
    return _extract_verified_bytes(
        data,
        relative_path=relative_path,
        format_value=format_value,
        content_sha256=hashlib.sha256(data).hexdigest(),
        source_size_bytes=len(data),
        limits=chosen_limits,
    )


def extract_office_tabular_file(
    source_path: str | Path,
    *,
    relative_path: str,
    format_value: str,
    expected_sha256: str | None = None,
    limits: OfficeTabularExtractionLimits | None = None,
) -> ExtractionResult:
    """Read and hash one source without writing it, then apply C-06 extraction."""

    if (
        not isinstance(format_value, str)
        or format_value not in SUPPORTED_OFFICE_TABULAR_FORMATS
    ):
        return _unsupported_result(format_value)
    _validate_expected_sha256(expected_sha256)
    chosen_limits = (
        OfficeTabularExtractionLimits() if limits is None else limits
    )
    if not isinstance(chosen_limits, OfficeTabularExtractionLimits):
        raise TypeError("limits must be OfficeTabularExtractionLimits")
    try:
        source = _read_and_hash(Path(source_path), chosen_limits)
    except OSError as exc:
        return _failed_result(
            "source-read-failed", "the Office/tabular source file could not be read", str(exc)
        )
    if expected_sha256 is not None and source.content_sha256 != expected_sha256:
        return _failed_result(
            "content-hash-mismatch",
            "the Office/tabular content no longer matches the requested file version",
            f"expected {expected_sha256}; observed {source.content_sha256}",
        )
    return _extract_verified_bytes(
        source.data,
        relative_path=relative_path,
        format_value=format_value,
        content_sha256=source.content_sha256,
        source_size_bytes=source.source_size_bytes,
        limits=chosen_limits,
    )


def _reopen_limits_error(reason_code: str, message: str) -> OfficeTabularFormatError:
    return OfficeTabularFormatError(reason_code, message)


def _requested_table_bounds(
    locator: TableRangeLocator,
    limits: OfficeTabularExtractionLimits,
) -> tuple[int, int, int, int]:
    start_row, start_column, end_row, end_column = _locator_bounds(locator)
    row_count = end_row - start_row + 1
    column_count = end_column - start_column + 1
    cell_count = row_count * column_count
    if row_count > limits.max_rows:
        raise OfficeTabularLocatorError(
            "table-range-row-limit",
            "requested table range exceeds the deterministic row limit",
        )
    if column_count > limits.max_columns:
        raise OfficeTabularLocatorError(
            "table-range-column-limit",
            "requested table range exceeds the deterministic column limit",
        )
    if cell_count > limits.max_cells:
        raise OfficeTabularLocatorError(
            "table-range-cell-limit",
            "requested table range exceeds the deterministic cell limit",
        )
    return start_row, start_column, end_row, end_column


def _reopen_delimited_range(
    data: bytes,
    *,
    format_value: str,
    locator: TableRangeLocator,
    limits: OfficeTabularExtractionLimits,
) -> str:
    expected_sheet = DELIMITED_SHEETS[format_value]
    if locator.sheet != expected_sheet:
        raise OfficeTabularLocatorError(
            "delimited-sheet-missing",
            f"{format_value.upper()} logical sheet does not exist: {locator.sheet!r}",
        )
    start_row, start_column, end_row, end_column = _requested_table_bounds(
        locator, limits
    )
    table = _parse_delimited_rows(
        data,
        format_value=format_value,
        limits=limits,
        truncate_cells=False,
    )
    if table.partial_reasons:
        reasons = ", ".join(table.partial_reasons)
        raise _reopen_limits_error(
            "delimited-source-limit",
            f"delimited source exceeds deterministic reopening limits: {reasons}",
        )
    row_count = len(table.rows)
    width = max((len(row) for row in table.rows), default=0)
    if end_row > row_count or end_column > width:
        raise OfficeTabularLocatorError(
            "delimited-range-out-of-bounds",
            "requested table range is outside the delimited source coordinate space",
        )
    matrix: list[list[object]] = []
    for row in table.rows[start_row - 1 : end_row]:
        matrix.append(
            [
                row[column_index - 1] if column_index <= len(row) else None
                for column_index in range(start_column, end_column + 1)
            ]
        )
    return _canonical_matrix(matrix)


def _xlsx_source_dimensions(
    workbook: object,
    *,
    limits: OfficeTabularExtractionLimits,
) -> dict[str, tuple[int, int]]:
    sheet_names = list(workbook.sheetnames)  # type: ignore[attr-defined]
    if len(sheet_names) > limits.max_sheets:
        raise _reopen_limits_error(
            "xlsx-sheet-limit",
            "spreadsheet exceeds the deterministic sheet limit",
        )
    dimensions: dict[str, tuple[int, int]] = {}
    total_rows = 0
    total_cells = 0
    for sheet_name in sheet_names:
        worksheet = workbook[sheet_name]  # type: ignore[index]
        max_row = int(worksheet.max_row or 0)
        max_column = int(worksheet.max_column or 0)
        if max_column > limits.max_columns:
            raise _reopen_limits_error(
                "xlsx-column-limit",
                f"worksheet {sheet_name!r} exceeds the deterministic column limit",
            )
        total_rows += max_row
        total_cells += max_row * max_column
        if total_rows > limits.max_rows:
            raise _reopen_limits_error(
                "xlsx-row-limit",
                "spreadsheet exceeds the deterministic global row limit",
            )
        if total_cells > limits.max_cells:
            raise _reopen_limits_error(
                "xlsx-cell-limit",
                "spreadsheet exceeds the deterministic global cell limit",
            )
        dimensions[sheet_name] = (max_row, max_column)
    return dimensions


def _reopen_xlsx_range(
    data: bytes,
    *,
    locator: TableRangeLocator,
    limits: OfficeTabularExtractionLimits,
) -> str:
    _validate_archive(data, limits)
    start_row, start_column, end_row, end_column = _requested_table_bounds(
        locator, limits
    )
    workbook = _load_xlsx_workbook(data)
    try:
        dimensions = _xlsx_source_dimensions(workbook, limits=limits)
        if locator.sheet not in dimensions:
            raise OfficeTabularLocatorError(
                "xlsx-sheet-missing",
                f"spreadsheet sheet does not exist: {locator.sheet!r}",
            )
        max_row, max_column = dimensions[locator.sheet]
        if end_row > max_row or end_column > max_column:
            raise OfficeTabularLocatorError(
                "xlsx-range-out-of-bounds",
                "requested table range is outside the worksheet coordinate space",
            )
        worksheet = workbook[locator.sheet]
        matrix: list[list[object]] = []
        try:
            for cells in worksheet.iter_rows(
                min_row=start_row,
                max_row=end_row,
                min_col=start_column,
                max_col=end_column,
            ):
                row_values: list[object] = []
                for cell in cells:
                    value, _truncated = _normalize_xlsx_cell(
                        cell.value,
                        limits=limits,
                        truncate=False,
                    )
                    row_values.append(value)
                matrix.append(row_values)
        except OfficeTabularExtractionError:
            raise
        except Exception as exc:
            raise OfficeTabularFormatError(
                "xlsx-cell-read-failed",
                f"worksheet {locator.sheet!r} could not be reopened safely: {exc}",
            ) from exc
        return _canonical_matrix(matrix)
    finally:
        workbook.close()


def _reopen_docx_paragraph(
    data: bytes,
    *,
    locator: ParagraphLocator,
    limits: OfficeTabularExtractionLimits,
) -> str:
    paragraphs, _archive_stats = _docx_paragraphs(data, limits=limits)
    if len(paragraphs) > limits.max_paragraphs:
        raise _reopen_limits_error(
            "docx-paragraph-limit",
            "DOCX source exceeds the deterministic paragraph limit",
        )
    if locator.paragraph_index >= len(paragraphs):
        raise OfficeTabularLocatorError(
            "docx-paragraph-out-of-bounds",
            f"DOCX paragraph {locator.paragraph_index} does not exist",
        )
    text = _docx_paragraph_text(paragraphs[locator.paragraph_index])
    if len(text) > limits.max_block_characters:
        raise _reopen_limits_error(
            "block-character-limit",
            "requested DOCX paragraph exceeds the deterministic character limit",
        )
    return text


def _reopen_pptx_slide(
    data: bytes,
    *,
    locator: SlideLocator,
    limits: OfficeTabularExtractionLimits,
) -> str:
    slide_parts, _archive_stats, payloads = _pptx_slide_parts(data, limits=limits)
    if len(slide_parts) > limits.max_slides:
        raise _reopen_limits_error(
            "pptx-slide-limit",
            "PPTX source exceeds the deterministic slide limit",
        )
    if locator.slide_number > len(slide_parts):
        raise OfficeTabularLocatorError(
            "pptx-slide-out-of-bounds",
            f"PPTX slide {locator.slide_number} does not exist",
        )
    _relationship_id, part_name = slide_parts[locator.slide_number - 1]
    text = _pptx_slide_text(payloads[part_name], part_name=part_name)
    if len(text) > limits.max_block_characters:
        raise _reopen_limits_error(
            "block-character-limit",
            "requested PPTX slide exceeds the deterministic character limit",
        )
    return text


def reopen_office_locator_bytes(
    data: bytes,
    *,
    relative_path: str,
    format_value: str,
    locator: Locator,
    limits: OfficeTabularExtractionLimits | None = None,
) -> str:
    """Reopen one exact C-06 locator from the current immutable source bytes."""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("relative_path must be a non-empty string")
    if (
        not isinstance(format_value, str)
        or format_value not in SUPPORTED_OFFICE_TABULAR_FORMATS
    ):
        raise OfficeTabularFormatError(
            "office-tabular-format-unsupported",
            f"unsupported Office/tabular format {format_value!r}",
        )
    chosen_limits = (
        OfficeTabularExtractionLimits() if limits is None else limits
    )
    if not isinstance(chosen_limits, OfficeTabularExtractionLimits):
        raise TypeError("limits must be OfficeTabularExtractionLimits")
    if len(data) > chosen_limits.max_source_bytes:
        raise OfficeTabularFormatError(
            "office-tabular-source-byte-limit",
            "source exceeds the deterministic Office/tabular byte limit",
        )

    if format_value in DELIMITED_SHEETS:
        if not isinstance(locator, TableRangeLocator):
            raise OfficeTabularLocatorError(
                "office-tabular-locator-type-mismatch",
                f"{format_value.upper()} requires a table_range locator",
            )
        return _reopen_delimited_range(
            data,
            format_value=format_value,
            locator=locator,
            limits=chosen_limits,
        )
    if format_value == "xlsx":
        if not isinstance(locator, TableRangeLocator):
            raise OfficeTabularLocatorError(
                "office-tabular-locator-type-mismatch",
                "XLSX requires a table_range locator",
            )
        return _reopen_xlsx_range(data, locator=locator, limits=chosen_limits)
    if format_value == "docx":
        if not isinstance(locator, ParagraphLocator):
            raise OfficeTabularLocatorError(
                "office-tabular-locator-type-mismatch",
                "DOCX requires a paragraph locator",
            )
        return _reopen_docx_paragraph(data, locator=locator, limits=chosen_limits)
    if not isinstance(locator, SlideLocator):
        raise OfficeTabularLocatorError(
            "office-tabular-locator-type-mismatch",
            "PPTX requires a slide locator",
        )
    return _reopen_pptx_slide(data, locator=locator, limits=chosen_limits)
