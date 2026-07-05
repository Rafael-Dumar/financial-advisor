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

- `ALPHAVANTAGE_API_KEY`: optional fallback for stock prices and low-call aggregated news/sentiment context. Get it at `https://www.alphavantage.co/support/#api-key`; the free plan is enough for the bot's single cached news/sentiment request per report.
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

Direct CLI close report using the same-day main baseline and live gate:

```powershell
.\.venv\Scripts\python.exe -m advisor report close --from-main --require-live
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
- `.github/workflows/financial-advisor-nightly-review.yml`

Schedule:

- Main report: weekdays at 11:15 BRT (`15 14 * * 1-5` UTC).
- Close report: weekdays at 17:15 BRT (`15 20 * * 1-5` UTC).

Configure repository secrets in GitHub:

1. Open the repository on GitHub.
2. Go to Settings -> Secrets and variables -> Actions.
3. Add `FMP_API_KEY` and `COINGECKO_API_KEY`.
4. Optionally add `ALPHAVANTAGE_API_KEY`, `COINBASE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ADVISOR_ACCOUNT_CAPITAL`, `ADVISOR_RISK_FRACTION`, `ADVISOR_MAX_DAILY_LOSS_FRACTION`, and `ADVISOR_MAX_WEEKLY_LOSS_FRACTION`.

Manual run:

1. Open Actions -> Financial Advisor Reports.
2. Choose Run workflow.
3. Select `report_type` as `main` or `close`.
4. After completion, download the uploaded artifact named `financial-advisor-...`.

Nightly final review:

- Workflow name: `Financial Advisor Nightly Review`.
- Schedule: weekdays at 18:30 BRT (`30 21 * * 1-5` UTC), after the close report.
- It roda no GitHub Actions without Codex or this PC staying open.
- It uses GitHub CLI with the workflow `GH_TOKEN` to download the latest same-day main/close artifacts, writes `reports\analyst-final-review.md`, sends only the `## Telegram summary` section when Telegram secrets are configured, and uploads the final reports artifact.
- Public Equity Investing is not executed automatically in GitHub Actions; the generated review records `Public Equity Investing executed: false` and stays based on `nightly-review-input` plus safety rules.

If `advisor config validate --require-live` fails, the workflow still runs `advisor report ... --require-live`; the CLI writes a blocked/no-trade report under `reports/` so the artifact explains the failure. It does not connect to a broker, execute orders, or recommend automatic buying.

The workflow passes `--include-discovery` only for the main report. The close report uses `--from-main` and does not run discovery by default; it reuses the same-day main baseline from `reports/history/YYYY-MM-DD-main.md` or the restored SQLite cache when available. If the main baseline is missing or blocked, close writes a blocked/no-trade report with `main_baseline_missing_or_blocked`.

The GitHub workflow also sets a conservative universe and per-run budget:

- `ADVISOR_STOCK_WATCHLIST=INTC,AMD,NVDA,HIMS,MU,MSFT,USAR,CRDO,DELL,MRVL,HOOD`
- `ADVISOR_CRYPTO_WATCHLIST=SOL,HYPE,BTC,ETH`
- `ADVISOR_MAX_STOCKS_PER_RUN=11`
- `ADVISOR_FMP_CALL_BUDGET_PER_RUN=90`

With the current FMP data model, each stock costs about 7 FMP calls plus 2 benchmark calls when fresh data is needed. The close job is cache-first and reports `cache_reused_from_main`, `close_universe_source`, `skipped_provider_calls_due_to_cache`, and `skipped_provider_calls_due_to_rate_limit` so FMP usage stays visible.

The main/close report workflow does not send Telegram. Telegram is sent only by `Financial Advisor Nightly Review`, after it has fetched the main and close artifacts and generated `reports\analyst-final-review.md`. This keeps the phone notification focused on the final conservative review instead of the raw quantitative report.

## Nightly qualitative review prep

Use this local helper when you open Codex at night and want the latest GitHub Actions artifacts prepared for a manual qualitative review.

Install and authenticate GitHub CLI:

```powershell
winget install GitHub.cli
gh auth login
gh auth status
```

Fetch the latest Financial Advisor Reports artifacts:

```powershell
Set-Location "C:\Users\Administrador\Documents\financial advisor"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\fetch-latest-github-reports.ps1"
```

The script checks that `gh` exists, checks `gh auth status`, prefers workflow runs from the current BRT date, and uses `gh run download <run_id> --dir ...` to download all artifacts from candidate runs. It does not rely on the `artifacts` JSON field from `gh run view`, which keeps it compatible with older GitHub CLI versions. It writes:

