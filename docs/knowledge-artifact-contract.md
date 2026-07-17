# Project Knowledge Artifact Contract (F-01A, Schema v1)

F-01A defines the deterministic frontmatter and project-relative path contract
for curated Markdown under `<knowledge_projects_root>/<project_id>/`. It is an
in-memory validation layer only: it does not create directories, read a research
source, write a real knowledge page, synthesize prose, resolve Evidence, or expose
CLI/MCP/Hook/Skill/Web behavior.

The implementation is [`tools/knowledge_artifacts.py`](../tools/knowledge_artifacts.py).
Later controlled writers and renderers must use this contract instead of guessing
an artifact's role from body text or ad hoc filenames.

## P-08 responsibility boundary

The host Agent remains responsible for understanding the user's goal, deciding
what a page means, drafting or revising its body, and proposing source/Evidence
links. Research Core is responsible for deterministic checks: whether a path is
canonical, the page type matches that path, the frontmatter has the exact current
schema, IDs and timestamps are structurally valid, and a future schema fails
closed.

F-01A therefore exposes no MCP tool. Schema parsing is an internal Core primitive,
not a task-level operation for the host. It uses no LLM, keyword scoring, or
semantic heuristic, and it does not claim F-02 Claim–Evidence validation or F-05
controlled mixed/user-body protection.

## Required frontmatter

Every F-01A project knowledge page has exactly these top-level fields:

```yaml
---
schema_version: 1
kind: llmwiki-project-knowledge-page
project_id: tiny-study-0123456789ab
artifact_type: overview
title: Tiny Study Overview
status: draft
ownership: generated
source_ids: []
evidence_ids: []
generated_at: '2026-07-17T08:00:00Z'
updated_at: '2026-07-17T08:00:00Z'
last_verified_at: null
---
```

Unknown or missing top-level fields fail closed. `schema_version` must be the
integer `1`; missing, legacy, malformed, and future versions are rejected rather
than silently upgraded. `kind` is fixed to `llmwiki-project-knowledge-page`, and
`project_id` uses the existing path-safe project identity contract.

`title` is non-empty human-readable text without outer whitespace or control
characters. `source_ids` and `evidence_ids` are duplicate-free YAML sequences
using the existing Core forms:

- `src-` plus 32 lowercase hexadecimal digits;
- `evd-` plus 64 lowercase hexadecimal digits.

F-01A validates identifiers only. It does not open the source/Evidence registries,
check Evidence currentness, or require a verified Claim to have Evidence; those
are F-02 and later integration responsibilities.

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

`verified` is not model confidence. F-01A only requires a structurally valid,
non-null `last_verified_at` for that state; it never grants or converts the state.
Source-version/Evidence currentness remains a later Core check.

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
paper, method, claim, or other research meaning from page contents.

## Strict parsing and canonical serialization

`parse_knowledge_page(payload, path=...)` accepts bytes so it can enforce strict
UTF-8. A page must begin with exact `---` delimiters. Parsing uses a restricted
PyYAML `SafeLoader` variant and rejects duplicate keys at any mapping level, YAML
aliases, merge keys, unsafe/unknown tags, non-string mapping keys, malformed YAML,
and NUL content. YAML timestamp auto-construction is disabled so timestamp fields
are validated explicitly as strings.

`validate_knowledge_frontmatter(value, path=...)` returns an immutable validated
frontmatter object and verifies that `artifact_type` agrees with the path.
`artifact_contract_for_path(path)` performs the pure path mapping.
`serialize_knowledge_frontmatter(value, path=...)` returns a deterministic,
LF-only in-memory frontmatter block; it does not write a file or serialize a page
body.

## Explicit exclusions

F-01A does not:

- modify `tools/project_layout.py`, project registration, or directory initialization;
- create or alter real `wiki/projects/<project_id>/*.md` files;
- generate any of the 15 page bodies or a unified index;
- add source-project or research-binary reads;
- implement Claim–Evidence semantics, relationships, stale propagation, retrieval,
  controlled mixed/user writes, or migration;
- add a ResearchCoreService facade method, CLI command, MCP tool, Skill, Hook,
  Plugin, or Web surface.

Those remain separately authorized Roadmap work.
