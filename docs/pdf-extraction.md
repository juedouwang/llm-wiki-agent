# Deterministic PDF extraction (C-04)

C-04 adds local, deterministic page-level text extraction for PDF files classified by B-05. It uses the explicitly declared `pypdf` backend and never runs OCR, executes embedded content, loads external resources, calls an LLM, or writes into the source project.

## API

```python
from tools.pdf_extractor import extract_pdf_file

result = extract_pdf_file(
    source_path,
    relative_path="papers/result.pdf",
    expected_sha256=manifest_hash,
)
```

`extract_pdf_bytes(...)` provides the same C-01 `ExtractionResult` contract for already-loaded bytes. Unsupported formats are rejected before a source path is opened. The file API streams the complete source locally to calculate or verify its SHA-256 and never changes source bytes, timestamps, or directory entries.

## Page block contract

- Every PDF page emits one `text` block in page order, including pages with no extractable text.
- Every block uses `PdfPageLocator(page_number)` with an exact one-based page number and a stable `page-<number>` block ID.
- Block text is the deterministic text returned for that page by the pinned major version of `pypdf`. C-04 does not reorder pages or merge page text.
- Page metadata records the full extracted-text character count and SHA-256, non-whitespace character count, direct image XObject count, page assessment, follow-up flags, page dimensions, rotation, and any content-limit record.
- Document metadata records source size, PDF header, the exact runtime `pypdf` backend version, page count, metadata keys, bounded basic metadata, assessment counts, and explicit page lists for images, OCR, visual review, and truncation.

Concatenating blocks is not presented as a reconstruction of PDF layout: PDF text extraction is a rendering interpretation. The retained page locator and full page-text hash are the C-04 fidelity boundary. D-stage Evidence and reopening later verify against the same source version.

## Scanned and low-text detection

C-04 never invents text for a page. Each page receives one deterministic assessment:

| Assessment | Rule | Follow-up |
|---|---|---|
| `text` | Non-whitespace text meets the configured threshold. | None. |
| `low_text` | Some text exists but is below the threshold. | OCR recommended. |
| `likely_scanned` | No text is extractable and at least one direct image XObject exists. | OCR recommended. |
| `no_text` | No text and no direct image XObject are observed. | Visual review recommended; the page may be blank, vector-only, or otherwise non-text. |

A document containing any page that requires OCR or visual review returns `status=partial`; the empty page block still preserves the exact page locator. Image payloads are not decoded or copied into extracted output. This heuristic intentionally does not claim that every nested-form or inline image is detected; C-05 owns OCR and visual understanding.

## Deterministic limits

`PdfExtractionLimits` bounds source bytes, page count, characters per page, basic metadata characters, the low-text threshold, and read chunk size.

- A page exceeding `max_page_characters` is shortened, its block uses `page-character-limit`, and the full extracted-text count/hash remain in metadata.
- Basic metadata exceeding `max_metadata_characters` is shortened and reported with `metadata-character-limit`.
- OCR and visual-review requirements use document-level partial reasons `page-requires-ocr` and `page-requires-visual-review`.
- A PDF exceeding `max_source_bytes` or `max_pages` fails without a document; C-04 does not parse a source prefix or silently omit later pages.

Malformed PDFs, unreadable page trees, page extraction errors, read failures, and hash mismatches return `failed` without a document. Encrypted PDFs return `unsupported` because C-04 has no password or credential contract.

## Scope boundary

C-04 does not implement C-05 OCR/vision, C-08 chunking, PDF-region coordinates, semantic section recovery, source identity, Evidence, source reopening or relocation, Manifest scheduling, MCP, Hook, Web, or LLM behavior. It does not replace the older raw Markdown conversion workflow.
