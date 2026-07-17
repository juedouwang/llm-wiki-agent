# B-07 Deterministic Adaptive Reading Priority

- Status: implemented and validated on **2026-07-17**
- Domain module: `tools/reading_priority.py`
- Core facade: `ResearchCoreService.prioritize(project_id)`
- CLI: `python tools/project.py prioritize <project_id> --json`
- Durable artifact: `.llmwiki/projects/<project_id>/indexes/reading-priority.json`
- Artifact identity: Schema v1 / `llmwiki-reading-priority` / `reading-priority-v1`
- Validation: `tests/test_reading_priority.py`, `tests/test_project_layout.py`
- Checkpoint: `checkpoint/b-07-adaptive-reading-priority`

## Purpose

B-07 consumes the current B-06 `project-inventory-v4` Manifest and produces a
deterministic recommendation layer for later readers. It ranks every ordinary
Manifest file, identifies bounded deep-read candidates, and records a
reference-promotion queue when eligible project material points to a file that
has not already been deep-read.

The recommendation layer is deliberately separate from Manifest truth:

- it does not rewrite `manifest.jsonl` or any `file_state`;
- it does not create `project-inventory-v5`;
- `current_read_depth` is copied from the current Manifest;
- `recommended_read_depth` is a recommendation for a later consumer;
- only records with `deep_read_status="selected"` are executable selections;
- `deep_read_status="deferred"` remains visible for accountability but is not an
  instruction to read the file now.

B-07 is deterministic from the current classification role, project-relative
path/name signals, and bounded incoming-reference signals. It is **not goal-aware**, does not use onboarding goals, and performs no extraction,
semantic reading, vision/OCR, or LLM call.

## Preconditions and entry points

The project must already be registered and have a current, valid
`project-inventory-v4` Manifest. The policy snapshot persisted in that Manifest
must still exactly match the policy reconstructed from the registered source
root and current `.llmwikiignore`/configuration. If the Manifest or policy is
stale, run inventory first.

### Core

```python
from tools.research_core import ResearchCoreService

core = ResearchCoreService(workspace_root=r"E:\ResearchCore")
result = core.prioritize("study-0123456789ab")

print(result.priority_file)
print(result.priority["summary"])
```

`ReadingPriorityResult.as_dict()` has exactly these keys:

```text
project_id
manifest_file
priority_file
priority
```

The two file fields are local machine-state paths. This result is not a
host-safe MCP DTO.

### CLI

```powershell
python tools/project.py prioritize study-0123456789ab `
  --workspace-root E:\ResearchCore `
  --json
```

The JSON CLI envelope adds `ok: true` to the exact
`ReadingPriorityResult.as_dict()` payload. The CLI delegates ranking and writes
to `ResearchCoreService.prioritize`; it does not duplicate filesystem or policy
logic.

## Storage and identity

The command has one domain artifact and writes it atomically:

```text
.llmwiki/projects/<project_id>/indexes/reading-priority.json
```

It also participates in the shared per-project coordination protocol. First use
may create `indexes/machine-state.lock`, and that lock file intentionally remains
on disk after release; it is not a second B-07 domain artifact.

The priority artifact contains no timestamp or absolute source path. It records the
exact identity of its input Manifest:

- `manifest_version`: exactly `project-inventory-v4`;
- `scan_generation`: the exact positive Manifest generation;
- `file_count`: the number of ordinary Manifest `file` records;
- `byte_count`: the sum of those ordinary files' declared sizes;
- `content_sha256`: SHA-256 of the exact current `manifest.jsonl` bytes.

Excluded files, excluded/pruned directories, symbolic links, skipped
directories, and special entries are not ranked. They remain accountable in the
Manifest and coverage report, not in this ordinary-file recommendation list.

## Execution-authorizing load

Any later reader that will execute `deep_read_status="selected"` records **must**
load the artifact through:

```python
from tools.reading_priority import load_current_reading_priority

priority = load_current_reading_priority(workspace_root, project_id)
```

