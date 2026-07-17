# Research Core Service Facade (G-01, extended through H-07)

- G-01 status: implemented and accepted
- Public module: `tools.research_core`
- Public class: `ResearchCoreService`
- Core validation: `tests/test_research_core_service.py`
- G-07 host-view validation: `tests/test_research_mcp_server.py`
- G-08 Host Context Pack validation: `tests/test_host_context_pack.py`
- E-01 run-orchestration validation: `tests/test_project_run_orchestration.py`
- E-08 deterministic-action validation: `tests/test_project_understand.py`
- H-04 host-event validation: `tests/test_host_events.py`
- H-07 reconciliation validation: `tests/test_project_reconciliation.py`
- B-07 adaptive-reading-priority validation: `tests/test_reading_priority.py`, `tests/test_project_layout.py`
- B-07 status: implemented and validated on **2026-07-17**; intended checkpoint `checkpoint/b-07-adaptive-reading-priority` is not yet created
- Existing checkpoints: `checkpoint/g-01-core-service`, `checkpoint/g-07-mcp-server`, `checkpoint/g-08-host-context-pack`, `checkpoint/e-01-run-orchestrator`, `checkpoint/e-08-deterministic-understand`, and `checkpoint/h-04-host-event-ledger`; H-07 is complete after validation on 2026-07-16 and checkpointed as `checkpoint/h-07-project-reconciliation`

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
| `host_event_submit(project_id, ...)` | `tools.host_events.submit_host_event` append plus deterministic projection | path-free `HostEventSubmitResult` |
| `dirty_path_queue(project_id)` | `tools.host_events.load_dirty_path_queue` ledger validation and projection repair | path-free `DirtyPathQueueResult` |
| `project_reconcile(project_id, dirty_paths=...)` | `tools.project_reconciliation.reconcile_project` with the E-01 run boundary through `classify` | path-free `ProjectReconciliationResult` |
| `scan(...)` | `tools.project_inventory.inventory_project` with `ScanPolicyConfig` | `ProjectInventoryResult` |
| `prioritize(project_id)` | `tools.reading_priority.generate_reading_priority` over the exact current Manifest | local/path-bearing `ReadingPriorityResult` |
| `coverage(project_id)` | `tools.coverage_report.generate_coverage_report` | local/path-bearing `CoverageReportResult` |
| `coverage_view(project_id)` | `coverage(...)` plus host-safe projection | path-free `HostCoverageResult` |
| `host_context_pack(project_id, max_bytes=...)` | `tools.host_context.assemble_host_context_pack` over host-safe Core DTOs and current registries | path-free `HostContextPackResult` |
| `project_understand(project_root, ..., resume_run_id=...)` | idempotent registration plus the E-01 orchestrator through `classify` | local/path-bearing `ProjectRunResult` |
| `project_run_start(project_id, through_stage=...)` | `tools.project_orchestrator.ProjectRunOrchestrator.start` with current deterministic Core handlers | local/path-bearing `ProjectRunResult` |
| `project_run_resume(project_id, run_id, through_stage=...)` | persisted checkpoint recovery without repeating succeeded stages | local/path-bearing `ProjectRunResult` |
| `project_run_status(project_id, run_id)` | strict read-only run loading | local/path-bearing `ProjectRunResult` |
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

### Host event ledger and dirty-path queue

```python
submitted = core.host_event_submit(
    "study-0123456789ab",
    event_id="codex:evt-001",
    producer="codex",
    occurred_at="2026-07-16T09:00:00Z",
    operation="modified",
    paths=["src/model.py"],
)
queue = core.dirty_path_queue("study-0123456789ab")
```

H-04 accepts a closed host-neutral event model, assigns contiguous ingestion
sequences, and appends canonical Schema v1 JSONL to `events.jsonl`. Exact retries
are idempotent; reuse of an event ID with a different canonical payload fails
closed. The queue is a deterministic, atomically replaceable projection bound to
the exact ledger bytes by SHA-256. Missing or stale current-schema projections
are rebuilt from the ledger, while legacy and future schemas fail closed.

Both results expose only project IDs, project-relative paths, relative artifact
names, event metadata, and queue aggregates. Submission performs lexical path
validation only: it does not scan, stat, hash, open, or mutate source files and
never updates curated knowledge. H-04 remains the untrusted input ledger; H-07
consumes it through a separate full-scan checkpoint. Hook installation and
reliability remain separate. The complete event contract is documented in
[`host-event-ledger.md`](host-event-ledger.md).

