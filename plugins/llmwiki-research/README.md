# LLM Wiki Research Codex Plugin

`llmwiki-research` is the self-contained Windows Codex adapter for the accepted
**R3-minus-C-07** LLM Wiki Research workflows. Release `0.2.1` bundles the
validated Research Core, a private CPython runtime, locked dependencies, a Codex
Skill, MCP configuration, local CLI/cockpit launchers, and an optional fail-open
Hook. A release installation does **not** need an `llm-wiki-agent` checkout and
does **not** need `LLMWIKI_CORE_ROOT`.

The current release target is **Windows x86-64**. The original J-05B package
was validated with Codex CLI `0.144.2`; the J-05C `0.2.1` corrective package was
also validated end to end with Codex CLI `0.145.0-alpha.18`. Compatibility with
every future Codex release is not implied. Codex's current Plugin structure is
documented by OpenAI at <https://developers.openai.com/codex/plugins/build>.

## Install from the public Git marketplace (recommended)

```powershell
codex plugin marketplace add juedouwang/llmwiki-research-codex-plugin --ref v0.2.1
codex plugin add llmwiki-research@llmwiki-research-release --json
```

This clones the public marketplace snapshot and installs the complete bundled
Plugin, not the unbundled source template in this checkout. OpenAI's current
Plugin documentation defines the marketplace-add command and Plugin layout; the
tested Codex CLI builds `0.144.2` and `0.145.0-alpha.18` expose installation as
`plugin add`. Verify the verb on a future CLI with `codex plugin --help`.

Verify discovery:

```powershell
codex plugin list --available --json
codex mcp list --json
```

For a tagged upgrade or reinstall, remove the installed Plugin and marketplace,
then add the desired immutable tag and install again:

```powershell
codex plugin remove llmwiki-research@llmwiki-research-release --json
codex plugin marketplace remove llmwiki-research-release --json
codex plugin marketplace add juedouwang/llmwiki-research-codex-plugin --ref v0.2.1
codex plugin add llmwiki-research@llmwiki-research-release --json
```

Public repository:
<https://github.com/juedouwang/llmwiki-research-codex-plugin>

Release assets:
<https://github.com/juedouwang/llmwiki-research-codex-plugin/releases/tag/v0.2.1>

## Install from the release package

Extract `llmwiki-research-0.2.1-windows-x86_64.zip`, enter the extracted release
directory, and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

The installer verifies the complete internal `SHA256SUMS` inventory before it
stages or registers anything. It then installs the bundled local marketplace
`llmwiki-research-release` and the Plugin ID
`llmwiki-research@llmwiki-research-release` through the Codex CLI.

Verify discovery:

```powershell
codex plugin list --json
codex mcp list --json
```

Reinstall the same version, or upgrade from a newly extracted release package:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Force
```

Rollback to any version still staged under the managed package directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\rollback.ps1 -Version 0.2.0
```

Uninstall the Codex registration while retaining staged packages and all
workspace state:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

