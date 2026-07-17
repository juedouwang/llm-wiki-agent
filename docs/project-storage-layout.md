# Project Registration and Storage Layout (Schema v1)

A-03 established the storage boundary for project-scoped research data. B-01 adds deterministic project registration, B-03 writes the accountable directory ledger, B-04 adds incremental regular-file fingerprints, B-05 adds deterministic file classification, B-06 adds two-axis file state, B-07 writes independent reading-priority recommendations, B-08 writes an independent coverage audit, D-01 assigns persistent source identities, D-02 records source versions, and D-03 persists precise Evidence. Registration still assigns identity and initializes empty storage only; it does not scan project files.

## Storage contract

Default layout:

```text
llm-wiki-agent/
|-- .llmwiki/
|   |-- schema.json
|   `-- projects/
|       `-- <project_id>/
|           |-- project.yaml
|           |-- manifest.jsonl      # current B-06 project-inventory-v4 truth
|           |-- sources.jsonl       # D-01 identity + D-02 version/path ledger; not created by registration
|           |-- evidence.jsonl      # D-03 exact source/version/locator/excerpt-hash Evidence
|           |-- extracted/
|           |-- indexes/
|           |   |-- reading-priority.json  # B-07; not created by registration/inventory
|           |   `-- coverage-report.json   # B-08; not created by registration/inventory
|           `-- runs/
`-- wiki/
    `-- projects/
        `-- <project_id>/
            |-- index.md            # later task; not created by registration
            |-- overview.md
            |-- project-map.md
            |-- reproduction.md
            |-- architecture.md
            |-- papers/
            |-- methods/
            |-- datasets/
            |-- experiments/
            |-- results/
            |-- claims/
            |-- open-questions.md
            |-- status.md
            |-- risks.md
            |-- goals.md
            |-- plans/
            |   `-- daily/
            |-- decisions/
            `-- sources/
```

| Root | Owner | Intended content | Git policy |
|---|---|---|---|
| `.llmwiki/projects/<project_id>/` | Deterministic tools | Local registration, manifests, source records, extracted evidence, indexes, and run state | Ignore by default because it contains local paths and generated data |
| `<knowledge_projects_root>/<project_id>/` | Agent and user review | Curated Markdown knowledge for reading, editing, and long-term retention | User-controlled; normally version-controlled |

The default `knowledge_projects_root` is `wiki/projects/`. `--knowledge-root` can point it at a personal knowledge base outside this repository. The supplied directory is the parent of all project directories, so registration writes curated storage under `<knowledge-root>/<project_id>/`.

Machine-generated evidence must not be bulk-copied into the human knowledge tree. Curated claims and summaries point back to stable source and Evidence identifiers. See [`evidence-schema.md`](evidence-schema.md) for the D-03 artifact and validation contract.

F-01A defines the strict Schema v1 frontmatter and canonical project-relative path mapping for this curated tree in [`knowledge-artifact-contract.md`](knowledge-artifact-contract.md). F-01B connects that path contract to `ProjectLayout`: registration and layout initialization create the complete empty canonical directory skeleton, including `plans/daily/`, but still create no knowledge Markdown. Later renderers and controlled writers remain separately authorized Roadmap work.

## Register a project

From the repository root:

```powershell
python tools/project.py register <project-path> --json
```

With an external Markdown knowledge base and optional onboarding data:

```powershell
python tools/project.py register <project-path> `
  --knowledge-root "E:\ResearchKnowledge\projects" `
  --name "My Study" `
  --goal "Reproduce and extend the baseline" `
  --current-stage "environment setup" `
  --important-question "Which result is not yet reproducible?" `
  --deadline 2026-12-31 `
  --daily-hours 2 `
  --json
