# Host Event Ledger and Dirty-Path Queue (H-04)

- Roadmap slice: H-04
- Authoritative machine artifact: `.llmwiki/projects/<project_id>/events.jsonl`
- Derived machine artifact: `.llmwiki/projects/<project_id>/indexes/dirty-paths.json`
- Domain module: `tools.host_events`
- Core facade: `ResearchCoreService.host_event_submit(...)` and
  `ResearchCoreService.dirty_path_queue(...)`
- CLI: `python -m tools.project event submit ...` and
  `python -m tools.project event show ...`
- Focused validation: `tests/test_host_events.py`
- Checkpoint: `checkpoint/h-04-host-event-ledger`

## Purpose and scope

H-04 gives Codex, Claude Code, and other hosts one versioned, host-neutral way
to report project-relative file-change signals. Core records each accepted signal
once, in ingestion order, then derives a deterministic queue of paths that may
need later reconciliation.

```text
Codex / Claude Code / another host
                 |
                 v
        host_event_submit(...)
                 |
                 v
             events.jsonl       authoritative, append-only
                 |
                 v
      indexes/dirty-paths.json  deterministic, replaceable projection
                 |
                 v
         dirty_path_queue(...)
```

An event is an untrusted hint, not proof of current source state. H-04 does not
stat the submitted path, inventory the project, compare hashes, extract content,
mark knowledge stale, or refresh anything. A host-specific adapter may translate
its native signal into this model, but Core behavior does not branch on whether
`producer` is `codex`, `claude-code`, or another valid producer name.

A project must already be registered. A current Manifest, Source registry, or
Evidence registry is not required because H-04 records signals rather than
verifying source state.

## Storage and authority

H-04 writes only under the registered project machine-state directory:

```text
.llmwiki/projects/<project_id>/
|-- events.jsonl
|-- events.jsonl.lock          # ephemeral local writer lock
`-- indexes/
    |-- dirty-paths.json
    `-- .dirty-paths.json.*.tmp # ephemeral atomic-write temporary
```

The artifacts have deliberately different authority:

1. `events.jsonl` is the authoritative append-only ledger.
2. `indexes/dirty-paths.json` is a disposable projection derived only from the
   complete validated ledger.
3. A lock file and same-directory temporary projection file may exist briefly
   during a write or rebuild. They are coordination artifacts, not event data.
4. The projection must never be used to repair, reorder, or recreate ledger
   events.

Normal submission appends one compact canonical UTF-8 JSON object plus `LF`.
There is no mutable summary row. Accepted lines are never updated in place,
removed because a path later changes again, or reordered by timestamp.
Idempotent duplicates and event-ID collisions append no line and consume no
sequence number.

Every structured record carries `schema_version: 1`. A missing version is legacy
v0, for which H-04 has no compatibility reader. Unsupported future schemas and
artifact versions fail closed and are not silently downgraded or rewritten.

## Host-neutral Schema v1 event

