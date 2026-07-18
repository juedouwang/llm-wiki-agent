# Source Versions and Path History (D-02)

D-02 evolves the persistent source registry into an auditable ledger that binds each stable `source_id` to observed Manifest content hashes, a current path, and known path history. It remains deterministic, local-only, and source-read-only. Evidence, excerpts, reopening, relocation recovery, and source health remain D-03 through D-06 concerns.

## Commands

Register and inventory a project before synchronizing source state:

```powershell
python tools/project.py register <project-path> --json
python tools/project.py inventory <project_id> --json
python tools/project.py source sync <project_id> --json
```

Read one registered source's complete history without opening source-project bytes:

```powershell
python tools/project.py source history <project_id> <source_id> --json
```

Both commands operate on:

```text
.llmwiki/projects/<project_id>/sources.jsonl
```

`source sync` reads content hashes already recorded in the current `project-inventory-v4` Manifest. It does not stream source bytes, extract content, call an LLM, or make an external request. `source history` reads only machine state.

## Artifact contract

- Machine-record Schema: `schema_version: 1`
- `kind`: `llmwiki-source-registry`
- Artifact version: `source-registry-v2`
- Identity strategy: `core-uuid4-v1`
- Hash algorithm: `sha256`
- Ordering: summary, source rows by `source_id`, then version rows by `(source_id, version)`

The summary row records both source and version counts:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-source-registry",
  "registry_version": "source-registry-v2",
  "record_type": "summary",
  "project_id": "example-project",
  "identity_strategy": "core-uuid4-v1",
  "hash_algorithm": "sha256",
  "source_count": 1,
  "version_count": 2
}
```

A source row owns identity and path history:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-source-registry",
  "registry_version": "source-registry-v2",
  "record_type": "source",
  "project_id": "example-project",
  "source_id": "src-0123456789abcdef0123456789abcdef",
  "current_path": "src/model.py",
  "path_history": [
    {
      "schema_version": 1,
      "kind": "llmwiki-source-path",
      "path": "src/model.py",
      "first_seen_scan_generation": 1,
      "last_seen_scan_generation": 3
    }
  ],
  "first_seen_scan_generation": 1,
  "last_seen_scan_generation": 3,
  "current_version": 2
}
```

Each content transition is a separate version row:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-source-registry",
  "registry_version": "source-registry-v2",
  "record_type": "version",
  "project_id": "example-project",
  "source_id": "src-0123456789abcdef0123456789abcdef",
  "version": 2,
  "content_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "manifest_path": "src/model.py",
  "observed_scan_generation": 3
}
```

Versions are contiguous positive integers. Observation generations are strictly increasing within one source. Reverting from content A to B and back to A records three versions because the ledger preserves transitions rather than deduplicating historical hashes.

## Synchronization semantics

1. A current Manifest path not yet assigned receives a Core-generated `source_id` and version 1.
2. An unchanged hash preserves the current version. A newer inventory generation extends the path/source observation range but does not add a version.
3. A changed hash under the same current path preserves the `source_id` and appends the next version.
4. A path absent from the current Manifest remains registered with its complete path and version history.
5. D-02 matches only the current known path. Equal hashes at different paths do not imply movement and receive different IDs until D-05 relocation recovery exists.
6. A valid `source-registry-v1` artifact is loaded explicitly and rewritten canonically as v2 on the next sync. Existing IDs are preserved. A legacy source absent from the current Manifest may remain versionless because D-02 has no current Manifest hash to attach safely.
7. Repeating sync against the same Manifest generation and content does not rewrite an identical canonical payload.

## Validation, concurrency, and failure behavior

The v2 loader fails closed on malformed or unsupported Schema versions, unknown fields, bad IDs or hashes, invalid paths, inconsistent generation ranges, duplicate identities or paths, noncontiguous versions, count mismatches, unknown source references, current-version mismatches, and noncanonical row ordering. Nested path records carry and validate their own `schema_version` and `kind`.

Writers retain the D-01 adjacent lock and atomic same-directory replacement protocol. Concurrent writers reload under the exclusive lock, so one changed Manifest generation appends exactly one version. Ordinary loads and under-lock reloads read `sources.jsonl` through a stable, non-redirected descriptor beneath the project machine-state root; the existing-registry comparison and replacement retain that trusted-root binding. A Manifest older than any persisted observation is rejected rather than overwriting newer state. Redirected, unstable, or failed validation leaves an outside target untouched and creates no partial replacement.

## Explicit D-02 non-goals

D-02 does not define or persist Evidence, locators, excerpts, excerpt hashes, source opening, automatic path relocation, Git recovery, source-health state, extraction scheduling, MCP, Hooks, Web behavior, or LLM behavior. Those remain separate roadmap tasks.
