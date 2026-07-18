# Precise Evidence Schema (D-03)

D-03 defines deterministic machine Evidence for exact source excerpts. An Evidence identity contains the required `source_id + content_hash + locator + excerpt_hash`, is bound to one recorded source version, and is persisted outside the research project at:

```text
.llmwiki/projects/<project_id>/evidence.jsonl
```

D-03 never opens a source file, extracts content, calls an LLM, or makes an external request. Source reopening, relocation recovery, and aggregate health remain D-04 through D-06.

## Python API

Register and inventory a project, then synchronize source state before creating Evidence:

```python
from tools.evidence_registry import register_evidence
from tools.extraction_schema import LineRangeLocator

result = register_evidence(
    workspace_root,
    project_id,
    source_id="src-0123456789abcdef0123456789abcdef",
    source_version=1,  # optional when content_hash identifies a recorded version
    content_hash="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    locator=LineRangeLocator(12, 18),
    excerpt="exact source text, including its original line endings",
)
```

`register_evidence` verifies that the source exists and that the supplied content hash belongs to the selected version. When `source_version` is omitted, the latest historical version with that hash is selected deterministically. An unknown source, unrecorded hash, or version/hash mismatch fails closed.

Use `expected_excerpt_hash` when a caller already has an independently recorded exact excerpt digest. A mismatch rejects registration:

```python
register_evidence(
    workspace_root,
    project_id,
    source_id=source_id,
    content_hash=content_hash,
    locator=locator,
    excerpt=exact_bytes,
    expected_excerpt_hash=expected_sha256,
)
```

`load_evidence_registry(...)` strictly reloads every record and rechecks its historical source-version binding without reading source-project bytes. `validate_evidence(...)` checks one record against the current source registry and optional observed content hash or exact excerpt. It returns deterministic reason codes rather than performing D-06 aggregate health classification.

## Exact excerpt contract

- `bytes` are hashed byte-for-byte with SHA-256.
- `str` is encoded with strict UTF-8 before hashing.
- Line endings and Unicode normalization are not changed.
- The persisted registry stores `excerpt_hash`, not raw excerpt content.
- Invalid UTF-8 text values, non-SHA-256 digests, and mismatched expected hashes fail closed.

Consequently, `"line\r\n"` and `"line\n"` are different excerpts, as are precomposed and decomposed Unicode strings.

## Locator contract

Evidence reuses C-01 `llmwiki-locator` records and their nested `schema_version: 1` validation:

| Locator | Required location fields |
|---|---|
| `line_range` | inclusive one-based `start_line`, `end_line` |
| `pdf_page` | one-based `page_number` |
| `notebook_cell` | zero-based `cell_index`, optional `cell_id` |
| `table_range` | `sheet`, inclusive A1 `start_cell`, `end_cell` |
| `section` | `heading_path`, inclusive source lines |
| `symbol` | `symbol`, inclusive source lines |

Unknown locator variants, missing/legacy/future nested Schema versions, Boolean integer fields, and unknown fields are rejected. Canonical serialization preserves the locator exactly as validated by `tools/extraction_schema.py`.

## Identity and artifact contract

Evidence IDs use:

```text
evd-<64 lowercase SHA-256 hex digits>
```

The digest is derived from canonical JSON containing:

```text
identity_version + project_id + source_id + source_version + content_hash + locator + excerpt_hash
```

`source_version` is part of Evidence identity. Re-registering the same excerpt against the same source version is idempotent, but a later A -> B -> A content recurrence creates a distinct Evidence record. The older version remains non-current until that exact current version is reverified.

Artifact constants:

- Machine Schema: `schema_version: 1`
- Evidence kind: `llmwiki-evidence`
- Evidence version: `evidence-v1`
- Registry kind: `llmwiki-evidence-registry`
- Registry version: `evidence-registry-v1`
- Identity version: `evidence-identity-v2`
- Hash algorithm: `sha256`
- Text encoding: `utf-8`
- Ordering: one summary row, then Evidence rows by `evidence_id`

Example summary:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-evidence-registry",
  "registry_version": "evidence-registry-v1",
  "record_type": "summary",
  "project_id": "example-project",
  "evidence_version": "evidence-v1",
  "identity_version": "evidence-identity-v2",
  "hash_algorithm": "sha256",
  "excerpt_text_encoding": "utf-8",
  "locator_schema_version": 1,
  "evidence_count": 1
}
```

Example Evidence row:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-evidence",
  "evidence_version": "evidence-v1",
  "record_type": "evidence",
  "project_id": "example-project",
  "evidence_id": "evd-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "source_id": "src-0123456789abcdef0123456789abcdef",
  "source_version": 1,
  "content_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "locator": {
    "schema_version": 1,
    "kind": "llmwiki-locator",
    "locator_type": "line_range",
    "start_line": 12,
    "end_line": 18
  },
  "excerpt_hash": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
}
```

## Validation and writes

The loader rejects malformed UTF-8 or JSON, duplicate JSON keys, non-finite values, missing/legacy/future Schema versions, unknown fields, unsupported artifact versions, invalid IDs or hashes, noncanonical row order, duplicate IDs, count mismatches, unknown sources, unrecorded source versions, and source-version hash mismatches.

Writers use an adjacent exclusive lock, reload both source and Evidence state under the lock, and atomically replace `evidence.jsonl` from the same directory. Concurrent identical registrations produce one record. Source-project paths and bytes are never modified.

## Explicit non-goals

D-03 does not:

- reopen a locator against the current source;
- recover a moved or renamed source;
- classify a collection as `valid`, `stale`, `missing`, or `ambiguous`;
- propagate stale state into Claims or Markdown;
- schedule extraction or implement query, synthesis, MCP, Hooks, Web, or LLM behavior.
