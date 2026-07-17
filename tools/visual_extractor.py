#!/usr/bin/env python3
"""Bounded C-05 visual and OCR extraction for selected research artifacts.

The low-level functions in this module are intentionally in-memory and do not
persist extraction state. Project execution is added at the bottom of the
module and is authorized only by the current B-07 reading-priority artifact.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol, runtime_checkable

from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader

if __package__:
    from .extraction_schema import (
        Block,
        ExtractedDocument,
        ExtractionResult,
        ImageRegionLocator,
        Locator,
        PdfPageLocator,
    )
    from .pdf_extractor import extract_pdf_bytes
else:
    from extraction_schema import (  # type: ignore[no-redef]
        Block,
        ExtractedDocument,
        ExtractionResult,
        ImageRegionLocator,
        Locator,
        PdfPageLocator,
    )
    from pdf_extractor import extract_pdf_bytes  # type: ignore[no-redef]


VISUAL_EXTRACTOR_NAME = "bounded-visual-ocr"
VISUAL_EXTRACTOR_VERSION = "1"
SUPPORTED_RASTER_FORMATS = frozenset({"png", "jpeg", "gif", "tiff", "bmp"})
SUPPORTED_VISUAL_FORMATS = SUPPORTED_RASTER_FORMATS | {"pdf"}
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_STABLE_CODE_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


class VisualExtractionError(RuntimeError):
    """Raised for a fail-closed project-execution or backend contract error."""


class VisualBackendUnavailable(VisualExtractionError):
    """Raised when a configured backend cannot run in the current environment."""


class PdfRendererContractError(VisualExtractionError):
    """Raised when a PDF renderer returns malformed or over-budget payloads."""


ExternalSendAuthorizer = Callable[[], None]


@dataclass(frozen=True)
class VisualExtractionLimits:
    """Local resource and output bounds for one selected visual source."""

    max_source_bytes: int = 32 * 1024 * 1024
    max_width: int = 16_384
    max_height: int = 16_384
    max_pixels_per_frame: int = 64_000_000
    max_total_pixels: int = 96_000_000
    max_frames: int = 4
    max_output_characters: int = 200_000
    max_pdf_followup_pages: int = 64
    max_pdf_images_per_page: int = 16
    max_pdf_image_bytes: int = 32 * 1024 * 1024
    backend_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        for field_name in (
            "max_source_bytes",
            "max_width",
            "max_height",
            "max_pixels_per_frame",
            "max_total_pixels",
            "max_frames",
            "max_output_characters",
            "max_pdf_followup_pages",
            "max_pdf_images_per_page",
            "max_pdf_image_bytes",
            "backend_timeout_seconds",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True)
class VisualBackendOutput:
    """Text and JSON-compatible metadata returned by one visual backend."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("visual backend output text must be a string")
        if not isinstance(self.metadata, dict):
            raise TypeError("visual backend output metadata must be an object")
        try:
            json.dumps(self.metadata, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError("visual backend metadata must contain only JSON values") from exc
        pending: list[object] = [self.metadata]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise TypeError(
                        "visual backend metadata object keys must be strings"
                    )
                pending.extend(value.values())
            elif isinstance(value, (list, tuple)):
                pending.extend(value)


@dataclass(frozen=True)
class VisualPayload:
    """One normalized image submitted to OCR or semantic visual analysis."""

    png_bytes: bytes
    rgba_bytes: bytes
    width: int
    height: int
    locator: Locator
    frame_index: int
    image_index: int
    source_format: str

    def __post_init__(self) -> None:
        if not isinstance(self.png_bytes, bytes) or not self.png_bytes:
            raise TypeError("png_bytes must be non-empty bytes")
        if not isinstance(self.rgba_bytes, bytes) or not self.rgba_bytes:
            raise TypeError("rgba_bytes must be non-empty bytes")
        if self.width < 1 or self.height < 1:
            raise ValueError("payload dimensions must be positive")
        if len(self.rgba_bytes) != self.width * self.height * 4:
            raise ValueError("rgba_bytes do not match payload dimensions")
        if not isinstance(self.locator, Locator):
            raise TypeError("payload locator must be a Locator")
        if self.frame_index < 0 or self.image_index < 0:
            raise ValueError("payload indexes must be non-negative")

    @property
    def rgba_sha256(self) -> str:
        return hashlib.sha256(self.rgba_bytes).hexdigest()

    @property
    def png_sha256(self) -> str:
        return hashlib.sha256(self.png_bytes).hexdigest()


@dataclass(frozen=True)
class PdfPageRenderOutput:
    """Normalized direct images recovered from one PDF page."""

    payloads: tuple[VisualPayload, ...]
    diagnostics: tuple[str, ...] = ()
    partial_reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        payloads = tuple(self.payloads)
        diagnostics = tuple(self.diagnostics)
        partial_reason_codes = tuple(self.partial_reason_codes)
        if not all(isinstance(payload, VisualPayload) for payload in payloads):
            raise PdfRendererContractError(
                "PDF renderer payloads must contain only VisualPayload values"
            )
        if any(payload.source_format != "pdf" for payload in payloads):
            raise PdfRendererContractError(
                "PDF renderer payload source_format must be 'pdf'"
            )
        image_indexes = [payload.image_index for payload in payloads]
        if len(image_indexes) != len(set(image_indexes)):
            raise PdfRendererContractError(
                "PDF renderer payload image_index values must be unique per page"
            )
        if not all(isinstance(item, str) and item for item in diagnostics):
            raise PdfRendererContractError(
                "PDF renderer diagnostics must be non-empty strings"
            )
        if not all(
            isinstance(code, str) and _STABLE_CODE_PATTERN.fullmatch(code)
            for code in partial_reason_codes
        ):
            raise PdfRendererContractError(
                "PDF renderer partial reason codes must be stable codes"
            )
        object.__setattr__(self, "payloads", payloads)
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "partial_reason_codes", partial_reason_codes)


@runtime_checkable
class OcrBackend(Protocol):
    name: str
    version: str
    external_send: bool

    def recognize(
        self, payload: VisualPayload, *, timeout_seconds: int
    ) -> VisualBackendOutput:
        """Return OCR text for one normalized image or raise an explicit error."""


@runtime_checkable
class VisualAnalysisBackend(Protocol):
    name: str
    version: str
    external_send: bool

    def analyze(
        self, payload: VisualPayload, *, timeout_seconds: int
    ) -> VisualBackendOutput:
        """Return a grounded semantic description for one normalized image."""


@runtime_checkable
class PdfPageRenderer(Protocol):
    name: str
    version: str
    external_send: bool

    def render_page(
        self,
        data: bytes,
        *,
        page_number: int,
        limits: VisualExtractionLimits,
    ) -> PdfPageRenderOutput:
        """Return bounded normalized images for one one-based PDF page."""


