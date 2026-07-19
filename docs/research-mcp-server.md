# Minimal Research Core MCP Server (G-07, extended by G-08, H-07, and I-04)

- Status: implemented for G-07 transport, G-08 Host Context Pack, H-07 reconciliation, and I-04 DRAFT initial planning through 2026-07-19
- Server module: `tools.research_mcp`
- Core boundary: `tools.research_core.ResearchCoreService`
- Transport: MCP stdio, official Python SDK `mcp>=1.28.1,<2`
- JSON Schema validation: `jsonschema>=4.25,<5`
- Validation: `tests/test_research_mcp_server.py`, `tests/test_research_planning.py`,
  `tests/test_source_access.py`, `tests/test_host_context_pack.py`, and
  `tests/test_project_reconciliation.py`
- Existing checkpoints: `checkpoint/g-07-mcp-server`, `checkpoint/h-07-project-reconciliation`, and `checkpoint/i-04-initial-planning`

## Purpose and boundary

G-07 adds one host-neutral Model Context Protocol adapter over Research Core;
G-08 adds the bounded `llmwiki_host_context` handoff to that adapter. An
MCP-capable host can initialize a real stdio server, discover stable tool
contracts, and call deterministic Core operations without copying project or
filesystem logic into Codex, Claude Code, a Plugin, or a Hook.

The adapter is deliberately transport-only:

- registration loading, schema/version checks, coverage generation, Manifest
  policy, and source reopening remain in `ResearchCoreService` and its domain
  modules;
- the adapter writes no independent project database and performs no direct
  source-project filesystem workflow;
- every call returns a Schema v1 envelope in both `structuredContent` and JSON
  text content;
- every advertised input and output JSON Schema is compiled and enforced;
- locator unions, relative paths, coverage axes/buckets/failures, reconciliation,
  and error details use closed nested schemas rather than generic objects;
- malformed or path-injected Core output fails closed to a constant redacted
  `internal-error`;
- errors use MCP `isError=true`, stable codes, and no exception text;
- raw source text is returned only by explicit `llmwiki_source_open` after
  Manifest content-access authorization;
- no LLM, external network service, browser, or host-specific API is used.

G-07 and G-08 originally reserved later query, reconciliation, and planning
contracts so adapters could stabilize without successful placeholders. Validated
H-07 activates `llmwiki_reconcile`, and I-04 activates `llmwiki_plan` as real Core
calls. Planning returns strict non-executable DRAFT machine state only. Verified
Query remains an honest `capability-unavailable` contract until G-04 lands.

## Installation and startup

Install repository dependencies, then start the server over stdio:

```powershell
python -B -m tools.research_mcp `
  --workspace-root E:\ResearchAssistantWorkspace
```

Direct script execution is equivalent:

```powershell
python -B tools/research_mcp.py `
  --workspace-root E:\ResearchAssistantWorkspace
```

`--workspace-root` is required. It is the assistant workspace that owns
`.llmwiki/projects/<project_id>/` and the configured
`wiki/projects/<project_id>/` knowledge tree; it is not the research source
project.

A generic MCP host configuration uses the same command and arguments:

```json
{
  "command": "python",
  "args": [
    "-B",
    "-m",
    "tools.research_mcp",
    "--workspace-root",
    "E:\\ResearchAssistantWorkspace"
  ],
  "cwd": "E:\\GitHub\\llm-wiki-agent"
}
```

The J-05 Codex reference package now launches this server without adding
Codex-specific imports to the host-neutral Core. Claude Code packaging remains a
later J-06 adapter task. See
[`codex-reference-adapter.md`](codex-reference-adapter.md).

## Tool catalog

| MCP tool | Current behavior | Core delegation / honest status |
|---|---|---|
| `llmwiki_project_context` | Available, read-only | `ResearchCoreService.project_context(project_id)` returns a host-safe `ProjectContextResult` without scanning source files |
| `llmwiki_host_context` | Available, writes deterministic machine state | `ResearchCoreService.host_context_pack(project_id, max_bytes=...)` returns the closed, path-free `HostContextPackResult`; coverage persistence makes `readOnlyHint=false` |
| `llmwiki_coverage` | Available, writes deterministic machine state | `ResearchCoreService.coverage_view(project_id)` persists `indexes/coverage-report.json` and returns a path-free `HostCoverageResult`; `readOnlyHint=false` |
| `llmwiki_source_open` | Available, read-only source access | `ResearchCoreService.source_open_view(...)` enforces current Manifest content policy and returns a path-free `HostSourceOpenResult` |
| `llmwiki_query` | Contract only | Returns `capability-unavailable`, `available_after: G-04` |
| `llmwiki_reconcile` | Available, writes deterministic machine state | `ResearchCoreService.project_reconcile(project_id, dirty_paths=...)` snapshots H-04 events, always runs the full `register -> inventory -> classify` fallback, and advances only the starting snapshot checkpoint; `readOnlyHint=false` |
| `llmwiki_plan` | Available, writes deterministic DRAFT machine state | Delegates to `ResearchCoreService.plan(...)`; creates/loads Goal/tasks/project-state and publishes `indexes/initial-plan.json` without authorizing execution or directly writing Markdown |

