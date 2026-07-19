# J-03/J-04 Local Research Cockpit

Status: **implemented as the bounded R3 product cockpit.** J-03 provides the
loopback-only read surface by default. J-04 adds an explicit opt-in mode for four
fixed controlled Markdown user regions. Verified Query remains unavailable until
G-04, and C-07 remains `deferred/not_started`.

`tools/research_cockpit.py` is the registered-project product surface. It is
intentionally separate from `tools/development_dashboard.py`, which supervises
this repository's Roadmap, commits, checkpoints, and validation ledger. The
cockpit reads bounded local machine state and validated Knowledge Schema v2
Markdown. Its optional writes compose the existing F-05A planner and F-05B
persistence boundary rather than creating another Markdown write path.

## Start the cockpit

The default remains read-only:

```powershell
python -B tools/research_cockpit.py --workspace-root . serve
```

To enable only the four J-04 edit targets for a trusted local session:

```powershell
python -B tools/research_cockpit.py --workspace-root . serve --enable-editing --actor-id local-user
```

`--actor-id` supplies bounded local audit attribution. The host adapter creates a
fresh trusted session ID and random edit token for each server process; callers
cannot supply a decision ID or arbitrary filesystem path.

The default URL is `http://127.0.0.1:8765/`. A different loopback address or
port may be selected explicitly:

```powershell
python -B tools/research_cockpit.py --workspace-root . serve --host ::1 --port 8876
```

`--open` opens the bound local URL in the default browser. Non-loopback bind
addresses are rejected. The server also rejects non-loopback, malformed, or
wrong-port `Host` headers. In default mode every mutating method returns a
bounded read-only `405`; editing mode admits `POST` only on the exact fixed edit
routes described below.

The same deterministic read data is available without an HTTP server. Snapshot
and health commands never enable editing:

```powershell
python -B tools/research_cockpit.py --workspace-root . snapshot --pretty
python -B tools/research_cockpit.py --workspace-root . snapshot --project-id <project_id> --pretty
python -B tools/research_cockpit.py --workspace-root . health --pretty
```

## Product views

For each safely loadable registered project, the cockpit exposes:

- the registration projection without absolute source, knowledge, or machine
  storage paths;
- all fifteen required product artifacts: overview, project map, reproduction,
  architecture, papers, methods, datasets, experiments, results, claims, open
  questions, status, risks, goals, and backlog;
- explicit `DRAFT` gaps when a canonical page is missing, malformed, future
  schema, path/type mismatched, redirected, or otherwise unsafe;
- current Manifest coverage, every ordinary file's processing status, read
  depth, reason code, and bounded reason text;
- Claim status plus the declared Claim -> Evidence -> Source -> Locator trace,
  without reopening the registered source file;
- strict Goal, task, project-state, initial-plan, daily-plan, and run-history
  projections when their current artifacts are available;
- run stage/attempt status, duration, token usage, estimated cost, and bounded
  error information already present in local machine state.

The user interface distinguishes available, draft, stale, conflicting, failed,
and unsafe information rather than treating absent or invalid state as success.
The fifteen canonical entries always remain visible, including immediately after
registration when all of them are honest `DRAFT` gaps.

## J-04 controlled edit targets

Editing mode exposes exactly these mappings:

| Target key | Knowledge path | Mixed user region |
|---|---|---|
| `goal` | `goals.md` | `user-goals` |
| `backlog` | `plans/backlog.md` | `user-backlog` |
| `project_status` | `status.md` | `user-status` |
| `user_confirmed_conclusions` | `claims/index.md` | `user-confirmed-claims` |

There is no path, filename, artifact-type, or region input in the UI or API. An
editable target must already exist as a strict current Knowledge Schema v2 page,
match its canonical project/path/type identity, use `ownership: mixed`, remain
`status: draft`, and contain the exact renderer-owned mixed-region skeleton for
that target. Schema v1/future/malformed pages, generated/user ownership,
non-draft status, wrong markers, redirects, and unsafe paths fail closed.

A save operation:

1. validates the submitted full-page SHA-256 against freshly read page bytes;
2. validates bounded UTF-8 user content and rejects marker injection, NUL/control
   bytes, and registered local-root disclosure;
3. replaces only the fixed user region and advances only frontmatter
   `updated_at`;
4. independently recomputes the F-05A plan from live bytes;
5. binds it to the trusted host/session context and a server-generated unique
   decision ID;
6. persists through F-05B exact compare-and-swap and its body-free audit ledger;
7. refetches the edited target, project snapshot, and Markdown viewer after a
   confirmed commit.

A stale revision or CAS race returns `409` and never overwrites competing bytes.
The browser preserves the unsaved draft until the user explicitly reloads. A
commit/audit-unknown outcome returns bounded body-free `503` state rather than
claiming success. Later E-07 regeneration preserves the edited mixed user-region
bytes. These edits do not authorize task execution, scientific verification,
Claim lifecycle transitions, or arbitrary frontmatter changes.

## HTTP API

All responses are JSON except the three allowlisted static assets.

