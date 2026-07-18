# Hierarchical Project Understanding (E-03)

- Status: implemented for the bounded E-03 Core slice
- Machine artifact: `.llmwiki/projects/<project_id>/indexes/hierarchical-understanding.json`
- Schema: machine Schema v1
- Kind/version: `llmwiki-hierarchical-understanding` / `hierarchical-understanding-v1`
- Public module: `tools.hierarchical_understanding`
- Core facade: `ResearchCoreService.hierarchical_understanding(...)`
- Validation: `tests/test_hierarchical_understanding.py`

## Purpose

E-03 provides a deterministic and bounded `chunk -> file -> module -> project`
projection. The host Agent may supply summaries for chunks that it has already
read under the applicable content policy. Every semantic chunk must include one
or more Evidence IDs. Core validates those explicit observations, binds them to
the exact current B-06 Manifest, and carries their Evidence and input-budget
closure through every higher level.

Core does not open source-project files for E-03, choose chunks, infer module
identity, create Evidence, call an LLM, or write curated Markdown. If the caller
supplies no observations, E-03 produces an explicit Manifest-metadata-only
fallback rather than pretending semantic understanding succeeded.

## Host observation contract

A `ChunkObservation` declares exactly:

- canonical project-relative POSIX `path` present in the current Manifest;
- stable caller-selected `chunk_id` and `module_id`;
- a bounded NFC summary without surrounding whitespace;
- non-negative UTF-8 byte and token estimates for the summarized input;
- sorted, duplicate-free Evidence IDs matching `evd-<64 lowercase hex>`.

The host is responsible for the scientific meaning and Evidence grounding of the
observation. Core rejects duplicate `(path, chunk_id)` identities and unknown
paths. It never invents missing Evidence or treats metadata as semantic input.

## Artifact and closure

The canonical UTF-8 JSON artifact records:

- exact Manifest version, scan generation, ordinary-file/byte totals, and
  SHA-256 of the Manifest bytes;
- derivation mode and explicit `source_content_read=false` / `llm_used=false`;
- fixed safety ceilings plus the effective limits used by the build;
- coverage and overlapping omission counters;
- semantic chunk nodes and represented file/module/project aggregates;
- stable IDs binding every chunk input, file path/hash, module identity, and
  project/Manifest identity;
- sorted lower-level IDs, bounded Evidence closure, and summed input budgets at
  every aggregate level.

File, module, and project summaries are deterministic bounded joins of their
children. A file with no represented chunk is explicitly `metadata_only` and is
assigned to a deterministic path-derived fallback module. A file may participate
in multiple modules when its represented chunks declare different module IDs;
each chunk belongs to exactly one module, so project input totals do not
multiply shared-file input.

## Budgets and omissions

Default safety ceilings are 2,048 chunks, 1,024 files, 256 modules, 64 chunks per
file, 128 Evidence IDs per node, 4,096 UTF-8 bytes per summary, and 512 KiB of
selected chunk-summary bytes. Tests may request lower limits, but callers cannot
raise a ceiling.

Semantic files are prioritized before metadata-only files. Observed module IDs
are prioritized before fallback modules. Every collection and summary projection
is deterministic. `omissions.observations` and `omissions.chunks` both count
supplied observations that did not become represented chunks; file omissions
reconcile exactly to Manifest coverage. `summary_bytes` counts host-summary bytes
removed or excluded by summary budgets, and `evidence_ids` counts Evidence IDs
removed by per-node projections. These counters are not claims about scientific
coverage.

## Loading, currentness, and persistence

`load_hierarchical_understanding(...)` is strict structural parsing. It rejects
legacy/future versions, unknown fields, duplicate JSON keys, noncanonical JSON,
invalid IDs, inconsistent summaries, broken lower-level closure, exceeded
budgets, and symlinked artifact paths.

`load_current_hierarchical_understanding(...)` additionally acquires the shared
`indexes/machine-state.lock`, requires the exact current Manifest binding, and
checks every represented file path, content hash, byte size, and classification
against that Manifest. Metadata-only fallback artifacts are rebuilt completely
and compared with deterministic current truth. For explicit observations, Core
can validate the stored observations and current Manifest projection, but it
does not reopen Evidence or independently judge the host-authored chunk summary.

Generation also runs under `machine-state.lock`, writes through a same-directory
temporary file, fsyncs it, rechecks exact Manifest bytes immediately before
`os.replace`, and verifies the committed artifact. The operation writes only
project machine state. Registered source bytes and `wiki/projects/` are unchanged.

## API

```python
from tools.hierarchical_understanding import ChunkObservation
from tools.research_core import ResearchCoreService

core = ResearchCoreService(workspace_root)
result = core.hierarchical_understanding(
    project_id,
    observations=[
        ChunkObservation(
            path="src/model.py",
            chunk_id="src/model.py#chunk-0001",
            module_id="model",
            summary="Defines the model construction boundary.",
            input_utf8_bytes=1536,
            input_token_estimate=384,
            evidence_ids=("evd-" + "a" * 64,),
        )
    ],
)
```

E-03 adds no CLI, MCP, Hook, Web, or curated-Markdown operation. It does not
implement E-04 execution-flow analysis, E-05 research-link synthesis, E-06
experiment chains, E-07 rendering, or the complete E-08 one-action workflow.
C-07 remains `deferred/not_started`.
