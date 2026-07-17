from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from tools import reading_priority as priority_module
from tools import visual_extractor as visual_module
from tools.extraction_schema import (
    ExtractedDocument,
    ImageRegionLocator,
    PdfPageLocator,
    serialize_extraction_result,
)
from tools.visual_extractor import (
    PdfPageRenderOutput,
    TesseractCliOcrBackend,
    VisualBackendOutput,
    VisualExtractionLimits,
    VisualPayload,
    extract_visual_bytes,
    execute_selected_visuals,
    extract_visual_file,
)
from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.reading_priority import generate_reading_priority
from tools.scan_policy import ScanPolicyConfig



class RecordingOcr:
    name = "recording-ocr"
    version = "1"
    external_send = False

    def __init__(self, text: str = "recognized text") -> None:
        self.text = text
        self.calls: list[VisualPayload] = []

    def recognize(
        self, payload: VisualPayload, *, timeout_seconds: int
    ) -> VisualBackendOutput:
        self.calls.append(payload)
        return VisualBackendOutput(self.text, {"timeout": timeout_seconds})


class RecordingVisual:
    name = "recording-visual"
    version = "1"
    external_send = False

    def __init__(self, text: str = "architecture result figure") -> None:
        self.text = text
        self.calls: list[VisualPayload] = []

    def analyze(
        self, payload: VisualPayload, *, timeout_seconds: int
    ) -> VisualBackendOutput:
        self.calls.append(payload)
        return VisualBackendOutput(self.text, {"timeout": timeout_seconds})


class ExternalOcr(RecordingOcr):
    name = "external-ocr"
    external_send = True


class FailingOcr(RecordingOcr):
    name = "failing-ocr"

    def recognize(
        self, payload: VisualPayload, *, timeout_seconds: int
    ) -> VisualBackendOutput:
        self.calls.append(payload)
        raise RuntimeError("backend exploded")


class FakeRenderer:
    name = "fake-renderer"
    version = "1"
    external_send = False

    def __init__(self, payloads: dict[int, tuple[VisualPayload, ...]]) -> None:
        self.payloads = payloads
        self.calls: list[int] = []

    def render_page(
        self,
        data: bytes,
        *,
        page_number: int,
        limits: VisualExtractionLimits,
    ) -> PdfPageRenderOutput:
        self.calls.append(page_number)
        return PdfPageRenderOutput(self.payloads.get(page_number, ()))


class FailingRenderer(FakeRenderer):
    name = "failing-renderer"

    def render_page(
        self,
        data: bytes,
        *,
        page_number: int,
        limits: VisualExtractionLimits,
    ) -> PdfPageRenderOutput:
        self.calls.append(page_number)
        raise RuntimeError("renderer exploded")


