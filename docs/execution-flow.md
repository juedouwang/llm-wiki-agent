# E-04 Execution Flow Contract

E-04 adds the machine-only artifact
`.llmwiki/projects/<project_id>/indexes/execution-flow.json`.
It is Schema v1 with `kind=llmwiki-execution-flow` and
`flow_version=execution-flow-v1`.

## Inputs and boundary

The host may pass explicit `FlowNodeObservation` and
`FlowEdgeObservation` values to `ResearchCoreService.execution_flow(...)`.
Nodes identify entrypoints, modules, processes, files, symbols, and bounded
Evidence IDs. Edges are directed `calls`, `data-flow`, `consumes`, `produces`,
or `depends-on` relationships and carry `observed`, `inferred`, or `uncertain`
certainty. The Core validates the host declaration; it does not infer a call
chain from prose and does not treat a host inference as a fact.

With no observations, E-04 creates a deterministic Manifest-metadata fallback
from code/run-script candidates. Candidate entrypoints are explicitly marked
`inferred`; unresolved nodes and relationships carry an uncertainty reason.

## Integrity

The artifact binds to the exact current B-06 Manifest generation, ordinary-file
count/bytes, and SHA-256. JSON is strict UTF-8, canonical, duplicate-key-free,
closed-field, and rejects future/legacy/unknown or tampered stable IDs. Node
paths must be present in the current Manifest. Publication uses the shared
`indexes/machine-state.lock`, atomic replacement, and an exact Manifest
recheck immediately before replace. `load_current_execution_flow(...)` verifies
all of these conditions and rebuilds the metadata fallback deterministically.

E-04 never opens registered source bytes, reads research-binary tensors, calls an
LLM, sends content externally, writes curated Markdown, or exposes a CLI/MCP/Web
operation. Later E-07/E-08 layers may render or orchestrate this machine result.
