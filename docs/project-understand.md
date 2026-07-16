# Deterministic Project Understand (E-08, Initial R2 Slice)

- Status: implemented initial deterministic R2 slice
- Scope: deterministic `register -> inventory -> classify` entry point only
- Core method: `ResearchCoreService.project_understand(...)`
- CLI: `python -m tools.project understand ...`
- Orchestration: existing E-01 `ProjectRunOrchestrator`
- Durable result: `ProjectRunResult`
- Validation: `tests/test_project_understand.py` plus E-01/Core regressions
- Checkpoint: `checkpoint/e-08-deterministic-understand`
- H-07 reuse: [`project-reconciliation.md`](project-reconciliation.md)

## Purpose

This E-08 slice exposes the deterministic prefix that already exists behind the
E-01 run orchestrator as **one user action**. Given a research-project path, the
action idempotently registers or reuses that path and executes the canonical
run only through `classify`:

```text
register -> inventory -> classify
```

It does not duplicate the E-01 state machine, stage handlers, checkpointing, or
retry rules. The complete canonical stage list is still present in the durable
run report, but this entry point deliberately pauses before `extract`.

## Core API

```python
from tools.research_core import ResearchCoreService

core = ResearchCoreService(workspace_root=r"E:\ResearchCore")

result = core.project_understand(
    project_root=r"E:\Projects\study",
    project_id=None,
    name=None,
    knowledge_root=None,
    final_goal=None,
    current_stage=None,
    important_question=None,
    deadline=None,
    daily_available_hours=None,
    resume_run_id=None,
)

print(result.project_id)
print(result.run_id)
print(result.status)
print(result.record)
```

`project_understand` returns the same local, path-bearing `ProjectRunResult`
used by the E-01 run APIs. Its `as_dict()` projection contains the `project_id`,
`run_id`, durable `run_file`, and complete `run` report.

### Registration and onboarding options

The one-action API accepts the same first-registration controls as
`ResearchCoreService.register`:

| Core argument | CLI option | Meaning |
|---|---|---|
| `project_id` | `--project-id` | Optional explicit, path-safe project ID |
| `name` | `--name` | Human-readable project name |
| `knowledge_root` | `--knowledge-root` | Parent directory for curated per-project knowledge |
| `final_goal` | `--goal` / `--final-goal` | Initial final research goal |
| `current_stage` | `--current-stage` | Initial research stage |
| `important_question` | `--important-question` | Most important initial question |
| `deadline` | `--deadline` | Optional ISO date, `YYYY-MM-DD` |
| `daily_available_hours` | `--daily-hours` | Available hours per day, greater than 0 and at most 24 |

Registration is idempotent for the same resolved project path. If the path is
already registered, its existing identity and storage layout are reused rather
than rewritten. Explicit options supplied on reuse must agree with the existing
registration; this command is not a metadata-update operation.

Registration reuse and run reuse are separate. A call without `resume_run_id`
starts a new logical run and therefore gets a new `run_id`, even when the
project registration already exists.

## CLI

Synopsis:

```text
python -m tools.project understand <project-path>
    [--project-id <project_id>]
    [--name <name>]
    [--knowledge-root <projects-root>]
    [--goal <goal> | --final-goal <goal>]
    [--current-stage <stage>]
    [--important-question <question>]
    [--deadline YYYY-MM-DD]
    [--daily-hours <hours>]
    [--resume-run <run_id>]
    [--workspace-root <workspace>]
    [--json]
```

Start a fresh deterministic run:

```powershell
python -m tools.project understand E:\Projects\study `
  --workspace-root E:\ResearchCore `
  --goal "Reproduce the reported result" `
  --json
```

Resume the same logical run:

```powershell
python -m tools.project understand E:\Projects\study `
  --workspace-root E:\ResearchCore `
  --resume-run run-YYYYMMDDtHHMMSSffffffz-xxxxxxxxxxxx `
  --json
```

The CLI delegates directly to `ResearchCoreService.project_understand`; it does
not perform registration, source traversal, run persistence, or stage execution
inside the transport layer.

## Deterministic stage mapping

The entry point always asks E-01 to run through `classify`.

| Stage | Existing deterministic Core operation | Expected durable state after a successful fresh action |
|---|---|---|
| `register` | Revalidate the newly created or reused registration with `project_context` | `succeeded` |
| `inventory` | Run the B-02 through B-06 policy-aware `scan` chain | `succeeded` |
| `classify` | Generate deterministic classification coverage with `coverage` | `succeeded` |
| `extract` through `web-render` | Not invoked by this slice | `pending` |

The inventory handler already performs deterministic file classification while
writing the current Manifest. The separate `classify` stage consumes that
Manifest through the existing coverage operation; E-08 does not add another
scanner or classifier.

A complete `ProjectRunOrchestrator.start` or `resume` execution holds the stable
per-project `indexes/machine-state.lock`. Run creation/save, inventory, and
coverage acquire the same lock defensively and reenter it on the same thread.
This serializes same-project writers across every stage while leaving different
projects independent. The lock file is a persistent coordination artifact.

## Relationship to H-07 reconciliation

Validated on 2026-07-16, H-07 reuses this exact three-stage deterministic prefix
as the correctness fallback for an already registered project. The two entry
points have different responsibilities:

