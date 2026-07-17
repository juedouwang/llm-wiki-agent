# J-03A/J-03B Development Supervision Dashboard

Status: **development supervision and unit evidence views are implemented; the full J-03 research-product dashboard remains incomplete.**

J-03A provides the local, read-only roadmap dashboard. J-03B adds unit-level evidence grouping, clickable unit detail, validation/check navigation, and a narrowly allowlisted preview for committed UTF-8 text artifacts. These views supervise development work; they do not browse registered research projects or claim the complete J-03 product acceptance condition.

## Start the dashboard

From the repository root:

```bash
python -B tools/development_dashboard.py serve
```

Open `http://127.0.0.1:8765/`. A different loopback port may be selected explicitly:

```bash
python -B tools/development_dashboard.py serve --host 127.0.0.1 --port 8876
```

The server rejects non-loopback bind addresses. Add `--open` to open the local URL in the default browser. To inspect the same snapshot without starting the server:

```bash
python -B tools/development_dashboard.py snapshot --pretty
```

## What is visible

The dashboard refreshes every ten seconds and shows:

- repository branch, HEAD, clean/dirty state, and ahead/behind counts;
- completed, partial, active, blocked, and not-started task totals;
- the currently authorized unit and its latest progress record;
- the R0 through R6 milestone track;
- searchable and filterable roadmap tasks, acceptance conditions, capability deltas, commits, checkpoints, and file totals;
- clickable unit buttons inside task detail;
- selected unit detail containing its append-only records, checks, explicit commits, checkpoints, and catalog-declared artifacts;
- an append-only validation timeline whose entries open the corresponding task and unit;
- checkpoint entries that open a unit when the checkpoint name identifies one.

The snapshot exposes the same unit evidence in three forms:

- `units`: the flat unit list;
- `unit_groups`: units grouped by `task_id`;
- `tasks[].units`: units embedded in the existing task payload.

A unit can be discovered from the optional catalog, a progress record, a recognized branch, or a recognized checkpoint. Branch and checkpoint parsing distinguishes task `P-08` from suffixed units such as `F-01A` and `F-01B`. The historical J-03 slugs `dashboard-foundation` and `unit-visualization` map to `J-03A` and `J-03B` respectively.

The visual states are intentionally distinct:

| State | Meaning |
|---|---|
| `completed` | The roadmap records the whole task, or the latest unit validation, as passed. |
| `partial` | A slice or checkpoint exists, but the whole roadmap task is not complete. |
| `authorized` | The user approved the unit and implementation has not changed the worktree yet. |
| `in_progress` | The authorized unit has active work or an explicit in-progress record. |
| `awaiting_review` | Work is committed/ahead or explicitly submitted for review. |
| `needs_changes` | Validation failed and correction is required. |
| `blocked` | An explicit blocking condition is recorded. |
| `not_started` | No completion, checkpoint, catalog evidence, or active authorization is visible. |

## Authorization and append-only progress

The dashboard does not authorize work by itself. A bounded progress or validation event can be appended with:

```bash
python -B tools/development_dashboard.py record \
  --task-id J-03 \
  --unit-id J-03B \
  --status awaiting_review \
  --summary "Unit evidence visualization is ready for review." \
  --check dashboard-tests=passed:"focused dashboard tests passed"
```

Supported progress states are `authorized`, `in_progress`, `awaiting_review`, `passed`, `failed`, and `blocked`. Check states are `pending`, `passed`, `failed`, and `skipped`.

The command loads and validates the existing ledger, keeps every existing record in its original order, appends exactly one new record, preserves the optional catalog, and atomically replaces the ledger file. It does not edit prior records.

## Ledger and optional unit catalog

The local ledger is `.llmwiki/development-dashboard/progress.json` with `schema_version: 1` and `kind: development-dashboard-progress-ledger`. A Schema v1 ledger containing only `records` remains valid and behaves as it did before J-03B.

A ledger may additionally contain this strictly versioned catalog:

