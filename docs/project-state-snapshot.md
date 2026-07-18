# I-03 Project-state snapshot

I-03 defines the deterministic, rebuildable machine-state snapshot used by later
planning and product views.  It summarizes current registered state; it is not a
second source of scientific truth and it does not infer user intent.

## Artifact and identity

The current artifact is written only beneath the registered project machine root:

```text
.llmwiki/projects/<project_id>/indexes/project-state.json
```

It has `schema_version: 1`, `kind: llmwiki-project-state`, and
`state_version: project-state-v1`.  The stable `artifact_id` is a SHA-256 identity
over the complete canonical payload other than the ID itself.  The payload binds:

- the safe, path-free registration fields and exact `project.yaml` SHA-256;
- the exact current `project-inventory-v4` Manifest generation, ordinary-file
  count, byte count, and SHA-256;
- each available input artifact by its current version, item count, and SHA-256.

Malformed UTF-8/JSON, duplicate keys, unknown fields, legacy/future schema
versions, noncanonical JSON, mismatched stable identities, and stale registration
or Manifest bindings fail closed.

## Deterministic summary

The snapshot reads only already-registered machine artifacts and strict current
Knowledge Schema v2 Markdown.  It emits bounded, sorted collections for:

- experiments and results from current Knowledge pages and E-06 experiment chains;
- open questions from canonical Knowledge pages;
- blockers from draft/blocked goals, milestones, tasks, and failed/blocked runs;
- stale Knowledge pages;
- Evidence records whose Source/version binding is not current;
- recent changes across Knowledge, goals, milestones, tasks, Evidence, and runs.

Optional inputs are explicit.  Missing Knowledge, experiment chains, Source or
Evidence registries, Goal, tasks, or run history produce stable `gaps` and a
corresponding `inputs.<name>.status = missing`; absence is never reported as a
successful empty analysis.

Knowledge input is parsed from exact bytes with the strict current Schema v2
contract and canonical path/type checks.  Schema v1 pages are not upgraded or
accepted as current input.  Source and Evidence currentness is computed from the
registries; project-state generation does not reopen the registered source project.

## Storage API

```python
from tools.research_state import generate_project_state, load_project_state

result = generate_project_state(
    workspace_root,
    project_id,
    generated_at="2026-07-19T00:00:00Z",
)
state = load_project_state(workspace_root, project_id)
```

`generate_project_state(...)` acquires the shared `machine-state.lock`, snapshots
the current registration and Manifest, builds the complete payload in memory,
rechecks those exact bytes immediately before publication, and atomically replaces
only `indexes/project-state.json`.  `load_project_state(...)` and
`ProjectStateStore.load()` independently bind the persisted snapshot to the current
registration and Manifest.  `ProjectStateStore.write()` rejects a snapshot bound
to older registration or Manifest bytes.

A caller-supplied fixed `generated_at` makes repeated rebuilds byte-identical when
all inputs are unchanged.  The normal default is the current UTC time, so a new
snapshot revision remains explicit.

## Boundaries and non-goals

I-03 does not:

- read or modify the registered source project;
- read or extract C-07 research binaries;
- write curated Markdown or infer scientific truth, Evidence stance, or user
  confirmation;
- mutate Goal, task, Source, Evidence, run, or Knowledge records;
- add CLI, MCP, Hook, Web, or `ResearchCoreService` behavior;
- implement I-04 initial planning, I-05 mature prioritization, stale propagation,
  or Verified Query.

Validation on **2026-07-19** produced **9 passed** in the focused suite and
**104 passed, 3 skipped** across the dependent Goal/task/experiment-chain/
Knowledge/layout/Source suites.  Changed-file Ruff, `py_compile`, and
`git diff --check` passed.  C-07 remains `deferred/not_started`.
