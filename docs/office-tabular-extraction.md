# Office and tabular extraction (C-06)

C-06 adds deterministic, source-read-only extraction and exact source reopening
for CSV, TSV, XLSX, DOCX, and PPTX. The implementation lives in:

- `tools/office_tabular_extractor.py` — bounded format parsing and locator-driven
  reopening;
- `tools/extraction_schema.py` — strict Schema v1 paragraph and slide locators;
- `tools/source_access.py` — current-source reopening after source identity and
  content-hash verification;
- `tools/chunking.py` — atomic preservation of table, paragraph, and slide
  coordinates.

The extractor does not persist `extracted/` records, create Evidence, mutate a
Manifest, write curated Markdown, call an LLM or network service, or write to a
registered research project.

## Supported formats and local processing

The accepted format codes are exactly:

```text
csv, tsv, xlsx, docx, pptx
```

CSV and TSV are decoded and parsed with a strict local delimiter parser. XLSX
is opened locally with `openpyxl` in read-only, formula-preserving mode. DOCX
and PPTX are parsed directly from bounded OOXML ZIP members with the Python
standard library; no Office application, conversion service, macro execution,
external relationship fetch, or implicit package download is used.

`extract_office_tabular_bytes(...)` handles an immutable byte snapshot.
`extract_office_tabular_file(...)` hashes the complete source, supports an
expected SHA-256 precondition, and never opens the file for writing. Both return
the existing C-01 `ExtractionResult` union. Unsupported formats, unreadable or
malformed sources, and bounded partial results remain explicit; failed results
never fabricate an `ExtractedDocument`.

## Locator contracts

### CSV, TSV, and XLSX

Each table block uses `TableRangeLocator(sheet, start_cell, end_cell)`. Cell
coordinates are uppercase inclusive A1 ranges. XLSX retains the workbook's exact
sheet name; delimited files use the reserved logical sheet names `CSV` and
`TSV`. Blocks contain canonical compact JSON matrices (`table-json-matrix-v1`)
so the same current range can be reopened without depending on a rendered
Markdown table. Short delimited rows retain the distinction between an absent
cell and an explicitly empty field. XLSX formulas are retained rather than
executed. Date/time-like values use tagged JSON values rather than locale-
dependent display strings.

### DOCX

`ParagraphLocator(paragraph_index)` is zero-based. The index is the stable
document-order position of a `w:p` element under `word/document.xml`'s main
body, including paragraphs nested in body tables. Text is derived only from the
selected paragraph's local XML content; tabs and explicit line breaks are
represented as text control characters. Headers, footers, comments, embedded objects, and macros are not silently
merged into the main-body coordinate space. Text present inside main-body
tracked-change XML follows the same deterministic document order; C-06 does not
attempt to accept or reject revisions semantically.

### PPTX

`SlideLocator(slide_number)` is one-based. Slide order comes from
`ppt/presentation.xml` and its internal relationship table, not filename
lexicographic order. The extractor reads text in each selected slide part and
keeps paragraph boundaries deterministic. External relationships are never
opened. Notes, media payloads, embedded objects, and macros are not represented
as slide text.

Paragraph and slide blocks are atomic under C-08. Table blocks are already
emitted as bounded exact ranges and are also atomic. If one atomic block exceeds
the configured chunk byte limit, chunking rejects the block rather than slicing
text while pretending the original locator became more precise.

## Bounded parsing and partial states

`OfficeTabularExtractionLimits` validates every configured limit as a positive
non-boolean integer. The implementation bounds source bytes, decoded text,
rows, columns, cells, table rows per block, cell text, sheets, paragraphs,
slides, paragraph/slide block text, ZIP member count, individual member
expansion, and total archive expansion. OOXML parsing
rejects duplicate or unsafe member names, encrypted members, non-regular ZIP
entries, external slide targets, DTD/entity declarations, missing required
parts, malformed XML, and archive expansion beyond the configured limits.

When a deterministic content limit is reached after safe content has already
been extracted, the result is `partial`; retained blocks carry explicit
truncation metadata where their own text was shortened. A source that cannot be
parsed safely enough to establish its coordinates returns `failed` without a
document. Diagnostics and stable reason codes state which boundary was reached;
limits are never presented as successful full extraction.

## Exact reopening

`reopen_office_locator_bytes(...)` parses the requested current byte snapshot
by locator instead of relying on the block grouping chosen during an earlier
extraction. It supports:

- an arbitrary in-bounds CSV/TSV/XLSX `TableRangeLocator`;
- one exact DOCX `ParagraphLocator`;
- one exact PPTX `SlideLocator`.

Malformed source data and invalid source coordinates use separate exception
classes. `source_access.open_source(...)` maps them to `SourceFormatError` and
`SourceLocatorError`, respectively, after the normal project boundary, current
path, content-policy, source-version, and SHA-256 checks. Reopened table text
uses `table-json-matrix-v1`; paragraph and slide text use
`docx-paragraph-text` and `pptx-slide-text`. Evidence can therefore hash the
exact current excerpt without trusting a stale conversion artifact.

## Explicit non-goals

C-06 does not add extraction persistence, adaptive execution scheduling,
Evidence registration, semantic summaries, formula calculation, Office macro
execution, OCR for embedded Office images, external conversion, MCP operations,
Web behavior, or curated knowledge writes. Those remain responsibilities of
later orchestrator, Evidence, synthesis, and rendering tasks.