One persisted event has this closed shape:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-host-event",
  "event_version": "host-event-v1",
  "record_type": "dirty-path-event",
  "project_id": "study-0123456789ab",
  "sequence": 7,
  "event_id": "codex:0190f5a4-8c7d-7000-a000-000000000001",
  "producer": "codex",
  "occurred_at": "2026-07-16T09:14:03.123456Z",
  "ingested_at": "2026-07-16T09:14:05.456789Z",
  "operation": "modified",
  "paths": [
    "src/model.py",
    "tests/test_model.py"
  ]
}
```

| Field | Contract |
|---|---|
| `schema_version` | Integer `1`. Missing, non-integer, legacy, and future values fail closed. |
| `kind` | Exactly `llmwiki-host-event`. |
| `event_version` | Exactly `host-event-v1`. |
| `record_type` | Exactly `dirty-path-event`. |
| `project_id` | The registered project that owns this ledger. Every line must match its containing project. |
| `sequence` | Core-assigned contiguous positive ingestion sequence. The first event is `1`; for a valid ledger, the last sequence equals the event count. |
| `event_id` | Caller-supplied project-wide idempotency key. It is 1-128 ASCII characters, starts with a letter or digit, and otherwise uses letters, digits, `.`, `_`, `:`, or `-`. |
| `producer` | Audit provenance only. It is 1-64 lowercase kebab-case characters and does not select different Core behavior. |
| `occurred_at` | Caller-supplied ISO-8601 time with an explicit offset, normalized to UTC with six fractional digits and `Z`. |
| `ingested_at` | Core clock time normalized to the same UTC form. It is audit metadata, not the ordering key. |
| `operation` | One of `created`, `modified`, `deleted`, `moved`, or `unknown`. |
| `paths` | One to 256 sorted, unique, canonical project-relative paths. Each normalized path is at most 4096 UTF-8 bytes. |

The operation is intentionally descriptive rather than authoritative. For
example, a `deleted` path need not exist when submitted. For `moved`, an adapter
should include every path made dirty by the native signal, normally both the old
and new project-relative names. H-04 does not assign source/destination roles or
infer a move from content identity. Use `unknown` when the host knows that paths
may have changed but cannot make a stronger claim.

The ledger line limit is 1 MiB. JSON must be strict UTF-8, contain no duplicate
object keys or non-finite numeric constants, and satisfy the exact closed fields
above.

## Event-ID idempotency and collision handling

`event_id` uniqueness is scoped to one registered project and is the only event
deduplication key. Producers should use a stable namespaced native ID, for
example `codex:<native-id>` or `claude-code:<native-id>`, and must reuse it when
retrying an uncertain submission.

Core canonicalizes the caller-controlled identity payload before comparison:

```text
event_id + producer + occurred_at + operation + normalized sorted paths
```

`sequence` and `ingested_at` are Core-assigned and are not part of that payload.
Submission then has exactly three outcomes:

1. New ID -> append one event, assign `sequence = previous_count + 1`, and
   return `disposition: appended`.
2. Existing ID with the same canonical identity payload -> append nothing,
   return the original sequence with `disposition: duplicate`, and repair the
   derived projection if needed.
3. Existing ID with any different identity field -> fail closed with
   `host-event-conflict`; append nothing and leave both artifacts unchanged.

Equivalent input spellings that normalize to the same payload are duplicates.
For example, a timezone-offset spelling that represents the same instant and a
Windows path using `\` may normalize to the already persisted UTC time and POSIX
path. Two identical payloads with different event IDs remain two distinct
signals. Two hosts reusing the same project-wide ID are a collision even when
that reuse was accidental.

This rule makes retry safe across process failures: a caller does not need to
know whether the first attempt failed before or after the durable append. It
retries the same ID and receives either the original event or a collision error,
never a second copy of the same ID.

## Ingestion sequence and out-of-order `occurred_at`

`sequence` is the sole ledger order. Core assigns it while holding the project
event lock, so accepted lines are contiguous in the order they become durable.
Neither `occurred_at` nor `ingested_at` is required to be monotonic; host clock
skew and delayed delivery are allowed.

Example:

```text
Host event B occurred at 10:05 and arrives first  -> sequence 1
Host event A occurred at 10:00 and arrives second -> sequence 2
```

The ledger remains `[B, A]`. Core does not insert A before B, renumber B, or
rewrite prior bytes. Projection aggregation also follows `sequence`; timestamps
are retained only for audit. This deterministic ingestion order prevents a late
host signal from changing the identity or ordering of already accepted events.

## Path normalization and protected paths

Every submitted path is normalized lexically before idempotency comparison or
persistence:

1. require text or a path value;
2. reject ASCII control characters and `DEL`;
3. normalize Unicode to NFC;
4. convert `\` to `/`;
5. reject empty paths, the project root itself, absolute POSIX paths, UNC paths,
   bare Windows drive designators (`C:`), and drive-rooted Windows paths
   (`C:/...`);
6. collapse repeated `/` and remove a trailing `/`;
7. reject empty, `.`, and `..` segments;
8. preserve path case and do not resolve the path through the filesystem;
9. enforce the 4096-byte UTF-8 limit;
10. sort and deduplicate the event's final path list.

The protected project-state roots `.git`, `.hg`, `.svn`, and `.llmwiki` are
rejected case-insensitively when they are the submitted top-level path. An event
that contains any invalid or protected path is rejected as a whole; Core does
not append a partial subset.

H-04 does not evaluate ordinary include/exclude rules, `.llmwikiignore`, file
size, sensitivity, existence, regular-file type, or symlink targets. Those are
scan/reconciliation concerns. Protected-path rejection is a hard safety boundary
that prevents host noise from treating VCS or Core state as research source
changes.

## Deterministic `dirty-paths.json` projection

The queue is a closed Schema v1 JSON document derived by replaying every ledger
event in ascending `sequence` order:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-dirty-path-queue",
  "queue_version": "dirty-paths-v1",
  "project_id": "study-0123456789ab",
  "ledger_event_count": 3,
  "ledger_last_sequence": 3,
  "ledger_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "dirty_path_count": 1,
  "dirty_paths": [
    {
      "path": "src/model.py",
      "first_sequence": 1,
      "last_sequence": 3,
      "event_count": 2,
      "operations": ["modified", "unknown"],
      "producers": ["claude-code", "codex"]
    }
  ]
}
```

