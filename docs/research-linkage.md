# E-05 Research Linkage Contract

E-05 adds the machine-only artifact
`.llmwiki/projects/<project_id>/indexes/research-linkage.json`. It is Schema v1
with `kind=llmwiki-research-linkage` and
`linkage_version=research-linkage-v1`.

## Inputs and provenance boundary

The host explicitly supplies `ResearchEntityObservation` and
`ResearchRelationObservation` values to
`ResearchCoreService.research_linkage(...)`. Entity kinds cover papers,
methods, innovations, datasets, implementations, claims, experiments, results,
and an explicit `unknown` fallback. Relations are directed and closed to
`implements`, `describes`, `introduces`, `uses`, `evaluates`, `supports`,
`contradicts`, `derived-from`, and `references`.

Each entity and relation carries one assertion class:

- `implementation` - a project implementation observation;
- `paper-claim` - a paper-derived claim, method, or innovation;
- `inference` - a host-declared interpretation, never silently promoted to an
  observed fact;
- `metadata` - a deterministic Manifest metadata candidate.

Evidence IDs are optional, duplicate-free, bounded, and validated. An
`inference` relation cannot claim `observed` certainty. Relation endpoints must
be declared entities in the same project, and self-loops and duplicate
relations are rejected. The Core does not infer scientific relationships from
paths, prose, or filenames.

## Honest fallback and gaps

When no semantic observations are supplied, E-05 emits a deterministic
`manifest-metadata-fallback`. It may identify bounded candidates from current
Manifest classification metadata, but it emits no relations and does not
pretend that a paper, method, or implementation relationship was read. The
`gaps` and `coverage` fields make missing paper, method, relationship, and
Evidence grounding explicit.

## Integrity and currentness

The artifact binds to the exact current `project-inventory-v4` Manifest
version, scan generation, ordinary-file count/bytes, and SHA-256. JSON is
strict UTF-8, canonical, duplicate-key-free, closed-field, and rejects
legacy/future versions, unknown fields, stale or tampered stable IDs, invalid
paths, and invalid endpoints. Stable entity, relation, and artifact IDs are
deterministically derived from their canonical inputs.

Generation and current loading use the per-project `indexes/machine-state.lock`,
write through atomic replacement, and recheck the exact Manifest immediately
before replace. `load_current_research_linkage(...)` rejects a stale artifact
and deterministically verifies the metadata fallback.

## Explicit non-goals

E-05 does not open registered source bytes, read research binaries, call an LLM,
write curated Markdown, persist Evidence records, infer conflicts or
scientific conclusions, or add CLI, MCP, Hook, or Web behavior. Later rendering
and orchestration units may consume this machine artifact only after current
loading.
