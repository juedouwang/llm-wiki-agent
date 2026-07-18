# Controlled Markdown Planning and Persistence Contract (F-05A/F-05B)

F-05A adds a deterministic, Core-internal, **in-memory** planner for controlled
Knowledge Schema v2 Markdown updates. It closes the body-ownership safety gap that
F-01 through F-04 intentionally left open, but it does not write a file. The host
Agent supplies the complete proposed page, update intent, and every semantic choice;
Core only establishes that the proposal is bound to the exact caller-supplied current
snapshot and cannot cross declared generated/user ownership boundaries.

The implementation is [`tools/controlled_markdown.py`](../tools/controlled_markdown.py).
It composes with the strict path/frontmatter parser in
[`tools/knowledge_artifacts.py`](../tools/knowledge_artifacts.py). It is not a
replacement for F-02 Claim–Evidence currentness, F-04 lifecycle validation, or later
content synthesis.

## F-05A planning API

```python
from tools.controlled_markdown import plan_controlled_markdown_update

plan = plan_controlled_markdown_update(
    path="overview.md",
    current=current_page_bytes,          # None only for creation
    proposed=complete_proposed_page_bytes,
    intent="regenerate",                # or "user-edit"
    expected_current_sha256=current_hash, # None only for creation
)

candidate_output = plan.output_bytes
metadata = plan.as_dict()               # never includes body/output bytes
```

Inputs are bytes so strict UTF-8 remains enforceable. `path` must be one canonical
F-01A project-relative knowledge path. Both existing and proposed pages must use the
current strict Knowledge Schema v2; Schema v1 remains read-only compatibility and
future Schema versions fail closed. Pages and resulting output are bounded to 4 MiB.

An existing update requires the exact lowercase SHA-256 of the complete caller-supplied
current page snapshot. Project identity, artifact type, ownership, and `generated_at` are
immutable in an ordinary update, and proposed `updated_at` must advance strictly. Ownership changes
remain an explicit future migration or adoption operation; F-05A never guesses one.

## Ownership and intents

F-05A uses the existing closed `ownership` set without adding frontmatter fields:

| ownership | permitted intent | body contract |
|---|---|---|
| `generated` | `regenerate` | the complete body is generator-owned and may be replaced |
| `user` | `user-edit` | the complete body is user-owned; regeneration is rejected |
| `mixed` | both | generated text and explicit protected user regions are merged by exact structure |

A new `mixed` page must be created by `regenerate`, and every protected user region
must initially contain no non-whitespace content. A generator therefore may establish a place for future user
confirmation but cannot manufacture text and label it user-confirmed. A new `user`
page must come through `user-edit`.

The plan validates structural frontmatter only. Body ownership does not partition or
authorize mutable frontmatter: the change report may list title, status, Source,
Evidence, verification-time, or other structurally valid changes, but it is not a
permission grant. Callers must separately compose the applicable F-02/F-03/F-04 checks
and user authorization before a later writer persists the returned bytes.

## Mixed-page protected regions

Mixed pages use exact LF/CRLF-delimited markers:

```markdown
Generated explanation.

<!-- llmwiki:user-region:start id="confirmation" -->
User-confirmed text lives here.
<!-- llmwiki:user-region:end id="confirmation" -->

More generated explanation.
```

Region IDs use lowercase letters, digits, and hyphens, are duplicate-free, and are
bounded. Markers must be complete lines, may not nest, and must have a matching ID.
Marker-like malformed text fails closed. Unicode line/paragraph separators and bare
carriage returns are ordinary content, not marker boundaries. `generated` and `user`
pages may not contain this reserved marker namespace.

For an existing mixed page:

- an ordinary update preserves the same ordered region IDs;
- `regenerate` takes generated text and marker placement from the proposal but
  replaces every proposed user-region body with the exact user content from the
  caller-supplied current snapshot;
- `user-edit` may change user-region bodies only; generated text and marker skeleton
  must remain byte-for-byte identical;
- adding, removing, reordering, nesting, or silently renaming user regions is rejected.

This makes regeneration safe without asking Core to infer which prose "looks human."
The host and product UI must explicitly target the declared user regions.

## Result and audit handoff

A successful `ControlledMarkdownUpdatePlan` contains:

- `controlled-markdown-plan-v1` and an auditable `cmp-<64-hex>` plan ID;
- canonical path, project, artifact type, ownership, and intent;
- caller-supplied current-snapshot, raw-proposal, and canonical-output SHA-256 values plus output byte count;
- changed frontmatter field names;
- ordered user-region IDs, which regions were preserved, which were user-edited,
  and which proposal-region bodies regeneration discarded;
- generated-body and user-body change flags;
- canonical output bytes for a later bounded writer.

The plan ID closes over the exact caller-supplied current snapshot, exact raw proposal,
canonical output revision, timestamps, region declarations, change report, and intent.
`validation_complete` means only that the planner produced this in-memory result; it
is not filesystem currentness, authenticated actor identity, semantic authorization, or
permission to persist. The plan ID is deterministic correlation metadata, not a capability
or authorization token. A later writer must not trust caller-constructed plan metadata by
itself: it must freshly read the live file, deterministically rebind or recompute the plan
from the exact base/proposal bytes, reapply trusted host and user decisions, and perform an
exact CAS immediately before replacement. The serialized `as_dict()` view omits all
body/output content so a later bounded writer can map it into an audit record without
duplicating user text.

