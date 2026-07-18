# Stable descriptor-bound file access (F-02C)

Status: implemented as an internal deterministic hardening unit for R3-BATCH.

## Purpose

F-02C closes path-redirection and pathname time-of-check/time-of-use gaps that
were found while reviewing F-02B currentness validation. It does not add a new
research capability. It strengthens the existing rule that Core may only read
bytes proven to belong to the registered project and may only replace the intended
direct-child registry beneath its own machine-state root.

## Internal primitive

`tools/stable_file_access.py` provides `read_stable_regular_file(...)`.
For one lexical path beneath a trusted root it:

1. validates that the lexical path remains beneath the trusted root;
2. optionally rejects every symbolic-link or reparse-point component;
3. opens the file once, using nonblocking mode where the platform exposes it so
   a raced FIFO/device replacement cannot stall before the regular-file check;
4. obtains the opened descriptor's final target and proves that target remains
   beneath the resolved trusted root **before the first byte is read**;
5. proves descriptor/path file identity, hashes and optionally captures bytes
   through that same descriptor, and checks metadata and identity again after the
   read; and
6. fails closed when the platform cannot expose a descriptor final path or stable
   file identity.

The descriptor final-path adapters are deterministic and local:

- Windows: `GetFinalPathNameByHandleW`;
- Linux: `/proc/self/fd/<descriptor>`;
- macOS: `F_GETPATH`.

No network or LLM operation is involved.

`lease_stable_regular_file(...)` retains the verified descriptor for a bounded
transaction. Its `StableFileLease.revalidate()` operation rechecks pathname and
descriptor identity, final path, metadata signature, size, and a complete SHA-256
read through the same descriptor. Source relocation uses this lease across the
registry commit.

On Windows, a stable regular-file descriptor shares read access only: concurrent
writable opens, replacement, rename, and deletion are denied while the read or
lease is active. The separately pinned trusted-root directory handle permits
ordinary child writes but does not share delete access, so multiple transactions
can pin the same root without allowing that root to be renamed or deleted under
them.

## Transaction-scoped root and lock

`lease_stable_directory(...)` pins one non-redirected trusted-root identity.
`exclusive_stable_file_lock(...)` opens that lease first, opens only a direct-child
regular lock file through the pinned root, and holds an operating-system exclusive
lock for the complete registry transaction. The lock file is persistent machine
state: release unlocks and closes it but deliberately does not unlink it. File
existence is therefore not interpreted as ownership.

Ordinary Source and Evidence registry readers now join the same adjacent lock
protocol as writers. This prevents a Windows read-only descriptor from denying a
concurrent atomic replacement outside the lock protocol. A reader retains the lock
until its complete stable read and parse have finished; it never modifies registry
content.

Source and Evidence writers reuse the **same** `StableDirectoryLease` for lock
acquisition, under-lock registry loads, replacement, and any relocation rollback.
Operations that need one bound Source/Evidence snapshot always acquire locks in the
canonical order **Source registry lock ? Evidence registry lock** and retain the
Source snapshot until Evidence parsing or registration finishes. No internal path
acquires those locks in reverse order. A root identity substitution, lock-file
redirection, or lock identity change fails closed instead of moving later
transaction stages onto a different pathname object.

## Atomic machine-state replacement

The same module provides `write_atomic_stable_file(...)` for the direct-child
Source and Evidence registries. It first performs the non-redirected stable read
above when a destination exists. It then uses the caller's transaction root lease
(or creates one for standalone use), writes and flushes a unique temporary regular
file, replaces only the direct-child name, verifies that the temporary descriptor
became that destination, and flushes the directory where the platform supports
the required primitive. POSIX uses directory-descriptor-relative operations.
Windows holds a non-delete-shared root handle, creates the temporary file without
read/write sharing while retaining the delete sharing required for its own rename,
and rechecks final paths around replacement. Unsupported platform guarantees fail
closed.

Atomic write outcomes are explicit:

- `unchanged`: existing bytes already equal the requested payload;
- `committed`: replacement and required verification completed;
- `committed-durability-unknown`: replacement is verified as visible, but POSIX
  directory flushing failed; and
- `StableFileCommitUnknownError`: replacement occurred but post-replace identity
  or root verification could not establish which bytes are now current.

Failures before replacement remain ordinary no-commit exceptions. Registry
writers translate the two uncertain post-replace outcomes into explicit domain
commit-state errors rather than reporting success or retrying blindly.

For Source relocation, rollback uses the exact registry bytes observed under the
transaction lease, including a valid noncanonical serialization. It first performs
an exact compare-and-swap check that current registry bytes still equal the
attempted replacement. Foreign bytes are never overwritten. A successful rollback
then re-reads and verifies byte-for-byte restoration of that original preimage;
rollback visibility or durability uncertainty is reported explicitly.

The primitive intentionally covers the Source/Evidence registry and lock payloads
named below, not every machine-state loader in the repository.

## Consumers hardened in this unit

- `tools/source_access.py` uses the primitive before reading actual current Source
  bytes. The `recover_relocation` write boundary accepts only a literal boolean.
- `tools/source_recovery.py` hashes the registered lexical path and relocation
  candidates through the same primitive, including read-only inspection.
- `tools/source_registry.py` makes ordinary loads and writers participate in the
  persistent adjacent OS lock beneath one pinned root lease, loads and reloads
  `sources.jsonl` through that lease, and uses it for stable atomic replacement.
  Relocation retains/re-hashes its candidate around the commit and uses exact-CAS
  rollback from the actual registry-byte preimage when post-commit revalidation
  fails.
- `tools/evidence_registry.py` loads bound Source/Evidence snapshots under the
  canonical Source-then-Evidence lock order. Ordinary Evidence loads and Evidence
  registration retain both the selected Source snapshot and the Evidence lock
  through parsing or replacement, preserve the canonical lexical destination, and
  never bind Evidence against mismatched registry generations.

## Security and product boundary

F-02C:

- does not extract or semantically interpret source content;
- does not infer Evidence stance, Claim conflict, or a research conclusion;
- does not add CLI, MCP, Web, Hook, or external-send behavior;
- does not authorize mutating relocation when a caller passes a non-boolean value;
- does not weaken Source version, content-hash, Locator, excerpt, or policy checks;
- does not read real binary research sources as part of its validation suite; and
- keeps F-02B Claim validation read-only (`recover_relocation=False`).

Generated text fixtures exercise descriptor escape, symlink/reparse rejection,
missing verification primitives, partial identity loss, lexical-path retargeting,
registry-redirection and machine-root substitution races, direct-child atomic
replacement, reader/writer lock coordination, repeated concurrent Source recovery,
relocation commit races, and write-boundary behavior.