### Conservative project reconciliation

```python
result = core.project_reconcile(
    "study-0123456789ab",
    dirty_paths=["src/model.py"],  # optional untrusted hints
)
payload = result.as_dict()
```

The validated H-07 method snapshots the current H-04 event ledger, verifies the
previous acknowledged prefix, and always starts a fresh E-01 run with
`through_stage="classify"`. `register`, `inventory`, and `classify` must succeed;
the run pauses after `classify`, and later stages remain pending. Core then
validates the persisted Manifest and coverage report, requires zero failed files,
and rechecks their exact run-bound hashes and bytes before atomically advancing
`.llmwiki/projects/<project_id>/indexes/reconciliation-state.json`.

Dirty paths affect reporting only. Calls with no events or explicit paths, and
calls whose two hint sets disagree, use the same `mode: full-scan` and
`source_of_truth: manifest-and-hash`. This makes explicit Core/CLI/MCP
reconciliation independent of Hook availability.

Only the event prefix captured before the scan is acknowledged. Events appended
during the scan are returned as remaining and stay pending. Any run, positive
coverage-failure count, artifact hash/byte, lock, state, or snapshot-prefix
failure leaves the acknowledgement unchanged.
The strict Schema v1 state rejects legacy, future, malformed, or path-unsafe
records without migration.

`ProjectReconciliationResult` contains run/stage status, Manifest and coverage
hashes, bounded hint counts/status, and previous/current/remaining
acknowledgement metadata. It contains no absolute paths or dirty-path values. The
operation may update deterministic machine state, but it leaves the registered
source and curated knowledge trees unchanged and does not claim H-05 selective
extraction or knowledge refresh. See
[`project-reconciliation.md`](project-reconciliation.md).

### One-action deterministic project understanding

```python
report = core.project_understand(
    r"E:\Research\my-project",
    final_goal="Reproduce the baseline",
)

same_run = core.project_understand(
    r"E:\Research\my-project",
    resume_run_id=report.run_id,
)
```

The initial E-08 R2 method idempotently registers or reuses the project path,
then delegates to the E-01 orchestrator with an exact `through_stage="classify"`
boundary. A fresh call therefore succeeds at `register`, `inventory`, and
`classify`, leaves every later stage `pending`, persists the report, and returns
`paused`. Supplying `resume_run_id` retains the same logical run and skips
already successful stages.

This is deliberately not the full R3 E-08 contract. It does not extract project
content, generate the 15 Markdown artifacts, invoke a model or Hook, render a
website, or accept `--open`. See
[`project-understand.md`](project-understand.md).

### Persisted project runs

```python
started = core.project_run_start(
    registration.project_id,
    through_stage="classify",
)
resumed = core.project_run_resume(started.project_id, started.run_id)
report = core.project_run_status(resumed.project_id, resumed.run_id)
```

These E-01 methods expose the persisted, resumable orchestration state machine
without placing parsing or transport behavior in the service. The default Core
stage mapping validates registration, runs the current policy-aware inventory,
and derives classification coverage. Later unimplemented stages remain explicit
`unavailable` results. Run machine state lives only below
`.llmwiki/projects/<project_id>/runs/`; the source project remains read-only.

A retry retains its `run_id`, records interruption/failure history, and does not
execute stages that already reached `succeeded`. See
[`project-run-orchestration.md`](project-run-orchestration.md) for the closed
Schema v1 record, status transitions, CLI, and acceptance evidence. This E-01
addition is not the complete E-08 `project understand` workflow and does not
claim extraction, 15-artifact synthesis, Hook, model, or Web behavior.

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

### Reading priority

```python
priority = core.prioritize(registration.project_id)
```

B-07 consumes the exact current `project-inventory-v4` Manifest and atomically
writes `.llmwiki/projects/<project_id>/indexes/reading-priority.json`. It ranks
ordinary Manifest files only and records the Manifest version, generation,
ordinary-file/byte totals, and SHA-256. The recommendation artifact is separate
from Manifest `file_state`, does not create Manifest v5, and is deterministic
from classification role, path/name signals, and bounded incoming references.
It is not goal-aware and performs no extraction or semantic/LLM reading.

