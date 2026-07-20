# Codex Reference Adapter and Installable Release (J-05/J-05B/J-05C)

## Status

J-05 established the reference Codex adapter on **2026-07-16**. J-05B packaged
the already accepted R3-minus-C-07 capability set as the self-contained
**LLM Wiki Research Codex Plugin 0.2.0** for **Windows x86-64**. J-05C publishes
corrective release **0.2.1** on **2026-07-20**: human-readable reports default to
Simplified Chinese and are saved through a strict UTF-8 readback gate. J-05C adds
no Research Core capability. J-05B was validated with Codex CLI `0.144.2`; J-05C
was also validated end to end with Codex CLI `0.145.0-alpha.18`.

The installable release does not require the user to retain an
`llm-wiki-agent` source checkout and does not require
`LLMWIKI_CORE_ROOT`. It bundles a private CPython 3.13.9 runtime, exact-hash
Windows dependencies, the deterministic Core modules needed by the current
workflows, the Codex Skill and MCP configuration, local CLI and cockpit
launchers, and the optional H-04 hint Hook.

This is a packaging and distribution checkpoint, not a claim that the complete
research-assistant product is released. `llmwiki_query` remains unavailable,
C-07 remains deferred, and R4 has not started.

## Codex Plugin contract

The source template is `plugins/llmwiki-research/`:

```text
plugins/llmwiki-research/
  .codex-plugin/plugin.json
  .mcp.json
  README.md
  hooks/hooks.json
  release/
    core-files.txt
    python-runtime-win-amd64.json
    requirements-win-amd64.txt
    install-common.ps1
    install.ps1
    rollback.ps1
    uninstall.ps1
    RELEASE_NOTES.md
  scripts/
    _bootstrap.py
    host_event.py
    launch_mcp.py
    launch-mcp.cmd
    llmwiki.py
    llmwiki.cmd
    research_cockpit.py
    research-cockpit.cmd
    write_utf8_report.ps1
  skills/llmwiki-research/SKILL.md
```

The formal package adds:

```text
llmwiki-research-0.2.1-windows-x86_64/
  .agents/plugins/marketplace.json
  FILELIST.txt
  SHA256SUMS
  release.json
  install.ps1
  rollback.ps1
  uninstall.ps1
  plugins/llmwiki-research/
    runtime/python/
    runtime/python/Lib/site-packages/
    runtime/core/
```

OpenAI's current Plugin documentation defines `.codex-plugin/plugin.json` as
the required manifest and recognizes `skills/`, `.mcp.json`, and
`hooks/hooks.json` as default component locations:
<https://developers.openai.com/codex/plugins/build>. The manifest uses explicit
`skills` and `mcpServers` entries. The companion `.mcp.json` uses the documented
direct top-level server map; the same documentation also permits an
`mcp_servers` wrapper. The Hook stays in its default `hooks/hooks.json` location
and uses `${PLUGIN_ROOT}` so its command resolves from the installed Plugin
rather than from a checkout or shell working directory.

The MCP command and working directory are relative to the installed Plugin root:

```json
{
  "llmwiki-research-core": {
    "command": "runtime/python/python.exe",
    "args": ["-I", "-B", "scripts/launch_mcp.py"],
    "cwd": "."
  }
}
```

The private interpreter's isolated mode prevents ambient `PYTHONPATH`,
`PYTHONHOME`, site packages, or checkout import paths from satisfying release
imports.

## Deterministic release construction

Build on Windows x86-64 with Python 3.13:

```powershell
python -B tools/build_codex_plugin_release.py --output-dir dist --json
```

For a locked offline rebuild after the official Python archive and all wheels
are cached:

```powershell
python -B tools/build_codex_plugin_release.py `
  --output-dir dist `
  --cache-dir <verified-cache-directory> `
  --offline --json
