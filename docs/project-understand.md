# Complete Project Understand (E-08, R3 slice)

- Status: implemented deterministic full `project understand` pipeline
- Scope: one-action `register -> inventory -> classify -> extract -> adaptive-read -> synthesize -> evidence -> status -> plan -> index -> web-render`
- Core method: `ResearchCoreService.project_understand(...)`
- CLI: `python -m tools.project understand ...`
- Orchestration: existing E-01 `ProjectRunOrchestrator`
- Durable result: `ProjectRunResult`
- Validation: `tests/test_project_understand.py`, lock-coordination tests, and dependent Core/run suites
- Checkpoint: `checkpoint/e-08-complete-understand`

## Purpose

E-08 is the user-facing one-action entry point for the R3 project-understanding
slice. Given a registered research-project path, it reuses or creates the stable
project identity, runs every currently available deterministic stage, produces
the complete fifteen-entry curated Knowledge Schema package, and publishes a
self-contained read-only HTML index under the run directory.

The canonical stage order is durable and resumable:

```text
register -> inventory -> classify -> extract -> adaptive-read -> synthesize
         -> evidence -> status -> plan -> index -> web-render
```

The default API and CLI run through `web-render`. `through_stage` / `--through`
remain available for bounded prefix execution and recovery. A prefix result is
honest about being paused; it does not mark later stages as successful. Resuming
with the same `run_id` preserves successful stage checkpoints and only executes
remaining stages.

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
    through_stage=None,
)

print(result.project_id, result.run_id, result.status)
print(result.as_dict())
```

The Core facade has no browser, HTTP-server, MCP, Hook, or host-agent dependency.
It writes machine artifacts only below
`.llmwiki/projects/<project_id>/` and delegates curated Markdown persistence to
the existing E-07 renderer and F-05 controlled Markdown path.

### Stage boundaries

The stages are deterministic local orchestration boundaries, not a claim that
all source semantics have been understood:

| Stage | Deterministic output | Explicit limitation |
|---|---|---|
| `register` | stable project context | registration is not a scan |
| `inventory` | current Manifest | source project remains read-only |
| `classify` | coverage report | status/reason is classification state |
| `extract` | project-map and stage report | no invented semantic observations |
| `adaptive-read` | reading-priority and hierarchy artifacts | no LLM or semantic file reading |
| `synthesize` | execution/linkage/experiment-chain candidates | host observations remain separate |
| `evidence` | Evidence readiness report | no Evidence is invented without locators |
| `status` | reconstructable machine-state summary | user confirmation is not fabricated |
| `plan` | bounded `DRAFT` suggestions | not a mature autonomous planner |
| `index` | fifteen Markdown product entries and unified index | missing grounding stays `DRAFT` |
| `web-render` | run-local self-contained HTML | read-only static view |

The pipeline does not implement C-07. Scientific binary files remain covered by
the existing classification/policy boundary and are not semantically opened,
loaded, or sent externally. External model calls are not required for this
slice.

## Registration and onboarding options

The one-action API accepts the same first-registration controls as
`ResearchCoreService.register`:

| Core argument | CLI option | Meaning |
|---|---|---|
| `project_id` | `--project-id` | optional explicit path-safe project ID |
| `name` | `--name` | human-readable project name |
| `knowledge_root` | `--knowledge-root` | parent directory for curated knowledge |
| `final_goal` | `--goal` / `--final-goal` | initial final research goal |
| `current_stage` | `--current-stage` | initial research stage |
| `important_question` | `--important-question` | most important initial question |
| `deadline` | `--deadline` | optional ISO date (`YYYY-MM-DD`) |
| `daily_available_hours` | `--daily-hours` | number in `(0, 24]` |

Registration is idempotent for the same resolved source path. Reuse does not
silently update existing onboarding metadata. A fresh call without
`resume_run_id` creates a new logical run; `--resume-run` continues an existing
run with the same `run_id`.

## CLI

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
    [--through <canonical-stage>]
    [--open]
    [--workspace-root <workspace>]
    [--json]
```

Examples:

```powershell
python -m tools.project understand E:\Projects\study `
  --workspace-root E:\ResearchCore `
  --goal "Reproduce the baseline" `
  --json

python -m tools.project understand E:\Projects\study `
  --workspace-root E:\ResearchCore `
  --resume-run <run_id> `
  --open
```

`--open` is deliberately a CLI-adapter feature. After Core completes the
`web-render` stage, the CLI asks the platform default browser to open the
run-local `web/index.html`. Core never launches a browser. A missing page,
failed browser adapter, or a browser returning failure maps to the stable
`project-understand-browser-unavailable` error and does not create an alternate
web state.

## Outputs and storage boundary

A successful run has a durable report at:

```text
.llmwiki/projects/<project_id>/runs/<run_id>/run.json
```

Stage reports and JSON artifacts live under the same run directory. The generated
HTML is:

```text
.llmwiki/projects/<project_id>/runs/<run_id>/web/index.html
```

The `index` stage writes the fifteen required entries in the registered
knowledge root (or the configured custom knowledge root), including a dated
`plans/daily/YYYY-MM-DD.md` page and a unified `index.md`. Machine hashes,
indexes, run state, and reports never move into the curated knowledge root.
The source project is not modified.

## Validation

Focused and dependent checks:

```powershell
python -B -m pytest -q -p no:cacheprovider `
  tests/test_project_understand.py `
  tests/test_stable_file_access.py `
  tests/test_project_run_orchestration.py `
  tests/test_research_core_service.py

python -B -m py_compile `
  tools/project.py tools/project_understand.py tools/research_core.py `
  tools/advisory_lock.py tools/stable_file_access.py

python -m ruff check `
  tools/project.py tools/project_understand.py tools/research_core.py `
  tools/advisory_lock.py tools/stable_file_access.py `
  tests/test_project_understand.py tests/test_stable_file_access.py
```

Repository acceptance also runs the full test suite, health, dependency, and
Git whitespace checks. The E-08 assertions cover fresh/reused registration,
full stage order, prefix execution, resume, fifteen curated outputs, the static
web artifact, CLI/Core JSON parity, browser isolation, lock composition, and
source-tree preservation.

## Explicit non-goals

E-08 does not claim any of the following:

- C-07 scientific-binary metadata extraction;
- semantic or LLM-driven reading of every source file;
- Verified Query or a mature planning engine;
- incremental selective extraction or automatic stale propagation;
- a product Web dashboard or Web editing API (those are J-03/J-04);
- browser, HTTP, MCP, Hook, or Plugin behavior inside Core;
- external transmission of source content.

## Rollback

Use history-preserving rollback for the implementation commit:

```text
git revert <e-08-commit>
```

Do not delete registered project data or run artifacts during rollback. They are
machine-state records owned by the underlying project and run contracts.