This API acquires the shared machine-state lock and grounds the artifact in the
exact current Manifest bytes, hash, generation, counts, every ordinary file's
persisted identity/classification/state, the reconstructed current B-02 policy,
and recomputed intrinsic limitations. It revalidates Manifest and policy state
before returning.

`load_reading_priority(priority_file, ...)` is deliberately only a strict
**structural parser**. It rejects malformed, legacy, future, and internally
inconsistent Schema v1 data, but it cannot establish that the artifact is current
or policy-authorized for execution. In particular, a structurally valid artifact
whose recommendation conflicts with current policy must be rejected by
`load_current_reading_priority`; callers must never authorize reads from the bare
parser alone.

## Exact Schema v1 key sets

Unknown, missing, or extra fields fail closed. JSON is serialized as UTF-8 with
sorted keys, two-space indentation, and one final newline; the order below
documents the semantic key set rather than physical sorted-key order.

### Top-level object

Exactly:

```text
schema_version
kind
priority_version
project_id
manifest
limits
summary
files
promotion_queue
```

Fixed identity values:

```text
schema_version   = 1
kind             = llmwiki-reading-priority
priority_version = reading-priority-v1
```

### `manifest`

Exactly:

```text
manifest_version
scan_generation
file_count
byte_count
content_sha256
```

### `limits`

Exactly:

```text
reference_source_max_files
reference_source_max_file_bytes
reference_source_max_total_bytes
deep_read_max_files
deep_read_max_total_bytes
large_dataset_min_files
large_dataset_min_bytes
```

The v1 values are fixed:

| Key | Value |
|---|---:|
| `reference_source_max_files` | 128 |
| `reference_source_max_file_bytes` | 262,144 bytes (256 KiB) |
| `reference_source_max_total_bytes` | 4,194,304 bytes (4 MiB) |
| `deep_read_max_files` | 128 |
| `deep_read_max_total_bytes` | 33,554,432 bytes (32 MiB) |
| `large_dataset_min_files` | 32 |
| `large_dataset_min_bytes` | 67,108,864 bytes (64 MiB) |

A persisted v1 artifact with different limits is invalid rather than a new
configuration variant.

### `summary`

Exactly:

```text
manifest_file_count
manifest_byte_count
ranked_file_count
reference_source_count
reference_bytes_read
resolved_reference_count
promotion_candidate_count
promotion_queue_count
deep_read_candidate_count
deep_read_selected_count
deep_read_selected_bytes
deep_read_deferred_count
limited_file_count
priority_tiers
deep_read_statuses
reference_scan_statuses
```

The three final fields are sorted count maps and contain only positive counts
for enum values that occur. Every scalar and count map is recomputed from
`files` and `promotion_queue` when loading; a non-reconciling summary fails
closed.

### Each `files[]` record

Exactly:

```text
path
content_sha256
size_bytes
format
research_role
processing_status
current_read_depth
priority_score
priority_rank
priority_tier
recommended_read_depth
deep_read_status
reference_scan_status
reference_bytes_read
dataset_group
reason_codes
referenced_by
```

Important field semantics:

- `path` is normalized NFC project-relative POSIX form;
- `content_sha256`, `size_bytes`, `format`, `research_role`,
  `processing_status`, and `current_read_depth` are grounded in the current
  Manifest;
- `priority_rank` is a contiguous one-based rank across every ordinary file;
- `priority_score` is a non-negative deterministic integer;
- `dataset_group` is `null`, `<project-root>`, or a normalized project-relative
  group path;
- `reason_codes` is a non-empty, duplicate-free sequence of stable kebab-case
  codes in deterministic construction order;
- `referenced_by` is a sorted, duplicate-free list of ordinary Manifest source
  paths whose content was policy-authorized, verified, and read by B-07.

Closed enums:

