---
name: llmwiki-research
description: Use the local LLM Wiki Research Core through its Codex MCP adapter and portable CLI, including registration, context, coverage, source access, and explicit reconciliation.
---

# LLM Wiki Research

Use this skill when working with a registered external research project through the `llmwiki-research-core` MCP server or the plugin CLI.

## Core setup

1. Use `LLMWIKI_CORE_ROOT` when the plugin is installed outside an ancestor of the Core checkout. It must point to the repository root containing `tools/research_core.py`.
2. Use `LLMWIKI_WORKSPACE_ROOT` to select the assistant workspace. When omitted, the launcher uses the located Core root.
3. Register an external project with the portable CLI when needed:

```bash
python -B scripts/llmwiki.py register /path/to/research-project --json
```

The wrapper delegates to `tools.project` and supplies the workspace root only when the caller did not already provide one.

## Available MCP operations

- `llmwiki_project_context` loads trusted registered-project identity.
- `llmwiki_host_context` returns the bounded host context pack.
- `llmwiki_coverage` generates the deterministic coverage view.
- `llmwiki_source_open` reopens policy-authorized source content using the current Manifest contract.
- `llmwiki_reconcile` performs explicit conservative reconciliation through the current deterministic `classify` boundary.

Treat source-open policy denials as final unless project policy or registration is intentionally changed. Do not bypass Core by reading machine state directly.

## Explicit reconciliation

Run `llmwiki_reconcile` after relevant work whenever synchronization matters. Hooks are optional untrusted hints and are never a correctness dependency. If MCP is unavailable, use:

```bash
python -B scripts/llmwiki.py reconcile PROJECT_ID --json
```

The reconciliation operation uses the existing full deterministic fallback. Do not describe it as selective extraction or curated-knowledge refresh.

## Optional Hook

When `LLMWIKI_PROJECT_ID` is configured, the asynchronous `PostToolUse` Hook may submit successful file-changing paths to the H-04 ledger. The Hook is fail-open and only calls `ResearchCoreService.host_event_submit`; it does not scan, reconcile, update curated Markdown, or advance reconciliation state. Explicit reconciliation remains required when correctness matters or Hooks are disabled, unavailable, or malformed.

## Honest capability boundary

`llmwiki_query` and `llmwiki_plan` are reserved contracts that currently return `capability-unavailable`. Do not represent either as implemented, repeatedly retry them, or claim verified answers or generated research plans from those tools. Extraction, selective knowledge refresh, and Web behavior are also outside this adapter package.