| Entry point | Identity input | Event boundary | Durable result |
|---|---|---|---|
| `project_understand(project_root, ...)` | source-project path; register or reuse identity | none | resumable E-01 run paused after `classify` |
| `project_reconcile(project_id, ...)` | existing registered `project_id` | snapshot and later acknowledge only the starting H-04 event prefix | fresh E-01 run plus strict `indexes/reconciliation-state.json` checkpoint |

H-07 calls the full prefix even when Hooks are disabled and dirty-path hints are
absent or inconsistent. It does not resume an earlier understand run or use hints
to skip files. If reconciliation fails, its acknowledgement remains unchanged;
if events arrive after the starting snapshot, they remain pending.

This reuse does not expand E-08 beyond `classify`. Neither operation performs
H-05 selective extraction or refreshes curated knowledge. See
[`project-reconciliation.md`](project-reconciliation.md).

## Result and resume contract

After a successful fresh action:

- `result.run_id` identifies a newly created logical run;
- `result.status` and `result.record["status"]` are `paused`;
- `result.record["through_stage"]` is `classify`;
- `register`, `inventory`, and `classify` are `succeeded`;
- every later canonical stage remains `pending`, not falsely reported as
  successful or unavailable;
- the complete report is persisted at
  `.llmwiki/projects/<project_id>/runs/<run_id>/run.json`.

`resume_run_id` switches the operation from E-01 start to E-01 resume while
retaining the same `run_id`. Succeeded stages are skipped and do not receive
duplicate attempts. A failed or interrupted stage inside the three-stage prefix
is handled by the existing E-01 retry and recovery rules. Even on resume, this
E-08 slice still stops at `classify`; it does not advance into later stages.

Registration errors occur before a run can start. Once a run exists, stage
failures remain visible in its durable E-01 report rather than being converted
into a successful placeholder.

## Storage and source boundary

Generated machine state remains under the configured Research Core workspace:

```text
.llmwiki/projects/<project_id>/
|-- project.yaml
|-- manifest.jsonl
|-- indexes/
|   `-- coverage-report.json
`-- runs/
    `-- <run_id>/
        `-- run.json
```

The configured curated tree, normally `wiki/projects/<project_id>/` or the
corresponding directory below `--knowledge-root`, is only reserved by project
registration. This slice does not write curated summaries, plans, evidence
pages, or any of the planned research artifacts there.

When H-07 invokes the same stage prefix, it may additionally write the H-04
projection repair and strict
`.llmwiki/projects/<project_id>/indexes/reconciliation-state.json` checkpoint.
Those are machine-state effects of reconciliation, not effects of the
`project_understand` entry point, and curated knowledge remains unchanged.

The research source directory remains read-only. The deterministic action may
read source metadata and policy-authorized file content for inventory and
classification, but it does not create source-local `.llmwiki/`, `wiki/`, or
legacy `<project-name>-wiki/` output and does not modify source files.

## Explicit non-goals

This initial R2 slice is **not** any of the following:

- extraction or extracted-document orchestration;
- adaptive reading;
- synthesis or generation of the planned 15 Markdown artifacts;
- Evidence, status, planning, or indexing pipeline completion;
- Web rendering, a local dashboard, browser launch, or `--open` support;
- Hook or Plugin wiring; explicit H-07 reconciliation exists separately and does
  not install or trust Hooks;
- H-05 selective extraction or selective knowledge refresh;
- model-, provider-, or LLM-driven project understanding.

No later stage is simulated merely to make the run look complete. Those
capabilities remain later roadmap tasks and must install real handlers before
they can advance the same persisted pipeline.

## Validation

Run the existing orchestration/Core regressions and the E-08 CLI smoke check:

```powershell
python -B -m pytest -q -p no:cacheprovider `
  tests/test_project_understand.py `
  tests/test_project_run_orchestration.py `
  tests/test_research_core_service.py

python -B -m tools.project understand --help

python -m ruff check `
  tools/project.py `
  tools/research_core.py `
  tests/test_project_understand.py `
  tests/test_project_run_orchestration.py `
  tests/test_research_core_service.py
```

Then run repository-level acceptance checks:

```powershell
python -B -m pytest -q -p no:cacheprovider
$env:PYTHONIOENCODING = "utf-8"; python -B tools/health.py
python -m pip check
git diff --check
```

The E-08-focused assertions should verify fresh registration, registration
reuse, onboarding-option delegation, a new `run_id` for each fresh action,
`paused`/`classify` state, exactly three succeeded stages, pending later stages,
resume with the same `run_id`, skipped succeeded stages, JSON CLI parity, and
source-tree preservation.

## Rollback

Create the accepted checkpoint as:

```text
checkpoint/e-08-deterministic-understand
```

Use normal history-preserving rollback for the E-08 implementation commit:

```powershell
git revert <e-08-commit>
```

Do not delete existing project registrations, Manifests, coverage reports, or
run directories during rollback. They are valid versioned machine records from
the underlying B-series and E-01 contracts and should remain available for
audit, status inspection, or a later explicit migration. Reverting E-08 removes
the one-action facade; it does not require source-project cleanup.
