# A-02 Baseline Tests

This directory freezes the deterministic behavior that exists before the research-assistant refactor begins.

## Fixture

`fixtures/minimal_research_project/` is a small, synthetic research project containing:

- project documentation;
- Python source code;
- a Jupyter notebook;
- YAML configuration;
- CSV metrics;
- LaTeX and BibTeX paper sources;
- a training log.

The fixture contains no private or real research data. Tests copy it into a temporary directory before execution, so committed fixture files remain immutable.

## Covered baseline

- `tools/raw_md.py` mirrors supported project files into raw Markdown pages, records source paths and hashes, and writes a manifest and report without changing source files.
- `tools/health.py` deterministically detects empty/stub pages, index drift, missing ingest-log coverage, and renders its report.
- Notebook conversion deliberately disables MarkItDown in the test so the built-in fallback path is stable across environments.

## Non-goals

A-02 does not add a project scanner, a new manifest schema, semantic research knowledge extraction, retrieval, task planning, or LLM calls. Those changes belong to later tasks and must keep these baseline tests passing unless a deliberate migration updates the expectation.

## Run

From the repository root:

```powershell
python -B -m unittest discover -s tests -v
```

## A-03 storage-layout coverage

`test_project_layout.py` verifies that machine state and human-readable knowledge use disjoint project roots, project IDs cannot escape those roots, directory initialization is idempotent, schema v1 documents are validated, legacy unversioned JSON remains readable without mutation, future schemas fail closed, and existing `<project-name>-wiki` raw-md layouts remain available as an explicit fallback.

## B-01 project-registration coverage

`test_project_registration.py` verifies stable path-based IDs, Schema v1 `project.yaml`, optional onboarding and external `knowledge_root`, read-only local Git metadata, credential redaction, repeated-registration idempotency, source-tree zero writes, path/ID conflicts, fail-closed corrupt records, and direct CLI JSON output. B-01 deliberately creates no file Manifest, extracted content, knowledge page, or LLM output.

## B-02 scan-policy coverage

`test_scan_policy.py` verifies default and protected exclusions, `.llmwikiignore` ordering and re-inclusion, explicit override precedence, glob behavior, configuration conflicts, invalid UTF-8 fail-closed behavior, file-size and sensitive-path access, three external-send modes, symlink root/cycle/duplicate protection, versioned policy serialization, and source-tree zero writes. B-02 deliberately performs no directory inventory, Manifest write, source hash, extraction, or LLM call.

## B-03 project-inventory coverage

`test_project_inventory.py` retains the B-03 accountability checks: inventory starts from a validated B-01 registration, consumes B-02 include/exclude and re-inclusion decisions, records every in-scope regular file regardless of extension, records excluded files and pruned-directory boundaries with reconcilable summaries, applies symbolic-link safety, exposes parseable CLI JSON, leaves the source tree unchanged, and preserves the previous Manifest without temporary-file leaks when traversal fails.

## B-04 file-fingerprint coverage

The same test module retains coverage for B-04 scan generations, SHA-256/size/mtime fields, conservative reuse, same-size replacement, mtime-only touches, add/rename/delete refreshes, B-03 v1 compatibility, fail-closed corrupt/future Manifests, and atomic failure preservation. Current output is upgraded by B-06 to `project-inventory-v4`.

## B-05 deterministic-classification coverage

`test_file_classification.py` and `test_project_inventory.py` verify magic/signature priority, spoofed extensions, extensionless shebang and Notebook detection, common research formats and path roles, explicit unknown files, reason/schema round-trips, B-02-aware path-only classification for sensitive and oversized files without prefix reads, v2 fingerprint reuse during upgrade, unchanged classification reuse, summary reconciliation, source-tree zero writes, and continued absence of extraction, Evidence IDs, and reconciliation events.

## B-06 Manifest file-state coverage

`test_file_state.py` and `test_project_inventory.py` verify the stable processing-status/read-depth enums, valid and invalid combinations, exact Schema v1 serialization, required reason codes and explanations, truthful inventory states for sampled, sensitive, oversized, and unsupported files, v1/v2/v3 upgrade compatibility, unchanged-state reuse, reset on content or policy-basis changes, `missing` non-reuse, state-summary reconciliation, corrupt/future state fail-closed behavior, atomic Manifest preservation, CLI exposure, and source-tree zero writes. B-06 itself does not perform B-07 prioritization or B-08 coverage reporting; those are separate artifacts. It also does not add content extraction, Blocks, Locators, source IDs, Evidence, MCP, Hook, or Web behavior.

## B-07 adaptive-reading-priority coverage

