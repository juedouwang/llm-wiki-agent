# Research Core Service Facade (G-01)

- Status: implemented for the R2 G-01 scope
- Public module: `tools.research_core`
- Public class: `ResearchCoreService`
- Validation: `tests/test_research_core_service.py`
- Checkpoint: `checkpoint/g-01-core-service`

## Purpose

`ResearchCoreService` is the host-independent Python boundary for deterministic
Research Core capabilities. The project CLI now delegates the G-01 operations
to this facade instead of calling storage modules directly. Future MCP, Codex,
Claude Code, Hook, Web, or other adapters must call the same service boundary;
they must not duplicate the underlying filesystem workflows.

The facade does not parse command-line arguments, print terminal output, start a
server, call an LLM, open a browser, or import a host adapter. It returns the
existing typed result objects and lets the existing Core exceptions propagate.
This keeps policy, schema validation, source-read-only behavior, and fail-closed
version checks in the already accepted R1 implementations.

## Construction

```python
from tools.research_core import ResearchCoreService

core = ResearchCoreService(workspace_root=r"E:\ResearchCore")
```

Construction only normalizes the workspace path. It does not register or scan a
project and does not create state in the research source tree.

## G-01 operations

| Service method | Existing deterministic implementation | Return type |
|---|---|---|
| `register(...)` | `tools.project_registry.register_project` | `ProjectRegistrationResult` |
| `scan(...)` | `tools.project_inventory.inventory_project` with a `ScanPolicyConfig` | `ProjectInventoryResult` |
| `coverage(project_id)` | `tools.coverage_report.generate_coverage_report` | `CoverageReportResult` |
| `source_open(...)` | `tools.source_access.open_source` or `open_evidence` | `SourceOpenResult` |

### Register

```python
registration = core.register(
    r"E:\Projects\study",
    knowledge_root=r"E:\Knowledge\projects",
    final_goal="Reproduce the reported result",
)
```

Registration remains deterministic and source-read-only. It creates project
identity and external storage only; it is not a completed scan.

### Scan

```python
from tools.scan_policy import ScanPolicyConfig

policy = ScanPolicyConfig(
    include_patterns=("results/**",),
    exclude_patterns=("scratch/**",),
    sensitive_patterns=("private/**",),
    external_exclude_patterns=("private/**",),
    external_send_mode="local-only",
)
inventory = core.scan(registration.project_id, policy_config=policy)
```

`scan` is the service name for the current `inventory` command. A complete
`ScanPolicyConfig` is the canonical host-facing input, so adapters retain every
B-02 control: include/exclude and sensitive patterns, external-send rules, size
limits, symlink behavior, and case sensitivity. The convenience
`include_patterns`, `exclude_patterns`, and `follow_symlinks` arguments remain for
the existing CLI mapping and cannot be combined with `policy_config`. The method
then runs the accepted B-03 through B-06 inventory chain; policy and
source-read-only constraints remain authoritative in `tools.scan_policy` and
`tools.project_inventory`.

### Coverage

```python
coverage = core.coverage(registration.project_id)
```

Coverage is generated from the current Manifest and remains reconcilable by
count, byte size, role, status, read depth, and reason.

### Source open

```python
from tools.extraction_schema import LineRangeLocator

opened = core.source_open(
    registration.project_id,
    "src-...",
    locator=LineRangeLocator(10, 20),
    expected_content_hash="...",
)
```

A `src-*` target requires a typed Locator or locator dictionary. The CLI parses
strict locator JSON only for a confirmed `src-*` target; malformed JSON therefore
does not change the established Evidence-override or unknown-target error
priority. An `evd-*` target reuses its persisted locator and hashes and rejects
caller overrides. Unknown target types and invalid locator combinations fail
closed with the existing `SourceAccessError` hierarchy and stable `reason_code`
values.

## CLI equivalence

These CLI entry points instantiate `ResearchCoreService` with
`--workspace-root`, call the corresponding method, and serialize the returned
object with the same `as_dict()` method used by direct Python callers:

```text
python tools/project.py register ... --json      -> core.register(...)
python tools/project.py inventory ... --json     -> core.scan(...)
python tools/project.py coverage ... --json      -> core.coverage(...)
python tools/project.py source open ... --json   -> core.source_open(...)
```

For successful JSON commands, the CLI payload is exactly:

```python
{"ok": True, **service_result.as_dict()}
```

The G-01 tests compare all four CLI payloads with their direct service results
and verify the exact method arguments. Existing command names and JSON fields
remain compatible.

## Safety and ownership boundaries

- Machine state stays under `.llmwiki/projects/<project_id>/` in the configured
  workspace.
- Curated knowledge stays under the configured
  `wiki/projects/<project_id>/`-equivalent knowledge root.
- The registered research source project remains read-only.
- The facade adds no new persisted record or schema and does not rewrite legacy
  data.
- All schema/version checks continue through `tools.project_layout` and the
  accepted R1 modules; unsupported future versions fail closed.
- No source content is sent externally by the G-01 service path.

## Validation

Run the focused G-01 regression:

```powershell
python -B -m unittest tests.test_research_core_service
```

The test covers:

1. direct register, complete-policy scan, coverage, Source open, and Evidence open;
2. exact real script/module CLI versus direct structured-result equivalence;
3. exact adapter delegation and serialization arguments;
4. source-open failure priority and stable reason codes;
5. source-project hash/size/mtime/mode preservation and no in-project state;
6. the facade dependency closure contains no CLI or host adapter;
7. the deterministic chain performs no LLM, network, URL, or Web UI call.

## Explicit non-goals

G-01 does not implement MCP, Host Context Packs, orchestration, Hooks,
reconciliation, one-click `project understand`, Web rendering, Verified Query,
or planning. Those remain separate atomic roadmap tasks beginning with G-07.
