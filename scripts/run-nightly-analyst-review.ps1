[CmdletBinding()]
param(
    [switch]$SendTelegram
)

$ErrorActionPreference = 'Stop'

function Get-BrtDateString {
    try {
        $timeZone = [TimeZoneInfo]::FindSystemTimeZoneById('E. South America Standard Time')
        return [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $timeZone).ToString('yyyy-MM-dd')
    }
    catch {
        return (Get-Date).ToString('yyyy-MM-dd')
    }
}

function Resolve-PythonExe {
    param([string]$ProjectRoot)
    $venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        return $pythonCommand.Source
    }
    throw 'python_not_found: create .venv or install Python before running nightly analyst review.'
}

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot

& (Join-Path $PSScriptRoot 'fetch-latest-github-reports.ps1')
if ($LASTEXITCODE -ne 0) {
    throw "nightly_fetch_failed:$LASTEXITCODE"
}

$InputPath = Join-Path $ProjectRoot 'reports\nightly-review-input.md'
if (-not (Test-Path -LiteralPath $InputPath)) {
    throw "nightly_review_input_missing:$InputPath"
}

$ReportsDir = Join-Path $ProjectRoot 'reports'
$HistoryDir = Join-Path $ProjectRoot 'reports\history'
$OutputPath = Join-Path $ReportsDir 'analyst-final-review.md'
$BrtDate = Get-BrtDateString
$HistoryPath = Join-Path $HistoryDir "$BrtDate-analyst-final-review.md"
New-Item -ItemType Directory -Force -Path $ReportsDir | Out-Null
New-Item -ItemType Directory -Force -Path $HistoryDir | Out-Null

$PythonExe = Resolve-PythonExe -ProjectRoot $ProjectRoot
& $PythonExe -m advisor.analyst_review --input-path $InputPath --output-path $OutputPath --history-path $HistoryPath
if ($LASTEXITCODE -ne 0) {
    throw "analyst_final_review_failed:$LASTEXITCODE"
}

if ($SendTelegram) {
    & (Join-Path $PSScriptRoot 'send-analyst-final-telegram.ps1') -ReportPath 'reports\analyst-final-review.md'
    if ($LASTEXITCODE -ne 0) {
        throw "nightly_telegram_failed:$LASTEXITCODE"
    }
}