| Route | Purpose |
|---|---|
| `GET /api/health` | Cockpit health and capability boundary. |
| `GET /api/projects` | Bounded registered-project cards. |
| `GET /api/snapshot?project_id=<id>` | Workspace snapshot, optionally selecting one project. |
| `GET /api/projects/<id>` | Full bounded project snapshot. |
| `GET /api/projects/<id>/files` | Paginated file-state view; accepts `offset`, `limit`, and `status`. |
| `GET /api/projects/<id>/claims` | Paginated Claim view; accepts `offset`, `limit`, and `status`. |
| `GET /api/projects/<id>/knowledge?path=<canonical.md>` | Strictly parsed Knowledge Schema v2 page and body. |
| `GET /api/projects/<id>/evidence/<evidence_id>` | One Evidence record and source/locator trace. |
| `GET /api/projects/<id>/sources/<source_id>` | One Source registry projection and current locator metadata. |
| `GET /api/edit-session` | Editing-mode capability, fixed targets, random token, and required token header. Hidden when editing is disabled. |
| `GET /api/projects/<id>/edits/<target>` | Current fixed user region plus full-page revision hash. Hidden when editing is disabled. |
| `POST /api/projects/<id>/edits/<target>` | Exact-CAS update for one fixed target. The JSON body is exactly `expected_current_sha256` plus `content`. |

Request targets, percent escapes, path segments, project IDs, identifiers, query
names, duplicate query parameters, offsets, limits, and status filters are
strictly bounded. Encoded separators and traversal are rejected. Caller-
controlled invalid query names are not reflected in error responses. Arbitrary
or malformed `/edits` paths return `404`.

## Read, write, and security boundary

The cockpit remains local-only:

1. Static assets are loaded once from a real, non-redirected asset directory.
2. Machine and Knowledge files are bounded and read only when they are stable
   regular files beneath their expected registered roots.
3. Redirected project, daily-plan, run, asset, or Knowledge paths are not
   traversed. Their absence or unsafe state is reported as a gap.
4. Knowledge bytes are decoded as strict UTF-8 and parsed through the current
   Knowledge Schema v2 validator, including canonical path/type identity.
5. Absolute source, knowledge, machine, workspace, and Git-origin roots are
   removed from public payloads, including nested error and run fields.
6. The server emits a restrictive Content Security Policy, `nosniff`,
   no-referrer, frame denial, no-store caching, and API-specific sandbox
   headers. The shipped page uses no external scripts, styles, fonts, images, or
   network services.
7. Every edit `POST` requires the random session token in
   `X-LLMWiki-Edit-Token`, an exact same-origin `Origin`, an absent or
   `same-origin` `Sec-Fetch-Site`, and the current loopback Host/port.
8. Edit requests reject transfer encoding, missing/invalid/oversized or duplicate
   `Content-Length`, non-JSON content type, non-UTF-8 data, duplicate JSON keys,
   unknown/missing fields, and non-finite JSON constants.
9. The default cockpit performs no write. Editing mode writes only the four
   curated Markdown pages through F-05A/F-05B; it performs no registration,
   scan, extraction, reconciliation, task execution, source reopen, LLM call,
   external send, or C-07 research-binary read.

The registered source project remains byte-for-byte unchanged, including its
hashes, mtimes, and modes in the acceptance fixture. J-04 does not claim
Source/Evidence currentness beyond state it can safely load, and it never turns a
user-entered conclusion into scientific verification.

## Capability boundary

Default mode reports editing as unavailable for the current process:

```json
{
  "read_only": true,
  "editing": {
    "available": false,
    "available_after": "J-04"
  },
  "query": {
    "available": false,
    "error": "capability-unavailable",
    "available_after": "G-04"
  },
  "plan": {
    "available": true,
    "execution_authorized": false
  },
  "external_send": "local-only",
  "source_bytes_reopened": false,
  "c07": "deferred/not_started"
}
```

With `serve --enable-editing`, only `read_only` becomes `false` and the editing
object becomes `{"available": true, "available_after": null}`. The plan fields
refer to display of the existing I-04 DRAFT initial plan; the cockpit does not
authorize its execution. The Query object remains exactly unavailable until
G-04. C-07 remains `deferred/not_started`, and J-04 does not enter R4 Verified
Query or incremental stale-propagation work.

## Validation

The focused regression suite is:

```powershell
python -B -m pytest -q -p no:cacheprovider tests/test_research_cockpit.py tests/test_research_cockpit_editing.py
```

It covers the full J-03 read surface plus the four exact target mappings,
full-page hash/CAS behavior, F-05A/F-05B persistence and body-free audit,
frontmatter/skeleton/other-page protection, mixed-region preservation across
regeneration, strict schema/ownership/status/marker rejection, local-root and
marker-injection rejection, source-tree immutability, token/origin/fetch-site/
Host enforcement, strict request framing and JSON schema, bounded 409/503
outcomes, arbitrary edit-route rejection, static browser contract, conflict
draft preservation, and successful refetch order. Redirect tests remain skipped
when the Windows host does not grant symbolic-link creation privilege.