`test_reading_priority.py` verifies ordinary-file ranking; deterministic research-role, path, and filename scoring; bounded reference parsing and unique project-relative resolution; rejection of URI, absolute, traversal, ambiguous, and self references; sensitive, oversized, policy-denied, model/checkpoint, failed-state, ignored-depth, and large-dataset restrictions; selected and deferred promotion-queue semantics; bounded descriptor-based reads; empty-project handling; byte-identical regeneration; Manifest and scan-policy revalidation before reads and atomic replacement; post-read/pre-commit reference mutation rejection; preservation of valid existing artifacts on generation failure; shared machine-state locking; Core/CLI parity; and the absence of extraction, Evidence, Source, run-state, source-project, or curated-wiki writes. Loader tests distinguish strict structural parsing from `load_current_reading_priority(...)` execution authorization grounded in exact current Manifest and policy truth, and reject legacy-v0, malformed, symlinked, semantically tampered, policy-tampered, and unsupported future-version artifacts. Exact tests exercise the fixed 128-file, 256 KiB/file, 4 MiB aggregate-reference, 128-selected-file, 32 MiB selected-byte, and 64 MiB dataset boundaries. `test_project_layout.py` covers strict Schema v1/legacy/future compatibility, the canonical `reading-priority.json` path, and machine-state ancestor symbolic-link/reparse/redirection rejection before lock, temporary-file, or artifact writes.

Focused validation recorded on 2026-07-17:

```powershell
python -B -m pytest -q -p no:cacheprovider tests/test_reading_priority.py tests/test_project_layout.py
```

Result: **45 passed, 3 skipped**. The skipped cases require host symbolic-link creation privilege; deterministic mocked unresolved-link and ancestor-redirection paths remain covered.

## J-03 product-research-cockpit coverage

`test_research_cockpit.py` verifies honest registration-only DRAFT gaps; strict
Knowledge Schema v2/path handling for all fifteen canonical entries; bounded
Manifest/coverage/file-state, Claim/Evidence/Source/Locator, Goal/task/plan, and
run usage/cost/error projections; absolute-path redaction; source-tree
immutability; loopback bind and Host enforcement; read-only HTTP methods;
restrictive security headers; traversal/query rejection; and redirected-path
fail-closed behavior where the host permits symbolic-link creation. Focused
validation on 2026-07-19 produced **15 passed, 3 skipped**; the skipped cases
require Windows symbolic-link privilege.

## J-04 controlled-research-cockpit-editing coverage

`test_research_cockpit_editing.py` verifies the exact four target/path/region
mappings and absence of arbitrary edit paths; strict Knowledge Schema v2,
canonical identity, mixed ownership, draft status, and renderer-marker gates;
full-page SHA-256 revisions; stale and racing CAS behavior; F-05A/F-05B
persistence; body-free audit and commit-unknown errors; frontmatter, generated
skeleton, markers, and unrelated-page preservation; mixed user-byte survival
after E-07 regeneration; source hash/mtime/mode immutability; bounded content and
local-root rejection; random token, Origin, Fetch-Site, Host/port, request
framing, strict JSON, and fixed-route enforcement; plus the static UI contract,
conflict-draft retention, and successful refetch order. The default J-03 mode
remains read-only, Query remains exactly unavailable until G-04, and C-07 remains
`deferred/not_started`. Focused J-03/J-04 validation on 2026-07-19 produced
**30 passed, 3 skipped**; dependent validation produced **161 passed, 2
skipped**; full regression produced **824 passed, 31 skipped**.

## J-01 R3-minus-C-07 end-to-end coverage

`test_j01_r3_end_to_end.py` runs the fixed research fixture twice in independent
clean temporary workspaces through project understanding, Source registry and
Evidence registration, exact Locator/source reopening, the MCP Query unavailable
contract, H-07 full-scan reconciliation, I-04 planning, and persisted E-07/F-05
rendering. It verifies all fifteen canonical Knowledge paths, the unified index,
the dated daily plan, strict Knowledge Schema v2 parsing, canonical body-free
prepared/committed audit pairs, current Source/Evidence/hash/excerpt bindings,
and path-independent deterministic outcomes.

The test also covers the narrow reconcile-to-plan integration repair: a
well-formed I-03 state whose registration or Manifest binding is stale may be
rebuilt, while malformed/future state and Goal/task-revision drift still fail
closed. The resulting Goal, plan, and tasks remain DRAFT and every task remains
non-executable. A model/checkpoint binary canary remains classification-only and
limited; no semantic extraction or canary leakage is permitted. LLM, network,
URL, and browser calls are guarded, and source hashes, sizes, mtimes, modes, and
directory metadata must remain unchanged. Query stays exactly
`capability-unavailable` with `available_after=G-04`; C-07 stays
`deferred/not_started`; no R4 behavior is covered or implemented.

Validation recorded on 2026-07-19: focused J-01/planning/state tests produced
**19 passed**; dependent suites produced **339 passed, 12 skipped**; final full
regression produced **826 passed, 31 skipped**. Ruff, `py_compile`, and
`git diff --check` passed.
