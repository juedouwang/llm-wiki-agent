# LLM Wiki Agent — Schema & Workflow Instructions

This wiki is maintained entirely by your coding agent. No API key or Python scripts needed — just open this repo in Codex, OpenCode, or any agent that reads this file, and talk to it.

## How to Use

Describe what you want in plain English:
- *"Ingest this file: raw/papers/my-paper.md"*
- *"What does the wiki say about transformer models?"*
- *"Check the wiki for orphan pages and contradictions"*
- *"Build the knowledge graph"*

Or use shorthand triggers:
- `ingest <file>` → runs the Ingest Workflow
- `query: <question>` → runs the Query Workflow
- `health` → runs the Health Workflow (fast, every session)
- `lint` → runs the Lint Workflow (expensive, periodic)
- `build graph` → runs the Graph Workflow
- `raw-md <project-root>` -> runs the Raw Markdown Evidence Workflow

---

## Directory Layout

```
raw/          # Immutable source documents — never modify these
wiki/         # Agent owns this layer entirely
  index.md    # Catalog of all pages — update on every ingest
  log.md      # Append-only chronological record
  overview.md # Living synthesis across all sources
  sources/    # One summary page per source document
  entities/   # People, companies, projects, products
  concepts/   # Ideas, frameworks, methods, theories
  syntheses/  # Saved query answers
graph/        # Auto-generated graph data
tools/        # Standalone Python scripts
  health.py   # Structural checks (deterministic, no LLM calls)
  lint.py     # Content quality checks (uses LLM for semantic analysis)
  build_graph.py  # Knowledge graph generation
```

---

## Project-Scoped Storage Contract (Schema v1)

New research-project features must keep machine state and human-readable knowledge separate:

```text
.llmwiki/projects/<project_id>/   # local machine state and generated evidence
wiki/projects/<project_id>/       # curated Markdown knowledge for people and agents
```

The machine-state tree uses `project.yaml`, `manifest.jsonl`, `sources.jsonl`, `extracted/`, `indexes/`, and `runs/`. The curated tree uses `overview.md`, `sources/`, `papers/`, `experiments/`, `claims/`, and `plans/`.

Rules:
1. New structured machine records, including `project.yaml` and JSON/JSONL files, must carry `schema_version`.
2. Missing `schema_version` is legacy v0 and may be read only through the compatibility layer.
3. Future unsupported schema versions must fail closed.
4. Never silently move, rewrite, or delete legacy `<project-name>-wiki/` data.
5. Do not place local paths, hashes, indexes, or run state in `wiki/projects/`.
6. Do not place curated research summaries or plans in `.llmwiki/`.
7. Use `tools/project_layout.py` for path validation, initialization, version checks, and legacy resolution.

Register an external research project with:

```bash
python tools/project.py register <project-path> --json
```

Registration is deterministic and source-read-only. It creates a stable project identity and empty external storage only; it must not be described as a completed scan. `--knowledge-root` selects the parent directory for curated per-project Markdown.

Before any new project inventory or extraction workflow, load `tools/scan_policy.py`. The policy reads an optional root `.llmwikiignore`, applies protected/default and explicit include/exclude rules, and returns explanations for boundary, size, sensitive-path, external-send, and symlink decisions. Protected `.git/`, `.hg/`, `.svn/`, and `.llmwiki/` paths cannot be re-included. Research Core external sends default to `local-only`, and sensitive raw content must never be sent externally. B-02 policy evaluation is not a scan and must not create a Manifest or modify the source. See `docs/scan-policy.md`.

Inventory a B-01 registered project with:

```bash
python tools/project.py inventory <project_id> --json
```

B-03 established the accountable directory ledger: every in-scope regular file is recorded regardless of format, excluded files and pruned-directory boundaries remain reconcilable, and symbolic links use B-02 root/cycle/duplicate checks. B-04 adds scan generations and conservative local SHA-256 reuse. B-05 adds deterministic format, language, and research-role classification. B-06 writes the current `project-inventory-v4` artifact and gives every ordinary file a versioned `processing_status`, `read_depth`, stable reason code, and non-empty reason; inventory remains `discovered` rather than pretending extraction succeeded. A bounded classification prefix may be read only when B-02 grants local raw-content access. Sensitive and oversized files remain policy-limited after the B-04 hash; source samples and raw content must not be persisted, sent externally, passed to an LLM, or written back to the source project. Do not add B-08 coverage reporting, extraction, Evidence, MCP, Hook, or Web behavior to this inventory step. See `docs/project-inventory.md`, `docs/file-fingerprints.md`, `docs/file-classification.md`, and `docs/manifest-file-state.md`.

