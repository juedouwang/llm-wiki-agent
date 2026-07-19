[CmdletBinding()]
param(
    [switch]$Force,
    [string]$InstallRoot,
    [string]$CodexCommand = "codex"
)

try {
    . (Join-Path $PSScriptRoot "install-common.ps1")

    function Test-PackageIntegrity {
        $sumFile = Join-Path $PSScriptRoot "SHA256SUMS"
        if (-not (Test-Path -LiteralPath $sumFile -PathType Leaf)) { throw "Release checksums are missing." }
        $expected = @{}
        foreach ($line in [IO.File]::ReadAllLines($sumFile)) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { throw "Release checksums are malformed." }
            $relative = $Matches[2].Replace('/', '\')
            if ([IO.Path]::IsPathRooted($relative) -or $relative.Contains('..')) { throw "Release checksums are unsafe." }
            $expected[$relative] = $Matches[1]
        }
        foreach ($relative in $expected.Keys) {
            $path = Join-Path $PSScriptRoot $relative
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "A release file is missing." }
            $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
            if ($actual -ne $expected[$relative]) { throw "Release integrity verification failed." }
        }
        $actualFiles = Get-ChildItem -LiteralPath $PSScriptRoot -Recurse -File | ForEach-Object {
            $_.FullName.Substring($PSScriptRoot.Length + 1)
        } | Where-Object { $_ -ne 'SHA256SUMS' }
        if ($actualFiles.Count -ne $expected.Count) { throw "Release file inventory verification failed." }
        foreach ($relative in $actualFiles) {
            if (-not $expected.ContainsKey($relative)) { throw "Release file inventory verification failed." }
        }
    }

    Test-PackageIntegrity
    $release = Get-Content -LiteralPath (Join-Path $PSScriptRoot "release.json") -Raw | ConvertFrom-Json
    if ($release.schema_version -ne 1 -or $release.plugin_name -ne $script:PluginName) { throw "Release metadata is incompatible." }
    $version = [string]$release.version
    if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "Release version is invalid." }
    $base = Get-InstallBase $InstallRoot
    Initialize-ManagedInstallBase $base
    $versionRoot = Join-Path $base $version
    Assert-SafeChild $versionRoot $base
    if ((Test-Path -LiteralPath $versionRoot) -and -not $Force) { throw "This Plugin version is already staged; use -Force to reinstall." }
    if (Test-Path -LiteralPath $versionRoot) {
        Remove-Item -LiteralPath $versionRoot -Recurse -Force
    }
    $marketplaceRoot = Join-Path $versionRoot "marketplace"
    New-Item -ItemType Directory -Path $marketplaceRoot -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot ".agents") -Destination $marketplaceRoot -Recurse -Force
    $pluginSource = Join-Path $PSScriptRoot "plugins"
    $pluginTarget = Join-Path $marketplaceRoot "plugins"
    New-Item -ItemType Directory -Path $pluginTarget -Force | Out-Null
    & robocopy.exe $pluginSource $pluginTarget /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -gt 7) { throw "The Plugin package could not be staged." }
    $codex = Resolve-Codex $CodexCommand
    $result = Switch-CodexPlugin $codex $marketplaceRoot ([bool]$Force)
    [ordered]@{
        ok = $true
        plugin_id = "$script:PluginName@$script:MarketplaceName"
        version = $version
        install_root = $versionRoot
        default_workspace = (Join-Path (Get-SafeLocalAppData) "LLMWiki\workspace")
        codex = ($result | ConvertFrom-Json)
    } | ConvertTo-Json -Depth 8
}
catch {
    [Console]::Error.WriteLine("error: LLM Wiki Research Plugin installation failed.")
    exit 2
}
