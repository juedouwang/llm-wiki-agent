# Install LLM Wiki Research Plugin 0.2.1

This procedure installs the self-contained J-05B/J-05C Windows x86-64 release.
Version `0.2.1` corrects human-readable report language and Windows Unicode
integrity without adding a research capability. It does not require an
`llm-wiki-agent` checkout, a system Python, or `LLMWIKI_CORE_ROOT`.

## Prerequisites

- Windows x86-64.
- Codex CLI installed and available as `codex`.
- PowerShell 5.1 or later.

The original self-contained release was validated on **2026-07-19** with Codex
CLI `0.144.2`. The `0.2.1` corrective release was validated end to end on
**2026-07-20** with Codex CLI `0.145.0-alpha.18`. Other Codex versions are not
implied by those validations. Use the normal Codex profile or a short custom
`CODEX_HOME` on Windows. A deliberately very deep custom profile reproduced a
legacy Git `MAX_PATH` checkout failure; arbitrary path depth is not supported or
claimed.

## Recommended: public Git marketplace

Add the public, immutable marketplace tag and install the Plugin:

```powershell
codex plugin marketplace add juedouwang/llmwiki-research-codex-plugin --ref v0.2.1
codex plugin add llmwiki-research@llmwiki-research-release --json
```

This is a real Git-backed installation of the complete self-contained Plugin.
It does not use the unbundled `plugins/llmwiki-research/` source template from a
checkout. OpenAI's current Plugin documentation defines the Plugin layout and
`codex plugin marketplace add`; the locally verified CLI builds `0.144.2` and
`0.145.0-alpha.18` expose installation as `codex plugin add`. Check
`codex plugin --help` if a future CLI changes that verb.

Verify the remote marketplace, Plugin, Skill-backed prompt context, and MCP
registration:

```powershell
codex plugin marketplace list --json
codex plugin list --available --json
codex mcp list --json
```

For a tagged upgrade/reinstall or a full uninstall:

```powershell
# Upgrade/reinstall from the selected immutable tag
codex plugin remove llmwiki-research@llmwiki-research-release --json
codex plugin marketplace remove llmwiki-research-release --json
codex plugin marketplace add juedouwang/llmwiki-research-codex-plugin --ref v0.2.1
codex plugin add llmwiki-research@llmwiki-research-release --json

# Uninstall
codex plugin remove llmwiki-research@llmwiki-research-release --json
codex plugin marketplace remove llmwiki-research-release --json
```

The remote lifecycle was exercised on **2026-07-20**: install `v0.2.1`, rollback
to `v0.2.0`, reinstall `v0.2.1`, uninstall, and remove the marketplace. The final
clean profile contained zero installed Plugins and zero MCP registrations, while
the separate workspace remained intact.

Public repository:
<https://github.com/juedouwang/llmwiki-research-codex-plugin>

Release page:
<https://github.com/juedouwang/llmwiki-research-codex-plugin/releases/tag/v0.2.1>

The published `main` branch and annotated `v0.2.1` tag resolve to release commit
`81a9a589b3b207c04a56beb9ed53696bda2fde0f`. The GitHub Release contains both
the ZIP and its `.sha256` sidecar. A real clean profile fetched this exact remote
tag rather than a local checkout.

## Direct ZIP artifact names

```text
llmwiki-research-0.2.1-windows-x86_64.zip
llmwiki-research-0.2.1-windows-x86_64.zip.sha256
```

The extracted directory contains `FILELIST.txt` and `SHA256SUMS`. The installer
checks the entire internal inventory before staging the package.

## 1. Verify and extract

Place the ZIP and sidecar in the same directory:

```powershell
$archive = '.\llmwiki-research-0.2.1-windows-x86_64.zip'
$expected = (Get-Content "$archive.sha256" -Raw).Split()[0].ToLowerInvariant()
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw 'Release archive SHA-256 mismatch.' }
Expand-Archive -LiteralPath $archive -DestinationPath . -Force
Set-Location .\llmwiki-research-0.2.1-windows-x86_64
```

The published ZIP SHA-256 is:

```text
4e1206d31817aa9a59d45898347deabb1aadc88d7e53c3a5514b0921c44af806
```

It is also available in the release sidecar and GitHub asset digest. Do not
install an archive whose hash differs.

## 2. Direct installation

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

Successful JSON output identifies:

- Plugin ID `llmwiki-research@llmwiki-research-release`;
- Plugin version `0.2.1`;
- the managed staged version directory; and
- the default workspace.

Verify Codex discovery:

```powershell
codex plugin list --json
codex mcp list --json
```

Codex should list the Plugin and the `llmwiki-research-core` MCP server. Start a
new Codex session after installation if an already-running session has cached an
older Plugin catalog.

## 3. Workspace configuration

No configuration is required for the default workspace:

```text
%LOCALAPPDATA%\LLMWiki\workspace
```

The Plugin creates it when needed. To select another workspace, use an absolute
path before starting Codex or a launcher:

```powershell
$env:LLMWIKI_WORKSPACE_ROOT = 'D:\Research\LLMWikiWorkspace'
```

Relative values and locations inside the installed Plugin/Core are rejected.
The workspace is separate from the package and remains after uninstall or
package purge.

