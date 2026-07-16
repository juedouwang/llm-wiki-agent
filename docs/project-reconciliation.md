# Conservative Project Reconciliation Boundary (H-07)

- Roadmap slice: H-07
- Status: implemented and complete after validation on 2026-07-16
- Domain module: `tools.project_reconciliation`
- Core facade: `ResearchCoreService.project_reconcile(...)`
- CLI: `python -m tools.project reconcile ...`
- MCP tool: `llmwiki_reconcile`
- Durable state: `.llmwiki/projects/<project_id>/indexes/reconciliation-state.json`
- Related input ledger: [`host-event-ledger.md`](host-event-ledger.md)
- Deterministic run boundary: [`project-understand.md`](project-understand.md)
- Checkpoint: `checkpoint/h-07-project-reconciliation`

## Purpose and correctness boundary

H-07 provides one conservative synchronization operation for an already
registered research project. It is callable directly through Core, the local
CLI, or MCP and does not depend on a Hook, filesystem watcher, host session, or
usable dirty-path list.

Every successful call uses the same correctness fallback:

```text
snapshot host-event ledger
        |
        v
register -> inventory -> classify
        |
        v
validate Manifest + coverage artifacts
        |
        v
acknowledge only the starting ledger snapshot
```

The full project scan through `classify` is authoritative. Host events and
explicit dirty paths are untrusted latency hints only. They may describe what a
host believes changed, but they never narrow the scan, authorize content
access, prove current file state, or determine whether reconciliation is
correct.

H-07 is therefore useful in all of these cases:

- Hooks are disabled, unsupported, untrusted, or not installed;
- no host event has been submitted;
- no explicit dirty path is supplied;
- the event-derived and explicitly supplied path sets agree;
- the two hint sets disagree or appear incomplete.

An absent or inconsistent hint set is reported, not treated as an error. A
lexically invalid explicit path still fails closed before scanning.

## Transaction sequence

One reconciliation holds the stable per-project advisory lock at
`indexes/machine-state.lock`. Inventory, coverage generation, run creation/save,
whole run orchestration, and reconciliation share this same coordination point,
so cooperating machine-state writers cannot replace one another's artifacts. The
lock file remains on disk after release. Host events retain their separate
`events.jsonl.lock`, and the only permitted nested order is `machine-state.lock`
then `events.jsonl.lock`. The operation performs these steps in order:

1. Load the registered project and strict reconciliation state. A missing state
   file means no event prefix has been acknowledged yet.
2. Under the H-04 event-ledger lock, validate `events.jsonl`, take one exact
   starting snapshot, and repair the rebuildable `dirty-paths.json` projection
   if needed.
3. Verify that any previously acknowledged ledger prefix still matches its
   persisted SHA-256.
4. Derive pending event and path counts from the difference between the prior
   acknowledgement and the starting snapshot. Combine those paths with any
   explicit hints for reporting only.
5. Start a fresh E-01 run with `through_stage="classify"`. The required
   `register`, `inventory`, and `classify` stages must all succeed, and the run
   must pause after `classify` with every later stage still pending.
6. Generate and validate the current deterministic coverage artifact. The
   persisted coverage report must match the returned report and the same exact
   Manifest bytes, and `failed_file_count` must be zero. The run's inventory and
   classify artifact records must carry the exact SHA-256 values of those files.
7. Reacquire the event-ledger lock and require the original snapshot to remain
   an exact prefix of the current ledger.
8. Build the next checkpoint, then immediately reread and compare the exact
   Manifest and coverage bytes captured during validation.
9. Atomically write the reconciliation checkpoint for the starting snapshot.
   Events appended after that snapshot are counted as remaining and stay
   pending for a later call.

A successful H-07 result means the deterministic scan/classification boundary
and its exact artifacts were completed and validated with zero coverage failures.
It does not mean every file was deep-read, extracted, or summarized.

## Snapshot acknowledgement and pending events

The acknowledgement watermark is separate from both H-04 artifacts:

- `events.jsonl` remains append-only and is never truncated or rewritten;
- `indexes/dirty-paths.json` remains a deterministic projection of the complete
  ledger and is not used as an authoritative dequeue file;
- `indexes/reconciliation-state.json` records the exact ledger prefix covered by
  the last successful H-07 call.

Only the event snapshot taken before the full scan can be acknowledged. Suppose
reconciliation starts with events 1 through 12 and events 13 and 14 arrive while
the scan is running. On success, the checkpoint advances through sequence 12.
Events 13 and 14 remain pending, and the result reports their event and distinct
path counts.

