# Project Knowledge Artifact Contract (F-01 / F-02 / F-03 / F-04, Schema v2)

F-01 defines the deterministic project-relative path and empty-layout contract
for curated Markdown under `<knowledge_projects_root>/<project_id>/`. F-02A
advances the current frontmatter contract to Knowledge Schema v2 with explicit
directional Evidence references and a structural gate for verified key Claim
pages. F-02B adds a separate deterministic, read-only currentness validator over
caller-supplied Claim frontmatter, current Source/Evidence registries, actual
Source bytes, Locator, and exact excerpt hash. F-03A adds an in-memory,
project-scoped scientific entity/relation registry plus deterministic backlink
and project-index projection over caller-supplied current pages. F-04A adds a
separate in-memory validator for host-declared Claim lifecycle transitions and
explicit coexistence of conflicting Claim/Result variants.

The Schema/path implementation is
[`tools/knowledge_artifacts.py`](../tools/knowledge_artifacts.py); the F-02B
closure is [`tools/claim_evidence.py`](../tools/claim_evidence.py) and is specified
in [`claim-evidence-currentness.md`](claim-evidence-currentness.md). F-03A is
[`tools/research_relations.py`](../tools/research_relations.py) and is specified in
[`research-relations.md`](research-relations.md). F-04A is
[`tools/claim_lifecycle.py`](../tools/claim_lifecycle.py) and is specified in
[`claim-lifecycle.md`](claim-lifecycle.md). None of these modules writes a
Markdown page body. Only F-02B opens policy-authorized Source bytes for exact
currentness validation; F-04A consumes its result without reopening a Source.
Later controlled writers and renderers must use these contracts instead of
guessing an artifact's role from body text or ad hoc filenames.

## P-08 responsibility boundary

The host Agent remains responsible for understanding the user's goal, deciding
what a page means, drafting or revising its body, and explicitly choosing
Source/Evidence links and stance. Research Core is responsible for deterministic
checks: canonical path and type, strict Schema v2 structure, registry identities,
Source version and hash, Locator/excerpt fidelity, verified-state currentness, and
future-version fail-closed behavior.

F-01/F-02 expose no MCP tool. Schema parsing and Claim currentness are internal
Core primitives, not task-level operations for the host. They use no LLM,
keyword scoring, or semantic heuristic and never infer stance or conflict.
F-02B reports currentness without changing Markdown, Claim status, Evidence, or
Source bindings. For F-03A, the host explicitly supplies entity identities and
directed relation labels; Core checks identity, canonical page binding, endpoint
integrity, and backlinks without inferring entities or relationship meaning. For
F-04A, the host explicitly chooses lifecycle targets and conflict membership;
Core checks current Schema v2, identity/timestamp invariants, a matching F-02B
proof for `verified`, and distinct Claim/Result coexistence without inferring a
conflict, winner, or replacement. F-05 controlled mixed/user-body protection and
persistent status materialization remain separate.

## Required frontmatter

Every current Schema v2 project knowledge page has exactly these top-level fields:

```yaml
---
schema_version: 2
kind: llmwiki-project-knowledge-page
project_id: tiny-study-0123456789ab
artifact_type: claim
title: Model Improves Recall
status: verified
ownership: generated
source_ids: []
evidence_refs:
  - evidence_id: evd-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    stance: supporting
generated_at: '2026-07-17T08:00:00Z'
updated_at: '2026-07-17T08:30:00Z'
last_verified_at: '2026-07-17T08:30:00Z'
---
```

Unknown or missing top-level fields fail closed. Current validation and
serialization require integer `schema_version: 2`. `kind` is fixed to
`llmwiki-project-knowledge-page`, and `project_id` uses the existing path-safe
project identity contract.

`title` is non-empty human-readable text without outer whitespace or control
characters. `source_ids` is a duplicate-free YAML sequence of `src-` plus 32
lowercase hexadecimal digits. `evidence_refs` is a YAML sequence whose entries
contain exactly `evidence_id` and `stance`: the ID is `evd-` plus 64 lowercase
hexadecimal digits, the stance is `supporting`, `opposing`, or `context`, and an
Evidence ID may appear only once regardless of stance.