Rejected planning exposes a bounded body-free `as_dict()` classification. Its stable reason
codes distinguish revision conflicts, ownership denial, protected-region conflicts, and
generic structural rejection without copying Markdown/YAML bodies into audit metadata.

## F-05B persistence API

F-05B adds one Core-internal persistence primitive without publishing a service or
transport operation:

```python
from tools.controlled_markdown_persistence import (
    TrustedHostSessionContext,
    bind_controlled_markdown_authorization,
    persist_controlled_markdown_update,
)

host_context = TrustedHostSessionContext(
    host_id="codex",
    actor_type="user",          # or host-agent
    actor_id="local-user",
    session_id="session-42",
)
authorization = bind_controlled_markdown_authorization(
    plan,
    host_context=host_context,
    decision_id="decision-42",
    authorized_at="2026-07-18T00:00:00Z",
)
result = persist_controlled_markdown_update(
    workspace_root,
    project_id,
    path="overview.md",
    proposed=complete_proposed_page_bytes,
    intent="regenerate",
    expected_current_sha256=current_hash,
    authorization=authorization,
)
```

The authorization is a structured host attestation, not an operating-system login
or a Core semantic decision. Its deterministic ID closes over the trusted host,
actor type and ID, session, decision, timestamp, exact F-05A plan ID, project/path,
intent, base revision, raw proposal revision, and candidate output revision. A raw
actor string, a plan ID by itself, or caller-constructed plan metadata is never a
write capability. The same authorization ID and the same trusted
`(host, actor type, actor, session, decision)` tuple may bind only one transaction.
A retry after conflict or uncertainty therefore needs a fresh live read, plan, and
host/user decision.

Under the registered project's persistent `indexes/machine-state.lock`, Core:

1. reloads the registration and rejects any changed project/storage identity;
2. rejects malformed, future, noncanonical, incomplete, or replayed audit history;
3. opens the already-existing canonical knowledge parent without creating Markdown
   directories, then reads the live page through a stable directory lease;
4. independently recomputes F-05A from the live bytes and exact raw proposal rather
   than trusting the caller's plan object;
5. requires the recomputed plan to match the complete structured authorization;
6. appends a durable `prepared` record; and
7. performs a non-clobbering creation or stable-object replacement with an exact
   SHA-256 recheck immediately before atomic publication, followed by a live output
   hash verification.

The compare-and-swap boundary serializes all cooperating llm-wiki writers through
`machine-state.lock`, pins and revalidates the knowledge directory, uses
non-clobbering creation, and checks the exact live hash immediately before platform
atomic replace. It is not described as a kernel-provided hash-CAS primitive against
an arbitrary process that deliberately bypasses the product's mutation protocol.
A revision conflict never auto-rebases and never overwrites the newly observed page.

## Body-free two-phase audit

The strict canonical JSONL ledger is stored at
`.llmwiki/projects/<project_id>/indexes/controlled-markdown-audit.jsonl`. Records are
bounded by total bytes, record bytes, and record count; they use exact Schema v1,
strict UTF-8/JSON, contiguous sequence numbers, exact fields, canonical ordering,
and no Markdown, YAML body, excerpt, or generated output bytes.

Each transaction is adjacent and two-phase:

- `prepared -> committed` when the exact output is published and verified;
- `prepared -> conflict` when the live revision no longer matches;
- `prepared -> failed` for a proved-safe pre-publication failure; or
- `prepared -> commit-unknown` when publication or terminal durability cannot be
  proved. No rollback is attempted after possible publication.

`failed` is an explicit safe terminal state rather than a success claim. A dangling
`prepared` record is retained as crash evidence and makes every later transaction
fail closed until an operator inspects or repairs the ledger; Core never guesses
whether the authorization can be replayed. If Markdown may be committed but the
terminal audit append cannot be proved, the caller receives a distinct
commit-audit-unknown error carrying only the transaction ID.

## Explicit non-goals

F-05A remains the only body-ownership planner; F-05B only persists a host-authorized
proposal after independently reproducing that plan. Together they do not:

- decide or authorize scientific semantics, Evidence stance/currentness, Claim
  lifecycle, conflict, title, Source binding, or frontmatter policy;
- infer prose authorship, synthesize content, auto-rebase conflicts, migrate Schema
  v1, change ownership, or repair a dangling audit transaction;
- create missing Markdown directories, write the registered source project, read
  research binaries or Source content, mutate Source/Evidence/entity registries, or
  propagate stale state;
- call a model, send data externally, or add CLI, MCP, Hook, Web, or
  `ResearchCoreService` behavior.

Product Web edits, renderer integration, and higher-level semantic authorization
remain later Roadmap units. F-05B is the bounded persistence/audit primitive those
units may compose; it is not itself a user-facing editor.
