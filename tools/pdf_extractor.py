#!/usr/bin/env python3
"""Deterministic C-04 page-level extraction for PDF files."""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
import pypdf
from pypdf import PdfReader

if __package__:
    from .extraction_schema import (
        Block,
        ExtractedDocument,
        ExtractionResult,
        PdfPageLocator,
    )
else:
    from extraction_schema import (  # type: ignore[no-redef]
        Block,
        ExtractedDocument,
        ExtractionResult,
        PdfPageLocator,
    )


PDF_EXTRACTOR_NAME = "deterministic-pdf"
PDF_EXTRACTOR_VERSION = "1"
PDF_BACKEND_NAME = "pypdf"
PDF_BACKEND_VERSION = pypdf.__version__
SUPPORTED_PDF_FORMATS = frozenset({"pdf"})

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_BASIC_METADATA_FIELDS = (
    "/Author",
    "/CreationDate",
    "/Creator",
    "/Keywords",
    "/ModDate",
    "/Producer",
    "/Subject",
    "/Title",
)
_PARTIAL_REASON_DETAILS = {
    "metadata-character-limit": "PDF document metadata exceeded max_metadata_characters",
    "page-character-limit": "PDF page text exceeded max_page_characters",
    "page-requires-ocr": "at least one PDF page requires later OCR",
    "page-requires-visual-review": (
        "at least one PDF page has no extractable text and requires later visual review"
    ),
}


@dataclass(frozen=True)
class PdfExtractionLimits:
    """Deterministic safety bounds for one PDF source."""

    max_source_bytes: int = 256 * 1024 * 1024
    max_pages: int = 10_000
    max_page_characters: int = 2 * 1024 * 1024
    max_metadata_characters: int = 4096
    low_text_character_threshold: int = 20
    read_chunk_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        positive_fields = (
            "max_source_bytes",
            "max_pages",
            "max_page_characters",
            "max_metadata_characters",
            "low_text_character_threshold",
            "read_chunk_bytes",
        )
        for field_name in positive_fields:
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.max_source_bytes < 8:
            raise ValueError("max_source_bytes must be at least 8")


@dataclass(frozen=True)
class _ReadResult:
    data: bytes
    content_sha256: str
    source_size_bytes: int


@dataclass(frozen=True)
class _MetadataResult:
    values: dict[str, str]
    keys: tuple[str, ...]
    truncated_fields: tuple[str, ...]


@dataclass(frozen=True)
class _PageResult:
    block: Block
    assessment: str
    ocr_recommended: bool
    visual_review_recommended: bool
    image_xobject_count: int
    page_text_truncated: bool


def _unsupported_result(format_value: object) -> ExtractionResult:
    return ExtractionResult(
        status="unsupported",
        reason_code="unsupported-pdf-format",
        reason=f"format {format_value!r} is not the C-04 PDF format",
        document=None,
    )


def _failed_result(
    reason_code: str,
    reason: str,
    *diagnostics: str,
) -> ExtractionResult:
    return ExtractionResult(
        status="failed",
        reason_code=reason_code,
        reason=reason,
        document=None,
        diagnostics=tuple(item for item in diagnostics if item),
    )


def _read_and_hash(path: Path, limits: PdfExtractionLimits) -> _ReadResult:
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
            remaining = limits.max_source_bytes - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
    return _ReadResult(
        data=bytes(retained),
        content_sha256=digest.hexdigest(),
        source_size_bytes=source_size,
    )


def _resolve_pdf_object(value: object) -> object:
    get_object = getattr(value, "get_object", None)
    if callable(get_object):
        return get_object()
    return value


def _mapping_items(value: object) -> list[tuple[object, object]]:
    resolved = _resolve_pdf_object(value)
    items = getattr(resolved, "items", None)
    if not callable(items):
        return []
    return list(items())


