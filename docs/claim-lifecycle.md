# Claim Lifecycle and Conflict Coexistence Contract (F-04A)

F-04A adds a bounded, deterministic Core contract for validating host-declared
Claim lifecycle changes and explicit coexistence of conflicting Claim variants.
The implementation is
[`tools/claim_lifecycle.py`](../tools/claim_lifecycle.py).

This unit is deliberately in-memory and read-only. It consumes current Knowledge
Schema v2 Claim frontmatter, the F-03A project entity registry, optional F-02B
Claim currentness results, and caller-supplied current Knowledge page bytes. It
returns audit-shaped validation records only. It does not persist a status,
write Markdown, mutate an entity/relation registry, read a Source, open a
research binary, infer a scientific conflict, propagate stale state, or add a
CLI, MCP, Hook, Skill, or Web operation.

F-04 remains **partial** after this unit. F-04A defines the lifecycle/coexistence
validation primitive; later controlled writing and product integration must
materialize accepted host decisions without overwriting prior conclusions.

## Host Agent and Core boundary

The host Agent remains the semantic decision-maker. It explicitly supplies:

- the F-03A Claim entity identity;
- the current and proposed Claim frontmatter;
- the desired target status among `draft`, `verified`, `stale`, `conflicting`,
  and `rejected`;
- an F-02B currentness result when the target is `verified`; and
- an explicit conflict identity plus the Claim variants and Result entities that
  coexist when a conflict is declared.

Core validates only deterministic invariants: current Schema v2, project/path/
type/title binding, stable entity IDs, immutable Claim identity fields,
monotonic `updated_at`, preserved verification history, verified-currentness
closure, duplicate-free explicit conflict membership, at least two distinct
Results overall, and canonical fingerprints.
Core does not decide whether a status transition is scientifically justified,
infer conflict from opposing Evidence or a relation label, select a winner,
replace an older Claim, or derive Result membership from prose.

## Claim transition validation

The entry point is:

```python
validate_claim_transition(
    registry,
    *,
    claim_entity_id,
    before,
    after,
    currentness=None,
    conflict_coexistence=None,
) -> ClaimTransitionValidation
```

`registry` must be a current in-memory F-03A `ResearchRelationRegistry`.
`claim_entity_id` must resolve to a materialized project-local `claim` entity.
`before` may be `None` for creation; otherwise both `before` and `after` must be
current Knowledge Schema v2 Claim frontmatter bound to the entity's canonical
`claims/<slug>.md` path, project, and type. F-03A identity is
`project_id + entity_type + identity_key`, not display title: an older `before`
revision may retain its historical title, while the proposed `after` title must
match the current F-03A registry entity so a coordinated rename remains possible
without changing the entity ID.

The closed five-value status set is structurally composable in every direction.
Core intentionally does not encode a scientific transition matrix. For example,
a host may explicitly propose `rejected -> draft` after new work or
`conflicting -> verified` after resolving a disagreement. Core validates the
record but does not make that decision.

For an existing Claim:

- the transition must not be an identical no-op;
- `schema_version`, `kind`, `project_id`, `artifact_type`, and `generated_at` are
  immutable; display `title` may change only when the proposed value matches the
  current F-03A entity title;
- `updated_at` must advance strictly;
- a non-verified target must preserve the previous `last_verified_at` exactly, so
  moving away from `verified` cannot erase the most recent real verification; and
- a target `verified` Claim must set `last_verified_at == updated_at`.

Creation has no prior record to preserve, so a non-verified Claim may be
materialized with a structurally valid historical `last_verified_at`. For an
existing Claim, adding, removing, or advancing that history requires a verified
target and its current F-02B proof.

The validator consumes the complete frontmatter projection and reports every
changed field. That report is not authorization to persist unrelated ownership,
Source, Evidence, or body changes: those fields remain governed by their own
contracts and the F-05 controlled writer.

The result classifies the change as `created`, `status-changed`, `reverified`,
or `updated`, records canonical before/after frontmatter SHA-256 values, records
changed fields, and emits a stable `clt-<64-hex>` transition identity. The
identity also closes over the complete F-02B proof hash and, for a `conflicting`
target, the complete coexistence-proof hash, so changing either proof changes the
transition identity. It also
states `read_only: true`, `status_persisted: false`, and
`semantic_decision_made: false`.

