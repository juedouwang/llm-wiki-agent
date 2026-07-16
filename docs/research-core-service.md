# Research Core Service Facade (G-01, extended by G-07 and G-08)

- G-01 status: implemented and accepted
- Public module: `tools.research_core`
- Public class: `ResearchCoreService`
- Core validation: `tests/test_research_core_service.py`
- G-07 host-view validation: `tests/test_research_mcp_server.py`
- G-08 Host Context Pack validation: `tests/test_host_context_pack.py`
- Existing checkpoints: `checkpoint/g-01-core-service`, `checkpoint/g-07-mcp-server`

## Purpose

`ResearchCoreService` is the host-independent Python boundary for deterministic
Research Core capabilities. The project CLI and MCP transport delegate to this
facade instead of duplicating storage or source-access workflows. Future Codex,
Claude Code, Hook, Web, and other adapters must use the same boundary.

The facade does not parse command-line arguments, print terminal output, start a
server, call an LLM, open a browser, or import a host adapter. It returns typed
results and lets typed Core exceptions propagate. Policy, schema validation,
source-read-only behavior, and fail-closed version checks remain in the domain
modules accepted during R1.

## Construction

```python
from tools.research_core import ResearchCoreService

core = ResearchCoreService(workspace_root=r"E:\ResearchCore")
```

Construction only normalizes the workspace path. It does not register or scan a
project and does not create state in the research source tree.

## Service operations

| Service method | Deterministic implementation | Return type |
|---|---|---|
| `register(...)` | `tools.project_registry.register_project` | `ProjectRegistrationResult` |
| `project_context(project_id)` | validated `tools.project_registry.load_registered_project` projection | path-free `ProjectContextResult` |
| `scan(...)` | `tools.project_inventory.inventory_project` with `ScanPolicyConfig` | `ProjectInventoryResult` |
| `coverage(project_id)` | `tools.coverage_report.generate_coverage_report` | local/path-bearing `CoverageReportResult` |
| `coverage_view(project_id)` | `coverage(...)` plus host-safe projection | path-free `HostCoverageResult` |
| `host_context_pack(project_id, max_bytes=...)` | `tools.host_context.assemble_host_context_pack` over host-safe Core DTOs and current registries | path-free `HostContextPackResult` |
| `source_open(...)` | `tools.source_access.open_source` or `open_evidence` | local/path-bearing `SourceOpenResult` |
| `source_open_view(...)` | policy-enforced `source_open(...)` plus host-safe projection | path-free `HostSourceOpenResult` |

### Register

```python
registration = core.register(
    r"E:\Projects\study",
    knowledge_root=r"E:\Knowledge\projects",
    final_goal="Reproduce the reported result",
)
```

Registration remains deterministic and source-read-only. It creates project
identity and external storage only; it is not a completed scan.

### Project context

```python
context = core.project_context("study-0123456789ab")
```

This read-only G-07 extension revalidates the persisted registration and returns
a dedicated Schema v1 `ProjectContextResult`. It retains:

- project ID, name, identity strategy, and registration time;
- onboarding goal, stage, key question, deadline, and daily hours;
- Git availability, repository status, branch, and head commit.

It intentionally omits source/workspace/machine/knowledge paths, `project.yaml`
location, storage records, Git root, and Git origin URL. It performs no scan and
does not expose the raw `ProjectRegistrationResult`.

### Scan

```python
from tools.scan_policy import ScanPolicyConfig

policy = ScanPolicyConfig(
    include_patterns=("results/**",),
    exclude_patterns=("scratch/**",),
    sensitive_patterns=("private/**",),
    external_exclude_patterns=("private/**",),
    external_send_mode="local-only",
)
inventory = core.scan(registration.project_id, policy_config=policy)
```

`scan` is the service name for the existing `inventory` command. A complete
`ScanPolicyConfig` is the canonical host-facing input, retaining B-02 controls
for include/exclude and sensitive patterns, external-send rules, size limits,
symlink behavior, and case sensitivity. Convenience include/exclude/follow
arguments remain for CLI mapping and cannot be combined with `policy_config`.
The method runs the accepted B-03 through B-06 chain; `tools.scan_policy` and
`tools.project_inventory` remain authoritative.

### Coverage and host coverage view

```python
local_coverage = core.coverage(registration.project_id)
host_coverage = core.coverage_view(registration.project_id)
```

Both generate the same deterministic report from the current Manifest and
persist it under
`.llmwiki/projects/<project_id>/indexes/coverage-report.json`. The report remains
reconcilable by count, byte size, role, status, read depth, and reason.

`CoverageReportResult` retains trusted local `manifest_file` and `report_file`
paths for backward-compatible CLI automation. `HostCoverageResult` retains only
`project_id`, `report_persisted`, and a recursively projected report body with
closed fields, normalized relative failure paths, and validated coverage buckets.
Unknown nested fields fail closed instead of crossing the host boundary.
Consequently coverage is idempotent but not read-only.

### Host Context Pack

```python
pack = core.host_context_pack(
    registration.project_id,
    max_bytes=32_768,
)
payload = pack.as_dict()
```

