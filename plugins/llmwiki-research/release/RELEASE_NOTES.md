# LLM Wiki Research Codex Plugin 0.2.1

Release date: **2026-07-20**

Target: **Windows x86-64**
Validated hosts: **Codex CLI 0.144.2** (J-05B) and
**Codex CLI 0.145.0-alpha.18** (J-05C clean-profile acceptance)

J-05C is a corrective release for human-readable report language and Windows
Unicode integrity. It keeps the complete self-contained J-05B R3-minus-C-07
runtime and does not add a new research capability.

## Fixed in 0.2.1

- Human-readable Markdown reports now default to Simplified Chinese (`zh-CN`)
  unless the user explicitly requests another language.
- Code, paths, commands, API/MCP names, Schema fields and enum/error values, Git
  refs/hashes, project IDs, quoted source titles, and precision-sensitive
  technical names remain in their exact English form.
- Added `scripts/write_utf8_report.ps1`, which writes UTF-8 without a BOM,
  strictly reads the saved bytes back, requires CJK text by default, rejects
  invalid UTF-8, `U+FFFD`, and suspicious runs of literal `?`, and returns a
  bounded SHA-256 validation record without echoing report content or paths.
- The Skill now forbids the unsafe Windows PowerShell 5.1 pattern
  `@'...中文...'@ | python -` for report persistence. In an unconfigured native
  pipeline, CJK can be irreversibly converted to ASCII `?` before Python sees it.
- The release installer uses the tested Codex CLI `plugin add` verb. The
  installer fails closed if the host does not expose that command; check
  `codex plugin --help` before using a future CLI version.

## Included

- Valid `.codex-plugin/plugin.json`, Skill, `.mcp.json`, and default
  `hooks/hooks.json` Plugin components.
- Private CPython 3.13.9 Windows embeddable runtime.
- Exact-version, exact-SHA-256 runtime dependencies.
- Allowlisted deterministic Research Core modules and local cockpit assets.
- Bundled CLI, MCP, loopback cockpit, and UTF-8 report launch support using
  isolated or PowerShell-native execution paths.
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
release and installed Plugin tree, error redaction, Chinese report round-trip
and strict UTF-8 readback, rollback, uninstall, and package purge.

## Upgrade and rollback

Upgrade the public marketplace from `v0.2.0` to `v0.2.1`, or run the extracted
`0.2.1` package's `install.ps1 -Force`. The prior `0.2.0` tag and release remain
available for rollback, but do not contain the Chinese-report encoding fix.

## Honest boundaries

- `llmwiki_query` remains `capability-unavailable` with
  `available_after=G-04`.
- C-07 remains `deferred/not_started`.
- No Verified Query, H-05 selective refresh, mature planning, task execution,
  R4 behavior, research-binary semantic extraction, source-project writes, or
  unauthorized external sends are added.
- This is the J-05C corrective Plugin release, not a claim that the complete
  research-assistant product has shipped.