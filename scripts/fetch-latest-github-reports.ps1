[CmdletBinding()]
param(
    [string]$Repo = 'Rafael-Dumar/financial-advisor',
    [string]$WorkflowName = 'Financial Advisor Reports',
    [int]$RunLimit = 20
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

function ConvertTo-BrtDateString {
    param([string]$IsoDate)
    try {
        $timeZone = [TimeZoneInfo]::FindSystemTimeZoneById('E. South America Standard Time')
        $utc = [DateTimeOffset]::Parse($IsoDate).UtcDateTime
        return [TimeZoneInfo]::ConvertTimeFromUtc($utc, $timeZone).ToString('yyyy-MM-dd')
    }
    catch {
        return ''
    }
}

function Invoke-GhJson {
    param([string[]]$Arguments)
    $output = & gh @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "gh_failed: gh $($Arguments -join ' ') exited with $LASTEXITCODE"
    }
    if (-not $output) {
        return $null
    }
    return ($output | Out-String | ConvertFrom-Json)
}

function Find-ReportFile {
    param(
        [string]$ArtifactRoot,
        [string]$ReportType,
        [string]$BrtDate
    )
    $preferred = Get-ChildItem -LiteralPath $ArtifactRoot -Recurse -File -Filter "$BrtDate-$ReportType.md" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($preferred) {
        return $preferred.FullName
    }
    $history = Get-ChildItem -LiteralPath $ArtifactRoot -Recurse -File -Filter "*-$ReportType.md" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($history) {
        return $history.FullName
    }
    $latest = Get-ChildItem -LiteralPath $ArtifactRoot -Recurse -File -Include 'latest.md', 'advisor-report.md' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    return $latest.FullName
}

function Find-AnalystInputFile {
    param([string]$ArtifactRoot)
    $file = Get-ChildItem -LiteralPath $ArtifactRoot -Recurse -File -Filter 'analyst-review-input.md' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    return $file.FullName
}

function Get-ReportSummary {
    param(
        [string]$ReportPath,
        [string]$Label
    )
    if (-not $ReportPath -or -not (Test-Path -LiteralPath $ReportPath)) {
        return @(
            "## $Label summary",
            "",
            "WARNING: missing_$($Label.ToLower())_report",
            ""
        )
    }

    $markdown = Get-Content -LiteralPath $ReportPath -Raw -Encoding UTF8
    $fields = @(
        'report_type',
        'Data mode',
        'report_grade',
        'market_session',
        'Decisao geral',
        'Coverage universe'
    )
    $lines = @("## $Label summary", "", ('- raw_path: `{0}`' -f $ReportPath))
    foreach ($field in $fields) {
        $escapedField = [regex]::Escape($field)
        $pattern = '(?m)^- ' + $escapedField + ':\s*(.+)$'
        $match = [regex]::Match($markdown, $pattern)
        if ($match.Success) {
            $lines += ('- {0}: {1}' -f $field, $match.Groups[1].Value.Trim())
        }
    }
    if ($markdown.Contains('Data mode: `blocked`') -or $markdown.Contains('report_grade: `not_decision_grade`') -or $markdown.Contains('diagnostic')) {
        $lines += "- WARNING: blocked_or_diagnostic"
    }
    $lines += ""
    return $lines
}

function Add-AnalystInputSection {
    param(
        [System.Collections.Generic.List[string]]$Lines,
        [string]$Path,
        [string]$Label
    )
    $Lines.Add("## $Label analyst-review-input")
    $Lines.Add("")
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) {
        $Lines.Add("WARNING: missing_$($Label.ToLower())_analyst_review_input")
        $Lines.Add("")
        return
    }
    $Lines.Add(('- raw_path: `{0}`' -f $Path))
    $Lines.Add("")
    $Lines.Add('```markdown')
    $Lines.Add((Get-Content -LiteralPath $Path -Raw -Encoding UTF8).Trim())
    $Lines.Add('```')
    $Lines.Add("")
}

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw 'missing_github_cli: install GitHub CLI with winget install GitHub.cli, then run gh auth login.'
}

& gh auth status | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'github_cli_not_authenticated: run gh auth login before fetching artifacts.'
}

$BrtDate = Get-BrtDateString
$NightlyDir = Join-Path $ProjectRoot ".tmp\nightly-review\$BrtDate"
$ReportsDir = Join-Path $ProjectRoot 'reports'
$OutputPath = Join-Path $ReportsDir 'nightly-review-input.md'
New-Item -ItemType Directory -Force -Path $NightlyDir | Out-Null
New-Item -ItemType Directory -Force -Path $ReportsDir | Out-Null

$runs = Invoke-GhJson -Arguments @(
    # gh run list
    'run', 'list',
    '--repo', $Repo,
    '--workflow', $WorkflowName,
    '--limit', [string]$RunLimit,
    '--json', 'databaseId,createdAt,displayTitle,status,conclusion,url,workflowName'
)
if (-not $runs) {
    throw 'no_workflow_runs_found'
}

$todayRuns = @($runs | Where-Object { (ConvertTo-BrtDateString $_.createdAt) -eq $BrtDate })
$candidateRuns = if ($todayRuns.Count -gt 0) { $todayRuns } else { @($runs) }