```

`--workspace-root` selects the Research Core workspace and defaults to this repository. `--project-id` can provide a manual lowercase path-safe ID; otherwise the command generates one. Generated records use `path-sha256-v1`; manual IDs use `manual-v1`.

Registration guarantees:

1. the source path is resolved to a canonical existing directory;
2. the default ID is `<directory-slug>-<canonical-path-sha256-prefix>`;
3. registering the same canonical path and options again returns the existing ID and does not rewrite `project.yaml`;
4. re-registration rejects conflicting ID, storage, name, or onboarding values instead of silently treating registration as an update;
5. machine state and knowledge directories are outside the source project and outside each other;
6. only local, read-only Git commands are used; no fetch, pull, push, or remote request occurs;
7. credentials and HTTP query tokens are removed from a persisted Git remote URL;
8. only the canonical empty curated directory skeleton is initialized; no Markdown page is created;
9. no file inventory, Manifest, extraction, source-content read, research-binary read, or LLM call occurs.

## Inventory a registered project

B-03 through B-06 inventory consumes the persisted B-01 registration and B-02 scan policy:

```powershell
python tools/project.py inventory <project_id> --json
```

The command writes only `.llmwiki/projects/<project_id>/manifest.jsonl`. Every in-scope regular file is recorded without a format whitelist; excluded files and each pruned-directory boundary remain accountable. B-04 adds scan generation, SHA-256, size, mtime, and conservative fingerprint reuse. B-05 classifies every ordinary file by format, language, research role, and auditable reason. B-06 writes the current `project-inventory-v4` artifact and adds versioned `processing_status`, `read_depth`, reason code, and reason fields while preserving source-project zero writes. See [`project-inventory.md`](project-inventory.md), [`file-fingerprints.md`](file-fingerprints.md), [`file-classification.md`](file-classification.md), and [`manifest-file-state.md`](manifest-file-state.md).

## Prioritize the current Manifest

After inventory, generate the independent B-07 recommendation artifact:

```powershell
python tools/project.py prioritize <project_id> --json
```

The command requires an exact current `project-inventory-v4` Manifest. Its sole domain artifact is `.llmwiki/projects/<project_id>/indexes/reading-priority.json`; first use may also create the shared persistent coordination file `indexes/machine-state.lock`, which can remain after release. It ranks ordinary Manifest files, records the exact Manifest generation/file/byte/hash identity, and keeps recommendations separate from `file_state`; it does not create Manifest v5. Every eligible referenced promotion candidate not already deep-read stays in the queue, including budget-deferred entries. An executing consumer must load current-grounded state and then act only on `deep_read_status=selected`; a bare structural parse is not authorization. The operation is deterministic, not goal-aware, source-read-only, and performs no extraction, Evidence/Source creation, run advancement, external send, MCP action, or curated Markdown write. See [`reading-priority.md`](reading-priority.md).

## Synchronize persistent source identities

After inventory, D-01 assigns one Core-generated ID to every current in-scope regular file:

```powershell
python tools/project.py source sync <project_id> --json
```

The command writes only `.llmwiki/projects/<project_id>/sources.jsonl`, preserves prior assignments, records Manifest hash transitions and path history, serializes concurrent writers, and leaves the source project read-only. See [`source-identity.md`](source-identity.md) and [`source-versions.md`](source-versions.md).

## `project.yaml`

`project.yaml` is currently written as formatted JSON, which is a valid YAML subset and lets the project use the existing dependency-free versioned JSON reader. Its Schema v1 shape is:

```json
{
  "schema_version": 1,
  "kind": "llmwiki-project",
  "project_id": "my-study-0123456789ab",
  "identity_strategy": "path-sha256-v1",
  "name": "My Study",
  "root_path": "E:\\Research\\my-study",
  "registered_at": "2026-07-15T10:00:00Z",
  "storage": {
    "workspace_root": "E:\\GitHub\\llm-wiki-agent",
    "machine_root": "E:\\GitHub\\llm-wiki-agent\\.llmwiki\\projects\\my-study-0123456789ab",
    "knowledge_projects_root": "E:\\ResearchKnowledge\\projects",
    "knowledge_root": "E:\\ResearchKnowledge\\projects\\my-study-0123456789ab"
  },
  "git": {
    "available": true,
    "is_repository": true,
    "root": "E:\\Research\\my-study",
    "branch": "main",
    "head_commit": "0123456789abcdef",
    "origin_url": "https://github.com/example/my-study.git"
  },
  "onboarding": {
    "final_goal": "Reproduce and extend the baseline",
    "current_stage": "environment setup",
    "important_question": "Which result is not yet reproducible?",
    "deadline": "2026-12-31",
    "daily_available_hours": 2.0
  }
}
```

## Python API

```python
from pathlib import Path
from tools.project_registry import register_project

