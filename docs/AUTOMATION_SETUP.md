# Automation Setup

This project is configured for local analysis automation only. The scripts generate reports; they do not connect to a broker, place orders, or automate any financial action.

GitHub Actions is now the recommended unattended automation path because it runs in GitHub without Codex or this PC staying open.

## Verified Project Layout

- Project root: `C:\Users\Administrador\Documents\financial advisor`
- Required files present: `pyproject.toml`, `README.md`, `advisor\`
- Required Python: `>=3.12`
- System commands checked in this terminal:
  - `python --version`: not available
  - `python3 --version`: not available
  - `py -3 --version`: not available
- Bootstrap Python used to create `.venv`: `C:\Users\Administrador\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`
- Standard project Python command after setup: `.\.venv\Scripts\python.exe`

## Manual Setup

Run these from the project root:

```powershell
Set-Location "C:\Users\Administrador\Documents\financial advisor"
& "C:\Users\Administrador\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m advisor config validate --require-live
```

The project has no declared third-party runtime dependencies. The editable install is still useful because it validates packaging and installs the `advisor` console entry point inside `.venv`.

Do not commit `.env` or API keys. `.env` is ignored by `.gitignore`.

## Live Configuration

Create `.env` locally from the template if needed:

```powershell
Copy-Item .env.example .env
notepad .env
```

Required variables:

- `FMP_API_KEY`: stock prices, fundamentals, valuation, earnings.
- `COINGECKO_API_KEY`: crypto market cap, rank, and liquidity.

Optional variables:

- `ALPHAVANTAGE_API_KEY`
- `COINBASE_API_KEY`
- `ADVISOR_ACCOUNT_CAPITAL`
- `ADVISOR_RISK_FRACTION`
- `ADVISOR_MAX_DAILY_LOSS_FRACTION`
- `ADVISOR_MAX_WEEKLY_LOSS_FRACTION`

Validate before any scheduled run:

```powershell
.\.venv\Scripts\python.exe -m advisor config validate --require-live
```

If validation fails with `missing_fmp_api_key`, set `FMP_API_KEY` in `.env`.
If validation fails with `missing_coingecko_api_key`, set `COINGECKO_API_KEY` in `.env`.
If validation fails with `placeholder_fmp_api_key` or `placeholder_coingecko_api_key`, replace the `your_...` placeholder value in `.env`.

Alternative Windows user-level environment variables:

```powershell
[Environment]::SetEnvironmentVariable("FMP_API_KEY", "paste_value_here", "User")
[Environment]::SetEnvironmentVariable("COINGECKO_API_KEY", "paste_value_here", "User")
```

Open a new terminal after setting user-level variables.

## Manual Report Runs

Direct CLI main report with discovery and live gate:

```powershell
.\.venv\Scripts\python.exe -m advisor report main --include-discovery --require-live
```

Direct CLI close report with discovery and live gate:

```powershell
.\.venv\Scripts\python.exe -m advisor report close --include-discovery --require-live
```

Main report with discovery:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\Administrador\Documents\financial advisor\scripts\run-main-report.ps1"
```

Close report without discovery:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\Administrador\Documents\financial advisor\scripts\run-close-report.ps1"
```

Dry run, validation only:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\Administrador\Documents\financial advisor\scripts\run-main-report.ps1" -DryRun
```

Direct CLI scan:

```powershell
.\.venv\Scripts\python.exe -m advisor scan --include-discovery --require-live
```

## GitHub Actions

Workflow file:

- `.github/workflows/financial-advisor-reports.yml`

Schedule:

- Main report: weekdays at 11:15 BRT (`15 14 * * 1-5` UTC).
- Close report: weekdays at 17:15 BRT (`15 20 * * 1-5` UTC).

Configure repository secrets in GitHub:

1. Open the repository on GitHub.
2. Go to Settings -> Secrets and variables -> Actions.
3. Add `FMP_API_KEY` and `COINGECKO_API_KEY`.
4. Optionally add `ALPHAVANTAGE_API_KEY`, `COINBASE_API_KEY`, `ADVISOR_ACCOUNT_CAPITAL`, `ADVISOR_RISK_FRACTION`, `ADVISOR_MAX_DAILY_LOSS_FRACTION`, and `ADVISOR_MAX_WEEKLY_LOSS_FRACTION`.

Manual run:

1. Open Actions -> Financial Advisor Reports.
2. Choose Run workflow.
3. Select `report_type` as `main` or `close`.
4. After completion, download the uploaded artifact named `financial-advisor-...`.

If `advisor config validate --require-live` fails, the workflow still runs `advisor report ... --require-live`; the CLI writes a blocked/no-trade report under `reports/` so the artifact explains the failure. It does not connect to a broker, execute orders, or recommend automatic buying.