$selected = @{}
foreach ($run in $candidateRuns) {
    if ($selected.ContainsKey('main') -and $selected.ContainsKey('close')) {
        break
    }
    $view = Invoke-GhJson -Arguments @(
        # gh run view
        'run', 'view', [string]$run.databaseId,
        '--repo', $Repo,
        '--json', 'databaseId,createdAt,url,artifacts'
    )
    foreach ($artifact in @($view.artifacts)) {
        $name = [string]$artifact.name
        if (-not $selected.ContainsKey('main') -and $name -like 'financial-advisor-main-*') {
            $selected['main'] = [pscustomobject]@{ Run = $run; Artifact = $artifact }
        }
        elseif (-not $selected.ContainsKey('close') -and $name -like 'financial-advisor-close-*') {
            $selected['close'] = [pscustomobject]@{ Run = $run; Artifact = $artifact }
        }
    }
}

$warnings = New-Object System.Collections.Generic.List[string]
$artifactRoots = @{}
foreach ($reportType in @('main', 'close')) {
    if (-not $selected.ContainsKey($reportType)) {
        $warnings.Add("missing_${reportType}_artifact")
        continue
    }
    $runId = [string]$selected[$reportType].Run.databaseId
    $artifactName = [string]$selected[$reportType].Artifact.name
    $downloadDir = Join-Path $NightlyDir "$reportType-$runId"
    New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null

    & gh run download $runId --repo $Repo --name $artifactName --dir $downloadDir
    if ($LASTEXITCODE -ne 0) {
        throw "artifact_download_failed:${reportType}:${runId}:${artifactName}"
    }
    $artifactRoots[$reportType] = $downloadDir
}

$mainReport = if ($artifactRoots.ContainsKey('main')) { Find-ReportFile -ArtifactRoot $artifactRoots['main'] -ReportType 'main' -BrtDate $BrtDate } else { $null }
$closeReport = if ($artifactRoots.ContainsKey('close')) { Find-ReportFile -ArtifactRoot $artifactRoots['close'] -ReportType 'close' -BrtDate $BrtDate } else { $null }
$mainAnalyst = if ($artifactRoots.ContainsKey('main')) { Find-AnalystInputFile -ArtifactRoot $artifactRoots['main'] } else { $null }
$closeAnalyst = if ($artifactRoots.ContainsKey('close')) { Find-AnalystInputFile -ArtifactRoot $artifactRoots['close'] } else { $null }

foreach ($name in @('mainReport', 'closeReport', 'mainAnalyst', 'closeAnalyst')) {
    if (-not (Get-Variable -Name $name -ValueOnly)) {
        $warnings.Add("missing_$name")
    }
}
if (-not $mainReport) {
    $warnings.Add('main_baseline_missing')
}
if (-not $closeReport) {
    $warnings.Add('close_baseline_missing')
}

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("# Nightly qualitative review input")
$lines.Add("")
$lines.Add(('- brt_date: `{0}`' -f $BrtDate))
$lines.Add(('- generated_at_local: `{0}`' -f (Get-Date -Format s)))
$lines.Add(('- source_repo: `{0}`' -f $Repo))
$lines.Add(('- workflow: `{0}`' -f $WorkflowName))
$lines.Add(('- download_dir: `{0}`' -f $NightlyDir))
$lines.Add("- safety: analysis only; no broker; no order execution; no automatic buying.")
$lines.Add("")
$lines.Add("## Workflow runs used")
$lines.Add("")
foreach ($reportType in @('main', 'close')) {
    if ($selected.ContainsKey($reportType)) {
        $run = $selected[$reportType].Run
        $artifact = $selected[$reportType].Artifact
        $lines.Add("- ${reportType}: run_id=$($run.databaseId); artifact=$($artifact.name); created_at=$($run.createdAt); url=$($run.url)")
    }
    else {
        $lines.Add("- ${reportType}: WARNING missing artifact")
    }
}
$lines.Add("")
$lines.Add("## Warnings")
$lines.Add("")
if ($warnings.Count -eq 0) {
    $lines.Add("nenhum")
}
else {
    foreach ($warning in $warnings) {
        $lines.Add("- WARNING: $warning")
    }
}
$lines.Add("")

foreach ($line in (Get-ReportSummary -ReportPath $mainReport -Label 'Main')) { $lines.Add($line) }
foreach ($line in (Get-ReportSummary -ReportPath $closeReport -Label 'Close')) { $lines.Add($line) }

$lines.Add("## Raw file paths")
$lines.Add("")
$lines.Add(('- main_report: `{0}`' -f $mainReport))
$lines.Add(('- close_report: `{0}`' -f $closeReport))
$lines.Add(('- main_analyst_review_input: `{0}`' -f $mainAnalyst))
$lines.Add(('- close_analyst_review_input: `{0}`' -f $closeAnalyst))
$lines.Add("")

Add-AnalystInputSection -Lines $lines -Path $mainAnalyst -Label 'Main'
Add-AnalystInputSection -Lines $lines -Path $closeAnalyst -Label 'Close'

$lines.Add("## Suggested next step")
$lines.Add("")
$lines.Add("Use this file as context for a manual Codex/Public Equity Investing qualitative review. Keep any conclusion analysis-only; never execute orders from this package.")
$lines.Add("")

Set-Content -LiteralPath $OutputPath -Encoding UTF8 -Value $lines
Write-Host "nightly_review_dir=$NightlyDir"
Write-Host "nightly_review_input=reports\nightly-review-input.md"
if ($warnings.Count -gt 0) {
    Write-Warning ("nightly_review_warnings=" + ($warnings -join ','))
}
