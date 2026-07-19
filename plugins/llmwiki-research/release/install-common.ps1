Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:MarketplaceName = "llmwiki-research-release"
$script:PluginName = "llmwiki-research"
$script:ManagedMarkerName = ".llmwiki-codex-plugin-install.json"

function Get-SafeLocalAppData {
    $candidate = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($candidate) -or -not [IO.Path]::IsPathRooted($candidate)) {
        $candidate = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    }
    if ([string]::IsNullOrWhiteSpace($candidate) -or -not [IO.Path]::IsPathRooted($candidate)) {
        throw "A safe per-user installation directory is unavailable."
    }
    return [IO.Path]::GetFullPath($candidate)
}

function Get-InstallBase([string]$Override) {
    if (-not [string]::IsNullOrWhiteSpace($Override)) {
        if (-not [IO.Path]::IsPathRooted($Override)) {
            throw "InstallRoot must be an absolute directory."
        }
        $resolved = [IO.Path]::GetFullPath($Override)
    }
    else {
        $resolved = Join-Path (Get-SafeLocalAppData) "LLMWiki\CodexPlugins\llmwiki-research"
    }
    $trimmed = $resolved.TrimEnd('\')
    $filesystemRoot = [IO.Path]::GetPathRoot($resolved).TrimEnd('\')
    if ($trimmed -eq $filesystemRoot) {
        throw "InstallRoot must not be a filesystem root."
    }
    return $resolved
}

function Assert-SafeChild([string]$Child, [string]$Parent) {
    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [IO.Path]::GetFullPath($Child)
    if (-not $childFull.StartsWith($parentFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "A managed installation path failed validation."
    }
}

function Get-ManagedMarkerPath([string]$Base) {
    return Join-Path $Base $script:ManagedMarkerName
}

function Assert-ManagedInstallBase([string]$Base) {
    $markerPath = Get-ManagedMarkerPath $Base
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw "The managed Plugin installation marker is missing."
    }
    try {
        $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "The managed Plugin installation marker is invalid."
    }
    if ($marker.schema_version -ne 1 -or $marker.plugin_name -ne $script:PluginName) {
        throw "The managed Plugin installation marker is incompatible."
    }
}

function Initialize-ManagedInstallBase([string]$Base) {
    $markerPath = Get-ManagedMarkerPath $Base
    if (Test-Path -LiteralPath $Base) {
        if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
            Assert-ManagedInstallBase $Base
            return
        }
        if (@(Get-ChildItem -LiteralPath $Base -Force).Count -ne 0) {
            throw "InstallRoot is not an empty or managed Plugin directory."
        }
    }
    else {
        New-Item -ItemType Directory -Path $Base -Force | Out-Null
    }
    [ordered]@{
        schema_version = 1
        kind = "llmwiki-codex-plugin-managed-install"
        plugin_name = $script:PluginName
    } | ConvertTo-Json | Set-Content -LiteralPath $markerPath -Encoding UTF8
}

function Resolve-Codex([string]$CommandName) {
    $command = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -eq $command) { throw "Codex CLI is not installed or not on PATH." }
    return $command.Source
}

function Invoke-CodexProcess([string]$Codex, [string[]]$Arguments) {
    $stderrFile = [IO.Path]::GetTempFileName()
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $stdout = & $Codex @Arguments 2> $stderrFile | Out-String
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
        Remove-Item -LiteralPath $stderrFile -Force -ErrorAction SilentlyContinue
    }
    return [ordered]@{ ExitCode = $exitCode; Stdout = $stdout.Trim() }
}

function Invoke-CodexChecked([string]$Codex, [string[]]$Arguments) {
    $result = Invoke-CodexProcess $Codex $Arguments
    if ($result.ExitCode -ne 0) { throw "Codex rejected the Plugin operation." }
    return $result.Stdout
}

function Invoke-CodexOptional([string]$Codex, [string[]]$Arguments) {
    $result = Invoke-CodexProcess $Codex $Arguments
    return $result.ExitCode
}

function Switch-CodexPlugin([string]$Codex, [string]$MarketplaceRoot, [bool]$Replace) {
    if ($Replace) {
        [void](Invoke-CodexOptional $Codex @("plugin", "remove", "$script:PluginName@$script:MarketplaceName", "--json"))
        [void](Invoke-CodexOptional $Codex @("plugin", "marketplace", "remove", $script:MarketplaceName, "--json"))
    }
    [void](Invoke-CodexChecked $Codex @("plugin", "marketplace", "add", $MarketplaceRoot, "--json"))
    return Invoke-CodexChecked $Codex @("plugin", "add", "$script:PluginName@$script:MarketplaceName", "--json")
}