def _direct_image_xobject_count(page: object) -> int:
    get = getattr(page, "get", None)
    if not callable(get):
        return 0
    resources = _resolve_pdf_object(get("/Resources"))
    resources_get = getattr(resources, "get", None)
    if not callable(resources_get):
        return 0
    xobjects = resources_get("/XObject")
    count = 0
    for _, reference in sorted(_mapping_items(xobjects), key=lambda item: str(item[0])):
        resolved = _resolve_pdf_object(reference)
        object_get = getattr(resolved, "get", None)
        if callable(object_get) and str(object_get("/Subtype")) == "/Image":
            count += 1
    return count


def _page_geometry(page: object) -> tuple[float | None, float | None, int]:
    width: float | None = None
    height: float | None = None
    rotation = 0
    try:
        mediabox = getattr(page, "mediabox")
        width = float(mediabox.width)
        height = float(mediabox.height)
    except (AttributeError, TypeError, ValueError):
        pass
    get = getattr(page, "get", None)
    if callable(get):
        raw_rotation = get("/Rotate", 0)
        if isinstance(raw_rotation, int) and not isinstance(raw_rotation, bool):
            rotation = raw_rotation % 360
    return width, height, rotation


def _metadata_key(value: object) -> str:
    key = str(value)
    return key if key.startswith("/") else f"/{key}"


def _extract_metadata(reader: PdfReader, limits: PdfExtractionLimits) -> _MetadataResult:
    metadata = reader.metadata
    if metadata is None:
        return _MetadataResult(values={}, keys=(), truncated_fields=())
    keys = tuple(sorted(_metadata_key(key) for key in metadata.keys()))
    values: dict[str, str] = {}
    truncated_fields: list[str] = []
    for field_name in _BASIC_METADATA_FIELDS:
        value = metadata.get(field_name)
        if value is None:
            continue
        rendered = str(value)
        if len(rendered) > limits.max_metadata_characters:
            rendered = rendered[: limits.max_metadata_characters]
            truncated_fields.append(field_name)
        values[field_name.removeprefix("/").lower()] = rendered
    return _MetadataResult(
        values=values,
        keys=keys,
        truncated_fields=tuple(truncated_fields),
    )


def _assess_page(
    *,
    non_whitespace_characters: int,
    image_xobject_count: int,
    limits: PdfExtractionLimits,
) -> tuple[str, bool, bool, str]:
    if non_whitespace_characters == 0 and image_xobject_count > 0:
        return "likely_scanned", True, False, "image-page-without-extractable-text"
    if non_whitespace_characters == 0:
        return "no_text", False, True, "page-without-extractable-text"
    if non_whitespace_characters < limits.low_text_character_threshold:
        return "low_text", True, False, "low-extractable-text"
    return "text", False, False, "sufficient-extractable-text"


def _extract_page(
    page: object,
    *,
    page_number: int,
    limits: PdfExtractionLimits,
) -> _PageResult:
    extract_text = getattr(page, "extract_text", None)
    if not callable(extract_text):
        raise ValueError(f"page {page_number} does not expose text extraction")
    extracted = extract_text()
    if extracted is None:
        extracted = ""
    if not isinstance(extracted, str):
        raise ValueError(f"page {page_number} returned non-text extraction output")
    image_xobject_count = _direct_image_xobject_count(page)
    non_whitespace = sum(not character.isspace() for character in extracted)
    assessment, ocr_recommended, visual_review, assessment_reason = _assess_page(
        non_whitespace_characters=non_whitespace,
        image_xobject_count=image_xobject_count,
        limits=limits,
    )
    page_text_truncated = len(extracted) > limits.max_page_characters
    text = extracted[: limits.max_page_characters]
    truncation_reasons = (
        [
            {
                "code": "page-character-limit",
                "reason": _PARTIAL_REASON_DETAILS["page-character-limit"],
            }
        ]
        if page_text_truncated
        else []
    )
    width, height, rotation = _page_geometry(page)
    block = Block(
        block_id=f"page-{page_number}",
        block_type="text",
        text=text,
        locator=PdfPageLocator(page_number),
        metadata={
            "page_number": page_number,
            "text_character_count": len(extracted),
            "non_whitespace_character_count": non_whitespace,
            "text_sha256": hashlib.sha256(extracted.encode("utf-8")).hexdigest(),
            "image_xobject_count": image_xobject_count,
            "assessment": assessment,
            "assessment_reason_code": assessment_reason,
            "ocr_recommended": ocr_recommended,
            "visual_review_recommended": visual_review,
            "width_points": width,
            "height_points": height,
            "rotation_degrees": rotation,
            "truncation_reasons": truncation_reasons,
        },
        truncated=page_text_truncated,
        truncation_reason_code=("page-character-limit" if page_text_truncated else None),
        truncation_reason=(
            _PARTIAL_REASON_DETAILS["page-character-limit"]
            if page_text_truncated
            else None
        ),
    )
    return _PageResult(
        block=block,
        assessment=assessment,
        ocr_recommended=ocr_recommended,
        visual_review_recommended=visual_review,
        image_xobject_count=image_xobject_count,
        page_text_truncated=page_text_truncated,
    )