The workflow does not pass `--include-discovery` by default because FMP free-tier calls can be exhausted quickly. Run the direct CLI command with `--include-discovery` only when you intentionally want the larger universe.

The GitHub workflow also sets a conservative universe and per-run budget:

- `ADVISOR_STOCK_WATCHLIST=MSFT,NVDA`
- `ADVISOR_CRYPTO_WATCHLIST=HYPE`
- `ADVISOR_MAX_STOCKS_PER_RUN=2`
- `ADVISOR_FMP_CALL_BUDGET_PER_RUN=20`

With the current FMP data model, each stock costs about 7 FMP calls plus 2 benchmark calls. The scheduled default therefore estimates about 16 FMP calls per report. If you expand the watchlist, increase the budget intentionally and keep the daily 250-call cap in mind.

## Generated Files

The CLI writes:

- `reports\advisor-report.md`
- `reports\advisor-report.html`
- `reports\latest.md`, when run as `advisor report main|close`
- `reports\latest.html`, when run as `advisor report main|close`
- `reports\history\advisor-report-YYYYMMDD-HHMMSS.md`
- `reports\history\advisor-report-YYYYMMDD-HHMMSS.html`
- `reports\history\YYYY-MM-DD-main.md`, when run as `advisor report main`
- `reports\history\YYYY-MM-DD-close.md`, when run as `advisor report close`

The automation scripts also write:

- `reports\latest.md`
- `reports\latest.html`, when HTML is generated
- `reports\history\YYYY-MM-DD-main.md`
- `reports\history\YYYY-MM-DD-main.html`, when HTML is generated
- `reports\history\YYYY-MM-DD-close.md`
- `reports\history\YYYY-MM-DD-close.html`, when HTML is generated
- `.tmp\logs\main-report-YYYYMMDD-HHMMSS.log`
- `.tmp\logs\close-report-YYYYMMDD-HHMMSS.log`

The `YYYY-MM-DD` history name uses BRT via the Windows `E. South America Standard Time` zone.

## Windows Task Scheduler

Create two tasks in Task Scheduler.

Main report:

- Name: `Financial Advisor Main Report`
- Trigger: choose the desired local time for the main analysis.
- Action: Start a program
- Program/script: `powershell.exe`
- Add arguments:

```text
-NoProfile -ExecutionPolicy Bypass -File "C:\Users\Administrador\Documents\financial advisor\scripts\run-main-report.ps1"
```

- Start in:

```text
C:\Users\Administrador\Documents\financial advisor
```

Close report:

- Name: `Financial Advisor Close Report`
- Trigger: choose the desired local time after market close.
- Action: Start a program
- Program/script: `powershell.exe`
- Add arguments:

```text
-NoProfile -ExecutionPolicy Bypass -File "C:\Users\Administrador\Documents\financial advisor\scripts\run-close-report.ps1"
```

- Start in:

```text
C:\Users\Administrador\Documents\financial advisor
```

Recommended Task Scheduler settings:

- Run only when the user is logged on, unless you have confirmed the `.env` and network access work in the unattended account.
- Stop the task if it runs longer than 30 minutes.
- Do not start a new instance if the previous run is still active.
- Review `.tmp\logs\` after the first scheduled run.

## Troubleshooting

Python not found:

- Use the project standard command after setup: `.\.venv\Scripts\python.exe`.
- If `.venv` is missing, recreate it with the bootstrap Python shown above.

Dependency or editable install failure:

- Run `.\.venv\Scripts\python.exe -m pip install -e .`.
- If setuptools complains about multiple top-level packages, verify `pyproject.toml` contains the package discovery rule for `advisor*`.

API key failure:

- Run `.\.venv\Scripts\python.exe -m advisor config validate --require-live`.
- Fix only the variable named in the error.
- Do not paste API keys into docs, scripts, git commits, screenshots, or terminal output shared publicly.

API limit failure:

- The CLI estimates provider usage before discovery scans.
- If `api_budget_exceeded` appears, run the close script or a direct scan without `--include-discovery`, or wait for provider limits to reset.

Network/provider failure:

- The scripts fail explicitly and leave details in `.tmp\logs\`.
- They do not promote a non-live report to `reports\latest.md`.
- If Binance returns `http_error:451` in GitHub Actions, the runner location is restricted by Binance. The bot marks affected Binance crypto assets as blocked by `binance_restricted_location` instead of treating them as tradeable.

Report says `blocked` or `no_trade_day`:

- Treat that as the intended safe outcome when live data is missing, stale, incomplete, or not actionable.
- Win rate is historical/statistical context only. It is not a guarantee and must not be treated as permission to trade.
