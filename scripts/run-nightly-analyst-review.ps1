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

function Test-ContainsAny {
    param(
        [string]$Text,
        [string[]]$Needles
    )
    foreach ($needle in $Needles) {
        if ($Text.Contains($needle)) {
            return $true
        }
    }
    return $false
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

$InputMarkdown = Get-Content -LiteralPath $InputPath -Raw -Encoding UTF8
$NoEquityCandidates = $InputMarkdown.Contains('No equity candidates for qualitative review')
$BlockedOrDiagnostic = Test-ContainsAny -Text $InputMarkdown -Needles @(
    'blocked_or_diagnostic',
    'report_grade: `diagnostic',
    'report_grade: `blocked',
    'data_mode: `demo',
    'Data mode: `blocked`',
    'main_baseline_missing',
    'close_baseline_missing'
)
$CryptoNeedsReview = [regex]::IsMatch($InputMarkdown, '(?ms)## Crypto review needed.*?-\s+`[^`]+`')
$FinalDecision = if ($BlockedOrDiagnostic) { 'no_trade' } elseif ($NoEquityCandidates) { 'wait' } else { 'watch_only' }
$EquityReview = if ($NoEquityCandidates) {
    'No equity candidates for qualitative review. Public Equity Investing executed: false.'
} else {
    'Equity candidates require manual Public Equity Investing review before any decision. technical_unvalidated is not approval to buy.'
}
$CryptoReview = if ($CryptoNeedsReview) {
    'Crypto review separado de equities. Se CVD, OI, liquidations, premium ou news estiverem not_verified, a conclusao fica research_only ou blocked.'
} else {
    'No crypto candidates for qualitative review.'
}
$ChecklistSessionLine = '* Confirmar que o main rodou em sess' + [char]0x00E3 + 'o v' + [char]0x00E1 + 'lida: market_session = regular, n' + [char]0x00E3 + 'o unknown/closed.'

$Lines = @(
    '# Analyst Final Review',
    '',
    'Public Equity Investing executed: false',
    'Public Equity Investing note: not executed automatically in this environment; review based on nightly-review-input and safety rules.',
    '',
    '## Decisao geral para o proximo pregao',
    '',
    "* $FinalDecision",
    '',
    'Decisao final conservadora. Sem broker; sem ordem automatica; sem compra automatica.',
    '',
    '## Resumo do dia',
    '',
    "- Fonte: reports\nightly-review-input.md",
    "- Base decision-grade: $(if ($BlockedOrDiagnostic) { 'nao' } else { 'limitada' })",
    "- Equity candidates: $(if ($NoEquityCandidates) { 'nenhum' } else { 'revisao manual necessaria' })",
    "- Crypto review needed: $(if ($CryptoNeedsReview) { 'sim, apenas como risco/contexto' } else { 'nao' })",
    "- Limitacoes de dados: tratar not_verified/not_collected como bloqueio operacional.",
    '',
    '## Equity review',
    '',
    $EquityReview,
    '',
    '## Crypto review',
    '',
    $CryptoReview,
    '',
    '## Watchlist para amanha',
    '',
    'Somente watch/research, sem ordem automatica. Nenhum ativo vira tradeable sem novo main decision-grade e validacao de dados criticos.',
    '',
    '## Rejected/blocked',
    '',
    'Ativos com report diagnostic, blocked, demo, not_verified, research_queue ou technical_unvalidated permanecem bloqueados para operacao.',
    '',
    '## Checklist antes de operar',
    '',
    '* Confirmar que o proximo main esta `Data mode: live` e `report_grade` decision-grade.',
    $ChecklistSessionLine,
    '* Verificar noticias, earnings/guidance e catalisadores antes de qualquer decisao manual.',
    '* Nao transformar `technical_unvalidated` em compra.',
    '* Nao transformar research_queue ou watch em tradeable.',
    '* Sem alavancagem quando confidence for low ou missing_data_severity for high/blocking.',
    '',
    '## Telegram summary',
    '',
    "Decisao final: $FinalDecision. Revisao conservadora baseada em nightly-review-input. Sem broker, sem ordem automatica, sem compra automatica. Sem recomendacao de compra/venda imediata."
)

Set-Content -LiteralPath $OutputPath -Encoding UTF8 -Value $Lines
Copy-Item -LiteralPath $OutputPath -Destination $HistoryPath -Force
Write-Host "analyst_final_review=$OutputPath"
Write-Host "analyst_final_review_history=$HistoryPath"

if ($SendTelegram) {
    & (Join-Path $PSScriptRoot 'send-analyst-final-telegram.ps1') -ReportPath 'reports\analyst-final-review.md'
    if ($LASTEXITCODE -ne 0) {
        throw "nightly_telegram_failed:$LASTEXITCODE"
    }
}
