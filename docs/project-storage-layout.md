# Project Registration and Storage Layout (Schema v1)

A-03 established the storage boundary for project-scoped research data. B-01 adds deterministic project registration, B-03 writes the accountable directory ledger, and B-04 adds incremental regular-file fingerprints to that Manifest. Registration still assigns identity and initializes empty storage only; it does not scan project files.

## Storage contract

Default layout:

```text
llm-wiki-agent/
|-- .llmwiki/
|   |-- schema.json
|   `-- projects/
|       `-- <project_id>/
|           |-- project.yaml
|           |-- manifest.jsonl      # B-03 ledger + B-04 fingerprints; never created by registration
|           |-- sources.jsonl       # later task; not created by registration
|           |-- extracted/
|           |-- indexes/
|           `-- runs/
`-- wiki/
    `-- projects/
        `-- <project_id>/
            |-- overview.md         # later task; not created by registration
            |-- sources/
            |-- papers/
            |-- experiments/
            |-- claims/
            `-- plans/
```

| Root | Owner | Intended content | Git policy |
|---|---|---|---|
| `.llmwiki/projects/<project_id>/` | Deterministic tools | Local registration, manifests, source records, extracted evidence, indexes, and run state | Ignore by default because it contains local paths and generated data |
| `<knowledge_projects_root>/<project_id>/` | Agent and user review | Curated Markdown knowledge for reading, editing, and long-term retention | User-controlled; normally version-controlled |

The default `knowledge_projects_root` is `wiki/projects/`. `--knowledge-root` can point it at a personal knowledge base outside this repository. The supplied directory is the parent of all project directories, so registration writes curated storage under `<knowledge-root>/<project_id>/`.

Machine-generated evidence must not be bulk-copied into the human knowledge tree. Later curated claims and summaries will point back to stable source and Evidence identifiers.

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
8. no file inventory, Manifest, extraction, Markdown summary, or LLM call occurs.

## Inventory a registered project

B-03/B-04 inventory consumes the persisted B-01 registration and B-02 scan policy:

```powershell
python tools/project.py inventory <project_id> --json
```

The command writes only `.llmwiki/projects/<project_id>/manifest.jsonl`. Every in-scope regular file is recorded without a format whitelist; excluded files and each pruned-directory boundary remain accountable. B-04 adds scan generation, SHA-256, size, mtime, and conservative fingerprint reuse while preserving source-project zero writes. See [`project-inventory.md`](project-inventory.md) for directory accounting and [`file-fingerprints.md`](file-fingerprints.md) for the current v2 artifact, cache, compatibility, and atomic-failure contract.

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

## B-01/B-02/B-03/B-04 boundary

B-01 registration still does not scan a project. B-02 adds the separate, source-read-only policy layer in `tools/scan_policy.py`:

- optional project-root `.llmwikiignore`;
- explicit include/exclude precedence;
- protected VCS/Core paths and overridable noise defaults;
- content-size, sensitive-path, external-send, and symlink decisions;
- stable explanations and a versioned machine-readable policy snapshot.

See [`scan-policy.md`](scan-policy.md) for the complete policy contract. Neither registration nor policy loading performs file inventory or writes `manifest.jsonl`.

B-03 is the first consumer of both layers. It loads a project only by registered ID, inventories the effective boundary, and writes a versioned basic Manifest outside the source tree. It does not alter B-01 registration data or reinterpret B-02 rule precedence.

B-04 upgrades the artifact to `project-inventory-v2`. It hashes every in-scope ordinary file locally, stores SHA-256/size/mtime plus a conservative local reuse key, increments generation only after a successful atomic replacement, and reads valid B-03 v1 Manifests as generation 0. Hash input is never extracted, persisted as raw content, sent to an LLM, or written back to the source project.

Still isolated in later tasks:

- format/language/research-role classification and final Manifest states (B-05/B-06);
- content extraction, source IDs, Evidence locators, semantic knowledge, retrieval, MCP, web UI, planning, and legacy-data migration.
