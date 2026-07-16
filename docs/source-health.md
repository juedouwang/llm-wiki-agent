# Source and Evidence Health (D-06)

D-06 provides a deterministic project-wide health pass over every registered
source and every persisted D-03 Evidence record. It verifies current source
availability and exact bytes first, then checks Evidence source identity,
locator reopening, and excerpt hash. The pass is local-only and never writes to
the research project.

The Python entry point is:

```python
from tools.source_health import evaluate_source_health

result = evaluate_source_health(workspace_root, project_id)
```

## Status model

Every source and Evidence record receives exactly one status:

| Status | Source meaning | Evidence meaning |
|---|---|---|
| `valid` | The current path is available and its stable SHA-256 equals the recorded current source version | The current source version and content match, the locator reopens, and the exact excerpt hash matches |
| `stale` | Current bytes differ from the recorded hash, or the source lacks a recorded current version | The source is modified, the Evidence is bound to a non-current version/content hash, the locator is invalid for current bytes, the locator/source format cannot reopen, or the excerpt hash differs |
| `missing` | The current source is deleted, not a regular file, outside the registered boundary, or cannot be read, and no unique D-05 recovery succeeds | The Evidence source is unavailable for the same reason |
| `ambiguous` | D-05 finds multiple equal-priority exact-hash relocation candidates and binds none | The Evidence source identity cannot be resolved uniquely, so the Evidence is not reopened |

The report's `overall_status` uses deterministic fail-closed severity:
`ambiguous` > `missing` > `stale` > `valid`. Source status does not become stale
merely because one of its Evidence locators is stale; source and Evidence
classifications remain separate, while `overall_status` exposes the worst
observed condition.

## Evaluation order

For each source, sorted by `source_id`:

1. Require a recorded current source version and SHA-256.
2. Run the D-05 recovery core, which first verifies the current path and hash.
3. Accept `not-needed` or a uniquely verified `recovered` path as `valid`.
4. Map a current exact-hash mismatch with no safe recovery to `stale`.
5. Map unavailable current content with no safe recovery to `missing`.
6. Preserve D-05 multiple-candidate results as `ambiguous`; no binding changes.

A unique D-05 recovery may atomically update only `sources.jsonl` before health
continues. The `source_id`, source versions, and source content hash remain
unchanged.

For each persisted Evidence record, sorted by `evidence_id`:

1. If its source is non-valid, inherit that source's `stale`, `missing`, or
   `ambiguous` status without pretending the Evidence was reopened.
2. Revalidate the D-03 historical source-version/content-hash binding and require
   that exact version to be current. An A -> B -> A content recurrence does not
   make old Evidence current again.
3. Reopen the C-01 locator from exact current source bytes through D-04.
4. Hash the reopened excerpt with the strict D-03 UTF-8 contract and compare it
   to the persisted `excerpt_hash`.

A schema-valid locator that is out of bounds for current content is `stale` with
`source-locator-invalid`. A valid locator with different excerpt bytes is
`stale` with `excerpt-hash-mismatch`. Format parsing failures remain explicit
through D-04 reason codes such as `source-locator-format-unsupported`.

## Auditable result and reconciliation

`evaluate_source_health(...)` returns a transient
`llmwiki-source-health-report` with:

- `schema_version: 1`;
- `health_version: source-health-v1`;
- project and registry paths;
- separate source and Evidence registry/classified totals;
- a four-status count map for each registry;
- the number of D-05 registry writes completed by the pass;
- one `llmwiki-source-health` record per registered source; and
- one `llmwiki-evidence-health` record per persisted Evidence identity.

Each source record includes its path, version, hash, reason, and the full
schema-versioned D-05 recovery audit when recovery was evaluated. Each Evidence
record includes its exact identity fields, locator, expected excerpt hash,
source status, current path, and observed excerpt hash when reopening succeeded.
Raw source bytes and excerpts are not persisted in the health result.

Construction fails closed unless:

```text
source registry count == classified source count == sum(source status counts)
evidence registry count == classified Evidence count == sum(Evidence status counts)
```

The source registry is reloaded after recovery and after Evidence checks. The
Evidence registry is also reloaded before returning. Unexpected concurrent
identity/version/path or Evidence-registry changes abort the report rather than
returning mixed totals.

An absent `evidence.jsonl` means zero persisted Evidence and remains absent. A
present but malformed, legacy-incompatible, or future-version registry fails
closed through the existing schema compatibility boundary.

## CLI

Run the aggregate pass with:

```bash
python tools/project.py source health <project_id> --json
```

JSON output includes the complete schema-versioned report under `ok: true`.
Human output prints overall status, reconciled source/Evidence counts, each
status total, relocation writes, and every non-valid record with its stable
reason code.

A non-valid health result is a successful evaluation and exits zero. Parser,
registry, schema, or unreconciled-state failures exit with the normal structured
command error. This lets automation distinguish an unhealthy project from a
health pass that could not be trusted.

## Safety and non-goals

- Source files and directories are opened read-only. Their names, bytes,
  timestamps, and metadata are never changed.
- The only possible write is D-05's verified atomic relocation update under the
  existing `sources.jsonl.lock`.
- No Evidence records, curated Markdown, extraction output, Claim state, or run
  state is written by D-06.
- There are no LLM, MCP, Hook, Web, network, remote-Git, query/synthesis,
  extraction-scheduling, or orchestration calls.
- D-06 does not propagate Claim status or refresh stale Evidence. Those remain
  later roadmap work.
