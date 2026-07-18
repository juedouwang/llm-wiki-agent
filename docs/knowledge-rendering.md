# E-07 Knowledge Rendering

## Scope

E-07 is the deterministic rendering bridge from the machine artifacts produced by
E-02 through E-06 to a complete, human-readable project knowledge package.  The
implementation is `tools/knowledge_renderer.py`; the host-independent entry point
is `ResearchCoreService.knowledge_render(project_id, ...)` (also exposed through
the compatibility alias `render_knowledge`).  It is intentionally not a source
reader, an LLM workflow, or a scientific decision maker.

## Product contract

A successful build always describes all fifteen product deliverables, even when
upstream evidence is absent:

| Key | Canonical path | Meaning |
|---|---|---|
| `overview` | `overview.md` | bounded project overview |
| `project_map` | `project-map.md` | E-02 map summary |
| `reproduction` | `reproduction.md` | reproducibility inputs and gaps |
| `architecture` | `architecture.md` | E-03/E-04 structure and data flow |
| `paper` | `papers/index.md` | papers and literature links |
| `method` | `methods/index.md` | methods and innovations |
| `dataset` | `datasets/index.md` | datasets and provenance |
| `experiment` | `experiments/index.md` | experiment chains |
| `result` | `results/index.md` | results and metrics |
| `claim` | `claims/index.md` | claims and directional Evidence refs |
| `open_question` | `open-questions.md` | unresolved questions |
| `project_status` | `status.md` | current project state |
| `risk` | `risks.md` | risks and missing information |
| `goal` | `goals.md` | initial goal suggestions |
| `plan` | `plans/backlog.md` | initial backlog |

The renderer also emits detail pages for grounded collection items, a uniform
`index.md`, and a deterministic `plans/daily/<YYYY-MM-DD>.md` page.  Detail pages
use the canonical Schema v2 path/type mapping; two items with the same display
title remain separate through an identity digest rather than being merged.

## Missing and untrusted input

Machine artifacts and host observations are optional and bounded.  A missing,
malformed, stale, or ungrounded input produces a non-empty `DRAFT` placeholder
with a stable reason code and a next action.  It is never represented as an empty
successful page.  E-07 does not infer scientific claims, Evidence stance,
conflicts, or user goals.  The `goal` and `plan` outputs explicitly remain drafts
when the host has not supplied user-authorized direction.  C-07 research-binary
metadata extraction remains `deferred/not_started`; the renderer records that gap
rather than treating binary files as processed.

## Storage and write boundaries

Machine state remains under `.llmwiki/projects/<project_id>/`.  Curated Markdown
is written only under the registered knowledge root, normally
`wiki/projects/<project_id>/` or the explicitly configured custom knowledge root.
The renderer never writes the registered research source project and never places
paths, hashes, indexes, or run state in curated Markdown.  It sanitizes generated
text so absolute paths, long hashes, and NUL characters are not returned in page
content or result DTOs.

When `persist=False`, E-07 only builds/validates controlled plans and returns
content-free observations.  When persistence is requested, every page goes
through the F-05A controlled Markdown plan and the F-05B authorization/CAS/audit
boundary.  Existing Schema v1, future-version, malformed, user-owned, and
non-draft pages are protected and reported rather than rewritten.  Mixed pages
preserve the exact LF/CRLF-delimited `llmwiki:user-region` bytes.  One page failure
is isolated in the result so other pages can still be rendered; a partial result
is explicit.

E-07 performs no LLM call, external send, source-byte read, research-binary read,
CLI/MCP/Web operation, or semantic authorization.  Host callers remain responsible
for composing F-02/F-03/F-04 validation and user decisions before authorizing
frontmatter changes.

## Idempotency and validation

Rendering uses stable timestamps/identity inputs and compares normalized current
Schema v2 page identity before writing.  Re-running against unchanged inputs is
reported as unchanged rather than creating a new revision.  The accepted E-07
validation on 2026-07-18 was:

- focused renderer suite: **14 passed**;
- dependent renderer/Knowledge Schema/controlled Markdown/Core suites:
  **106 passed, 1 skipped**;
- full regression: **732 passed, 28 skipped**;
- changed-file Ruff and `py_compile`: passed;
- `git diff --check`: passed;
- C-07: **deferred/not_started**.

The implementation checkpoint is commit `9f4194b` and tag
`checkpoint/e-07-knowledge-rendering`.
