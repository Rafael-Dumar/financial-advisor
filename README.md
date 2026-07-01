# Financial Advisor V1

Local personal assistant for investment quality and swing trade analysis.

This v1 is intentionally simple:

- Python standard library only.
- CLI first.
- SQLite cache and report history.
- Markdown and HTML reports.
- No automatic order execution.
- No dashboard.
- Telegram is optional and reserved for the nightly final review summary.

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
python -m advisor report close --from-main --require-live
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

- `ALPHAVANTAGE_API_KEY`: weak fallback for stock prices when FMP prices are missing, plus optional aggregated news/sentiment checks.
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
- Optional: `ALPHAVANTAGE_API_KEY`, `COINBASE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ADVISOR_ACCOUNT_CAPITAL`, `ADVISOR_RISK_FRACTION`, `ADVISOR_MAX_DAILY_LOSS_FRACTION`, `ADVISOR_MAX_WEEKLY_LOSS_FRACTION`.

If live validation fails in Actions, the workflow still uploads a `reports/` artifact with `Data mode: blocked` and `Decisao geral: no_trade_day`; it does not create a decision-grade report.
The scheduled main workflow runs with `--include-discovery`, but `ADVISOR_MAX_STOCKS_PER_RUN=11` keeps the stock scan focused on the configured watchlist before discovery extras are added.
It also runs with a per-run FMP budget of `ADVISOR_FMP_CALL_BUDGET_PER_RUN=90`, which is designed to cover the 11 stock watchlist plus benchmarks while staying below the free daily quota when the close report reuses the same-day main cache.
Telegram delivery is handled by `.github/workflows/financial-advisor-nightly-review.yml`, which sends only the final `Telegram summary` from `reports/analyst-final-review.md`.

Discovery mode:

- `python -m advisor scan --include-discovery` adds curated large/liquid candidates beyond your watchlist.
- Stock discovery currently focuses on large US technology/product names such as `AAPL`, `AVGO`, `GOOGL`, `META`, `AMZN`, `TSM`, `ASML`, `ORCL`, `CRM`, and `NOW`.
- Crypto discovery adds liquid large-cap candidates such as `BNB`, `XRP`, `LINK`, and `AVAX`.
- The bot estimates API usage before a live discovery scan and refuses scans that would exceed configured free-tier limits.

## Data honesty

- Stock growth uses FMP income-statement growth data. Historical PE is the median of available positive annual PE observations.
- If `ALPHAVANTAGE_API_KEY` is configured, the live loader collects one cached News Sentiment payload for the configured stock and crypto universe and attaches relevant items to each asset. Missing or rate-limited news remains `not_verified`; news context never approves an automatic trade by itself.
- `advisor collect-crypto-flow` uses public Binance futures data for BTC/ETH/SOL/ZEC and Hyperliquid `metaAndAssetCtxs` for HYPE.
- Funding rates are compared and reported as 8-hour equivalents. Binance adjusted intervals come from `fundingInfo`; HYPE's hourly Hyperliquid funding rate is multiplied by eight before regime and risk scoring.
- CVD is always labeled as a proxy derived from taker buy/sell volume.
- Liquidation history is optional and may be unavailable or incomplete; the command reports `degraded` instead of inventing a value.
- Missing guidance and post-earnings gap data are rendered as `n/a`, not as `no` or `0%`.
- `advisor backtest` without a fixture is demo-only and hides win rates. A fixture needs at least 30 similar setups to show +2R win rate and 60 to show +3R.
