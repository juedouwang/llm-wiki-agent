# LLM Wiki Research Codex Plugin

This package is the portable Codex reference adapter for the existing local Research Core. It provides a Skill, an MCP stdio configuration, thin CLI launchers, and an optional fail-open `PostToolUse` Hook. Filesystem scanning, coverage, source access, and reconciliation remain implemented by the Core modules rather than by this plugin.

## Install and discovery

The repository ships the plugin package rather than modifying a user's personal
marketplace. For a clean-profile installation, stage this directory as
`plugins/llmwiki-research/` beneath a local marketplace whose
`.agents/plugins/marketplace.json` names `llmwiki-research`, then run:

```bash
codex plugin marketplace add /path/to/local-marketplace
codex plugin add llmwiki-research@MARKETPLACE_NAME
codex plugin list --json
```

`tests/test_codex_plugin.py` builds that marketplace in a temporary directory and
checks real discovery, cache installation, enablement, Skill, MCP, and Hook
files when the Codex CLI is available. The validated reference host is Codex CLI
`0.144.2`; compatibility with every Codex release is not implied.

## Configuration

The launchers locate Core in this order:

1. `LLMWIKI_CORE_ROOT`, pointing to a checkout root that contains `tools/research_core.py`, `tools/research_mcp.py`, and `tools/project.py`.
2. A bounded ancestor search from the plugin scripts and current working directory.

Set `LLMWIKI_WORKSPACE_ROOT` to the assistant workspace containing `.llmwiki/projects/` and `wiki/projects/`. If it is unset, the located Core root is used. The bundled `.mcp.json` starts from `cwd: "."`, which Codex resolves relative to the plugin root, and calls the plugin-relative `scripts/launch_mcp.py` launcher.

Example environment setup:

```bash
export LLMWIKI_CORE_ROOT=/path/to/llm-wiki-agent
export LLMWIKI_WORKSPACE_ROOT=/path/to/assistant-workspace
```

## Portable CLI

`scripts/llmwiki.py` delegates directly to `tools.project`. It injects `--workspace-root` only when the caller has not supplied that option.

```bash
python -B scripts/llmwiki.py register /path/to/research-project --json
python -B scripts/llmwiki.py context PROJECT_ID --json
python -B scripts/llmwiki.py reconcile PROJECT_ID --json
```

Registration creates stable project identity and empty external storage; it is not a completed scan. Explicit `reconcile` remains the correctness path when Hooks are disabled, unavailable, malformed, or untrusted.

## MCP operations

The MCP server delegates to `tools.research_mcp` and exposes the Core catalog:

- `llmwiki_project_context`
- `llmwiki_host_context`
- `llmwiki_coverage`
- `llmwiki_source_open`
- `llmwiki_reconcile`
- `llmwiki_query`
- `llmwiki_plan`

Project context, host context, coverage, source-open, explicit reconciliation, and I-04 initial planning are real Core operations. `llmwiki_plan` writes strict DRAFT Goal/task/project-state/initial-plan machine artifacts only; it does not authorize task execution or directly write curated Markdown. `llmwiki_query` intentionally returns `capability-unavailable` with `available_after=G-04`. The plugin does not add extraction, selective knowledge refresh, or Web behavior.

## Optional H-04 Hook

Set `LLMWIKI_PROJECT_ID` to enable the asynchronous `PostToolUse` Hook for one registered project:

```bash
export LLMWIKI_PROJECT_ID=my-project-id
```

For successful file-changing tools, `scripts/host_event.py` loads the existing registration, extracts path hints from Hook input (including `tool_input.file_path` and `tool_input.path`), resolves them against the registered source root, and calls only `ResearchCoreService.host_event_submit`. A relative path is interpreted from a Hook `cwd` only when that directory is inside the registered source root. An optional `LLMWIKI_PROJECT_ROOT` is accepted only when it exactly matches the registered root. Outside, traversal, protected, malformed, and ambiguous paths are ignored.

The Hook writes untrusted H-04 event hints only. It never starts a scan, invokes reconciliation, updates curated knowledge, or advances a reconciliation checkpoint. It exits successfully and without output when disabled, when input is malformed or irrelevant, or when event submission fails. Run `llmwiki_reconcile` explicitly whenever synchronized state is required.

## Boundary summary

- The source project remains read-only to registration, inventory, coverage, source-open, and reconciliation workflows except for the user or host tool action that originally triggered a Hook.
- Machine state stays under `.llmwiki/projects/`; curated Markdown stays under `wiki/projects/`.
- Hook paths are untrusted hints, not evidence that a source now has particular content.
- Initial planning is available only as strict non-executable I-04 DRAFT machine state; Query remains an explicit `capability-unavailable` contract until G-04.
