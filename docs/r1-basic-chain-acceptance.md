# R1 Basic-Chain Acceptance

- Status: accepted implementation; repository-level landing results are recorded in
  `r1-acceptance-report.md`
- Date: 2026-07-16
- Roadmap slice: J-01 (R1 basic chain)
- Automated test: `tests/test_r1_end_to_end.py`

## Scope

This acceptance closes the deterministic R1 chain defined by
`research-assistant-roadmap.md`:

```text
register -> inventory/classify/state -> coverage -> source identity
-> deterministic extraction -> locator-preserving chunking -> Evidence
-> source open -> controlled rename -> relocation recovery -> source health
```

The fixture is generated inside a clean temporary directory. It contains exactly
four regular files:

| Path | Purpose | Expected inventory state |
|---|---|---|
| `src/model.py` | line-addressable source code | `discovered / sampled / classification-sample` |
| `notebooks/analysis.ipynb` | cell-addressable Notebook | `discovered / sampled / classification-sample` |
| `papers/study.pdf` | page-addressable PDF text | `discovered / sampled / classification-sample` |
| `artifacts/blob.weird` | accountable unsupported input | `discovered / unsupported / unsupported-format` |

The test deliberately renames only `src/model.py` to `archive/model.py` after
initial Evidence has been persisted.

## Automated acceptance matrix

| R1 requirement | Automated proof |
|---|---|
| No silent omission | An independent `os.scandir` regular-file walk is reconciled exactly with Manifest file records before and after the rename. |
| Every in-scope file is auditable | The Manifest contains one and only one file record for each of the four inputs, including the opaque unsupported file. |
| Every file has status, read depth, and reason | Each record is schema-decoded and checked for the expected `processing_status`, `read_depth`, `reason_code`, non-empty reason, and SHA-256; summary axes must each reconcile to the file count. |
| Key formats retain original locators | Python extraction/chunking preserves line ranges `1-2` and `3-3`; Notebook chunks preserve cell indexes and IDs; PDF chunks preserve page numbers. Chunk coverage validation must pass. |
| Sources have stable identity | Initial synchronization assigns four unique `source_id` values. An unchanged second synchronization performs no write, adds no version, and leaves `sources.jsonl` byte-identical. |
| Evidence reopens current original bytes | Selected Python, Notebook, and PDF chunks are persisted as Evidence and reopened with the same source ID, locator, exact excerpt, and verified excerpt hash. |
| A moved file can be rebound safely | After the one controlled rename, a refreshed inventory lets D-05 recover the original source ID by a unique exact content hash. Existing Evidence then reopens at the new path. |
| Source and Evidence health is deterministic | D-06 reports all four Sources and all three Evidence records as `valid`; a repeated health evaluation produces the same structured result. |
| The chain is repeatable | The complete chain runs from two independent clean temporary directories and their path-independent summaries must be equal. |

## Deterministic and read-only safety proof

The test keeps the generated research project and the assistant workspace in
separate sibling directories. Before Core operations it snapshots:

- the complete directory set;
- each regular file's SHA-256;
- byte size;
- nanosecond modification time; and
- permission mode.

The snapshot must remain identical through registration, inventory, coverage,
source synchronization, extraction, chunking, Evidence persistence, and source
opening. After the fixture-controlled rename, the only permitted difference is
that the exact metadata tuple formerly keyed by `src/model.py` is keyed by
`archive/model.py`. The test also asserts that neither `.llmwiki/` nor `wiki/`
is created inside the research source project.

The acceptance installs fail-fast guards around:

- `tools._utils.call_llm`;
- `socket.create_connection`;
- `urllib.request.urlopen`; and
- `webbrowser.open`.

Any attempted LLM, socket, URL, or browser call fails the test. The exercised
R1 path uses direct deterministic Core functions and does not invoke MCP, Hooks,
query/synthesis, planning, or rendering behavior.

## Repeatability checks inside the chain

In addition to running the whole fixture twice, the test requires:

- two coverage generations to be byte-identical;
- an unchanged source synchronization to be a no-op and byte-stable; and
- two D-06 health evaluations to have identical dictionaries.

Run the focused acceptance with:

```powershell
python -B -m unittest tests.test_r1_end_to_end
```

The landing gate additionally requires the full test suite, targeted Ruff, the
unchanged full-Ruff historical baseline, wiki health, diff hygiene, an atomic
commit, and a local checkpoint tag. Exact landing results belong in
`r1-acceptance-report.md` because the J-01 commit cannot record its own hash.

## Explicit non-goals and known limitations

- This is only J-01's deterministic R1 regression slice. The full J-01
  `understand -> locate -> query -> reconcile -> plan -> render` and 15-artifact
  acceptance remains scheduled for R3.
- Extraction is currently in memory and does not promote Manifest file state
  beyond the honest B-06 inventory state.
- The opaque unsupported file remains fully inventoried with an explicit state
  and reason, but it is not extracted.
- PDF support in this slice is deterministic text extraction only; OCR and
  vision remain later work.
- D-05 rebinds only a unique exact-hash candidate. Multiple matching candidates
  remain ambiguous and fail closed.
- This test does not implement or claim later understand/query/reconcile/plan,
  rendering, MCP, Hook, Web, or external-provider behavior.
