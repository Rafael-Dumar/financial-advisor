[CmdletBinding()]
param(
    [string]$Repo = 'Rafael-Dumar/financial-advisor',
    [string]$WorkflowName = 'Financial Advisor Reports',
    [int]$RunLimit = 20,
    [string]$ExpectedHeadSha = '',
    [string]$WorkflowEvent = 'local',
    [string]$RuntimeSha = '',
    [switch]$DryRun,
    [string]$ReplayReason = '',
    [switch]$AllowManual,
    [switch]$AllowStaleDiagnostic
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

function ConvertTo-Iso8601UtcString {
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [object]$Value
    )

    $invariantCulture = [System.Globalization.CultureInfo]::InvariantCulture
    if ($Value -is [DateTimeOffset]) {
        $timestamp = [DateTimeOffset]$Value
    }
    elseif ($Value -is [DateTime]) {
        $dateTime = [DateTime]$Value
        if ($dateTime.Kind -eq [DateTimeKind]::Unspecified) {
            $timestamp = [DateTimeOffset]::new($dateTime, [TimeSpan]::Zero)
        }
        else {
            $timestamp = [DateTimeOffset]::new($dateTime)
        }
    }
    elseif ($Value -is [string]) {
        $text = ([string]$Value).Trim()
        $isoPattern = '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$'
        if (-not [regex]::IsMatch($text, $isoPattern)) {
            throw 'invalid_iso8601_timestamp'
        }
        $timestamp = [DateTimeOffset]::MinValue
        $style = [System.Globalization.DateTimeStyles]::RoundtripKind
        if (-not [DateTimeOffset]::TryParse($text, $invariantCulture, $style, [ref]$timestamp)) {
            throw 'invalid_iso8601_timestamp'
        }
    }
    else {
        throw 'unsupported_timestamp_type'
    }

    return $timestamp.ToUniversalTime().UtcDateTime.ToString('o', $invariantCulture)
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

function Invoke-GhCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Operation,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $stderrPath = [System.IO.Path]::GetTempFileName()
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& gh @Arguments 2> $stderrPath)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
    }
    if ($exitCode -ne 0) {
        throw "github_api_call_failed:operation=$Operation`:exit_code=$exitCode"
    }
    return $output
}

function Invoke-GhJson {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Operation,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $output = Invoke-GhCommand -Operation $Operation -Arguments $Arguments
    if (-not $output) {
        return $null
    }
    try {
        return ($output | Out-String | ConvertFrom-Json)
    }
    catch {
        throw "github_api_invalid_json:operation=$Operation"
    }
}

function Find-ReportFile {
    param(
        [string]$ArtifactRoot,
        [string]$ReportType,
        [string]$BrtDate
    )
    function Test-ReportType {
        param(
            [string]$Path,
            [string]$ExpectedType
        )
        if (-not $Path -or -not (Test-Path -LiteralPath $Path)) {
            return $false
        }
        $markdown = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
        $pattern = '(?m)^-\s*report_type:\s*`?' + [regex]::Escape($ExpectedType) + '`?\s*$'
        return [regex]::IsMatch($markdown, $pattern)
    }

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
        Where-Object { Test-ReportType -Path $_.FullName -ExpectedType $ReportType } |
        Select-Object -First 1
    if ($latest) {
        return $latest.FullName
    }
    return $null
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
        'primary_report_grade',
        'overall_report_grade',
        'market_session',
        'primary_market_session',
        'discovery_coverage_grade',
        'stale_asset_count_primary',
        'provider_rate_limit_status',
        'blocking_reasons',
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

function Get-ReportField {
    param(
        [string]$ReportPath,
        [string]$Field
    )
    if (-not $ReportPath -or -not (Test-Path -LiteralPath $ReportPath)) {
        return ''
    }
    $markdown = Get-Content -LiteralPath $ReportPath -Raw -Encoding UTF8
    $pattern = '(?m)^-\s*' + [regex]::Escape($Field) + ':\s*`?([^`\r\n]+)`?\s*$'
    $match = [regex]::Match($markdown, $pattern)
    if ($match.Success) {
        return $match.Groups[1].Value.Trim()
    }
    return ''
}

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectRoot

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw 'gh_cli_missing'
}

if (-not $env:GH_TOKEN -and -not $env:GITHUB_TOKEN) {
    throw 'github_token_missing: set GH_TOKEN or GITHUB_TOKEN; local users may authenticate securely with gh auth login first.'
}
if (-not $env:GH_TOKEN -and $env:GITHUB_TOKEN) {
    $env:GH_TOKEN = $env:GITHUB_TOKEN
}

$BrtDate = Get-BrtDateString
$NightlyDir = Join-Path $ProjectRoot ".tmp\nightly-review\$BrtDate"
$ReportsDir = Join-Path $ProjectRoot 'reports'
$OutputPath = Join-Path $ReportsDir 'nightly-review-input.md'
if (-not $ExpectedHeadSha) {
    $ExpectedHeadSha = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $ExpectedHeadSha) {
        throw 'expected_head_sha_missing: pass -ExpectedHeadSha explicitly or run inside the repository checkout.'
    }
}
if (-not $RuntimeSha) {
    $RuntimeSha = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $RuntimeSha) {
        throw 'runtime_sha_missing'
    }
}
New-Item -ItemType Directory -Force -Path $NightlyDir | Out-Null
New-Item -ItemType Directory -Force -Path $ReportsDir | Out-Null