```

The builder:

1. verifies the locked CPython 3.13.9 embeddable archive SHA-256;
2. verifies every pinned Windows CPython 3.13 wheel against the hashes in
   `requirements-win-amd64.txt`;
3. installs into private `runtime/python/Lib/site-packages` and performs an
   isolated import smoke test;
4. copies only the Core allowlist in `core-files.txt`, cockpit assets, and the
   repository license;
5. writes `runtime/core/BUILD.json`, including the exact source revision and
   the `deferred/not_started` C-07 state;
6. writes the local marketplace, `release.json`, a complete `FILELIST.txt`, and
   internal `SHA256SUMS`;
7. rejects source-checkout absolute paths in release text files; and
8. creates a deterministic ZIP plus a `.zip.sha256` sidecar.

The release target is intentionally Windows x86-64 only. Other operating systems
or architectures require a separately locked and tested release unit; they are
not inferred from this package.

## Public Git marketplace publication

The complete built Plugin is published separately from the unbundled source
adapter at:

<https://github.com/juedouwang/llmwiki-research-codex-plugin>

Public commit `81a9a589b3b207c04a56beb9ed53696bda2fde0f` is the `main`
head and the commit selected by annotated tag `v0.2.1`. Do not move or rewrite
that published tag.

Install the immutable `v0.2.1` marketplace snapshot with the current validated
CLI commands:

```powershell
codex plugin marketplace add juedouwang/llmwiki-research-codex-plugin --ref v0.2.1
codex plugin add llmwiki-research@llmwiki-research-release --json
```

OpenAI's current Plugin documentation defines `codex plugin marketplace add` and
the manifest/component shape. The tested CLI builds `0.144.2` and
`0.145.0-alpha.18` expose the installation verb as `codex plugin add`; the real
clean-profile gate uses that command rather than inferring installation from
files.

The corresponding GitHub Release publishes the deterministic ZIP, sidecar,
release notes, `FILELIST.txt`, internal `SHA256SUMS`, and `release.json`:

<https://github.com/juedouwang/llmwiki-research-codex-plugin/releases/tag/v0.2.1>

The ZIP SHA-256 is `4e1206d31817aa9a59d45898347deabb1aadc88d7e53c3a5514b0921c44af806`. A remote Git-backed clean-profile
acceptance on **2026-07-20** confirms marketplace and Skill discovery, MCP
registration and handshake, all seven tool Schemas, project context, Host Context
Pack, coverage, policy-authorized source-open, explicit full-scan reconciliation
with absent Hook hints, I-04 DRAFT planning, exact Query unavailability, default
workspace selection, source immutability, error redaction, and strict Chinese
UTF-8 report write/readback without `LLMWIKI_CORE_ROOT` or a source checkout. It
then rolled back from `0.2.1` to `0.2.0`, reinstalled `0.2.1`, uninstalled the
Plugin, removed the marketplace, and verified zero residual Plugin/MCP
registrations while retaining workspace state.

A deliberately much deeper custom `CODEX_HOME` failed during Git checkout under
legacy Windows `MAX_PATH`. The default Codex profile and a short independent
profile pass; this release does not claim support for arbitrary custom path
depth.

## Installation, upgrade, rollback, and removal

From the extracted release root:

```powershell
# Direct installation
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1

# Upgrade or deterministic reinstall of the extracted version
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Force

# Select a previously staged version
powershell -NoProfile -ExecutionPolicy Bypass -File .\rollback.ps1 -Version 0.2.0

# Remove Plugin and marketplace registration, retaining packages/workspace
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1