```text
priority_tier:
  promoted | critical | high | normal | low | limited

deep_read_status:
  selected | deferred | not-candidate | limited

reference_scan_status:
  read | deferred-file-size | deferred-file-count |
  deferred-total-bytes | limited | unsupported-format | not-applicable
```

`current_read_depth` and `recommended_read_depth` use the B-06 read-depth enum:

```text
deep_read | normal_read | sampled | metadata_only | ignored | unsupported
```

### Each `promotion_queue[]` record

Exactly:

```text
queue_rank
path
content_sha256
size_bytes
current_read_depth
recommended_read_depth
priority_rank
priority_score
deep_read_status
referenced_by
reason_codes
```

Queue ranks are contiguous and queue entries must exactly match their
corresponding `files[]` records. The queue contains **every** referenced
promotion candidate whose current depth is `normal_read`, `sampled`, or
`metadata_only` and that is not otherwise limited or already deep-read. This is
true whether its `deep_read_status` is `selected` or `deferred`.

Consumers must therefore filter the queue by:

```text
deep_read_status == selected
```

A deferred queue entry records a valid promotion recommendation that did not
fit the fixed current deep-read budget; it must not be silently dropped, and it
must not be executed in the current pass.

## Deterministic scoring and rank order

### Role base scores

The classification role supplies the base score and the stable
`role-<role-name>` reason:

| Research role | Base score |
|---|---:|
| `project_documentation` | 1800 |
| `paper` | 1700 |
| `configuration` | 1600 |
| `experiment` | 1500 |
| `result` | 1400 |
| `notebook` | 1300 |
| `source_code` | 1200 |
| `automation` | 1100 |
| `documentation` | 1000 |
| `dependency_manifest` | 900 |
| `bibliography` | 800 |
| `test_code` | 700 |
| `project_metadata` | 600 |
| `figure` | 500 |
| `run_log` | 400 |
| `dataset` | 300 |
| `model_artifact` | 200 |
| `unknown` | 100 |

### Path/name and reference bonuses

Bonuses are additive:

| Signal | Bonus | Reason code |
|---|---:|---|
| File is at project root | 250 | `root-level-file` |
| Stem starts with `readme` | 600 | `readme-priority` |
| Exact known entrypoint name | 450 | `entrypoint-priority` |
| Filename contains a research signal word | 300 | `research-signal-name` |
| At least one resolved incoming reference | 10,000 + 100 per distinct source | `inbound-key-reference` |

Known entrypoint names are `main.py`, `train.py`, `evaluate.py`, `eval.py`,
`run.py`, `app.py`, `cli.py`, `main.r`, `main.jl`, `main.m`, `index.js`, and
`index.ts`.

Research signal words are `architecture`, `ablation`, `benchmark`,
`evaluation`, `metric`, `metrics`, `result`, and `results`.

### Tier selection and ordering

Tier selection is deterministic and evaluated in this order:

1. any hard limitation -> `limited`;
2. eligible incoming-reference promotion -> `promoted`;
3. role is `project_documentation`, `paper`, or `configuration` -> `critical`;
4. score at least 1450 -> `high`;
5. score at least 700 -> `normal`;
6. otherwise -> `low`.

Files are sorted by the fixed tier order
`promoted, critical, high, normal, low, limited`, then descending score, then
ascending normalized path. `priority_rank` is assigned after this sort.

The scoring inputs and thresholds are fixed v1 behavior. No goal, query,
embedding, semantic similarity, model output, timestamp, or host state affects
the result.

## Deep-read candidate and budget semantics

A file can be a normal role-based deep-read candidate only when it is not
limited, is not already `deep_read`, and its role is one of:

```text
project_documentation, paper, configuration, experiment, result, notebook,
source_code, automation, documentation, dependency_manifest, bibliography,
test_code, project_metadata, figure
```

An eligible incoming reference can also make a file a promotion candidate when
its current depth is one of:

```text
normal_read | sampled | metadata_only
```

Candidates are considered in deterministic priority-rank order. At most 128
files and at most 32 MiB of selected file bytes are admitted. A candidate that
fits both remaining limits receives:

