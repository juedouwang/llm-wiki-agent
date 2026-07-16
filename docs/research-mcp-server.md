# Minimal Research Core MCP Server (G-07, extended by G-08)

- Status: implemented for the R2 G-07 transport scope and G-08 Host Context Pack
- Server module: `tools.research_mcp`
- Core boundary: `tools.research_core.ResearchCoreService`
- Transport: MCP stdio, official Python SDK `mcp>=1.28.1,<2`
- JSON Schema validation: `jsonschema>=4.25,<5`
- Validation: `tests/test_research_mcp_server.py`, `tests/test_source_access.py`,
  and `tests/test_host_context_pack.py`
- Existing checkpoint: `checkpoint/g-07-mcp-server`

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

G-07 and G-08 do **not** pretend that later Core query, reconciliation, or planning
pipelines exist. Their contracts are discoverable so adapters can stabilize,
but calls return `capability-unavailable` until the corresponding roadmap slices
land.

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

Host-specific packaging remains a later adapter task. The server has no Codex-
or Claude-specific import.

## Tool catalog

| MCP tool | Current behavior | Core delegation / honest status |
|---|---|---|
| `llmwiki_project_context` | Available, read-only | `ResearchCoreService.project_context(project_id)` returns a host-safe `ProjectContextResult` without scanning source files |
| `llmwiki_host_context` | Available, writes deterministic machine state | `ResearchCoreService.host_context_pack(project_id, max_bytes=...)` returns the closed, path-free `HostContextPackResult`; coverage persistence makes `readOnlyHint=false` |
| `llmwiki_coverage` | Available, writes deterministic machine state | `ResearchCoreService.coverage_view(project_id)` persists `indexes/coverage-report.json` and returns a path-free `HostCoverageResult`; `readOnlyHint=false` |
| `llmwiki_source_open` | Available, read-only source access | `ResearchCoreService.source_open_view(...)` enforces current Manifest content policy and returns a path-free `HostSourceOpenResult` |
| `llmwiki_query` | Contract only | Returns `capability-unavailable`, `available_after: G-04` |
| `llmwiki_reconcile` | Contract only | Returns `capability-unavailable`, `available_after: H-07` |
| `llmwiki_plan` | Contract only | Returns `capability-unavailable`, `available_after: I-04` |

The catalog contains exactly seven tools. The three unavailable tools are
negative capability contracts, not successful
placeholders: `isError` is true, `ok` is false, no `result` exists, and no
project state changes.

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
project-relative paths, exact Locator union members, and extra fields are checked
before Core invocation.

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
non-read-only. `llmwiki_host_context` uses that same coverage path and also has
`readOnlyHint=false`, while the registered source project stays unchanged. Tests
verify no `.llmwiki/` or `wiki/` directory is created inside the source project.

## Automated contract evidence

`tests/test_research_mcp_server.py` starts the server as a real subprocess and
uses the official MCP `ClientSession` and stdio client to verify:

1. protocol initialization, module/direct-script startup, and the exact seven-tool
   catalog;
2. explicit input and output JSON Schema enforcement, including malformed types,
   duplicate arrays, malformed hashes, missing fields, and extra fields;
3. exact Core/CLI/MCP project-context and Host Context Pack parity, plus semantic
   coverage/source parity;
4. omission of absolute paths, storage records, Git root, and Git origin URL;
5. coverage report persistence with `readOnlyHint=false` and no source writes;
6. Manifest denial of a real `.env` secret, every policy-limit reason, and any
   `read_depth: ignored` state while ordinary default `local-only` source access
   succeeds;
7. stable typed error mapping, including real source-open wrappers, and
   future-schema rejection;
8. malformed or nested path-injected Core DTO fallback to a schema-valid redacted
   `internal-error`;
9. honest unavailable capability results with no state change or input echo;
10. no raw-content leakage outside explicit policy-authorized source-open;
11. clean server stderr and no duplicated filesystem workflow in the adapter.

`tests/test_host_context_pack.py` additionally verifies the closed G-08 payload,
exact canonical UTF-8 byte accounting, deterministic truncation, omission counts,
sensitive/ignored/stale Evidence filtering, the pre-I-02 empty task contract,
typed too-small-budget failure, and fail-closed future schemas.

`tests/test_source_access.py` separately verifies the domain policy gate on
`open_source(..., enforce_content_policy=True)`.

## Non-goals and rollback

G-07 and G-08 do not implement Verified Query (G-02 through G-06), event-ledger
reconciliation (H-04/H-07), the I-02 task store or planning pipeline (I-01 through
I-04), Hooks, Plugins, Web rendering, or one-click project understanding.

To roll back after dependent work has been reverted, use non-destructive Git
history operations:

```powershell
git revert (git rev-list -n 1 checkpoint/g-07-mcp-server)
```

Do not delete project state, rewrite source projects, or use destructive Git
history commands as rollback mechanisms.
