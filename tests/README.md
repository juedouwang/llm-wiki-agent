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
