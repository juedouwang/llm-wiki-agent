# Codex Reference Adapter Package (J-05)

## Status

J-05 was implemented and validated on **2026-07-16**. The reference package is
located at `plugins/llmwiki-research/` and is reference-tested with Codex CLI
`0.144.2`. Compatibility with other host versions is not implied by this
checkpoint; the Core API, schemas, and project storage remain host-independent.

The package exposes only the currently implemented Research Core boundary:

- project registration and other deterministic CLI operations through a thin
  `tools.project` launcher;
- `llmwiki_project_context`;
- `llmwiki_host_context`;
- `llmwiki_coverage`;
- policy-authorized `llmwiki_source_open`;
- conservative full-scan `llmwiki_reconcile`;
- I-04 non-executable DRAFT initial planning through `llmwiki_plan`;
- an explicit `capability-unavailable` result for `llmwiki_query`.

The package now exposes the later I-04 planning slice through the same stable J-05
transport, but it does **not** claim Verified Query, mature I-05 planning, adaptive
extraction, selective knowledge refresh, product Web rendering/editing, or
cross-host continuity. Those remain later roadmap work.

## Package layout

```text
plugins/llmwiki-research/
  .codex-plugin/plugin.json
  .mcp.json
  README.md
  hooks/hooks.json
  scripts/
    _bootstrap.py
    host_event.py
    launch_mcp.py
    llmwiki.py
  skills/llmwiki-research/SKILL.md
```

The manifest advertises the Skill and MCP configuration. Hooks use Codex's
default `hooks/hooks.json` discovery and are deliberately omitted from
`plugin.json`, keeping the manifest within the validated plugin schema.

No package file embeds a developer checkout path. Codex resolves the relative
MCP `cwd` against the installed plugin root, after which the launcher finds the
Core with this closed order:

1. an explicit `LLMWIKI_CORE_ROOT`;
2. bounded ancestors of the plugin package;
3. bounded ancestors of the current working directory.

An explicit but invalid `LLMWIKI_CORE_ROOT` fails closed. A copied plugin that
cannot locate the Core also fails closed with configuration guidance; it never
silently imports an unrelated `tools` package.

`LLMWIKI_WORKSPACE_ROOT` selects the assistant workspace containing
`.llmwiki/projects/` and `wiki/projects/`. When omitted, it defaults to the
validated Core repository root. `scripts/llmwiki.py` injects this workspace into
the existing `tools.project` CLI only when the caller did not already provide
`--workspace-root`.

## Operator workflow

### 1. Identify or register the project

Resolve the plugin root from the installed Skill path, then run the portable
CLI wrapper. For a source-checkout installation, this is:

```powershell
python plugins/llmwiki-research/scripts/llmwiki.py register `
  <project-path> --knowledge-root <knowledge-parent> --json
