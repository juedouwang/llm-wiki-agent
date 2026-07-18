# Controlled Markdown Update Planning Contract (F-05A)

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

## API

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

## Explicit non-goals

F-05A performs no filesystem I/O and does not:

- persist Markdown, create directories, acquire filesystem locks, or append an audit
  ledger;
- write the registered source project or machine registries;
- migrate Schema v1 or change page ownership;
- validate Source/Evidence currentness or authorize Claim lifecycle transitions;
- infer prose ownership, research meaning, Evidence stance, conflict, or conclusions;
- synthesize page content, call a model, read research binaries, or send data
  externally;
- add CLI, MCP, Hook, or Web behavior.

Atomic knowledge-root persistence, creation non-existence checks, exact-CAS recheck against
the actual knowledge-root file, trusted host/session actor attribution, and a body-free audit
trail belong to F-05B. A conflict retry requires a fresh stable read, a fresh plan, and
a fresh host/user decision; F-05A does not auto-rebase or replay authorization. Product Web
edits and the 15-artifact renderers remain later units.
F-05 therefore remains partial after F-05A.
