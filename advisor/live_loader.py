from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Callable

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
from advisor.data_sources import AlphaVantageSource, BinanceSource, CoinbaseSource, CoinGeckoSource, FmpSource, HyperliquidSource, StooqSource, YahooChartSource
from advisor.http_client import fetch_json, fetch_text
from advisor.models import AssetSnapshot, Candle


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


class LiveDataLoader:
    def __init__(
        self,
        config: AdvisorConfig,
        *,
        fetch_json: FetchJson = fetch_json,
        fetch_text: FetchText = fetch_text,
        today: str | None = None,
        db_path: Path | str | None = None,
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
        self._binance_funding_info_loaded = False
        self._binance_funding_info: Any = []
        self.cache_hits = 0
        self.cache_misses = 0
        self.provider_call_counts: dict[str, int] = {}
        self.provider_statuses: dict[str, str] = {}
        self.provider_retry_after: dict[str, str] = {}
        self.skipped_provider_calls_due_to_rate_limit: dict[str, int] = {}
        self.fmp = FmpSource(config.fmp_api_key)
        self.alphavantage = AlphaVantageSource(config.alphavantage_api_key)
        self.binance = BinanceSource()
        self.coinbase = CoinbaseSource()
        self.coingecko = CoinGeckoSource(config.coingecko_api_key)
        self.hyperliquid = HyperliquidSource()
        self.stooq = StooqSource()
        self.yahoo = YahooChartSource()

    def load_snapshots(self, *, include_discovery: bool = False) -> list[AssetSnapshot]:
        snapshots = []
        stock_symbols, crypto_symbols = self.config.symbols_for_scan(include_discovery=include_discovery)
        for symbol in stock_symbols:
            snapshots.append(self.load_stock(symbol))
        for symbol in crypto_symbols:
            snapshots.append(self.load_crypto(symbol))
        return snapshots

    def load_benchmarks(self) -> dict[str, list[Candle]]:
        benchmarks: dict[str, list[Candle]] = {}
        for symbol in ["SPY", "QQQ"]:
            try:
                historical_payload = self._fetch("fmp", "prices", self.fmp.historical_prices_url(symbol))
            except RuntimeError as error:
                if _is_fmp_price_unavailable(error):
                    benchmarks[symbol] = []
                    continue
                raise
            benchmarks[symbol] = stock_snapshot_from_payloads(
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
                taker_payload=self._fetch_optional(
                    "binance",
                    "crypto_flow",
                    self.binance.taker_long_short_url(binance_symbol),
                    default=[],
                ),
                liquidation_payload=self._fetch_optional(
                    "binance",
                    "crypto_flow",
                    self.binance.liquidation_orders_url(binance_symbol),
                    default=[],
                ),
            )
        return flows

    def load_stock(self, symbol: str) -> AssetSnapshot:
        historical_payload = self._stock_historical_payload(symbol)
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
                data_timestamp=_now_iso(),
                cache_age_seconds=0,
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
            data_timestamp=_now_iso(),
            cache_age_seconds=0,
        )

    def load_crypto(self, symbol: str) -> AssetSnapshot:
        market_payload = self._market_payload(symbol)
        if symbol == "HYPE":
            klines_payload = self._hyperliquid_klines(symbol)
            context_payload = self._fetch_optional(
                "hyperliquid",
                "crypto_flow",
                self.hyperliquid.info_url(),
                payload=self.hyperliquid.meta_and_asset_contexts_payload(),
                default=[],
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
        else:
            binance_symbol = f"{symbol}USDT"
            try:
                klines_payload = self._fetch("binance", "prices", self.binance.klines_url(binance_symbol))
            except RuntimeError as error:
                if _is_binance_restricted_location(error):
                    return crypto_snapshot_from_payloads(
                        symbol=symbol,
                        theme=THEMES.get(symbol, "crypto"),
                        klines_payload=[],
                        market_payload=market_payload,
                        funding_payload=[],
                        open_interest_payload={},
                        taker_payload=[],
                        coinbase_payload={},
                        liquidation_payload=[],
                        missing_data=[
                            "binance_restricted_location",
                            "price_history_unavailable",
                        ],
                        data_source="binance_unavailable",
                        data_timestamp=_now_iso(),
                        cache_age_seconds=0,
                    )
                raise
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
            taker_payload = self._fetch("binance", "crypto_flow", self.binance.taker_long_short_url(binance_symbol))
            liquidation_payload = self._fetch_optional(
                "binance",
                "crypto_flow",
                self.binance.liquidation_orders_url(binance_symbol),
                default=[],
            )
            coinbase_payload = self._coinbase_payload(symbol)
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
            data_timestamp=_now_iso(),
            cache_age_seconds=0,
        )

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

    def _coinbase_payload(self, symbol: str) -> dict[str, Any]:
        if not self.config.coinbase_api_key:
            return {}
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
            payload = self._fetch("fmp", namespace, url)
        except RuntimeError as error:
            if _is_degradable_fetch_error(error):
                if namespace == "earnings":
                    missing_data.append("earnings_unavailable")
                else:
                    missing_data.append("fundamentals_unavailable")
                return []
            raise
        return payload if isinstance(payload, list) else []

    def _stock_historical_payload(self, symbol: str) -> dict[str, Any]:
        try:
            payload = self._fetch("fmp", "prices", self.fmp.historical_prices_url(symbol))
        except RuntimeError as error:
            if _is_fmp_price_unavailable(error):
                light_payload = self._fetch_optional(
                    "fmp",
                    "prices",
                    self.fmp.historical_prices_light_url(symbol),
                    default=[],
                )
                if _has_price_history(light_payload):
                    self._fmp_price_light_fallback_symbols.add(symbol)
                    return light_payload
                yahoo_payload = self._yahoo_historical_payload(symbol)
                if _has_price_history(yahoo_payload):
                    self._yahoo_fallback_symbols.add(symbol)
                    return yahoo_payload
                stooq_payload = self._stooq_historical_payload(symbol)
                if _has_price_history(stooq_payload):
                    self._stooq_fallback_symbols.add(symbol)
                    return stooq_payload
                self._fmp_price_unavailable_symbols.add(symbol)
                if not self.config.alphavantage_api_key:
                    return []
            elif _is_fmp_rate_limited(error):
                fallback_payload = self._stock_price_fallback_payload(symbol)
                if _has_price_history(fallback_payload):
                    return fallback_payload
                self._fmp_price_unavailable_symbols.add(symbol)
                raise
            else:
                raise
            payload = {}
        if _has_price_history(payload) or not self.config.alphavantage_api_key:
            return payload
        alpha_payload = self._fetch("alphavantage", "prices", self.alphavantage.daily_adjusted_url(symbol))
        self._alphavantage_fallback_symbols.add(symbol)
        return fmp_historical_from_alphavantage(alpha_payload)

    def _stock_price_fallback_payload(self, symbol: str) -> dict[str, Any]:
        if self.config.alphavantage_api_key:
            try:
                alpha_payload = self._fetch("alphavantage", "prices", self.alphavantage.daily_adjusted_url(symbol))
            except RuntimeError:
                alpha_payload = {}
            if alpha_payload:
                converted = fmp_historical_from_alphavantage(alpha_payload)
                if _has_price_history(converted):
                    self._alphavantage_fallback_symbols.add(symbol)
                    return converted
        yahoo_payload = self._yahoo_historical_payload(symbol)
        if _has_price_history(yahoo_payload):
            self._yahoo_fallback_symbols.add(symbol)
            return yahoo_payload
        stooq_payload = self._stooq_historical_payload(symbol)
        if _has_price_history(stooq_payload):
            self._stooq_fallback_symbols.add(symbol)
            return stooq_payload
        return {"historical": []}

    def _yahoo_historical_payload(self, symbol: str) -> dict[str, list[dict[str, Any]]]:
        payload = self._fetch_optional(
            "yahoo",
            "prices",
            self.yahoo.daily_chart_url(symbol),
            default={},
        )
        return yahoo_historical_from_chart(payload)

    def _stooq_historical_payload(self, symbol: str) -> dict[str, list[dict[str, Any]]]:
        try:
            csv_text = self.fetch_text(self.stooq.daily_csv_url(symbol), headers={"User-Agent": "financial-advisor-v1"})
        except RuntimeError:
            return {"historical": []}
        return stooq_historical_from_csv(csv_text)

    def _fetch(self, provider: str, namespace: str, url: str, *, payload: dict[str, Any] | None = None) -> Any:
        key = _cache_key(url, payload)
        if self.cache is not None:
            cached = self.cache.get_json(
                namespace,
                key,
                max_age_seconds=self.config.freshness_seconds[namespace],
            )
            if cached is not None:
                self.cache_hits += 1
                _raise_for_provider_error(provider, cached)
                return cached
            self.cache_misses += 1
            if self.provider_statuses.get(provider) == "rate_limited":
                self.skipped_provider_calls_due_to_rate_limit[provider] = (
                    self.skipped_provider_calls_due_to_rate_limit.get(provider, 0) + 1
                )
                raise RuntimeError(f"provider_rate_limited:{provider}")
            if self.limiter is not None and not self.limiter.allow(
                provider,
                limit=self.config.api_limits[provider],
            ):
                raise RuntimeError(f"api_limit_exhausted:{provider}")
        self.provider_call_counts[provider] = self.provider_call_counts.get(provider, 0) + 1
        try:
            fresh = self.fetch_json(url, payload=payload, headers=self._headers_for(provider))
        except RuntimeError as error:
            if _is_rate_limit_error(error):
                self.provider_statuses[provider] = "rate_limited"
                retry_after = _retry_after_from_error(error)
                if retry_after != "unknown":
                    self.provider_retry_after[provider] = retry_after
            raise RuntimeError(f"provider_fetch_error:{provider}:{namespace}:{error}") from error
        _raise_for_provider_error(provider, fresh)
        if self.cache is not None:
            self.cache.set_json(namespace, key, fresh)
        return fresh

    def _fetch_optional(
        self,
        provider: str,
        namespace: str,
        url: str,
        *,
        default: Any,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        try:
            return self._fetch(provider, namespace, url, payload=payload)
        except RuntimeError as error:
            if _is_degradable_fetch_error(error):
                return default
            raise

    def _headers_for(self, provider: str) -> dict[str, str]:
        if provider == "coingecko" and self.config.coingecko_api_key:
            return {"x-cg-demo-api-key": self.config.coingecko_api_key}
        return {}


def _cache_key(url: str, payload: dict[str, Any] | None) -> str:
    if payload is None:
        return url
    return f"{url}|{json.dumps(payload, sort_keys=True)}"


def _has_price_history(payload: Any) -> bool:
    if isinstance(payload, list):
        return bool(payload)
    if isinstance(payload, dict):
        historical = payload.get("historical")
        return isinstance(historical, list) and bool(historical)
    return False


def _is_fmp_price_unavailable(error: RuntimeError) -> bool:
    message = str(error)
    return (
        message.startswith("provider_fetch_error:fmp:prices:http_error:402")
        and "Premium Query Parameter" in message
    )


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
    )