class TesseractCliOcrBackend:
    """Local, no-network OCR through an already-installed Tesseract executable."""

    name = "tesseract-cli"
    version = "system"
    external_send = False

    def __init__(self, executable: str | None = None, *, page_segmentation_mode: int = 6):
        if not isinstance(page_segmentation_mode, int) or isinstance(
            page_segmentation_mode, bool
        ) or not 0 <= page_segmentation_mode <= 13:
            raise ValueError("page_segmentation_mode must be an integer from 0 to 13")
        self.executable = executable
        self.page_segmentation_mode = page_segmentation_mode

    def _resolved_executable(self) -> str:
        if self.executable:
            resolved = shutil.which(self.executable)
        else:
            resolved = shutil.which("tesseract")
        if not resolved:
            raise VisualBackendUnavailable(
                "the local tesseract executable is not available"
            )
        return resolved

    def recognize(
        self, payload: VisualPayload, *, timeout_seconds: int
    ) -> VisualBackendOutput:
        executable = self._resolved_executable()
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                handle.write(payload.png_bytes)
                temporary_name = handle.name
            completed = subprocess.run(
                [
                    executable,
                    temporary_name,
                    "stdout",
                    "--psm",
                    str(self.page_segmentation_mode),
                ],
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise VisualExtractionError("tesseract OCR timed out") from exc
        except OSError as exc:
            raise VisualBackendUnavailable(f"tesseract could not be started: {exc}") from exc
        finally:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError as exc:
                    raise VisualExtractionError(
                        "the sensitive temporary OCR image could not be removed"
                    ) from exc
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        if completed.returncode != 0:
            raise VisualExtractionError(
                f"tesseract exited with status {completed.returncode}"
                + (f": {stderr[:500]}" if stderr else "")
            )
        text = completed.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
        return VisualBackendOutput(
            text=text,
            metadata={
                "page_segmentation_mode": self.page_segmentation_mode,
                "stderr_present": bool(stderr),
            },
        )

@dataclass
class _OutputBudget:
    remaining: int
    truncated: bool = False

    def take(self, text: str) -> tuple[str, bool]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if len(normalized) <= self.remaining:
            self.remaining -= len(normalized)
            return normalized, False
        retained = normalized[: self.remaining]
        self.remaining = 0
        self.truncated = True
        return retained, True


def _backend_identity(backend: object) -> tuple[str, str, bool]:
    name = getattr(backend, "name", None)
    version = getattr(backend, "version", None)
    external_send = getattr(backend, "external_send", None)
    if not isinstance(name, str) or not name.strip():
        raise VisualExtractionError("visual backend name must be a non-empty string")
    if not isinstance(version, str) or not version.strip():
        raise VisualExtractionError("visual backend version must be a non-empty string")
    if not isinstance(external_send, bool):
        raise VisualExtractionError("visual backend external_send must be a boolean")
    return name, version, external_send


def _failed(reason_code: str, reason: str, *diagnostics: str) -> ExtractionResult:
    return ExtractionResult(
        status="failed",
        reason_code=reason_code,
        reason=reason,
        document=None,
        diagnostics=tuple(item for item in diagnostics if item),
    )


def _unsupported(format_value: object) -> ExtractionResult:
    return ExtractionResult(
        status="unsupported",
        reason_code="unsupported-visual-format",
        reason=f"format {format_value!r} is outside the C-05 visual formats",
        document=None,
    )


def _validate_sha256(value: str | None) -> None:
    if value is not None and (
        not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("expected_sha256 must be 64 lowercase hexadecimal characters")


def _normalized_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("relative_path must be a non-empty string")
    parsed = PurePosixPath(value)
    if (
        "\\" in value
        or parsed.is_absolute()
        or parsed.as_posix() != value
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise ValueError("relative_path must be normalized project-relative POSIX form")
    return value


def _normalized_image_size(image: Image.Image) -> tuple[int, int]:
    width, height = image.size
    try:
        orientation = image.getexif().get(274)
    except (AttributeError, OSError, SyntaxError, ValueError):
        orientation = None
    if orientation in {5, 6, 7, 8}:
        width, height = height, width
    return width, height


def _bounded_pixel_count(
    width: int,
    height: int,
    *,
    limits: VisualExtractionLimits,
) -> int:
    if width < 1 or height < 1:
        raise VisualExtractionError("decoded image has empty dimensions")
    if width > limits.max_width or height > limits.max_height:
        raise VisualExtractionError(
            f"decoded image dimensions {width}x{height} exceed "
            f"{limits.max_width}x{limits.max_height}"
        )
    pixels = width * height
    if pixels > limits.max_pixels_per_frame:
        raise VisualExtractionError(
            f"decoded image has {pixels} pixels; limit is {limits.max_pixels_per_frame}"
        )
    return pixels


def _encode_payload(
    image: Image.Image,
    *,
    locator: Locator,
    frame_index: int,
    image_index: int,
    source_format: str,
    limits: VisualExtractionLimits,
) -> VisualPayload:
    normalized = ImageOps.exif_transpose(image)
    width, height = normalized.size
    _bounded_pixel_count(width, height, limits=limits)
    rgba = normalized.convert("RGBA")
    rgba.load()
    rgba_bytes = rgba.tobytes()
    destination = io.BytesIO()
    rgba.save(destination, format="PNG", optimize=False, compress_level=9)
    return VisualPayload(
        png_bytes=destination.getvalue(),
        rgba_bytes=rgba_bytes,
        width=width,
        height=height,
        locator=locator,
        frame_index=frame_index,
        image_index=image_index,
        source_format=source_format,
    )


def _decode_standalone_payloads(
    data: bytes,
    *,
    format_value: str,
    limits: VisualExtractionLimits,
) -> tuple[tuple[VisualPayload, ...], list[str], list[str]]:
    payloads: list[VisualPayload] = []
    diagnostics: list[str] = []
    partial_codes: list[str] = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                declared_frames = getattr(source, "n_frames", 1)
                if not isinstance(declared_frames, int) or declared_frames < 1:
                    declared_frames = 1
                retained_frames = min(declared_frames, limits.max_frames)
                if declared_frames > limits.max_frames:
                    partial_codes.append("image-frame-limit")
                    diagnostics.append(
                        f"source declares {declared_frames} frames; "
                        f"only {limits.max_frames} were considered"
                    )
                total_pixels = 0
                for frame_index in range(retained_frames):
                    try:
                        source.seek(frame_index)
                        width, height = _normalized_image_size(source)
                        pixels = _bounded_pixel_count(width, height, limits=limits)
                        if total_pixels + pixels > limits.max_total_pixels:
                            partial_codes.append("image-total-pixel-limit")
                            diagnostics.append(
                                f"frame {frame_index} would exceed the total pixel limit"
                            )
                            break
                        frame = source.copy()
                        payload = _encode_payload(
                            frame,
                            locator=ImageRegionLocator(
                                frame_index=frame_index,
                                x=0,
                                y=0,
                                width=width,
                                height=height,
                            ),
                            frame_index=frame_index,
                            image_index=frame_index,
                            source_format=format_value,
                            limits=limits,
                        )
                    except (
                        Image.DecompressionBombError,
                        Image.DecompressionBombWarning,
                        UnidentifiedImageError,
                        OSError,
                        SyntaxError,
                        EOFError,
                        VisualExtractionError,
                    ) as exc:
                        if payloads:
                            partial_codes.append("image-frame-processing-failed")
                            diagnostics.append(
                                f"frame {frame_index} could not be retained safely: {exc}"
                            )
                            break
                        if isinstance(
                            exc,
                            (Image.DecompressionBombError, Image.DecompressionBombWarning),
                        ):
                            raise VisualExtractionError(
                                f"Pillow rejected a decompression bomb: {exc}"
                            ) from exc
                        if isinstance(exc, VisualExtractionError):
                            raise
                        raise VisualExtractionError(
                            f"the raster source could not be decoded: {exc}"
                        ) from exc
                    payloads.append(payload)
                    total_pixels += payload.width * payload.height
    except Image.DecompressionBombError as exc:
        raise VisualExtractionError(f"Pillow rejected a decompression bomb: {exc}") from exc
    except Image.DecompressionBombWarning as exc:
        raise VisualExtractionError(f"Pillow warned about a decompression bomb: {exc}") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, EOFError) as exc:
        raise VisualExtractionError(f"the raster source could not be decoded: {exc}") from exc
    return tuple(payloads), diagnostics, partial_codes


class PypdfPageImageRenderer:
    """Local PDF follow-up renderer using direct image objects exposed by pypdf.

    This deliberately does not rasterize arbitrary PDF drawing commands. When a
    selected follow-up page has no recoverable direct image, the caller records
    an explicit partial state instead of fabricating pixels or silently adding a
    heavyweight rendering dependency.
    """

    name = "pypdf-direct-images"
    version = "1"
    external_send = False

    def render_page(
        self,
        data: bytes,
        *,
        page_number: int,
        limits: VisualExtractionLimits,
    ) -> PdfPageRenderOutput:
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            if page_number < 1 or page_number > len(reader.pages):
                raise VisualExtractionError(
                    f"PDF page {page_number} is outside the document"
                )
            images = list(reader.pages[page_number - 1].images)
        except VisualExtractionError:
            raise
        except Exception as exc:
            raise VisualExtractionError(
                f"pypdf could not enumerate direct images on page {page_number}: {exc}"
            ) from exc

        diagnostics: list[str] = []
        partial_codes: list[str] = []
        if not images:
            return PdfPageRenderOutput(
                payloads=(),
                diagnostics=(
                    f"page {page_number} has no direct image recoverable by pypdf",
                ),
                partial_reason_codes=("pdf-direct-image-unavailable",),
            )
        retained = images[: limits.max_pdf_images_per_page]
        if len(images) > len(retained):
            partial_codes.append("pdf-page-image-count-limit")
            diagnostics.append(
                f"page {page_number} exposes {len(images)} direct images; "
                f"only {len(retained)} were considered"
            )
        payloads: list[VisualPayload] = []
        total_pixels = 0
        for image_index, image_file in enumerate(retained):
            image_data = getattr(image_file, "data", None)
            if not isinstance(image_data, bytes) or not image_data:
                partial_codes.append("pdf-direct-image-invalid")
                diagnostics.append(
                    f"page {page_number} image {image_index} has no byte payload"
                )
                continue
            if len(image_data) > limits.max_pdf_image_bytes:
                partial_codes.append("pdf-image-byte-limit")
                diagnostics.append(
                    f"page {page_number} image {image_index} has {len(image_data)} "
                    f"bytes; limit is {limits.max_pdf_image_bytes}"
                )
                continue
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    decoded = Image.open(io.BytesIO(image_data))
                    decoded.seek(0)
                    width, height = _normalized_image_size(decoded)
                    pixels = _bounded_pixel_count(width, height, limits=limits)
                    if total_pixels + pixels > limits.max_total_pixels:
                        partial_codes.append("image-total-pixel-limit")
                        diagnostics.append(
                            f"page {page_number} image {image_index} would exceed "
                            "the total pixel limit"
                        )
                        break
                    payload = _encode_payload(
                        decoded,
                        locator=PdfPageLocator(page_number),
                        frame_index=0,
                        image_index=image_index,
                        source_format="pdf",
                        limits=limits,
                    )
                payloads.append(payload)
                total_pixels += pixels
            except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                partial_codes.append("pdf-image-decompression-bomb")
                diagnostics.append(
                    f"page {page_number} image {image_index} was rejected: {exc}"
                )
            except (UnidentifiedImageError, OSError, SyntaxError, EOFError, VisualExtractionError) as exc:
                partial_codes.append("pdf-direct-image-invalid")
                diagnostics.append(
                    f"page {page_number} image {image_index} could not be decoded: {exc}"
                )
        if not payloads and "pdf-direct-image-unavailable" not in partial_codes:
            partial_codes.append("pdf-direct-image-unavailable")
        return PdfPageRenderOutput(
            payloads=tuple(payloads),
            diagnostics=tuple(diagnostics),
            partial_reason_codes=tuple(sorted(set(partial_codes))),
        )

_DEFAULT_OCR_BACKEND = object()
_DEFAULT_PDF_RENDERER = object()


def _attempt_base(
    *,
    operation: str,
    backend_name: str,
    backend_version: str,
    external_send: bool,
    payload: VisualPayload,
) -> dict[str, Any]:
    return {
        "operation": operation,
        "backend": backend_name,
        "backend_version": backend_version,
        "external_send": external_send,
        "locator": payload.locator.as_dict(),
        "frame_index": payload.frame_index,
        "image_index": payload.image_index,
        "width": payload.width,
        "height": payload.height,
        "rgba_sha256": payload.rgba_sha256,
        "png_sha256": payload.png_sha256,
    }


def _invoke_backend(
    backend: OcrBackend | VisualAnalysisBackend,
    payload: VisualPayload,
    *,
    operation: str,
    allow_external_send: bool,
    limits: VisualExtractionLimits,
    output_budget: _OutputBudget,
    block_id: str,
    external_send_authorizer: ExternalSendAuthorizer | None = None,
) -> tuple[Block | None, dict[str, Any], str | None, bool]:
    """Return block, attempt, partial code, and whether execution crashed."""

    backend_name, backend_version, external_send = _backend_identity(backend)
    attempt = _attempt_base(
        operation=operation,
        backend_name=backend_name,
        backend_version=backend_version,
        external_send=external_send,
        payload=payload,
    )
    if external_send and not allow_external_send:
        attempt.update(
            {
                "status": "denied",
                "reason_code": "raw-external-send-denied",
                "output_characters": 0,
            }
        )
        return None, attempt, "raw-external-send-denied", False

    try:
        if operation == "ocr":
            invoke = getattr(backend, "recognize", None)
            if not callable(invoke):
                raise VisualExtractionError("OCR backend does not implement recognize")
        else:
            invoke = getattr(backend, "analyze", None)
            if not callable(invoke):
                raise VisualExtractionError(
                    "visual-analysis backend does not implement analyze"
                )
    except Exception as exc:
        attempt.update(
            {
                "status": "failed",
                "reason_code": f"{operation}-backend-failed",
                "output_characters": 0,
                "diagnostic": str(exc)[:1000],
            }
        )
        return None, attempt, f"{operation}-backend-failed", True

    if external_send and external_send_authorizer is not None:
        external_send_authorizer()

    try:
        output = invoke(payload, timeout_seconds=limits.backend_timeout_seconds)
        if not isinstance(output, VisualBackendOutput):
            raise VisualExtractionError(
                "visual backend must return VisualBackendOutput"
            )
    except VisualBackendUnavailable as exc:
        attempt.update(
            {
                "status": "unavailable",
                "reason_code": f"{operation}-backend-unavailable",
                "output_characters": 0,
                "diagnostic": str(exc)[:1000],
            }
        )
        return None, attempt, f"{operation}-backend-unavailable", False
    except Exception as exc:
        attempt.update(
            {
                "status": "failed",
                "reason_code": f"{operation}-backend-failed",
                "output_characters": 0,
                "diagnostic": str(exc)[:1000],
            }
        )
        return None, attempt, f"{operation}-backend-failed", True

    raw_text = output.text.replace("\r\n", "\n").replace("\r", "\n")
    if not raw_text.strip():
        attempt.update(
            {
                "status": "empty",
                "reason_code": f"{operation}-empty-output",
                "output_characters": len(raw_text),
                "output_sha256": hashlib.sha256(
                    raw_text.encode("utf-8")
                ).hexdigest(),
            }
        )
        return None, attempt, f"{operation}-empty-output", False
    retained, truncated = output_budget.take(raw_text)
    attempt.update(
        {
            "status": "succeeded",
            "reason_code": (
                "visual-output-character-limit" if truncated else f"{operation}-succeeded"
            ),
            "output_characters": len(raw_text),
            "retained_characters": len(retained),
            "output_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        }
    )
    metadata = {
        "derived_output": True,
        "operation": operation,
        "backend": backend_name,
        "backend_version": backend_version,
        "external_send": external_send,
        "frame_index": payload.frame_index,
        "image_index": payload.image_index,
        "decoded_width": payload.width,
        "decoded_height": payload.height,
        "rgba_sha256": payload.rgba_sha256,
        "normalized_png_sha256": payload.png_sha256,
        "raw_output_character_count": len(raw_text),
        "raw_output_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        "backend_metadata": output.metadata,
    }
    block = Block(
        block_id=block_id,
        block_type="text" if operation == "ocr" else "output_summary",
        text=retained,
        locator=payload.locator,
        metadata=metadata,
        truncated=truncated,
        truncation_reason_code=(
            "visual-output-character-limit" if truncated else None
        ),
        truncation_reason=(
            "derived visual text exceeded the per-document output character limit"
            if truncated
            else None
        ),
    )
    return (
        block,
        attempt,
        "visual-output-character-limit" if truncated else None,
        False,
    )


def _payload_metadata(payload: VisualPayload) -> dict[str, Any]:
    return {
        "locator": payload.locator.as_dict(),
        "frame_index": payload.frame_index,
        "image_index": payload.image_index,
        "width": payload.width,
        "height": payload.height,
        "pixel_count": payload.width * payload.height,
        "rgba_sha256": payload.rgba_sha256,
        "normalized_png_sha256": payload.png_sha256,
    }


def _chosen_ocr_backend(
    value: OcrBackend | None | object,
) -> OcrBackend | None:
    if value is _DEFAULT_OCR_BACKEND:
        return TesseractCliOcrBackend()
    if value is None:
        return None
    return value  # type: ignore[return-value]


def _extract_raster_data(
    data: bytes,
    *,
    relative_path: str,
    format_value: str,
    content_sha256: str,
    ocr_backend: OcrBackend | None,
    visual_backend: VisualAnalysisBackend | None,
    allow_external_send: bool,
    limits: VisualExtractionLimits,
    external_send_authorizer: ExternalSendAuthorizer | None = None,
) -> ExtractionResult:
    try:
        payloads, diagnostics, partial_codes = _decode_standalone_payloads(
            data,
            format_value=format_value,
            limits=limits,
        )
    except VisualExtractionError as exc:
        return _failed(
            "raster-decode-failed",
            "the selected raster source could not be decoded within safety bounds",
            str(exc),
        )
    if not payloads:
        return _failed(
            "raster-no-processable-frame",
            "the selected raster source has no frame within the visual safety bounds",
            *diagnostics,
        )

    configured: list[tuple[str, OcrBackend | VisualAnalysisBackend]] = []
    if ocr_backend is not None:
        configured.append(("ocr", ocr_backend))
    if visual_backend is not None:
        configured.append(("visual-analysis", visual_backend))
    attempts: list[dict[str, Any]] = []
    blocks: list[Block] = []
    crashed_attempts = 0
    output_budget = _OutputBudget(limits.max_output_characters)
    for payload in payloads:
        for operation, backend in configured:
            suffix = "ocr" if operation == "ocr" else "visual"
            block, attempt, partial_code, crashed = _invoke_backend(
                backend,
                payload,
                operation=operation,
                allow_external_send=allow_external_send,
                limits=limits,
                output_budget=output_budget,
                block_id=f"frame-{payload.frame_index}-{suffix}",
                external_send_authorizer=external_send_authorizer,
            )
            attempts.append(attempt)
            crashed_attempts += int(crashed)
            if block is not None:
                blocks.append(block)
            if partial_code is not None:
                partial_codes.append(partial_code)
                diagnostic = attempt.get("diagnostic")
                if isinstance(diagnostic, str) and diagnostic:
                    diagnostics.append(
                        f"{operation} backend {attempt['backend']}: {diagnostic}"
                    )

    if not configured:
        partial_codes.append("visual-backends-unconfigured")
        diagnostics.append("no OCR or visual-analysis backend was configured")
    if configured and not blocks and attempts and crashed_attempts == len(attempts):
        return _failed(
            "visual-backends-failed",
            "all configured visual backends failed without usable derived text",
            *diagnostics,
        )

    partial_codes = sorted(set(partial_codes))
    document = ExtractedDocument(
        path=relative_path,
        content_sha256=content_sha256,
        format=format_value,
        extractor=VISUAL_EXTRACTOR_NAME,
        extractor_version=VISUAL_EXTRACTOR_VERSION,
        encoding=None,
        blocks=tuple(blocks),
        metadata={
            "source_size_bytes": len(data),
            "source_kind": "standalone-raster",
            "frame_count_processed": len(payloads),
            "payloads": [_payload_metadata(payload) for payload in payloads],
            "backend_attempts": attempts,
            "external_send_allowed": allow_external_send,
            "partial_reason_codes": partial_codes,
        },
    )
    partial = bool(partial_codes)
    return ExtractionResult(
        status="partial" if partial else "processed",
        reason_code=("visual-raster-partial" if partial else "visual-raster-extracted"),
        reason=(
            "the selected raster was processed with explicit visual follow-up gaps"
            if partial
            else "the selected raster was processed by all configured visual backends"
        ),
        document=document,
        diagnostics=tuple(diagnostics),
    )


def _unconfigured_attempt(
    *, operation: str, page_number: int, payload: VisualPayload | None = None
) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "operation": operation,
        "backend": None,
        "backend_version": None,
        "external_send": None,
        "page_number": page_number,
        "status": "unconfigured",
        "reason_code": f"{operation}-backend-unconfigured",
        "output_characters": 0,
    }
    if payload is not None:
        attempt.update(
            {
                "locator": payload.locator.as_dict(),
                "frame_index": payload.frame_index,
                "image_index": payload.image_index,
                "width": payload.width,
                "height": payload.height,
                "rgba_sha256": payload.rgba_sha256,
                "png_sha256": payload.png_sha256,
            }
        )
    return attempt


def _validated_pdf_render_pixels(
    rendered: object,
    *,
    page_number: int,
    limits: VisualExtractionLimits,
    remaining_pixels: int,
) -> int:
    if not isinstance(rendered, PdfPageRenderOutput):
        raise PdfRendererContractError(
            "PDF page renderer must return PdfPageRenderOutput"
        )
    if len(rendered.payloads) > limits.max_pdf_images_per_page:
        raise PdfRendererContractError(
            f"PDF renderer returned {len(rendered.payloads)} payloads for page "
            f"{page_number}; limit is {limits.max_pdf_images_per_page}"
        )
    seen_indexes: set[int] = set()
    page_pixels = 0
    for payload in rendered.payloads:
        if not isinstance(payload, VisualPayload):
            raise PdfRendererContractError(
                "PDF renderer returned a non-VisualPayload value"
            )
        if not isinstance(payload.locator, PdfPageLocator) or (
            payload.locator.page_number != page_number
        ):
            raise PdfRendererContractError(
                "PDF renderer returned a payload for a different page"
            )
        if payload.source_format != "pdf" or payload.frame_index != 0:
            raise PdfRendererContractError(
                "PDF renderer payloads require source_format 'pdf' and frame_index 0"
            )
        if payload.image_index in seen_indexes:
            raise PdfRendererContractError(
                "PDF renderer payload image_index values must be unique per page"
            )
        seen_indexes.add(payload.image_index)
        pixels = _bounded_pixel_count(payload.width, payload.height, limits=limits)
        page_pixels += pixels
        if page_pixels > remaining_pixels:
            raise PdfRendererContractError(
                f"PDF renderer exceeded the remaining document pixel budget on page "
                f"{page_number}"
            )
    return page_pixels


def _compose_pdf_document(
    native: ExtractedDocument,
    *,
    blocks: list[Block],
    attempts: list[dict[str, Any]],
    renderer_attempts: list[dict[str, Any]],
    payloads: list[VisualPayload],
    followup_pages: list[int],
    ocr_pages: list[int],
    visual_pages: list[int],
    partial_codes: list[str],
    allow_external_send: bool,
) -> ExtractedDocument:
    return ExtractedDocument(
        path=native.path,
        content_sha256=native.content_sha256,
        format="pdf",
        extractor=VISUAL_EXTRACTOR_NAME,
        extractor_version=VISUAL_EXTRACTOR_VERSION,
        encoding=None,
        blocks=tuple([*native.blocks, *blocks]),
        metadata={
            **native.metadata,
            "native_extractor": native.extractor,
            "native_extractor_version": native.extractor_version,
            "native_block_count": len(native.blocks),
            "visual_followup_pages": followup_pages,
            "ocr_followup_pages": ocr_pages,
            "visual_review_followup_pages": visual_pages,
            "visual_payloads": [_payload_metadata(payload) for payload in payloads],
            "renderer_attempts": renderer_attempts,
            "backend_attempts": attempts,
            "external_send_allowed": allow_external_send,
            "visual_partial_reason_codes": sorted(set(partial_codes)),
        },
    )


def _extract_pdf_visual_data(
    data: bytes,
    *,
    relative_path: str,
    ocr_backend: OcrBackend | None,
    visual_backend: VisualAnalysisBackend | None,
    page_renderer: PdfPageRenderer | None,
    allow_external_send: bool,
    limits: VisualExtractionLimits,
    external_send_authorizer: ExternalSendAuthorizer | None = None,
) -> ExtractionResult:
    native_result = extract_pdf_bytes(data, relative_path=relative_path)
    native = native_result.document
    if native is None:
        return native_result

    raw_ocr_pages = native.metadata.get("ocr_recommended_pages", [])
    raw_visual_pages = native.metadata.get("visual_review_recommended_pages", [])
    if not isinstance(raw_ocr_pages, list) or not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 1
        for value in raw_ocr_pages
    ):
        return _failed(
            "pdf-followup-metadata-invalid",
            "C-04 OCR follow-up page metadata is invalid",
        )
    if not isinstance(raw_visual_pages, list) or not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 1
        for value in raw_visual_pages
    ):
        return _failed(
            "pdf-followup-metadata-invalid",
            "C-04 visual-review page metadata is invalid",
        )
    ocr_pages = sorted(set(raw_ocr_pages))
    visual_pages = sorted(set(raw_visual_pages))
    all_followups = sorted(set(ocr_pages) | set(visual_pages))
    if not all_followups:
        document = _compose_pdf_document(
            native,
            blocks=[],
            attempts=[],
            renderer_attempts=[],
            payloads=[],
            followup_pages=[],
            ocr_pages=[],
            visual_pages=[],
            partial_codes=[],
            allow_external_send=allow_external_send,
        )
        return ExtractionResult(
            status="processed",
            reason_code="visual-followup-not-required",
            reason="C-04 did not identify any PDF page requiring OCR or visual review",
            document=document,
        )

    selected_followups = all_followups[: limits.max_pdf_followup_pages]
    partial_codes: list[str] = []
    diagnostics: list[str] = []
    if len(all_followups) > len(selected_followups):
        partial_codes.append("pdf-followup-page-limit")
        diagnostics.append(
            f"C-04 selected {len(all_followups)} follow-up pages; "
            f"only {len(selected_followups)} were processed"
        )

    attempts: list[dict[str, Any]] = []
    renderer_attempts: list[dict[str, Any]] = []
    payloads: list[VisualPayload] = []
    derived_blocks: list[Block] = []
    output_budget = _OutputBudget(limits.max_output_characters)
    text_attempt_statuses: list[str] = []

    total_pdf_pixels = 0
    for page_number in selected_followups:
        remaining_pixels = limits.max_total_pixels - total_pdf_pixels
        if remaining_pixels < 1:
            renderer_attempts.append(
                {
                    "page_number": page_number,
                    "backend": None,
                    "backend_version": None,
                    "external_send": None,
                    "status": "limited",
                    "reason_code": "image-total-pixel-limit",
                    "payload_count": 0,
                    "pixel_count": 0,
                }
            )
            partial_codes.append("image-total-pixel-limit")
            diagnostics.append(
                "the PDF document-wide visual pixel budget was exhausted before "
                f"page {page_number}"
            )
            break
        if page_renderer is None:
            renderer_attempts.append(
                {
                    "page_number": page_number,
                    "backend": None,
                    "backend_version": None,
                    "external_send": None,
                    "status": "unconfigured",
                    "reason_code": "pdf-page-renderer-unconfigured",
                    "payload_count": 0,
                    "pixel_count": 0,
                }
            )
            partial_codes.append("pdf-page-renderer-unconfigured")
            continue
        try:
            renderer_name, renderer_version, renderer_external = _backend_identity(
                page_renderer
            )
            render_page = getattr(page_renderer, "render_page", None)
            if not callable(render_page):
                raise PdfRendererContractError(
                    "PDF page renderer does not implement render_page"
                )
        except VisualExtractionError as exc:
            return _failed(
                "pdf-page-renderer-invalid",
                "the configured PDF page renderer violates the backend contract",
                str(exc),
            )
        except Exception as exc:
            return _failed(
                "pdf-page-renderer-invalid",
                "the configured PDF page renderer violates the backend contract",
                str(exc),
            )
        renderer_attempt: dict[str, Any] = {
            "page_number": page_number,
            "backend": renderer_name,
            "backend_version": renderer_version,
            "external_send": renderer_external,
        }
        if renderer_external and not allow_external_send:
            renderer_attempt.update(
                {
                    "status": "denied",
                    "reason_code": "raw-external-send-denied",
                    "payload_count": 0,
                    "pixel_count": 0,
                }
            )
            renderer_attempts.append(renderer_attempt)
            partial_codes.append("raw-external-send-denied")
            continue

        page_limits = replace(limits, max_total_pixels=remaining_pixels)
        if renderer_external and external_send_authorizer is not None:
            external_send_authorizer()
        try:
            rendered = render_page(
                data,
                page_number=page_number,
                limits=page_limits,
            )
        except PdfRendererContractError as exc:
            return _failed(
                "pdf-page-renderer-contract-violation",
                "the PDF page renderer returned an invalid payload contract",
                str(exc),
            )
        except VisualBackendUnavailable as exc:
            renderer_attempt.update(
                {
                    "status": "unavailable",
                    "reason_code": "pdf-page-renderer-unavailable",
                    "payload_count": 0,
                    "pixel_count": 0,
                    "diagnostic": str(exc)[:1000],
                }
            )
            renderer_attempts.append(renderer_attempt)
            partial_codes.append("pdf-page-renderer-unavailable")
            diagnostics.append(
                f"PDF page renderer {renderer_name} on page {page_number}: {exc}"
            )
            continue
        except Exception as exc:
            renderer_attempt.update(
                {
                    "status": "failed",
                    "reason_code": "pdf-page-renderer-failed",
                    "payload_count": 0,
                    "pixel_count": 0,
                    "diagnostic": str(exc)[:1000],
                }
            )
            renderer_attempts.append(renderer_attempt)
            partial_codes.append("pdf-page-renderer-failed")
            diagnostics.append(
                f"PDF page renderer {renderer_name} on page {page_number}: {exc}"
            )
            continue
        try:
            page_pixels = _validated_pdf_render_pixels(
                rendered,
                page_number=page_number,
                limits=page_limits,
                remaining_pixels=remaining_pixels,
            )
        except PdfRendererContractError as exc:
            return _failed(
                "pdf-page-renderer-contract-violation",
                "the PDF page renderer returned an invalid payload contract",
                str(exc),
            )
        total_pdf_pixels += page_pixels
        renderer_attempt.update(
            {
                "status": "succeeded" if rendered.payloads else "empty",
                "reason_code": (
                    "pdf-page-rendered"
                    if rendered.payloads
                    else "pdf-page-renderer-empty"
                ),
                "payload_count": len(rendered.payloads),
                "pixel_count": page_pixels,
                "document_pixel_count": total_pdf_pixels,
            }
        )
        renderer_attempts.append(renderer_attempt)
        partial_codes.extend(rendered.partial_reason_codes)
        diagnostics.extend(rendered.diagnostics)
        if not rendered.payloads:
            partial_codes.append("pdf-page-renderer-empty")
            continue

        for payload in rendered.payloads:
            payloads.append(payload)
            if page_number in ocr_pages:
                if ocr_backend is None:
                    attempts.append(
                        _unconfigured_attempt(
                            operation="ocr",
                            page_number=page_number,
                            payload=payload,
                        )
                    )
                    partial_codes.append("ocr-backend-unconfigured")
                else:
                    block, attempt, partial_code, _crashed = _invoke_backend(
                        ocr_backend,
                        payload,
                        operation="ocr",
                        allow_external_send=allow_external_send,
                        limits=limits,
                        output_budget=output_budget,
                        block_id=(
                            f"page-{page_number}-image-{payload.image_index}-ocr"
                        ),
                        external_send_authorizer=external_send_authorizer,
                    )
                    attempt["page_number"] = page_number
                    attempts.append(attempt)
                    text_attempt_statuses.append(str(attempt["status"]))
                    if block is not None:
                        derived_blocks.append(block)
                    if partial_code is not None:
                        partial_codes.append(partial_code)
                        diagnostic = attempt.get("diagnostic")
                        if isinstance(diagnostic, str) and diagnostic:
                            diagnostics.append(
                                f"OCR backend {attempt['backend']} on page "
                                f"{page_number}: {diagnostic}"
                            )
            if visual_backend is not None:
                block, attempt, partial_code, _crashed = _invoke_backend(
                    visual_backend,
                    payload,
                    operation="visual-analysis",
                    allow_external_send=allow_external_send,
                    limits=limits,
                    output_budget=output_budget,
                    block_id=(
                        f"page-{page_number}-image-{payload.image_index}-visual"
                    ),
                    external_send_authorizer=external_send_authorizer,
                )
                attempt["page_number"] = page_number
                attempts.append(attempt)
                text_attempt_statuses.append(str(attempt["status"]))
                if block is not None:
                    derived_blocks.append(block)
                if partial_code is not None:
                    partial_codes.append(partial_code)
                    diagnostic = attempt.get("diagnostic")
                    if isinstance(diagnostic, str) and diagnostic:
                        diagnostics.append(
                            f"visual backend {attempt['backend']} on page "
                            f"{page_number}: {diagnostic}"
                        )
            elif page_number in visual_pages:
                attempts.append(
                    _unconfigured_attempt(
                        operation="visual-analysis",
                        page_number=page_number,
                        payload=payload,
                    )
                )
                partial_codes.append("visual-analysis-backend-unconfigured")

    native_has_text = any(block.text.strip() for block in native.blocks)
    if (
        not native_has_text
        and not derived_blocks
        and text_attempt_statuses
        and all(status == "failed" for status in text_attempt_statuses)
    ):
        return _failed(
            "visual-backends-failed",
            "all configured visual text backends failed without usable native or derived text",
            *diagnostics,
        )

    raw_native_partial = native.metadata.get("partial_reasons", [])
    if isinstance(raw_native_partial, list):
        partial_codes.extend(
            str(code)
            for code in raw_native_partial
            if code not in {"page-requires-ocr", "page-requires-visual-review"}
        )
    partial_codes = sorted(set(partial_codes))
    document = _compose_pdf_document(
        native,
        blocks=derived_blocks,
        attempts=attempts,
        renderer_attempts=renderer_attempts,
        payloads=payloads,
        followup_pages=selected_followups,
        ocr_pages=[page for page in ocr_pages if page in selected_followups],
        visual_pages=[page for page in visual_pages if page in selected_followups],
        partial_codes=partial_codes,
        allow_external_send=allow_external_send,
    )
    partial = bool(partial_codes)
    return ExtractionResult(
        status="partial" if partial else "processed",
        reason_code=("visual-pdf-partial" if partial else "visual-pdf-extracted"),
        reason=(
            "C-04 native PDF text was preserved with explicit visual follow-up gaps"
            if partial
            else "C-04 native PDF text was preserved and all selected visual follow-up completed"
        ),
        document=document,
        diagnostics=tuple(diagnostics),
    )


