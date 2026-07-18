# Claim–Evidence Currentness Contract (F-02B)

F-02B adds a deterministic, Core-internal, read-only validator that closes a
strict Knowledge Schema v2 Claim over the project's current Source and Evidence
state. It answers whether the Claim's explicit bindings are present and current;
it does not decide what the Claim means or whether its stance is scientifically
correct.

The implementation is [`tools/claim_evidence.py`](../tools/claim_evidence.py).
It composes the existing Knowledge Schema, Source registry, Evidence registry,
source-access, and source-relocation primitives. It adds no CLI, MCP,
ResearchCoreService, Hook, Skill, Plugin, or Web operation.

## API

```python
validate_claim_evidence(
    workspace_root,
    project_id,
    *,
    path,
    payload,
) -> ClaimEvidenceValidationResult
```

`path` is a canonical project-relative knowledge path. `payload` is supplied
frontmatter, not a Markdown filename to open. The function first calls the
strict current-schema validator from `tools/knowledge_artifacts.py`; Schema v1
is read-only compatibility and is rejected by this currentness API, while
Schema v3 and later fail closed.

The canonical path must resolve to `artifact_type: claim`. F-02B does not read a
knowledge page body and does not write any Markdown file.

## Deterministic validation sequence

For one structurally valid Schema v2 Claim, F-02B:

1. requires the Claim `project_id` to match the requested registered project;
2. requires every declared `source_id` to exist in that project's Source
   registry;
3. requires every explicit `evidence_ref.evidence_id` to exist in that
   project's Evidence registry;
4. requires each Evidence record to belong to the requested project and to a
   Source declared by the Claim;
5. validates the Evidence's recorded `source_version + content_hash` binding and
   requires that exact version to remain current;
6. reopens the exact Locator from current Source bytes with automatic relocation
   recovery disabled;
7. requires the actual current bytes, Locator, and reopened excerpt hash to match
   the Evidence record; and
8. computes the verified-state closure described below.

The version check precedes byte recurrence: an A -> B -> A content sequence does
not make Evidence bound to the old source version current again.

## Verified-state closure

`claims/<slug>.md` is a key Claim detail. A verified key Claim is current only
when all of the following are true:

- all declared Claim Sources are registered;
- every declared Evidence reference is current;
- at least one declared `supporting` reference is current; and
- `last_verified_at == updated_at`.

The equality rule means that any controlled page modification after verification
invalidates the current verified state until the page is verified again.

`claims/index.md` is a collection index, not a key Claim. Its declared Evidence,
if any, is still validated, and a verified index still requires current bindings
and `last_verified_at == updated_at`; F-02B does not invent a supporting-Evidence
requirement for the index. The API requires a canonical path, so the pathless
conservative structural behavior remains an F-02A parser rule rather than an
F-02B call mode.

For `draft`, `stale`, `conflicting`, or `rejected` pages,
`verified_state_current` is `null`. Their declared bindings are still checked,
and top-level `valid` remains false when a declared Source or Evidence is missing
or noncurrent. F-02B reports currentness; it never changes the page's status.

## Auditable result

`ClaimEvidenceValidationResult.as_dict()` emits Schema v1 structured data with:

- `kind: llmwiki-claim-evidence-validation`;
- `validation_version: claim-evidence-validation-v1`;
- requested project, canonical Claim path, status, and `key_claim` role;
- duplicate-free stable reason codes;
- one Source-binding result per declared `source_id`;
- one directional currentness result per declared Evidence reference;
- `all_evidence_current`;
- `current_supporting_evidence_count`;
- `verified_state_current`; and
- `read_only: true`.

Per-reference results include the Evidence ID, caller-declared stance, Source ID,
currentness, reason, observed excerpt hash when successful, and an optional
read-only relocation inspection. If that inspection itself fails closed, the
original Source-access reason remains primary and
`relocation_inspection_reason_code` records the independent inspection failure.
A relocation result and an inspection-failure code are mutually exclusive. The
result never includes the reopened excerpt or raw Source bytes.

Stable failures distinguish missing registry identities, project/source binding
errors, historical/current source-version mismatches, physical content mismatch,
invalid Locator, excerpt mismatch, unrecorded relocation, ambiguous relocation,
and verified-closure failures. Structural malformed/future Schema or registry
state raises through the existing fail-closed compatibility layers.

## Read-only relocation inspection

F-02B calls `open_source(..., recover_relocation=False)`. A failed current path
or content check therefore cannot invoke D-05's mutating recovery path.

It may then call `inspect_source_relocation(...)`, which evaluates the same
ordered deterministic candidate groups as D-05:

1. recorded path aliases;
2. current-Manifest exact-content-hash candidates; and
3. local Git rename history.

The inspection returns `current`, `relocatable`, `unresolved`, or `ambiguous` in
`llmwiki-source-relocation-inspection` / `source-relocation-inspection-v1` data.
Even one unique exact-hash candidate is report-only: validation never updates
`sources.jsonl` or path history. Callers must use an explicitly authorized D-05
recovery operation if they want to persist a relocation.

## Read-only and semantic boundaries

F-02B does not:

- open, generate, serialize, or modify a Markdown page or body;
- register, rewrite, delete, or migrate Evidence;
- repair or otherwise modify a Source registry;
- persist `stale`, `verified`, or any other Claim state;
- infer Evidence stance, Claim conflict semantics, or a research conclusion;
- propagate stale state to dependent pages or plans;
- call an LLM, use a semantic heuristic, or send content externally; or
- add a public CLI/MCP/Web interface.

The host Agent remains responsible for meaning, stance selection, conflict
analysis, synthesis, and deciding what should be verified. Core only establishes
whether the explicit identities and bytes are safe, current, and traceable.
Controlled mixed/user Markdown writes and persistent stale propagation remain
separate later work, including F-05.
