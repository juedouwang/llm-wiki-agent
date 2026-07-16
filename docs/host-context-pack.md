# Host Context Pack (G-08)

- Roadmap slice: G-08
- Domain module: `tools.host_context`
- Core facade: `ResearchCoreService.host_context_pack(...)`
- CLI: `python -m tools.project context-pack ...`
- MCP tool: `llmwiki_host_context`
- Focused validation: `tests/test_host_context_pack.py`

## Purpose

A Host Context Pack is the deterministic, host-safe handoff for one registered
research project. It gives Codex, Claude Code, or another host a compact view of
project state, current risks, task availability, and reopenable Evidence
references without loading the source tree or curated wiki into the model
context.

G-08 is metadata and reference assembly only. It does not extract source files,
run a model, answer a research query, orchestrate stages, reconcile an unscanned
source change, invoke Hooks, render a Web UI, or create the I-02 task store.

## Prerequisites

The project must already be registered and have a current supported Manifest.
For example:

```powershell
python -B -m tools.project register E:\Research\study `
  --workspace-root E:\ResearchAssistantWorkspace `
  --project-id study `
  --json

python -B -m tools.project inventory study `
  --workspace-root E:\ResearchAssistantWorkspace `
  --json
```

A missing Source registry or Evidence registry does not make the pack pretend
that Evidence exists. The pack returns an empty `evidence_refs` array and an
explicit omission reason instead. A missing or unsupported Manifest remains a
hard Core error because the state and policy boundary cannot be established.

## Public APIs

### Direct builder

```python
from tools.host_context import build_host_context_pack

result = build_host_context_pack(
    workspace_root=r"E:\ResearchAssistantWorkspace",
    project_id="study",
    max_bytes=32_768,
)
payload = result.as_dict()
wire_bytes = result.serialized_bytes()
```

`build_host_context_pack(...)` intentionally routes through
`ResearchCoreService`; it does not duplicate project-context or coverage logic.
`HostContextPackResult.as_dict()` returns a defensive copy.

### Research Core facade

```python
from tools.research_core import ResearchCoreService

core = ResearchCoreService(r"E:\ResearchAssistantWorkspace")
result = core.host_context_pack("study", max_bytes=32_768)
```

The Core method assembles the pack from the host-safe
`project_context(...).as_dict()` and `coverage_view(...).as_dict()` DTOs, then
joins the current Manifest, Source registry, and Evidence registry for Evidence
references.

### CLI

```powershell
python -B -m tools.project context-pack study `
  --workspace-root E:\ResearchAssistantWorkspace `
  --max-bytes 32768 `
  --json
```

JSON CLI output adds `ok: true` to the same pack payload. The non-JSON form prints
project ID, budget use, truncation state, and section counts.

### MCP

Call `llmwiki_host_context` with:

```json
{
  "project_id": "study",
  "max_bytes": 32768
}
```

`max_bytes` is optional and defaults to 32,768. The MCP output is the same
host-safe pack inside the standard `capability: "host-context"` result envelope.
The tool is idempotent but advertises `readOnlyHint=false` because Core coverage
generation persists the deterministic coverage report in machine state.

## Schema v1 payload

