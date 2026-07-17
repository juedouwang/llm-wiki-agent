# Project Knowledge Artifact Contract (F-01A / F-02A, Schema v2)

F-01A defines the deterministic project-relative path contract for curated
Markdown under `<knowledge_projects_root>/<project_id>/`. F-02A advances the
current frontmatter contract to Knowledge Schema v2 with directional Evidence
references and a structural gate for verified key Claim pages. This remains an
in-memory validation layer only: it does not create directories, read a research
source, write a real knowledge page, synthesize prose, resolve an Evidence
registry, check source health/currentness, or expose CLI/MCP/Hook/Skill/Web
behavior.

The implementation is [`tools/knowledge_artifacts.py`](../tools/knowledge_artifacts.py).
Later controlled writers and renderers must use this contract instead of guessing
an artifact's role from body text or ad hoc filenames.

## P-08 responsibility boundary

The host Agent remains responsible for understanding the user's goal, deciding
what a page means, drafting or revising its body, and proposing source/Evidence
links. Research Core is responsible for deterministic checks: whether a path is
canonical, the page type matches that path, Schema v2 directional reference
entries are structurally valid, key Claim verification has the required supporting
reference and timestamp, and a future schema fails closed.

F-01A/F-02A expose no MCP tool. Schema parsing is an internal Core primitive, not
a task-level operation for the host. It uses no LLM, keyword scoring, or semantic
heuristic. F-02A does not establish Evidence registry existence, source health, or
currentness; that remains F-02B. F-05 controlled mixed/user-body protection also
remains separate.

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
validated as key. These checks are structural only: F-02A does not open an
Evidence registry or source-health state and does not prove that a referenced ID
exists or is current. F-02 remains partial pending F-02B currentness validation.

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
Schema v2 reference. The validator never grants or converts the state, and
source-version/Evidence currentness remains F-02B work.

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
- add source-project or research-binary reads;
- load an Evidence registry or source-health state, establish Evidence currentness,
  propagate stale state, retrieve sources, perform migration, or control mixed/user
  writes;
- add a ResearchCoreService facade method, CLI command, MCP tool, Skill, Hook,
  Plugin, or Web surface.

F-02A is the structural directional-reference slice only. Overall F-02 remains
partial until F-02B adds registry/source currentness validation. Controlled
mixed/user Markdown writes, conflict audit records, and protection of
user-confirmed content remain F-05 work. Those capabilities require their own
authorization.