Projection rules are deterministic:

- `ledger_sha256` hashes the exact canonical bytes of the validated ledger;
- `ledger_event_count` and `ledger_last_sequence` bind the projection to one
  append point;
- there is exactly one entry per normalized path;
- `first_sequence` and `last_sequence` are the lowest and highest ingestion
  sequences that mention the path;
- `event_count` counts ledger events that mention the path once each;
- `operations` and `producers` are sorted unique sets accumulated across those
  events;
- `dirty_paths` is sorted lexicographically by normalized path;
- no rebuild timestamp, source stat, Manifest generation, or host-specific field
  is included, so replaying the same ledger produces byte-identical JSON.

The queue is coalesced but not consumed in H-04. There is no acknowledge,
dequeue, clear, or reconciliation watermark operation. A path remains dirty for
as long as the H-04 ledger contains an event for it. A later H-07/H-05 contract
may add an explicit reconciliation checkpoint, but H-04 must not invent one or
silently discard old events.

## Core APIs

The host-independent facade is the only supported integration boundary for CLI
and future host adapters.

### Submit one event

```python
from tools.research_core import ResearchCoreService

core = ResearchCoreService(workspace_root=r"E:\ResearchAssistantWorkspace")

result = core.host_event_submit(
    "study-0123456789ab",
    event_id="codex:0190f5a4-8c7d-7000-a000-000000000001",
    producer="codex",
    occurred_at="2026-07-16T17:14:03+08:00",
    operation="modified",
    paths=["src/model.py", "tests/test_model.py"],
)
```

The path-safe result identifies the project, event ID, original/new sequence,
`appended` or `duplicate` disposition, relative artifact names, whether the
projection was updated, and the resulting queue. It does not return absolute
source, workspace, machine-state, or knowledge paths.

### Read or repair the queue

```python
result = core.dirty_path_queue("study-0123456789ab")
```

This call never appends an event. It validates the authoritative ledger and
returns the current queue. It may atomically rebuild `dirty-paths.json` when the
projection is absent, malformed at the current supported contract, or stale
against the ledger count, last sequence, or SHA-256. Its result reports whether
a rebuild occurred.

A parseable future `schema_version` fails closed rather than being replaced by
an older implementation. A missing-schema legacy projection also fails closed
because H-04 defines no implicit migration. Malformed bytes and other
current-schema projection mismatches are rebuildable because the queue has no
independent authority. An invalid ledger always fails closed; Core never treats
the projection as a backup ledger.