## Binding verified state to F-02B

A target `verified` status cannot be accepted from frontmatter alone. The caller
must supply a `ClaimEvidenceValidationResult` produced by the F-02B contract.
F-04A binds that result to the exact target:

- the same project and canonical Claim path;
- the exact canonical SHA-256 of the complete target Claim frontmatter revision;
- the same target status and key-Claim role;
- the exact declared Source IDs;
- the exact ordered `(evidence_id, stance)` references;
- internally consistent Source/Evidence currentness and supporting-Evidence
  counts;
- a current verified-state outcome; and
- a validity result consistent with those component outcomes.

The transition record fingerprints the complete F-02B result and reports the
Evidence-binding and verified-state dimensions separately. A missing, stale,
mismatched, or internally inconsistent result fails closed for a `verified`
target. Supplying a result for a non-verified target never upgrades that target;
it only records the independent currentness dimension.

F-04A does not rerun F-02B, open Source bytes, or treat its aggregate result as a
new Evidence registry. The host/Core orchestration layer is responsible for
obtaining the current F-02B result immediately before proposing a verified
transition.

## Explicit conflict coexistence

The second entry point is:

```python
validate_claim_conflict_coexistence(
    registry,
    *,
    identity_key,
    variants,
    knowledge_pages,
) -> ClaimConflictCoexistenceValidation
```

`identity_key` is an explicit project-local conflict key supplied by the host.
The stable conflict identity hashes `project_id + identity_key`; membership and
display titles do not silently redefine it.

Each `ClaimConflictVariant` contains one distinct F-03A Claim entity ID and one
or more explicitly assigned F-03A Result entity IDs. Validation requires:

- at least two distinct Claim variants;
- current Schema v2 bytes for every materialized Claim and Result page;
- exact project, canonical type/path, and direct-page title binding;
- every Claim page already explicitly marked `conflicting` by the host;
- at least one duplicate-free Result reference per Claim variant; and
- at least two distinct Results overall.

A Result may be referenced by multiple variants. Competing scientific
interpretations often concern the same experimental Results, so Core must not
invent exclusive ownership. Result pages themselves do not need
`status: conflicting`; that status belongs to the explicit Claim conclusions.

The returned record retains every Claim and Result binding with frontmatter
fingerprints. It contains no winner or replacement field and performs no
overwrite. Same-title Claims remain separate because identity comes from F-03A
entity IDs, not titles.

Opposing Evidence, a `conflicts-with` relation label, or any other structural
signal is insufficient by itself. Core validates only the explicit declaration
and never promotes a Claim to `conflicting` automatically.

## Fail-closed and side-effect boundaries

- Knowledge Schema v1 remains strict read-only compatibility and is rejected by
  this current-state validator; it is never migrated or assigned directional
  meaning.
- Schema v3 and future unknown versions fail closed.
- Unknown, wrong-type, cross-project, unmaterialized, missing, or stale-title
  entity/page bindings fail closed.
- Inputs are not mutated; output ordering and hashes are deterministic.
- Validation performs no filesystem writes, no Source/research-binary access, no
  network or external model send, and no persistence.
- A target `conflicting` transition is rejected unless it binds a successful
  coexistence validation that contains the exact target Claim path and frontmatter
  fingerprint. A successful transition record is therefore complete and never
  carries unresolved follow-up validation requirements.

## Validation

The focused regression is:

```powershell
python -B -m pytest -q `
  tests/test_claim_lifecycle.py `
  tests/test_knowledge_artifacts.py `
  tests/test_claim_evidence.py `
  tests/test_research_relations.py
```

It covers all 25 host-declared status pairs, strict Schema v2 and future-version
behavior, exact F-03A identity binding, F-02B verified proof matching, stale and
inconsistent proof rejection, preservation of verification history, explicit
two-Claim/two-Result coexistence, same-title distinct identities, shared Result
interpretations, Result-status independence, absence of winner/replacement
semantics, deterministic output, input immutability, and the in-memory/no-Source-
access boundary.
