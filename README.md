# Financial Advisor V1

Local personal assistant for investment quality and swing trade analysis.

This v1 is intentionally simple:

- Python standard library only.
- CLI first.
- SQLite cache and report history.
- Markdown and HTML reports.
- No automatic order execution.
- No dashboard or Telegram integration.

Core commands:

```powershell
python -m advisor config validate --allow-missing-keys
python -m advisor config validate --require-live
python -m advisor scan
python -m advisor scan --require-live
python -m advisor scan --include-discovery
python -m advisor backtest
python -m advisor collect-crypto-flow
python -m advisor report
python -m advisor report main --include-discovery --require-live
python -m advisor report close --include-discovery --require-live
```

The report separates `investment_quality_score` from `swing_trade_score` so a good asset is not confused with a good entry right now.

## Live data setup

Create a local `.env` from `.env.example`:

```powershell
Copy-Item .env.example .env
```

Required for live scan:

- `FMP_API_KEY`: US stock fundamentals, prices, valuation, and earnings.
- `COINGECKO_API_KEY`: crypto market cap, rank, and liquidity. Demo keys are sent with the `x-cg-demo-api-key` header.

Optional:

- `ALPHAVANTAGE_API_KEY`: weak fallback for stock prices when FMP prices are missing.
- `COINBASE_API_KEY`: optional switch for Coinbase premium checks; public product data is used when available.

Risk config:

- `ADVISOR_ACCOUNT_CAPITAL`: account size used for position sizing.
- `ADVISOR_RISK_FRACTION`: default risk per trade. Keep this between `0.005` and `0.01`.
- `ADVISOR_MAX_DAILY_LOSS_FRACTION`: daily loss limit used in report/risk context.
- `ADVISOR_MAX_WEEKLY_LOSS_FRACTION`: weekly loss limit used in report/risk context.

If keys are missing, `advisor scan` runs with demo data and the report header shows `Data mode: demo` plus `demo_data_not_live`.
Use `python -m advisor config validate --require-live` before trusting a live scan, or `python -m advisor scan --require-live` to fail instead of falling back to demo data.

GitHub Actions uses `.github/workflows/financial-advisor-reports.yml` to run without Codex open:

- main report: weekdays at 11:15 BRT.
- close report: weekdays at 17:15 BRT.
- manual run: Actions -> Financial Advisor Reports -> Run workflow -> choose `report_type`.

Configure repository secrets in GitHub under Settings -> Secrets and variables -> Actions:

- Required: `FMP_API_KEY`, `COINGECKO_API_KEY`.
- Optional: `ALPHAVANTAGE_API_KEY`, `COINBASE_API_KEY`, `ADVISOR_ACCOUNT_CAPITAL`, `ADVISOR_RISK_FRACTION`, `ADVISOR_MAX_DAILY_LOSS_FRACTION`, `ADVISOR_MAX_WEEKLY_LOSS_FRACTION`.

If live validation fails in Actions, the workflow still uploads a `reports/` artifact with `Data mode: blocked` and `Decisao geral: no_trade_day`; it does not create a decision-grade report.
The scheduled Actions workflow omits `--include-discovery` by default to preserve free-tier FMP calls. Use the direct CLI command with `--include-discovery` only when you intentionally want the larger scan.
It also runs with a conservative default budget: `ADVISOR_STOCK_WATCHLIST=MSFT,NVDA`, `ADVISOR_CRYPTO_WATCHLIST=HYPE`, `ADVISOR_MAX_STOCKS_PER_RUN=2`, and `ADVISOR_FMP_CALL_BUDGET_PER_RUN=20`. That keeps scheduled runs around 16 estimated FMP calls each instead of scanning the full list.

Discovery mode:

- `python -m advisor scan --include-discovery` adds curated large/liquid candidates beyond your watchlist.
- Stock discovery currently focuses on large US technology/product names such as `AAPL`, `AVGO`, `GOOGL`, `META`, `AMZN`, `TSM`, `ASML`, `ORCL`, `CRM`, and `NOW`.
- Crypto discovery adds liquid large-cap candidates such as `BNB`, `XRP`, `LINK`, and `AVAX`.
- The bot estimates API usage before a live discovery scan and refuses scans that would exceed configured free-tier limits.

## Data honesty

- Stock growth uses FMP income-statement growth data. Historical PE is the median of available positive annual PE observations.
- `advisor collect-crypto-flow` uses public Binance futures data for BTC/ETH/SOL/ZEC and Hyperliquid `metaAndAssetCtxs` for HYPE.
- Funding rates are compared and reported as 8-hour equivalents. Binance adjusted intervals come from `fundingInfo`; HYPE's hourly Hyperliquid funding rate is multiplied by eight before regime and risk scoring.
- CVD is always labeled as a proxy derived from taker buy/sell volume.
- Liquidation history is optional and may be unavailable or incomplete; the command reports `degraded` instead of inventing a value.
- Missing guidance and post-earnings gap data are rendered as `n/a`, not as `no` or `0%`.
- `advisor backtest` without a fixture is demo-only and hides win rates. A fixture needs at least 30 similar setups to show +2R win rate and 60 to show +3R.