- `.tmp\nightly-review\YYYY-MM-DD\...`
- `reports\nightly-review-input.md`

Validate the download:

```powershell
Test-Path ".tmp\nightly-review\$(Get-Date -Format yyyy-MM-dd)"
Test-Path "reports\nightly-review-input.md"
Get-Content "reports\nightly-review-input.md" -TotalCount 80
```

Use `reports\nightly-review-input.md` as the context file for a manual Codex/Public Equity Investing qualitative review. The file includes workflow run IDs/links, main and close summaries, raw artifact paths, available `analyst-review-input.md` content, and warnings when main or close is blocked/diagnostic. It does not print secrets, connect to a broker, execute orders, or suggest automatic buying.

The final analyst review separates operational decision from observation labels. A diagnostic main report can still force operational `no_trade`, while assets with partial positive evidence may be labeled `watch_pending_checks`, `research_only`, or `crypto_research_only`. `blocked` is reserved for critical missing price/provider/data-mode issues; `rejected` is used for weak thesis, negative EV, or invalid setup.

Crypto review separates `basic_data_status` from `flow_data_status`. Binance `http_error:451` or `binance_restricted_location` marks `binance_status: restricted` and limits Binance-dependent flow/derivatives checks, but it does not block BTC/ETH/SOL by itself when CoinGecko/Coinbase/fallback basic data is available. If basic price/liquidity/history is also missing, the asset remains `blocked`.

News/catalyst context is optional and budget-conscious. When `ALPHAVANTAGE_API_KEY` is configured, the live loader requests one cached News Sentiment payload for the current stock and crypto universe, then maps relevant items back to each asset. If the key is absent, the provider is rate-limited, or no matching item is returned, reports keep `news_status` as `not_verified/not_collected`; they must not state that there is no news/event risk.

SEC EDGAR is used as a no-key filing source for supported US equities. The loader checks recent 8-K, 10-Q, 10-K, 20-F, and 6-K submissions from `data.sec.gov` and attaches them as confirmed corporate events. SEC filings improve catalyst context, but they do not approve a trade by themselves.

When Binance is restricted in the GitHub runner, the crypto loader falls back to CoinGecko for basic price history, Hyperliquid for available funding/open-interest context, and public Coinbase product data for premium checks. CVD and liquidations can still remain `not_verified` if no free source is available.

## Optional Telegram for nightly analyst review

The nightly Telegram step sends only the `## Telegram summary` section from `reports\analyst-final-review.md`. It does not send `latest.md`, does not send the full quantitative report, and does not print the bot token.

Create a Telegram bot:

1. Open Telegram and start a chat with `BotFather`.
2. Send `/newbot` and follow the prompts.
3. Copy the token shown by BotFather. This is `TELEGRAM_BOT_TOKEN`.
4. Send one message to your new bot from the chat that should receive alerts.
5. Open `https://api.telegram.org/bot<token>/getUpdates` in a browser, replacing `<token>` locally, and copy the numeric chat id from the response. This is `TELEGRAM_CHAT_ID`.

Configure Windows user-level environment variables without committing secrets:

```powershell
[Environment]::SetEnvironmentVariable("TELEGRAM_BOT_TOKEN", "paste_token_here", "User")
[Environment]::SetEnvironmentVariable("TELEGRAM_CHAT_ID", "paste_chat_id_here", "User")
```

Open a new terminal after setting them. Test without exposing secrets:

```powershell
if ($env:TELEGRAM_BOT_TOKEN) { "TELEGRAM_BOT_TOKEN=set" } else { "TELEGRAM_BOT_TOKEN=missing" }
if ($env:TELEGRAM_CHAT_ID) { "TELEGRAM_CHAT_ID=set" } else { "TELEGRAM_CHAT_ID=missing" }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\send-analyst-final-telegram.ps1"
```

Run the nightly review with optional Telegram:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run-nightly-analyst-review.ps1" -SendTelegram
```

If `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` is missing, the send script writes a clear warning and exits without failing the automation. If `reports\analyst-final-review.md` is missing, it blocks the send with `analyst_final_review_missing`.

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
- If Binance returns `http_error:451` in GitHub Actions, the runner location is restricted by Binance. The bot marks `binance_restricted_location`, uses CoinGecko market-chart history as a basic-price fallback when available, and keeps Binance-dependent flow/derivatives as not verified. If no basic history is available, the asset remains blocked.

Report says `blocked` or `no_trade_day`:

- Treat that as the intended safe outcome when live data is missing, stale, incomplete, or not actionable.
- Win rate is historical/statistical context only. It is not a guarantee and must not be treated as permission to trade.