G-08 assembles a closed, deterministic Schema v1 handoff from
`project_context(...)` and `coverage_view(...)`, then joins the current Manifest,
Source registry, and Evidence registry. The result contains bounded project and
inventory state, deterministic coverage risks, explicit omission counts, and
reopenable Evidence references. It contains no raw Evidence excerpts, source or
storage paths, or curated wiki content. Until I-02 lands, `tasks` is empty and a
`task-store-unavailable` omission records that limitation.

The complete compact, sorted UTF-8 JSON payload is budgeted. Its
`budget.used_bytes` equals the encoded envelope size and never exceeds
`budget.max_bytes`; a budget that cannot hold the mandatory envelope raises
`HostContextBudgetError` with `reason_code: context-budget-too-small`. See
[`host-context-pack.md`](host-context-pack.md) for the complete payload, filtering,
and truncation contract.

`host_context_pack(...)` is deterministic and idempotent, but it is not strictly
read-only: its `coverage_view(...)` dependency persists the deterministic
coverage report under machine state. It does not modify the registered source
project or curated knowledge tree. The convenience
`tools.host_context.build_host_context_pack(...)` routes through this same Core
method rather than duplicating filesystem workflows.

### Source open and host source-open view

```python
from tools.extraction_schema import LineRangeLocator

local_open = core.source_open(
    registration.project_id,
    "src-...",
    locator=LineRangeLocator(10, 20),
    expected_content_hash="...",
)

host_open = core.source_open_view(
    registration.project_id,
    "src-...",
    locator=LineRangeLocator(10, 20),
    expected_content_hash="...",
)
```

A `src-*` target requires a typed Locator or locator dictionary. An `evd-*`
target reuses its persisted locator and hashes and rejects caller overrides.
Unknown target types and invalid locator combinations fail closed through the
existing `SourceAccessError` hierarchy and stable `reason_code` values.

`source_open(...)` keeps backward-compatible local behavior and accepts the
explicit `enforce_content_policy` switch. `source_open_view(...)` always enables
that switch. Before returning raw content it requires one unique current Manifest
record matching relative path plus content SHA-256, and denies
`sensitive-path`, `content-size-limit`, `outside-scan-boundary`, and every
`read_depth: ignored` state. The check runs before byte reads and again after
relocation recovery.

The default `external_send_mode: local-only` prohibits sending raw content to an
independent external provider. It does not by itself prohibit an explicit local
host-agent open when Manifest local content access is allowed.

`SourceOpenResult` retains local `project_root` and `absolute_path` fields.
`HostSourceOpenResult` retains only stable source identity, a validated normalized
current relative path and version, a re-parsed closed Locator union, excerpt,
hashes, formats, and Evidence identity fields. Injected locator or path fields fail
closed before serialization.

## CLI and transport equivalence

The local CLI delegates these commands to `ResearchCoreService`:

```text
python -m tools.project register ... --json      -> core.register(...)
python -m tools.project context ... --json       -> core.project_context(...)
python -m tools.project context-pack ... --json  -> core.host_context_pack(...)
python -m tools.project inventory ... --json     -> core.scan(...)
python -m tools.project coverage ... --json      -> core.coverage(...)
python -m tools.project source open ... --json   -> core.source_open(...)
```

For JSON commands, the CLI adds top-level `ok: true` to the service
`as_dict()` payload. `context` therefore has exact Core/CLI/MCP data parity.
After removing the CLI `ok` field and the MCP success envelope, `context-pack`
has exact direct-builder/Core/CLI/MCP pack parity. Coverage and source-open MCP
calls use `coverage_view` and `source_open_view`, so
they have semantic report/excerpt parity while intentionally omitting the local
CLI's absolute paths.

## Safety and ownership boundaries

- Machine state stays under `.llmwiki/projects/<project_id>/` in the configured
  workspace.
- Curated knowledge stays under the configured
  `wiki/projects/<project_id>/`-equivalent knowledge root.
- The registered research source project remains read-only.
- Host-safe DTOs add no persisted record and do not rewrite legacy data; Host
  Context assembly only triggers the existing deterministic coverage-report write.
- All schema/version checks continue through `tools.project_layout` and accepted
  R1 modules; future unsupported versions fail closed.
- No source content is sent externally by this deterministic service path.
- Raw content crosses a host boundary only through explicit, policy-authorized
  source-open.

## Validation

Run the focused regressions:

```powershell
python -B -m pytest -q `
  tests/test_research_core_service.py `
  tests/test_source_access.py `
  tests/test_research_mcp_server.py `
  tests/test_host_context_pack.py
```

They cover direct service operations, real CLI delegation, complete scan-policy
mapping, host-safe DTO redaction, Manifest source-content authorization, exact
source/evidence reopening, Host Context byte accounting and deterministic
truncation, output-schema fail-closed behavior, source-project
hash/size/mtime/mode preservation, and the absence of LLM/network/Web calls.

## Explicit non-goals

G-01, G-07, and G-08 do not implement orchestration, Hooks, event-ledger
reconciliation, one-click `project understand`, Web rendering, Verified Query,
or the I-02 task store and planning pipeline. G-07 supplies the minimal MCP
adapter documented in
[`research-mcp-server.md`](research-mcp-server.md); later capabilities remain
separate roadmap tasks.