def extract_visual_bytes(
    data: bytes,
    *,
    relative_path: str,
    format_value: str,
    limits: VisualExtractionLimits | None = None,
    ocr_backend: OcrBackend | None | object = _DEFAULT_OCR_BACKEND,
    visual_backend: VisualAnalysisBackend | None = None,
    page_renderer: PdfPageRenderer | None | object = _DEFAULT_PDF_RENDERER,
    allow_external_send: bool = False,
    external_send_authorizer: ExternalSendAuthorizer | None = None,
) -> ExtractionResult:
    """Process one verified byte snapshot without writes or hidden network access."""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    relative_path = _normalized_relative_path(relative_path)
    if format_value not in SUPPORTED_VISUAL_FORMATS:
        return _unsupported(format_value)
    if not isinstance(allow_external_send, bool):
        raise TypeError("allow_external_send must be a boolean")
    if external_send_authorizer is not None and not callable(
        external_send_authorizer
    ):
        raise TypeError("external_send_authorizer must be callable or None")
    chosen_limits = limits or VisualExtractionLimits()
    digest = hashlib.sha256(data).hexdigest()
    if len(data) > chosen_limits.max_source_bytes:
        return _failed(
            "visual-source-byte-limit",
            "the selected visual source exceeds the local byte limit",
            f"source has {len(data)} bytes; limit is {chosen_limits.max_source_bytes}",
            f"content SHA-256 is {digest}",
        )
    chosen_ocr = _chosen_ocr_backend(ocr_backend)
    if format_value == "pdf":
        chosen_renderer: PdfPageRenderer | None
        if page_renderer is _DEFAULT_PDF_RENDERER:
            chosen_renderer = PypdfPageImageRenderer()
        elif page_renderer is None:
            chosen_renderer = None
        else:
            chosen_renderer = page_renderer  # type: ignore[assignment]
        return _extract_pdf_visual_data(
            data,
            relative_path=relative_path,
            ocr_backend=chosen_ocr,
            visual_backend=visual_backend,
            page_renderer=chosen_renderer,
            allow_external_send=allow_external_send,
            limits=chosen_limits,
            external_send_authorizer=external_send_authorizer,
        )
    return _extract_raster_data(
        data,
        relative_path=relative_path,
        format_value=format_value,
        content_sha256=digest,
        ocr_backend=chosen_ocr,
        visual_backend=visual_backend,
        allow_external_send=allow_external_send,
        limits=chosen_limits,
        external_send_authorizer=external_send_authorizer,
    )