```text
recommended_read_depth = deep_read
deep_read_status        = selected
reason_codes           += deep-read-selected
```

Every other valid candidate remains explicit:

```text
recommended_read_depth = deep_read
deep_read_status        = deferred
reason_codes           += deep-read-budget-deferred
```

A limited or non-candidate file preserves its current read depth as the
recommendation. Files already at `deep_read` receive `already-deep-read`; other
non-candidates receive `not-deep-read-candidate`.

## Hard limitations that references cannot override

A reference never bypasses B-02 or Manifest safety. A file is limited when any
of these applies:

- current B-02 local content access is not `allowed`; the current policy reason
  code, including sensitive or oversized restrictions, is preserved;
- `processing_status` is `failed` or `missing`;
- current read depth is `ignored` or `unsupported`;
- format is `unknown`, `binary`, `zip`, `gzip`, `tar`, `wav`, or `mp3`;
- role is `model_artifact`;
- format is `pytorch_checkpoint`, `model_checkpoint`, or `pickle`;
- the dataset group is large.

Dataset files are grouped by the nearest path component named `data`,
`dataset`, or `datasets`; otherwise their parent directory is the group. A
group is large at 32 files or 64 MiB, whichever threshold is reached first.
Every file in a large group remains limited, so one README/config reference
cannot trigger bulk reading of a training dataset.

Stable limitation reasons include the B-02 policy reason itself plus
`processing-state-limited`, `read-depth-limited`, `unsupported-format`,
`model-artifact-limited`, and `large-dataset-limited`.

## Bounded reference scan

Only eligible text-like files in selected source roles are considered as
reference sources.

Source roles:

```text
project_documentation, paper, configuration, experiment, notebook,
source_code, automation, documentation, dependency_manifest, bibliography
```

Supported reference-source formats:

```text
plain_text, markdown, restructured_text, python, r, julia, matlab, c, cpp,
java, javascript, typescript, shell, powershell, batch, rust, go, ruby, perl,
sql, html, css, xml, json, jsonl, yaml, toml, ini, latex, bibtex, notebook,
log, svg
```

The deterministic source order is classification-role order, then shallower
path depth, then path. The scan reads at most:

- 128 reference source files;
- 256 KiB from any one declared source file;
- 4 MiB total declared reference-source content.

An eligible source beyond a bound is retained in `files[]` with an explicit
`reference_scan_status`; it is not silently treated as read.

### Parsed reference forms

The bounded parser recognizes:

- Markdown links and images;
- quoted path-like values, including configuration strings;
- LaTeX `includegraphics`, `input`, `include`, `bibliography`, and
  `addbibresource` arguments;
- bare path-like tokens with a filename extension.

References are URL-decoded, fragments and query strings are removed, and a
suffix-less candidate also tries `.tex`, `.bib`, `.md`, `.rst`, `.yaml`,
`.yml`, and `.json`.

Resolution is conservative:

1. exact source-relative path;
2. exact project-root-relative path;
3. unique basename only when the candidate is not an explicit path.

Absolute paths, Windows drive paths, URI schemes, outside-root or traversal
paths, directory-only values, ambiguous matches, self-references, and paths not
present as ordinary Manifest files are rejected. An explicit unresolved path
never falls back to an unrelated basename match.

Incoming edges only originate from sources whose
`reference_scan_status="read"`. `referenced_by` cannot name a source that was
limited, unsupported, deferred, or otherwise unread.

## Verified local reads and atomic failure behavior

Generation holds the persistent per-project
`indexes/machine-state.lock`. The lock file remains on disk after release and is
shared with the other cooperating machine-state writers. Before the lock is
opened and before any artifact operation, every existing machine-state path
component is checked from the canonical workspace root. Symbolic-link and
Windows reparse-point ancestors, path redirection, non-directory ancestors, and
wrong-type leaves fail closed, so an `indexes/` redirection cannot send the lock,
temporary file, or priority artifact outside the registered machine-state root.

