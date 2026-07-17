# C-08 Deterministic Locator-Preserving Chunking

## Purpose

C-08 divides an existing C-01 `ExtractedDocument` into bounded chunks without
losing the coordinates needed to reopen the extracted source. It is a pure,
deterministic transformation: it does not open files, infer semantic topics,
assign source identities, or create Evidence.

Implementation: `tools/chunking.py`.

## Versioned records

Every persisted chunk and chunked-document record carries
`schema_version: 1` and an exact kind:

| Record | Kind |
|---|---|
| Chunk | `llmwiki-locator-chunk` |
| Chunked document | `llmwiki-chunked-document` |
| Boundary snapshot | `llmwiki-chunk-boundary-snapshot` |

The JSON reader requires integer schema versions and rejects missing, boolean, or
future versions, missing or extra fields,
wrong kinds, duplicate keys, non-finite numbers, malformed nested C-01
locators, inconsistent hashes, and non-contiguous ordering. Stable serializers
use UTF-8, sorted keys, and a final newline.

## Boundary rules

`ChunkingLimits.max_chunk_utf8_bytes` is a positive byte bound. Chunking uses
UTF-8 byte length rather than Python character count so the same Unicode input
always produces the same boundaries.

- `line_range`, `section`, and `symbol` blocks may split only between complete
  source lines. Each emitted locator has adjusted inclusive line bounds.
- Section chunks retain the complete heading path.
- Symbol chunks retain the exact symbol name.
- `pdf_page`, `image_region`, `notebook_cell`, `paragraph`, `slide`, and
  `table_range` locators are atomic. C-08 does not claim a smaller coordinate
  unless an extractor can
  prove one; therefore an oversized atomic block is rejected.
- In particular, C-08 never slices OCR or vision text at arbitrary character
  offsets while retaining the same `image_region`. A producer must supply a
  truthful smaller region locator or a block that fits the configured byte
  limit.
- Empty page, Notebook-cell, paragraph, slide, and table blocks remain one
  empty, located chunk.
- An empty line-oriented block is rejected because a positive line locator cannot
  truthfully identify zero source lines; it is never silently omitted.
- A single source line larger than the byte limit is rejected instead of being
  cut at an arbitrary character offset.
- A line-oriented block is rejected when its text line count differs from its
  locator span.
- Unknown locator variants fail closed.

This first table implementation deliberately treats the complete C-01 A1 range
as atomic. A later table extractor may add exact cell-range subdivisions, but
C-08 never guesses them from rendered text. Likewise, an `image_region` remains
one atomic chunk because its pixel rectangle does not encode trustworthy text
subdivision boundaries.

## Exact coverage proof

Each chunk records:

- its source block index and ID;
- an exact chunk locator and the original source-block locator;
- half-open UTF-8 byte boundaries within the source block;
- hashes for the chunk text and complete source-block text;
- the source block type, metadata, and truncation fields.

`ChunkedDocument` construction verifies that block and chunk indexes are
ordered and contiguous, byte ranges have no gaps or overlaps, concatenated
chunk text matches the recorded source-block hash, and locators cover the
complete original range. `validate_chunk_coverage()` additionally compares the
result to the supplied `ExtractedDocument` and proves that concatenating chunks
reproduces every `Block.text` exactly while preserving its type, locator,
metadata, and truncation state.

The chunked document carries a SHA-256 of the canonical C-01 document record.
This is an extraction-record fingerprint only; it is not the D-stage
`source_id` or Evidence identity.

## Boundary snapshot

`serialize_boundary_snapshot()` emits canonical UTF-8 JSON containing only the
source record fingerprint, configured byte limit, ordered byte boundaries,
chunk hashes, and chunk locators. `boundary_snapshot_sha256` is embedded in the
chunked-document record and checked on load. Golden tests lock this snapshot
hash so changes to boundary behavior are explicit and reviewable.

## Explicit non-goals

C-08 does not add format extraction, semantic summarization, source IDs, source
versions, Evidence, source reopening, relocation recovery, Manifest
scheduling, MCP, Hooks, Web access, LLM calls, or writes to a scanned source
project.
