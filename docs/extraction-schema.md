# C-01 Extraction Schema

## Purpose

C-01 defines the versioned in-memory and JSON contract shared by deterministic
extractors. It does not open source files or perform extraction. The contract
keeps extracted content uniform enough for later chunking and Evidence while
retaining a locator that can reopen the original file version.

Implementation: `tools/extraction_schema.py`.

## Versioned records

Every persisted object carries `schema_version: 1` and an exact `kind`:

| Object | Kind |
|---|---|
| Locator | `llmwiki-locator` |
| Block | `llmwiki-extracted-block` |
| ExtractedDocument | `llmwiki-extracted-document` |
| ExtractionResult | `llmwiki-extraction-result` |

Missing fields, extra fields, legacy/missing versions, future versions, wrong
kinds, duplicate JSON keys, non-finite numbers, and malformed nested records
fail closed.

## Locator union

Locators use exact, format-appropriate coordinates:

- `line_range`: inclusive one-based `start_line` and `end_line`;
- `pdf_page`: one-based `page_number`;
- `image_region`: zero-based `frame_index`, zero-based top-left pixel `x` and
  `y`, and positive pixel `width` and `height`;
- `notebook_cell`: zero-based `cell_index` and optional non-empty `cell_id`;
- `paragraph`: zero-based `paragraph_index` in the DOCX main document body;
- `slide`: one-based `slide_number` in presentation order;
- `table_range`: sheet name and inclusive uppercase A1 cell range;
- `section`: non-empty heading path plus inclusive source lines;
- `symbol`: symbol name plus inclusive source lines.

Ranges reject zero, negative, reversed, or structurally invalid coordinates.
For `image_region`, `frame_index`, `x`, and `y` must be non-negative integers,
and `width` and `height` must be positive integers. Pixel coordinates use a
zero-based top-left origin in the Pillow-decoded frame **after**
`ImageOps.exif_transpose()` orientation normalization. The locator schema can
validate coordinate shape but not source-specific bounds; D-04 reopening checks
that the selected frame exists and that the complete rectangle is inside the
normalized frame. First-version C-05 producers use whole-frame rectangles, but
Schema v1 deliberately supports any in-bounds rectangle without inventing text
line or PDF-page coordinates.

Paragraph and slide locators reject negative, boolean, and zero (for slides)
coordinates. They are atomic source positions: the paragraph index is defined by
document-order `w:p` elements under the DOCX main body, and the slide number is
defined by the PPTX presentation relationship order. Section and symbol locators
retain line bounds so later chunking never replaces reopenable coordinates with
an ungrounded label.

## Standalone raster reopening

D-04 `source_access` reopens an `image_region` only from the already
content-hash-verified source bytes. It selects the zero-based frame, applies
EXIF orientation normalization, validates the rectangle, crops it, converts the
crop to RGBA, and hashes the raw row-major RGBA bytes with SHA-256. The returned
UTF-8 excerpt has format `image-region-rgba-sha256` and is canonical compact
JSON containing exactly:

```text
frame_index, x, y, width, height,
decoded_width, decoded_height, rgba_sha256
```

No image bytes, OCR text, or vision description are returned. Pillow
malformation and decompression-bomb failures are format errors; nonexistent
frames and out-of-bounds rectangles are locator errors. Reopening does not
change Pillow's process-wide safety settings and never mutates the source file.

This contract verifies the selected standalone raster pixels. It does **not**
verify derived OCR or vision text, and it does not represent a PDF crop. PDF
content continues to use `pdf_page` unless a separate future PDF-region schema
is explicitly introduced.

## Block

A `Block` contains:

- a document-unique `block_id`;
- a bounded `block_type` (`text`, `code`, `markdown`, `table`,
  `output_summary`, or `metadata`);
- text, one Locator, and JSON-only metadata;
- an explicit `truncated` flag;
- a stable truncation reason code and human reason when truncated.

A non-truncated block cannot carry truncation reasons, and a truncated block
cannot omit them. Empty text is allowed so later PDF handling can represent a
page that was inspected but had no extractable text without inventing content.

## ExtractedDocument

An `ExtractedDocument` describes one Manifest file version using:

- normalized project-relative POSIX `path`;
- lowercase SHA-256 `content_sha256`;
- deterministic format and extractor identifiers;
- extractor version and optional detected encoding;
- zero or more uniquely identified Blocks;
- JSON-only document metadata.

It deliberately does not contain `source_id` or Evidence; those identities are
introduced by the D tasks.

## ExtractionResult

An `ExtractionResult` has one status:

```text
processed | partial | failed | unsupported
```

`processed` and `partial` require an `ExtractedDocument`. `failed` and
`unsupported` require `document: null`, preventing a failed extractor from
fabricating text. Every result carries a stable reason code, human reason, and
optional ordered diagnostics.

`serialize_extraction_result()` emits stable UTF-8-compatible JSON with sorted
keys, two-space indentation, and one final newline.
`deserialize_extraction_result()` performs strict recursive validation and
round-trips every supported Locator.

## Explicit non-goals

C-01 does not implement text, Notebook, PDF, or raster extraction; does not write
`extracted/` artifacts; does not chunk content; does not assign `source_id` or
Evidence; and does not add MCP, Hook, Web, LLM, or curated-knowledge behavior.
