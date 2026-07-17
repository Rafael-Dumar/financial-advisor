[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Invoke-GitHubReadValidation {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Operation,
        [Parameter(Mandatory = $true)]
        [string]$Endpoint,
        [Parameter(Mandatory = $true)]
        [string]$DeniedError,
        [Parameter(Mandatory = $true)]
        [string]$Jq
    )

    $stderrPath = [System.IO.Path]::GetTempFileName()
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& gh api $Endpoint --jq $Jq 2> $stderrPath)
        $exitCode = $LASTEXITCODE
        $stderr = if (Test-Path -LiteralPath $stderrPath) {
            Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue
        }
        else {
            ''
        }
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
    }

    if ($exitCode -eq 0) {
        return $output
    }
    if ($stderr -match '(?i)(HTTP\s+)?(401|403)') {
        throw $DeniedError
    }
    throw "github_api_unavailable:operation=$Operation`:exit_code=$exitCode"
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw 'gh_cli_missing'
}
if (-not $env:GH_TOKEN -and -not $env:GITHUB_TOKEN) {
    throw 'github_token_missing'
}
if (-not $env:GH_TOKEN -and $env:GITHUB_TOKEN) {
    $env:GH_TOKEN = $env:GITHUB_TOKEN
}

$repository = if ($env:GH_REPO) { $env:GH_REPO } else { $env:GITHUB_REPOSITORY }
if (-not $repository) {
    throw 'github_api_unavailable:operation=resolve_repository:exit_code=0'
}

Invoke-GitHubReadValidation `
    -Operation 'read_repository' `
    -Endpoint "repos/$repository" `
    -DeniedError 'github_repository_read_denied' `
    -Jq '.full_name' | Out-Null
Invoke-GitHubReadValidation `
    -Operation 'read_actions' `
    -Endpoint "repos/$repository/actions/runs?per_page=1" `
    -DeniedError 'github_actions_read_denied' `
    -Jq '.total_count' | Out-Null

Write-Output 'github_api_access_validated'
