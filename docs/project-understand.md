# Deterministic Project Understand (E-08, Initial R2 Slice)

- Status: implemented initial deterministic R2 slice
- Scope: deterministic `register -> inventory -> classify` entry point only
- Core method: `ResearchCoreService.project_understand(...)`
- CLI: `python -m tools.project understand ...`
- Orchestration: existing E-01 `ProjectRunOrchestrator`
- Durable result: `ProjectRunResult`
- Validation: `tests/test_project_understand.py` plus E-01/Core regressions
- Checkpoint: `checkpoint/e-08-deterministic-understand`

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
- Hook or Plugin behavior;
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
