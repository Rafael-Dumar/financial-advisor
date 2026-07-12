from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from advisor.cache import ApiLimiter, SQLiteCache
from advisor.config import AdvisorConfig
from advisor.data_pipeline import (
    binance_crypto_flow_from_payloads,
    binance_funding_rate_8h_from_payloads,
    crypto_snapshot_from_payloads,
    fmp_historical_from_alphavantage,
    hyperliquid_crypto_flow_from_payload,
    stock_snapshot_from_payloads,
)
from advisor.data_sources import AlphaVantageSource, BinanceSource, CoinbaseSource, CoinGeckoSource, FmpSource, HyperliquidSource, SecEdgarSource, StooqSource, YahooChartSource
from advisor.http_client import fetch_json, fetch_text
from advisor.models import AssetSnapshot, Candle, DataFetchMetadata, ProviderCapability


FetchJson = Callable[..., Any]
FetchText = Callable[..., str]

CRYPTO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "HYPE": "hyperliquid",
    "ZEC": "zcash",
}

THEMES = {
    "AAPL": "hardware",
    "AMZN": "cloud_ecommerce",
    "ASML": "semiconductors",
    "AVGO": "semiconductors",
    "INTC": "semiconductors",
    "AMD": "semiconductors",
    "NVDA": "semiconductors",
    "MU": "semiconductors",
    "GOOGL": "software_ai",
    "META": "software_ai",
    "MSFT": "software",
    "MSTR": "crypto",
    "DELL": "hardware",
    "HIMS": "healthcare",
    "USAR": "materials",
    "CRDO": "semiconductors",
    "CRM": "software",
    "NOW": "software",
    "ORCL": "software",
    "TSM": "semiconductors",
    "BTC": "crypto",
    "ETH": "crypto",
    "SOL": "crypto",
    "HYPE": "crypto",
    "ZEC": "crypto",
}

SECTOR_BENCHMARKS = {
    "semiconductors": "SMH",
    "software": "IGV",
    "software_ai": "IGV",
    "healthcare": "XLV",
}

SEC_CIKS = {
    "AMD": "0000002488",
    "CRDO": "0001807794",
    "DELL": "0001571996",
    "HIMS": "0001773751",
    "HOOD": "0001783879",
    "INTC": "0000050863",
    "MRVL": "0001835632",
    "MSFT": "0000789019",
    "MU": "0000723125",
    "NVDA": "0001045810",
}


