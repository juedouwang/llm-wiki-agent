# J-03 Local Research Cockpit

Status: **implemented as the bounded R3 read-only product cockpit.** Controlled
Web editing belongs to J-04; Verified Query remains unavailable until G-04.

`tools/research_cockpit.py` is the registered-project product surface. It is
intentionally separate from `tools/development_dashboard.py`, which supervises
this repository's Roadmap, commits, checkpoints, and validation ledger. The
research cockpit reads only bounded local machine state and validated Knowledge
Schema v2 Markdown for registered research projects.

## Start the cockpit

From the workspace root:

```powershell
python -B tools/research_cockpit.py --workspace-root . serve
```

The default URL is `http://127.0.0.1:8765/`. A different loopback address or
port may be selected explicitly:

```powershell
python -B tools/research_cockpit.py --workspace-root . serve --host ::1 --port 8876
```

`--open` opens the bound local URL in the default browser. Non-loopback bind
addresses are rejected. The server also rejects non-loopback or malformed
`Host` headers, and every method other than `GET` and `HEAD` returns a bounded
read-only `405` response.

The same deterministic data is available without an HTTP server:

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

Request targets, percent escapes, path segments, project IDs, identifiers, query
names, duplicate query parameters, offsets, limits, and status filters are
strictly bounded. Encoded separators and traversal are rejected. Caller-
controlled invalid query names are not reflected in error responses.

## Read and security boundary

J-03 is read-only and local-only:

1. Static assets are loaded once from a real, non-redirected asset directory.
2. Machine and Knowledge files are bounded and read only when they are stable
   regular files beneath their expected registered roots.
3. Redirected project, daily-plan, run, asset, or Knowledge paths are not
   traversed. Their absence or unsafe state is reported as a gap.
4. Knowledge bytes are decoded as strict UTF-8 and parsed through the current
   Knowledge Schema v2 validator, including canonical path/type identity.
5. Absolute source, knowledge, machine, workspace, and Git-origin roots are
   removed from every public payload, including nested error and run fields.
6. The server emits a restrictive Content Security Policy, `nosniff`, no-referrer,
   frame denial, no-store caching, and API-specific sandbox headers. The shipped
   page uses no external scripts, styles, fonts, images, or network services.
7. The cockpit performs no registration, scan, extraction, reconciliation,
   Markdown write, task execution, source reopen, LLM call, external send, or
   C-07 research-binary read.

The registered source project remains byte-for-byte unchanged. J-03 does not
claim Source/Evidence currentness beyond the strict registries and validated
state it can safely load, and it never converts a display status into scientific
verification.

## Capability boundary

The public capability object deliberately reports:

```json
{
  "query": {
    "available": false,
    "error": "capability-unavailable",
    "available_after": "G-04"
  },
  "editing": {
    "available": false,
    "available_after": "J-04"
  },
  "research_binary_metadata": {
    "available": false,
    "status": "deferred/not_started",
    "available_after": "C-07"
  }
}
```

J-04 may add narrowly mapped controlled edits, but it must reuse the F-05A/F-05B
controlled Markdown boundary and must not turn these read routes into arbitrary
filesystem access. C-07 remains `deferred/not_started`, and J-03 does not enter
R4 Verified Query or incremental stale-propagation work.

## Validation

The focused regression suite is:

```powershell
python -m unittest tests.test_research_cockpit -v
```

It covers honest registration-only gaps; current synthetic inventory, coverage,
Claim/Evidence/Source/Locator, planning, run usage/cost/error projections;
malformed and future artifacts; absolute-root redaction; source immutability;
loopback bind and Host validation; read-only methods; security headers; request
traversal/query rejection; and redirect safety where the host permits symbolic
link creation.