If the scan, required stage validation, positive coverage-failure count, artifact
hash/byte validation, prefix check, lock, or atomic state write fails, the
acknowledgement does not advance. Events that
were pending at the start remain pending, and events that arrived later also
remain pending. A failed call may leave its E-01 run report or deterministic
scan artifacts for audit, but it must not turn that work into a successful
reconciliation checkpoint.

The next call verifies the previously acknowledged prefix hash before doing new
work. A shortened, rewritten, or otherwise inconsistent event ledger fails
closed rather than silently accepting a different history.

## Hint contract

H-07 reports one of five hint states:

| Status | Event-ledger pending paths | Explicit dirty paths |
|---|---:|---:|
| `absent` | none | none |
| `ledger-only` | present | none |
| `explicit-only` | none | present |
| `consistent` | present | same normalized set |
| `inconsistent` | present | different normalized set |

The status is diagnostic. Every row still uses `mode: full-scan` and
`source_of_truth: manifest-and-hash`.

Core normalizes explicit paths as project-relative POSIX paths, accepts at most
256 supplied path items, and deduplicates them for comparison. The MCP input
schema additionally requires the supplied array itself to contain unique
normalized paths. Neither
Core nor MCP returns the path values in the reconciliation result; only bounded
counts and hashes cross the host result boundary.

## Strict Schema v1 machine state

The only H-07 checkpoint is:

```text
.llmwiki/projects/<project_id>/indexes/reconciliation-state.json
```

It is local machine state, not curated knowledge. Its closed top-level shape is:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-reconciliation-state",
  "reconciliation_version": "project-reconciliation-v1",
  "project_id": "study-0123456789ab",
  "revision": 3,
  "acknowledged_through_sequence": 12,
  "acknowledged_ledger_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "last_success": {
    "run_id": "run-YYYYMMDDtHHMMSSffffffz-xxxxxxxxxxxx",
    "completed_at": "2026-07-16T09:30:00Z",
    "manifest_version": "project-inventory-v4",
    "manifest_scan_generation": 8,
    "manifest_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "coverage_report_version": "coverage-report-v1",
    "coverage_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "coverage_failure_count": 0,
    "snapshot_event_count": 12,
    "snapshot_last_sequence": 12,
    "snapshot_ledger_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "pending_event_count": 4,
    "pending_dirty_path_count": 3,
    "hint_status": "inconsistent",
    "explicit_dirty_path_count": 2,
    "queued_dirty_path_count": 3,
    "projection_rebuilt": false
  }
}
```

The exact implementation validates all fields, versions, counts, timestamps,
and hashes. The example digests are placeholders showing shape only.

State rules are deliberately strict:

- no file is written until the first successful reconciliation;
- every persisted record carries `schema_version: 1`;
- missing `schema_version` is legacy v0 and is rejected without rewrite;
- unsupported future schemas and reconciliation versions fail closed;
- unknown, missing, or malformed fields fail closed; duplicate JSON keys,
  non-finite values, and invalid UTF-8 also fail closed;
- duplicated audit counts must agree, explicit-path counts remain bounded, and
  every persisted hint status must be consistent with its queued/explicit count
  presence;
- a symbolic-link or non-file state path is rejected;
- writes use a same-directory temporary file, flush, and atomic replacement;
- `.reconciliation-state.json.*.tmp` is ephemeral; the shared
  `machine-state.lock` is stable and remains after release.

The checkpoint records acknowledgement and audit metadata. It does not contain
source bytes, excerpts, absolute source paths, curated Markdown, prompts, model
output, or host-specific payloads.

## Core API

```python
from tools.research_core import ResearchCoreService

core = ResearchCoreService(workspace_root=r"E:\ResearchAssistantWorkspace")

result = core.project_reconcile(
    "study-0123456789ab",
    dirty_paths=["src/model.py", "tests/test_model.py"],
)

payload = result.as_dict()
```

`dirty_paths` is optional. Omitting it invokes the same full-scan boundary. The
result is a closed Schema v1 object with:

- `status: reconciled`, `mode: full-scan`, and
  `source_of_truth: manifest-and-hash`;
- the paused run ID and succeeded `register`, `inventory`, and `classify` stages;
- Manifest version, scan generation, and SHA-256;
- coverage version, SHA-256, and failed-file count;
- hint status, snapshot counts/hash, and projection-repair status;
- previous/current acknowledgement sequences, newly acknowledged count,
  remaining post-snapshot counts, state revision, and acknowledged prefix hash.

## CLI

Run reconciliation without relying on Hooks or dirty paths:

```powershell
python -B -m tools.project reconcile study-0123456789ab `
  --workspace-root E:\ResearchAssistantWorkspace `
  --json