def extract_visual_file(
    source_path: str | Path,
    *,
    relative_path: str,
    format_value: str,
    expected_sha256: str | None = None,
    limits: VisualExtractionLimits | None = None,
    ocr_backend: OcrBackend | None | object = _DEFAULT_OCR_BACKEND,
    visual_backend: VisualAnalysisBackend | None = None,
    page_renderer: PdfPageRenderer | None | object = _DEFAULT_PDF_RENDERER,
    allow_external_send: bool = False,
    external_send_authorizer: ExternalSendAuthorizer | None = None,
) -> ExtractionResult:
    """Read one local visual file and process the observed immutable byte snapshot."""

    if format_value not in SUPPORTED_VISUAL_FORMATS:
        return _unsupported(format_value)
    _validate_sha256(expected_sha256)
    chosen_limits = limits or VisualExtractionLimits()
    digest = hashlib.sha256()
    retained = bytearray()
    source_size = 0
    try:
        with Path(source_path).open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                source_size += len(chunk)
                remaining = chosen_limits.max_source_bytes - len(retained)
                if remaining > 0:
                    retained.extend(chunk[:remaining])
    except OSError as exc:
        return _failed(
            "source-read-failed",
            "the selected visual source file could not be read",
            str(exc),
        )
    observed_sha256 = digest.hexdigest()
    if expected_sha256 is not None and observed_sha256 != expected_sha256:
        return _failed(
            "content-hash-mismatch",
            "the visual source no longer matches the requested file version",
            f"expected {expected_sha256}; observed {observed_sha256}",
        )
    if source_size > chosen_limits.max_source_bytes:
        return _failed(
            "visual-source-byte-limit",
            "the selected visual source exceeds the local byte limit",
            f"source has {source_size} bytes; limit is {chosen_limits.max_source_bytes}",
            f"content SHA-256 is {observed_sha256}",
        )
    return extract_visual_bytes(
        bytes(retained),
        relative_path=relative_path,
        format_value=format_value,
        limits=chosen_limits,
        ocr_backend=ocr_backend,
        visual_backend=visual_backend,
        page_renderer=page_renderer,
        allow_external_send=allow_external_send,
        external_send_authorizer=external_send_authorizer,
    )