# Also remove only the marker-validated managed package directory
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1 -PurgePackages
```

The installer delegates registration to the real Codex CLI commands
`codex plugin marketplace add` and `codex plugin add`. Upgrade/reinstall first
removes the old Plugin and marketplace registration, then installs from the
selected staged package. Uninstall never deletes the workspace. Purge refuses
an unmarked or unsafe target; a non-empty custom install root without the exact
managed marker also fails closed.

See [`codex-plugin-installation.md`](codex-plugin-installation.md) for the
operator-facing procedure.

## Bootstrap and storage boundaries

A release launcher resolves Core in this order:

1. the validated bundled `runtime/core` next to the launcher;
2. only when that bundle is absent, an explicit `LLMWIKI_CORE_ROOT` for source
   development;
3. only when that bundle is absent, a bounded ancestor search used by repository
   tests and development.

Therefore an installed package neither needs nor trusts an ambient Core checkout.
If a bundled Core exists, even a stale `LLMWIKI_CORE_ROOT` cannot replace it.

`LLMWIKI_WORKSPACE_ROOT` remains optional. On Windows, the safe default is:

```text
%LOCALAPPDATA%\LLMWiki\workspace
```

The launcher creates the default directory when needed. An explicit workspace
must be absolute and must not resolve inside the Plugin root or bundled Core.
Machine state remains under `.llmwiki/projects/<project_id>/`, curated Markdown
under `wiki/projects/<project_id>/`, and neither uninstall nor package purge
removes that workspace.

The managed package base is:

```text
%LOCALAPPDATA%\LLMWiki\CodexPlugins\llmwiki-research\
```

Each version is staged separately. A marker file
`.llmwiki-codex-plugin-install.json` binds destructive package-purge operations
to this managed directory. The complete Plugin tree can be moved before or
after Codex installation because runtime and Core paths are Plugin-relative.

## Chinese report language and encoding boundary

The installed Skill directs human-readable Markdown reports to Simplified
Chinese unless the user explicitly requests another language. Code, paths,
commands, API/MCP names, Schema fields, enum/error values, Git refs/hashes,
project IDs, quoted source titles, and precision-sensitive technical terms stay
in exact English.

`scripts/write_utf8_report.ps1` is part of both the source template and built
package. It uses Windows PowerShell 5.1-compatible .NET APIs to write UTF-8
without a BOM, strictly reads the final bytes back, requires CJK text by default,
rejects invalid UTF-8, `U+FFFD`, NUL, and suspicious runs of literal `?`, and
returns only bounded metadata plus SHA-256. It does not echo report content or
absolute paths on failure. `-Language any` is an explicit override for a
user-requested non-Chinese report.

The Skill explicitly forbids the unsafe pattern of piping a CJK PowerShell
here-string directly to native Python in an unconfigured Windows PowerShell 5.1
session. That native pipeline can replace CJK with ASCII `?` before Python sees
the text. File existence or a zero exit status is not report acceptance; strict
readback must pass after every write.

## Operator workflow

### 1. Register or understand a project

From the staged Plugin root:

```powershell
scripts\llmwiki.cmd register C:\path\to\project --json
scripts\llmwiki.cmd understand C:\path\to\project --json
```

Registration is deterministic, source-read-only, and creates only stable
identity plus empty external storage. It must not be described as a scan.
`understand` runs the existing E-08 sequence:

```text
register -> inventory -> classify -> extract -> adaptive-read -> synthesize ->
evidence -> status -> plan -> index -> web-render
```

C-07 scientific-binary metadata extraction remains deferred; binary canaries
stay policy/classification limited rather than being semantically interpreted.

### 2. Use the honest MCP boundary

The server exposes seven schema-validated tools:

- `llmwiki_project_context`
- `llmwiki_host_context`
- `llmwiki_coverage`
- `llmwiki_source_open`
- `llmwiki_reconcile`
- `llmwiki_plan`
- `llmwiki_query`

Project context, Host Context Pack, coverage, policy-authorized source-open,
explicit reconciliation, and I-04 DRAFT initial planning delegate to the bundled
Core. `llmwiki_plan` produces non-executable machine state only. The Query tool
returns exactly `capability-unavailable` with `available_after=G-04`.

### 3. Reconcile explicitly

After relevant host work, call:

```text
llmwiki_reconcile(project_id, dirty_paths?)
```

or:

```powershell
scripts\llmwiki.cmd reconcile PROJECT_ID --json
```

H-07 always has its complete deterministic scan-through-`classify` fallback.
Hook events and caller dirty paths are untrusted hints. Explicit reconciliation
continues to work when Hooks are disabled, deleted, missing, delayed, malformed,
or inconsistent.

### 4. Open the local cockpit

```powershell
scripts\research-cockpit.cmd serve
```

The existing J-03/J-04 loopback cockpit remains read-only by default. Editing is
limited to its four fixed renderer-owned mixed regions when explicitly enabled;
this release adds no arbitrary editor or R4 behavior.

## Optional Hook boundary

`hooks/hooks.json` registers one asynchronous `PostToolUse` command against the
bundled interpreter. It does nothing unless `LLMWIKI_PROJECT_ID` identifies an
existing project in the selected workspace. It accepts only bounded recognized
path fields, normalizes accepted paths beneath the registered source root, and
calls only `ResearchCoreService.host_event_submit`.

The Hook does not register, scan, read raw source content, extract, reconcile,
acknowledge reconciliation checkpoints, write run records, or modify curated
Markdown. Malformed input, missing configuration, unsupported payloads, and
unavailable state fail open without blocking Codex. Explicit reconciliation is
mandatory whenever correctness matters.

## Privacy and error guarantees

- The Research Core does not modify registered source-project files.
- Sensitive/ignored source-open requests are denied by current Manifest policy.
- The release adds no LLM call or external raw-content send.
- Installer, launcher, Hook, and MCP errors are bounded and do not echo secrets,
  caller-controlled sensitive values, release-checkout paths, or clean-profile
  absolute paths.
- MCP host DTOs remain path-free except for the explicit bounded source-open
  response contract.
- Package and workspace directories are separate; uninstall cannot silently
  destroy research state.

## Validation

Fast source-template validation:

```powershell
python -B -m pytest -q -p no:cacheprovider `
  tests/test_codex_plugin.py tests/test_codex_plugin_release.py
python -m ruff check `
  plugins/llmwiki-research/scripts `
  tools/build_codex_plugin_release.py `
  tests/test_codex_plugin.py tests/test_codex_plugin_release.py
python C:/Users/lyn/.codex/skills/.system/skill-creator/scripts/quick_validate.py `
  plugins/llmwiki-research/skills/llmwiki-research
```