For Schema v2, a verified key Claim must have a non-null `last_verified_at` and at
least one `supporting` reference. Canonical `claims/<slug>.md` detail pages are key;
`claims/index.md` is not. A pathless `artifact_type: claim` is conservatively
validated as key by the structural validator.

F-02B then validates a canonical Claim path against the requested project and
current registries. Every declared Source must be registered; every declared
Evidence ID must exist, belong to the project, and use a Source declared by the
Claim; its exact Source version/hash, Locator, and excerpt must still be current.
A verified key Claim remains current only when all declared Evidence is current,
at least one current reference is `supporting`, and
`last_verified_at == updated_at`. A verified `claims/index.md` still requires
current declared bindings and timestamp equality, but does not acquire a key-Claim
support requirement. The validator reports this closure and never rewrites a page
or status.

Schema v1 pages retain strict read-only parsing compatibility with their exact
`evidence_ids` field. The current validator and serializer reject v1, never infer
stances, and never rewrite or migrate a v1 page. Schema v3 and later fail closed.

## Types, status, and ownership

The 15 product artifact types are:

```text
overview
project_map
reproduction
architecture
paper
method
dataset
experiment
result
claim
open_question
project_status
risk
goal
plan
```

The auxiliary types are:

```text
project_index
decision
source
metric
```

`metric` is reserved as a semantic type for later reusable/library work. F-01A
does not invent a project path such as `metrics/` or `results/metrics/`; project
metric summaries remain `result` pages until a separately approved contract adds
another canonical location.

Statuses are closed to:

```text
draft | verified | stale | conflicting | rejected
```

`verified` is not model confidence. Every verified page requires a structurally
valid, non-null `last_verified_at`; verified key Claims also require a supporting
Schema v2 reference. F-02B does not grant or convert that state. It reports a
verified Claim as current only after the explicit registry, Source version/hash,
Locator/excerpt, all-declared-Evidence, current-support, and timestamp-equality
checks pass.

Ownership is closed to:

```text
generated | mixed | user
```

`mixed` is recorded but has no body-region or overwrite behavior in F-01A. F-05
must define protected regions, conflict handling, and controlled writes before a
regenerator or Web editor modifies mixed/user content.

## Timestamp contract

`generated_at` is the first controlled materialization time, including for
`ownership: user`; it is not a machine-ownership marker. `updated_at` is the most
recent controlled modification time. `last_verified_at` is nullable and records
the most recent real verification time when available.

Timestamps must be RFC 3339 date-times with an explicit known timezone and at
most microsecond precision. Canonical serialization converts them to UTC with a
`Z` suffix. The structural ordering is:

```text
generated_at <= updated_at

generated_at <= last_verified_at <= updated_at   # when non-null
```

For a verified Claim to remain current under F-02B, the stronger runtime closure
is:

```text
last_verified_at == updated_at
```

A later controlled modification therefore invalidates current verification until
a host-directed verification pass succeeds again. F-02B only reports the result;
it does not persist a status transition.

## Canonical project-relative paths

All paths passed to the contract are relative to one
`wiki/projects/<project_id>/` equivalent root. They use canonical POSIX `/`
separators. Absolute paths, drive paths, backslashes, empty components, `.`,
`..`, control characters, colons, traversal, and unknown layouts are rejected.
Detail names use lowercase kebab-case slugs; `index` is reserved for collection
indexes.

| Canonical path | `artifact_type` | Structural page role |
|---|---|---|
| `index.md` | `project_index` | project index |
| `overview.md` | `overview` | singleton |
| `project-map.md` | `project_map` | singleton |
| `reproduction.md` | `reproduction` | singleton |
| `architecture.md` | `architecture` | singleton |
| `papers/index.md` | `paper` | collection index |
| `papers/<slug>.md` | `paper` | detail |
| `methods/index.md` | `method` | collection index |
| `methods/<slug>.md` | `method` | detail |
| `datasets/index.md` | `dataset` | collection index |
| `datasets/<slug>.md` | `dataset` | detail |
| `experiments/index.md` | `experiment` | collection index |
| `experiments/<slug>.md` | `experiment` | detail |
| `results/index.md` | `result` | collection index |
| `results/<slug>.md` | `result` | detail |
| `claims/index.md` | `claim` | collection index |
| `claims/<slug>.md` | `claim` | detail |
| `open-questions.md` | `open_question` | singleton |
| `status.md` | `project_status` | singleton |
| `risks.md` | `risk` | singleton |
| `goals.md` | `goal` | singleton |
| `plans/backlog.md` | `plan` | backlog |
| `plans/daily/YYYY-MM-DD.md` | `plan` | daily plan |
| `decisions/<slug>.md` | `decision` | detail |
| `sources/<slug>.md` | `source` | detail |

