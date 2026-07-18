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

## Atomic machine-state replacement

The same module provides `write_atomic_stable_file(...)` for the direct-child
Source and Evidence registries. It first performs the non-redirected stable read
above when a destination exists. It then pins the trusted machine-state directory,
writes and flushes a unique temporary regular file, replaces only the direct-child
name, verifies that the temporary descriptor became that destination, and flushes
the directory where the platform supports the required primitive. POSIX uses
directory-descriptor-relative operations. Windows holds a non-delete-shared
directory handle and rechecks its final path around replacement. Unsupported
platform guarantees fail closed.

This primitive does not harden unrelated lock-token reads or every machine-state
loader. Its scope is the Source/Evidence registry payloads named below.

## Consumers hardened in this unit

- `tools/source_access.py` uses the primitive before reading actual current Source
  bytes. The `recover_relocation` write boundary accepts only a literal boolean.
- `tools/source_recovery.py` hashes the registered lexical path and relocation
  candidates through the same primitive, including read-only inspection.
- `tools/source_registry.py` loads and reloads `sources.jsonl` through a
  non-redirected stable descriptor, uses the stable atomic writer for registry
  replacement, and retains/re-hashes a relocation candidate immediately before
  and after binding. If post-commit revalidation fails, it atomically restores the
  previous registry and reports failure.
- `tools/evidence_registry.py` loads `evidence.jsonl` through a non-redirected
  stable descriptor, preserves the canonical lexical destination, and uses the
  same stable atomic writer for replacement.

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
replacement, relocation commit races, and write-boundary behavior.
