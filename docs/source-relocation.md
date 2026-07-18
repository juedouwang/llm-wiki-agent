# Source Relocation Recovery Contract (D-05)

D-05 restores a persistent source identity when its current recorded path can no
longer reopen the source registry's current content version. Recovery is fully
deterministic, local-only, and source-read-only. It may update only
`.llmwiki/projects/<project_id>/sources.jsonl`, and only after one candidate is
uniquely verified.

The implementation lives in `tools/source_recovery.py`; the final registry
transaction is owned by `tools/source_registry.py`.

## Trigger boundary

Recovery begins only after the current recorded path fails access or exact
content verification:

- `source locate` first resolves the current path without hashing it. A normal,
  resolvable current path therefore retains D-04's inexpensive behavior.
- `source open` hashes the current bytes. If reading fails or the bytes do not
  match the registered current source version, it attempts recovery once and
  retries once after a successful relocation.
- `source recover` explicitly evaluates current-path health, including its
  SHA-256, before considering relocation candidates.

A valid current path returns `not-needed` and does not write the registry.
Locator, source-version, content-hash, and excerpt-hash checks remain unchanged
after recovery. Relocation cannot make stale Evidence current again.

## Ordered recovery groups

D-05 evaluates exactly one priority group at a time:

1. **Path aliases** — every historical path in the source record except the
   failed current path.
2. **Exact content hash** — regular-file rows in the current validated Manifest
   whose `content_sha256` equals the source's recorded current SHA-256. Existing
   path-history entries are excluded because aliases have higher priority.
3. **Deterministic Git path history** — committed rename records in chronological
   order, followed by the current `git diff HEAD` rename records.

A lower-priority group is considered only when the current group has no verified
match and no blocked candidate. A unique verified match is persisted
immediately. Multiple exact matches in the same priority group return
`ambiguous` without considering a lower-priority group or changing any binding.

The Manifest is the deterministic local hash ledger; D-05 does not perform a
second ad hoc project-wide scan. Therefore a non-Git move normally requires a
fresh `project inventory` before hash recovery. A committed or working-tree Git
rename can still be recovered while the Manifest is stale. If the Manifest is
missing, malformed, or unsupported, recovery fails closed before Git history
because the higher-priority hash group cannot be audited safely.

## Read-only inspection boundary

`inspect_source_relocation(workspace_root, project_id, source_id)` evaluates the
same current-path check and ordered candidate groups without calling
`record_source_relocation(...)`. Its versioned result is
`llmwiki-source-relocation-inspection` / `source-relocation-inspection-v1` and
has exactly four statuses:

| Status | Meaning | Registry write |
|---|---|---|
| `current` | The registered path still matches the current recorded hash | Never |
| `relocatable` | One exact-hash candidate exists in the highest applicable group | Never |
| `ambiguous` | Multiple equal-priority exact-hash candidates exist | Never |
| `unresolved` | No safe candidate exists or a group cannot be audited | Never |

The result includes the registered path, source version/hash, ordered attempts,
candidate paths, and current-path failure reason. It always emits
`registry_write_performed: false`. F-02B uses this inspection only after strict
`open_source(..., recover_relocation=False)` fails, so Claim validation can
report a move without repairing the Source registry as a side effect.

Persisting a unique candidate remains an explicit D-05 recovery action through
`recover_source(...)`; inspection alone is never authorization to write.

## Candidate acceptance and fail-closed behavior

Every recovered candidate must:

- use a canonical project-relative POSIX path;
- remain within the registered project root;
- remain included by the current deterministic scan boundary;
- be a regular file, with no symbolic-link or reparse-point component;
- remain stable while read; and
- exactly match the current source version's recorded SHA-256.

Candidate bytes are hashed through one stable descriptor whose final target is
proven inside the registered project before the first read. Symbolic-link or
reparse-point redirection and path/file identity changes fail closed. The
read-only inspector and mutating recovery share this candidate-verification
primitive.