def _reader_header(reader: PdfReader) -> str | None:
    header = getattr(reader, "pdf_header", None)
    return str(header) if header is not None else None


def _build_document(
    reader: PdfReader,
    *,
    relative_path: str,
    content_sha256: str,
    source_size_bytes: int,
    limits: PdfExtractionLimits,
) -> ExtractionResult:
    if reader.is_encrypted:
        return ExtractionResult(
            status="unsupported",
            reason_code="encrypted-pdf-unsupported",
            reason="encrypted PDF files are outside the C-04 extraction contract",
            document=None,
        )
    try:
        page_count = len(reader.pages)
    except Exception as exc:
        return _failed_result(
            "pdf-page-tree-invalid",
            "the PDF page tree could not be read safely",
            str(exc),
        )
    if page_count > limits.max_pages:
        return _failed_result(
            "pdf-page-count-limit",
            "the PDF exceeds the deterministic page-count limit",
            f"source has {page_count} pages; limit is {limits.max_pages}",
        )
    try:
        metadata = _extract_metadata(reader, limits)
    except Exception as exc:
        return _failed_result(
            "pdf-metadata-invalid",
            "the PDF document metadata could not be read safely",
            str(exc),
        )
    page_results: list[_PageResult] = []
    for page_index in range(page_count):
        page_number = page_index + 1
        try:
            page_results.append(
                _extract_page(
                    reader.pages[page_index],
                    page_number=page_number,
                    limits=limits,
                )
            )
        except Exception as exc:
            return _failed_result(
                "pdf-page-extraction-failed",
                "a PDF page could not be extracted without fabricating text",
                f"page {page_number}: {exc}",
            )

    ocr_pages = [
        result.block.locator.page_number
        for result in page_results
        if result.ocr_recommended
        and isinstance(result.block.locator, PdfPageLocator)
    ]
    visual_review_pages = [
        result.block.locator.page_number
        for result in page_results
        if result.visual_review_recommended
        and isinstance(result.block.locator, PdfPageLocator)
    ]
    truncated_pages = [
        result.block.locator.page_number
        for result in page_results
        if result.page_text_truncated
        and isinstance(result.block.locator, PdfPageLocator)
    ]
    partial_reasons: set[str] = set()
    if metadata.truncated_fields:
        partial_reasons.add("metadata-character-limit")
    if truncated_pages:
        partial_reasons.add("page-character-limit")
    if ocr_pages:
        partial_reasons.add("page-requires-ocr")
    if visual_review_pages:
        partial_reasons.add("page-requires-visual-review")

    assessment_counts = {
        assessment: sum(result.assessment == assessment for result in page_results)
        for assessment in ("text", "low_text", "no_text", "likely_scanned")
    }
    document = ExtractedDocument(
        path=relative_path,
        content_sha256=content_sha256,
        format="pdf",
        extractor=PDF_EXTRACTOR_NAME,
        extractor_version=PDF_EXTRACTOR_VERSION,
        encoding=None,
        blocks=tuple(result.block for result in page_results),
        metadata={
            "source_size_bytes": source_size_bytes,
            "pdf_header": _reader_header(reader),
            "backend": PDF_BACKEND_NAME,
            "backend_version": PDF_BACKEND_VERSION,
            "page_count": page_count,
            "metadata_keys": list(metadata.keys),
            "basic_metadata": metadata.values,
            "metadata_truncated_fields": list(metadata.truncated_fields),
            "assessment_counts": assessment_counts,
            "pages_with_images": [
                result.block.locator.page_number
                for result in page_results
                if result.image_xobject_count > 0
                and isinstance(result.block.locator, PdfPageLocator)
            ],
            "ocr_recommended_pages": ocr_pages,
            "visual_review_recommended_pages": visual_review_pages,
            "truncated_pages": truncated_pages,
            "partial_reasons": sorted(partial_reasons),
            "low_text_character_threshold": limits.low_text_character_threshold,
        },
    )
    partial = bool(partial_reasons)
    diagnostics = tuple(
        _PARTIAL_REASON_DETAILS[reason_code]
        for reason_code in sorted(partial_reasons)
    )
    return ExtractionResult(
        status="partial" if partial else "processed",
        reason_code=(
            "deterministic-pdf-partial" if partial else "deterministic-pdf-extracted"
        ),
        reason=(
            "PDF pages were extracted with pages requiring bounded follow-up"
            if partial
            else "all PDF pages were deterministically extracted"
        ),
        document=document,
        diagnostics=diagnostics,
    )


