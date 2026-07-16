# Persistent Source Identity (D-01)

D-01 assigns one stable, Research Core-generated `source_id` to every in-scope regular file in the current `project-inventory-v4` Manifest. The registry is project machine state. D-01 itself does not define source versions, path aliases, Evidence, or source reopening. D-02 now evolves live registries to `source-registry-v2`; see [`source-versions.md`](source-versions.md).

## Command

Register and inventory a project before synchronizing identities:

```powershell
python tools/project.py register <project-path> --json
python tools/project.py inventory <project_id> --json
python tools/project.py source sync <project_id> --json
```

`source sync` reads only the registered project's current Manifest and writes:

```text
.llmwiki/projects/<project_id>/sources.jsonl
```

It does not open, modify, or create files in the source research project. It does not extract content, call a model, or make an external request.

## Artifact contract

- Machine-record Schema: `schema_version: 1`
- `kind`: `llmwiki-source-registry`
- Original D-01 artifact version: `source-registry-v1`
- Identity strategy: `core-uuid4-v1`
- `source_id`: `src-` followed by 32 lowercase hexadecimal characters

The first JSONL row is the summary:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-source-registry",
  "registry_version": "source-registry-v1",
  "record_type": "summary",
  "project_id": "example-project",
  "identity_strategy": "core-uuid4-v1",
  "source_count": 2
}
```

Each subsequent row is one source assignment:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-source-registry",
  "registry_version": "source-registry-v1",
  "record_type": "source",
  "project_id": "example-project",
  "source_id": "src-0123456789abcdef0123456789abcdef",
  "manifest_path": "src/model.py",
  "first_seen_scan_generation": 1
}
```

Rows are sorted by normalized project-relative POSIX path. Every `source_id` and `manifest_path` must be unique, and the summary count must equal the number of source rows. This v1 shape remains a strict read-compatible migration input; the next `source sync` upgrades it atomically to v2 while preserving IDs.

## Synchronization semantics

1. The first observation of a Manifest path receives a new opaque UUID4 identity.
2. Repeating a sync preserves every ID. An identical canonical payload is not rewritten.
3. A new path receives one new ID without changing existing assignments.
4. A path absent from the current Manifest remains in the registry. D-01 does not interpret absence as deletion, movement, or renaming, and does not reuse that ID.
5. Sync accepts only the current `project-inventory-v4` Manifest. Older artifacts must first be upgraded through the inventory compatibility layer; unknown or future versions fail closed.
6. A registry assignment whose first-seen generation is newer than the current Manifest causes sync to fail instead of allowing stale state to overwrite newer identity state.

## Concurrency and fail-closed behavior

Writers serialize through the adjacent `sources.jsonl.lock` file. The registry is reloaded while holding the lock, so concurrent synchronizations cannot allocate multiple IDs for the same path. New data is written to a same-directory temporary file, flushed, and installed with `os.replace`.

The following state is rejected without replacing the existing registry:

- missing, Boolean, legacy-v0, or future `schema_version` values;
- malformed JSON, duplicate JSON keys, non-finite numbers, or blank JSONL rows;
- extra/missing fields or the wrong kind, artifact version, identity strategy, or project ID;
- non-normalized paths, malformed IDs, duplicate IDs, duplicate paths, or unsorted rows;
- count mismatches, future first-seen generations, or a stale Manifest;
- a registry represented by a symbolic link or a non-regular file.

## Explicit D-01 non-goals

`source-registry-v1` stores no content hash, source version, historical path alias, Evidence, locator, excerpt hash, extraction result, processing state, source health, MCP, Hook, Web, or LLM state. D-02 adds content versions and explicit path history in `source-registry-v2`; D-03 through D-06 add Evidence, reopening, relocation recovery, and health separately.