The catalog contains exactly seven tools. Query is the only remaining negative
capability contract: `isError` is true, `ok` is false, and no `result` exists.
Plan and reconciliation are real non-read-only operations. Plan annotations set
`readOnlyHint=false` and `idempotentHint=false` because each call may publish a
new explicitly dated DRAFT revision.

## Host-safe result DTOs

MCP returns dedicated host DTOs rather than raw local CLI/storage records.

`project-context` retains stable identity, registration time, onboarding fields,
and Git availability/repository/branch/head metadata. It omits source root,
workspace/machine/knowledge paths, project-file paths, storage records, Git root,
and Git origin URL. The JSON CLI command uses the same DTO, so direct Core, CLI,
and MCP project-context payloads have exact data parity:

```powershell
python -B -m tools.project context <project_id> `
  --workspace-root E:\ResearchAssistantWorkspace `
  --json
```

Host Context Pack returns the same closed pack produced by the direct builder
and Core facade. The equivalent local command is:

```powershell
python -B -m tools.project context-pack <project_id> `
  --workspace-root E:\ResearchAssistantWorkspace `
  --max-bytes 32768 `
  --json
```

After removing the CLI's top-level `ok` field and the MCP success envelope, the
pack payloads have exact parity. `budget.used_bytes` is the canonical compact,
sorted UTF-8 JSON size of the complete pack, and the payload always fits
`budget.max_bytes`. The tool is idempotent but advertises `readOnlyHint=false`
because its Core path persists `indexes/coverage-report.json`. The payload carries
IDs, hashes, and typed Locators for eligible Evidence, but no raw excerpts or
source/storage/wiki paths. Full semantics are documented in
[`host-context-pack.md`](host-context-pack.md).

Reconciliation returns the same closed, already path-free result as direct Core
and JSON CLI calls after their transport wrappers are removed. Its optional
`dirty_paths` array accepts at most 256 unique normalized project-relative paths,
but those values are untrusted hints and are not returned. The result reports:

- `mode: full-scan` and `source_of_truth: manifest-and-hash`;
- the paused run and three succeeded stages through `classify`;
- Manifest and coverage versions, generations, hashes, and failed-file count;
- hint status and bounded counts for the starting event snapshot;
- the previous/current acknowledgement, newly acknowledged count, remaining
  post-snapshot event/path counts, state revision, and prefix hash.

Calls with no dirty paths or with hints inconsistent with the event ledger still
perform the same full scan. Only the starting event snapshot is acknowledged;
a positive failed-file count, artifact mismatch, other failures, and events
arriving during the scan remain pending. The tool advertises
`readOnlyHint=false` because it may write Manifest, coverage, run, projection,
and strict `indexes/reconciliation-state.json` machine state. See
[`project-reconciliation.md`](project-reconciliation.md).

Coverage and source-open have **semantic Core parity**, not byte-for-byte parity
with the older path-bearing local CLI wrappers:

- MCP coverage contains the same deterministic coverage report but omits
  `manifest_file` and `report_file` absolute paths;
- MCP source-open contains the same source identity, current relative path,
  locator, excerpt, and hashes but omits `project_root` and `absolute_path`.

Local CLI output remains backward compatible for trusted local automation. Host
transports must use the safe views.

## JSON Schema enforcement and result envelope

All tool input schemas are closed with `additionalProperties: false`. The server
uses Draft 2020-12 validators even though low-level SDK validation is disabled,
so behavior does not depend on a host or SDK validation path. Required fields,
types, enums, unique arrays, lowercase 64-character SHA-256 values, normalized
project-relative paths, exact Locator union members, reconciliation hint bounds,
and extra fields are checked before Core invocation.

Output schemas are recursively closed for all currently published DTO fields,
including coverage maps and Locator variants, and require exactly one branch:

- `ok: true` requires `result` and forbids `error`;
- `ok: false` requires `error` and forbids `result`.

Successful call:

```json
{
  "schema_version": 1,
  "ok": true,
  "capability": "coverage",
  "result": {}
}
```

Failed call:

```json
{
  "schema_version": 1,
  "ok": false,
  "capability": "source-open",
  "error": {
    "code": "source-locator-invalid",
    "message": "The Source locator is invalid for this request.",
    "retryable": false
  }
}
```

The adapter never serializes Python exception text. Caller-controlled questions,
objectives, dirty paths, unknown tool names, local paths, and sensitive raw
content are not echoed through errors or stderr.

## Stable G-07/G-08 error codes

| Family | Stable codes |
|---|---|
| MCP contract | `invalid-arguments`, `mcp-tool-not-found`, `capability-unavailable` |
| Project/schema | `project-id-invalid`, `project-not-registered`, `project-record-invalid`, `schema-version-invalid`, `schema-version-unsupported` |
| Coverage/layout/I/O | `coverage-unavailable`, `core-layout-invalid`, `local-io-failed`, `internal-error` |
| Reconciliation | `reconciliation-failed`, `reconciliation-hint-invalid`, `reconciliation-state-invalid`, `reconciliation-lock-failed`, `reconciliation-run-failed`, `reconciliation-snapshot-invalid` |
| Host Context | `context-budget-too-small`, `host-context-invalid` |
| Source/Evidence | Existing D-04 codes plus `source-content-policy-denied`, including `source-not-registered`, `source-locator-invalid`, `source-content-hash-mismatch`, `current-source-version-mismatch`, `source-current-path-missing`, and `source-excerpt-hash-mismatch` |

`project-not-registered` is mapped by the typed
`ProjectNotRegisteredError`, including when it appears in a wrapped exception
chain; message matching is not used. Future unsupported persisted schema
versions remain `schema-version-unsupported` and never fall back to permissive
parsing.

## Manifest content policy and privacy

`llmwiki_source_open` requires one unique current Manifest v4 file record matching
both the Source relative path and content SHA-256. It denies access when the
current file state has any of these conditions:

- `reason_code: sensitive-path`;
- `reason_code: content-size-limit`;
- `reason_code: outside-scan-boundary`;
- any `read_depth: ignored`.

The authorization is checked before reading bytes and rechecked after relocation
recovery. A denied call returns `source-content-policy-denied` without returning
the sensitive content.

The default Research Core external-send policy is `local-only`. That setting
forbids sending raw content to an independent external provider; it does **not**
by itself forbid explicit local host-agent source reopening. Ordinary in-scope
files therefore remain locally openable when their Manifest content-access state
allows it. Sensitive or otherwise policy-limited files remain denied.

Project context and coverage return metadata only. Host Context Pack additionally
returns only bounded state, deterministic risks, omission counts, and reopenable
Evidence IDs/hashes/Locators. It never bulk-loads curated wiki Markdown and does
not include raw Evidence excerpts or source/storage/wiki paths. Sensitive,
policy-limited, ignored, stale, missing, and out-of-date Evidence references are
withheld and represented only by stable reason counts.

Coverage persists deterministic machine state under
`.llmwiki/projects/<project_id>/indexes/` and is therefore correctly advertised as
non-read-only. Same-project inventory, coverage, run, and reconciliation writers
share the stable `indexes/machine-state.lock`. `llmwiki_host_context` uses that same coverage path and also has
`readOnlyHint=false`. `llmwiki_reconcile` additionally persists the conservative
run/checkpoint boundary, but returns no dirty-path values, absolute paths, raw
source content, or curated Markdown. The registered source project and configured
curated knowledge tree stay unchanged. Tests verify no `.llmwiki/` or `wiki/`
directory is created inside the source project.

## Automated contract evidence

`tests/test_research_mcp_server.py` starts the server as a real subprocess and
uses the official MCP `ClientSession` and stdio client to verify:

1. protocol initialization, module/direct-script startup, and the exact seven-tool
   catalog with reconciliation and I-04 DRAFT planning available and only query unavailable;
2. explicit input and output JSON Schema enforcement, including malformed types,
   duplicate arrays, reconciliation hint bounds, malformed hashes, missing fields,
   and extra fields;
3. exact Core/CLI/MCP project-context, Host Context Pack, and reconciliation
   parity, plus semantic coverage/source parity;
4. omission of absolute paths, dirty-path values, storage records, Git root, and
   Git origin URL;
5. coverage and reconciliation persistence with `readOnlyHint=false`, exact
   starting-snapshot acknowledgement, post-snapshot retention, and no source or
   curated-knowledge writes;
6. Manifest denial of a real `.env` secret, every policy-limit reason, and any
   `read_depth: ignored` state while ordinary default `local-only` source access
   succeeds;
7. stable typed error mapping, including real source-open wrappers, and
   future-schema rejection;
8. malformed or nested path-injected Core DTO fallback to a schema-valid redacted
   `internal-error`;
9. stable redacted reconciliation failures without dirty-path or exception-text
   echo;
10. honest query-unavailable results plus strict host-safe I-04 DRAFT planning with no input echo, source write, or direct Markdown write;
11. no raw-content leakage outside explicit policy-authorized source-open;
12. clean server stderr and no duplicated filesystem workflow in the adapter.

`tests/test_host_context_pack.py` additionally verifies the closed G-08 payload,
exact canonical UTF-8 byte accounting, deterministic truncation, omission counts,
sensitive/ignored/stale Evidence filtering, the pre-I-02 empty task contract,
typed too-small-budget failure, and fail-closed future schemas.

`tests/test_source_access.py` separately verifies the domain policy gate on
`open_source(..., enforce_content_policy=True)`.

## Non-goals and rollback

G-07, G-08, and H-07 do not implement Verified Query (G-02 through G-06),
H-05 selective extraction or knowledge refresh, the I-02 task store or planning
mature planning beyond I-04, Hook/Plugin installation, Web rendering, or the full
15-artifact one-click project-understanding contract. H-07 reconciliation stops
at `classify`, leaves later run stages pending, and does not mutate curated
knowledge.

To roll back after dependent work has been reverted, use non-destructive Git
history operations:

```powershell
git revert (git rev-list -n 1 checkpoint/g-07-mcp-server)
```

Do not delete project state, rewrite source projects, or use destructive Git
history commands as rollback mechanisms.