```json
{
  "schema_version": 1,
  "kind": "development-dashboard-progress-ledger",
  "records": [],
  "unit_catalog": {
    "schema_version": 1,
    "kind": "development-dashboard-unit-catalog",
    "units": [
      {
        "task_id": "J-03",
        "unit_id": "J-03B",
        "title": "Unit evidence visualization",
        "summary": "Group records and checks and expose bounded committed previews.",
        "commits": ["0123456789abcdef0123456789abcdef01234567"],
        "checkpoints": ["checkpoint/j-03-unit-visualization"],
        "artifacts": [
          {
            "artifact_id": "dashboard-doc",
            "label": "Dashboard documentation",
            "description": "The committed operator and security contract.",
            "path": "docs/development-dashboard.md",
            "commit": "0123456789abcdef0123456789abcdef01234567"
          }
        ]
      }
    ]
  }
}
```

Catalog rules are fail-closed:

- `unit_catalog.schema_version` must be exactly `1`, and its `kind` must match;
- catalog, unit, and artifact objects reject unknown fields;
- `unit_id` must belong to its declared `task_id`;
- every catalog commit is an explicit full 40-hex Git commit ID;
- a catalog checkpoint must parse to the declared unit;
- every artifact commit must occur in that unit's `commits` list;
- catalog size, per-unit commit/checkpoint/artifact counts, text fields, and artifact paths are bounded;
- duplicate units, artifact IDs, records, and opaque artifact-route collisions are rejected.

Missing catalog data is not an error. Missing or unsupported ledger/catalog schema versions are errors; future versions are never interpreted as the current schema.

## Allowlisted artifact preview

A catalog artifact appears in the snapshot with an opaque URL of the form:

```text
/api/artifacts/<64 lowercase hex characters>
```

The token is derived from the validated unit/artifact declaration. The route does not accept a path parameter or a query-string override. On every request the server reloads and validates the current ledger, resolves the token only through the catalog allowlist, and reads the declared object from Git. It never reads the corresponding workspace file.

A preview is served only when all of these conditions hold:

1. the artifact remains declared in the current Schema v1 catalog;
2. its commit is a full explicit commit listed by the unit;
3. that object exists as a Git commit and the exact path resolves to one regular `100644` or `100755` Git blob;
4. the blob is at most 256 KiB;
5. the complete blob decodes as strict UTF-8.

Artifact paths must be canonical repository-relative paths. Absolute paths, Windows drive/UNC forms, backslashes, URL-like values, traversal components, empty components, and any `raw`, `.git`, or `.llmwiki` component are rejected. Symlinks, submodules, arbitrary workspace paths, external URLs/content, oversized blobs, and non-UTF-8 blobs are never returned. Preview content is assigned with DOM `textContent`; it is not inserted as HTML.

## Security and source boundaries

The dashboard keeps the J-03A security boundary while adding only the opaque artifact route:

- binds only to an IP loopback address and defaults to `127.0.0.1`;
- accepts only well-formed loopback `Host` headers;
- exposes fixed local assets, `/api/status`, `/api/health`, and allowlisted `/api/artifacts/<token>`;
- rejects POST, PUT, PATCH, DELETE, and OPTIONS with a read-only response;
- provides no arbitrary file-read or workspace-path endpoint;
- sends no data externally and loads no CDN assets;
- applies a restrictive Content Security Policy and related browser security headers;
- uses DOM node construction and `textContent`, with no dynamic HTML injection;
- removes the repository absolute path from public snapshots;
- does not register, inventory, extract, or read a research source project.

The server is a development aid, not an authentication boundary or network service. Do not expose it through a public interface or reverse proxy.

## J-03 scope still open

This remains a **partial J-03 result**. Later, separately authorized work is still required for registered project selection, the complete 15-class curated research package, coverage and failure views, source/Evidence navigation, run history, goals, plans, user tasks, and links to precise curated knowledge and Evidence locators. J-03A/J-03B improve development supervision without claiming those research-product capabilities.
