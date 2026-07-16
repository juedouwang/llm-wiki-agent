# B-08 Coverage and Failure Report

## Purpose

B-08 turns the B-06 two-axis file state into a deterministic audit artifact. It
measures what the latest Manifest says; it does not scan, extract, classify, or
change any file state.

Run it after inventory:

```powershell
python tools/project.py coverage <project_id> --json
```

The command strictly validates the registered project's
`project-inventory-v4` Manifest before producing output. Legacy compatible
Manifest artifacts may still be read by inventory, but coverage generation
requires v4 because every ordinary file must already contain both
`classification` and versioned `file_state` records.

## Storage boundary

The report is written atomically to machine state:

```text
.llmwiki/projects/<project_id>/indexes/coverage-report.json
```

It is not written into the scanned source project and is not curated Markdown.
The command reads only project registration state and `manifest.jsonl`; it does
not traverse or open source-project files. A failed generation leaves any
previous report unchanged.

## Report Schema v1

Every report contains:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-coverage-report",
  "report_version": "coverage-report-v1",
  "project_id": "study",
  "manifest": {
    "manifest_version": "project-inventory-v4",
    "scan_generation": 1
  },
  "totals": {
    "file_count": 3,
    "byte_count": 1024,
    "failed_file_count": 0
  },
  "coverage": {},
  "failures": [],
  "reconciliation": {}
}
```

`coverage` groups every ordinary Manifest file by these independent axes:

- `research_role`;
- `processing_status`;
- `read_depth`;
- stable file-state `reason_code`.

Each bucket records both `file_count` and `byte_count`. Reason buckets also
retain a sorted list of the human-readable reason details observed for that
code. Every axis is checked against the ordinary-file count and summed byte
volume before the report can be written. The same totals are persisted under
`reconciliation` so consumers can audit the snapshot without inferring success
from missing data.

A failure is defined narrowly as a row whose
`processing_status == "failed"`. Other states remain visible in the status
coverage axis but are not mislabeled as failures. Failure entries retain the
normalized project-relative POSIX path, size, research role, read depth, reason
code, and reason. They are sorted by path.

## Determinism and failure semantics

- No timestamps or host-specific absolute source paths are stored.
- Maps use stable keys, failure rows use stable path ordering, and JSON uses
  UTF-8, sorted keys, two-space indentation, and one final newline.
- Re-running coverage against the same Manifest produces identical bytes.
- Corrupt rows, future Schema versions, unsupported Manifest artifacts,
  classification/state summary mismatches, unsafe file paths, or reconciliation
  failures fail closed.
- The Manifest is never rewritten and classification/state values are never
  promoted or repaired by this command.

## Explicit non-goals

B-08 does not implement the B-07 priority/reference queue, extraction, Blocks,
Locators, chunks, `source_id`, Evidence, MCP, Hooks, Web behavior, or curated
research summaries.
