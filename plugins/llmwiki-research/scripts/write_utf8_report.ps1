#requires -Version 5.1

[CmdletBinding(DefaultParameterSetName = 'Write')]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$LiteralPath,

    [Parameter(Mandatory = $true, ParameterSetName = 'Write')]
    [AllowEmptyString()]
    [string]$Content,

    [Parameter(ParameterSetName = 'Write')]
    [switch]$Force,

    [Parameter(Mandatory = $true, ParameterSetName = 'Verify')]
    [switch]$VerifyOnly,

    [ValidateSet('zh-CN', 'any')]
    [string]$Language = 'zh-CN'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:KnownReportErrors = @{
    'report-file-has-utf8-bom' = $true
    'report-file-is-not-strict-utf8' = $true
    'report-file-not-found' = $true
    'report-language-zh-cn-missing' = $true
    'report-output-exists' = $true
    'report-output-parent-invalid' = $true
    'report-text-contains-nul' = $true
    'report-text-contains-question-mark-run' = $true
    'report-text-contains-replacement-character' = $true
}

function Throw-ReportError {
    param([string]$Code)
    throw (New-Object System.InvalidOperationException($Code))
}

function Resolve-ReportPath {
    param([string]$Path)

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath(
        (Join-Path -Path (Get-Location).Path -ChildPath $Path)
    )
}

function Test-ReportText {
    param(
        [string]$Text,
        [string]$ExpectedLanguage
    )

    if ($Text.IndexOf([char]0) -ge 0) {
        Throw-ReportError 'report-text-contains-nul'
    }
    if ($Text.IndexOf([char]0xFFFD) -ge 0) {
        Throw-ReportError 'report-text-contains-replacement-character'
    }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '\?{4,}')) {
        Throw-ReportError 'report-text-contains-question-mark-run'
    }

    $cjkPattern = '[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]'
    $cjkCount = [System.Text.RegularExpressions.Regex]::Matches(
        $Text,
        $cjkPattern
    ).Count
    if ($ExpectedLanguage -eq 'zh-CN' -and $cjkCount -eq 0) {
        Throw-ReportError 'report-language-zh-cn-missing'
    }
    return $cjkCount
}

function Read-StrictUtf8Report {
    param(
        [string]$Path,
        [string]$ExpectedLanguage
    )

    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if (
        $bytes.Length -ge 3 -and
        $bytes[0] -eq 0xEF -and
        $bytes[1] -eq 0xBB -and
        $bytes[2] -eq 0xBF
    ) {
        Throw-ReportError 'report-file-has-utf8-bom'
    }

    $decoder = New-Object System.Text.UTF8Encoding($false, $true)
    try {
        $text = $decoder.GetString($bytes)
    }
    catch {
        Throw-ReportError 'report-file-is-not-strict-utf8'
    }

    $cjkCount = Test-ReportText -Text $text -ExpectedLanguage $ExpectedLanguage
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha256.ComputeHash($bytes)
    }
    finally {
        $sha256.Dispose()
    }
    $hash = ([System.BitConverter]::ToString($digest)).Replace(
        '-',
        ''
    ).ToLowerInvariant()

    return [ordered]@{
        schema_version = 1
        kind = 'llmwiki-report-encoding-validation'
        status = 'passed'
        encoding = 'utf-8'
        utf8_bom = $false
        language = $ExpectedLanguage
        bytes = $bytes.Length
        characters = $text.Length
        lines = ([System.Text.RegularExpressions.Regex]::Matches($text, '\n').Count + 1)
        cjk_characters = $cjkCount
        literal_question_marks = [System.Text.RegularExpressions.Regex]::Matches(
            $text,
            '\?'
        ).Count
        replacement_characters = 0
        sha256 = $hash
    }
}

try {
    $target = Resolve-ReportPath -Path $LiteralPath

    if ($VerifyOnly) {
        if (-not [System.IO.File]::Exists($target)) {
            Throw-ReportError 'report-file-not-found'
        }
        $result = Read-StrictUtf8Report -Path $target -ExpectedLanguage $Language
    }
    else {
        if ([System.IO.File]::Exists($target) -and -not $Force) {
            Throw-ReportError 'report-output-exists'
        }

        $parent = [System.IO.Path]::GetDirectoryName($target)
        if ([string]::IsNullOrWhiteSpace($parent)) {
            Throw-ReportError 'report-output-parent-invalid'
        }
        [System.IO.Directory]::CreateDirectory($parent) | Out-Null

        Test-ReportText -Text $Content -ExpectedLanguage $Language | Out-Null
        $encoder = New-Object System.Text.UTF8Encoding($false, $true)
        $bytesToWrite = $encoder.GetBytes($Content)
        $temp = Join-Path -Path $parent -ChildPath (
            '.' + [System.IO.Path]::GetFileName($target) + '.' +
            [System.Guid]::NewGuid().ToString('N') + '.tmp'
        )
        try {
            [System.IO.File]::WriteAllBytes($temp, $bytesToWrite)
            Read-StrictUtf8Report -Path $temp -ExpectedLanguage $Language | Out-Null
            if ([System.IO.File]::Exists($target)) {
                [System.IO.File]::Replace($temp, $target, $null)
            }
            else {
                [System.IO.File]::Move($temp, $target)
            }
        }
        finally {
            if ([System.IO.File]::Exists($temp)) {
                Remove-Item -LiteralPath $temp -Force
            }
        }
        $result = Read-StrictUtf8Report -Path $target -ExpectedLanguage $Language
    }

    Write-Output ($result | ConvertTo-Json -Compress)
}
catch {
    $code = $_.Exception.Message
    if (-not $script:KnownReportErrors.ContainsKey($code)) {
        if ($VerifyOnly) {
            $code = 'report-verification-failed'
        }
        else {
            $code = 'report-write-failed'
        }
    }
    [Console]::Error.WriteLine('error: ' + $code)
    exit 2
}