def _extract_pdf_data(
    data: bytes,
    *,
    relative_path: str,
    content_sha256: str,
    source_size_bytes: int,
    limits: PdfExtractionLimits,
) -> ExtractionResult:
    if source_size_bytes > limits.max_source_bytes:
        return _failed_result(
            "pdf-source-byte-limit",
            "the PDF exceeds the deterministic source byte limit",
            f"source has {source_size_bytes} bytes; limit is {limits.max_source_bytes}",
            f"content SHA-256 is {content_sha256}",
        )
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except Exception as exc:
        return _failed_result(
            "pdf-parse-failed",
            "the PDF could not be parsed safely",
            str(exc),
        )
    return _build_document(
        reader,
        relative_path=relative_path,
        content_sha256=content_sha256,
        source_size_bytes=source_size_bytes,
        limits=limits,
    )


def extract_pdf_bytes(
    data: bytes,
    *,
    relative_path: str,
    format_value: str = "pdf",
    limits: PdfExtractionLimits | None = None,
) -> ExtractionResult:
    """Extract one in-memory PDF without OCR, writes, or external services."""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if format_value not in SUPPORTED_PDF_FORMATS:
        return _unsupported_result(format_value)
    chosen_limits = limits or PdfExtractionLimits()
    return _extract_pdf_data(
        data[: chosen_limits.max_source_bytes],
        relative_path=relative_path,
        content_sha256=hashlib.sha256(data).hexdigest(),
        source_size_bytes=len(data),
        limits=chosen_limits,
    )


def extract_pdf_file(
    source_path: str | Path,
    *,
    relative_path: str,
    format_value: str = "pdf",
    expected_sha256: str | None = None,
    limits: PdfExtractionLimits | None = None,
) -> ExtractionResult:
    """Read one PDF without writing it and return the C-01 result Schema."""

    if format_value not in SUPPORTED_PDF_FORMATS:
        return _unsupported_result(format_value)
    chosen_limits = limits or PdfExtractionLimits()
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or _SHA256_PATTERN.fullmatch(expected_sha256) is None
    ):
        raise ValueError(
            "expected_sha256 must be 64 lowercase hexadecimal characters"
        )
    try:
        source = _read_and_hash(Path(source_path), chosen_limits)
    except OSError as exc:
        return _failed_result(
            "source-read-failed",
            "the PDF source file could not be read",
            str(exc),
        )
    if expected_sha256 is not None and source.content_sha256 != expected_sha256:
        return _failed_result(
            "content-hash-mismatch",
            "the PDF content no longer matches the requested file version",
            f"expected {expected_sha256}; observed {source.content_sha256}",
        )
    return _extract_pdf_data(
        source.data,
        relative_path=relative_path,
        content_sha256=source.content_sha256,
        source_size_bytes=source.source_size_bytes,
        limits=chosen_limits,
    )
