[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$InstallRoot,
    [string]$CodexCommand = "codex"
)

try {
    . (Join-Path $PSScriptRoot "install-common.ps1")
    if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Rollback version is invalid." }
    $base = Get-InstallBase $InstallRoot
    Assert-ManagedInstallBase $base
    $versionRoot = Join-Path $base $Version
    Assert-SafeChild $versionRoot $base
    $marketplaceRoot = Join-Path $versionRoot "marketplace"
    if (-not (Test-Path -LiteralPath (Join-Path $marketplaceRoot ".agents\plugins\marketplace.json") -PathType Leaf)) {
        throw "The requested rollback version is not staged."
    }
    $codex = Resolve-Codex $CodexCommand
    $result = Switch-CodexPlugin $codex $marketplaceRoot $true
    [ordered]@{
        ok = $true
        plugin_id = "$script:PluginName@$script:MarketplaceName"
        version = $Version
        codex = ($result | ConvertFrom-Json)
    } | ConvertTo-Json -Depth 8
}
catch {
    [Console]::Error.WriteLine("error: LLM Wiki Research Plugin rollback failed.")
    exit 2
}