result = register_project(
    workspace_root=Path("/path/to/llm-wiki-agent"),
    project_root=Path("/path/to/research-project"),
    knowledge_root=Path("/path/to/personal-knowledge/projects"),
)

print(result.created)
print(result.project_id)
print(result.project_file)
print(result.layout.knowledge_root)
```

Low-level callers can still use `ProjectLayout` and `resolve_project_layout()`. Both preserve the default layout; they also accept an external knowledge-projects root when needed.

## Schema version rules

`tools/project_layout.py` defines `CURRENT_SCHEMA_VERSION = 1`.

- New structured machine-state documents, including `project.yaml` and JSON/JSONL records, include `schema_version`.
- A JSON object without `schema_version` is legacy schema v0 only when compatibility is explicitly enabled.
- Project registration does not accept a legacy or future-version `project.yaml`.
- Reading legacy data never rewrites or migrates it implicitly.
- Data with a future unsupported schema version fails closed.
- Migration must be an explicit, separately tested operation.

The legacy raw Markdown manifest and generated raw-md page frontmatter also include `schema_version: 1`. Their artifact-specific `version` field has a separate purpose.

## Legacy layout compatibility

Before A-03, `tools/raw_md.py` used this project-local layout:

```text
<project-root>/<project-name>-wiki/
|-- raw-md/
|-- state/raw-md-manifest.json
`-- reports/raw-md-report.md
```

That command and layout remain supported. `resolve_project_layout()` follows these rules without moving data:

1. if project-scoped v1 state exists, prefer `.llmwiki/projects/<project_id>/`;
2. otherwise, if the supplied legacy wiki root exists, resolve it as the active legacy layout;
3. when both exist, prefer v1 and retain the legacy manifest as an explicit fallback candidate;
4. never delete, rename, or silently migrate legacy files.

A later migration task may copy validated legacy evidence into the project-scoped layout. Registration itself never invokes the legacy scanner.

## B-01/B-02/B-03/B-04/B-05/B-06/B-07/B-08 boundary

B-01 registration still does not scan a project. B-02 adds the separate, source-read-only policy layer in `tools/scan_policy.py`:

- optional project-root `.llmwikiignore`;
- explicit include/exclude precedence;
- protected VCS/Core paths and overridable noise defaults;
- content-size, sensitive-path, external-send, and symlink decisions;
- stable explanations and a versioned machine-readable policy snapshot.

See [`scan-policy.md`](scan-policy.md) for the complete policy contract. Neither registration nor policy loading performs file inventory or writes `manifest.jsonl`.

B-03 is the first consumer of both layers. It loads a project only by registered ID, inventories the effective boundary, and writes a versioned basic Manifest outside the source tree. It does not alter B-01 registration data or reinterpret B-02 rule precedence.

B-04 upgrades the artifact to `project-inventory-v2`. It hashes every in-scope ordinary file locally, stores SHA-256/size/mtime plus a conservative local reuse key, increments generation only after a successful atomic replacement, and reads valid B-03 v1 Manifests as generation 0. Hash input is never extracted, persisted as raw content, sent to an LLM, or written back to the source project.

B-05 upgrades the artifact to `project-inventory-v3`. It reads a bounded local prefix only when the B-02 file decision grants `local_content_access=allowed`; sensitive or oversized files are classified from filename/path signals without a second raw-content read after the B-04 hash. B-06 upgrades the current writer to `project-inventory-v4`, adding a versioned two-axis state and reconciled state summary to every ordinary file. Valid v1/v2/v3 records remain compatible, permitted samples are ephemeral, and neither raw content nor curated knowledge is written into machine-state records.

B-07 and B-08 are independent post-inventory consumers of current v4 truth. B-07 writes deterministic reading recommendations to `indexes/reading-priority.json`; B-08 writes the Manifest audit to `indexes/coverage-report.json`. Neither rewrites the Manifest, and B-08 neither consumes nor validates B-07 recommendations.

Still isolated in later tasks:

- semantic knowledge, retrieval, full adaptive-read execution, MCP exposure for prioritization, web UI, planning, and legacy-data migration.
