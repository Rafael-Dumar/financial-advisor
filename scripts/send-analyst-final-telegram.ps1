[CmdletBinding()]
param(
    [string]$ReportPath = "reports\analyst-final-review.md"
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot

$ResolvedReportPath = Join-Path $ProjectRoot $ReportPath
if (-not (Test-Path -LiteralPath $ResolvedReportPath)) {
    throw "analyst_final_review_missing:$ResolvedReportPath"
}

if (-not $env:TELEGRAM_BOT_TOKEN -or -not $env:TELEGRAM_CHAT_ID) {
    Write-Warning 'telegram_skipped_missing_secrets: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to send analyst final summary.'
    exit 0
}

$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe)) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        throw 'python_not_found: create .venv or install Python before sending Telegram.'
    }
    $PythonExe = $PythonCommand.Source
}

& $PythonExe -m advisor.telegram_notify analyst-final --report-path $ResolvedReportPath
if ($LASTEXITCODE -ne 0) {
    throw "telegram_send_failed:$LASTEXITCODE"
}
