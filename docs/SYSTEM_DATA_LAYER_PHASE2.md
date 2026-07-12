# System Data Layer -- Phase 2

## Scope and invariants

Phase 2 separates collection provenance from the trading decision path. It does
not change scoring, classification, gates, risk sizing, strategy, backtests,
Telegram output, or analyst review. A missing, stale, restricted, or
unimplemented data source remains a provenance/status condition for the
existing downstream controls to interpret.

The audit command writes four sanitized JSON summaries alongside the existing
audit artifacts. They are generated from loader/cache metadata, never from a
new production-cache write. `--no-network` performs only read-only inspection
of the source cache and does not construct the live loader or create an audit
database.

## Corrected timestamp and cache semantics

`original_fetched_at` is the original time a cache entry was written. Cache
age is measured from that time; reading the entry never refreshes it.
`source_data_latest_timestamp` is the most recent timestamp inside the
provider payload. It is distinct from both fetch time and cache age. Where
available, source age is reported separately.

Daily/EOD candles are explicitly represented as daily, EOD market data. They
are not quotes and must not be labelled intraday. Equity quotes are reported
separately from the historical candle series. Crypto daily candle provenance is
kept separate from the shorter-lived funding, open-interest, CVD, premium, and
liquidation metric provenance.

## Providers, endpoints, and fallbacks

The capability matrix distinguishes configuration, plan support,
implementation, most recent status, and fallback availability. It represents
`not_configured`, `not_implemented`, `unsupported_by_plan`,
`temporarily_unavailable`, `rate_limited`, `available`, and `partial` without
converting them into a trading recommendation.

- FMP supports the implemented stock EOD price, fundamentals, and earnings
  endpoints when the configured plan allows them. The historical full-price
  path can fall back to the light-price path; a 402 is recorded as
  `unsupported_by_plan`, not as a successful response.
- Yahoo and Stooq are implemented EOD stock-price fallbacks. CoinGecko
  supplies daily crypto market/chart data. Binance supplies daily klines and
  implemented derivatives-flow metrics. Hyperliquid supplies its implemented
  price and flow contexts. Coinbase supplies its public product price. Alpha
  Vantage and SEC are represented when configured/available.
- Provider URLs are emitted only in sanitized form. API keys, tokens, and
  secrets are replaced with `REDACTED`; credentials are never written to the
  Phase 2 artifacts.

“Available” in a no-network artifact means configured and implemented in the
registry; it is not evidence of a fresh successful request. A bounded live
audit remains the separate validation workflow when external calls are
authorized.

## Intentionally unimplemented items

The invalid Binance liquidation-order request is deliberately not called.
Liquidations therefore remain `not_implemented`, with no fabricated value.
Hyperliquid's independent CVD and open-interest-change metrics are likewise
represented as not implemented. Guidance and macro-regime collectors are also
explicitly not implemented. These records appear in both the provider
validation and capability-matrix artifacts.

## Phase 2 artifacts

- `phase2-provider-validation.json`: configured providers, endpoint shape,
  capability/status records, request restriction/fallback causes, and known
  invalid or unimplemented endpoints.
- `phase2-cache-validation.json`: read-only cache rows with original fetched
  time, cache age/expiry, source timestamp, and source age as separate fields.
- `phase2-source-timestamps.json`: stock EOD candles versus live quotes, and
  crypto daily candles versus intraday metric provenance.
- `phase2-capability-matrix.json`: configured, plan support, implementation,
  last status, fallback availability, and intentional omissions.

Each file has `schema_version: "phase2-v1"`, a UTC generation timestamp, and
the audit network mode. Provider/capability rows are ordered deterministically;
timestamps and cache ages naturally reflect the audit time.

## Expected impact and Phase 3 deferrals

The immediate effect is diagnostic clarity: operators can identify plan
restrictions, fallback use, cache freshness, source age, and missing collectors
without mistaking EOD data for live data or exposing credentials. It does not
make unavailable data available and does not relax any fail-closed decision
logic.

Phase 3 may add an independently validated liquidation data provider, guidance
and macro collectors, richer intraday quote coverage, and a controlled live
audit comparison history. Those additions require separate data contracts and
tests before they can affect any decision-facing behavior.
