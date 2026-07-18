# E-05 Research Linkage Contract

E-05 adds the machine-only artifact
`.llmwiki/projects/<project_id>/indexes/research-linkage.json`. The envelope is
Schema v1 with `kind=llmwiki-research-linkage`; the current closed artifact
version is `linkage_version=research-linkage-v2`. Legacy v1 and future versions
fail closed and are not migrated.

## Inputs and semantic ownership

The host explicitly supplies `ResearchEntityObservation` and
`ResearchRelationObservation` values to
`ResearchCoreService.research_linkage(...)`. Entity kinds cover papers,
methods, innovations, datasets, configurations, implementations, claims,
experiments, results, and an explicit `unknown` fallback.

Directed relation labels are also host declarations. Core accepts only bounded
lowercase kebab-case labels; it does not maintain a scientific-semantic
whitelist or infer a relation from paths, prose, or filenames. This keeps the
host Agent responsible for what a relation means while Core validates its
shape, endpoints, provenance, and deterministic identity.

Each entity and relation carries one assertion class:

- `implementation` - an explicit project-implementation observation;
- `paper-claim` - a paper-derived claim, method, or innovation;
- `inference` - a host-declared interpretation;
- `metadata` - a deterministic Manifest metadata candidate.

Inference entities and inference relations cannot claim `observed` certainty.
Relation endpoints must be declared entities in the same artifact; self-loops
and duplicate `(source, target, relation, assertion_class)` identities are
rejected. A paper claim is not silently promoted to a project fact, and an
inference is never silently promoted to an observation.

## Evidence registry boundary

Evidence IDs are optional, duplicate-free, bounded, and structurally validated.
Any Evidence-bearing build must receive an explicit project-registry snapshot.
Generation and current loading resolve every referenced ID against the actual
current project's Evidence registry, so a syntactically valid fake ID or an ID
from another project fails closed.

This is Evidence-registry referential integrity only. E-05 does not invoke the
F-02B Claim-Evidence validator, reopen Source bytes, or establish current Source
version, Locator, excerpt, stance, Claim verification, or scientific truth.
Consumers needing those guarantees must compose the corresponding currentness
capability separately.

## Honest fallback and gaps

When no semantic observations are supplied, E-05 emits a deterministic
`manifest-metadata-fallback`. Current Manifest classification may produce
bounded paper, dataset, configuration, implementation, experiment, and result
*candidates*. Every such row has `assertion_class=metadata` and uncertain
certainty; source-code or notebook classification therefore does not masquerade
as observed implementation provenance. The fallback emits no relations and
makes missing paper, method, configuration, relationship, and Evidence
information explicit through `gaps` and `coverage`.

## Integrity, identity, and current loading

The artifact binds to the exact current `project-inventory-v4` Manifest version,
scan generation, ordinary-file count/bytes, and SHA-256. JSON is strict UTF-8,
canonical, duplicate-key-free, closed-field, and rejects unsupported versions,
unknown fields, invalid paths/endpoints, noncanonical order, inconsistent
coverage, and tampered stable identities.

Explicit entity IDs are caller-declared bounded stable keys. A relation ID binds
only project, source, target, relation label, and assertion class; mutable prose,
Evidence lists, certainty, and uncertainty do not redefine the relation. The
artifact ID still covers the complete canonical payload. Metadata-fallback IDs
are deterministic from current Manifest metadata.

Generation publishes atomically under the per-project
`indexes/machine-state.lock` and rechecks exact Manifest bytes immediately
before replacement. Current loading snapshots and validates the artifact under
that lock, validates referenced Evidence through the Source/Evidence registry
lock order outside `machine-state.lock`, then reacquires `machine-state.lock`
and rejects Manifest or artifact changes before returning. Consumers must use
`load_current_research_linkage(...)` rather than treating structural loading as
current authorization.

## Explicit non-goals

E-05 does not open registered Source bytes, read research binaries, call an LLM,
write curated Markdown, register or mutate Evidence, infer stance/conflicts or
scientific conclusions, or add CLI, MCP, Hook, or Web behavior. Later rendering
and orchestration units may consume this machine artifact only after current
loading and any additional capability-specific gates.
