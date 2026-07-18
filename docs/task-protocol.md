# I-02 Task Protocol

I-02 defines the internal, host-neutral protocol used to turn a proposed Todo
into an executable research task.  It is deliberately a stricter boundary than
a checklist: a task is not executable merely because it has a title or because
an agent says that it is complete.

## Machine artifact

The current artifact is written only below the registered project machine state:

```text
.llmwiki/projects/<project_id>/indexes/tasks.json
```

The document has `schema_version: 1`, `kind: llmwiki-research-tasks`, and
`task_version: research-task-v1`.  It contains one project ID, an acyclic task
collection, and a canonical UTC `updated_at`.  All writes use the registered
project layout, stable regular-file access, and the shared `machine-state.lock`.
The TaskStore never writes curated Markdown and never opens the registered
source project.

Each current task contains:

```text
task_id, title, why_now, inputs, evidence,
allowed_paths, denied_paths, dependencies,
dod, verification, artifacts, timebox_minutes,
status, completion_refs, draft_reasons,
created_at, updated_at
```

The closed status set is `draft`, `ready`, `in_progress`, `blocked`,
`completed`, and `cancelled`.  `ready` and `in_progress` are executable states
only after the host separately performs its authorization and policy checks.
The Core does not infer user intent or semantic priority.

## Safety rules

* `why_now`, inputs, allowed paths, DoD, verification, and artifacts are
  structural requirements for a current task.  `draft` means that confirmation
  is still missing; it does not waive those requirements.
* Evidence entries use canonical `evd-` plus 64 lowercase hexadecimal digits.
* Dependencies are duplicate-free, same-collection IDs, cannot self-reference,
  and must form an acyclic graph.
* Allowed and denied scopes are project-relative POSIX paths or bounded globs.
  Absolute paths, drive letters, backslashes, empty segments, `.` and `..` are
  rejected.  Artifacts are either such paths or a controlled logical namespace.
* A `completed` task must carry explicit controlled `completion_refs` such as
  `test:...`, `artifact:...`, `run:...`, `git:...`, or a canonical Evidence
  reference.  Completion is never accepted from a status string alone.

## Compatibility

Legacy Todo records are read only through the explicit compatibility reader.
The aliases `definition_of_done`, `expected_artifacts`, `forbidden_paths`, and
`timebox` are accepted there, but the result is a non-executable `draft` view.
Legacy `done`/completed statuses receive
`legacy-completion-unverified`; other old statuses receive
`legacy-status-unconfirmed`.  A legacy collection cannot be serialized as the
current artifact, so no silent migration or rewrite occurs.

## Markdown projection

The optional human-readable projection is:

```text
wiki/projects/<project_id>/plans/backlog.md
```

It is Knowledge Schema v2, `artifact_type: plan`, and a mixed page.  The task
collection is stored in exactly one canonical JSON block.  The bounded
`llmwiki:user-region` for `user-backlog` is preserved byte-for-byte by the
controlled Markdown layer; TaskStore does not write this page directly.

The projection is visibly `DRAFT` when the collection is empty or contains a
draft task.  A valid projection is not an authorization to execute a task.