The path identifies only a structural role and expected type. It does not infer a
paper, method, claim, or other research meaning from page contents. F-02A uses
only the canonical Claim role to distinguish `claims/index.md` from key
`claims/<slug>.md` details; a pathless Claim fails closed as key.

## Empty directory initialization boundary

F-01B connects the canonical collection paths to `ProjectLayout`. The exact
project-relative directory skeleton is:

```text
papers/
methods/
datasets/
experiments/
results/
claims/
plans/
plans/daily/
decisions/
sources/
```

`ProjectLayout.ensure_directories()` and B-01 registration create this skeleton
under either the default `wiki/projects/<project_id>/` root or the selected
external knowledge root. Initialization is idempotent. Registration may claim an
unregistered legacy empty skeleton containing any subset of canonical
directories, including the former `sources/`, `papers/`, `experiments/`,
`claims/`, and `plans/` set, and then fills missing directories. Unknown files,
unknown directories, symbolic-link entries, and content inside the unregistered
skeleton fail closed rather than being overwritten or adopted.

Directory initialization creates no Markdown page, does not read source or
research-binary content, does not create or rewrite a knowledge Schema, and does not decide research
semantics.

## F-03A entity, relation, and backlink projection

F-03A represents project-local `paper`, `method`, `dataset`, `experiment`,
`metric`, `result`, `claim`, `decision`, `question`, and `source` entities. An
explicit `identity_key` rather than the display title defines identity, so same
titles never trigger an automatic merge. Direct entities bind canonical detail
pages. A project metric binds an explicit anchor inside a result detail page and
a question binds an explicit anchor inside `open-questions.md`; this preserves
the F-01 path contract without inventing `metrics/` or `questions/` directories.
Entities may remain unmaterialized until a later controlled writer creates their
knowledge representation.

Relations are explicit, directed, project-local edges with caller-declared
lowercase kebab-case labels. Core validates endpoint existence, self-loop and
duplicate constraints, stable IDs, canonical serialization, and optional
Evidence-ID referential integrity. It does not apply a semantic endpoint matrix,
infer a relation from body text, infer stance/currentness, or merge same-named
objects.

The strict canonical JSONL registry and read-only renderer projection are defined
in [`research-relations.md`](research-relations.md). Current Schema v2 pages must
match entity project, path type/role, and direct-page title before the projection
is built. The projection contains deterministic entity groups and incoming/
outgoing relation IDs, but is not project `index.md` content. F-03A performs no
filesystem I/O or persistence; later approved writers must keep relation machine
state under `.llmwiki/` and curated Markdown under `wiki/projects/`.

## F-04A Claim lifecycle and conflict coexistence

F-04A validates host-declared transitions across the existing closed status set:
`draft`, `verified`, `stale`, `conflicting`, and `rejected`. Core intentionally
has no scientific transition matrix. It binds both current and proposed
frontmatter to the same materialized F-03A Claim identity, preserves immutable
identity fields while allowing a coordinated display-title rename to the current
registry title, requires a strictly advancing `updated_at`, preserves the most
recent real verification time across non-verified transitions, and classifies the
change without persisting it.

A target `verified` Claim must include `last_verified_at == updated_at` and a
matching current F-02B `ClaimEvidenceValidationResult` for the exact Claim
frontmatter fingerprint, project, path, Source IDs, directional Evidence
references, and verified-currentness outcome. F-04A fingerprints that result but
does not rerun Source access or
register Evidence. A non-verified target may report Evidence currentness as an
independent dimension and is never silently promoted.

Conflict coexistence is also explicit. The host supplies a stable project-local
conflict key and at least two distinct F-03A Claim variants, each already marked
`conflicting` and referencing one or more duplicate-free Result entities. Core
validates current Schema v2 Claim/Result page bindings and at least two distinct
Results overall. A Result may support multiple competing interpretations; Core
does not invent exclusive ownership, and Result pages need not themselves carry
`status: conflicting`. It retains every variant and emits no winner or
replacement. Opposing Evidence and relation labels do not infer a conflict. A
`conflicting` transition is complete only when it binds a coexistence proof whose
target path and frontmatter fingerprint match the proposed Claim revision.

