[CmdletBinding()]
param(
    [switch]$DryRun
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

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot

$LogDir = Join-Path $ProjectRoot '.tmp\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$RunStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$LogPath = Join-Path $LogDir "main-report-$RunStamp.log"
$TranscriptStarted = $false

try {
    Start-Transcript -Path $LogPath -Force | Out-Null
    $TranscriptStarted = $true
    Write-Host "project_root=$ProjectRoot"

    $ActivatePath = Join-Path $ProjectRoot '.venv\Scripts\Activate.ps1'
    if (-not (Test-Path -LiteralPath $ActivatePath)) {
        throw 'missing_virtualenv: .venv not found. Create it before scheduling this script.'
    }
    . $ActivatePath

    $PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        throw 'missing_python: .venv\Scripts\python.exe not found.'
    }

    & $PythonPath -m advisor config validate --require-live
    if ($LASTEXITCODE -ne 0) {
        throw "live_validation_failed: advisor config validate --require-live exited with $LASTEXITCODE"
    }

    if ($DryRun) {
        Write-Host 'dry_run_ok: live validation passed; scan skipped.'
        exit 0
    }

    $ReportsDir = Join-Path $ProjectRoot 'reports'
    & $PythonPath -m advisor report main --include-discovery --require-live --output-dir $ReportsDir
    if ($LASTEXITCODE -ne 0) {
        throw "report_failed: advisor report main --include-discovery --require-live exited with $LASTEXITCODE"
    }

    $MarkdownReport = Join-Path $ReportsDir 'advisor-report.md'
    $HtmlReport = Join-Path $ReportsDir 'advisor-report.html'
    if (-not (Test-Path -LiteralPath $MarkdownReport)) {
        throw 'missing_report: reports\advisor-report.md was not generated.'
    }

    $Markdown = Get-Content -LiteralPath $MarkdownReport -Raw -Encoding UTF8
    if (-not $Markdown.Contains('Data mode: `live`')) {
        throw 'latest_report_not_live: expected Data mode: `live` in generated report.'
    }

    $HistoryDir = Join-Path $ReportsDir 'history'
    New-Item -ItemType Directory -Force -Path $HistoryDir | Out-Null
    $BrtDate = Get-BrtDateString

    Copy-Item -LiteralPath $MarkdownReport -Destination (Join-Path $ReportsDir 'latest.md') -Force
    Copy-Item -LiteralPath $MarkdownReport -Destination (Join-Path $HistoryDir "$BrtDate-main.md") -Force

    if (Test-Path -LiteralPath $HtmlReport) {
        Copy-Item -LiteralPath $HtmlReport -Destination (Join-Path $ReportsDir 'latest.html') -Force
        Copy-Item -LiteralPath $HtmlReport -Destination (Join-Path $HistoryDir "$BrtDate-main.html") -Force
    }

    Write-Host 'report_ready=reports\latest.md'
    Write-Host "history_report=reports\history\$BrtDate-main.md"
    Write-Host "log=$LogPath"
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
finally {
    if ($TranscriptStarted) {
        Stop-Transcript | Out-Null
    }
}