Reconcile a B-01 registered project with:

```bash
python tools/project.py reconcile <project_id> --json
```

H-07 reconciliation always uses a full deterministic scan through `classify` as its correctness fallback. Host events and explicit dirty paths are untrusted hints only; the explicit CLI and `llmwiki_reconcile` MCP tool must work when Hooks are disabled and when hints are absent or inconsistent. Snapshot the event ledger before the scan, acknowledge only that starting snapshot after exact Manifest/coverage hash and byte validation with zero coverage failures, and leave failures plus post-snapshot events pending. Inventory, coverage, project-run mutations/orchestration, and reconciliation share the stable per-project `indexes/machine-state.lock`; host events retain `events.jsonl.lock`, and the only nested order is machine-state then event lock. Lock files remain on disk after release. Persist the strict Schema v1 checkpoint only at `.llmwiki/projects/<project_id>/indexes/reconciliation-state.json`. Reconciliation may update machine Manifest, coverage, run, projection, and checkpoint state, but it must not modify the source project or curated knowledge. Do not describe this boundary as H-05 selective extraction or selective knowledge refresh. See `docs/project-reconciliation.md`.

For project registration, scan/inventory, coverage, reconciliation, and source-open integrations, use the host-independent `tools.research_core.ResearchCoreService` facade. CLI and future MCP/host adapters must delegate to this boundary rather than duplicating filesystem workflows. See `docs/research-core-service.md`.

The minimal MCP stdio adapter is `python -m tools.research_mcp --workspace-root <workspace>`. Its project-context, coverage, reconciliation, and source-open tools delegate to host-safe `ResearchCoreService` views; all advertised input/output JSON Schemas are enforced. Coverage and reconciliation write deterministic machine state and are not read-only. Source-open must enforce current Manifest content policy, deny sensitive or ignored files, omit absolute/storage/Git-origin paths, and may allow ordinary local access even when independent external sends are `local-only`. Query and plan must return explicit `capability-unavailable` errors until their real Core roadmap slices land. The adapter must not echo caller-controlled sensitive inputs or return raw content outside explicit policy-authorized source-open. See `docs/research-mcp-server.md`.

Existing top-level `wiki/` workflows and the current `raw-md` output layout remain compatible during migration. See `docs/project-storage-layout.md`.

## Codex Research Adapter (J-05)

The reference Codex package is `plugins/llmwiki-research/`. Installed copies
locate this checkout through `LLMWIKI_CORE_ROOT` and write project state under
`LLMWIKI_WORKSPACE_ROOT` (defaulting to the validated Core root). Use the
host-safe MCP operations for project context, Host Context Pack, coverage,
policy-authorized source-open, and explicit `llmwiki_reconcile`.

The optional asynchronous `PostToolUse` Hook is an untrusted H-04 hint producer
only. It requires an existing `LLMWIKI_PROJECT_ID`, must normalize paths beneath
the registered source root, and must never register, scan, extract, reconcile,
acknowledge checkpoints, or update curated Markdown. Always keep explicit
reconciliation available when Hooks are disabled, unavailable, malformed, or
untrusted. `llmwiki_query` and `llmwiki_plan` currently return
`capability-unavailable`; do not claim Verified Query or planning from J-05.

See `docs/codex-reference-adapter.md`.

---

## Page Format

Every wiki page uses this frontmatter:

```yaml
---
title: "Page Title"
type: source | entity | concept | synthesis
tags: []
sources: []       # list of source slugs that inform this page
last_updated: YYYY-MM-DD
---
```

Use `[[PageName]]` wikilinks to link to other wiki pages.

---

## Raw Markdown Evidence Workflow

Triggered by: *"raw-md <project-root>"*, *"build raw-md for <project-root>"*, *"initialize project wiki raw layer"*, or any request to convert a whole project into raw Markdown evidence.

Run:

```bash
python tools/raw_md.py <project-root>
```

Default output goes to `<project-root>/<project-name>-wiki/`:

```text
raw-md/primary/              # mirrored source paths, one .md page per input file
state/raw-md-manifest.json   # machine-readable conversion manifest
reports/raw-md-report.md     # human-readable conversion report
```