```

For a copied or cached installation, set the Core and workspace explicitly:

```powershell
$env:LLMWIKI_CORE_ROOT = '<llm-wiki-agent-checkout>'
$env:LLMWIKI_WORKSPACE_ROOT = '<assistant-workspace>'
python <plugin-root>/scripts/llmwiki.py register <project-path> --json
```

Registration is deterministic, source-read-only, and does not imply that a scan
has completed.

### 3. Call the honest MCP boundary

Use `llmwiki_project_context` or the budget-bounded `llmwiki_host_context` for
project handoff, `llmwiki_coverage` for accountable inventory state, and
`llmwiki_source_open` only for an explicit current-source excerpt. Use
`llmwiki_plan` only for strict I-04 DRAFT Goal/task/plan machine state; it does not
authorize execution or directly write curated Markdown. `llmwiki_query` still
returns `capability-unavailable` with `available_after=G-04`.

### 4. Reconcile explicitly after work

Always finish a host work cycle with:

```text
llmwiki_reconcile(project_id, dirty_paths?)
```

or the equivalent portable CLI call:

```powershell
python <plugin-root>/scripts/llmwiki.py reconcile <project_id> --json
```

The H-07 correctness path always performs a fresh
`register -> inventory -> classify` run. Hook events and explicit dirty paths
are untrusted hints; reconciliation remains correct when Hooks are disabled,
untrusted, absent, malformed, delayed, or incomplete.

## Optional Hook boundary

`hooks/hooks.json` registers one asynchronous `PostToolUse` command. The handler
is intentionally best-effort and fail-open:

- it does nothing unless `LLMWIKI_PROJECT_ID` identifies an existing registered
  project and the Core/workspace can be resolved;
- it accepts bounded path hints from recognized path fields in the tool input;
- it loads the existing registration and normalizes accepted paths beneath that
  registered source root, rejecting an outside or ambiguous Hook `cwd`;
- it calls only `ResearchCoreService.host_event_submit`, producing H-04
  `events.jsonl` and the derived dirty-path projection;
- malformed input, missing configuration, unsupported tool payloads, and
  unavailable Core code produce no project mutation and do not block Codex.

The Hook does not register projects, scan source files, read raw source content,
run reconciliation, acknowledge reconciliation checkpoints, write run records,
or mutate curated Markdown. It also does not parse arbitrary shell commands to
guess changed files. Therefore explicit reconciliation is mandatory even when
the Hook is trusted and enabled.

`LLMWIKI_PROJECT_ID` is host-session configuration, not long-term project
identity storage. Project identity remains in the Core's schema-versioned
`project.yaml`.

## Storage and privacy guarantees

The adapter delegates all stateful behavior to the existing Core. Consequently:

- source projects remain read-only;
- reconciliation may update project-scoped machine state under
  `.llmwiki/projects/<project_id>/` but not curated Markdown;
- optional Hook submission updates only the H-04 event ledger/projection;
- no raw source bytes are included in Hook events;
- host-safe MCP responses remain path-free except for the explicit source-open
  excerpt contract;
- the adapter contains no scan, extraction, coverage, reconciliation, or
  knowledge-generation implementation of its own.

## Validation

Focused J-05 validation:

```powershell
python -B -m pytest -q -p no:cacheprovider tests/test_codex_plugin.py
python -m ruff check `
  plugins/llmwiki-research/scripts `
  tests/test_codex_plugin.py
python C:/Users/lyn/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py `
  plugins/llmwiki-research
python C:/Users/lyn/.codex/skills/.system/skill-creator/scripts/quick_validate.py `
  plugins/llmwiki-research/skills/llmwiki-research
```

The automated acceptance covers:

- exact package structure, manifest/MCP/Hook/Skill wiring, no placeholders, no
  checkout-specific path, and clean-profile installation through a temporary
  local marketplace when Codex CLI is available;
- repo-local Core discovery and copied-plugin operation with explicit roots;
- clean-fixture registration through the portable CLI;
- real stdio MCP initialization and the seven-tool catalog;
- real project context, Host Context Pack, coverage, source-open, and
  reconciliation delegation;
- strict non-executable I-04 plan results and an explicit unavailable query result;
- reconciliation with absent or malformed Hooks;
- valid Hook-to-H-04 submission from project-root and nested working
  directories, rejection of outside-root hints, plus malformed, disabled, and
  unavailable Hook fail-open behavior;
- unchanged source and curated knowledge across reconciliation and Hook signals;
- absence of scan, run, source-registry, Evidence, coverage, or reconciliation
  checkpoint side effects from the Hook.

After focused validation, run the full repository suite, Ruff, `py_compile`,
`pip check`, health, and `git diff --check` before checkpointing.

## Rollback and checkpoint

Create the normal history-preserving lightweight checkpoint after validation:

```bash
git tag checkpoint/j-05-codex-plugin <j-05-implementation-commit>
```

Rollback code and documentation with normal Git history:

```bash
git revert <j-05-implementation-commit>
```

Rollback must not delete project registrations, H-04 event ledgers, H-07
reconciliation checkpoints, Manifest generations, or curated project knowledge.
The plugin is an adapter; uninstalling or reverting it must not migrate or copy
long-term project state.
