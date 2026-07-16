# R1 Acceptance Report

- Verdict: **ACCEPTED**
- Acceptance date: 2026-07-16
- Integration branch: `research-assistant`
- Accepted implementation head: `73070f1`
- Final report checkpoint: `checkpoint/r1-accepted`
- Detailed regression contract:
  [`r1-basic-chain-acceptance.md`](r1-basic-chain-acceptance.md)
- Product truth source:
  [`research-assistant-product-contract.md`](research-assistant-product-contract.md)
- Execution plan:
  [`research-assistant-roadmap.md`](research-assistant-roadmap.md)

`checkpoint/r1-accepted` is created when this report commit is fast-forwarded to
`research-assistant`; the tag therefore identifies the report commit without
requiring the document to predict its own commit ID.

## Accepted scope

R1 establishes the deterministic trusted-inventory and original-source-location
chain:

```text
project registration
-> accountable inventory and local fingerprints
-> classification and two-axis file state
-> coverage reconciliation
-> deterministic text / Notebook / PDF extraction
-> locator-preserving chunking
-> persistent source identity and versions
-> precise Evidence
-> current-source reopen
-> moved-source recovery
-> Source and Evidence health
-> repeatable basic-chain end-to-end acceptance
```

This milestone keeps source projects read-only, machine state under `.llmwiki/`,
and curated Markdown under `wiki/`. It does not make Research Core a separate
chatbot and does not introduce network, LLM, MCP, Hook, or Web behavior into the
deterministic chain.

## R1 completion criteria

| # | Required condition | Acceptance evidence |
|---|---|---|
| 1 | No silent omission inside scan scope | Independent regular-file enumeration equals the Manifest ledger before and after a controlled rename. |
| 2 | Every in-scope file has an auditable record | The fixture's Python, Notebook, PDF, and opaque unsupported file each have exactly one Manifest file record. |
| 3 | Every file has processing status, read depth, and reason | Schema-decoded file states and all summary axes reconcile exactly to the file count. |
| 4 | Code, Notebook, and PDF extraction retain original location | Python line ranges, Notebook cell indexes/IDs, and PDF page numbers survive extraction and C-08 chunking. |
| 5 | Every source has a stable `source_id` | Four unique IDs are assigned; unchanged synchronization is a byte-stable no-op. |
| 6 | Evidence can locate and reopen the current original file | Three persisted Evidence records reopen exact excerpts with verified hashes and locators. |
| 7 | A moved file can be rebound by identity and fingerprint | D-05 recovers the same source ID through one unique exact content hash; existing Evidence reopens at the new path. |
| 8 | Basic end-to-end acceptance passes | `tests.test_r1_end_to_end` runs the complete chain twice from independent clean temporary directories and compares normalized results. |
| 9 | Full repository tests and health checks pass | 207 tests passed with 6 intentional skips; wiki health reports zero structural issues. |
| 10 | `research-assistant` is clean | Verified after fast-forward landing and checkpoint creation. |
| 11 | Completed R1 tasks have atomic commits and local checkpoints | The checkpoint inventory below resolves every R1 task and hardening change. |
| 12 | Final report, commits, tests, limitations, and rollback are recorded | This document provides the required record. |

## Commit and checkpoint inventory

| Capability | Commit | Local checkpoint |
|---|---|---|
| B-01 project registration | `15d317a` | `checkpoint/b-01-project-register` |
| B-02 scan policy | `07be63d` | `checkpoint/b-02-scan-policy` |
| B-03 accountable project inventory | `be4b825` | `checkpoint/b-03-project-inventory` |
| B-04 incremental file fingerprints | `f6fcd43` | `checkpoint/b-04-file-fingerprints` |
| B-05 file classification | `7ed9196` | `checkpoint/b-05-file-classification` |
| B-06 two-axis Manifest file state | `9c6ba5d` | `checkpoint/b-06-manifest-file-state` |
| B-08 coverage and failure report | `5c864a2` | `checkpoint/b-08-coverage-report` |
| C-01 extraction schema | `e4b3fed` | `checkpoint/c-01-extraction-schema` |
| C-02 text-family extraction | `5093f80` | `checkpoint/c-02-text-extractors` |
| C-03 Notebook extraction | `946d05a` | `checkpoint/c-03-notebook-extractor` |
| C-03 Notebook payload hardening | `daf1330` | `checkpoint/c-03-notebook-payload-bounds` |
| C-04 PDF page extraction | `66a7ba7` | `checkpoint/c-04-pdf-extractor` |
| C-08 locator-preserving chunking | `1eb64a2` | `checkpoint/c-08-locator-chunking` |
| C-08 chunk-schema hardening | `6f01136` | `checkpoint/c-08-locator-chunking-hardening` |
| D-01 persistent source identity | `a11fe6d` | `checkpoint/d-01-source-identity` |
| D-02 source versions and path history | `1230ac2` | `checkpoint/d-02-source-versions` |
| D-03 precise Evidence schema | `00f036c` | `checkpoint/d-03-evidence-schema` |
| D-03 Evidence version hardening | `8e13c6f` | `checkpoint/d-03-evidence-version-hardening` |
| D-04 source locate/open | `917475c` | `checkpoint/d-04-source-open` |
| D-05 source relocation recovery | `a49c6a8` | `checkpoint/d-05-source-relocation` |
| D-06 Source and Evidence health | `9b38353` | `checkpoint/d-06-source-health` |
| J-01 R1 basic-chain acceptance | `73070f1` | `checkpoint/j-01-r1-e2e` |
| Final R1 acceptance record | resolve tag | `checkpoint/r1-accepted` |

