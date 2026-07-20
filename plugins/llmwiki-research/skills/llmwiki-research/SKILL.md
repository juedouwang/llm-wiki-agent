---
name: llmwiki-research
description: Use the self-contained LLM Wiki Research Codex Plugin for project registration, deterministic understanding, context, coverage, policy-authorized source access, explicit reconciliation, DRAFT initial planning, and the local cockpit.
---

# LLM Wiki Research

Use this Skill for registered research projects exposed by the
`llmwiki-research-core` MCP server or by the bundled Plugin launchers.

## Runtime and workspace

A formal Plugin release contains its own Research Core, Python runtime, and
locked dependencies. Do not ask the user for an `llm-wiki-agent` checkout or
`LLMWIKI_CORE_ROOT`.

`LLMWIKI_WORKSPACE_ROOT` is optional. When absent on Windows, the Plugin creates
and uses `%LOCALAPPDATA%\LLMWiki\workspace`. If the user sets it, require an
absolute path. Do not place the workspace inside the Plugin package or bundled
Core. Project machine state belongs under `.llmwiki/projects/`; curated project
Markdown belongs under `wiki/projects/`.

## Human-readable report language and UTF-8 integrity

Human-readable Markdown reports default to Simplified Chinese (`zh-CN`) unless
the user explicitly requests another language. Write report titles, headings,
explanations, summaries, conclusions, risk notes, and verification narratives in
Chinese. Preserve exact English text for code, paths, shell commands, API and MCP
tool names, Schema field names and enum/error values, Git refs and hashes,
project IDs, quoted source titles, and standard technical names when translating
them would reduce precision.

A successful command or an existing file is not sufficient report validation.
After every report write, strictly decode the saved bytes as UTF-8 and verify that
CJK text survived, no Unicode replacement character is present, and no suspicious
run of literal `?` characters replaced Chinese text. Use the bundled writer at
`<plugin-root>\scripts\write_utf8_report.ps1`; derive `<plugin-root>` by moving
up two directories from this loaded Skill's directory.

```powershell
$report = @'
# 项目理解报告

这里写中文分析；`ResearchCoreService`、`llmwiki_query`、路径和命令保留英文。
'@

powershell -NoProfile -ExecutionPolicy Bypass `
  -File <plugin-root>\scripts\write_utf8_report.ps1 `
  -LiteralPath C:\path\to\outputs\project-understanding.md `
  -Content $report `
  -Force
```

The writer saves strict UTF-8 without a BOM, reads the result back, and returns
bounded JSON metadata including CJK count, literal-question-mark count, and
SHA-256. To validate a report written by another safe Unicode method, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File <plugin-root>\scripts\write_utf8_report.ps1 `
  -LiteralPath C:\path\to\outputs\project-understanding.md `
  -VerifyOnly
```

For a user-requested non-Chinese report, add `-Language any`. Never save CJK by
piping a PowerShell here-string directly to native Python under an unconfigured
Windows PowerShell 5.1 session, for example `@'...中文...'@ | python -`; that
pipeline can irreversibly replace CJK bytes with literal `?`. If the writer or
verification fails, do not claim that the report was completed: correct the
content or output method and validate again.

From the staged Plugin root, use the bundled launcher rather than a system
Python:
```powershell
scripts\llmwiki.cmd register C:\path\to\research-project --json
scripts\llmwiki.cmd understand C:\path\to\research-project --json
scripts\llmwiki.cmd reconcile PROJECT_ID --json
scripts\research-cockpit.cmd serve
```

Registration is deterministic and source-read-only, and does not imply a scan.
`understand` runs the current deterministic R3-minus-C-07 E-08 sequence.

## Available MCP operations

- `llmwiki_project_context` loads trusted registered-project identity.
- `llmwiki_host_context` returns the bounded Host Context Pack.
- `llmwiki_coverage` generates the deterministic coverage view.
- `llmwiki_source_open` reopens only current policy-authorized source content.
- `llmwiki_reconcile` performs the conservative H-07 full deterministic fallback.
- `llmwiki_plan` creates or loads strict I-04 DRAFT planning machine state.
- `llmwiki_query` returns the intentional unavailable contract described below.

Treat source-open policy denial as final unless the user intentionally changes
registration or policy. Do not bypass Core validation by interpreting machine
files as authorization.

## Explicit reconciliation

Call `llmwiki_reconcile` after relevant work whenever synchronization matters.
Hooks are optional untrusted hints and are never a correctness dependency. If
MCP is unavailable, run:

```powershell
scripts\llmwiki.cmd reconcile PROJECT_ID --json
```

Reconciliation uses the existing full deterministic scan through `classify`.
Do not describe it as H-05 selective extraction or curated-knowledge refresh.

## Optional Hook

When `LLMWIKI_PROJECT_ID` names an existing registration, the asynchronous
`PostToolUse` Hook may submit successful file-changing path hints to the H-04
ledger. It is fail-open and calls only
`ResearchCoreService.host_event_submit`. It does not register, scan, extract,
reconcile, acknowledge reconciliation state, or update curated Markdown.
Explicit reconciliation remains required when correctness matters and when Hooks
are disabled, unavailable, malformed, delayed, incomplete, or untrusted.

## Honest capability boundary

`llmwiki_plan` is only I-04 initial planning. Every generated Goal, task, and
plan remains DRAFT and non-executable until separately confirmed; the MCP call
writes machine state only.

`llmwiki_query` must return exactly `capability-unavailable` with
`available_after=G-04`. Do not retry it, emulate Verified Query, or present an
answer as a Core-verified result. C-07 remains `deferred/not_started`.

Do not claim or implement H-05, mature planning, task execution, R4 behavior,
research-binary semantic extraction, source-project writes, or unauthorized
external sends through this Plugin.