`ReadingPriorityResult` retains local `manifest_file` and `priority_file` paths,
so it is a local/path-bearing result rather than a host-safe DTO. The current MCP
catalog does not expose B-07. See
[`reading-priority.md`](reading-priority.md) for the exact Schema v1 key sets,
limits, promotion-queue semantics, reference rules, and fail-closed safety
contract.

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
python -m tools.project understand ... --json    -> core.project_understand(...)
python -m tools.project context ... --json       -> core.project_context(...)
python -m tools.project context-pack ... --json  -> core.host_context_pack(...)
python -m tools.project inventory ... --json     -> core.scan(...)
python -m tools.project prioritize ... --json    -> core.prioritize(...)
python -m tools.project coverage ... --json      -> core.coverage(...)
python -m tools.project event submit ... --json  -> core.host_event_submit(...)
python -m tools.project event show ... --json    -> core.dirty_path_queue(...)
python -m tools.project reconcile ... --json     -> core.project_reconcile(...)
python -m tools.project source open ... --json   -> core.source_open(...)
```

For JSON commands, the CLI adds top-level `ok: true` to the service
`as_dict()` payload. `context` therefore has exact Core/CLI/MCP data parity.
After removing the CLI `ok` field and the MCP success envelope, `context-pack`
has exact direct-builder/Core/CLI/MCP pack parity. Reconciliation also has exact
Core/CLI/MCP result parity after removing the CLI `ok` field and MCP success
envelope because its result is already path-free. Coverage and source-open MCP
calls use `coverage_view` and `source_open_view`, so they have semantic
report/excerpt parity while intentionally omitting the local CLI's absolute
paths.

## Safety and ownership boundaries

- Machine state stays under `.llmwiki/projects/<project_id>/` in the configured
  workspace. Inventory, reading-priority generation, coverage, run
  mutations/orchestration, and reconciliation serialize through the stable
  `indexes/machine-state.lock`.
- Curated knowledge stays under the configured
  `wiki/projects/<project_id>/`-equivalent knowledge root.
- The registered research source project remains read-only.
- Host-safe DTOs do not rewrite legacy data; Host Context assembly only triggers
  the existing deterministic coverage-report write. H-04 persists only its
  append-only event ledger and rebuildable dirty-path projection. H-07 may also
  persist a fresh run, Manifest/coverage generation, projection repair, and the
  strict reconciliation checkpoint.
- H-07 acknowledges only its starting event snapshot; failures and events that
  arrive after that snapshot remain pending.
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
  tests/test_host_context_pack.py `
  tests/test_project_understand.py `
  tests/test_host_events.py `
  tests/test_project_reconciliation.py
```

The focused B-07 regression is:

```powershell
python -B -m pytest -q -p no:cacheprovider `
  tests/test_reading_priority.py tests/test_project_layout.py
```

On **2026-07-17**, it completed with **45 passed, 3 skipped**. The suite includes current-grounded execution authorization, intrinsic/current-policy tamper rejection, existing-ancestor symlink/reparse protection, post-read/pre-commit source mutation, and exact default-boundary coverage.

The broader regressions cover direct service operations, real CLI delegation,
complete scan-policy
mapping, host-safe DTO redaction, Manifest source-content authorization, exact
source/evidence reopening, Host Context byte accounting and deterministic
truncation, host-event idempotency and projection repair, reconciliation
snapshot acknowledgement and failure retention, output-schema fail-closed
behavior, source-project hash/size/mtime/mode preservation, unchanged curated
knowledge, and the absence of LLM/network/Web calls.

## Explicit non-goals

The accepted deterministic Core slices also include the standalone B-07
reading-priority operation. It does not add an MCP operation, run creation or
stage advancement, extraction, semantic reading, `project_understand`, H-07
reconciliation, or H-05 refresh behavior. The H-04 host-event ledger and
validated H-07 conservative reconciliation boundary remain independent. They do
not yet implement
Hook installation or reliability, H-05 selective extraction/knowledge refresh,
full extraction/synthesis, 15-artifact rendering, Web rendering/`--open`,
Verified Query, or the I-02 task store and planning pipeline. The initial E-08
action and H-07 fallback both stop at the deterministic
`register -> inventory -> classify` prefix. G-07 supplies the minimal MCP
adapter documented in [`research-mcp-server.md`](research-mcp-server.md); query
and plan remain honest unavailable contracts, and later capabilities remain
separate roadmap tasks.