Optionally purge only the marker-validated managed package directory. The
workspace is still retained:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1 -PurgePackages
```

See [`../../docs/codex-plugin-installation.md`](../../docs/codex-plugin-installation.md)
for archive verification, exact paths, rollback semantics, and troubleshooting.

## Runtime and storage

The managed package base defaults to:

```text
%LOCALAPPDATA%\LLMWiki\CodexPlugins\llmwiki-research\
```

Version `0.2.1` is staged below that directory and remains movable as a complete
Plugin tree. Codex may also copy the Plugin into its own cache. All launchers
resolve their bundled Core relative to the installed Plugin root, so moving the
complete installed Plugin directory does not require path rewrites.

`LLMWIKI_WORKSPACE_ROOT` is optional. If unset, the launchers create and use:

```text
%LOCALAPPDATA%\LLMWiki\workspace
```

An explicit `LLMWIKI_WORKSPACE_ROOT` must be an absolute path. The bootstrap
rejects a workspace inside the Plugin package or bundled Core. The workspace
holds machine state under `.llmwiki/projects/` and curated project knowledge
under `wiki/projects/`; it is never deleted by uninstall or package purge.

`LLMWIKI_CORE_ROOT` is retained only as a source-development fallback when the
unbundled Plugin directory in this repository is tested. A formal release always
prefers its bundled Core and requires no manual Core-root configuration.

## Local launchers

From the staged Plugin root, use the bundled `.cmd` launchers; no system Python is
required:

```powershell
scripts\llmwiki.cmd register C:\path\to\research-project --json
scripts\llmwiki.cmd understand C:\path\to\research-project --json
scripts\llmwiki.cmd context PROJECT_ID --json
scripts\llmwiki.cmd reconcile PROJECT_ID --json
scripts\research-cockpit.cmd serve
```

## Chinese reports and UTF-8 validation

Human-readable Markdown reports default to Simplified Chinese unless the user
explicitly requests another language. Keep code, paths, commands, API/MCP tool
names, Schema fields and enum/error values, Git refs/hashes, project IDs, quoted
source titles, and precision-sensitive technical names in their exact English
form.

From the installed Plugin root, save a report through the PowerShell-native
writer instead of piping CJK text into native Python:

```powershell
$report = @(
  '# 项目理解报告',
  '',
  '这里写中文分析；`ResearchCoreService` 和 `llmwiki_query` 保留英文。'
) -join "`r`n"

powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\write_utf8_report.ps1 `
  -LiteralPath C:\path\to\outputs\project-understanding.md `
  -Content $report `
  -Force
```

The command writes UTF-8 without a BOM and immediately performs strict readback,
CJK, replacement-character, suspicious-`?`, and SHA-256 checks. Validate an
existing Chinese report with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\write_utf8_report.ps1 `
  -LiteralPath C:\path\to\outputs\project-understanding.md `
  -VerifyOnly
```

Use `-Language any` only when the user explicitly requests a non-Chinese report.
Do not report success if this validation fails.

The Python launchers remain available for source development. The release MCP
entry uses `runtime/python/python.exe -I -B`, so ambient `PYTHONPATH`,
`PYTHONHOME`, and checkout import paths cannot supply the Core or dependencies.

Registration creates stable project identity and empty external storage; it is
not a completed scan. `understand` runs the existing deterministic E-08
R3-minus-C-07 pipeline. Explicit reconciliation remains the correctness path
when Hooks are disabled, unavailable, malformed, delayed, or untrusted.

## MCP operations

The MCP server exposes exactly these seven operations:

- `llmwiki_project_context`
- `llmwiki_host_context`
- `llmwiki_coverage`
- `llmwiki_source_open`
- `llmwiki_reconcile`
- `llmwiki_query`
- `llmwiki_plan`

Project context, Host Context Pack, coverage, policy-authorized source-open,
conservative H-07 reconciliation, and I-04 initial planning are real Core
operations. `llmwiki_plan` writes strict non-executable DRAFT machine state; it
does not authorize execution or directly write curated Markdown.

`llmwiki_query` intentionally returns `capability-unavailable` with
`available_after=G-04`.

## Optional H-04 Hook

Set `LLMWIKI_PROJECT_ID` only when a Codex session should submit successful
file-changing path hints for one already registered project. The default
workspace must contain that registration, or `LLMWIKI_WORKSPACE_ROOT` must point
to the workspace that does.

The asynchronous `PostToolUse` Hook calls only
`ResearchCoreService.host_event_submit`. It records untrusted H-04 hints and
never registers, scans, extracts, reconciles, acknowledges checkpoints, or
updates curated Markdown. Missing or malformed configuration fails open and
must not block Codex. Run explicit reconciliation whenever synchronized state is
required.

## Honest boundary

This release packages only the already accepted R3-minus-C-07 capability set:

- Query is unavailable until G-04.
- C-07 remains `deferred/not_started`.
- There is no Verified Query, H-05 selective refresh, mature planning, task
  execution loop, R4 behavior, or cross-host continuity claim.
- Research binaries are not semantically extracted.
- Registered research-project source files are not modified by the Core.
- Sensitive raw content is not sent externally, and no new external-send path is
  introduced.
