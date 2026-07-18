# Research Entity and Relation Contract (F-03A)

F-03A adds the deterministic project-scoped entity/relation layer required to
express scientific workflow structure without moving semantic judgment into
Research Core. The implementation is
[`tools/research_relations.py`](../tools/research_relations.py).

This unit is intentionally in-memory and read-only. It defines canonical
`relations.jsonl` bytes and a deterministic project-index projection for later
controlled persistence, Markdown rendering, and product UI work. It does not
choose or write a filesystem location, modify curated knowledge, read research
sources, or expose a CLI, MCP, Hook, Skill, or Web operation.

## Host Agent and Core boundary

The host Agent explicitly supplies:

- what scientific object exists;
- its project-local `entity_type`, `identity_key`, display `title`, and optional
  canonical knowledge location;
- the direction and normalized lowercase kebab-case label of each relation; and
- any Evidence IDs that structurally ground a relation.

Core only validates schema, stable identity, project/path binding, endpoint
existence, duplicate constraints, optional Evidence-ID existence, canonical
serialization, and reversible incoming/outgoing backlinks. It never derives an
entity from prose, chooses a relation label, infers a relation from same names,
merges identities, infers Evidence stance/currentness, or decides what a
relation means for a research conclusion.

## Entity identities

The supported project entity types are:

```text
paper
method
dataset
experiment
metric
result
claim
decision
question
source
```

Every identity is explicit and project-scoped. `research_entity_id_for(...)`
hashes the exact identity version, `project_id`, `entity_type`, and caller
supplied `identity_key`, producing `ent-` plus 64 lowercase hexadecimal digits.
`title` and Markdown location are deliberately excluded: a rename or relocation
does not silently create a new scientific identity. Conversely, two objects with
the same title remain distinct when the host supplies different identity keys.
The Core does not perform same-name merging.

An entity may remain unmaterialized with `knowledge_path: null` and
`anchor: null`. A materialized entity must use the F-01 canonical path contract:

| Entity type | Canonical knowledge binding |
|---|---|
| `paper` | `papers/<slug>.md` detail page |
| `method` | `methods/<slug>.md` detail page |
| `dataset` | `datasets/<slug>.md` detail page |
| `experiment` | `experiments/<slug>.md` detail page |
| `result` | `results/<slug>.md` detail page |
| `claim` | `claims/<slug>.md` detail page |
| `decision` | `decisions/<slug>.md` detail page |
| `source` | `sources/<slug>.md` detail page |
| `metric` | explicit anchor inside a `results/<slug>.md` detail page |
| `question` | explicit anchor inside `open-questions.md` |

This mapping does not invent a `metrics/` or `questions/` directory. Collection
indexes cannot stand in for detail entities. Direct-page entities bind to the
page and therefore cannot add an anchor; embedded metric/question entities
require a normalized explicit anchor. F-03A validates the location contract but
does not write or repair body anchors; the later controlled writer/renderer owns
materialization.

## Directed relation identities

A relation contains:

```text
project_id
relation_id
relation_type
source_entity_id
target_entity_id
evidence_ids
```

`relation_type` is an explicit lowercase kebab-case label chosen by the host.
F-03A deliberately has no hardcoded semantic endpoint matrix: labels such as
`evaluates-on`, `produces`, `measured-by`, or a domain-specific label retain the
host's meaning. Relations are directed, cannot be self-loops, and both endpoints
must exist in the same project registry.

`research_relation_id_for(...)` hashes the exact identity version, project,
relation type, source entity, and target entity, producing `rel-` plus 64
lowercase hexadecimal digits. Evidence IDs do not change relation identity. A
registry rejects duplicate IDs, duplicate explicit entity identities, duplicate
Markdown locations, and duplicate type/source/target edges.

Optional `evidence_ids` are canonical, sorted, duplicate-free `evd-` identifiers.
When `known_evidence_ids` is supplied to project-index construction, every
relation Evidence ID must exist in that caller-supplied set. This is structural
referential integrity only: F-03A does not call F-02B, determine Evidence
currentness, infer stance, or claim that the relation is verified.

## Canonical relation registry

`ResearchRelationRegistry.serialized_bytes()` defines strict UTF-8 LF-terminated
JSONL:

1. one summary row;
2. entity rows sorted by `entity_id`;
3. relation rows sorted by `relation_id`.

Every row carries `schema_version`, a fixed `kind`, a fixed record version, the
project identity, and the fixed identity version. The summary records exact
entity/relation counts. Parsing rejects missing or unknown fields, duplicate JSON
keys, non-finite constants, invalid UTF-8, blank rows, wrong counts/order,
noncanonical bytes, identity mismatches, cross-project records, unsupported
legacy version 0, and future versions. Future versions fail closed.

The payload is a machine-state contract, not curated Markdown. F-03A does not
persist it; a later approved storage/writer unit must keep it beneath the
project's `.llmwiki/` machine state rather than placing it in
`wiki/projects/<project_id>/`.

## Current-page binding and project index

`validate_research_entity_bindings(...)` consumes caller-supplied Markdown bytes.
Every supplied page must be a canonical project-relative path, strict Knowledge
Schema v2, and belong to the registry project. Materialized entities require a
matching page type/role; direct-page titles must still match the entity title.
Schema v1 is read-only compatibility elsewhere but is not accepted as a current
F-03 entity binding, and Schema v3+ fails closed.

`build_research_project_index(...)` returns deterministic renderer input with:

- entities grouped by type;
- each entity's canonical page/anchor location;
- sorted incoming relation IDs;
- sorted outgoing relation IDs; and
- the complete directed relation rows needed to resolve each backlink.

The projection is not `wiki/projects/<project_id>/index.md` content and performs
no Markdown generation. F-05 or a later renderer may consume it without
re-deriving identities or relationships from prose.

## Safety and explicit non-goals

F-03A:

- performs no filesystem read or write;
- accepts knowledge as caller-supplied strict UTF-8 bytes;
- does not read Source or research-binary content;
- performs no external send and invokes no LLM;
- does not infer entities, relation labels, stance, conflicts, or conclusions;
- does not register or validate current Evidence;
- does not generate or modify Markdown or body anchors;
- does not persist `relations.jsonl` or the project-index projection; and
- adds no CLI, MCP, Hook, Skill, Plugin, or Web operation.

## Validation

Focused validation covers deterministic identity, all entity types and canonical
locations, explicit same-title separation, relation constraints, canonical JSONL,
strict/future schema rejection, current Knowledge Schema v2 binding, optional
Evidence referential integrity, deterministic entity groups, incoming/outgoing
backlinks, input immutability, and zero filesystem access:

```powershell
python -B -m pytest -q -p no:cacheprovider `
  tests/test_research_relations.py `
  tests/test_knowledge_artifacts.py
python -B -m ruff check `
  tools/research_relations.py `
  tests/test_research_relations.py
git diff --check
```
