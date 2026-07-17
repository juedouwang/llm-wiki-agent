# Deterministic text extraction (C-02)

C-02 adds a local, deterministic extractor for the text-family formats classified by B-05: plain text, source code, Markdown/reStructuredText, LaTeX/BibTeX, configuration, logs, HTML/XML/SVG, and structured text such as JSON/JSONL/CSV/TSV.

## API

```python
from tools.text_extractor import extract_text_file

result = extract_text_file(
    source_path,
    relative_path="src/model.py",
    format_value="python",
    expected_sha256=manifest_hash,
)
```

`extract_text_bytes(...)` provides the same extraction contract for already-loaded bytes. Both APIs return the strict C-01 `ExtractionResult` Schema; they do not write extracted state, mutate the source project, call an LLM, or send content externally.

## Output contract

- Every non-empty emitted block has an inclusive, one-based `LineRangeLocator`.
- Fixed line-count blocks are only bounded transport units. C-02 does not infer sections or symbols and does not implement C-08 chunking.
- When no content limit applies, concatenating all block texts reproduces the decoded source exactly, including mixed newline styles.
- Source code blocks use `block_type=code`, Markdown-family blocks use `markdown`, and other supported formats use `text`.
- The document records the full-file SHA-256, detected encoding, source byte count, extracted prefix byte count, omitted byte count, line count, newline styles, and stable truncation reason codes.
- Empty valid text files return `processed` with zero blocks.

## Encodings and binary rejection

Detection is deterministic and strict. The extractor recognizes UTF-8 (with or without BOM), UTF-16 LE/BE with BOM, GB18030, CP-1252, and Latin-1. An explicit supported encoding hint may be supplied when legacy bytes are inherently ambiguous. Decoding never inserts replacement characters.

Before permissive legacy decoding, a bounded sample is checked for NUL and control-heavy binary content. Binary bytes misclassified as a text format return `failed` with `document=null`; no fabricated text is emitted. UTF-16 BOM input is exempted from the NUL check.

## Bounded extraction

`TextExtractionLimits` defines deterministic limits for source bytes, characters per line, block lines, binary sample bytes, and file read chunks.

- `source-byte-limit` means only a bounded source prefix was extracted. The full file is still streamed locally to compute or verify its SHA-256.
- `line-character-limit` means at least one logical line was shortened.
- If both apply to one block, the primary block reason is `multiple-content-limits`. Block `metadata.truncation_reasons` retains stable codes and explanations, while document metadata provides the sorted aggregate reason-code list.
- A byte limit that cuts through a multibyte character omits the incomplete encoded tail rather than fabricating a replacement character.

Any applied content limit produces `status=partial`. Read errors, hash mismatches, binary contradictions, and decoding failures produce `failed` without a document. Formats outside C-02 produce `unsupported` without a document.

CSV and TSV remain supported here as line-oriented text for backward-compatible
C-02 workflows. C-06 adds a separate table-coordinate interpretation for those
same bytes, using strict delimiter parsing, logical `CSV`/`TSV` sheet names,
canonical JSON matrices, and exact `TableRangeLocator` reopening. Selecting the
C-06 extractor does not change or silently reinterpret an existing C-02 result.

## Scope boundary

C-02 does not extract Notebook cells, PDF pages, Office/table coordinates, images, or research binary metadata. It does not allocate `source_id`, create Evidence, mutate Manifest state, schedule adaptive reads, perform semantic chunking, reopen sources, or provide MCP/Hook/Web/LLM behavior. Those belong to later roadmap tasks.
