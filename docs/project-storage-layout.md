# Project Storage Layout (Schema v1)

A-03 establishes the storage boundary used by the research-assistant roadmap. It separates local machine state from human-readable research knowledge before project registration, scanning, retrieval, and planning are added.

## Storage contract

```text
llm-wiki-agent/
?? .llmwiki/
?  ?? schema.json
?  ?? projects/
?     ?? <project_id>/
?        ?? project.yaml
?        ?? manifest.jsonl
?        ?? sources.jsonl
?        ?? extracted/
?        ?? indexes/
?        ?? runs/
?? wiki/
   ?? projects/
      ?? <project_id>/
         ?? overview.md
         ?? sources/
         ?? papers/
         ?? experiments/
         ?? claims/
         ?? plans/
```

| Root | Owner | Intended content | Git policy |
|---|---|---|---|
| `.llmwiki/projects/<project_id>/` | Deterministic tools | Local project registration, manifests, source records, extracted evidence, indexes, run state | Ignored by default because it may contain local paths, hashes, and generated data |
| `wiki/projects/<project_id>/` | Agent and user review | Curated Markdown knowledge intended for reading, review, editing, and long-term retention | Version-controlled |

Machine-generated evidence must not be bulk-copied into the human knowledge tree. Curated claims and summaries should point back to stable source/evidence identifiers introduced by later tasks.

## Schema version rules

`tools/project_layout.py` defines `CURRENT_SCHEMA_VERSION = 1`.

- New structured machine-state documents, including `project.yaml` and JSON/JSONL records, must include `schema_version`.
- A JSON object without `schema_version` is read as legacy schema v0 only when legacy compatibility is enabled.
- Reading legacy data never rewrites or migrates it implicitly.
- Data with a future unsupported schema version fails closed.
- Migration must be an explicit, separately tested operation.

The raw Markdown manifest and generated raw-md page frontmatter now include `schema_version: 1`. The existing artifact-specific `version` field remains in place and has a different purpose.

## Legacy layout compatibility

Before A-03, `tools/raw_md.py` used this project-local layout:

```text
<project-root>/<project-name>-wiki/
?? raw-md/
?? state/raw-md-manifest.json
?? reports/raw-md-report.md
```

That command and layout remain supported. `resolve_project_layout()` follows these rules without moving data:

1. If project-scoped v1 state exists, prefer `.llmwiki/projects/<project_id>/`.
2. Otherwise, if the supplied legacy wiki root exists, resolve it as the active legacy layout.
3. When both exist, prefer v1 and retain the legacy manifest as an explicit fallback candidate.
4. Never delete, rename, or silently migrate legacy files.

A later migration task may copy validated legacy evidence into the project-scoped layout. A-03 only supplies the compatibility boundary.

## API example

```python
from pathlib import Path
from tools.project_layout import ProjectLayout, resolve_project_layout

layout = ProjectLayout(Path.cwd(), "my-study")
layout.ensure_directories()

resolution = resolve_project_layout(
    Path.cwd(),
    "my-study",
    legacy_wiki_root=Path("/path/to/my-study/my-study-wiki"),
)
print(resolution.mode)
print(resolution.manifest_candidates)
```

`project_id` generation and registration are intentionally not part of A-03. Callers must currently supply a lowercase, path-safe identifier. B-01 will define how a stable ID is derived and retained.

## A-03 non-goals

A-03 does not implement:

- project registration;
- `.llmwikiignore` or scan policies;
- the full file Manifest record schema;
- content extraction relocation;
- source IDs or evidence locators;
- legacy-data migration;
- semantic knowledge extraction;
- retrieval or task planning.

Those features build on this storage contract in later B/C/D tasks.