if __package__:
    from .project_registry import load_registered_project
    from .visual_selection import (
        VisualSelectionRecord,
        VisualSelectionReport,
        build_visual_selection,
    )
else:
    from project_registry import load_registered_project  # type: ignore[no-redef]
    from visual_selection import (  # type: ignore[no-redef]
        VisualSelectionRecord,
        VisualSelectionReport,
        build_visual_selection,
    )


VISUAL_PROJECT_EXECUTION_SCHEMA_VERSION = 1
VISUAL_PROJECT_EXECUTION_KIND = "llmwiki-visual-project-execution"
VISUAL_PROJECT_ITEM_KIND = "llmwiki-visual-project-item"


@dataclass(frozen=True)
class VisualProjectItem:
    """One selected B-07 record and its non-persisted C-05 result."""

    record: VisualSelectionRecord
    extraction: ExtractionResult

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": VISUAL_PROJECT_EXECUTION_SCHEMA_VERSION,
            "kind": VISUAL_PROJECT_ITEM_KIND,
            "record": self.record.as_dict(),
            "extraction": self.extraction.as_dict(),
        }


@dataclass(frozen=True)
class VisualProjectExecution:
    """Current-grounded, selected-only in-memory project execution report."""

    project_id: str
    selection: VisualSelectionReport
    items: tuple[VisualProjectItem, ...]

    def __post_init__(self) -> None:
        if self.project_id != self.selection.project_id:
            raise VisualExtractionError(
                "project execution and visual selection project IDs differ"
            )
        selected = tuple(
            record for record in self.selection.records if record.decision == "selected"
        )
        if tuple(item.record for item in self.items) != selected:
            raise VisualExtractionError(
                "project execution items must exactly match selected records in order"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": VISUAL_PROJECT_EXECUTION_SCHEMA_VERSION,
            "kind": VISUAL_PROJECT_EXECUTION_KIND,
            "project_id": self.project_id,
            "selection": self.selection.as_dict(),
            "items": [item.as_dict() for item in self.items],
        }


