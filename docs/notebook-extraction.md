# Deterministic Notebook extraction (C-03)

C-03 adds a local, deterministic extractor for Jupyter Notebook files classified by B-05. It parses Notebook JSON without executing cells, loading external resources, calling an LLM, or sending source content outside the machine.

## API

```python
from tools.notebook_extractor import extract_notebook_file

result = extract_notebook_file(
    source_path,
    relative_path="notebooks/experiment.ipynb",
    expected_sha256=manifest_hash,
)
```

`extract_notebook_bytes(...)` provides the same extraction contract for already-loaded bytes. Both APIs return the strict C-01 `ExtractionResult` Schema and never write extracted state or mutate the source project.

## Cell and block contract

- Only Notebook major version 4 is supported. Other major versions return `unsupported` without a document.
- Each supported `markdown`, `code`, or `raw` cell emits exactly one source block in Notebook order.
- Every source block uses `NotebookCellLocator(cell_index, cell_id)`, where `cell_index` is zero-based and `cell_id` is retained when present. Duplicate or malformed IDs fail closed.
- Source block metadata records the cell type, execution count for code cells, source character count and SHA-256, metadata keys, and attachment names/MIME types without reproducing attachment payloads.
- Code cells with outputs emit a second `output_summary` block using the same cell locator. Its metadata preserves the execution count, total and included output counts, full rendered-summary character count and SHA-256, and stable truncation records.
- Notebook-level metadata records `nbformat`, `nbformat_minor`, Notebook metadata keys, cell/type counts, block counts, source byte size, and aggregate truncation reason codes.

Changing one cell leaves the serialized source blocks for all other cells unchanged. Block IDs are derived from the zero-based cell index (`cell-<index>-source` and `cell-<index>-outputs`). C-03 does not claim identity across cell insertion or reordering; persistent source identity belongs to the D stage.

## Output summaries and payload omission

Stream text, error values/tracebacks, and textual MIME values receive bounded previews plus exact character counts and SHA-256 hashes. Non-text MIME payloads, including image formats and base64-encoded binary data, are never copied into the summary; only their MIME type, character count, and SHA-256 are retained. Attachment payloads are likewise omitted.

Output summaries normally contain stable, pretty-printed JSON text. If `max_output_summary_characters` is reached, the block text is a bounded prefix of that rendered summary and is not guaranteed to remain valid JSON. Consumers must treat `Block.text` as summary text and use the block truncation fields and `full_summary_sha256` rather than assuming a nested JSON storage contract.

## Deterministic limits

`NotebookExtractionLimits` bounds source bytes, cell characters, textual output previews, rendered output-summary characters, outputs per cell, MIME entries per output, and file read chunks. Applied limits return `status=partial` and use these stable reason codes:

| Code | Meaning |
|---|---|
| `cell-character-limit` | A cell source exceeded `max_cell_characters`. |
| `output-count-limit` | A code cell had more outputs than `max_outputs_per_cell`. |
| `output-mime-entry-limit` | A MIME bundle had more entries than `max_mime_entries_per_output`. |
| `output-text-character-limit` | A textual output preview exceeded `max_output_text_characters`. |
| `output-summary-character-limit` | The rendered summary exceeded `max_output_summary_characters`. |
| `multiple-output-limits` | More than one output limit affected the same summary block; detailed codes remain in block metadata. |

A cell source block currently has only one applicable content limit. `multiple-cell-limits` is reserved as the aggregate block code if later compatible cell-local limits coexist; detailed stable codes remain in metadata.

## Source-byte and failure behavior

The file API streams the complete source locally to calculate or verify the full SHA-256 while retaining at most `max_source_bytes`. A Notebook larger than that limit returns `failed` with `notebook-source-byte-limit`; C-03 does not parse a prefix or fabricate a partial document. Strict UTF-8 and UTF-8-BOM are accepted. Invalid encoding, malformed or duplicate-key JSON, non-finite JSON constants, invalid Notebook/cell/output structure, read failures, and hash mismatches all fail without a document.

## Scope boundary

C-03 does not execute Notebook code, trust output HTML/JavaScript, load referenced files or URLs, infer experiment meaning, perform C-04 PDF extraction, perform C-08 chunking, allocate `source_id`, create Evidence, reopen or relocate sources, mutate Manifest scheduling state, or add MCP, Hook, Web, or LLM behavior.
