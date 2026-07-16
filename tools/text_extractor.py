#!/usr/bin/env python3
"""Deterministic C-02 extraction for text-like research files."""

from __future__ import annotations

import codecs
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

if __package__:
    from .extraction_schema import (
        Block,
        ExtractedDocument,
        ExtractionResult,
        LineRangeLocator,
    )
else:
    from extraction_schema import (  # type: ignore[no-redef]
        Block,
        ExtractedDocument,
        ExtractionResult,
        LineRangeLocator,
    )


TEXT_EXTRACTOR_NAME = "deterministic-text"
TEXT_EXTRACTOR_VERSION = "1"

SOURCE_CODE_FORMATS = frozenset(
    {
        "batch",
        "c",
        "cpp",
        "go",
        "java",
        "javascript",
        "julia",
        "matlab",
        "perl",
        "powershell",
        "python",
        "r",
        "ruby",
        "rust",
        "shell",
        "sql",
        "typescript",
    }
)
MARKDOWN_FORMATS = frozenset({"markdown", "restructured_text"})
TEXT_FORMATS = frozenset(
    {
        "bibtex",
        "css",
        "csv",
        "html",
        "ini",
        "json",
        "jsonl",
        "latex",
        "log",
        "plain_text",
        "svg",
        "toml",
        "tsv",
        "xml",
        "yaml",
    }
)
SUPPORTED_TEXT_FORMATS = SOURCE_CODE_FORMATS | MARKDOWN_FORMATS | TEXT_FORMATS

