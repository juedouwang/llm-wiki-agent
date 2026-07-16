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

The same test module retains coverage for B-04 scan generations, SHA-256/size/mtime fields, conservative reuse, same-size replacement, mtime-only touches, add/rename/delete refreshes, B-03 v1 compatibility, fail-closed corrupt/future Manifests, and atomic failure preservation. Current output is upgraded by B-05 to `project-inventory-v3`.

## B-05 deterministic-classification coverage

`test_file_classification.py` and `test_project_inventory.py` verify magic/signature priority, spoofed extensions, extensionless shebang and Notebook detection, common research formats and path roles, explicit unknown files, reason/schema round-trips, B-02-aware path-only classification for sensitive and oversized files without prefix reads, v2 fingerprint reuse during upgrade, unchanged classification reuse, summary reconciliation, source-tree zero writes, and continued absence of B-06 processing/read-depth fields, extraction, Evidence IDs, and reconciliation events.