Before each raw reference read, B-07 reconstructs and revalidates the current
B-02 policy against the Manifest snapshot. Each selected source read then uses:

- `lstat` before and after;
- descriptor `fstat` before and after;
- no-follow descriptor open where the platform supports it;
- regular-file and resolved-root checks;
- device/inode identity checks;
- declared size and `mtime_ns` checks;
- a read bound of declared size plus one byte;
- exact byte-count and SHA-256 verification against the Manifest.

A replaced, growing, truncated, moved-outside-root, symlinked, or otherwise
changed source fails the whole generation. It is never partially trusted.

Every reference source that contributed an incoming edge is retained as a
verified file record. Immediately before atomic replacement, B-07 revalidates
policy and repeats the bounded descriptor/hash/size/mtime/root verification for
each of those sources. A source mutation after the initial reference scan but
before commit therefore fails the generation rather than committing stale edges.

The exact Manifest bytes are snapshotted around loading and generation. The
Manifest and policy are revalidated immediately before atomic replacement. If
an existing priority artifact is bound to the same Manifest, it must also pass
full current Manifest/policy grounding before replacement. A structurally valid
artifact bound to an older Manifest may be replaced, but it is never treated as
current executable state.

Malformed artifacts, unversioned legacy v0 artifacts, unsupported future
artifacts, symbolic-link artifacts, redirected machine-state ancestors, stale
Manifest input, policy changes, lock timeouts, source mutations, read races, and
validation mismatches fail closed. The previous `reading-priority.json` remains
byte-for-byte preserved and temporary files are cleaned up without following a
redirected temporary path.

For unchanged valid inputs, regeneration is byte-identical.

## Independent workflow boundaries

B-07 is independent of these neighboring slices:

- **B-06 Manifest state:** B-07 consumes v4 state but does not change
  `processing_status`, `read_depth`, reason codes, or the Manifest version.
- **B-08 coverage:** coverage audits Manifest truth only. It neither consumes
  nor summarizes recommendations from `reading-priority.json`.
- **E-08 project understand:** the current one-action prefix still stops at
  `register -> inventory -> classify`; it does not call `prioritize` or advance
  the canonical `adaptive-read` stage.
- **H-07 reconciliation:** reconciliation still performs its full correctness
  fallback through `classify` and validates Manifest/coverage only. It does not
  generate, validate, or acknowledge a reading-priority artifact.

B-07 also creates no extracted document, Block, Locator, chunk, `source_id`,
Evidence, project run, run-stage transition, Host Context Pack, MCP operation,
Hook event, Plugin behavior, Web output, source-project write, or curated
Markdown write. It sends no content to an external service and does not invoke
an LLM.

## Validation

Focused validation recorded on **2026-07-17**:

```powershell
python -B -m pytest -q -p no:cacheprovider `
  tests/test_reading_priority.py `
  tests/test_project_layout.py
```

Result:

```text
45 passed, 3 skipped
```

The focused suite covers ranking and promotion semantics, selected-versus-
deferred queue retention, current-grounded execution authorization versus bare
structural parsing, intrinsic and current-policy tamper rejection, exact fixed-v1
boundaries (128 reference files, 256 KiB per reference, 4 MiB aggregate, 128
selected deep reads, 32 MiB selected bytes, and 64 MiB dataset grouping),
reference-resolution rejection, byte-identical reruns, verified descriptor
reads, post-read/pre-commit source mutation, Manifest/policy races, atomic
preservation, ancestor symbolic-link/reparse redirection, Core/CLI parity,
shared-lock behavior, empty projects, and absence of extraction, run,
source/Evidence, curated knowledge, network, LLM, MCP, Hook, Plugin, Web, or
source-write side effects. Three platform-dependent symbolic-link cases are
skipped when the host does not grant link-creation privilege; deterministic
mocked redirection checks remain covered.