class LiveDataLoader:
    def __init__(
        self,
        config: AdvisorConfig,
        *,
        fetch_json: FetchJson = fetch_json,
        fetch_text: FetchText = fetch_text,
        today: str | None = None,
        db_path: Path | str | None = None,
        audit_recorder: Any | None = None,
        http_observer: Any | None = None,
    ):
        self.config = config
        self.fetch_json = fetch_json
        self.fetch_text = fetch_text
        self.today = today or datetime.now(timezone.utc).date().isoformat()
        self.cache = SQLiteCache(db_path) if db_path is not None else None
        self.limiter = ApiLimiter(db_path) if db_path is not None else None
        self._alphavantage_fallback_symbols: set[str] = set()
        self._fmp_price_unavailable_symbols: set[str] = set()
        self._fmp_price_light_fallback_symbols: set[str] = set()
        self._yahoo_fallback_symbols: set[str] = set()
        self._stooq_fallback_symbols: set[str] = set()
        self._stock_quotes: dict[str, dict[str, object]] = {}
        self.quote_provider_status = "not_requested"
        self.benchmark_status: dict[str, dict[str, object]] = {}
        self._last_snapshots: list[AssetSnapshot] | None = None
        self._binance_funding_info_loaded = False
        self._binance_funding_info: Any = []
        self.cache_hits = 0
        self.cache_misses = 0
        self.provider_call_counts: dict[str, int] = {}
        self.provider_statuses: dict[str, str] = {}
        self.provider_capabilities: dict[tuple[str, str], ProviderCapability] = {}
        self._plan_restricted_capabilities: set[tuple[str, str]] = set()
        self.provider_retry_after: dict[str, str] = {}
        self.skipped_provider_calls_due_to_rate_limit: dict[str, int] = {}
        self.audit_recorder = audit_recorder
        self.http_observer = http_observer
        self._last_audit_call_id: str | None = None
        self._last_fetch_metadata: DataFetchMetadata | None = None
        self._earnings_status_by_symbol: dict[str, str] = {}
        self._news_status_by_symbol: dict[str, str] = {}
        self._sec_filings_status_by_symbol: dict[str, str] = {}
        self.fmp = FmpSource(config.fmp_api_key)
        self.alphavantage = AlphaVantageSource(config.alphavantage_api_key)
        self.binance = BinanceSource()
        self.coinbase = CoinbaseSource()
        self.coingecko = CoinGeckoSource(config.coingecko_api_key)
        self.hyperliquid = HyperliquidSource()
        self.stooq = StooqSource()
        self.yahoo = YahooChartSource()
        self.sec = SecEdgarSource()

    def load_snapshots(self, *, include_discovery: bool = False) -> list[AssetSnapshot]:
        snapshots = []
        stock_symbols, crypto_symbols = self.config.symbols_for_scan(include_discovery=include_discovery)
        news_by_symbol = self._news_events_by_symbol([*stock_symbols, *crypto_symbols])
        sec_by_symbol = self._sec_events_by_symbol(stock_symbols)
        self._load_stock_quotes(stock_symbols)
        for symbol in stock_symbols:
            snapshots.append(
                self.load_stock(
                    symbol,
                    news_events=[*news_by_symbol.get(symbol, []), *sec_by_symbol.get(symbol, [])],
                    quote=self._stock_quotes.get(symbol),
                    news_status=self._news_status_by_symbol.get(symbol, "not_configured"),
                    sec_filings_status=self._sec_filings_status_by_symbol.get(symbol, "not_implemented"),
                )
            )
        for symbol in crypto_symbols:
            snapshots.append(
                self.load_crypto(
                    symbol,
                    news_events=news_by_symbol.get(symbol, []),
                    news_status=self._news_status_by_symbol.get(symbol, "not_configured"),
                )
            )
        self._last_snapshots = snapshots
        return snapshots

    def load_benchmarks(self) -> dict[str, list[Candle]]:
        benchmarks: dict[str, list[Candle]] = {}
        self.benchmark_status = {}
        for symbol in ["SPY", "QQQ", "SMH", "IGV", "XLV"]:
            try:
                historical_payload = self._fetch("fmp", "prices", self.fmp.historical_prices_url(symbol))
            except RuntimeError as error:
                if _is_fmp_price_unavailable(error):
                    benchmarks[symbol] = []
                    self.benchmark_status[symbol] = _benchmark_status(symbol, [])
                    continue
                raise
            candles = stock_snapshot_from_payloads(
                symbol=symbol,
                theme="benchmark",
                historical_payload=historical_payload,
                profile_payload=[],
                ratios_payload=[],
                metrics_payload=[],
                historical_metrics_payload=[],
                growth_payload=[],
                earnings_payload=[],
                today=self.today,
            ).candles
            benchmarks[symbol] = candles
            self.benchmark_status[symbol] = _benchmark_status(symbol, candles)
        self._attach_benchmark_provenance()
        return benchmarks

    def collect_crypto_flow(self, symbols: list[str] | None = None) -> dict[str, dict[str, Any]]:
        flows: dict[str, dict[str, Any]] = {}
        for symbol in symbols or self.config.crypto_watchlist:
            if symbol == "HYPE":
                payload = self._fetch_optional(
                    "hyperliquid",
                    "crypto_flow",
                    self.hyperliquid.info_url(),
                    payload=self.hyperliquid.meta_and_asset_contexts_payload(),
                    default=[],
                    symbol=symbol,
                )
                flows[symbol] = hyperliquid_crypto_flow_from_payload(payload, symbol=symbol)
                continue
            binance_symbol = f"{symbol}USDT"
            flows[symbol] = binance_crypto_flow_from_payloads(
                funding_payload=self._fetch_optional(
                    "binance",
                    "crypto_flow",
                    self.binance.funding_rate_url(binance_symbol),
                    default=[],
                ),
                funding_info_payload=self._binance_funding_info_payload(),
                symbol=binance_symbol,
                open_interest_payload=self._fetch_optional(
                    "binance",
                    "crypto_flow",
                    self.binance.open_interest_history_url(binance_symbol),
                    default=[],
                ),
                current_open_interest_payload=self._fetch_optional(
                    "binance",
                    "crypto_flow",
                    self.binance.open_interest_url(binance_symbol),
                    default={},
                ),
                taker_payload=self._fetch_optional(
                    "binance",
                    "crypto_flow",
                    self.binance.taker_long_short_url(binance_symbol),
                    default=[],
                ),
                liquidation_payload=[],
            )
        return flows

    def _load_stock_quotes(self, symbols: list[str]) -> None:
        self._stock_quotes = {}
        if not symbols:
            self.quote_provider_status = "not_requested"
            return
        try:
            payload = self._fetch(
                "fmp",
                "prices",
                self.fmp.batch_quote_url(symbols),
                symbol=",".join(symbols),
            )
        except RuntimeError as error:
            if not _is_degradable_fetch_error(error):
                raise
            self.quote_provider_status = "unavailable"
            self._stock_quotes = {symbol: _quote_unavailable() for symbol in symbols}
            return

        rows = payload if isinstance(payload, list) else []
        by_symbol = {
            str(row.get("symbol") or "").upper(): row
            for row in rows
            if isinstance(row, dict) and row.get("symbol")
        }
        self.quote_provider_status = "available" if by_symbol else "unavailable"
        self._stock_quotes = {
            symbol: _quote_from_row(by_symbol.get(symbol))
            for symbol in symbols
        }

    def _attach_benchmark_provenance(self) -> None:
        if self._last_snapshots is None:
            return
        for index, snapshot in enumerate(self._last_snapshots):
            if snapshot.asset_type != "stock":
                continue
            sector_symbol = SECTOR_BENCHMARKS.get(snapshot.theme)
            market = dict(self.benchmark_status.get("SPY", _benchmark_status("SPY", [])))
            sector = dict(self.benchmark_status.get(sector_symbol, _benchmark_status(sector_symbol, []))) if sector_symbol else None
            asset_change = _daily_change_pct(snapshot.candles)
            sector_change = sector.get("daily_change_pct") if sector else None
            relative_strength = (
                asset_change - float(sector_change)
                if asset_change is not None and isinstance(sector_change, (int, float))
                else None
            )
            self._last_snapshots[index] = replace(
                snapshot,
                benchmark_provenance={
                    "market": market,
                    "sector": sector,
                    "asset_daily_change_pct": asset_change,
                    "relative_strength_pct": relative_strength,
                },
            )

    def load_stock(
        self,
        symbol: str,
        *,
        news_events: list[dict[str, Any]] | None = None,
        quote: dict[str, object] | None = None,
        news_status: str = "not_configured",
        sec_filings_status: str = "not_implemented",
    ) -> AssetSnapshot:
        historical_payload = self._stock_historical_payload(symbol)
        price_metadata = self._last_fetch_metadata
        missing_data = _stock_missing_data(
            symbol,
            alphavantage_fallback_symbols=self._alphavantage_fallback_symbols,
            fmp_price_unavailable_symbols=self._fmp_price_unavailable_symbols,
            fmp_price_light_fallback_symbols=self._fmp_price_light_fallback_symbols,
            yahoo_fallback_symbols=self._yahoo_fallback_symbols,
            stooq_fallback_symbols=self._stooq_fallback_symbols,
        )
        data_source = _stock_data_source(symbol, missing_data)
        if symbol in self._fmp_price_unavailable_symbols and not _has_price_history(historical_payload):
            return stock_snapshot_from_payloads(
                symbol=symbol,
                theme=THEMES.get(symbol, "unknown"),
                historical_payload=historical_payload,
                profile_payload=[],
                ratios_payload=[],
                metrics_payload=[],
                historical_metrics_payload=[],
                growth_payload=[],
                earnings_payload=[],
                today=self.today,
                missing_data=missing_data,
                data_source=data_source,
                data_fetch_metadata=price_metadata,
                news_events=news_events,
                provider_capabilities=self._snapshot_capabilities(),
                earnings_status=self._earnings_status_by_symbol.get(symbol),
                news_status=news_status,
                sec_filings_status=sec_filings_status,
                **(quote or _quote_not_requested()),
            )
        profile_payload = self._fetch_optional_stock_payload(
            symbol,
            "fundamentals",
            self.fmp.profile_url(symbol),
            missing_data=missing_data,
        )
        ratios_payload = self._fetch_optional_stock_payload(
            symbol,
            "fundamentals",
            self.fmp.ratios_url(symbol),
            missing_data=missing_data,
        )
        metrics_payload = self._fetch_optional_stock_payload(
            symbol,
            "fundamentals",
            self.fmp.key_metrics_url(symbol),
            missing_data=missing_data,
        )
        historical_metrics_payload = self._fetch_optional_stock_payload(
            symbol,
            "fundamentals",
            self.fmp.historical_key_metrics_url(symbol),
            missing_data=missing_data,
        )
        growth_payload = self._fetch_optional_stock_payload(
            symbol,
            "fundamentals",
            self.fmp.income_statement_growth_url(symbol),
            missing_data=missing_data,
        )
        earnings_payload = self._fetch_optional_stock_payload(
            symbol,
            "earnings",
            self.fmp.earnings_calendar_url(symbol),
            missing_data=missing_data,
        )
        return stock_snapshot_from_payloads(
            symbol=symbol,
            theme=THEMES.get(symbol, "unknown"),
            historical_payload=historical_payload,
            profile_payload=profile_payload,
            ratios_payload=ratios_payload,
            metrics_payload=metrics_payload,
            historical_metrics_payload=historical_metrics_payload,
            growth_payload=growth_payload,
            earnings_payload=earnings_payload,
            today=self.today,
            missing_data=missing_data,
            data_source=data_source,
            data_fetch_metadata=price_metadata,
            news_events=news_events,
            provider_capabilities=self._snapshot_capabilities(),
            earnings_status=self._earnings_status_by_symbol.get(symbol),
            news_status=news_status,
            sec_filings_status=sec_filings_status,
            **(quote or _quote_not_requested()),
        )

    def load_crypto(
        self,
        symbol: str,
        *,
        news_events: list[dict[str, Any]] | None = None,
        news_status: str = "not_configured",
    ) -> AssetSnapshot:
        market_payload = self._market_payload(symbol)
        if symbol == "HYPE":
            klines_payload = self._hyperliquid_klines(symbol)
            price_metadata = self._last_fetch_metadata
            context_payload = self._fetch_optional(
                "hyperliquid",
                "crypto_flow",
                self.hyperliquid.info_url(),
                payload=self.hyperliquid.meta_and_asset_contexts_payload(),
                default=[],
                symbol=symbol,
            )
            flow = hyperliquid_crypto_flow_from_payload(context_payload, symbol=symbol)
            funding_payload = (
                [{"fundingRate": flow["funding_rate"]}]
                if flow["funding_rate"] is not None
                else []
            )
            open_interest_payload = (
                {"openInterest": flow["open_interest"]}
                if flow["open_interest"] is not None
                else {}
            )
            taker_payload: list[dict[str, Any]] = []
            liquidation_payload: list[dict[str, Any]] = []
            coinbase_payload: dict[str, Any] = {}
            crypto_metric_provenance = {
                **flow["metric_provenance"],
                "candles": {
                    "provider": "hyperliquid",
                    "endpoint": "candleSnapshot",
                    "status": "available" if klines_payload else "provider_unavailable",
                    "granularity": "daily",
                    "market_data_kind": "eod_candle",
                },
                "spot": {
                    "provider": "hyperliquid",
                    "endpoint": "candleSnapshot",
                    "status": "available" if klines_payload else "provider_unavailable",
                    "granularity": "daily",
                    "market_data_kind": "eod_candle",
                },
            }
        else:
            binance_symbol = f"{symbol}USDT"
            try:
                klines_payload = self._fetch("binance", "prices", self.binance.klines_url(binance_symbol))
            except RuntimeError as error:
                if _is_binance_restricted_location(error):
                    parent_call_id = self._last_audit_call_id
                    coingecko_klines = self._coingecko_market_chart_klines(
                        symbol,
                        parent_call_id=parent_call_id,
                        fallback_from="binance",
                        fallback_reason="binance_restricted_location",
                    )
                    price_metadata = self._last_fetch_metadata
                    flow = self._hyperliquid_flow(
                        symbol,
                        parent_call_id=parent_call_id,
                        fallback_from="binance",
                        fallback_reason="binance_flow_unavailable",
                    )
                    missing_data = ["binance_restricted_location", "binance_flow_unavailable"]
                    if flow.get("funding_rate") is not None or flow.get("open_interest") is not None:
                        missing_data.append("hyperliquid_flow_fallback")
                    if coingecko_klines:
                        missing_data.append("coingecko_price_history_fallback")
                    else:
                        missing_data.append("price_history_unavailable")
                    return crypto_snapshot_from_payloads(
                        symbol=symbol,
                        theme=THEMES.get(symbol, "crypto"),
                        klines_payload=coingecko_klines,
                        market_payload=market_payload,
                        funding_payload=(
                            [{"fundingRate": flow["funding_rate"]}]
                            if flow.get("funding_rate") is not None
                            else []
                        ),
                        open_interest_payload=(
                            {"openInterest": flow["open_interest"]}
                            if flow.get("open_interest") is not None
                            else {}
                        ),
                        taker_payload=[],
                        coinbase_payload=self._coinbase_payload(symbol),
                        liquidation_payload=[],
                        missing_data=missing_data,
                        data_source="coingecko_fallback",
                        data_fetch_metadata=price_metadata,
                        news_events=news_events,
                        crypto_metric_provenance={
                            **flow["metric_provenance"],
                            "candles": {
                                "provider": "coingecko",
                                "endpoint": "market_chart",
                                "status": "available" if coingecko_klines else "provider_unavailable",
                                "granularity": "daily",
                                "market_data_kind": "eod_candle",
                            },
                            "spot": {
                                "provider": "coingecko",
                                "endpoint": "market_chart",
                                "status": "available" if coingecko_klines else "provider_unavailable",
                                "granularity": "daily",
                                "market_data_kind": "eod_candle",
                            },
                        },
                    )
                raise
            price_metadata = self._last_fetch_metadata
            raw_funding_payload = self._fetch("binance", "crypto_flow", self.binance.funding_rate_url(binance_symbol))
            funding_rate = binance_funding_rate_8h_from_payloads(
                funding_payload=raw_funding_payload,
                funding_info_payload=self._binance_funding_info_payload(),
                symbol=binance_symbol,
            )
            funding_payload = [{"fundingRate": funding_rate}] if funding_rate is not None else []
            open_interest_payload = self._fetch(
                "binance",
                "crypto_flow",
                self.binance.open_interest_history_url(binance_symbol),
            )
            current_open_interest_payload = self._fetch_optional(
                "binance",
                "crypto_flow",
                self.binance.open_interest_url(binance_symbol),
                default={},
            )
            taker_payload = self._fetch("binance", "crypto_flow", self.binance.taker_long_short_url(binance_symbol))
            liquidation_payload: list[dict[str, Any]] = []
            coinbase_payload = self._coinbase_payload(symbol)
            crypto_metric_provenance = binance_crypto_flow_from_payloads(
                funding_payload=funding_payload,
                open_interest_payload=open_interest_payload,
                current_open_interest_payload=current_open_interest_payload,
                taker_payload=taker_payload,
                liquidation_payload=liquidation_payload,
            )["metric_provenance"]
        return crypto_snapshot_from_payloads(
            symbol=symbol,
            theme=THEMES.get(symbol, "crypto"),
            klines_payload=klines_payload,
            market_payload=market_payload,
            funding_payload=funding_payload,
            open_interest_payload=open_interest_payload,
            taker_payload=taker_payload,
            coinbase_payload=coinbase_payload,
            liquidation_payload=liquidation_payload,
            data_source="hyperliquid" if symbol == "HYPE" else "binance/coingecko",
            data_fetch_metadata=price_metadata,
            news_events=news_events,
            provider_capabilities=self._snapshot_capabilities(),
            news_status=news_status,
            crypto_metric_provenance=crypto_metric_provenance,
        )

    def _news_events_by_symbol(self, symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not self.config.alphavantage_api_key or not symbols:
            self._set_capability_status("alphavantage", "news_sentiment", "not_configured", configured=False)
            self._news_status_by_symbol = {symbol: "not_configured" for symbol in symbols}
            return {symbol: [] for symbol in symbols}
        tickers = [_alphavantage_news_ticker(symbol) for symbol in symbols]
        try:
            payload = self._fetch("alphavantage", "news", self.alphavantage.news_sentiment_url(tickers))
        except RuntimeError as error:
            if not _is_degradable_fetch_error(error):
                raise
            status = _normalized_status_from_error(error)
            self._news_status_by_symbol = {symbol: status for symbol in symbols}
            return {symbol: [] for symbol in symbols}
        status = "available" if isinstance(payload, dict) and isinstance(payload.get("feed"), list) else "partial"
        self._news_status_by_symbol = {symbol: status for symbol in symbols}
        return _news_events_from_alphavantage(payload, symbols)

    def _sec_events_by_symbol(self, symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
        events: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
        for symbol in symbols:
            cik = SEC_CIKS.get(symbol)
            if not cik:
                self._sec_filings_status_by_symbol[symbol] = "not_implemented"
                continue
            try:
                payload = self._fetch("sec", "news", self.sec.submissions_url(cik), symbol=symbol)
            except RuntimeError as error:
                if not _is_degradable_fetch_error(error):
                    raise
                self._sec_filings_status_by_symbol[symbol] = _normalized_status_from_error(error)
                continue
            self._sec_filings_status_by_symbol[symbol] = "available" if isinstance(payload, dict) else "partial"
            events[symbol] = _sec_events_from_submissions(payload, symbol=symbol, today=self.today)
        return events

    def _binance_funding_info_payload(self) -> Any:
        if not self._binance_funding_info_loaded:
            self._binance_funding_info = self._fetch_optional(
                "binance",
                "crypto_flow",
                self.binance.funding_info_url(),
                default=[],
            )
            self._binance_funding_info_loaded = True
        return self._binance_funding_info

    def _market_payload(self, symbol: str) -> dict[str, Any]:
        coin_id = CRYPTO_IDS.get(symbol, symbol.lower())
        markets = self._fetch("coingecko", "fundamentals", self.coingecko.markets_url([coin_id]))
        if isinstance(markets, list) and markets:
            return markets[0]
        return {}

    def _coingecko_market_chart_klines(
        self,
        symbol: str,
        *,
        parent_call_id: str | None = None,
        fallback_from: str | None = None,
        fallback_reason: str | None = None,
    ) -> list[list[Any]]:
        coin_id = CRYPTO_IDS.get(symbol, symbol.lower())
        payload = self._fetch_optional(
            "coingecko",
            "prices",
            self.coingecko.market_chart_url(coin_id),
            default={},
            parent_call_id=parent_call_id,
            attempt_number=2 if parent_call_id else 1,
            fallback_from=fallback_from,
            fallback_to="coingecko" if fallback_from else None,
            fallback_reason=fallback_reason,
            symbol=symbol,
        )
        return _coingecko_market_chart_to_klines(payload)

    def _hyperliquid_klines(self, symbol: str) -> list[list[Any]]:
        end_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_time_ms = end_time_ms - (220 * 24 * 60 * 60 * 1000)
        payload = self.hyperliquid.candle_snapshot_payload(
            symbol,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
        )
        rows = self._fetch("hyperliquid", "prices", self.hyperliquid.info_url(), payload=payload)
        return [
            [row["t"], row["o"], row["h"], row["l"], row["c"], row.get("v", 0)]
            for row in rows
        ]

    def _hyperliquid_flow(
        self,
        symbol: str,
        *,
        parent_call_id: str | None = None,
        fallback_from: str | None = None,
        fallback_reason: str | None = None,
    ) -> dict[str, Any]:
        payload = self._fetch_optional(
            "hyperliquid",
            "crypto_flow",
            self.hyperliquid.info_url(),
            payload=self.hyperliquid.meta_and_asset_contexts_payload(),
            default=[],
            parent_call_id=parent_call_id,
            attempt_number=2 if parent_call_id else 1,
            fallback_from=fallback_from,
            fallback_to="hyperliquid" if fallback_from else None,
            fallback_reason=fallback_reason,
            symbol=symbol,
        )
        return hyperliquid_crypto_flow_from_payload(payload, symbol=symbol)

    def _coinbase_payload(self, symbol: str) -> dict[str, Any]:
        return self._fetch_optional(
            "coinbase",
            "prices",
            self.coinbase.public_product_url(f"{symbol}-USD"),
            default={},
        )

    def _fetch_optional_stock_payload(
        self,
        symbol: str,
        namespace: str,
        url: str,
        *,
        missing_data: list[str],
    ) -> list[dict[str, Any]]:
        try:
            payload = self._fetch("fmp", namespace, url, symbol=symbol)
        except RuntimeError as error:
            if _is_degradable_fetch_error(error):
                if namespace == "earnings":
                    missing_data.append("earnings_unavailable")
                    self._earnings_status_by_symbol[symbol] = _earnings_status_from_error(error)
                else:
                    missing_data.append("fundamentals_unavailable")
                return []
            raise
        if isinstance(payload, list):
            return payload
        if namespace == "earnings":
            self._earnings_status_by_symbol[symbol] = "schema_error"
        return []

    def _stock_historical_payload(self, symbol: str) -> dict[str, Any]:
        try:
            payload = self._fetch("fmp", "prices", self.fmp.historical_prices_url(symbol))
        except RuntimeError as error:
            parent_call_id = self._last_audit_call_id
            if _is_fmp_price_unavailable(error):
                light_payload = self._fetch_optional(
                    "fmp",
                    "prices",
                    self.fmp.historical_prices_light_url(symbol),
                    default=[],
                    parent_call_id=parent_call_id,
                    attempt_number=2,
                    fallback_from="fmp",
                    fallback_to="fmp",
                    fallback_reason="fmp_price_endpoint_unavailable",
                )
                if _has_price_history(light_payload):
                    self._fmp_price_light_fallback_symbols.add(symbol)
                    return light_payload
                yahoo_payload = self._yahoo_historical_payload(
                    symbol,
                    parent_call_id=self._last_audit_call_id,
                    attempt_number=3,
                    fallback_from="fmp",
                    fallback_reason="fmp_light_empty",
                )
                if _has_price_history(yahoo_payload):
                    self._yahoo_fallback_symbols.add(symbol)
                    return yahoo_payload
                stooq_payload = self._stooq_historical_payload(
                    symbol,
                    parent_call_id=self._last_audit_call_id,
                    attempt_number=4,
                    fallback_from="yahoo",
                    fallback_reason="yahoo_empty",
                )
                if _has_price_history(stooq_payload):
                    self._stooq_fallback_symbols.add(symbol)
                    return stooq_payload
                self._fmp_price_unavailable_symbols.add(symbol)
                if not self.config.alphavantage_api_key:
                    return []
            elif _is_fmp_rate_limited(error):
                fallback_payload = self._stock_price_fallback_payload(symbol, parent_call_id=parent_call_id)
                if _has_price_history(fallback_payload):
                    return fallback_payload
                self._fmp_price_unavailable_symbols.add(symbol)
                raise
            else:
                raise
            payload = {}
        if _has_price_history(payload) or not self.config.alphavantage_api_key:
            return payload
        alpha_payload = self._fetch(
            "alphavantage",
            "prices",
            self.alphavantage.daily_adjusted_url(symbol),
            parent_call_id=self._last_audit_call_id,
            attempt_number=2,
            fallback_from="fmp",
            fallback_to="alphavantage",
            fallback_reason="fmp_price_empty",
            symbol=symbol,
        )
        self._alphavantage_fallback_symbols.add(symbol)
        return fmp_historical_from_alphavantage(alpha_payload)

    def _stock_price_fallback_payload(
        self,
        symbol: str,
        *,
        parent_call_id: str | None = None,
    ) -> dict[str, Any]:
        if self.config.alphavantage_api_key:
            try:
                alpha_payload = self._fetch(
                    "alphavantage",
                    "prices",
                    self.alphavantage.daily_adjusted_url(symbol),
                    parent_call_id=parent_call_id,
                    attempt_number=2,
                    fallback_from="fmp",
                    fallback_to="alphavantage",
                    fallback_reason="fmp_rate_limited",
                    symbol=symbol,
                )
            except RuntimeError:
                alpha_payload = {}
            if alpha_payload:
                converted = fmp_historical_from_alphavantage(alpha_payload)
                if _has_price_history(converted):
                    self._alphavantage_fallback_symbols.add(symbol)
                    return converted
        yahoo_payload = self._yahoo_historical_payload(
            symbol,
            parent_call_id=self._last_audit_call_id,
            attempt_number=3,
            fallback_from="alphavantage",
            fallback_reason="alphavantage_empty_or_failed",
        )
        if _has_price_history(yahoo_payload):
            self._yahoo_fallback_symbols.add(symbol)
            return yahoo_payload
        stooq_payload = self._stooq_historical_payload(
            symbol,
            parent_call_id=self._last_audit_call_id,
            attempt_number=4,
            fallback_from="yahoo",
            fallback_reason="yahoo_empty",
        )
        if _has_price_history(stooq_payload):
            self._stooq_fallback_symbols.add(symbol)
            return stooq_payload
        return {"historical": []}

    def _yahoo_historical_payload(
        self,
        symbol: str,
        *,
        parent_call_id: str | None = None,
        attempt_number: int = 1,
        fallback_from: str | None = None,
        fallback_reason: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        payload = self._fetch_optional(
            "yahoo",
            "prices",
            self.yahoo.daily_chart_url(symbol),
            default={},
            parent_call_id=parent_call_id,
            attempt_number=attempt_number,
            fallback_from=fallback_from,
            fallback_to="yahoo" if fallback_from else None,
            fallback_reason=fallback_reason,
            symbol=symbol,
        )
        return yahoo_historical_from_chart(payload)

    def _stooq_historical_payload(
        self,
        symbol: str,
        *,
        parent_call_id: str | None = None,
        attempt_number: int = 1,
        fallback_from: str | None = None,
        fallback_reason: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        url = self.stooq.daily_csv_url(symbol)
        call_id = self._start_audit_call(
            "stooq",
            "prices",
            url,
            symbol=symbol,
            parent_call_id=parent_call_id,
            attempt_number=attempt_number,
            fallback_from=fallback_from,
            fallback_to="stooq" if fallback_from else None,
            fallback_reason=fallback_reason,
        )
        try:
            if self.http_observer is None:
                csv_text = self.fetch_text(url, headers={"User-Agent": "financial-advisor-v1"})
            else:
                csv_text = self.fetch_text(
                    url,
                    headers={"User-Agent": "financial-advisor-v1"},
                    observer=self.http_observer,
                )
        except RuntimeError as error:
            self._finish_audit_call(call_id, error=str(error))
            return {"historical": []}
        self._finish_audit_call(call_id, response=csv_text)
        historical_payload = stooq_historical_from_csv(csv_text)
        fetched_at = _now_iso()
        self._last_fetch_metadata = _fetch_metadata(
            provider="stooq",
            endpoint="prices",
            payload=historical_payload,
            fetched_at=fetched_at,
            cache_fetched_at=None,
            cache_age_seconds=None,
            is_fresh=True,
            cache_hit=False,
            fallback_from=fallback_from,
            fallback_to="stooq" if fallback_from else None,
        )
        return historical_payload

    def _fetch(
        self,
        provider: str,
        namespace: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        parent_call_id: str | None = None,
        attempt_number: int = 1,
        fallback_from: str | None = None,
        fallback_to: str | None = None,
        fallback_reason: str | None = None,
        symbol: str | None = None,
    ) -> Any:
        key = _cache_key(url, payload)
        capability = _provider_capability_name(provider, namespace, url)
        call_id = self._start_audit_call(
            provider,
            namespace,
            url,
            payload=payload,
            parent_call_id=parent_call_id,
            attempt_number=attempt_number,
            fallback_from=fallback_from,
            fallback_to=fallback_to,
            fallback_reason=fallback_reason,
            symbol=symbol,
        )
        cache_metadata = None
        if self.cache is not None:
            cache_entry = self.cache.get_json_with_metadata(
                namespace,
                key,
                max_age_seconds=self.config.freshness_seconds[namespace],
            )
            if cache_entry is not None:
                cache_metadata = {
                    "fetched_at": cache_entry["fetched_at"],
                    "cache_age_seconds": cache_entry["cache_age_seconds"],
                    "expired": cache_entry["is_expired"],
                }
            cached = cache_entry["payload"] if cache_entry is not None and not cache_entry["is_expired"] else None
            if cached is not None:
                self.cache_hits += 1
                try:
                    _raise_for_provider_error(provider, cached)
                except RuntimeError as error:
                    self._finish_audit_call(call_id, error=str(error), cache_metadata=cache_metadata)
                    raise
                self._finish_audit_call(
                    call_id,
                    response=cached,
                    cache_hit=True,
                    cache_metadata=cache_metadata,
                )
                self._last_fetch_metadata = _fetch_metadata(
                    provider=provider,
                    endpoint=namespace,
                    payload=cached,
                    fetched_at=str(cache_entry["fetched_at"]),
                    cache_fetched_at=str(cache_entry["fetched_at"]),
                    cache_age_seconds=int(cache_entry["cache_age_seconds"]),
                    is_fresh=True,
                    cache_hit=True,
                    fallback_from=fallback_from,
                    fallback_to=fallback_to,
                )
                return cached
            self.cache_misses += 1
        if self._capability_is_plan_restricted(provider, capability):
            error = RuntimeError(f"provider_capability_unavailable:{provider}:{capability}:unsupported_by_plan")
            self._finish_audit_call(call_id, error=str(error), cache_metadata=cache_metadata)
            raise error
        if self.cache is not None:
            if self.provider_statuses.get(provider) == "rate_limited":
                self.skipped_provider_calls_due_to_rate_limit[provider] = (
                    self.skipped_provider_calls_due_to_rate_limit.get(provider, 0) + 1
                )
                error = RuntimeError(f"provider_rate_limited:{provider}")
                self._finish_audit_call(call_id, error=str(error), cache_metadata=cache_metadata)
                raise error
            if self.limiter is not None and not self.limiter.allow(
                provider,
                limit=self.config.api_limits[provider],
            ):
                error = RuntimeError(f"api_limit_exhausted:{provider}")
                self._finish_audit_call(call_id, error=str(error), cache_metadata=cache_metadata)
                raise error
        self.provider_call_counts[provider] = self.provider_call_counts.get(provider, 0) + 1
        try:
            if self.http_observer is None:
                fresh = self.fetch_json(url, payload=payload, headers=self._headers_for(provider))
            else:
                fresh = self.fetch_json(
                    url,
                    payload=payload,
                    headers=self._headers_for(provider),
                    observer=self.http_observer,
                )
        except RuntimeError as error:
            if _is_plan_restricted_error(error):
                self._set_capability_status(provider, capability, "unsupported_by_plan", supported_by_plan=False)
                self._plan_restricted_capabilities.add((provider, capability))
            elif _is_rate_limit_error(error):
                self.provider_statuses[provider] = "rate_limited"
                self._set_capability_status(provider, capability, "rate_limited")
                retry_after = _retry_after_from_error(error)
                if retry_after != "unknown":
                    self.provider_retry_after[provider] = retry_after
            else:
                self._set_capability_status(provider, capability, "temporarily_unavailable")
            wrapped = RuntimeError(f"provider_fetch_error:{provider}:{namespace}:{error}")
            self._finish_audit_call(call_id, error=str(wrapped), cache_metadata=cache_metadata)
            raise wrapped from error
        try:
            _raise_for_provider_error(provider, fresh)
        except RuntimeError as error:
            self._set_capability_status(provider, capability, _normalized_status_from_error(error))
            self._finish_audit_call(call_id, error=str(error), cache_metadata=cache_metadata)
            raise
        existing_capability = self.provider_capabilities.get((provider, capability))
        if existing_capability is None or existing_capability.last_status != "unsupported_by_plan":
            self._set_capability_status(provider, capability, "available")
        fetched_at = _now_iso()
        if self.cache is not None:
            self.cache.set_json(namespace, key, fresh, fetched_at=fetched_at)
        self._finish_audit_call(call_id, response=fresh, cache_hit=False, cache_metadata=cache_metadata)
        self._last_fetch_metadata = _fetch_metadata(
            provider=provider,
            endpoint=namespace,
            payload=fresh,
            fetched_at=fetched_at,
            cache_fetched_at=fetched_at if self.cache is not None else None,
            cache_age_seconds=0 if self.cache is not None else None,
            is_fresh=True,
            cache_hit=False,
            fallback_from=fallback_from,
            fallback_to=fallback_to,
        )
        return fresh

    def _capability_is_plan_restricted(self, provider: str, capability: str) -> bool:
        return (provider, capability) in self._plan_restricted_capabilities

    def _set_capability_status(
        self,
        provider: str,
        capability: str,
        status: str,
        *,
        configured: bool | None = None,
        supported_by_plan: bool | None = None,
    ) -> None:
        existing = self.provider_capabilities.get((provider, capability))
        is_configured = _provider_is_configured(self.config, provider) if configured is None else configured
        self.provider_capabilities[(provider, capability)] = ProviderCapability(
            provider=provider,
            capability=capability,
            configured=is_configured,
            supported_by_plan=(existing.supported_by_plan if existing is not None and supported_by_plan is None else supported_by_plan if supported_by_plan is not None else True),
            implemented=True,
            last_status=status,
            fallback_available=_capability_has_fallback(provider, capability),
        )

    def _snapshot_capabilities(self) -> list[ProviderCapability]:
        return [self.provider_capabilities[key] for key in sorted(self.provider_capabilities)]

    def _fetch_optional(
        self,
        provider: str,
        namespace: str,
        url: str,
        *,
        default: Any,
        payload: dict[str, Any] | None = None,
        parent_call_id: str | None = None,
        attempt_number: int = 1,
        fallback_from: str | None = None,
        fallback_to: str | None = None,
        fallback_reason: str | None = None,
        symbol: str | None = None,
    ) -> Any:
        try:
            return self._fetch(
                provider,
                namespace,
                url,
                payload=payload,
                parent_call_id=parent_call_id,
                attempt_number=attempt_number,
                fallback_from=fallback_from,
                fallback_to=fallback_to,
                fallback_reason=fallback_reason,
                symbol=symbol,
            )
        except RuntimeError as error:
            if _is_degradable_fetch_error(error):
                return default
            raise

    def _start_audit_call(self, provider: str, namespace: str, url: str, **metadata: object) -> str | None:
        if self.audit_recorder is None:
            return None
        call_id = self.audit_recorder.start_call(provider, namespace, url, **metadata)
        self._last_audit_call_id = call_id
        return call_id

    def _finish_audit_call(
        self,
        call_id: str | None,
        *,
        response: Any = None,
        error: str | None = None,
        cache_hit: bool = False,
        cache_metadata: dict[str, object] | None = None,
    ) -> None:
        if call_id is None or self.audit_recorder is None:
            return
        metadata = dict(cache_metadata or {})
        self.audit_recorder.finish_call(
            call_id,
            response=response,
            error=error,
            cache_hit=cache_hit,
            cache_fetched_at=metadata.get("fetched_at"),
            cache_age_seconds=metadata.get("cache_age_seconds"),
            cache_expired=metadata.get("expired"),
        )

    def _headers_for(self, provider: str) -> dict[str, str]:
        if provider == "coingecko" and self.config.coingecko_api_key:
            return {"x-cg-demo-api-key": self.config.coingecko_api_key}
        if provider == "sec":
            return {"User-Agent": "financial-advisor-v1 contact@example.com"}
        return {}


def _cache_key(url: str, payload: dict[str, Any] | None) -> str:
    parts = urlsplit(url)
    query = urlencode(
        sorted(
            (
                key,
                "REDACTED" if _is_secret_query_name(key) else value,
            )
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        )
    )
    sanitized_url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    if payload is None:
        return sanitized_url
    return f"{sanitized_url}|{json.dumps(payload, sort_keys=True)}"


def _is_secret_query_name(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return normalized in {"apikey", "api_key", "token", "access_token", "secret", "key", "password"}


def _has_price_history(payload: Any) -> bool:
    if isinstance(payload, list):
        return bool(payload)
    if isinstance(payload, dict):
        historical = payload.get("historical")
        return isinstance(historical, list) and bool(historical)
    return False


def _coingecko_market_chart_to_klines(payload: Any) -> list[list[Any]]:
    if not isinstance(payload, dict):
        return []
    prices = payload.get("prices")
    if not isinstance(prices, list):
        return []
    volume_by_time: dict[int, float] = {}
    total_volumes = payload.get("total_volumes")
    if isinstance(total_volumes, list):
        for row in total_volumes:
            if isinstance(row, list) and len(row) >= 2:
                try:
                    volume_by_time[int(row[0])] = float(row[1])
                except (TypeError, ValueError):
                    continue
    klines: list[list[Any]] = []
    for row in prices:
        if not isinstance(row, list) or len(row) < 2:
            continue
        try:
            timestamp_ms = int(row[0])
            close = float(row[1])
        except (TypeError, ValueError):
            continue
        volume = volume_by_time.get(timestamp_ms, 0.0)
        klines.append([timestamp_ms, close, close, close, close, volume])
    return sorted(klines, key=lambda item: int(item[0]))


def _alphavantage_news_ticker(symbol: str) -> str:
    if symbol in {"BTC", "ETH", "SOL", "HYPE", "ZEC"}:
        return f"CRYPTO:{symbol}"
    return symbol


def _news_events_from_alphavantage(payload: Any, symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
    by_symbol = {symbol: [] for symbol in symbols}
    feed = payload.get("feed") if isinstance(payload, dict) else None
    if not isinstance(feed, list):
        return by_symbol
    wanted = {symbol: {_alphavantage_news_ticker(symbol), symbol} for symbol in symbols}
    for item in feed:
        if not isinstance(item, dict):
            continue
        ticker_sentiment = item.get("ticker_sentiment")
        if not isinstance(ticker_sentiment, list):
            continue
        matched_symbols = _symbols_for_news_item(ticker_sentiment, wanted)
        if not matched_symbols:
            continue
        event = _news_event_from_alphavantage_item(item)
        for symbol in matched_symbols:
            by_symbol.setdefault(symbol, []).append(event)
    return {symbol: events[:5] for symbol, events in by_symbol.items()}


def _symbols_for_news_item(
    ticker_sentiment: list[Any],
    wanted: dict[str, set[str]],
) -> list[str]:
    raw_tickers = {
        str(row.get("ticker", "")).upper()
        for row in ticker_sentiment
        if isinstance(row, dict)
    }
    return [
        symbol
        for symbol, accepted in wanted.items()
        if raw_tickers & {item.upper() for item in accepted}
    ]


def _news_event_from_alphavantage_item(item: dict[str, Any]) -> dict[str, object]:
    sentiment_label = str(item.get("overall_sentiment_label", "neutral")).lower()
    return {
        "news_event_type": "news_sentiment",
        "confirmed_status": "confirmed",
        "already_priced": "unclear",
        "market_effect": _market_effect_from_sentiment(sentiment_label),
        "news_confidence": "medium",
        "title": str(item.get("title", ""))[:160],
        "source": str(item.get("source", "alphavantage")),
        "time_published": str(item.get("time_published", "")),
    }


def _market_effect_from_sentiment(label: str) -> str:
    if "bearish" in label:
        return "risk_off"
    if "bullish" in label:
        return "risk_on"
    return "neutral"


def _sec_events_from_submissions(payload: Any, *, symbol: str, today: str) -> list[dict[str, object]]:
    recent = payload.get("filings", {}).get("recent", {}) if isinstance(payload, dict) else {}
    if not isinstance(recent, dict):
        return []
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    accession_numbers = recent.get("accessionNumber", [])
    primary_documents = recent.get("primaryDocument", [])
    if not isinstance(forms, list) or not isinstance(filing_dates, list):
        return []
    today_date = _parse_yyyy_mm_dd(today)
    events = []
    for index, form in enumerate(forms):
        form_name = str(form)
        if form_name not in {"8-K", "10-Q", "10-K", "20-F", "6-K"}:
            continue
        filing_date = _list_value(filing_dates, index)
        filing_day = _parse_yyyy_mm_dd(str(filing_date))
        if today_date and filing_day and today_date - filing_day > timedelta(days=45):
            continue
        accession = str(_list_value(accession_numbers, index) or "")
        primary_document = str(_list_value(primary_documents, index) or "")
        events.append(
            {
                "news_event_type": f"sec_{form_name.lower().replace('-', '')}",
                "confirmed_status": "confirmed",
                "already_priced": "unclear",
                "market_effect": "neutral",
                "news_confidence": "medium",
                "title": f"{symbol} SEC filing {form_name} filed {filing_date}",
                "source": "sec_edgar",
                "filing_date": str(filing_date),
                "accession_number": accession,
                "primary_document": primary_document,
            }
        )
        if len(events) >= 3:
            break
    return events


def _list_value(values: Any, index: int) -> Any:
    if isinstance(values, list) and index < len(values):
        return values[index]
    return None


def _parse_yyyy_mm_dd(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def _is_fmp_price_unavailable(error: RuntimeError) -> bool:
    message = str(error)
    return (
        (message.startswith("provider_fetch_error:fmp:prices:http_error:402") and "Premium Query Parameter" in message)
        or message.startswith("provider_capability_unavailable:fmp:historical_prices:unsupported_by_plan")
    )


def _is_plan_restricted_error(error: RuntimeError) -> bool:
    message = str(error).lower()
    return "http_error:402" in message or "premium query parameter" in message or "plan_restricted" in message


def _normalized_status_from_error(error: RuntimeError) -> str:
    if _is_plan_restricted_error(error):
        return "unsupported_by_plan"
    if _is_rate_limit_error(error):
        return "rate_limited"
    return "temporarily_unavailable"


def _earnings_status_from_error(error: RuntimeError) -> str:
    return "plan_restricted" if _is_plan_restricted_error(error) else "provider_unavailable"


def _provider_capability_name(provider: str, namespace: str, url: str) -> str:
    if provider == "fmp" and "historical-price-eod/full" in url:
        return "historical_prices"
    if provider == "fmp" and "historical-price-eod/light" in url:
        return "historical_prices_light"
    if provider == "fmp" and "/stable/quote" in url:
        return "quote"
    if provider == "alphavantage" and "NEWS_SENTIMENT" in url:
        return "news_sentiment"
    if provider == "sec" and "/submissions/" in url:
        return "sec_filings"
    return namespace


def _capability_has_fallback(provider: str, capability: str) -> bool:
    return (provider, capability) in {("fmp", "historical_prices"), ("fmp", "historical_prices_light")}


def _provider_is_configured(config: AdvisorConfig, provider: str) -> bool:
    return {
        "fmp": bool(config.fmp_api_key),
        "coingecko": bool(config.coingecko_api_key),
        "alphavantage": bool(config.alphavantage_api_key),
    }.get(provider, True)


def _is_fmp_rate_limited(error: RuntimeError) -> bool:
    message = str(error).lower()
    return "fmp" in message and ("http_error:429" in message or "limit reach" in message or "rate_limited" in message)


def _is_rate_limit_error(error: RuntimeError) -> bool:
    message = str(error).lower()
    return "http_error:429" in message or "limit reach" in message or "rate limit" in message


def _retry_after_from_error(error: RuntimeError) -> str:
    match = re.search(r"retry[-_ ]after[:= ]+([0-9]+)", str(error), flags=re.IGNORECASE)
    return match.group(1) if match else "unknown"


def _is_binance_restricted_location(error: RuntimeError) -> bool:
    message = str(error)
    return (
        message.startswith("provider_fetch_error:binance:")
        and (
            "http_error:451" in message
            or "restricted location" in message.lower()
        )
    )


def _stock_missing_data(
    symbol: str,
    *,
    alphavantage_fallback_symbols: set[str],
    fmp_price_unavailable_symbols: set[str],
    fmp_price_light_fallback_symbols: set[str],
    yahoo_fallback_symbols: set[str],
    stooq_fallback_symbols: set[str],
) -> list[str]:
    missing_data = []
    if symbol in alphavantage_fallback_symbols:
        missing_data.append("alphavantage_price_fallback")
    if symbol in fmp_price_light_fallback_symbols:
        missing_data.append("fmp_price_light_fallback")
    if symbol in yahoo_fallback_symbols:
        missing_data.append("yahoo_price_fallback")
    if symbol in stooq_fallback_symbols:
        missing_data.append("stooq_price_fallback")
    if symbol in fmp_price_unavailable_symbols:
        missing_data.append("fmp_price_unavailable")
        missing_data.append("probable_cause:fmp_plan_or_price_endpoint_unavailable")
    return missing_data


def _quote_not_requested() -> dict[str, object]:
    return {
        "quote_status": "not_requested",
        "quote_price": None,
        "quote_timestamp": None,
        "quote_source": None,
        "quote_age_seconds": None,
        "quote_is_intraday": False,
        "previous_close": None,
        "daily_change": None,
        "daily_change_pct": None,
    }


def _quote_unavailable() -> dict[str, object]:
    return {**_quote_not_requested(), "quote_status": "unavailable", "quote_source": "fmp"}


def _quote_from_row(row: dict[str, Any] | None) -> dict[str, object]:
    price = _quote_number(row, "price")
    if price is None:
        return _quote_unavailable()
    timestamp = _quote_timestamp(row.get("timestamp") if row else None)
    return {
        "quote_status": "available",
        "quote_price": price,
        "quote_timestamp": timestamp,
        "quote_source": "fmp",
        "quote_age_seconds": _quote_age_seconds(timestamp),
        "quote_is_intraday": timestamp is not None,
        "previous_close": _quote_number(row, "previousClose", "previous_close"),
        "daily_change": _quote_number(row, "change"),
        "daily_change_pct": _quote_number(row, "changesPercentage", "changePercentage", "change_percent"),
    }


def _quote_number(row: dict[str, Any] | None, *keys: str) -> float | None:
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            continue
    return None


def _quote_timestamp(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).replace(microsecond=0).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(microsecond=0).isoformat()
    return None


def _quote_age_seconds(timestamp: str | None) -> int | None:
    if timestamp is None:
        return None
    try:
        quote_time = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if quote_time.tzinfo is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - quote_time).total_seconds()))


def _daily_change_pct(candles: list[Candle]) -> float | None:
    if len(candles) < 2 or candles[-2].close == 0:
        return None
    return ((candles[-1].close - candles[-2].close) / candles[-2].close) * 100


def _benchmark_status(symbol: str | None, candles: list[Candle]) -> dict[str, object]:
    latest = candles[-1] if candles else None
    return {
        "symbol": symbol,
        "status": "available" if latest else "unavailable",
        "source": "fmp",
        "source_timestamp": latest.date if latest else None,
        "daily_change_pct": _daily_change_pct(candles),
        "is_eod": True,
    }


def _stock_data_source(symbol: str, missing_data: list[str]) -> str:
    if "yahoo_price_fallback" in missing_data:
        return "yahoo"
    if "stooq_price_fallback" in missing_data:
        return "stooq"
    if "fmp_price_light_fallback" in missing_data:
        return "fmp_light"
    if "alphavantage_price_fallback" in missing_data:
        return "alphavantage"
    if "fmp_price_unavailable" in missing_data:
        return "unavailable"
    return "fmp"


def yahoo_historical_from_chart(payload: Any) -> dict[str, list[dict[str, Any]]]:
    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return {"historical": []}
    historical = []
    for index, timestamp in enumerate(timestamps):
        close = _list_value(quote.get("close"), index)
        if close is None:
            continue
        historical.append(
            {
                "date": datetime.fromtimestamp(int(timestamp), timezone.utc).date().isoformat(),
                "open": _list_value(quote.get("open"), index) or close,
                "high": _list_value(quote.get("high"), index) or close,
                "low": _list_value(quote.get("low"), index) or close,
                "close": close,
                "volume": _list_value(quote.get("volume"), index) or 0,
            }
        )
    return {"historical": historical}


def _list_value(values: Any, index: int) -> Any:
    if not isinstance(values, list) or index >= len(values):
        return None
    return values[index]


def stooq_historical_from_csv(csv_text: str) -> dict[str, list[dict[str, Any]]]:
    lines = [line.strip() for line in csv_text.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].lower().startswith("date,"):
        return {"historical": []}
    historical = []
    for line in lines[1:]:
        columns = line.split(",")
        if len(columns) < 6 or columns[4].lower() in {"", "null", "n/a"}:
            continue
        historical.append(
            {
                "date": columns[0],
                "open": columns[1],
                "high": columns[2],
                "low": columns[3],
                "close": columns[4],
                "volume": columns[5],
            }
        )
    return {"historical": historical}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fetch_metadata(
    *,
    provider: str,
    endpoint: str,
    payload: Any,
    fetched_at: str,
    cache_fetched_at: str | None,
    cache_age_seconds: int | None,
    is_fresh: bool,
    cache_hit: bool,
    fallback_from: str | None,
    fallback_to: str | None,
) -> DataFetchMetadata:
    source_timestamp = _latest_source_timestamp(payload)
    return DataFetchMetadata(
        provider=provider,
        endpoint=endpoint,
        fetched_at=fetched_at,
        cache_fetched_at=cache_fetched_at,
        source_timestamp=source_timestamp,
        cache_age_seconds=cache_age_seconds,
        source_age_seconds=_timestamp_age_seconds(source_timestamp),
        is_fresh=is_fresh,
        cache_hit=cache_hit,
        fallback_used=bool(fallback_from or fallback_to),
        fallback_from=fallback_from,
        fallback_to=fallback_to,
        granularity="daily" if endpoint == "prices" else None,
        market_data_kind="eod_candle" if endpoint == "prices" else None,
    )


def _latest_source_timestamp(payload: Any) -> str | None:
    candidates: list[str] = []

    def add_timestamp(value: Any) -> None:
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            normalized = _normalize_source_timestamp(value)
            if normalized is not None:
                candidates.append(normalized)
        elif isinstance(value, (list, tuple)):
            for item in value:
                add_timestamp(item)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in {"date", "timestamp", "time", "t", "ts"}:
                    add_timestamp(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (list, tuple)) and item and isinstance(item[0], (int, float)):
                    add_timestamp(item[0])
                else:
                    visit(item)

    visit(payload)
    return max(candidates) if candidates else None


def _normalize_source_timestamp(value: str | int | float) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if 1_000_000_000 <= timestamp <= 10_000_000_000:
        seconds = timestamp
    elif 1_000_000_000_000 <= timestamp <= 10_000_000_000_000:
        seconds = timestamp / 1000
    else:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _timestamp_age_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        if value.isdigit():
            timestamp = int(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            source_time = datetime.fromtimestamp(timestamp, timezone.utc)
        else:
            source_time = datetime.fromisoformat(value)
            if source_time.tzinfo is None:
                source_time = source_time.replace(tzinfo=timezone.utc)
    except (OverflowError, ValueError):
        return None
    return max(0, int((datetime.now(timezone.utc) - source_time).total_seconds()))


def _raise_for_provider_error(provider: str, payload: Any) -> None:
    message = _provider_error_message(payload)
    if message:
        raise RuntimeError(f"provider_api_error:{provider}:{message}")


def _provider_error_message(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("Error Message", "Note", "Information", "msg"):
        value = payload.get(key)
        if value:
            if key == "msg" and "code" not in payload:
                continue
            return _short_error_text(value)
    error = payload.get("error")
    if isinstance(error, dict):
        return _short_error_text(error.get("message") or error.get("status") or error)
    if error:
        return _short_error_text(error)
    if "code" in payload and str(payload.get("code")) not in {"0", "200"}:
        return _short_error_text(payload.get("code"))
    return None


def _short_error_text(value: Any, max_length: int = 120) -> str:
    text = str(value).replace("\n", " ").strip()
    return text[:max_length] if text else "unknown_error"


def _is_degradable_fetch_error(error: RuntimeError) -> bool:
    message = str(error)
    return (
        message.startswith("provider_api_error:")
        or message.startswith("provider_fetch_error:")
        or message.startswith("provider_rate_limited:")
        or message.startswith("api_limit_exhausted:")
        or message.startswith("http_error:")
        or message.startswith("network_error:")
        or message.startswith("provider_capability_unavailable:")
    )
