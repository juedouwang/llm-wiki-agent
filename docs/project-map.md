# Deterministic Project Map (E-02)

- Status: implemented for the bounded E-02 Core slice
- Machine artifact: `.llmwiki/projects/<project_id>/indexes/project-map.json`
- Schema: machine Schema v1
- Kind/version: `llmwiki-project-map` / `project-map-v1`
- Public module: `tools.project_map`
- Core facade: `ResearchCoreService.project_map(project_id)`
- Validation: `tests/test_project_map.py`

## Purpose

E-02 creates a stable project outline before any semantic deep read. It consumes
only the exact current B-06 `project-inventory-v4` Manifest and derives directory
shape, format/language/research-role distributions, and bounded candidates for
entrypoints, dependency declarations, configuration, run scripts, and key files.
The output is deterministic for the same Manifest even when in-memory row order
differs.

This is a machine index, not curated knowledge. It does not summarize source
content, infer architecture or scientific meaning, generate Markdown, call an
LLM, or send data externally. Policy-limited, sensitive, oversized, unsupported,
and binary rows may contribute only their already-recorded Manifest metadata.

## Artifact contract

The canonical UTF-8 JSON document contains exactly:

- `schema_version`, `kind`, `map_version`, and `project_id`;
- an exact Manifest binding: version, scan generation, ordinary-file/byte totals,
  and SHA-256 of the current Manifest bytes;
- a fixed derivation boundary: `manifest-metadata-only`,
  `source_content_read=false`, and `llm_used=false`;
- fixed directory/candidate limits;
- deterministic directory aggregates and classification distributions;
- ranked, duplicate-free candidates with path, score, reason codes, Manifest
  classification, size, and rank;
- explicit omission counts for every bounded collection.

Legacy v0, future schema versions, unknown fields, duplicate JSON keys, malformed
values, noncanonical JSON, symlinked machine-state paths, stale Manifest bindings,
and validly shaped payload tampering fail closed. `load_project_map(...)` performs
structural parsing only. `load_current_project_map(...)` independently rebuilds
the complete expected payload from the current Manifest and authorizes use only
when every field matches.

## Generation and concurrency

`generate_project_map(...)` runs under the shared per-project
`indexes/machine-state.lock`. It validates the registered layout and exact
Manifest snapshot, derives the complete map in memory, rechecks the Manifest
bytes immediately before atomic replacement, and validates the committed bytes.
A stale map can be replaced by a map for a newer Manifest generation. A map that
claims the current Manifest identity but differs from deterministic truth is
treated as tampering and is not silently repaired.

Generation requires the already-created machine index directory. It creates no
curated Markdown directory, changes no registration or source-project bytes, and
never opens a registered source file.

## Public API

```python
from tools.project_map import (
    build_project_map,
    generate_project_map,
    load_current_project_map,
    load_project_map,
)

result = generate_project_map(workspace_root, project_id)
current = load_current_project_map(workspace_root, project_id)
```

`build_project_map(...)` is the pure derivation boundary for a validated
`ProjectManifest` plus an exact Manifest byte snapshot or SHA-256. Supplying
disagreeing hash inputs is rejected. `ProjectMapResult.as_dict()` is local and
path-bearing; E-02 adds no CLI, MCP, Hook, or Web operation.

## Explicit non-goals

E-02 does not implement E-03 hierarchical understanding, E-04 execution/data
flow, E-05 paper/method/dataset linkage, E-06 experiment chains, E-07 Markdown
rendering, or the full E-08 one-action workflow. C-07 remains deferred and no
research-binary metadata extractor is introduced here.