Both Core methods load the project by registered ID and delegate to the same
`tools.host_events` persistence rules. Host adapters must not append JSONL or
write the projection directly.

## CLI

Submit a single host-neutral event with one or more repeated `--path` options:

```powershell
python -B -m tools.project event submit study-0123456789ab `
  --workspace-root E:\ResearchAssistantWorkspace `
  --event-id codex:0190f5a4-8c7d-7000-a000-000000000001 `
  --producer codex `
  --occurred-at 2026-07-16T17:14:03+08:00 `
  --operation modified `
  --path src/model.py `
  --path tests/test_model.py `
  --json
```

Show the current coalesced queue and repair its projection if necessary:

```powershell
python -B -m tools.project event show study-0123456789ab `
  --workspace-root E:\ResearchAssistantWorkspace `
  --json
```

`event show` does not mean source-open and does not print raw source content. It
shows the queue projection, not a timestamp-sorted rewrite of the event ledger.
Both commands delegate to `ResearchCoreService`; the CLI does not duplicate
locking, append, replay, or path-validation logic. JSON errors retain stable Core
reason codes such as `host-event-invalid`, `host-event-conflict`,
`host-event-state-invalid`, `host-event-lock-failed`, and
`dirty-path-queue-invalid`.

## Locking, durability, and crash recovery

Submission and projection repair use one exclusive per-project event lock. The
lock serializes sequence allocation, duplicate detection, ledger append, and
projection replacement across local processes. The lock is local machine-state
coordination, not a distributed lock and not a claim that Hooks deliver exactly
once.

The durability order is:

```text
validate registration and event
  -> acquire project event lock
  -> validate and replay complete ledger
  -> resolve duplicate or collision
  -> append one canonical newline-terminated event and fsync
  -> derive queue from the resulting ledger snapshot
  -> write a same-directory temporary queue, fsync, and atomic replace
  -> release lock
```

The durable newline-terminated ledger append is the event commit point. Crash
outcomes are handled as follows:

1. Crash before the append commit -> no event is accepted; retrying the same ID
   appends it normally.
2. Crash after the append commit but before projection replacement -> the event
   remains authoritative; the next duplicate retry, `dirty_path_queue(...)`, or
   `project event show` rebuilds the projection from the ledger.
3. Crash during projection replacement -> an old, missing, or temporary
   projection may remain; replay of the valid ledger deterministically repairs
   it. Temporary files never override the ledger.
4. A non-newline-terminated final ledger fragment is not accepted as a record,
   but H-04 does not truncate it automatically. A partial tail, malformed
   newline-terminated record, sequence gap, duplicate persisted ID, or project
   mismatch makes the authoritative ledger fail closed instead of being guessed
   at or skipped. Recovery requires an explicit operator restore from a preserved
   valid ledger boundary; the derived queue is never used as the source.
5. A normal exception releases only the lock token created by that process. A
   hard process or machine crash can leave a stale lock marker. H-04 does not
   steal a lock merely because a timeout elapsed; an operator must first verify
   that no writer is active, remove only the stale event lock, and rerun
   `project event show` to validate the ledger and rebuild the projection.

If a caller receives an uncertain post-append failure, it must retry the same
`event_id`, not mint a new one. Idempotency then determines whether the original
append committed.

## Machine, knowledge, and source boundaries

H-04 preserves the project-scoped storage contract:

- Machine state: `events.jsonl`, `indexes/dirty-paths.json`, lock state, and
  temporary projection files live only under
  `.llmwiki/projects/<project_id>/`.
- Curated knowledge: H-04 does not read or write
  `wiki/projects/<project_id>/`, summaries, claims, plans, or any other Markdown
  knowledge artifact.
- Source project: event submission performs lexical path handling only. It does
  not create, modify, delete, open, hash, stat, scan, or follow submitted source
  paths. Deleted and not-yet-created paths can therefore be recorded safely.