@dataclass(frozen=True)
class _VerifiedSourceSnapshot:
    data: bytes | None
    content_sha256: str
    size_bytes: int
    path_signature: tuple[int, int, int, int, int]
    resolved_path: Path


def _path_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_mode),
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_reparse_point(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _safe_project_source_path(project_root: Path, relative_path: str) -> tuple[Path, Path]:
    normalized = _normalized_relative_path(relative_path)
    root = project_root.resolve(strict=True)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    current = root
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise VisualExtractionError(f"project root became unreadable: {exc}") from exc
    if root.is_symlink() or _is_reparse_point(root_stat):
        raise VisualExtractionError("registered project root is a symlink or reparse point")
    for part in PurePosixPath(normalized).parts:
        current = current / part
        try:
            observed = current.lstat()
        except OSError as exc:
            raise VisualExtractionError(
                f"selected source path became unreadable: {normalized}: {exc}"
            ) from exc
        if stat.S_ISLNK(observed.st_mode) or _is_reparse_point(observed):
            raise VisualExtractionError(
                f"selected source uses a symlink or reparse point: {normalized}"
            )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise VisualExtractionError(
            f"selected source could not be resolved: {normalized}: {exc}"
        ) from exc
    if not _is_within(resolved, root):
        raise VisualExtractionError(
            f"selected source resolves outside the registered project: {normalized}"
        )
    return candidate, resolved


def _read_verified_project_source(
    project_root: Path,
    record: VisualSelectionRecord,
    *,
    retain_limit: int,
    expected_signature: tuple[int, int, int, int, int] | None = None,
) -> _VerifiedSourceSnapshot:
    path, resolved = _safe_project_source_path(project_root, record.path)
    try:
        before = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise VisualExtractionError(
            f"selected source could not be stated: {record.path}: {exc}"
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise VisualExtractionError(
            f"selected source is no longer a regular file: {record.path}"
        )
    if before.st_size != record.size_bytes or before.st_mtime_ns != record.mtime_ns:
        raise VisualExtractionError(
            f"selected source changed after inventory: {record.path}; run inventory"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    digest = hashlib.sha256()
    retained = bytearray()
    observed_size = 0
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise VisualExtractionError(
                f"selected source descriptor is not a regular file: {record.path}"
            )
        if _path_signature(opened) != _path_signature(before):
            raise VisualExtractionError(
                f"selected source changed while it was opened: {record.path}"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                observed_size += len(chunk)
                remaining = retain_limit - len(retained)
                if remaining > 0:
                    retained.extend(chunk[:remaining])
            after_descriptor = os.fstat(source.fileno())
    except VisualExtractionError:
        raise
    except OSError as exc:
        raise VisualExtractionError(
            f"selected source could not be read safely: {record.path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        after_path = path.stat(follow_symlinks=False)
        resolved_after = path.resolve(strict=True)
    except OSError as exc:
        raise VisualExtractionError(
            f"selected source changed after reading: {record.path}: {exc}"
        ) from exc
    signature = _path_signature(before)
    if (
        _path_signature(after_descriptor) != signature
        or _path_signature(after_path) != signature
        or resolved_after != resolved
    ):
        raise VisualExtractionError(
            f"selected source changed during verified reading: {record.path}"
        )
    if expected_signature is not None and signature != expected_signature:
        raise VisualExtractionError(
            f"selected source changed across C-05 processing: {record.path}"
        )
    observed_sha256 = digest.hexdigest()
    if observed_size != record.size_bytes or observed_sha256 != record.content_sha256:
        raise VisualExtractionError(
            f"selected source no longer matches the current Manifest: {record.path}"
        )
    return _VerifiedSourceSnapshot(
        data=bytes(retained) if observed_size <= retain_limit else None,
        content_sha256=observed_sha256,
        size_bytes=observed_size,
        path_signature=signature,
        resolved_path=resolved,
    )


def _assert_same_selection(
    expected: VisualSelectionReport,
    workspace_root: str | Path,
    project_id: str,
) -> VisualSelectionReport:
    try:
        current = build_visual_selection(workspace_root, project_id)
    except Exception as exc:
        raise VisualExtractionError(
            "Manifest, scan policy, or B-07 visual authorization changed during C-05"
        ) from exc
    if current != expected:
        raise VisualExtractionError(
            "Manifest, scan policy, or B-07 visual authorization changed during C-05"
        )
    return current


def _assert_same_registration(expected: object, current: object) -> None:
    expected_layout = getattr(expected, "layout", None)
    current_layout = getattr(current, "layout", None)
    if (
        getattr(current, "project_id", None) != getattr(expected, "project_id", None)
        or getattr(current, "project_root", None)
        != getattr(expected, "project_root", None)
        or getattr(current_layout, "machine_root", None)
        != getattr(expected_layout, "machine_root", None)
        or getattr(current_layout, "knowledge_root", None)
        != getattr(expected_layout, "knowledge_root", None)
    ):
        raise VisualExtractionError(
            "project registration changed during C-05 visual execution"
        )


def execute_selected_visuals(
    workspace_root: str | Path,
    project_id: str,
    *,
    limits: VisualExtractionLimits | None = None,
    ocr_backend: OcrBackend | None | object = _DEFAULT_OCR_BACKEND,
    visual_backend: VisualAnalysisBackend | None = None,
    page_renderer: PdfPageRenderer | None | object = _DEFAULT_PDF_RENDERER,
) -> VisualProjectExecution:
    """Execute only currently selected B-07 visual/PDF records, fail closed.

    Deferred and limited records remain report data and are never interpreted as
    backend instructions. Each selected source is reauthorized and re-read
    around backend execution so stale Manifest, policy, priority, and source
    state cannot be returned as a successful current result.
    """

    chosen_limits = limits or VisualExtractionLimits()
    initial_selection = build_visual_selection(workspace_root, project_id)
    selected_records = tuple(
        record
        for record in initial_selection.records
        if record.decision == "selected" and record.deep_read_status == "selected"
    )
    if len(selected_records) != initial_selection.summary.selected_count:
        raise VisualExtractionError(
            "visual selection selected_count does not match executable records"
        )
    initial_registration = load_registered_project(workspace_root, project_id)
    items: list[VisualProjectItem] = []
    for record in selected_records:
        if record.local_content_access != "allowed":
            raise VisualExtractionError(
                f"selected record lacks local raw-content permission: {record.path}"
            )
        _assert_same_selection(initial_selection, workspace_root, project_id)
        registration = load_registered_project(workspace_root, project_id)
        _assert_same_registration(initial_registration, registration)
        snapshot = _read_verified_project_source(
            registration.project_root,
            record,
            retain_limit=chosen_limits.max_source_bytes,
        )
        def authorize_external_send() -> None:
            _assert_same_selection(initial_selection, workspace_root, project_id)
            current_registration = load_registered_project(workspace_root, project_id)
            _assert_same_registration(initial_registration, current_registration)
            _read_verified_project_source(
                current_registration.project_root,
                record,
                retain_limit=0,
                expected_signature=snapshot.path_signature,
            )

        if snapshot.data is None:
            extraction = _failed(
                "visual-source-byte-limit",
                "the selected visual source exceeds the local byte limit",
                f"source has {snapshot.size_bytes} bytes; "
                f"limit is {chosen_limits.max_source_bytes}",
                f"content SHA-256 is {snapshot.content_sha256}",
            )
        else:
            extraction = extract_visual_bytes(
                snapshot.data,
                relative_path=record.path,
                format_value=record.format,
                limits=chosen_limits,
                ocr_backend=ocr_backend,
                visual_backend=visual_backend,
                page_renderer=page_renderer,
                allow_external_send=record.raw_external_send == "allowed",
                external_send_authorizer=authorize_external_send,
            )
        _read_verified_project_source(
            registration.project_root,
            record,
            retain_limit=chosen_limits.max_source_bytes,
            expected_signature=snapshot.path_signature,
        )
        _assert_same_selection(initial_selection, workspace_root, project_id)
        _read_verified_project_source(
            registration.project_root,
            record,
            retain_limit=chosen_limits.max_source_bytes,
            expected_signature=snapshot.path_signature,
        )
        items.append(VisualProjectItem(record=record, extraction=extraction))
    _assert_same_selection(initial_selection, workspace_root, project_id)
    return VisualProjectExecution(
        project_id=initial_selection.project_id,
        selection=initial_selection,
        items=tuple(items),
    )