Do not set `LLMWIKI_CORE_ROOT` for a release installation. The bundled Core is
resolved from the Plugin tree and takes precedence over that development-only
fallback.

## 4. Use the bundled workflows

The managed package base is:

```text
%LOCALAPPDATA%\LLMWiki\CodexPlugins\llmwiki-research\
```

For version `0.2.1`, the staged Plugin is normally:

```text
%LOCALAPPDATA%\LLMWiki\CodexPlugins\llmwiki-research\0.2.1\marketplace\plugins\llmwiki-research\
```

Example PowerShell usage:

```powershell
$plugin = Join-Path $env:LOCALAPPDATA `
  'LLMWiki\CodexPlugins\llmwiki-research\0.2.1\marketplace\plugins\llmwiki-research'

& "$plugin\scripts\llmwiki.cmd" register C:\path\to\research-project --json
& "$plugin\scripts\llmwiki.cmd" understand C:\path\to\research-project --json
& "$plugin\scripts\llmwiki.cmd" reconcile PROJECT_ID --json
& "$plugin\scripts\research-cockpit.cmd" serve
```

The MCP tools are available directly to Codex through the installed Plugin:
project context, Host Context Pack, coverage, policy-authorized source-open,
explicit reconciliation, I-04 DRAFT initial planning, and the intentional Query
unavailable result.

## 5. Chinese report output and strict UTF-8 verification

Human-readable Markdown reports default to Simplified Chinese unless the user
explicitly requests another language. Keep code, paths, commands, API/MCP names,
Schema fields, enum/error values, Git refs/hashes, project IDs, quoted source
titles, and precision-sensitive technical identifiers in exact English.

Write a report from the installed Plugin root with the PowerShell-native writer:

```powershell
$report = @(
  '# 项目理解报告',
  '',
  '这里写中文分析；`ResearchCoreService` 和 `llmwiki_query` 保留英文标识。'
) -join "`r`n"

powershell -NoProfile -ExecutionPolicy Bypass `
  -File "$plugin\scripts\write_utf8_report.ps1" `
  -LiteralPath C:\path\to\outputs\project-understanding.md `
  -Content $report `
  -Force
```

The writer emits UTF-8 without a BOM, strictly reads the saved bytes back,
requires CJK text by default, rejects invalid UTF-8, `U+FFFD`, NUL, and suspicious
runs of literal `?`, and returns bounded JSON metadata with SHA-256. Re-verify an
existing Chinese report with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File "$plugin\scripts\write_utf8_report.ps1" `
  -LiteralPath C:\path\to\outputs\project-understanding.md `
  -VerifyOnly
```

Use `-Language any` only when the user explicitly requests another language.
Do not pipe a CJK PowerShell here-string directly to native Python in an
unconfigured Windows PowerShell 5.1 session; that path can replace Chinese with
literal ASCII `?` before Python receives it. A successful command or an existing
file is not acceptance unless strict readback passes.

## 6. Upgrade or reinstall

Extract the desired release and run its installer with `-Force`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Force
```

Each version is staged in a separate managed subdirectory, allowing an older
retained version to be selected later. `-Force` is also the supported reinstall
command for a damaged or already staged copy of the same version.

## 7. Rollback

Rollback selects a version that is still present in the managed package base:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\rollback.ps1 -Version 0.2.0
```

Rollback to `0.2.0` changes Codex Plugin/marketplace registration and restores
the previous package, which does not include the J-05C Chinese-report encoding
fix. Rollback does not rewrite, migrate, or delete workspace state. If package
purge removed the requested version, re-extract that release and reinstall it
instead.

## 8. Uninstall

Remove Codex registration but keep staged packages and all workspace state:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

Also purge the managed package base:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1 -PurgePackages
```

`-PurgePackages` proceeds only when the install base has the exact managed
marker and is a safe non-root path. It never removes
`%LOCALAPPDATA%\LLMWiki\workspace`.

## Custom managed package base

All install, rollback, and uninstall scripts accept the same absolute custom
base:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
  -InstallRoot 'D:\ManagedApps\LLMWikiResearch'
```

A custom root must be absolute and safe. If it already exists and is non-empty,
it must contain the exact managed marker created by this installer. Use the same
`-InstallRoot` for later rollback or purge. The scripts intentionally return a
fixed generic failure message instead of echoing unsafe caller input.

## Hook-disabled correctness

The Hook is optional. Explicit reconciliation remains the correctness path even
when `hooks/hooks.json` is absent:

```powershell
& "$plugin\scripts\llmwiki.cmd" reconcile PROJECT_ID --json
```

Do not treat Hook delivery as proof that the project is synchronized.

## Capability boundary

Release `0.2.1` is a report-language/encoding correction and still packages
R3-minus-C-07 only:

- `llmwiki_query` returns `capability-unavailable`;
- `available_after` is `G-04`;
- C-07 is `deferred/not_started`;
- plans are I-04 non-executable DRAFT machine state;
- there is no Verified Query, H-05 selective refresh, mature planning, task
  execution, R4 behavior, or scientific-binary semantic extraction;
- the Core does not modify registered source projects; and
- the release adds no unauthorized external send.
