# Source Locate/Open Contract (D-04 + D-05)

D-04 provides deterministic, local-only source access for a B-01 registered
research project. It resolves a persistent `source_id` to the source registry's
**current recorded path and version**, then can reopen one exact C-01 locator
from the current source bytes.

D-05 extends this access boundary with deterministic relocation recovery. The
current path is always tried first. Only after resolution, read, or exact-hash
verification fails does access try recorded path aliases, the current Manifest's
exact content hashes, and local Git rename history. See
[`source-relocation.md`](source-relocation.md). Aggregate source/Evidence health
classification remains D-06.

## Core APIs

`tools/source_access.py` exposes:

```python
locate_source(workspace_root, project_id, source_id) -> SourceLocation
open_source(
    workspace_root,
    project_id,
    *,
    source_id,
    locator,
    expected_content_hash=None,
    expected_excerpt_hash=None,
) -> SourceOpenResult
open_evidence(workspace_root, project_id, evidence_id) -> SourceOpenResult
deserialize_locator(payload) -> Locator
```

`locate_source` returns the current project-relative path, resolved absolute
path, current source version, and recorded content SHA-256. It first resolves
only the registered current path and does not hash a normally resolvable path. If
that access fails, D-05 may repair one unique exact-hash relocation, reload the
registry, and resolve the recovered path. Ambiguous recovery fails explicitly.

`open_source` reads the resolved file, computes its SHA-256, and refuses to
extract a locator unless the bytes still match the source registry's current
content hash. A read or content mismatch may trigger one D-05 recovery attempt
and one retry. Optional expected content and excerpt hashes still require the
requested current identity and exact excerpt; recovery does not weaken them.

`open_evidence` loads one persisted D-03 Evidence record and requires all of:

1. the current source version matches `Evidence.source_version`;
2. the current source bytes match `Evidence.content_hash`; and
3. the reopened excerpt matches `Evidence.excerpt_hash`.

The version comparison happens before content-hash comparison, so an
A -> B -> A byte recurrence cannot make old Evidence current again.

A successful `SourceOpenResult` therefore reports
`content_hash_verified: true`. `excerpt_hash_verified` is `true` when an
expected excerpt hash was supplied (including every successful Evidence open),
and `null` for an unhashed direct excerpt request.

Structured results carry schema v1 plus `access_version: source-access-v1`.

## CLI

Evaluate or repair a moved source identity explicitly:

```bash
python tools/project.py source recover <project_id> <source_id> --json
```

Locate a source's current recorded path and version, with automatic recovery
only if current-path access fails:

```bash
python tools/project.py source locate <project_id> <source_id> --json
```

Open a locator directly from a source:

```bash
python tools/project.py source open <project_id> <source_id> \
  --locator-json '{"schema_version":1,"kind":"llmwiki-locator","locator_type":"line_range","start_line":10,"end_line":20}' \
  --expected-content-hash <sha256> \
  --expected-excerpt-hash <sha256> \
  --json
```

Open persisted Evidence using its own locator and hashes:

```bash
python tools/project.py source open <project_id> <evidence_id> --json
```

Evidence targets reject locator and hash overrides. JSON failures are written to
standard error with `ok: false`, an error string, and a stable `reason_code`
when the source-access layer classified the failure.

## Reopened excerpt formats

| Locator | Accepted current source | `excerpt_format` | Exact excerpt contract |
|---|---|---|---|
| `line_range` | C-02 supported text/code formats | `text-line-range` | Inclusive one-based decoded lines, preserving the decoder's original `CRLF`, `LF`, or `CR` endings |
| `section` / `symbol` | C-02 supported text/code formats | `text-line-range` | The locator's inclusive source-line span; heading/symbol names remain binding metadata |
| `pdf_page` | PDF readable by the pinned C-04 `pypdf` backend | `pdf-extracted-page-text` | The exact page text emitted by C-04; a truncated or nonexistent page fails |
| `notebook_cell` | Supported Notebook v4 JSON | `notebook-cell-source` | Exact C-03 cell source; an optional persisted `cell_id` must still match |
| `table_range` | OOXML workbook readable by `openpyxl>=3.1,<4.0` | `table-json-matrix-v1` | Inclusive rectangular cell values with formulas preserved (`data_only=False`) |

Table excerpts are compact canonical JSON arrays of rows. Strings, finite
numbers, booleans, and null are emitted directly. Values that JSON cannot
represent without losing their type use sorted-key objects:

```json
{"type":"datetime","value":"2026-07-16T12:30:45"}
{"type":"date","value":"2026-07-16"}
{"type":"time","value":"12:30:45"}
{"microseconds":86400000000,"type":"timedelta"}
{"type":"decimal","value":"1.25"}
```

Non-finite numbers and unsupported workbook value types fail instead of being
silently coerced. A direct table request is limited to 100,000 cells and OOXML
worksheet bounds; larger ranges fail before iteration. Legacy `.xls` workbooks
are not opened by D-04.

## Stable source-access reason codes

| Reason code | Meaning |
|---|---|
| `source-not-registered` | The source or Evidence target is unknown or malformed |
| `source-current-path-missing` | The current recorded path is absent or is not a regular file, and no relocation resolved it |
| `source-path-outside-project` | The current path resolves beyond the registered source root, and no relocation resolved it |
| `source-relocation-ambiguous` | Multiple equal-priority exact-hash relocation candidates remain; no binding changed |
| `current-source-version-mismatch` | Evidence is bound to a non-current source version, even if the same bytes recur later |
| `source-content-hash-mismatch` | Current bytes or a requested content hash differ from the recorded current source version |
| `source-locator-invalid` | Locator JSON, bounds, page, cell, cell ID, or sheet is invalid for current content |
| `source-locator-format-unsupported` | The source cannot be safely reopened by the locator-specific deterministic extractor |
| `source-read-failed` | The current source path cannot be resolved or read |
| `source-excerpt-hash-mismatch` | Reopened text differs from the expected or persisted excerpt hash |
| `source-access-failed` | A source-access dependency or registry failed without a narrower classification |

## Safety and non-goals

- Source projects are read-only. D-04/D-05 write no source bytes, metadata, or
  timestamps. D-05 may atomically update only the local source registry after a
  unique verified relocation.
- All source content remains local. There are no LLM, MCP, Hook, Web, network,
  or external-provider calls.
- Machine records remain under `.llmwiki/projects/<project_id>/`; D-04 does not
  place paths, hashes, or run state in curated `wiki/projects/` Markdown.
- D-05 recovery is limited to path aliases, the current Manifest hash ledger,
  and bounded local Git rename history. It never binds an ambiguous candidate.
  Aggregate health reports, query/synthesis, claim propagation, and extraction
  scheduling remain out of scope.
- Exact reopening is bounded by the existing deterministic C-02/C-03/C-04
  extractor safety limits. If a requested unit would be truncated, D-04 fails
  rather than returning a partial excerpt as exact Evidence.