The closed top-level shape is:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-host-context-pack",
  "pack_version": "host-context-pack-v1",
  "project_id": "study",
  "budget": {
    "unit": "canonical-json-utf8-bytes",
    "max_bytes": 32768,
    "used_bytes": 2048,
    "truncated": false
  },
  "state": {
    "project": {},
    "inventory": {}
  },
  "tasks": [],
  "risks": [],
  "evidence_refs": [],
  "omissions": []
}
```

### `state`

`state.project` is a path-free projection of Core project context:

- project name, identity strategy, and registration time;
- Git availability, repository status, branch, and head commit;
- onboarding goal, current stage, important question, deadline, and available
  daily hours.

`state.inventory` is a bounded projection of the current Core coverage report:

- Manifest version and scan generation;
- file, byte, and failed-file totals;
- processing-status counts;
- read-depth counts.

It does not contain source, workspace, machine-state, or curated-knowledge paths.

### `tasks`

G-08 does not invent tasks from onboarding text. Until I-02 implements the
canonical task store, the section is always:

```json
"tasks": []
```

and `omissions` contains one `tasks / task-store-unavailable` record with a
count of `1`.

### `risks`

Risks are deterministic summaries derived from Core coverage, not free-form LLM
judgments. Current risk codes are:

- `processing-failures`;
- `sensitive-path`;
- `outside-scan-boundary`;
- `content-size-limit`;
- `unsupported-format`.

Each risk carries `risk_code`, `severity`, `file_count`, `byte_count`, and a
constant summary. It never lists affected paths.

### `evidence_refs`

An included Evidence reference contains only:

- `evidence_id` and `source_id`;
- source version and content SHA-256;
- the closed typed Locator;
- excerpt SHA-256.

It contains no raw excerpt and no current, absolute, source-project, storage, or
wiki path. The host can pass `evidence_id` to the policy-authorized source-open
operation when exact source text is needed.

Evidence is included only when the persisted state agrees that:

1. its Source registry version and content hash are current;
2. the Source registry has observed the current Manifest generation;
3. one Manifest file record matches the Source path and content hash;
4. the Manifest file state is not sensitive, otherwise policy-limited, or
   `read_depth: ignored`.

Evidence is sorted by `evidence_id`, so registry write order cannot change pack
ordering.

“Current” here means current relative to the latest persisted Manifest and Source
registry. G-08 does not hash the live source tree or perform H-07 reconciliation.
If source bytes changed after the last inventory, inventory/reconciliation must
run before the pack can classify the old Evidence as stale.

### `omissions`

Every omission is path-free and count-based:

```json
{
  "section": "evidence_refs",
  "reason_code": "evidence-stale",
  "count": 2
}
```

Current reasons include:

| Section | Reason code | Meaning |
|---|---|---|
| `tasks` | `task-store-unavailable` | I-02 task state does not exist yet |
| `evidence_refs` | `source-registry-unavailable` | No Source registry has been created |
| `evidence_refs` | `evidence-registry-unavailable` | No Evidence registry has been created |
| `evidence_refs` | `evidence-source-unregistered` | The Evidence Source ID is absent from the Source registry |
| `evidence_refs` | `evidence-stale` | Evidence version/hash disagrees with current persisted source state |
| `evidence_refs` | `source-registry-out-of-date` | Source registry generation does not match the Manifest |
| `evidence_refs` | `evidence-source-missing` | No matching current Manifest file exists |
| `evidence_refs` | `sensitive-path-filtered` | Sensitive Evidence was withheld |
| `evidence_refs` | `content-policy-filtered` | Another policy limit or ignored read depth withheld Evidence |
| `risks`, `evidence_refs` | `context-budget-exceeded` | A lower-priority candidate did not fit |

Omission rows do not contain omitted filenames, paths, hashes, excerpts, or raw
error text.

## Deterministic budget contract

The default budget is 32,768 bytes. Accepted values are 512 through 1,048,576
inclusive. Booleans and non-integers are rejected.

The budget unit is the byte length of the complete compact canonical JSON
payload using:

```python
json.dumps(
    payload,
    ensure_ascii=False,
    allow_nan=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
```

Therefore:

- multibyte Unicode is counted as UTF-8 bytes, not characters;
- `budget`, `omissions`, and the `used_bytes` field itself are included;
- `budget.used_bytes` exactly equals the final serialized byte length;
- the complete envelope never exceeds `budget.max_bytes`.

Assembly priority is deterministic:

1. mandatory project/inventory state, the empty tasks contract, and existing
   omission reasons;
2. coverage-derived risks in their fixed risk order;
3. eligible Evidence references sorted by `evidence_id`.

When a candidate cannot fit, it is omitted and counted as
`context-budget-exceeded`. Omission metadata is itself budgeted; if adding those
counts pushes the pack over the limit, the lowest-priority included records are
removed deterministically until the complete envelope fits.

`budget.truncated` is `true` only when budget pressure caused at least one
`context-budget-exceeded` omission. Policy, stale-state, or unavailable-registry
omissions can exist while `truncated` remains `false`.

If a valid budget within the published range cannot hold the mandatory envelope,
Core raises `HostContextBudgetError` with stable `reason_code:
context-budget-too-small`. Local callers can inspect `minimum_required_bytes`.
MCP returns the stable redacted `context-budget-too-small` error without Python
exception text.

## Storage, privacy, and side effects

- The registered source project remains read-only.
- Host Context assembly does not write into the source project or curated wiki.
- It does not read or bulk-load curated Markdown under `wiki/projects/`.
- It does not return source samples or Evidence excerpts.
- Sensitive and ignored Evidence is filtered before host serialization.
- The only expected write is deterministic coverage state under
  `.llmwiki/projects/<project_id>/indexes/coverage-report.json`.
- No LLM, external network call, Hook, browser, or Web render is used.
- Unsupported future structured-record schemas fail closed through the existing
  Core compatibility layer.

## Validation

Run the focused G-08 tests:

```powershell
python -B -m pytest -q tests/test_host_context_pack.py
```

The suite verifies:

- exact Schema v1 top-level and budget fields;
- direct-builder/Core parity and use of Core state;
- byte-identical repeated assembly;
- canonical full-envelope UTF-8 accounting, including multibyte text;
- deterministic large-project truncation and counted omission reasons;
- sensitive, ignored, and stale Evidence filtering;
- absence of paths and raw excerpts from Evidence references and omissions;
- honest empty task state before I-02;
- explicit missing-registry reasons;
- typed too-small-budget failure;
- source-tree preservation, no curated-wiki bulk load, and no LLM/network/Web
  behavior;
- fail-closed future project schemas.

MCP schema, parity, and error mapping are additionally exercised by
`tests/test_research_mcp_server.py`; Core/CLI delegation is exercised by
`tests/test_research_core_service.py`.

## Explicit non-goals

G-08 does not implement the I-02 task schema, I-04 plan lifecycle, E-01 run
orchestrator, extraction, adaptive reading, Verified Query, source-change
reconciliation, Hooks, Plugins, Web rendering, or one-click project
understanding. Those remain separate roadmap slices and must consume the same
Core state rather than adding host-specific copies.