The full read-only API, audit records, and fail-closed boundaries are specified
in [`claim-lifecycle.md`](claim-lifecycle.md). F-04 remains partial until later
controlled persistence and product integration can materialize accepted host
decisions without overwriting prior conclusions.

## F-05A controlled Markdown update planning

F-05A adds a pure, in-memory update planner for current Knowledge Schema v2 pages.
The host supplies the complete proposal and every semantic choice. Core validates the
canonical project-relative path, exact caller-supplied current-snapshot SHA-256 precondition, immutable
project/type/ownership/`generated_at`, strictly advancing `updated_at`, and the closed
`generated | mixed | user` body-ownership contract. Schema v1 remains read-only and
future versions fail closed.

`generated` and `user` pages assign the complete body to their declared owner. Mixed
pages use bounded, duplicate-free, LF/CRLF-delimited protected regions. Regeneration
takes the generated skeleton from the proposal but restores each region's exact user
content from the caller-supplied base snapshot; a user edit may change only region bodies and must preserve the
generated skeleton and ordered region IDs byte-for-byte. Unicode line separators are
ordinary content rather than hidden marker boundaries. A generator may create only
empty protected regions, so it cannot manufacture text and label it user-confirmed.

This ownership gate covers the Markdown body only. Structurally valid mutable
frontmatter changes are reported, not semantically authorized; callers must compose
F-02/F-03/F-04 proofs and explicit user authorization as applicable. The plan contains
canonical output bytes plus content-free hashes/metadata for a later writer. The plan
ID is correlation metadata rather than a write-authorization token; a writer must freshly
rebind the live base/proposal and trusted host/user decisions before exact-CAS persistence.
F-05A does not persist, lock, append audit state, read Sources/binaries, send externally, or
add CLI/MCP/Web behavior. See
[`controlled-markdown-writes.md`](controlled-markdown-writes.md). F-05 remains partial
until F-05B supplies exact-CAS persistence and an accountable audit record.

## Strict parsing and canonical serialization

`parse_knowledge_page(payload, path=...)` accepts bytes so it can enforce strict
UTF-8 and reads strict Schema v1 or v2 without migration. A page must begin with
exact `---` delimiters. Parsing uses a restricted PyYAML `SafeLoader` variant and
rejects duplicate keys at any mapping level, YAML aliases, merge keys,
unsafe/unknown tags, non-string mapping keys, malformed YAML, and NUL content.
YAML timestamp auto-construction is disabled so timestamp fields are validated
explicitly as strings.

`validate_knowledge_frontmatter(value, path=...)` validates current Schema v2 only,
returns an immutable object, and verifies that `artifact_type` agrees with the
path. `artifact_contract_for_path(path)` performs the pure path mapping.
`serialize_knowledge_frontmatter(value, path=...)` emits deterministic LF-only
Schema v2 frontmatter and rejects v1 rather than rewriting it. The immutable
object exposes `evidence_ids` as a derived read-only view; this does not infer a
stance for legacy pages. No function writes a file or serializes a page body.

## Explicit exclusions

F-01A remains the in-memory Schema/path slice; F-01B only integrates its
canonical empty directory skeleton with layout initialization and registration.
Combined F-01 does not:

- create or alter real `wiki/projects/<project_id>/*.md` files;
- generate any of the 15 page bodies or a unified index;
- read a research source or research-binary payload; or
- add a ResearchCoreService facade method, CLI command, MCP tool, Skill, Hook,
  Plugin, or Web surface.

F-02A is the structural directional-reference gate. F-02B is the read-only
registry/Source/Locator/excerpt currentness gate for caller-supplied Schema v2
Claim frontmatter. Its relocation inspection is report-only and it performs no
Markdown, Evidence-registry, Source-registry, run-state, or status write. It does
not migrate Schema v1, infer stance/conflicts, persist stale propagation, or add a
public interface.

F-05A now supplies in-memory mixed/user body protection and a content-free update
plan only. Filesystem persistence, conflict/audit ledger writes, product Web editing,
and persistent propagation of stale state remain F-05B or later authorized work.