```

Optional explicit hints are repeatable:

```powershell
python -B -m tools.project reconcile study-0123456789ab `
  --workspace-root E:\ResearchAssistantWorkspace `
  --dirty-path src/model.py `
  --dirty-path tests/test_model.py `
  --json
```

The CLI delegates directly to `ResearchCoreService.project_reconcile(...)`. It
does not implement its own scan, acknowledgement, event, or state logic.

## MCP

`llmwiki_reconcile` is an available seven-tool-catalog operation, not the former
H-07 `capability-unavailable` placeholder. Its input is:

```json
{
  "project_id": "study-0123456789ab",
  "dirty_paths": ["src/model.py"]
}
```

`dirty_paths` may be omitted. The adapter delegates to the same Core method and
returns the same reconciliation result inside the standard MCP success envelope.
It advertises `readOnlyHint: false` because reconciliation writes deterministic
machine state. It remains non-destructive with respect to the source project and
curated knowledge.

Stable reconciliation failures are mapped to redacted MCP codes including:

- `reconciliation-hint-invalid`;
- `reconciliation-state-invalid`;
- `reconciliation-lock-failed`;
- `reconciliation-run-failed`;
- `reconciliation-snapshot-invalid`;
- `reconciliation-failed`.

The adapter does not echo caller-supplied dirty paths or local exception text.

## Source, machine-state, and knowledge boundaries

H-07 may update only deterministic project machine state, including:

- `manifest.jsonl` and its scan generation;
- `indexes/coverage-report.json`;
- `runs/<run_id>/run.json`;
- a repaired `indexes/dirty-paths.json` projection;
- `indexes/reconciliation-state.json`.

The registered research source tree remains read-only. Inventory and
classification may read metadata and policy-authorized bounded content under the
existing B-02 through B-06 rules, but H-07 never writes source files or creates
source-local `.llmwiki/`, `wiki/`, or legacy `<project-name>-wiki/` output.

The configured curated knowledge tree also remains unchanged. H-07 does not
write summaries, Claims, syntheses, plans, indexes, or Web pages under
`wiki/projects/<project_id>/`.

## Explicit non-goals

H-07 does not implement or claim:

- H-05 selective extraction, selective reconciliation, or selective knowledge
  refresh;
- H-01 Manifest-generation diff reports, H-02 dependency edges, H-03 stale
  propagation, or H-06 deletion/move impact handling;
- extraction, adaptive reading, Evidence generation, 15-artifact synthesis, or
  curated Markdown updates;
- Hook installation, Hook trust, automatic Stop wiring, filesystem watching,
  polling, or a background daemon;
- Verified Query, planning, task completion verification, Web rendering, or
  browser launch;
- proof that dirty paths are complete or that a host event reflects current
  source truth.

Those later capabilities must build on this checkpoint without weakening its
full-scan fallback or snapshot acknowledgement rules.

## Validation, completion, and next task

H-07 is marked complete only after its focused Core/CLI/MCP checks and the
repository-level regression, health, dependency, and diff-hygiene gates pass.
The completion date recorded for this slice is **2026-07-16**.

Validation must cover at least:

- no-Hook/no-hint, ledger-only, explicit-only, consistent, and inconsistent hint
  cases, all using the same full scan;
- exact success through `classify` with later stages still pending;
- strict Schema v1 state, legacy/future rejection, prefix-hash validation, and
  atomic state replacement;
- no acknowledgement after scan, positive coverage-failure count, artifact
  hash/byte, lock, state, or snapshot-prefix failure;
- acknowledgement of only the starting snapshot and retention of events appended
  during the scan;
- Core/CLI/MCP result parity and stable redacted MCP errors;
- unchanged source and curated-knowledge snapshots;
- absence of selective extraction, knowledge refresh, LLM, network, and Web
  behavior.

J-05 now wires this validated H-07 boundary into the Codex reference package at
`plugins/llmwiki-research/`. Its Plugin, Skill, MCP configuration, and optional
Hook preserve the same invariant: Hook signals are untrusted hints, while
explicit `llmwiki_reconcile` remains the correctness path. See
[`codex-reference-adapter.md`](codex-reference-adapter.md). B-07 is the next
executable roadmap task.