All implementation checkpoint tags through J-01 were resolved locally before
preparing this report. `checkpoint/r1-accepted` is created only after the report
commit lands. No commit or tag was pushed.

## Validation results

Validation environment:

- Windows `10.0.26200.0`
- PowerShell `5.1.26100.8875`
- Python `3.13.9`
- Ruff `0.12.0`
- Git `2.55.0.windows.2`

| Gate | Command / method | Result |
|---|---|---|
| Focused J-01 | `python -B -m unittest tests.test_r1_end_to_end` | 1 passed |
| Relevant B/C/D/J regression | Explicit 18-module `unittest` run covering registration, scan policy, inventory, state, coverage, extraction, chunking, Source, Evidence, recovery, health, and J-01 | 190 passed, 6 skipped |
| Full repository suite | `python -B -m unittest discover -s tests` | 207 passed, 6 skipped |
| Targeted Ruff | `python -m ruff check tests/test_r1_end_to_end.py` | Passed |
| Full Ruff invariant | `python -m ruff check .` | Exactly 46 historical errors, matching the required baseline; no new Ruff error |
| Wiki health | `$env:PYTHONIOENCODING='utf-8'; python -B tools/health.py` | 1 page scanned; 0 stubs, 0 index issues, 0 log-coverage issues |
| Diff hygiene | `git diff --check` and `git diff --cached --check` | Passed |
| Generated bytecode hygiene | Recursive `__pycache__` audit | No generated cache directories remain |
| Checkpoint audit | Resolve every R1 tag with `git rev-list -n 1` | All required R1 checkpoints resolve to the commits listed above |

The full Ruff command intentionally exits nonzero while reporting the historical
violations. R1 acceptance requires the count to remain exactly 46, not silently
to redefine those pre-existing violations as clean.

## Determinism, locality, and source-read-only evidence

The J-01 fixture uses separate temporary directories for the generated research
project and assistant workspace. It records the complete source directory set
and each regular file's SHA-256, size, nanosecond mtime, and permission mode.
Those values remain unchanged through Core operations. The only allowed source
change is the test-controlled rename from `src/model.py` to
`archive/model.py`; the renamed file retains the exact recorded metadata tuple.
No `.llmwiki/` or `wiki/` directory is created in the source project.

Fail-fast patches reject calls through the repository LLM helper, sockets, URL
opening, and browser opening. The exercised functions are deterministic local
Core functions. Coverage output, unchanged source synchronization, health
output, and normalized results from two clean runs are all checked for stable
repetition.

## Known limitations

1. This acceptance covers only J-01's deterministic R1 chain. The complete
   15-artifact `understand -> locate -> query -> reconcile -> plan -> render`
   expansion remains a distinct J-01 slice in R3.
2. Extraction is in memory and does not promote Manifest state beyond the honest
   B-06 inventory classification state.
3. Opaque unsupported files are accountable and carry explicit state/reason
   records, but are not extracted.
4. PDF support is deterministic text extraction only. OCR and vision are later
   scope.
5. D-05 rebinds only a unique exact-hash candidate. Multiple exact-hash
   candidates remain ambiguous and fail closed.
6. R1 does not implement later verified query, reconciliation, planning,
   rendering, Web, MCP, Hook, host-adapter, or external-provider behavior.

## Rollback

Use non-destructive history operations only:

1. To inspect or branch from the state immediately before J-01:

   ```powershell
   git switch -c rollback/r1-pre-j01 checkpoint/d-06-source-health
   ```

2. To remove the final report from an integration branch, resolve and revert its
   tagged commit:

   ```powershell
   git revert (git rev-list -n 1 checkpoint/r1-accepted)
   ```

3. To revert the J-01 regression after reverting this report:

   ```powershell
   git revert 73070f1
   ```

4. Revert earlier dependent R1 commits only in reverse dependency order. Keep
   checkpoint tags as an audit trail.

Do not use `git reset --hard`, `git clean`, force push, or source-project writes
as rollback mechanisms.

## Next milestone

R1 is complete. The next executable task is G-01: a host-independent Research
Core service facade for register, inventory/scan, coverage, and source-open.
Starting R2 is outside this R1 acceptance/report task.