class VisualExtractorTests(unittest.TestCase):
    @staticmethod
    def image_bytes(
        *,
        image_format: str = "PNG",
        size: tuple[int, int] = (12, 8),
        frames: int = 1,
    ) -> bytes:
        destination = io.BytesIO()
        images = [
            Image.new("RGB", size, (index * 30, 255 - index * 30, 100))
            for index in range(frames)
        ]
        if frames == 1:
            images[0].save(destination, format=image_format)
        else:
            images[0].save(
                destination,
                format=image_format,
                save_all=True,
                append_images=images[1:],
                duration=10,
                loop=0,
            )
        return destination.getvalue()

    @staticmethod
    def payload(page_number: int, image_index: int = 0) -> VisualPayload:
        image = Image.new("RGBA", (2, 3), "white")
        destination = io.BytesIO()
        image.save(destination, format="PNG")
        return VisualPayload(
            png_bytes=destination.getvalue(),
            rgba_bytes=image.tobytes(),
            width=2,
            height=3,
            locator=PdfPageLocator(page_number),
            frame_index=0,
            image_index=image_index,
            source_format="pdf",
        )

    @staticmethod
    def _pdf_literal(text: str) -> bytes:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return escaped.encode("latin-1")

    @classmethod
    def pdf_bytes(cls, pages: list[dict[str, object]]) -> bytes:
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
            resources = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
            )
            operators: list[bytes] = []
            text = page_spec.get("text")
            if isinstance(text, str):
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
        destination = io.BytesIO()
        writer.write(destination)
        return destination.getvalue()

    def require_document(self, result: object) -> ExtractedDocument:
        document = getattr(result, "document", None)
        self.assertIsInstance(document, ExtractedDocument)
        assert isinstance(document, ExtractedDocument)
        return document

    def test_raster_runs_ocr_and_visual_backends_with_whole_frame_locator(self) -> None:
        data = self.image_bytes()
        ocr = RecordingOcr("OCR")
        visual = RecordingVisual("A model architecture with an accuracy result")
        result = extract_visual_bytes(
            data,
            relative_path="figures/model.png",
            format_value="png",
            ocr_backend=ocr,
            visual_backend=visual,
        )
        self.assertEqual(result.status, "processed")
        document = self.require_document(result)
        self.assertEqual([block.text for block in document.blocks], ["OCR", visual.text])
        self.assertEqual(len(ocr.calls), 1)
        self.assertEqual(len(visual.calls), 1)
        locator = document.blocks[0].locator
        self.assertEqual(locator, ImageRegionLocator(0, 0, 0, 12, 8))
        self.assertEqual(
            document.blocks[0].metadata["rgba_sha256"],
            hashlib.sha256(ocr.calls[0].rgba_bytes).hexdigest(),
        )
        self.assertEqual(
            serialize_extraction_result(result),
            serialize_extraction_result(
                extract_visual_bytes(
                    data,
                    relative_path="figures/model.png",
                    format_value="png",
                    ocr_backend=RecordingOcr("OCR"),
                    visual_backend=RecordingVisual(visual.text),
                )
            ),
        )

    def test_tesseract_unavailable_is_explicit_partial_without_text(self) -> None:
        result = extract_visual_bytes(
            self.image_bytes(),
            relative_path="figure.png",
            format_value="png",
            ocr_backend=TesseractCliOcrBackend(
                executable="llmwiki-definitely-missing-tesseract"
            ),
        )
        self.assertEqual(result.status, "partial")
        document = self.require_document(result)
        self.assertEqual(document.blocks, ())
        attempt = document.metadata["backend_attempts"][0]
        self.assertEqual(attempt["status"], "unavailable")
        self.assertEqual(attempt["reason_code"], "ocr-backend-unavailable")

    def test_external_send_is_independently_denied_without_invocation(self) -> None:
        backend = ExternalOcr()
        result = extract_visual_bytes(
            self.image_bytes(),
            relative_path="private.png",
            format_value="png",
            ocr_backend=backend,
            allow_external_send=False,
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual(backend.calls, [])
        document = self.require_document(result)
        self.assertEqual(
            document.metadata["backend_attempts"][0]["reason_code"],
            "raw-external-send-denied",
        )

    def test_external_send_runs_only_when_independently_allowed(self) -> None:
        backend = ExternalOcr("allowed")
        result = extract_visual_bytes(
            self.image_bytes(),
            relative_path="allowed.png",
            format_value="png",
            ocr_backend=backend,
            allow_external_send=True,
        )
        self.assertEqual(result.status, "processed")
        self.assertEqual(len(backend.calls), 1)

    def test_external_send_authorizer_runs_at_send_boundary(self) -> None:
        backend = ExternalOcr("allowed")
        authorizations: list[str] = []
        result = extract_visual_bytes(
            self.image_bytes(),
            relative_path="allowed.png",
            format_value="png",
            ocr_backend=backend,
            allow_external_send=True,
            external_send_authorizer=lambda: authorizations.append("authorized"),
        )
        self.assertEqual(result.status, "processed")
        self.assertEqual(authorizations, ["authorized"])
        self.assertEqual(len(backend.calls), 1)

    def test_external_send_authorizer_failure_prevents_backend_invocation(self) -> None:
        backend = ExternalOcr()

        def deny() -> None:
            raise visual_module.VisualExtractionError("authorization changed")

        with self.assertRaisesRegex(
            visual_module.VisualExtractionError,
            "authorization changed",
        ):
            extract_visual_bytes(
                self.image_bytes(),
                relative_path="private.png",
                format_value="png",
                ocr_backend=backend,
                allow_external_send=True,
                external_send_authorizer=deny,
            )
        self.assertEqual(backend.calls, [])

    def test_backend_metadata_rejects_nested_non_string_object_keys(self) -> None:
        with self.assertRaisesRegex(TypeError, "keys must be strings"):
            VisualBackendOutput("text", {"nested": {1: "invalid"}})

    def test_malformed_and_dimension_limited_rasters_fail_without_document(self) -> None:
        malformed = extract_visual_bytes(
            b"not an image",
            relative_path="bad.png",
            format_value="png",
            ocr_backend=RecordingOcr(),
        )
        self.assertEqual(malformed.status, "failed")
        self.assertIsNone(malformed.document)
        oversized = extract_visual_bytes(
            self.image_bytes(size=(5, 4)),
            relative_path="wide.png",
            format_value="png",
            limits=VisualExtractionLimits(max_width=4),
            ocr_backend=RecordingOcr(),
        )
        self.assertEqual(oversized.status, "failed")
        self.assertIsNone(oversized.document)

    def test_multiframe_limit_is_partial_and_never_invokes_omitted_frames(self) -> None:
        backend = RecordingOcr()
        result = extract_visual_bytes(
            self.image_bytes(image_format="GIF", frames=3),
            relative_path="animation.gif",
            format_value="gif",
            limits=VisualExtractionLimits(max_frames=2),
            ocr_backend=backend,
        )
        self.assertEqual(result.status, "partial")
        document = self.require_document(result)
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(len(document.blocks), 2)
        self.assertIn("image-frame-limit", document.metadata["partial_reason_codes"])

    def test_oversized_later_frame_retains_earlier_frames_as_partial(self) -> None:
        destination = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(
            destination,
            format="TIFF",
            save_all=True,
            append_images=[Image.new("RGB", (20, 20), "black")],
        )
        backend = RecordingOcr()
        result = extract_visual_bytes(
            destination.getvalue(),
            relative_path="mixed.tiff",
            format_value="tiff",
            limits=VisualExtractionLimits(max_width=10, max_height=10),
            ocr_backend=backend,
        )
        self.assertEqual(result.status, "partial")
        document = self.require_document(result)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(document.metadata["frame_count_processed"], 1)
        self.assertIn(
            "image-frame-processing-failed",
            document.metadata["partial_reason_codes"],
        )

    def test_derived_output_is_bounded_and_marked_truncated(self) -> None:
        result = extract_visual_bytes(
            self.image_bytes(),
            relative_path="long.png",
            format_value="png",
            limits=VisualExtractionLimits(max_output_characters=5),
            ocr_backend=RecordingOcr("abcdefgh"),
        )
        self.assertEqual(result.status, "partial")
        block = self.require_document(result).blocks[0]
        self.assertEqual(block.text, "abcde")
        self.assertTrue(block.truncated)
        self.assertEqual(
            block.truncation_reason_code, "visual-output-character-limit"
        )

    def test_all_configured_text_backends_crashing_fails_without_document(self) -> None:
        result = extract_visual_bytes(
            self.image_bytes(),
            relative_path="broken.png",
            format_value="png",
            ocr_backend=FailingOcr(),
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason_code, "visual-backends-failed")
        self.assertIsNone(result.document)

    def test_source_byte_limit_and_unsupported_format_do_not_run_backends(self) -> None:
        backend = RecordingOcr()
        limited = extract_visual_bytes(
            self.image_bytes(),
            relative_path="too-big.png",
            format_value="png",
            limits=VisualExtractionLimits(max_source_bytes=10),
            ocr_backend=backend,
        )
        self.assertEqual(limited.status, "failed")
        unsupported = extract_visual_bytes(
            b"anything",
            relative_path="figure.svg",
            format_value="svg",
            ocr_backend=backend,
        )
        self.assertEqual(unsupported.status, "unsupported")
        self.assertEqual(backend.calls, [])

    def test_file_hash_mismatch_is_explicit_and_source_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "figure.png"
            data = self.image_bytes()
            path.write_bytes(data)
            before_mode = path.stat().st_mode
            before_mtime = path.stat().st_mtime_ns
            result = extract_visual_file(
                path,
                relative_path="figure.png",
                format_value="png",
                expected_sha256="0" * 64,
                ocr_backend=RecordingOcr(),
            )
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason_code, "content-hash-mismatch")
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mode, before_mode)
            self.assertEqual(path.stat().st_mtime_ns, before_mtime)

    def test_scanned_pdf_runs_only_c04_followup_pages_and_preserves_native_text(self) -> None:
        data = self.pdf_bytes(
            [
                {"image": True},
                {
                    "image": True,
                    "text": "This second page contains enough native research text.",
                },
            ]
        )
        ocr = RecordingOcr("scanned page text")
        result = extract_visual_bytes(
            data,
            relative_path="paper.pdf",
            format_value="pdf",
            ocr_backend=ocr,
        )
        self.assertEqual(result.status, "processed")
        document = self.require_document(result)
        self.assertEqual(len(ocr.calls), 1)
        self.assertEqual(ocr.calls[0].locator, PdfPageLocator(1))
        self.assertEqual(
            document.metadata["visual_followup_pages"],
            [1],
        )
        self.assertIn("enough native research text", document.blocks[1].text)
        self.assertEqual(document.blocks[-1].text, "scanned page text")
        self.assertEqual(document.blocks[-1].locator, PdfPageLocator(1))

    def test_pdf_without_c04_followup_is_processed_without_backend_calls(self) -> None:
        data = self.pdf_bytes(
            [{"text": "This page has enough deterministic native PDF text."}]
        )
        ocr = RecordingOcr()
        renderer = FakeRenderer({1: (self.payload(1),)})
        result = extract_visual_bytes(
            data,
            relative_path="text.pdf",
            format_value="pdf",
            ocr_backend=ocr,
            page_renderer=renderer,
        )
        self.assertEqual(result.status, "processed")
        self.assertEqual(result.reason_code, "visual-followup-not-required")
        self.assertEqual(ocr.calls, [])
        self.assertEqual(renderer.calls, [])
        self.assertIn("enough deterministic", self.require_document(result).blocks[0].text)

    def test_pdf_missing_direct_image_is_explicit_partial_not_fabricated_text(self) -> None:
        data = self.pdf_bytes([{}])
        result = extract_visual_bytes(
            data,
            relative_path="blank.pdf",
            format_value="pdf",
            ocr_backend=RecordingOcr(),
        )
        self.assertEqual(result.status, "partial")
        document = self.require_document(result)
        self.assertEqual(len(document.blocks), 1)
        self.assertEqual(document.blocks[0].text, "")
        self.assertIn(
            "pdf-direct-image-unavailable",
            document.metadata["visual_partial_reason_codes"],
        )

    def test_visual_review_page_uses_injected_visual_backend(self) -> None:
        data = self.pdf_bytes([{}])
        payload = self.payload(1)
        renderer = FakeRenderer({1: (payload,)})
        visual = RecordingVisual("A chart with improving validation accuracy")
        result = extract_visual_bytes(
            data,
            relative_path="result.pdf",
            format_value="pdf",
            ocr_backend=None,
            visual_backend=visual,
            page_renderer=renderer,
        )
        self.assertEqual(result.status, "processed")
        self.assertEqual(renderer.calls, [1])
        self.assertEqual(len(visual.calls), 1)
        document = self.require_document(result)
        self.assertEqual(document.blocks[-1].text, visual.text)
        self.assertEqual(document.blocks[-1].locator, PdfPageLocator(1))

    def test_visual_review_without_backend_is_explicit_partial(self) -> None:
        data = self.pdf_bytes([{}])
        renderer = FakeRenderer({1: (self.payload(1),)})
        result = extract_visual_bytes(
            data,
            relative_path="result.pdf",
            format_value="pdf",
            ocr_backend=None,
            visual_backend=None,
            page_renderer=renderer,
        )
        self.assertEqual(result.status, "partial")
        document = self.require_document(result)
        self.assertIn(
            "visual-analysis-backend-unconfigured",
            document.metadata["visual_partial_reason_codes"],
        )

    def test_pdf_renderer_failure_is_explicit_and_native_blocks_are_preserved(self) -> None:
        data = self.pdf_bytes([{"text": "x", "image": True}])
        renderer = FailingRenderer({})
        result = extract_visual_bytes(
            data,
            relative_path="low-text.pdf",
            format_value="pdf",
            ocr_backend=RecordingOcr(),
            page_renderer=renderer,
        )
        self.assertEqual(result.status, "partial")
        document = self.require_document(result)
        self.assertEqual(document.blocks[0].text.strip(), "x")
        self.assertEqual(renderer.calls, [1])
        self.assertIn(
            "pdf-page-renderer-failed",
            document.metadata["visual_partial_reason_codes"],
        )

    def test_pdf_followup_page_limit_bounds_renderer_invocations(self) -> None:
        data = self.pdf_bytes([{"image": True}, {"image": True}])
        renderer = FakeRenderer({1: (self.payload(1),), 2: (self.payload(2),)})
        ocr = RecordingOcr()
        result = extract_visual_bytes(
            data,
            relative_path="scan.pdf",
            format_value="pdf",
            limits=VisualExtractionLimits(max_pdf_followup_pages=1),
            ocr_backend=ocr,
            page_renderer=renderer,
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual(renderer.calls, [1])
        self.assertEqual(len(ocr.calls), 1)
        self.assertIn(
            "pdf-followup-page-limit",
            self.require_document(result).metadata["visual_partial_reason_codes"],
        )

    def test_pdf_pixel_budget_is_document_wide_and_reduced_per_page(self) -> None:
        class BudgetAwareRenderer(FakeRenderer):
            def __init__(self) -> None:
                super().__init__({})
                self.observed_limits: list[int] = []

            def render_page(
                self,
                data: bytes,
                *,
                page_number: int,
                limits: VisualExtractionLimits,
            ) -> PdfPageRenderOutput:
                self.calls.append(page_number)
                self.observed_limits.append(limits.max_total_pixels)
                payload = VisualExtractorTests.payload(page_number)
                if payload.width * payload.height > limits.max_total_pixels:
                    return PdfPageRenderOutput(
                        (),
                        (f"page {page_number} exceeded the remaining pixel budget",),
                        ("image-total-pixel-limit",),
                    )
                return PdfPageRenderOutput((payload,))

        data = self.pdf_bytes([{"image": True}, {"image": True}])
        renderer = BudgetAwareRenderer()
        ocr = RecordingOcr()
        result = extract_visual_bytes(
            data,
            relative_path="two-page-scan.pdf",
            format_value="pdf",
            limits=VisualExtractionLimits(max_total_pixels=10),
            ocr_backend=ocr,
            page_renderer=renderer,
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual(renderer.calls, [1, 2])
        self.assertEqual(renderer.observed_limits, [10, 4])
        self.assertEqual(len(ocr.calls), 1)
        document = self.require_document(result)
        self.assertEqual(
            sum(item["pixel_count"] for item in document.metadata["visual_payloads"]),
            6,
        )
        self.assertIn(
            "image-total-pixel-limit",
            document.metadata["visual_partial_reason_codes"],
        )

    def test_pdf_renderer_over_budget_payload_fails_contract_closed(self) -> None:
        data = self.pdf_bytes([{"image": True}, {"image": True}])
        renderer = FakeRenderer(
            {1: (self.payload(1),), 2: (self.payload(2),)}
        )
        result = extract_visual_bytes(
            data,
            relative_path="invalid-renderer.pdf",
            format_value="pdf",
            limits=VisualExtractionLimits(max_total_pixels=10),
            ocr_backend=RecordingOcr(),
            page_renderer=renderer,
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason_code, "pdf-page-renderer-contract-violation")
        self.assertIsNone(result.document)

    def test_external_pdf_renderer_authorizer_failure_prevents_invocation(self) -> None:
        class ExternalRenderer(FakeRenderer):
            external_send = True

        renderer = ExternalRenderer({1: (self.payload(1),)})

        def deny() -> None:
            raise visual_module.VisualExtractionError("authorization changed")

        with self.assertRaisesRegex(
            visual_module.VisualExtractionError,
            "authorization changed",
        ):
            extract_visual_bytes(
                self.pdf_bytes([{"image": True}]),
                relative_path="private.pdf",
                format_value="pdf",
                ocr_backend=RecordingOcr(),
                page_renderer=renderer,
                allow_external_send=True,
                external_send_authorizer=deny,
            )
        self.assertEqual(renderer.calls, [])

    def test_external_pdf_renderer_is_denied_before_invocation(self) -> None:
        class ExternalRenderer(FakeRenderer):
            external_send = True

        data = self.pdf_bytes([{"image": True}])
        renderer = ExternalRenderer({1: (self.payload(1),)})
        result = extract_visual_bytes(
            data,
            relative_path="private.pdf",
            format_value="pdf",
            ocr_backend=RecordingOcr(),
            page_renderer=renderer,
            allow_external_send=False,
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual(renderer.calls, [])
        self.assertIn(
            "raw-external-send-denied",
            self.require_document(result).metadata["visual_partial_reason_codes"],
        )


class VisualProjectExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"

    @staticmethod
    def _png(color: str = "white") -> bytes:
        destination = io.BytesIO()
        Image.new("RGB", (4, 4), color).save(destination, format="PNG")
        return destination.getvalue()

    def _registered_project(
        self,
        name: str,
        *,
        image_paths: tuple[str, ...],
        references: tuple[str, ...] = (),
        policy_config: ScanPolicyConfig | None = None,
    ) -> tuple[Path, object]:
        project = self.root / name
        project.mkdir(parents=True)
        for index, relative_path in enumerate(image_paths):
            path = project.joinpath(*relative_path.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self._png("white" if index % 2 == 0 else "black"))
        links = "\n".join(f"![fixture]({path})" for path in references)
        (project / "README.md").write_text(
            f"# {name}\n\n{links}\n",
            encoding="utf-8",
            newline="\n",
        )
        registration = register_project(
            self.workspace,
            project,
            project_id=name,
        )
        inventory_project(self.workspace, name, policy_config=policy_config)
        generate_reading_priority(self.workspace, name)
        return project, registration

    def test_selected_architecture_and_result_figures_are_deep_read(self) -> None:
        paths = ("figures/architecture.png", "figures/result.png")
        _project, _registration = self._registered_project(
            "selected-figures",
            image_paths=paths,
            references=paths,
        )
        visual = RecordingVisual("grounded architecture/result analysis")
        execution = execute_selected_visuals(
            self.workspace,
            "selected-figures",
            ocr_backend=None,
            visual_backend=visual,
        )
        self.assertEqual([item.record.path for item in execution.items], list(paths))
        self.assertTrue(
            all(item.extraction.status == "processed" for item in execution.items)
        )
        self.assertEqual(len(visual.calls), 2)
        self.assertEqual(
            execution.as_dict()["schema_version"],
            1,
        )

    def test_deferred_visual_records_never_invoke_backends(self) -> None:
        paths = ("figures/a.png", "figures/b.png")
        project = self.root / "deferred"
        project.mkdir(parents=True)
        (project / "figures").mkdir()
        for path in paths:
            project.joinpath(*path.split("/")).write_bytes(self._png())
        (project / "README.md").write_text("# Deferred\n", encoding="utf-8")
        register_project(self.workspace, project, project_id="deferred")
        inventory_project(self.workspace, "deferred")
        visual = RecordingVisual()
        with patch.object(priority_module, "DEEP_READ_MAX_FILES", 2):
            generate_reading_priority(self.workspace, "deferred")
            execution = execute_selected_visuals(
                self.workspace,
                "deferred",
                ocr_backend=None,
                visual_backend=visual,
            )
        self.assertEqual(execution.selection.summary.selected_count, 1)
        self.assertEqual(execution.selection.summary.deferred_count, 1)
        self.assertEqual(len(execution.items), 1)
        self.assertEqual(len(visual.calls), 1)

    def test_large_training_image_collection_is_limited_without_bulk_calls(self) -> None:
        paths = tuple(f"data/train/image-{index:02}.png" for index in range(32))
        self._registered_project(
            "training-images",
            image_paths=paths,
        )
        visual = RecordingVisual()
        execution = execute_selected_visuals(
            self.workspace,
            "training-images",
            ocr_backend=None,
            visual_backend=visual,
        )
        self.assertEqual(execution.items, ())
        self.assertEqual(execution.selection.summary.limited_count, 32)
        self.assertEqual(visual.calls, [])

    def test_project_policy_denies_external_backend_independently(self) -> None:
        paths = ("figures/private.png",)
        self._registered_project(
            "external-denied",
            image_paths=paths,
            references=paths,
        )

        class ExternalVisual(RecordingVisual):
            external_send = True

        visual = ExternalVisual()
        execution = execute_selected_visuals(
            self.workspace,
            "external-denied",
            ocr_backend=None,
            visual_backend=visual,
        )
        self.assertEqual(visual.calls, [])
        extraction = execution.items[0].extraction
        self.assertEqual(extraction.status, "partial")
        self.assertEqual(
            self.require_attempt(extraction)["reason_code"],
            "raw-external-send-denied",
        )

    @staticmethod
    def require_attempt(extraction: object) -> dict[str, object]:
        document = getattr(extraction, "document", None)
        assert isinstance(document, ExtractedDocument)
        attempt = document.metadata["backend_attempts"][0]
        assert isinstance(attempt, dict)
        return attempt

    def test_policy_change_at_external_send_boundary_prevents_invocation(self) -> None:
        paths = ("figures/external.png",)
        project, _registration = self._registered_project(
            "external-policy-change",
            image_paths=paths,
            references=paths,
            policy_config=ScanPolicyConfig(external_send_mode="safe"),
        )

        class MutatingExternalVisual:
            name = "mutating-external-visual"
            version = "1"

            def __init__(self) -> None:
                self.calls: list[VisualPayload] = []

            @property
            def external_send(self) -> bool:
                (project / ".llmwikiignore").write_text(
                    "figures/\n",
                    encoding="utf-8",
                    newline="\n",
                )
                return True

            def analyze(
                self,
                payload: VisualPayload,
                *,
                timeout_seconds: int,
            ) -> VisualBackendOutput:
                self.calls.append(payload)
                return VisualBackendOutput("must not run")

        visual = MutatingExternalVisual()
        with self.assertRaisesRegex(
            visual_module.VisualExtractionError,
            "changed",
        ):
            execute_selected_visuals(
                self.workspace,
                "external-policy-change",
                ocr_backend=None,
                visual_backend=visual,
            )
        self.assertEqual(visual.calls, [])

    def test_source_mutation_during_backend_execution_fails_closed(self) -> None:
        paths = ("figures/mutable.png",)
        project, _registration = self._registered_project(
            "source-mutation",
            image_paths=paths,
            references=paths,
        )
        source = project / "figures" / "mutable.png"

        class MutatingVisual(RecordingVisual):
            def analyze(
                inner_self,
                payload: VisualPayload,
                *,
                timeout_seconds: int,
            ) -> VisualBackendOutput:
                inner_self.calls.append(payload)
                source.write_bytes(VisualProjectExecutionTests._png("red"))
                return VisualBackendOutput("result")

        with self.assertRaisesRegex(
            visual_module.VisualExtractionError,
            "changed",
        ):
            execute_selected_visuals(
                self.workspace,
                "source-mutation",
                ocr_backend=None,
                visual_backend=MutatingVisual(),
            )

    def test_manifest_mutation_during_backend_execution_fails_closed(self) -> None:
        paths = ("figures/manifest.png",)
        _project, registration = self._registered_project(
            "manifest-mutation",
            image_paths=paths,
            references=paths,
        )
        manifest_file = registration.layout.manifest_file

        class MutatingVisual(RecordingVisual):
            def analyze(
                inner_self,
                payload: VisualPayload,
                *,
                timeout_seconds: int,
            ) -> VisualBackendOutput:
                inner_self.calls.append(payload)
                manifest_file.write_bytes(manifest_file.read_bytes() + b"\n")
                return VisualBackendOutput("result")

        with self.assertRaises(Exception):
            execute_selected_visuals(
                self.workspace,
                "manifest-mutation",
                ocr_backend=None,
                visual_backend=MutatingVisual(),
            )

    def test_priority_mutation_during_backend_execution_fails_closed(self) -> None:
        paths = ("figures/priority.png",)
        _project, registration = self._registered_project(
            "priority-mutation",
            image_paths=paths,
            references=paths,
        )
        priority_file = registration.layout.reading_priority_file

        class MutatingVisual(RecordingVisual):
            def analyze(
                inner_self,
                payload: VisualPayload,
                *,
                timeout_seconds: int,
            ) -> VisualBackendOutput:
                inner_self.calls.append(payload)
                payload = json.loads(priority_file.read_text(encoding="utf-8"))
                payload["files"][0]["priority_score"] += 1
                priority_file.write_text(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                return VisualBackendOutput("result")

        with self.assertRaises(Exception):
            execute_selected_visuals(
                self.workspace,
                "priority-mutation",
                ocr_backend=None,
                visual_backend=MutatingVisual(),
            )

    def test_policy_mutation_during_backend_execution_fails_closed(self) -> None:
        paths = ("figures/policy.png",)
        project, _registration = self._registered_project(
            "policy-mutation",
            image_paths=paths,
            references=paths,
        )

        class MutatingVisual(RecordingVisual):
            def analyze(
                inner_self,
                payload: VisualPayload,
                *,
                timeout_seconds: int,
            ) -> VisualBackendOutput:
                inner_self.calls.append(payload)
                (project / ".llmwikiignore").write_text(
                    "figures/\n", encoding="utf-8", newline="\n"
                )
                return VisualBackendOutput("result")

        with self.assertRaises(Exception):
            execute_selected_visuals(
                self.workspace,
                "policy-mutation",
                ocr_backend=None,
                visual_backend=MutatingVisual(),
            )


if __name__ == "__main__":
    unittest.main()
