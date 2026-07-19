# Initial Research Planning (I-04)

- Status: implemented on **2026-07-19**
- Core module: `tools.research_planning`
- Machine artifact: `.llmwiki/projects/<project_id>/indexes/initial-plan.json`
- Service operation: `ResearchCoreService.plan(...)`
- MCP operation: `llmwiki_plan`
- Tests: `tests/test_research_planning.py`, `tests/test_research_state.py`,
  `tests/test_knowledge_renderer.py`, `tests/test_research_mcp_server.py`, and
  `tests/test_codex_plugin.py`

## Scope

I-04 supplies the first-run planning slice required by the R3 fifteen-artifact
contract. It deterministically creates or loads a project Goal and task backlog,
binds a dated initial plan to the current project-state snapshot, and leaves every
automatically proposed item visibly `DRAFT` and non-executable. It is deliberately
smaller than the later I-05 mature planner: it does not optimize priorities,
deadlines, dependencies, time budgets, risk, or scientific value, and it does not
authorize an Agent to execute a task.

The machine and human-readable layers remain separate. I-04 owns strict machine
state; E-07 plus F-05A/F-05B separately render and persist `goals.md`,
`plans/backlog.md`, and `plans/daily/<date>.md`.

## Machine artifacts

Planning uses four current Schema v1 artifacts below the registered project machine
root:

```text
indexes/goals.json         # I-01 Goal/Milestone store
indexes/tasks.json         # I-02 task collection
indexes/project-state.json # I-03 deterministic status snapshot
indexes/initial-plan.json  # I-04 dated draft plan
```

The I-04 artifact has:

```text
schema_version = 1
kind = llmwiki-initial-plan
plan_version = initial-plan-v1
status = draft
```

It records a canonical UTC `generated_at`, `plan_date`, the Goal ID, exact
project-state artifact ID, ordered task IDs, bounded `why_now`, timebox, machine
inputs, intended Markdown outputs, verification steps, and explicit blockers. It
contains project-relative artifact names only: absolute paths, storage roots,
Manifest hashes, and source content are rejected.

Parsing and serialization are strict canonical JSON. Invalid UTF-8, duplicate keys,
unknown fields, legacy or future schema versions, noncanonical bytes, invalid IDs,
path injection, inconsistent task/Goal/state bindings, and stale registration or
Manifest state fail closed.

## Deterministic generation

`generate_initial_plan(...)` executes under the shared per-project
`indexes/machine-state.lock` and uses exact compare-and-swap publication. It:

1. reloads the current registration and Manifest;
2. generates or refreshes the I-03 project-state snapshot;
3. strictly loads an existing Goal/task collection without rewriting either one;
4. when absent, creates a bounded Goal and backlog as explicit drafts;
5. builds the dated `initial-plan-v1` payload from those exact revisions;
6. revalidates all inputs immediately before publishing only
   `indexes/initial-plan.json`.

A caller-supplied `objective` takes precedence over the registration onboarding
`final_goal`. If neither exists, the Goal records a stable missing-objective draft
reason. Onboarding `important_question` remains context only and is never promoted
into a success criterion. Automatically generated tasks retain the complete I-02
shape but are `draft`, have explicit verification/DoD, and remain
`executable = false` until a separate authorized workflow changes them.

Existing Goal and task bytes are preserved byte-for-byte. I-04 does not silently
mature, rewrite, or mark them complete.

## Service, understand stage, and rendering

The host-independent facade is:

```python
result = core.plan(
    project_id,
    objective="Reproduce the baseline result",  # optional
    generated_at="2026-07-19T00:00:00Z",       # optional deterministic clock
    plan_date="2026-07-19",                    # optional
)
```

The complete E-08 `project understand` runner invokes the same Core planning
boundary at its `plan` stage. Its subsequent `index` stage calls the E-07 renderer,
which consumes the current I-01 through I-04 machine artifacts. The renderer emits
real Goal/backlog/daily-plan content when those artifacts are current and otherwise
emits explicit DRAFT gaps; it never leaks machine paths, hashes, or internal
Evidence identifiers. Persisted mixed pages preserve the exact bytes inside
`llmwiki:user-region` blocks through F-05A/F-05B.

The MCP adapter now exposes `llmwiki_plan` as a real mutating Core operation. Its
closed Schema v1 result is host-safe and content-free. `llmwiki_query` remains an
honest `capability-unavailable` operation with `available_after=G-04`; I-04 does
not claim Verified Query.

## Boundaries and non-goals

I-04 does not:

- read or modify registered research-source bytes;
- read or extract C-07 research binaries;
- call an LLM or send content externally;
- write curated Markdown directly or bypass F-05A/F-05B;
- infer scientific truth, user confirmation, Evidence stance, or conflict status;
- authorize task execution or claim completion;
- implement I-05 mature daily planning, I-06 execution verification, stale
  propagation, selective refresh, or Verified Query.

Validation on **2026-07-19** produced **71 passed** across the focused
I-03/I-04/renderer/MCP/Codex suites and **794 passed, 28 skipped** in full
regression. Changed-file Ruff, isolated `py_compile`, UTF-8 health, and
`git diff --check` passed. C-07 remains `deferred/not_started`.