Rules:
1. This workflow is deterministic and must not call an LLM API.
2. Never modify source project files; write only to the generated project wiki directory.
3. Treat `raw-md/` as the raw evidence layer. It preserves source paths, hashes, byte counts, converters, status, direct code fences, document conversion output, and metadata-only placeholders.
4. Treat `wiki/sources/`, `wiki/entities/`, `wiki/concepts/`, and `wiki/overview.md` as curated knowledge produced after reading evidence. Do not bulk-copy every `raw-md/` file into `wiki/sources/`.
5. For exact values, implementation details, disputed claims, or low-confidence summaries, trace back from curated wiki pages to the relevant `raw-md/` pages.

---

## Ingest Workflow

Triggered by: *"ingest <file>"*

**Supported formats:** Markdown (`.md`) is ingested directly. Non-markdown files (`.pdf`, `.docx`, `.pptx`, `.xlsx`, `.html`, `.txt`, `.csv`, `.json`, `.xml`, `.rst`, `.rtf`, `.epub`, `.ipynb`, `.yaml`, `.yml`, `.tsv`, `.wav`, `.mp3`) are auto-converted in memory via [markitdown](https://github.com/microsoft/markitdown) before ingestion. Ingest must not write converted `.md` files beside source files. Use `--no-convert` to skip auto-conversion.

Steps (in order):
1. Read the source document fully (auto-convert in memory if non-markdown)
2. Read `wiki/index.md` and `wiki/overview.md` for current wiki context
3. Write `wiki/sources/<slug>.md` — use the source page format below
4. Update `wiki/index.md` — add entry under Sources section
5. Update `wiki/overview.md` — revise synthesis if warranted
6. Update/create entity pages for key people, companies, projects mentioned
7. Update/create concept pages for key ideas and frameworks discussed
8. Flag any contradictions with existing wiki content
9. Append to `wiki/log.md`: `## [YYYY-MM-DD] ingest | <Title>`
10. **Post-ingest validation** — check for broken `[[wikilinks]]`, verify all new pages are in `index.md`, print a change summary

### Source Page Format

```markdown
---
title: "Source Title"
type: source
tags: []
date: YYYY-MM-DD
source_file: raw/...
---

## Summary
2–4 sentence summary.

## Key Claims
- Claim 1
- Claim 2

## Key Quotes
> "Quote here" — context

## Connections
- [[EntityName]] — how they relate
- [[ConceptName]] — how it connects

## Contradictions
- Contradicts [[OtherPage]] on: ...
```

### Domain-Specific Templates

If the source falls into a specific domain (e.g., personal diary, meeting notes), the agent should use a specialized template instead of the default generic one above:

#### Diary / Journal Template
```markdown
---
title: "YYYY-MM-DD Diary"
type: source
tags: [diary]
date: YYYY-MM-DD
---
## Event Summary
...
## Key Decisions
...
## Energy & Mood
...
## Connections
...
## Shifts & Contradictions
...
```

#### Meeting Notes Template
```markdown
---
title: "Meeting Title"
type: source
tags: [meeting]
date: YYYY-MM-DD
---
## Goal
...
## Key Discussions
...
## Decisions Made
...
## Action Items
...
```

---

## Query Workflow

Triggered by: *"query: <question>"*

Steps:
1. Read `wiki/index.md` to identify relevant pages
2. Read those pages
3. If the question needs exact source fidelity (numbers, code behavior, file paths, conflicts, or detailed quotations), read the relevant `raw-md/` pages before finalizing.
4. Synthesize an answer with inline citations as `[[PageName]]` wikilinks and mention raw-md paths when the answer depends on raw evidence.
5. Ask the user if they want the answer filed as `wiki/syntheses/<slug>.md`

---

## Lint Workflow

Triggered by: *"lint"*

Check for:
- **Orphan pages** — wiki pages with no inbound `[[links]]` from other pages
- **Broken links** — `[[WikiLinks]]` pointing to pages that don't exist
- **Contradictions** — claims that conflict across pages
- **Stale summaries** — pages not updated after newer sources
- **Missing entity pages** — entities mentioned in 3+ pages but lacking their own page
- **Sparse pages** — pages with fewer than 2 outbound `[[wikilinks]]` (link density budget)
- **Data gaps** — questions the wiki can't answer; suggest new sources

Graph-aware checks (require `graph.json` from `build graph`):
- **Hub stubs** — god nodes (degree > μ+2σ) with thin content (< 500 chars)
- **Fragile bridges** — community pairs connected by only 1 edge
- **Isolated communities** — clusters with zero external connections

Output a lint report and ask if the user wants it saved to `wiki/lint-report.md`.

---

## Health Workflow

Triggered by: *"health"*

Run: `python tools/health.py` (or `python tools/health.py --json` for machine-readable output)

Fast structural integrity checks — **zero LLM calls**, safe to run every session:
- **Empty / stub files** — pages with no content beyond frontmatter (rate-limit damage)
- **Index sync** — `wiki/index.md` entries vs actual files on disk
- **Log coverage** — source pages missing a corresponding `ingest` entry in `wiki/log.md`

Output a health report. Use `--save` to write to `wiki/health-report.md`.

### Health vs Lint Boundary

| Dimension | `health` | `lint` |
|---|---|---|
| **Scope** | Structural integrity | Content quality |
| **LLM calls** | Zero | Yes (semantic analysis) |
| **Cost** | Free | Tokens |
| **Frequency** | Every session, before other work | Every 10-15 ingests |
| **Checks** | Empty files, index sync, log sync | Orphans, broken links, contradictions, gaps |
| **Tool** | `tools/health.py` | `tools/lint.py` |
| **Run order** | First (pre-flight) | After health passes |

> Run `health` first — linting an empty file wastes tokens.

---

## Graph Workflow

Triggered by: *"build graph"*

First try: `python tools/build_graph.py --open`

If Python/deps unavailable, build manually:
1. Search for all `[[wikilinks]]` across wiki pages
2. Build nodes (one per page) and edges (one per link)
3. Infer implicit relationships not captured by wikilinks — tag `INFERRED` with confidence score; low confidence → `AMBIGUOUS`
4. Write `graph/graph.json` with `{nodes, edges, built: date}`
5. Write `graph/graph.html` as a self-contained vis.js visualization

---

## Naming Conventions

- Source slugs: `kebab-case` matching source filename
- Entity pages: `TitleCase.md` (e.g. `OpenAI.md`, `SamAltman.md`)
- Concept pages: `TitleCase.md` (e.g. `ReinforcementLearning.md`, `RAG.md`)

## Index Format

```markdown
# Wiki Index

## Overview
- [Overview](overview.md) — living synthesis

## Sources
- [Source Title](sources/slug.md) — one-line summary

## Entities
- [Entity Name](entities/EntityName.md) — one-line description

## Concepts
- [Concept Name](concepts/ConceptName.md) — one-line description

## Syntheses
- [Analysis Title](syntheses/slug.md) — what question it answers
```

## Log Format

`## [YYYY-MM-DD] <operation> | <title>`

Operations: `raw-md`, `ingest`, `query`, `health`, `lint`, `graph`, `report`

---

## Graph Health Report

Triggered by: *"graph report"* or `python tools/build_graph.py --report`

The `--report` flag generates a structured graph health report covering:
- **Health summary** — edges/node ratio, orphan %, community count, link density
- **Orphan nodes** — pages with zero graph connections
- **God nodes** — hub pages with degree > μ+2σ (disproportionate connectivity)
- **Fragile bridges** — community pairs connected by only 1 edge
- **Phantom hubs** — `[[wikilinks]]` referenced by 2+ existing pages but pointing to non-existent pages (page creation signals)

Use `--save` to write the report to `graph/graph-report.md`.

---

## Phase 3 Design Constraints (Auto-Linking — Open)

Phase 3 proposes automatic `[[wikilink]]` insertion based on graph analysis. The following hard rules apply:

### Promotion Gate: `draft → stable`
- Auto-linked edges start as `DRAFT` (visible in graph, not written to page body)
- A dedicated `promote` pass validates source grounding + consistency
- Only edges that pass get materialized as `[[wikilinks]]` in the page
- **Link density budget**: a page must have ≥2 outbound wikilinks before promotion

### Hard Rules
| ID | Rule | Rationale |
|---|---|---|
| HG-WA-01 | Graph layer MUST NOT auto-create pages from broken links — report only | LLM ingest produces hallucinated wikilinks; auto-creating amplifies noise |
| HG-WA-02 | New slash commands MUST NOT duplicate existing command coverage | Prevents user confusion; merge into existing commands instead |
