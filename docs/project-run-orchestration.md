# Persisted Project-Run Orchestration (E-01)

- Status: implemented and locally accepted
- Public state module: `tools.project_runs`
- Orchestrator: `tools.project_orchestrator.ProjectRunOrchestrator`
- Core entry points: `ResearchCoreService.project_run_start`,
  `project_run_resume`, and `project_run_status`
- CLI: `python -m tools.project run ...`
- Validation: `tests/test_project_run_orchestration.py`
- Checkpoint: `checkpoint/e-01-run-orchestrator`

## Scope

E-01 establishes a **persisted, resumable run state machine** and explicit
**stage boundaries for deterministic project-understanding work**. It reuses the
registered project layout and existing Core operations, records versioned
machine state under `.llmwiki/projects/<project_id>/runs/`, and leaves the
source project read-only.

E-01 does not claim that extraction, adaptive reading, 15-artifact synthesis,
planning, Hook integration, Web rendering, or model-driven understanding is
implemented. Missing stage handlers are recorded as `unavailable`; they are
never reported as successful placeholders.

## Canonical stages

Every run contains the complete, closed stage list in this order:

```text
register
→ inventory
→ classify
→ extract
→ adaptive-read
→ synthesize
→ evidence
→ status
→ plan
→ index
→ web-render
```

The stage list is persisted even when only a prefix is requested. This makes
pending, failed, unavailable, and completed work visible in one auditable
report.

## Registration bootstrap and current Core mapping

A project must already be registered before a run can be created. This is
necessary because the run itself is stored below the registered
`<project_id>/runs/` directory. The `register` stage therefore verifies and
records the existing registration; it does not silently create a new project.

The current deterministic Core handlers are:

| E-01 stage | Current Core operation | Persisted artifact |
|---|---|---|
| `register` | `project_context(project_id)` validates the existing registration | `project.yaml` |
| `inventory` | `scan(project_id)` runs the B-02 through B-06 policy-aware inventory chain | `manifest.jsonl` |
| `classify` | `coverage(project_id)` validates and summarizes the classified Manifest | `indexes/coverage-report.json` |

The current `scan` operation performs both inventory and B-05/B-06
classification. E-01 keeps separate stage boundaries without duplicating the
scan: `inventory` produces the classified Manifest and `classify` consumes that
Manifest through the deterministic coverage operation. All later handlers are
truthfully `unavailable` until their roadmap tasks install real implementations.

## Storage and Schema v1

One logical run is stored at:

```text
.llmwiki/projects/<project_id>/runs/<run_id>/run.json
```

`run_id` uses a path-safe UTC-and-random form:

```text
run-YYYYMMDDtHHMMSSffffffz-xxxxxxxxxxxx
```

A new logical run gets a new ID. Resume retains the existing ID. The run record
is a closed Schema v1 object and includes:

- schema, kind, run contract, operation, and optimistic `revision`;
- run status, timestamps, duration, active stage, and optional `through_stage`;
- the project/Manifest snapshot present when the run was created;
- every canonical stage and every attempt in chronological order;
- per-attempt input versions, provider, model, token usage, estimated USD cost,
  error, and artifact references;
- run-level usage, artifacts, and errors derived exactly from the attempt
  ledger.

Deterministic stages persist `provider: null`, `model: null`, zero token usage,
and zero cost. Those fields are explicit rather than omitted or fabricated.
Every new structured record carries `schema_version`. A missing version is
recognized only as legacy v0 and is not rewritten. A future unsupported schema
fails closed through `tools.project_layout.load_versioned_json`.

Artifact paths are normalized project-machine-state-relative POSIX paths. An
absolute path, traversal component, or backslash is rejected. Curated Markdown
is not stored in the run directory.

## State and retry semantics

Run states are:

```text
pending | running | paused | partial | succeeded | failed
```

Stage states are:

```text
pending | running | succeeded | failed | unavailable
```

The orchestrator writes a checkpoint **before** invoking a handler and another
checkpoint after the handler completes. Its behavior is:

1. A `succeeded` stage is never executed again in the same logical run.
2. A `failed` stage is retried when the run resumes; downstream stages remain
   pending until the failure is resolved.
3. A missing handler produces one explicit `unavailable` attempt. Resume does
   not append duplicate unavailable attempts unless a handler has since been
   installed, in which case that stage is retried.
4. `--through <stage>` executes only the requested prefix and leaves the run
   `paused`; resuming without `--through` continues the remaining stages.
5. If all stages finish successfully, the run is `succeeded`. If the complete
   pipeline contains unavailable stages but no failed stages, it is `partial`.
6. Handler exceptions derived from `Exception` are recorded as
   `stage-handler-exception`. `KeyboardInterrupt` and other `BaseException`
   subclasses are not swallowed. The persisted attempt remains `running`;
   resume closes it as `stage-interrupted` and then retries it.
7. Historical failed/interrupted attempts remain in the run-level error and
   usage ledger even after a later retry succeeds.

Persistence uses atomic same-directory replacement. `revision` supplies an
optimistic stale-writer check. It is not a distributed lock: callers must not
resume a run that is still actively executing in another process.

## Core API

```python
from tools.research_core import ResearchCoreService

core = ResearchCoreService(workspace_root=r"E:\ResearchCore")

started = core.project_run_start(
    "study-0123456789ab",
    through_stage="classify",
)

resumed = core.project_run_resume(
    started.project_id,
    started.run_id,
)

report = core.project_run_status(
    resumed.project_id,
    resumed.run_id,
)
```

`project_run_status` is read-only and does not repair or execute a persisted
`running` attempt. Recovery occurs only through explicit resume.

## CLI

```bash
python -m tools.project run start <project_id> --json
python -m tools.project run start <project_id> --through classify --json
python -m tools.project run resume <project_id> <run_id> --json
python -m tools.project run resume <project_id> <run_id> --through evidence --json
python -m tools.project run show <project_id> <run_id> --json
```

The CLI delegates to `ResearchCoreService`; it does not call run persistence or
source traversal directly. JSON failures retain stable Core `reason_code`
values.

## Source and capability boundaries

E-01 writes only to the registered external machine-state tree. Tests snapshot
source file hashes, sizes, timestamps, and modes across start/resume paths and
assert that no source-local `.llmwiki/` or `wiki/` directory is created.

The deterministic E-01 path makes no LLM call, opens no network connection,
opens no browser, and triggers no Hook. The public E-08 one-action
`project understand` command, extraction orchestration, 15 Markdown outputs,
and Web `--open` behavior remain later work.

## Validation

Task acceptance commands:

```bash
python -B -m pytest -q -p no:cacheprovider \
  tests/test_project_run_orchestration.py \
  tests/test_research_core_service.py
python -m ruff check \
  tools/project_runs.py \
  tools/project_orchestrator.py \
  tools/research_core.py \
  tools/project.py \
  tests/test_project_run_orchestration.py
python -B -m pytest -q -p no:cacheprovider
python -B tools/health.py
python -m pip check
git diff --check
```

The dedicated tests cover unique IDs, strict Schema v1 validation, future and
legacy behavior, complete attempt ledgers, unavailable stages, failure retry,
interruption recovery, checkpoint skipping, pause/resume, optimistic revision
conflicts, Core/CLI delegation, process-independent status loading, source
read-only behavior, and absence of LLM/network/Web effects.

## Rollback

Use normal history-preserving rollback:

```bash
git revert <e-01-commit>
```

Do not delete existing run directories during rollback. They are versioned
machine records and should remain available for audit or a later explicit
migration.
