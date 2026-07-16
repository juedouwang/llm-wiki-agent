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
- `notebook_cell`: zero-based `cell_index` and optional non-empty `cell_id`;
- `table_range`: sheet name and inclusive uppercase A1 cell range;
- `section`: non-empty heading path plus inclusive source lines;
- `symbol`: symbol name plus inclusive source lines.

Ranges reject zero, negative, reversed, or structurally invalid coordinates.
Section and symbol locators retain line bounds so later chunking never replaces
reopenable coordinates with an ungrounded label.

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

C-01 does not implement text, Notebook, or PDF extraction; does not write
`extracted/` artifacts; does not chunk content; does not assign `source_id` or
Evidence; and does not add MCP, Hook, Web, LLM, or curated-knowledge behavior.