Unreadable, unstable, outside-root, symlink/reparse, or otherwise unverifiable
paths are never bound. If a priority group contains a blocked candidate, the
result is `unresolved` with reason
`source-relocation-candidate-unreadable`; D-05 does not silently choose another
candidate from that group. Missing, excluded, or hash-mismatched paths are not
matches and do not become bindings.

## Local Git boundary

Git recovery runs only the local executable and never contacts a remote. It:

- detects and validates the containing worktree root;
- invokes Git directly without a shell;
- disables optional repository locks, terminal prompts, and pagers;
- reads rename-only, NUL-delimited `git log` and `git diff` output;
- considers at most 4,096 commits;
- rejects output above 16 MiB; and
- applies a 15-second timeout to each Git command.

Git recovery relies on Git's deterministic rename detection. It does not infer
arbitrary copies, search remote history, fetch objects, or bind a candidate that
fails the same project-boundary and exact-hash checks used by other groups.

## Registry transaction and concurrency

`record_source_relocation(...)` owns the only persistence path. Under the
existing `sources.jsonl.lock`, it reloads current state and verifies the expected
source ID, current path/version/hash, final candidate boundary, regular-file
status, stable bytes, and SHA-256. It retains that candidate descriptor across the
registry replacement, re-hashes it immediately before and after the commit, and
atomically restores the previous registry if post-commit identity/path/hash
verification fails. A changed candidate therefore never produces a successful
relocation binding.

Relocation preserves:

- the existing `source_id`;
- every `SourceVersion` record and the current version number;
- existing path-history entries; and
- the source's last observed Manifest generation.

The recovered path is added to or refreshed in path history. A path already
owned historically by another source fails closed. Concurrent callers that
select the same path converge: at most one writes, while later callers observe
the already-current binding or return `not-needed`. No duplicate path-history
entry is created.

## Result model

`recover_source(...)` returns a transient, schema-versioned
`llmwiki-source-recovery-result` with one of four statuses:

| Status | Meaning | Registry write |
|---|---|---|
| `not-needed` | Current path is accessible and exact-hash valid | Never |
| `recovered` | One candidate was uniquely verified and is current | At most one concurrent caller writes |
| `ambiguous` | Multiple equal-priority exact-hash candidates remain | Never |
| `unresolved` | No safe match exists, or a priority group is blocked | Never |

Each ordered attempt records its method, verified candidate paths, and blocked
paths. Methods are `path-alias`, `content-hash`, and `git-history`.

## CLI

Evaluate or repair one source identity:

```bash
python tools/project.py source recover <project_id> <source_id> --json
```

All four evaluated statuses are successful command executions and return
`ok: true`; `ambiguous` and `unresolved` are explicit no-write outcomes rather
than parser/runtime failures. Human output includes status, method, previous and
current paths, candidates, write flag, reason, and detail.

`source locate` and `source open` invoke the same recovery core automatically
only after access failure. Automatic ambiguity raises the stable access reason
`source-relocation-ambiguous`. An unresolved automatic attempt re-raises the
original missing, boundary, read, or content-mismatch error.

## Safety, non-goals, and limitations

- Source projects are never renamed, moved, rewritten, timestamped, or otherwise
  modified by recovery. Candidate bytes are streamed only into local SHA-256
  state.
- There are no LLM, MCP, Hook, Web, network, remote-Git, or external-provider
  calls.
- Machine paths, hashes, locks, and recovery state stay under `.llmwiki/`; no
  curated knowledge is written to `wiki/projects/`.
- D-05 does not aggregate `valid/stale/missing/ambiguous` source or Evidence
  health, validate all locators, schedule extraction, propagate Claim state, or
  implement query/synthesis behavior. Those source/Evidence health semantics
  begin in D-06.
- Hash recovery can see only the current validated Manifest. Uninventoried,
  non-Git moves remain unresolved by design.
- Git rename detection can miss transformations Git does not classify as a
  rename; exact content verification prevents a false positive but cannot
  manufacture missing history.
