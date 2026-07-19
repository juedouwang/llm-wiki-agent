# LLM Wiki Research Codex Plugin 0.2.0

Release date: **2026-07-19**

Target: **Windows x86-64**
Validated host: **Codex CLI 0.144.2**

J-05B packages the accepted R3-minus-C-07 capabilities as a self-contained
Codex Plugin. Users do not need an `llm-wiki-agent` source checkout, a system
Python, or `LLMWIKI_CORE_ROOT`.

## Included

- Valid `.codex-plugin/plugin.json`, Skill, `.mcp.json`, and default
  `hooks/hooks.json` Plugin components.
- Private CPython 3.13.9 Windows embeddable runtime.
- Exact-version, exact-SHA-256 runtime dependencies.
- Allowlisted deterministic Research Core modules and local cockpit assets.
- Bundled CLI, MCP, and loopback cockpit launchers using Python isolated mode.
- Direct install, `-Force` upgrade/reinstall, staged-version rollback, uninstall,
  and marker-validated package purge scripts.
- Complete `FILELIST.txt`, internal `SHA256SUMS`, `release.json`, and deterministic
  release ZIP with a `.zip.sha256` sidecar.

## Storage defaults

- Managed packages:
  `%LOCALAPPDATA%\LLMWiki\CodexPlugins\llmwiki-research\<version>`
- Optional workspace default:
  `%LOCALAPPDATA%\LLMWiki\workspace`

`LLMWIKI_WORKSPACE_ROOT` may select another absolute workspace. The workspace is
never removed by Plugin uninstall or package purge.

## Verified release behavior

The clean-profile acceptance removes `LLMWIKI_CORE_ROOT` and
`LLMWIKI_WORKSPACE_ROOT`, isolates Codex home and user directories, poisons
ambient Python paths, and checks Plugin/Skill/MCP discovery, MCP initialization
and schemas, project context, Host Context Pack, coverage, source-open,
reconciliation, current I-04 planning, complete bundled `understand`, Query's
unavailable result, explicit reconciliation without Hooks, relocation of the
release and installed Plugin tree, error redaction, rollback, uninstall, and
package purge.

## Honest boundaries

- `llmwiki_query` remains `capability-unavailable` with
  `available_after=G-04`.
- C-07 remains `deferred/not_started`.
- No Verified Query, H-05 selective refresh, mature planning, task execution,
  R4 behavior, research-binary semantic extraction, source-project writes, or
  unauthorized external sends are added.
- This is the J-05B installable adapter release, not a claim that the complete
  research-assistant product has shipped.
