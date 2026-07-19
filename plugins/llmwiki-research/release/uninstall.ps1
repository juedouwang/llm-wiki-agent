[CmdletBinding()]
param(
    [switch]$PurgePackages,
    [string]$InstallRoot,
    [string]$CodexCommand = "codex"
)

try {
    . (Join-Path $PSScriptRoot "install-common.ps1")
    $codex = Resolve-Codex $CodexCommand
    [void](Invoke-CodexOptional $codex @("plugin", "remove", "$script:PluginName@$script:MarketplaceName", "--json"))
    [void](Invoke-CodexOptional $codex @("plugin", "marketplace", "remove", $script:MarketplaceName, "--json"))
    if ($PurgePackages) {
        $base = Get-InstallBase $InstallRoot
        Assert-ManagedInstallBase $base
        $parent = Split-Path -Parent $base
        Assert-SafeChild $base $parent
        if (Test-Path -LiteralPath $base) { Remove-Item -LiteralPath $base -Recurse -Force }
    }
    [ordered]@{
        ok = $true
        plugin_id = "$script:PluginName@$script:MarketplaceName"
        package_cache_retained = (-not [bool]$PurgePackages)
        workspace_retained = $true
    } | ConvertTo-Json
}
catch {
    [Console]::Error.WriteLine("error: LLM Wiki Research Plugin uninstall failed.")
    exit 2
}