- Existing machine registries: H-04 does not mutate `project.yaml`,
  `manifest.jsonl`, `sources.jsonl`, `evidence.jsonl`, coverage state, extracted
  payloads, or run records.
- Privacy: events contain only project ID, project-relative paths, event
  metadata, and timestamps. They contain no raw file bytes, excerpts, local
  absolute paths, Git origin URLs, prompts, model output, credentials, or
  arbitrary host payload blobs.
- Side effects: the deterministic path makes no LLM call, network request,
  browser launch, Web render, or Hook invocation.

Project-relative paths are still local machine metadata and are not automatically
approved for external transmission. A future MCP or host transport must apply
its own host-safe result contract rather than exposing machine-state files.

## Explicit non-goals

H-04 intentionally does not implement or claim:

- H-01 Manifest-generation diffing;
- H-02 source-to-knowledge dependency edges;
- H-03 stale propagation;
- H-05 selective reconciliation or selective refresh;
- H-06 deletion/move recovery beyond recording the paths supplied by the host;
- H-07 Hook installation, Hook reliability, Stop-boundary synchronization,
  missed-event detection, full-scan fallback, or eventual-consistency policy;
- filesystem watching, background daemons, polling, or host-native Hook code;
- project registration, scanning, inventory, hashing, classification,
  extraction, source sync, Evidence creation, or source-open;
- dirty-path acknowledgement, clearing, dequeue, or reconciliation checkpoints;
- curated Markdown mutation, automatic claim updates, plan updates, or Wiki
  synthesis;
- LLM, Web, browser, network, MCP-tool, or Plugin behavior.

The H-04 queue is therefore an incremental input ledger only. Later
reconciliation must verify current source truth independently and must not treat
a host event as a completed refresh.

## Validation

Focused acceptance commands for the complete H-04 implementation are:

```powershell
python -B -m pytest -q -p no:cacheprovider `
  tests/test_host_events.py `
  tests/test_research_core_service.py
python -m ruff check `
  tools/host_events.py `
  tools/research_core.py `
  tools/project.py `
  tests/test_host_events.py `
  tests/test_research_core_service.py
python -B -m pytest -q -p no:cacheprovider
$env:PYTHONIOENCODING='utf-8'; python -B tools/health.py
python -m pip check
git diff --check
```

Focused tests must cover at least:

- exact event, submit-result, queue, and queue-result Schema v1 fields;
- legacy/future fail-closed behavior and strict UTF-8/JSON parsing;
- canonical timestamp and path normalization;
- absolute, traversal, control-character, overlong, and protected-path rejection;
- sorted/unique path bounds and all five operation values;
- first append, exact duplicate retry, and same-ID/different-payload collision;
- no sequence gap after duplicates, collisions, or failed validation;
- deterministic ingestion of out-of-order `occurred_at` values;
- byte-identical projection rebuilds, ledger SHA-256 binding, and coalesced path
  aggregates;
- missing, stale, and interrupted projection recovery without ledger mutation;
- lock contention, timeout, token-safe release, and documented stale-lock
  recovery;
- interruption before append, after append, before projection replacement, and
  fail-closed handling of a partial ledger tail;
- Core/CLI delegation and JSON error reason codes;
- unchanged source and curated-knowledge snapshots;
- absence of scan, reconciliation, Hook, LLM, network, and Web side effects.

## Rollback and checkpoint

After the focused and full validation commands pass, create the normal
history-preserving lightweight checkpoint:

```bash
git tag checkpoint/h-04-host-event-ledger <h-04-commit>
```

Rollback code and documentation with normal history-preserving Git operations:

```bash
git revert <h-04-commit>
```

Rollback must not delete or rewrite an existing `events.jsonl`. It is an
append-only machine audit record and may be needed by a later compatible reader
or explicit migration. `dirty-paths.json` is derived, but rollback should leave
it in place rather than silently deleting user machine state. If an operator
chooses to remove a stale projection while using a compatible H-04 version, it
must be rebuilt from the validated ledger; the projection must never be used to
replace the ledger.