$runs = Invoke-GhJson -Operation 'list_workflow_runs' -Arguments @(
    # gh run list
    'run', 'list',
    '--repo', $Repo,
    '--workflow', $WorkflowName,
    '--limit', [string]$RunLimit,
    '--json', 'databaseId,createdAt,displayTitle,status,conclusion,url,workflowName,event,headSha,headBranch'
)
if (-not $runs) {
    throw 'no_workflow_runs_found'
}

$todayRuns = @($runs | Where-Object { (ConvertTo-BrtDateString $_.createdAt) -eq $BrtDate })
$candidateRuns = if ($AllowStaleDiagnostic) { @($runs) } else { $todayRuns }
if ($candidateRuns.Count -eq 0) {
    throw "no_valid_current_day_artifact_pair:brt_date=$BrtDate:expected_head_sha=$ExpectedHeadSha"
}

$candidateRecords = New-Object System.Collections.Generic.List[object]
$downloadDirsByRun = @{}
foreach ($run in $candidateRuns) {
    if ($run.conclusion -ne 'success' -or $run.headBranch -ne 'main' -or $run.headSha -ne $ExpectedHeadSha) {
        continue
    }
    if (-not $AllowManual -and $run.event -ne 'schedule') {
        continue
    }
    $view = Invoke-GhJson -Operation 'view_workflow_run' -Arguments @(
        # gh run view
        'run', 'view', [string]$run.databaseId,
        '--repo', $Repo,
        '--json', 'databaseId,createdAt,url,status,conclusion,name,workflowName,headBranch,headSha,event'
    )

    $runId = [string]$view.databaseId
    $artifactResponse = Invoke-GhJson -Operation 'list_run_artifacts' -Arguments @('api', "repos/$Repo/actions/runs/$runId/artifacts")
    foreach ($artifact in @($artifactResponse.artifacts)) {
        $artifactPattern = '^financial-advisor-(main|close)-' + [regex]::Escape($runId) + '$'
        $artifactMatch = [regex]::Match([string]$artifact.name, $artifactPattern)
        if (-not $artifactMatch.Success -or [bool]$artifact.expired) {
            continue
        }
        $reportType = $artifactMatch.Groups[1].Value
        $downloadDir = Join-Path $NightlyDir "run-$runId-$reportType"
        if (Test-Path -LiteralPath $downloadDir) {
            $resolvedDownloadDir = (Resolve-Path -LiteralPath $downloadDir).Path
            $resolvedNightlyDir = (Resolve-Path -LiteralPath $NightlyDir).Path
            if (-not $resolvedDownloadDir.StartsWith($resolvedNightlyDir, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "unsafe_download_dir:${resolvedDownloadDir}"
            }
            Remove-Item -LiteralPath $downloadDir -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null
        Invoke-GhCommand -Operation 'download_report_artifact' -Arguments @(
            # gh run download
            'run', 'download', $runId,
            '--repo', $Repo,
            '--name', [string]$artifact.name,
            '--dir', $downloadDir
        ) | Out-Null
        $reportPath = Find-ReportFile -ArtifactRoot $downloadDir -ReportType $reportType -BrtDate (ConvertTo-BrtDateString $view.createdAt)
        if (-not $reportPath) {
            continue
        }
        $contentReportType = Get-ReportField -ReportPath $reportPath -Field 'report_type'
        $generatedAt = Get-ReportField -ReportPath $reportPath -Field 'Generated at'
        if (-not $generatedAt) {
            $generatedAt = Get-ReportField -ReportPath $reportPath -Field 'generated_at'
        }
        $reportBrtDate = ConvertTo-BrtDateString $generatedAt
        $candidateRecords.Add([pscustomobject]@{
            run_id = [int64]$view.databaseId
            created_at = ConvertTo-Iso8601UtcString $view.createdAt
            event = [string]$view.event
            conclusion = [string]$view.conclusion
            head_sha = [string]$view.headSha
            head_branch = [string]$view.headBranch
            url = [string]$view.url
            artifact_name = [string]$artifact.name
            artifact_expired = [bool]$artifact.expired
            artifact_created_at = ConvertTo-Iso8601UtcString $artifact.created_at
            report_type = [string]$contentReportType
            report_brt_date = [string]$reportBrtDate
            report_generated_at = ConvertTo-Iso8601UtcString $generatedAt
            report_path = [string]$reportPath
        })
        $downloadDirsByRun["$runId-$reportType"] = $downloadDir
    }
}

$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = 'python'
}
$selectorArgs = @('-m', 'advisor.artifact_selection', '--brt-date', $BrtDate, '--expected-head-sha', $ExpectedHeadSha)
if ($AllowManual) { $selectorArgs += '--allow-manual' }
if ($AllowStaleDiagnostic) { $selectorArgs += '--allow-stale-diagnostic' }
$candidateJson = ConvertTo-Json -InputObject $candidateRecords.ToArray() -Depth 8
$selectionJson = $candidateJson | & $Python @selectorArgs
if ($LASTEXITCODE -ne 0 -or -not $selectionJson) {
    throw "artifact_selection_failed:$($selectionJson | Out-String)"
}
$selection = ($selectionJson | Out-String | ConvertFrom-Json)
$mainRun = $selection.main
$closeRun = $selection.close
$mainRoot = [string]$downloadDirsByRun["$($mainRun.run_id)-main"]
$closeRoot = [string]$downloadDirsByRun["$($closeRun.run_id)-close"]
$mainReport = [string]$mainRun.report_path
$closeReport = [string]$closeRun.report_path
$mainAnalyst = Find-AnalystInputFile -ArtifactRoot $mainRoot
$closeAnalyst = Find-AnalystInputFile -ArtifactRoot $closeRoot
if (-not $mainAnalyst -or -not $closeAnalyst) {
    throw 'artifact_content_mismatch:analyst_review_input_missing'
}
$warnings = New-Object System.Collections.Generic.List[string]
if (-not [bool]$selection.operational_allowed) {
    $warnings.Add("stale_diagnostic:source_date=$($selection.source_date):artifact_age_seconds=$($selection.artifact_age_seconds)")
}

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("# Nightly qualitative review input")
$lines.Add("")
$lines.Add(('- brt_date: `{0}`' -f $BrtDate))
$lines.Add(('- generated_at_local: `{0}`' -f (Get-Date -Format s)))
$lines.Add(('- source_repo: `{0}`' -f $Repo))
$lines.Add(('- workflow: `{0}`' -f $WorkflowName))
$lines.Add(('- workflow_event: `{0}`' -f $WorkflowEvent))
$lines.Add(('- runtime_sha: `{0}`' -f $RuntimeSha))
$lines.Add(('- source_report_sha: `{0}`' -f $ExpectedHeadSha))
$lines.Add(('- dry_run: `{0}`' -f ([bool]$DryRun).ToString().ToLowerInvariant()))
if ($ReplayReason) {
    $lines.Add(('- replay_reason: `{0}`' -f $ReplayReason))
}
if ($DryRun) {
    $lines.Add('- telegram_sent: `false`')
}
$lines.Add(('- download_dir: `{0}`' -f $NightlyDir))
$lines.Add(('- main_run_id: `{0}`' -f $mainRun.run_id))
$lines.Add(('- close_run_id: `{0}`' -f $closeRun.run_id))
$lines.Add(('- main_head_sha: `{0}`' -f $mainRun.head_sha))
$lines.Add(('- close_head_sha: `{0}`' -f $closeRun.head_sha))
$lines.Add(('- main_event: `{0}`' -f $mainRun.event))
$lines.Add(('- close_event: `{0}`' -f $closeRun.event))
$lines.Add(('- main_generated_at: `{0}`' -f $mainRun.report_generated_at))
$lines.Add(('- close_generated_at: `{0}`' -f $closeRun.report_generated_at))
$lines.Add(('- artifact_selection_status: `{0}`' -f $selection.status))
$lines.Add(('- artifact_age_seconds: {0}' -f $selection.artifact_age_seconds))
$lines.Add(('- artifact_valid: `{0}`' -f ([bool]$selection.operational_allowed).ToString().ToLowerInvariant()))
$lines.Add(('- operational_decision_allowed: `{0}`' -f ([bool]$selection.operational_allowed).ToString().ToLowerInvariant()))
$lines.Add(('- source_date: `{0}`' -f $selection.source_date))
$lines.Add("- safety: analysis only; no broker; no order execution; no automatic buying.")
$lines.Add("")
$lines.Add("## Workflow runs used")
$lines.Add("")
$lines.Add("- main: run_id=$($mainRun.run_id); download_dir=$mainRoot; created_at=$($mainRun.created_at); url=$($mainRun.url); event=$($mainRun.event); head_sha=$($mainRun.head_sha); artifact=$($mainRun.artifact_name)")
$lines.Add("- close: run_id=$($closeRun.run_id); download_dir=$closeRoot; created_at=$($closeRun.created_at); url=$($closeRun.url); event=$($closeRun.event); head_sha=$($closeRun.head_sha); artifact=$($closeRun.artifact_name)")
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
$lines.Add("Use this file as input for the Rule-Based Final Review. Public Equity Investing executed: false. Keep any conclusion analysis-only; never execute orders from this package.")
$lines.Add("")

Set-Content -LiteralPath $OutputPath -Encoding UTF8 -Value $lines
Write-Host "nightly_review_dir=$NightlyDir"
Write-Host "nightly_review_input=reports\nightly-review-input.md"
if ($warnings.Count -gt 0) {
    Write-Warning ("nightly_review_warnings=" + ($warnings -join ','))
}