The locally bundled `plugin-creator` validator currently expects the historical
camel-case `.mcp.json` `mcpServers` wrapper and therefore rejects the current
official direct-map schema. That is a validator-version skew, not a reason to
ship the obsolete shape. The repository contract tests and a real clean-profile
Codex CLI `0.144.2` install/discovery/startup acceptance validate the documented
current shape; the independent Skill validator passes.

The real release acceptance is opt-in because it builds the full runtime and
uses an installed Codex CLI:

```powershell
$env:LLMWIKI_RUN_RELEASE_TESTS = '1'
$env:LLMWIKI_RELEASE_CACHE_DIR = '<verified-release-cache>'
$env:LLMWIKI_RELEASE_OFFLINE = '1'
$env:LLMWIKI_RELEASE_TEST_ROOT = Join-Path $env:USERPROFILE 'l'
$codexExe = Get-Command codex.exe -ErrorAction SilentlyContinue
if ($codexExe) {
  $env:LLMWIKI_CODEX_EXE = $codexExe.Source
} else {
  $env:LLMWIKI_CODEX_EXE = Get-ChildItem `
    -LiteralPath "$env:LOCALAPPDATA\OpenAI\Codex\bin" `
    -Recurse -Filter codex.exe -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName
}
if (-not $env:LLMWIKI_CODEX_EXE) { throw 'Codex executable not found.' }
python -B -m pytest -q -p no:cacheprovider tests/test_codex_plugin_release.py
```

The acceptance uses independent `CODEX_HOME`, `LOCALAPPDATA`, home, temp,
PowerShell module cache, workspace, and source-fixture directories. It removes
`LLMWIKI_CORE_ROOT` and `LLMWIKI_WORKSPACE_ROOT`, poisons ambient Python paths,
and verifies:

- direct installation and `-Force` reinstall;
- Codex Plugin, Skill, MCP, and Hook discovery;
- MCP initialization, protocol handshake, seven-tool catalog, and every declared
  input/output JSON Schema;
- project context, Host Context Pack, coverage, source-open, reconciliation,
  current I-04 plan, and exact unavailable Query behavior;
- explicit reconciliation with the Hook removed;
- complete bundled `understand` R3-minus-C-07 operation;
- startup after moving the release and after moving the installed Plugin tree;
- source bytes, hash, timestamps, mode, and curated knowledge preservation;
- no binary-canary leakage or semantic extraction;
- error redaction;
- Chinese report write and strict UTF-8 readback from both installed and relocated
  Plugin paths; and
- rollback, uninstall, and marker-validated package purge.

Recorded J-05B validation on **2026-07-19** produced **8 passed, 1 skipped**
for the focused source-template suite, **149 passed, 5 skipped** for related
Core/adapter dependencies, **827 passed, 32 skipped** for the full repository,
and **2 passed** for the enabled clean-profile release acceptance with Codex CLI
`0.144.2`. The clean acceptance includes direct Skill discovery through
`codex debug prompt-input`. Changed-file and bundled-Core Ruff checks, Ruff
format checks, isolated `py_compile`, and `git diff --check` passed. The final
archive is rebuilt from the committed release head and its SHA-256 is recorded
in the adjacent sidecar and release report rather than hard-coded into source
that would alter the archive.

Recorded J-05C validation on **2026-07-20** produced **16 passed, 1 skipped** for
the report/Plugin/release focused suite, **158 passed, 4 skipped** for the final
related Core/adapter suite, **835 passed, 32 skipped** for the full repository,
and **3 passed** for the enabled clean-profile release acceptance with Codex CLI
`0.145.0-alpha.18`. Two independent release builds from commit `427fc83` produced
the same 2,129-file ZIP and SHA-256 after unused staging-sensitive `pip` console
launchers were removed. The clean run installs without a checkout or
`LLMWIKI_CORE_ROOT`, discovers the Skill through Codex, handshakes and exercises
MCP, writes and re-verifies Chinese UTF-8 reports before and after Plugin
relocation, removes Hooks for explicit reconciliation, and completes rollback,
uninstall, and purge. J-05C changed-file Ruff and Ruff-format checks, isolated
`py_compile`, and `git diff --check` pass. The repository-wide Ruff baseline still
contains unrelated legacy-tool findings and is not misreported as a J-05C pass.

## Honest stop boundary

J-05B/J-05C do not implement or claim:

- Verified Query or any alternative query synthesis;
- G-04 (Query remains `capability-unavailable` with `available_after=G-04`);
- C-07 (`deferred/not_started`);
- H-05 selective extraction or selective knowledge refresh;
- mature planning, task authorization, or task execution;
- R4 behavior;
- scientific-binary semantic extraction;
- source-project mutation; or
- any new unauthorized external send.

After J-05C acceptance and publication, work stops at this corrective release boundary.