_ALLOWED_ENCODING_HINTS = {
    "utf-8",
    "utf-8-sig",
    "utf-16-le",
    "utf-16-be",
    "gb18030",
    "cp1252",
    "latin-1",
}
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig", "utf-8-sig", 0),
    (codecs.BOM_UTF16_LE, "utf-16-le", "utf-16-le", 2),
    (codecs.BOM_UTF16_BE, "utf-16-be", "utf-16-be", 2),
)
_CANDIDATE_ENCODINGS = (
    ("utf-8", "utf-8", 0),
    ("gb18030", "gb18030", 0),
    ("cp1252", "cp1252", 0),
    ("latin-1", "latin-1", 0),
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_INCOMPLETE_DECODE_MARKERS = ("incomplete", "truncated", "unexpected end")


@dataclass(frozen=True)
class TextExtractionLimits:
    """Deterministic safety bounds for one text-like source."""

    max_source_bytes: int = 8 * 1024 * 1024
    max_line_characters: int = 64 * 1024
    max_block_lines: int = 200
    binary_sample_bytes: int = 16 * 1024
    read_chunk_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        for field_name in (
            "max_source_bytes",
            "max_line_characters",
            "max_block_lines",
            "binary_sample_bytes",
            "read_chunk_bytes",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.max_source_bytes < 4:
            raise ValueError("max_source_bytes must be at least 4")


@dataclass(frozen=True)
class _ReadResult:
    prefix: bytes
    content_sha256: str
    source_size_bytes: int
    source_truncated: bool


@dataclass(frozen=True)
class _DecodedText:
    text: str
    encoding: str
    omitted_tail_bytes: int


@dataclass(frozen=True)
class _RenderedLine:
    text: str
    reason_codes: tuple[str, ...]


def _block_type(format_value: str) -> str:
    if format_value in SOURCE_CODE_FORMATS:
        return "code"
    if format_value in MARKDOWN_FORMATS:
        return "markdown"
    return "text"


def _read_prefix_and_hash(path: Path, limits: TextExtractionLimits) -> _ReadResult:
    digest = hashlib.sha256()
    prefix = bytearray()
    source_size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(limits.read_chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
            source_size += len(chunk)
            remaining = limits.max_source_bytes - len(prefix)
            if remaining > 0:
                prefix.extend(chunk[:remaining])
    return _ReadResult(
        prefix=bytes(prefix),
        content_sha256=digest.hexdigest(),
        source_size_bytes=source_size,
        source_truncated=source_size > len(prefix),
    )


def _looks_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return False
    if b"\x00" in sample:
        return True
    disallowed_controls = sum(
        1
        for byte in sample
        if byte < 32 and byte not in {8, 9, 10, 12, 13, 27}
    )
    return disallowed_controls / len(sample) > 0.02


def _decode_candidate(
    data: bytes,
    *,
    codec_name: str,
    bom_bytes: int,
    allow_incomplete_tail: bool,
) -> tuple[str, int]:
    payload = data[bom_bytes:]
    first_error: UnicodeDecodeError | None = None
    try:
        return payload.decode(codec_name, errors="strict"), 0
    except UnicodeDecodeError as exc:
        first_error = exc
        reason = exc.reason.lower()
        incomplete_tail = (
            exc.end == len(payload)
            and any(marker in reason for marker in _INCOMPLETE_DECODE_MARKERS)
        )
        if not allow_incomplete_tail or not incomplete_tail:
            raise

    assert first_error is not None
    last_error = first_error
    for trim in range(1, min(4, len(payload)) + 1):
        candidate = payload[:-trim]
        try:
            return candidate.decode(codec_name, errors="strict"), trim
        except UnicodeDecodeError as exc:
            last_error = exc
    raise last_error


def _encoding_candidates(
    data: bytes,
    encoding_hint: str | None,
) -> Iterable[tuple[str, str, int]]:
    if encoding_hint is not None:
        if encoding_hint not in _ALLOWED_ENCODING_HINTS:
            raise ValueError(f"unsupported encoding_hint {encoding_hint!r}")
        bom_bytes = 0
        codec_name = encoding_hint
        if encoding_hint == "utf-16-le" and data.startswith(codecs.BOM_UTF16_LE):
            bom_bytes = 2
        elif encoding_hint == "utf-16-be" and data.startswith(codecs.BOM_UTF16_BE):
            bom_bytes = 2
        yield encoding_hint, codec_name, bom_bytes
        return
    for bom, label, codec_name, bom_bytes in _BOMS:
        if data.startswith(bom):
            yield label, codec_name, bom_bytes
            return
    yield from _CANDIDATE_ENCODINGS


def _decode_text(
    data: bytes,
    *,
    source_truncated: bool,
    encoding_hint: str | None,
) -> _DecodedText:
    failures: list[str] = []
    for label, codec_name, bom_bytes in _encoding_candidates(data, encoding_hint):
        try:
            text, omitted = _decode_candidate(
                data,
                codec_name=codec_name,
                bom_bytes=bom_bytes,
                allow_incomplete_tail=source_truncated,
            )
        except UnicodeDecodeError as exc:
            failures.append(f"{label}: byte {exc.start}")
            continue
        if (
            encoding_hint is None
            and label == "gb18030"
            and omitted
            and not any(ord(character) > 127 for character in text)
        ):
            failures.append("gb18030: ambiguous incomplete trailing sequence")
            continue
        return _DecodedText(
            text=text,
            encoding=label,
            omitted_tail_bytes=omitted,
        )
    raise UnicodeError("; ".join(failures) or "no deterministic decoder succeeded")


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1:]
    return line, ""


def _render_lines(
    text: str,
    *,
    source_truncated: bool,
    limits: TextExtractionLimits,
) -> list[_RenderedLine]:
    raw_lines = text.splitlines(keepends=True)
    if text and not raw_lines:
        raw_lines = [text]
    rendered: list[_RenderedLine] = []
    for index, line in enumerate(raw_lines):
        body, ending = _split_line_ending(line)
        reasons: list[str] = []
        if len(body) > limits.max_line_characters:
            body = body[: limits.max_line_characters]
            reasons.append("line-character-limit")
        if source_truncated and index == len(raw_lines) - 1:
            reasons.append("source-byte-limit")
        rendered.append(
            _RenderedLine(
                text=body + ending,
                reason_codes=tuple(reasons),
            )
        )
    return rendered


def _reason_detail(reason_code: str) -> str:
    if reason_code == "line-character-limit":
        return "one or more source lines exceeded max_line_characters"
    if reason_code == "source-byte-limit":
        return "source content exceeded max_source_bytes"
    raise AssertionError(f"unknown truncation reason {reason_code}")


def _blocks_from_lines(
    lines: list[_RenderedLine],
    *,
    format_value: str,
    limits: TextExtractionLimits,
) -> tuple[Block, ...]:
    blocks: list[Block] = []
    for start_index in range(0, len(lines), limits.max_block_lines):
        group = lines[start_index : start_index + limits.max_block_lines]
        start_line = start_index + 1
        end_line = start_index + len(group)
        reason_codes = tuple(
            sorted({code for line in group for code in line.reason_codes})
        )
        reason_records = [
            {"code": code, "reason": _reason_detail(code)}
            for code in reason_codes
        ]
        if len(reason_codes) == 1:
            primary_code = reason_codes[0]
            primary_reason = _reason_detail(primary_code)
        elif len(reason_codes) > 1:
            primary_code = "multiple-content-limits"
            primary_reason = "multiple deterministic content limits were applied"
        else:
            primary_code = None
            primary_reason = None
        blocks.append(
            Block(
                block_id=f"lines-{start_line}-{end_line}",
                block_type=_block_type(format_value),
                text="".join(line.text for line in group),
                locator=LineRangeLocator(start_line, end_line),
                metadata={
                    "line_count": len(group),
                    "truncation_reasons": reason_records,
                },
                truncated=bool(reason_codes),
                truncation_reason_code=primary_code,
                truncation_reason=primary_reason,
            )
        )
    return tuple(blocks)


def _newline_styles(text: str) -> list[str]:
    styles: list[str] = []
    if "\r\n" in text:
        styles.append("crlf")
    without_crlf = text.replace("\r\n", "")
    if "\n" in without_crlf:
        styles.append("lf")
    if "\r" in without_crlf:
        styles.append("cr")
    return styles


def _unsupported_result(format_value: object) -> ExtractionResult:
    return ExtractionResult(
        status="unsupported",
        reason_code="unsupported-text-format",
        reason=f"format {format_value!r} is not a C-02 text-like format",
        document=None,
    )


def extract_text_bytes(
    data: bytes,
    *,
    relative_path: str,
    format_value: str,
    limits: TextExtractionLimits | None = None,
    encoding_hint: str | None = None,
) -> ExtractionResult:
    """Extract one in-memory text-like source without external services."""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if format_value not in SUPPORTED_TEXT_FORMATS:
        return _unsupported_result(format_value)
    chosen_limits = limits or TextExtractionLimits()
    digest = hashlib.sha256(data).hexdigest()
    prefix = data[: chosen_limits.max_source_bytes]
    return _extract_read_result(
        _ReadResult(
            prefix=prefix,
            content_sha256=digest,
            source_size_bytes=len(data),
            source_truncated=len(data) > len(prefix),
        ),
        relative_path=relative_path,
        format_value=format_value,
        limits=chosen_limits,
        encoding_hint=encoding_hint,
    )


def _extract_read_result(
    source: _ReadResult,
    *,
    relative_path: str,
    format_value: str,
    limits: TextExtractionLimits,
    encoding_hint: str | None,
) -> ExtractionResult:
    if format_value not in SUPPORTED_TEXT_FORMATS:
        return _unsupported_result(format_value)
    sample = source.prefix[: limits.binary_sample_bytes]
    if _looks_binary(sample):
        return ExtractionResult(
            status="failed",
            reason_code="binary-content-detected",
            reason="binary control bytes contradict the text-like classification",
            document=None,
        )
    try:
        decoded = _decode_text(
            source.prefix,
            source_truncated=source.source_truncated,
            encoding_hint=encoding_hint,
        )
    except (UnicodeError, ValueError) as exc:
        return ExtractionResult(
            status="failed",
            reason_code="text-decoding-failed",
            reason="no permitted deterministic text decoder succeeded",
            document=None,
            diagnostics=(str(exc),),
        )

    effective_source_truncated = (
        source.source_truncated or decoded.omitted_tail_bytes > 0
    )
    lines = _render_lines(
        decoded.text,
        source_truncated=effective_source_truncated,
        limits=limits,
    )
    blocks = _blocks_from_lines(
        lines,
        format_value=format_value,
        limits=limits,
    )
    truncated_blocks = sum(1 for block in blocks if block.truncated)
    truncation_reason_codes = sorted(
        {
            reason["code"]
            for block in blocks
            for reason in block.metadata["truncation_reasons"]
        }
    )
    if effective_source_truncated and "source-byte-limit" not in truncation_reason_codes:
        truncation_reason_codes.append("source-byte-limit")
        truncation_reason_codes.sort()
    partial = effective_source_truncated or truncated_blocks > 0
    diagnostics: list[str] = []
    if source.source_truncated:
        diagnostics.append(
            f"content limited to {limits.max_source_bytes} source bytes"
        )
    if decoded.omitted_tail_bytes:
        diagnostics.append(
            f"omitted {decoded.omitted_tail_bytes} incomplete trailing encoded bytes"
        )
    if any(
        reason["code"] == "line-character-limit"
        for block in blocks
        for reason in block.metadata["truncation_reasons"]
    ):
        diagnostics.append(
            f"one or more lines limited to {limits.max_line_characters} characters"
        )

    document = ExtractedDocument(
        path=relative_path,
        content_sha256=source.content_sha256,
        format=format_value,
        extractor=TEXT_EXTRACTOR_NAME,
        extractor_version=TEXT_EXTRACTOR_VERSION,
        encoding=decoded.encoding,
        blocks=blocks,
        metadata={
            "source_size_bytes": source.source_size_bytes,
            "extracted_prefix_bytes": len(source.prefix),
            "line_count": len(lines),
            "newline_styles": _newline_styles(decoded.text),
            "source_truncated": effective_source_truncated,
            "omitted_tail_bytes": decoded.omitted_tail_bytes,
            "omitted_source_bytes": (
                source.source_size_bytes
                - len(source.prefix)
                + decoded.omitted_tail_bytes
            ),
            "truncation_reasons": truncation_reason_codes,
        },
    )
    return ExtractionResult(
        status="partial" if partial else "processed",
        reason_code=(
            "deterministic-text-partial"
            if partial
            else "deterministic-text-extracted"
        ),
        reason=(
            "text was extracted with deterministic content limits"
            if partial
            else "all text content was deterministically extracted"
        ),
        document=document,
        diagnostics=tuple(diagnostics),
    )


def extract_text_file(
    source_path: str | Path,
    *,
    relative_path: str,
    format_value: str,
    expected_sha256: str | None = None,
    limits: TextExtractionLimits | None = None,
    encoding_hint: str | None = None,
) -> ExtractionResult:
    """Read one source file without writing it and return the C-01 result Schema."""

    path = Path(source_path)
    if format_value not in SUPPORTED_TEXT_FORMATS:
        return _unsupported_result(format_value)
    chosen_limits = limits or TextExtractionLimits()
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or _SHA256_PATTERN.fullmatch(expected_sha256) is None
    ):
        raise ValueError(
            "expected_sha256 must be 64 lowercase hexadecimal characters"
        )
    try:
        source = _read_prefix_and_hash(path, chosen_limits)
    except OSError as exc:
        return ExtractionResult(
            status="failed",
            reason_code="source-read-failed",
            reason="the source file could not be read",
            document=None,
            diagnostics=(str(exc),),
        )
    if expected_sha256 is not None and source.content_sha256 != expected_sha256:
        return ExtractionResult(
            status="failed",
            reason_code="content-hash-mismatch",
            reason="the source content no longer matches the requested file version",
            document=None,
            diagnostics=(
                f"expected {expected_sha256}; observed {source.content_sha256}",
            ),
        )
    return _extract_read_result(
        source,
        relative_path=relative_path,
        format_value=format_value,
        limits=chosen_limits,
        encoding_hint=encoding_hint,
    )
