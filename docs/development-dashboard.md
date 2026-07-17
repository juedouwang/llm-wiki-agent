# J-03A Development Supervision Dashboard

Status: **dashboard foundation implemented; the full J-03 research-product dashboard remains incomplete.**

J-03A provides a local visual control surface for supervising development of the research-assistant roadmap. It is deliberately separate from the future J-03 product dashboard that will browse registered research projects, the 15 curated artifact classes, coverage, Evidence, runs, and user tasks.

## Start the dashboard

From the repository root:

```bash
python -B tools/development_dashboard.py serve
```

Then open:

```text
http://127.0.0.1:8765/
```

Choose another loopback port when needed:

```bash
python -B tools/development_dashboard.py serve --host 127.0.0.1 --port 8876
```

The server rejects non-loopback bind addresses. `--open` may be added to open the local URL in the default browser.

To inspect the same data without starting a server:

```bash
python -B tools/development_dashboard.py snapshot --pretty
```

## What is visible

The dashboard refreshes every ten seconds and shows:

- repository branch, HEAD, clean/dirty state, and ahead/behind counts;
- completed, partial, active, blocked, and not-started task totals;
- the currently authorized unit and its latest validation record;
- the R0 through R6 milestone track, including accepted milestones and progress;
- searchable and filterable roadmap tasks;
- each task's acceptance condition and before/after capability delta;
- task-related commits, checkpoint tags, and file addition/deletion totals;
- an append-only local authorization and validation timeline.

The visual states are intentionally distinct:

| State | Meaning |
|---|---|
| `completed` | The roadmap records the whole task as complete. |
| `partial` | A phase/slice or checkpoint exists, but the whole roadmap task is not complete. |
| `authorized` | The user approved the unit and implementation has not changed the worktree yet. |
| `in_progress` | The authorized unit has active work or an explicit in-progress record. |
| `awaiting_review` | Work is committed/ahead or explicitly submitted for review. |
| `needs_changes` | Validation failed and correction is required. |
| `blocked` | An explicit blocking condition is recorded. |
| `not_started` | No completion, checkpoint, or active authorization is visible. |

## Authorization gate

The dashboard does not authorize work by itself. The collaboration rule is:

1. propose one bounded unit and its key decisions;
2. obtain explicit user approval;
3. record authorization locally;
4. implement and validate only that unit;
5. stop after reporting the result, unless the user separately authorizes the next unit.

A progress or validation event can be recorded with:

```bash
python -B tools/development_dashboard.py record   --task-id J-03   --unit-id J-03A   --status awaiting_review   --summary "Dashboard foundation is ready for review."   --check pytest=passed:"dashboard tests passed"   --check health=passed:"repository health passed"
```

Supported progress states are `authorized`, `in_progress`, `awaiting_review`, `passed`, `failed`, and `blocked`. Check states are `pending`, `passed`, `failed`, and `skipped`.

## Data sources and storage

The snapshot combines three local sources:

1. `docs/research-assistant-roadmap.md` for task definitions, completion records, capability deltas, and milestones;
2. read-only Git commands for branch, commit, checkpoint, and file-delta evidence;
3. `.llmwiki/development-dashboard/progress.json` for bounded local authorization and validation records.

The progress ledger is Schema v1 machine state. It is ignored by Git, written atomically, and constrained to the repository's `.llmwiki/` directory. Missing or unsupported schema versions fail closed. No machine path, hash index, or run state is written into curated `wiki/` content.

## Security and source boundaries

J-03A has the following hard boundaries:

- binds only to an IP loopback address and defaults to `127.0.0.1`;
- accepts only well-formed loopback `Host` headers;
- exposes only fixed static assets plus `/api/status` and `/api/health`;
- rejects POST, PUT, PATCH, DELETE, and OPTIONS with a read-only response;
- has no arbitrary file-read endpoint;
- sends no data to an external service and loads no CDN assets;
- applies a restrictive Content Security Policy and related browser security headers;
- removes the repository absolute path from public snapshots;
- does not register, inventory, extract, or read a research source project.

The server is a development aid, not an authentication boundary and not a network service. Do not expose it through a public interface or reverse proxy.

## J-03 scope still open

This checkpoint must remain a **partial J-03 result**. Later, separately authorized J-03 units still need to add the actual research-product views for:

- registered project selection and project context;
- the complete 15-class research artifact package;
- coverage and failure details;
- source and Evidence navigation;
- extraction/understanding run history;
- research goals, plans, and tasks;
- links back to curated Markdown and precise Evidence locators.

J-03A therefore improves development supervision without claiming that the roadmap's full J-03 acceptance condition has been met.
