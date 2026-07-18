# Goals and milestones (I-01)

I-01 defines the strict, project-scoped Goal/Milestone contract used by later
planning work. It records research direction without treating missing onboarding
answers as user confirmation.

## Storage boundary

The canonical machine artifact is:

```text
.llmwiki/projects/<project_id>/indexes/goals.json
```

It is Schema v1 with:

- `kind = llmwiki-research-goal`;
- `goal_version = research-goal-v1`;
- one stable `project_id` and `goal_id`;
- goal text, success criteria, deadline, current stage, dependencies, milestones,
  lifecycle status, draft reasons, and canonical UTC timestamps.

The human-readable projection is the current Knowledge Schema v2 singleton:

```text
wiki/projects/<project_id>/goals.md
```

A custom registered knowledge root is honored. Machine hashes, registration
paths, locks, and run state remain outside the Markdown page. `GoalStore` writes
only the machine artifact. A curated `goals.md` update must be composed through
the F-05A/F-05B controlled Markdown boundary; I-01 does not bypass that writer.

## Closed statuses

Goal status is one of:

```text
draft | active | blocked | completed | cancelled
```

Milestone status is one of:

```text
draft | pending | active | blocked | completed | cancelled
```

A non-draft Goal requires a non-empty goal, at least one success criterion, an
ISO `YYYY-MM-DD` deadline, and a current stage. A non-draft Milestone requires
success criteria, deadline, and current stage. Dependencies and Milestone IDs are
path-safe, duplicate-free identifiers; self-dependencies are rejected.

## Draft semantics

Missing onboarding fields do not block project understanding. Call
`Goal.draft(...)` to create an explicit draft. The factory records stable reasons
such as:

```text
missing-goal
missing-success_criteria
missing-deadline
missing-current_stage
awaiting-user-confirmation
```

Even a field-complete draft remains `draft` until the host records an authorized
user decision. Core does not infer confirmation from presence of text.

## Strict parsing and serialization

`parse_goal(...)` accepts strict UTF-8 JSON bytes, text, or an already decoded
mapping. It rejects duplicate JSON keys, non-finite numbers, unknown or missing
fields, invalid types, malformed timestamps or dates, unsupported legacy/future
schema versions, duplicate dependencies, duplicate Milestone IDs, and project
identity mismatches. Unsupported versions fail closed; no migration or rewrite is
performed.

`serialize_goal(...)` emits deterministic UTF-8-compatible JSON. The Markdown
projection contains exactly one canonical embedded Goal JSON block, so
`goal_to_markdown(...)` and `goal_from_markdown(...)` round-trip the structured
record without extending Knowledge Schema v2 frontmatter.

A mixed `goals.md` page contains the protected F-05 user region:

```text
<!-- llmwiki:user-region:start id="user-goals" -->
...
<!-- llmwiki:user-region:end id="user-goals" -->
```

Regeneration must preserve those exact user-region bytes through F-05.

## Registered storage API

```python
from tools.research_goals import Goal, GoalStore

goal = Goal.draft(
    project_id,
    goal="Reproduce the baseline",
    created_at="2026-07-18T08:00:00Z",
)
store = GoalStore(workspace_root, project_id)
store.write(goal)
loaded = store.load()
```

`GoalStore` reloads registration under the shared `machine-state.lock`, verifies
that machine and knowledge roots did not change, validates the target beneath the
registered machine root, and performs a stable atomic write. Reads use stable,
non-redirected regular-file access. Source project files are never modified or
sent externally.

## Explicit non-goals

I-01 does not:

- infer a scientific goal, success criterion, deadline, or user confirmation;
- validate the later I-02 executable task graph;
- build the I-03 project-state snapshot;
- initialize the I-04 backlog or daily plan;
- persist curated Markdown outside F-05;
- expose a Web editor, MCP operation, Hook, or external send;
- read or extract C-07 research binaries.
