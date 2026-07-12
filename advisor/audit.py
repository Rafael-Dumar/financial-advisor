from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from advisor.data_sources import (
    AlphaVantageSource,
    BinanceSource,
    CoinbaseSource,
    CoinGeckoSource,
    FmpSource,
    HyperliquidSource,
    SecEdgarSource,
    StooqSource,
    YahooChartSource,
)


SECRET_QUERY_NAMES = ("apikey", "api_key", "token", "secret", "key")
REQUIRED_PROVIDERS = ("fmp", "coingecko", "binance", "hyperliquid", "coinbase", "alphavantage", "sec", "yahoo", "stooq")
CRYPTO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "HYPE": "hyperliquid"}
PHASE2_SCHEMA_VERSION = "phase2-v1"


INTENTIONALLY_UNIMPLEMENTED = (
    {
        "provider": "binance",
        "endpoint_name": "liquidation_orders",
        "capability": "liquidations",
        "implemented": False,
        "status": "not_implemented",
        "reason": "invalid_liquidation_endpoint_is_not_called",
    },
    {
        "provider": "hyperliquid",
        "endpoint_name": "not_applicable",
        "capability": "open_interest_change",
        "implemented": False,
        "status": "not_implemented",
        "reason": "independent_metric_not_collected",
    },
    {
        "provider": "hyperliquid",
        "endpoint_name": "not_applicable",
        "capability": "cvd",
        "implemented": False,
        "status": "not_implemented",
        "reason": "independent_metric_not_collected",
    },
    {
        "provider": "system",
        "endpoint_name": "not_applicable",
        "capability": "guidance",
        "implemented": False,
        "status": "not_implemented",
        "reason": "event_guidance_collector_not_implemented",
    },
    {
        "provider": "system",
        "endpoint_name": "not_applicable",
        "capability": "macro_regime",
        "implemented": False,
        "status": "not_implemented",
        "reason": "macro_collector_not_implemented",
    },
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sanitize_url(url: str) -> str:
    parts = urlsplit(str(url))
    query = urlencode(
        [
            (key, "REDACTED" if any(secret in key.lower() for secret in SECRET_QUERY_NAMES) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _sanitize_text(value: object, max_length: int = 240) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    for marker in ("apikey=", "api_key=", "token=", "secret=", "key="):
        if marker in text.lower():
            text = text[: text.lower().index(marker)] + marker + "REDACTED"
    return text[:max_length] if text else "unknown_error"


def _payload_type(payload: Any) -> str:
    if payload is None:
        return "null"
    if isinstance(payload, bool):
        return "boolean"
    if isinstance(payload, (int, float)):
        return "number"
    if isinstance(payload, str):
        return "string"
    if isinstance(payload, list):
        return "list"
    if isinstance(payload, dict):
        return "dict"
    return type(payload).__name__


def _record_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("historical", "feed", "data", "prices", "total_volumes"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
        return 1 if payload else 0
    return 0


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
                normalized = str(key).lower()
                if normalized in {"date", "timestamp", "time", "time_published", "fundingtime", "t", "ts"}:
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


def _endpoint_name(url: str, payload: Any = None) -> str:
    path = urlsplit(url).path.lower()
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    if "historical-price-eod/full" in path:
        return "historical_prices"
    if "historical-price-eod/light" in path:
        return "historical_prices_light"
    if path.endswith("/quote"):
        return "quote"
    if path.endswith("/profile"):
        return "profile"
    if "ratios-ttm" in path:
        return "ratios"
    if "key-metrics-ttm" in path:
        return "key_metrics_ttm"
    if path.endswith("/key-metrics"):
        return "historical_key_metrics"
    if "income-statement-growth" in path:
        return "growth"
    if "earnings-calendar" in path:
        return "earnings_calendar"
    if path.endswith("/klines"):
        return "klines"
    if path.endswith("/fundingrate"):
        return "funding_rate"
    if path.endswith("/fundinginfo"):
        return "funding_info"
    if path.endswith("/openinterest"):
        return "open_interest"
    if path.endswith("/openinteresthist"):
        return "open_interest_history"
    if path.endswith("/takerlongshortratio"):
        return "taker_long_short_ratio"
    if "/coins/markets" in path:
        return "markets"
    if "/market_chart" in path:
        return "market_chart"
    if path.endswith("/info") and isinstance(payload, dict):
        return str(payload.get("type", "hyperliquid_info"))
    if "/brokerage/market/products/" in path:
        return "coinbase_product"
    if "alphavantage.co/query" in url:
        return str(query.get("function", "query")).lower()
    if "/submissions/" in path:
        return "sec_submissions"
    if "/v8/finance/chart/" in path:
        return "yahoo_daily_chart"
    if "stooq.com/q/d/l" in url:
        return "stooq_daily_csv"
    return path.rsplit("/", 1)[-1] or "unknown"


def _symbol_from_call(url: str, payload: Any = None) -> str | None:
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    if query.get("symbol"):
        return query["symbol"].upper().replace("USDT", "")
    if query.get("tickers"):
        return query["tickers"].split(",")[0].replace("CRYPTO:", "").upper()
    if query.get("ids"):
        coin_id = query["ids"].split(",")[0]
        return next((symbol for symbol, value in CRYPTO_IDS.items() if value == coin_id), coin_id.upper())
    path = urlsplit(url).path.upper()
    for suffix in ("-USD", "USDT"):
        if suffix in path:
            return path.rsplit("/", 1)[-1].replace(suffix, "")
    if isinstance(payload, dict):
        req = payload.get("req")
        if isinstance(req, dict) and req.get("coin"):
            return str(req["coin"]).upper()
    return None


def _expected_fields(endpoint_name: str) -> list[str]:
    return {
        "historical_prices": ["historical"],
        "historical_prices_light": ["historical"],
        "profile": ["mktCap", "volAvg"],
        "ratios": ["priceEarningsRatioTTM"],
        "markets": ["market_cap", "total_volume"],
        "market_chart": ["prices"],
        "coinbase_product": ["price"],
        "sec_submissions": ["filings"],
    }.get(endpoint_name, [])


def validate_schema(payload: Any, expected_fields: list[str]) -> dict[str, object]:
    if not expected_fields:
        return {"schema_valid": True, "fields_present": [], "fields_missing": []}
    if isinstance(payload, list):
        sample = payload[0] if payload and isinstance(payload[0], dict) else {}
    else:
        sample = payload if isinstance(payload, dict) else {}
    present = [field for field in expected_fields if field in sample]
    missing = [field for field in expected_fields if field not in sample]
    return {"schema_valid": not missing, "fields_present": present, "fields_missing": missing}


def _schema_summary(payload: Any, endpoint_name: str) -> dict[str, object]:
    if endpoint_name in {"historical_prices", "historical_prices_light", "profile", "ratios", "key_metrics_ttm", "historical_key_metrics", "growth", "earnings_calendar"} and isinstance(payload, list):
        if not payload:
            return {"schema_valid": True, "fields_present": [], "fields_missing": []}
        sample = payload[0] if payload and isinstance(payload[0], dict) else {}
        accepted_groups = {
            "historical_prices": [{"date"}, {"close", "adjClose"}],
            "historical_prices_light": [{"date"}, {"close", "adjClose"}],
            "profile": [{"mktCap", "marketCap"}, {"volAvg", "averageVolume"}],
            "ratios": [{"priceEarningsRatioTTM", "priceToEarningsRatioTTM", "peRatioTTM", "pe"}],
            "key_metrics_ttm": [{"freeCashFlowPerShareTTM", "freeCashFlowTTM", "freeCashFlowToEquityTTM", "freeCashFlowToFirmTTM", "freeCashFlowYieldTTM"}],
            "historical_key_metrics": [{"peRatio", "peRatioTTM", "priceEarningsRatio", "priceToEarningsRatio", "priceToEarningsRatioTTM", "earningsYield", "earningsYieldTTM"}],
            "growth": [{"growthRevenue", "revenueGrowth"}, {"growthEPS", "epsGrowth", "epsgrowth"}],
            "earnings_calendar": [{"date"}],
        }.get(endpoint_name, [])
        present = sorted(str(key) for key in sample)
        missing = ["|".join(sorted(group)) for group in accepted_groups if not (set(sample) & group)]
        return {"schema_valid": not missing, "fields_present": present, "fields_missing": missing}
    return validate_schema(payload, _expected_fields(endpoint_name))


class AuditRecorder:
    """In-memory recorder used as both loader recorder and optional HTTP observer."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._by_id: dict[str, dict[str, object]] = {}
        self._next_id = 0
        self._active_call_id: str | None = None

    def start_call(self, provider: str, namespace: str, url: str, **metadata: object) -> str:
        self._next_id += 1
        call_id = f"audit-call-{self._next_id:04d}"
        payload = metadata.get("payload")
        call = {
            "call_id": call_id,
            "parent_call_id": metadata.get("parent_call_id"),
            "attempt_number": metadata.get("attempt_number", 1),
            "provider": provider,
            "endpoint_name": _endpoint_name(url, payload),
            "url_sanitized": sanitize_url(url),
            "method": "POST" if payload is not None else "GET",
            "symbol": metadata.get("symbol") or _symbol_from_call(url, payload),
            "namespace": namespace,
            "cache_hit": False,
            "cache_fetched_at": None,
            "cache_age_seconds": None,
            "cache_expired": None,
            "request_started_at": _now_iso(),
            "response_received_at": None,
            "source_data_latest_timestamp": None,
            "http_status": None,
            "records_returned": 0,
            "schema_valid": True,
            "fields_present": [],
            "fields_missing": [],
            "fallback_used": bool(metadata.get("fallback_from")),
            "fallback_provider": metadata.get("fallback_to"),
            "fallback_from": metadata.get("fallback_from"),
            "fallback_to": metadata.get("fallback_to"),
            "fallback_reason": metadata.get("fallback_reason"),
            "retry_after": None,
            "payload_type": None,
            "payload_size_bytes": None,
            "error_code": None,
            "error": None,
        }
        self.calls.append(call)
        self._by_id[call_id] = call
        self._active_call_id = call_id
        return call_id

    def finish_call(self, call_id: str, **metadata: object) -> None:
        call = self._by_id[call_id]
        response = metadata.get("response")
        error = metadata.get("error")
        call["cache_hit"] = bool(metadata.get("cache_hit", False))
        call["cache_fetched_at"] = metadata.get("cache_fetched_at")
        call["cache_age_seconds"] = metadata.get("cache_age_seconds")
        call["cache_expired"] = metadata.get("cache_expired")
        if response is not None:
            schema = _schema_summary(response, str(call["endpoint_name"]))
            call.update(schema)
            call["records_returned"] = _record_count(response)
            call["payload_type"] = _payload_type(response)
            call["source_data_latest_timestamp"] = _latest_source_timestamp(response)
        if call["response_received_at"] is None:
            call["response_received_at"] = _now_iso()
        if error:
            call["error"] = _sanitize_text(error)
            call["error_code"] = _error_code(str(error))

    def on_request(self, **metadata: object) -> None:
        call = self._active_call()
        if call is None:
            return
        call["request_started_at"] = metadata.get("started_at") or call["request_started_at"]

    def on_response(self, **metadata: object) -> None:
        call = self._active_call()
        if call is None:
            return
        call["response_received_at"] = metadata.get("received_at")
        call["http_status"] = metadata.get("http_status")
        call["payload_type"] = metadata.get("payload_type")
        call["payload_size_bytes"] = metadata.get("payload_size_bytes")

    def on_error(self, **metadata: object) -> None:
        call = self._active_call()
        if call is None:
            return
        call["response_received_at"] = metadata.get("received_at")
        call["http_status"] = metadata.get("http_status")
        call["retry_after"] = metadata.get("retry_after")
        call["error"] = _sanitize_text(metadata.get("error"))
        call["error_code"] = _error_code(str(call["error"]))

    def _active_call(self) -> dict[str, object] | None:
        return self._by_id.get(self._active_call_id or "")


def _error_code(error: str) -> str | None:
    for prefix in ("provider_fetch_error:", "provider_api_error:", "http_error:", "network_error:", "api_limit_exhausted:", "provider_rate_limited:"):
        if prefix in error:
            fragment = error[error.index(prefix) + len(prefix) :]
            return prefix[:-1] + (":" + fragment.split(":", 1)[0] if fragment else "")
    return None


def provider_registry(config: Any) -> dict[str, dict[str, object]]:
    fmp = FmpSource(config.fmp_api_key)
    alpha = AlphaVantageSource(config.alphavantage_api_key)
    binance = BinanceSource()
    coingecko = CoinGeckoSource(config.coingecko_api_key)
    hyperliquid = HyperliquidSource()
    coinbase = CoinbaseSource()
    sec = SecEdgarSource()
    yahoo = YahooChartSource()
    stooq = StooqSource()
    specs = {
        "fmp": [
            ("historical_prices", fmp.historical_prices_url("AMD"), "prices"),
            ("historical_prices_light", fmp.historical_prices_light_url("AMD"), "prices"),
            ("profile", fmp.profile_url("AMD"), "fundamentals"),
            ("ratios", fmp.ratios_url("AMD"), "fundamentals"),
            ("key_metrics_ttm", fmp.key_metrics_url("AMD"), "fundamentals"),
            ("historical_key_metrics", fmp.historical_key_metrics_url("AMD"), "fundamentals"),
            ("growth", fmp.income_statement_growth_url("AMD"), "fundamentals"),
            ("earnings_calendar", fmp.earnings_calendar_url("AMD"), "earnings"),
        ],
        "coingecko": [("markets", coingecko.markets_url(["bitcoin"]), "fundamentals"), ("market_chart", coingecko.market_chart_url("bitcoin"), "prices")],
        "binance": [
            ("klines", binance.klines_url("BTCUSDT"), "prices"),
            ("funding_rate", binance.funding_rate_url("BTCUSDT"), "crypto_flow"),
            ("funding_info", binance.funding_info_url(), "crypto_flow"),
            ("open_interest", binance.open_interest_url("BTCUSDT"), "crypto_flow"),
            ("open_interest_history", binance.open_interest_history_url("BTCUSDT"), "crypto_flow"),
            ("taker_long_short_ratio", binance.taker_long_short_url("BTCUSDT"), "crypto_flow"),
        ],
        "hyperliquid": [("candleSnapshot", hyperliquid.info_url(), "prices"), ("metaAndAssetCtxs", hyperliquid.info_url(), "crypto_flow")],
        "coinbase": [("coinbase_product", coinbase.public_product_url("BTC-USD"), "prices")],
        "alphavantage": [("news_sentiment", alpha.news_sentiment_url(["AMD"]), "news"), ("time_series_daily_adjusted", alpha.daily_adjusted_url("AMD"), "prices")],
        "sec": [("sec_submissions", sec.submissions_url("0000002488"), "news")],
        "yahoo": [("yahoo_daily_chart", yahoo.daily_chart_url("AMD"), "prices")],
        "stooq": [("stooq_daily_csv", stooq.daily_csv_url("AMD"), "prices")],
    }
    auth = {"fmp": bool(config.fmp_api_key), "coingecko": bool(config.coingecko_api_key), "alphavantage": bool(config.alphavantage_api_key), "coinbase": True}
    result: dict[str, dict[str, object]] = {}
    for provider in REQUIRED_PROVIDERS:
        configured = auth.get(provider, True)
        result[provider] = {
            "configured": configured,
            "status": "available" if configured else "not_configured",
            "capabilities": [
                {
                    "provider": provider,
                    "capability": _capability_name_from_endpoint(name, namespace),
                    "configured": configured,
                    "supported_by_plan": True,
                    "implemented": True,
                    "last_status": "available" if configured else "not_configured",
                    "fallback_available": _capability_has_fallback(provider, _capability_name_from_endpoint(name, namespace)),
                }
                for name, _url, namespace in specs.get(provider, [])
            ],
            "endpoints": [
                {
                    "endpoint_name": name,
                    "url_sanitized": sanitize_url(url),
                    "method": "POST" if provider == "hyperliquid" else "GET",
                    "auth": "public" if provider in {"binance", "hyperliquid", "coinbase", "sec", "yahoo", "stooq"} else ("key_present" if auth.get(provider) else "key_missing"),
                    "namespace": namespace,
                    "freshness_seconds": config.freshness_seconds.get(namespace),
                }
                for name, url, namespace in specs.get(provider, [])
            ],
            "calls": [],
        }
    return result


def _safe_preview(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, list):
        return {"record_count": len(value)}
    if isinstance(value, dict):
        return {"field_count": len(value), "fields": sorted(str(key) for key in value)[:20]}
    return str(value)[:80]


def _call_for_symbol(recorder: AuditRecorder, symbol: str, endpoint_names: set[str]) -> dict[str, object] | None:
    matches = [
        call
        for call in recorder.calls
        if symbol.upper() in {value.strip().upper() for value in str(call.get("symbol") or "").split(",")}
        and str(call.get("endpoint_name")) in endpoint_names
    ]
    return matches[-1] if matches else None


def _field_entry(
    *,
    value: Any,
    call: dict[str, object] | None,
    parser: str,
    limitations: list[str] | None = None,
    is_intraday: bool = False,
    is_eod: bool = False,
    data_fetch_metadata: Any | None = None,
    status: str | None = None,
) -> dict[str, object]:
    metadata = data_fetch_metadata
    return {
        "value_present": value is not None and value != [] and value != {},
        "value_preview": _safe_preview(value),
        "source_provider": _call_or_metadata(call, "provider", metadata, "provider"),
        "source_endpoint": _call_or_metadata(call, "endpoint_name", metadata, "endpoint"),
        "source_data_timestamp": _call_or_metadata(call, "source_data_latest_timestamp", metadata, "source_timestamp"),
        "fetched_at": _call_or_metadata(call, "response_received_at", metadata, "fetched_at"),
        "cache_fetched_at": _call_or_metadata(call, "cache_fetched_at", metadata, "cache_fetched_at"),
        "cache_age_seconds": _call_or_metadata(call, "cache_age_seconds", metadata, "cache_age_seconds"),
        "source_age_seconds": _call_or_metadata(call, "source_age_seconds", metadata, "source_age_seconds"),
        "is_fresh": getattr(metadata, "is_fresh", None),
        "granularity": getattr(metadata, "granularity", None),
        "market_data_kind": getattr(metadata, "market_data_kind", None),
        "is_intraday": getattr(metadata, "market_data_kind", None) == "intraday" if metadata else is_intraday,
        "is_eod": getattr(metadata, "market_data_kind", None) == "eod_candle" if metadata else is_eod,
        "fallback_used": bool(call and call.get("fallback_used")) if call else bool(getattr(metadata, "fallback_used", False)),
        "fallback_from": call.get("fallback_from") if call else getattr(metadata, "fallback_from", None),
        "fallback_to": call.get("fallback_to") if call else getattr(metadata, "fallback_to", None),
        "parser": parser,
        "limitations": sorted(set(limitations or [])),
        "status": status,
    }


def _call_or_metadata(
    call: dict[str, object] | None,
    call_key: str,
    metadata: Any | None,
    metadata_attribute: str,
) -> object | None:
    if call is not None and call.get(call_key) is not None:
        return call[call_key]
    return getattr(metadata, metadata_attribute, None)


def _empty_fields(field_names: list[str], reason: str) -> dict[str, dict[str, object]]:
    return {
        field: _field_entry(value=None, call=None, parser="not_collected", limitations=[reason])
        for field in field_names
    }


def _apply_crypto_metric_provenance(
    field: dict[str, object],
    provenance: dict[str, object] | None,
) -> dict[str, object]:
    if not provenance:
        return field
    for output_key, provenance_key in (
        ("source_provider", "provider"),
        ("source_endpoint", "endpoint"),
        ("granularity", "granularity"),
        ("market_data_kind", "market_data_kind"),
    ):
        if provenance.get(provenance_key) is not None:
            field[output_key] = provenance[provenance_key]
    if provenance.get("status") is not None:
        field["status"] = provenance["status"]
    return field


STOCK_AUDIT_FIELDS = [
    "candles",
    "latest_candle_date",
    "latest_close",
    "live_quote",
    "daily_change",
    "volume",
    "market_cap",
    "average_volume",
    "pe",
    "peg",
    "historical_pe",
    "revenue_growth",
    "eps_growth",
    "margin",
    "free_cash_flow",
    "earnings_date",
    "guidance",
    "post_earnings_gap",
    "news",
    "sec_filings",
    "macro_regime",
    "benchmark",
    "sector_benchmark",
    "sector_relative_strength",
]

CRYPTO_AUDIT_FIELDS = [
    "candles",
    "latest_candle_date",
    "spot_price",
    "market_cap",
    "volume",
    "rsi",
    "ema",
    "sma",
    "funding",
    "open_interest",
    "open_interest_change",
    "cvd",
    "coinbase_premium",
    "liquidations",
    "news",
    "crypto_regime",
]


def build_data_lineage(snapshots: list[Any], recorder: AuditRecorder, requested_symbols: list[str]) -> dict[str, object]:
    from advisor.indicators import ema, rsi, sma

    by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
    result: dict[str, object] = {}
    for symbol in requested_symbols:
        snapshot = by_symbol.get(symbol)
        asset_type = snapshot.asset_type if snapshot is not None else ("crypto" if symbol in CRYPTO_IDS else "stock")
        field_names = CRYPTO_AUDIT_FIELDS if asset_type == "crypto" else STOCK_AUDIT_FIELDS
        if snapshot is None:
            result[symbol] = {"asset_type": asset_type, "fields": _empty_fields(field_names, "not_collected:no_network_or_failed_collection")}
            continue
        candles = list(snapshot.candles)
        closes = [candle.close for candle in candles]
        latest = candles[-1] if candles else None
        previous = candles[-2] if len(candles) >= 2 else None
        price_call = _call_for_symbol(recorder, symbol, {"historical_prices", "historical_prices_light", "klines", "market_chart", "candleSnapshot", "yahoo_daily_chart", "stooq_daily_csv"})
        quote_call = _call_for_symbol(recorder, symbol, {"quote"})
        profile_call = _call_for_symbol(recorder, symbol, {"profile", "markets"})
        ratios_call = _call_for_symbol(recorder, symbol, {"ratios"})
        history_metrics_call = _call_for_symbol(recorder, symbol, {"historical_key_metrics"})
        growth_call = _call_for_symbol(recorder, symbol, {"growth"})
        earnings_call = _call_for_symbol(recorder, symbol, {"earnings_calendar"})
        news_call = _call_for_symbol(recorder, symbol, {"news_sentiment"})
        sec_call = _call_for_symbol(recorder, symbol, {"sec_submissions"})
        limitations = list(snapshot.missing_data)
        price_metadata = snapshot.data_fetch_metadata
        if asset_type == "stock":
            quote_limitations = list(limitations)
            if snapshot.quote_status != "available":
                quote_limitations.append(f"quote_status:{snapshot.quote_status}")
            quote_field = _field_entry(
                value=snapshot.quote_price,
                call=quote_call,
                parser="stock_quote_from_fmp_batch",
                limitations=quote_limitations,
            )
            quote_field.update(
                {
                    "quote_status": snapshot.quote_status,
                    "quote_timestamp": snapshot.quote_timestamp,
                    "quote_age_seconds": snapshot.quote_age_seconds,
                    "quote_is_intraday": bool(snapshot.quote_is_intraday and snapshot.quote_timestamp),
                    "is_intraday": bool(snapshot.quote_is_intraday and snapshot.quote_timestamp),
                    "is_eod": False,
                    "market_data_kind": "intraday" if snapshot.quote_is_intraday and snapshot.quote_timestamp else None,
                    "source_data_timestamp": snapshot.quote_timestamp or quote_field["source_data_timestamp"],
                }
            )
            provenance = snapshot.benchmark_provenance
            market_benchmark = provenance.get("market") if isinstance(provenance, dict) else None
            sector_benchmark = provenance.get("sector") if isinstance(provenance, dict) else None
            relative_strength = provenance.get("relative_strength_pct") if isinstance(provenance, dict) else None
            market_field = _field_entry(
                value=market_benchmark,
                call=None,
                parser="live_loader.benchmark_status",
                limitations=[] if market_benchmark else ["benchmark_not_attached_to_snapshot"],
                is_eod=True,
            )
            market_field["benchmark"] = market_benchmark
            sector_field = _field_entry(
                value=sector_benchmark,
                call=None,
                parser="live_loader.benchmark_status",
                limitations=[] if sector_benchmark else ["sector_benchmark_not_available"],
                is_eod=True,
            )
            sector_field["benchmark"] = sector_benchmark
            fields = {
                "candles": _field_entry(value=candles or None, call=price_call, parser="stock_snapshot_from_payloads", limitations=limitations, is_eod=True, data_fetch_metadata=price_metadata),
                "latest_candle_date": _field_entry(value=latest.date if latest else None, call=price_call, parser="stock_snapshot_from_payloads", limitations=limitations, is_eod=True, data_fetch_metadata=price_metadata),
                "latest_close": _field_entry(value=latest.close if latest else None, call=price_call, parser="stock_snapshot_from_payloads", limitations=limitations, is_eod=True, data_fetch_metadata=price_metadata),
                "live_quote": quote_field,
                "daily_change": _field_entry(value=snapshot.daily_change if snapshot.daily_change is not None else ((latest.close - previous.close) / previous.close if latest and previous and previous.close else None), call=quote_call if snapshot.daily_change is not None else price_call, parser="stock_quote_daily_change" if snapshot.daily_change is not None else "derived_from_last_two_candles", limitations=limitations, is_eod=snapshot.daily_change is None, data_fetch_metadata=price_metadata if snapshot.daily_change is None else None),
                "volume": _field_entry(value=latest.volume if latest else None, call=price_call, parser="stock_snapshot_from_payloads", limitations=limitations, is_eod=True, data_fetch_metadata=price_metadata),
                "market_cap": _field_entry(value=snapshot.fundamentals.market_cap, call=profile_call, parser="stock_snapshot_from_payloads", limitations=limitations),
                "average_volume": _field_entry(value=snapshot.fundamentals.average_volume, call=profile_call, parser="stock_snapshot_from_payloads", limitations=limitations),
                "pe": _field_entry(value=snapshot.fundamentals.pe, call=ratios_call, parser="stock_snapshot_from_payloads", limitations=limitations),
                "peg": _field_entry(value=snapshot.fundamentals.peg, call=ratios_call, parser="stock_snapshot_from_payloads", limitations=limitations),
                "historical_pe": _field_entry(value=snapshot.fundamentals.historical_pe, call=history_metrics_call, parser="_historical_pe", limitations=limitations),
                "revenue_growth": _field_entry(value=snapshot.fundamentals.revenue_growth, call=growth_call, parser="stock_snapshot_from_payloads", limitations=limitations),
                "eps_growth": _field_entry(value=snapshot.fundamentals.eps_growth, call=growth_call, parser="stock_snapshot_from_payloads", limitations=limitations),
                "margin": _field_entry(value=snapshot.fundamentals.margin_trend, call=ratios_call, parser="stock_snapshot_from_payloads", limitations=limitations),
                "free_cash_flow": _field_entry(value=snapshot.fundamentals.free_cash_flow_positive, call=profile_call, parser="stock_snapshot_from_payloads", limitations=limitations),
                "earnings_date": _field_entry(value=snapshot.event.next_earnings_date if snapshot.event else None, call=earnings_call, parser="_next_earnings_date", limitations=limitations, status=snapshot.earnings_status),
                "guidance": _field_entry(value=snapshot.event.guidance_recent if snapshot.event else None, call=None, parser="not_implemented_by_data_pipeline", limitations=["guidance_recent_not_collected"], status=snapshot.guidance_status),
                "post_earnings_gap": _field_entry(value=snapshot.event.post_earnings_gap_percent if snapshot.event else None, call=price_call, parser="_post_earnings_gap", limitations=limitations, is_eod=True, data_fetch_metadata=price_metadata),
                "news": _field_entry(value=snapshot.news_events or None, call=news_call, parser="_news_events_from_alphavantage", limitations=[] if snapshot.news_events else ["news_not_collected"], status=snapshot.news_status),
                "sec_filings": _field_entry(value=[event for event in snapshot.news_events if str(event.get("news_event_type", "")).startswith("sec_")] or None, call=sec_call, parser="_sec_events_from_submissions", limitations=[] if snapshot.sec_filings_status == "available" else ["sec_filings_not_collected"], status=snapshot.sec_filings_status),
                "macro_regime": _field_entry(value=None, call=None, parser="not_implemented_by_scoring", limitations=["macro_not_collected"], status=snapshot.macro_status),
                "benchmark": market_field,
                "sector_benchmark": sector_field,
                "sector_relative_strength": _field_entry(value=relative_strength, call=None, parser="live_loader._attach_benchmark_provenance", limitations=[] if relative_strength is not None else ["sector_relative_strength_not_calculable"], is_eod=True),
            }
        else:
            funding_call = _call_for_symbol(recorder, symbol, {"funding_rate", "metaAndAssetCtxs"})
            oi_call = _call_for_symbol(recorder, symbol, {"open_interest", "open_interest_history", "metaAndAssetCtxs"})
            cvd_call = _call_for_symbol(recorder, symbol, {"taker_long_short_ratio"})
            liquidation_call = _call_for_symbol(recorder, symbol, {"liquidation_orders"})
            coinbase_call = _call_for_symbol(recorder, symbol, {"coinbase_product"})
            metric_provenance = snapshot.crypto_metric_provenance
            fields = {
                "candles": _field_entry(value=candles or None, call=price_call, parser="crypto_snapshot_from_payloads", limitations=limitations, is_eod=True, data_fetch_metadata=price_metadata),
                "latest_candle_date": _field_entry(value=latest.date if latest else None, call=price_call, parser="crypto_snapshot_from_payloads", limitations=limitations, is_eod=True, data_fetch_metadata=price_metadata),
                "spot_price": _field_entry(value=latest.close if latest else None, call=price_call, parser="crypto_snapshot_from_payloads", limitations=limitations, is_eod=True, data_fetch_metadata=price_metadata),
                "market_cap": _field_entry(value=snapshot.fundamentals.market_cap, call=profile_call, parser="crypto_snapshot_from_payloads", limitations=limitations),
                "volume": _field_entry(value=snapshot.fundamentals.average_volume, call=profile_call, parser="crypto_snapshot_from_payloads", limitations=limitations),
                "rsi": _field_entry(value=rsi(closes)[-1] if closes else None, call=price_call, parser="rsi", limitations=limitations),
                "ema": _field_entry(value=ema(closes, 20)[-1] if closes else None, call=price_call, parser="ema", limitations=limitations),
                "sma": _field_entry(value=sma(closes, 20)[-1] if closes else None, call=price_call, parser="sma", limitations=limitations),
                "funding": _field_entry(value=snapshot.funding_rate, call=funding_call, parser="binance_funding_rate_8h_from_payloads_or_hyperliquid_crypto_flow_from_payload", limitations=limitations),
                "open_interest": _field_entry(value=metric_provenance.get("current_open_interest", {}).get("value"), call=oi_call, parser="flow_payload_parser", limitations=limitations),
                "open_interest_change": _field_entry(value=snapshot.open_interest_change, call=oi_call, parser="_open_interest_change", limitations=limitations),
                "cvd": _field_entry(value=snapshot.cvd_proxy, call=cvd_call, parser="_cvd_proxy", limitations=limitations),
                "coinbase_premium": _field_entry(value=snapshot.coinbase_premium, call=coinbase_call, parser="_coinbase_premium", limitations=limitations),
                "liquidations": _field_entry(value=snapshot.liquidation_imbalance, call=liquidation_call, parser="not_implemented_liquidations", limitations=limitations),
                "news": _field_entry(value=snapshot.news_events or None, call=news_call, parser="_news_events_from_alphavantage", limitations=[] if snapshot.news_events else ["news_not_collected"]),
                "crypto_regime": _field_entry(value=None, call=None, parser="regime_not_run_by_data_audit", limitations=["not_collected_without_regime_stage"]),
            }
            for field_name, metric_name in {
                "candles": "candles",
                "latest_candle_date": "candles",
                "spot_price": "spot",
                "funding": "funding",
                "open_interest": "current_open_interest",
                "open_interest_change": "open_interest_change",
                "cvd": "cvd",
                "coinbase_premium": "premium",
                "liquidations": "liquidations",
            }.items():
                _apply_crypto_metric_provenance(fields[field_name], metric_provenance.get(metric_name))
        result[symbol] = {"asset_type": asset_type, "fields": fields}
    return result


def build_cache_audit(
    *,
    source_db: Path | str | None,
    audit_db: Path | str | None,
    freshness_seconds: dict[str, int],
    now: str | None = None,
) -> dict[str, object]:
    databases: list[dict[str, object]] = []
    for label, db_path in (("source", source_db), ("audit", audit_db)):
        if db_path is None:
            continue
        path = Path(db_path)
        rows = []
        if path.exists():
            from advisor.cache import SQLiteCache

            rows = SQLiteCache(path, read_only=True).inspect(now=now, freshness_seconds=freshness_seconds)
        for row in rows:
            row["key"] = sanitize_url(str(row["key"]))
        databases.append({"database": label, "path": str(path), "read_only": True, "rows": rows})
    return {
        "generated_at_utc": now or _now_iso(),
        "databases": databases,
        "reuse_observations": {
            "same_cache_between_main_and_close": "not_determined_from_cache_rows",
            "snapshot_cache_age_matches_real_age": "requires_live_snapshot_trace",
            "data_timestamp_is_source_timestamp": False,
        },
    }


def _provider_status(configured: bool, calls: list[dict[str, object]]) -> str:
    if not configured:
        return "not_configured"
    if not calls:
        return "available"
    statuses = [str(call.get("status") or "available") for call in calls]
    if "available" in statuses and any(status != "available" for status in statuses):
        return "partial"
    if "partial" in statuses:
        return "partial"
    if "unsupported_by_plan" in statuses:
        return "unsupported_by_plan"
    if "rate_limited" in statuses:
        return "rate_limited"
    if "temporarily_unavailable" in statuses:
        return "temporarily_unavailable"
    return "available"


def _capability_name_from_endpoint(endpoint_name: str, namespace: str) -> str:
    if endpoint_name in {"historical_prices", "prices"}:
        return "historical_prices"
    if endpoint_name == "historical_prices_light":
        return "historical_prices_light"
    if endpoint_name == "quote":
        return "quote"
    if endpoint_name == "news_sentiment":
        return "news_sentiment"
    if endpoint_name == "sec_submissions":
        return "sec_filings"
    return namespace


def _capability_name_from_call(call: dict[str, object]) -> str:
    return _capability_name_from_endpoint(str(call.get("endpoint_name") or ""), str(call.get("namespace") or ""))


def _capability_has_fallback(provider: str, capability: str) -> bool:
    return (provider, capability) in {("fmp", "historical_prices"), ("fmp", "historical_prices_light")}


def _failure_cause(call: dict[str, object]) -> str | None:
    error = str(call.get("error") or "").lower()
    if "http_error:401" in error or "http_error:403" in error or "unauthorized" in error:
        return "unauthorized"
    if "http_error:402" in error or "premium query parameter" in error or "plan_restricted" in error or "provider_capability_unavailable" in error and "unsupported_by_plan" in error:
        return "plan_restricted"
    if "http_error:429" in error or "rate_limited" in error or "api_limit_exhausted" in error:
        return "rate_limited"
    if "http_error:404" in error or "not found" in error:
        return "not_found"
    if "network_error" in error or "timeout" in error:
        return "network_error"
    if call.get("schema_valid") is False or "schema" in error:
        return "schema_error"
    if not error and call.get("records_returned") == 0 and call.get("payload_type") in {"list", "dict"}:
        return "empty_payload"
    return "unknown_error" if error else None


def _status_from_failure_cause(cause: str | None) -> str:
    if cause is None:
        return "available"
    if cause == "plan_restricted":
        return "unsupported_by_plan"
    if cause == "rate_limited":
        return "rate_limited"
    if cause == "empty_payload":
        return "partial"
    return "temporarily_unavailable"


def _status_priority(status: str) -> int:
    return {
        "not_configured": 6,
        "unsupported_by_plan": 5,
        "rate_limited": 4,
        "temporarily_unavailable": 3,
        "partial": 2,
        "available": 1,
    }.get(status, 0)


def build_provider_audit(config: Any, recorder: AuditRecorder, *, network_mode: str) -> dict[str, object]:
    providers = provider_registry(config)
    by_provider: dict[str, list[dict[str, object]]] = {name: [] for name in providers}
    for call in recorder.calls:
        by_provider.setdefault(str(call["provider"]), []).append(dict(call))
    result: dict[str, object] = {}
    for name, metadata in providers.items():
        calls = [dict(call) for call in by_provider.get(name, [])]
        capabilities = {str(item["capability"]): dict(item) for item in metadata["capabilities"]}
        for call in calls:
            cause = _failure_cause(call)
            status = _status_from_failure_cause(cause)
            call["failure_cause"] = cause
            call["status"] = status
            capability = _capability_name_from_call(call)
            entry = capabilities.setdefault(
                capability,
                {
                    "provider": name,
                    "capability": capability,
                    "configured": bool(metadata["configured"]),
                    "supported_by_plan": True,
                    "implemented": True,
                    "last_status": "available" if metadata["configured"] else "not_configured",
                    "fallback_available": _capability_has_fallback(name, capability),
                },
            )
            if _status_priority(status) >= _status_priority(str(entry["last_status"])):
                entry["last_status"] = status
            if cause == "plan_restricted":
                entry["supported_by_plan"] = False
        result[name] = {
            "configured": metadata["configured"],
            "status": _provider_status(bool(metadata["configured"]), calls) if network_mode == "live" else ("not_configured" if not metadata["configured"] else "available"),
            "capabilities": sorted(capabilities.values(), key=lambda item: str(item["capability"])),
            "endpoints": metadata["endpoints"],
            "calls": calls,
        }
    return result


def _copy_config_for_symbols(config: Any, symbols: list[str] | None, include_discovery: bool) -> Any:
    from copy import deepcopy

    copied = deepcopy(config)
    if symbols is not None:
        crypto_symbols = [symbol for symbol in symbols if symbol in CRYPTO_IDS or symbol in {"BNB", "XRP", "LINK", "AVAX"}]
        stock_symbols = [symbol for symbol in symbols if symbol not in crypto_symbols]
        copied.stock_watchlist = stock_symbols
        copied.crypto_watchlist = crypto_symbols
        copied.discovery_stock_candidates = []
        copied.discovery_crypto_candidates = []
        copied.max_stocks_per_run = None
    elif not include_discovery:
        copied.discovery_stock_candidates = []
        copied.discovery_crypto_candidates = []
    return copied


def _symbols_for_audit(config: Any, symbols: list[str] | None, include_discovery: bool) -> list[str]:
    if symbols is not None:
        return list(dict.fromkeys(symbols))
    stocks, cryptos = config.symbols_for_scan(include_discovery=include_discovery)
    return [*stocks, *cryptos]


def _brt_iso(utc_value: str) -> str:
    return datetime.fromisoformat(utc_value).astimezone(timezone(timedelta(hours=-3))).isoformat()


def _trace_gates(snapshots: list[Any], benchmarks: dict[str, list[Any]], config: Any) -> dict[str, object]:
    from advisor.backtest import backtest_similar_setups
    from advisor.scan_engine import derive_market_regimes, derive_relative_strength
    from advisor.scoring import (
        _base_decision,
        _has_blocking_data_gap,
        _has_confidence_limiting_data_gap,
        _is_technical_unvalidated,
        _missing_data_severity,
        _weaker_cap,
        classify_asset,
        score_asset,
    )

    if not snapshots:
        return {"status": "not_run", "reason": "no_snapshots"}
    regimes = derive_market_regimes(snapshots=snapshots, benchmarks=benchmarks)
    assets: dict[str, object] = {}
    for snapshot in snapshots:
        if len(snapshot.candles) < 80:
            assets[snapshot.symbol] = {
                "base_decision": "blocked",
                "base_scores": {"investment_quality": 0, "swing_trade": 0},
                "gates": [{"gate": "insufficient_price_history", "source": "advisor/cli.py:_unscorable_decision", "category": "data_missing", "effect": "blocked"}],
                "final_decision": "blocked",
                "would_change_if_removed": {},
            }
            continue
        stock_label = regimes.stock.label
        crypto_label = regimes.crypto.label
        relative = derive_relative_strength(snapshot, snapshots=snapshots, benchmarks=benchmarks)
        scored = score_asset(
            snapshot,
            stock_regime_label=stock_label,
            crypto_regime_label=crypto_label,
            account_capital=config.account_capital,
            risk_fraction=config.risk_fraction,
            relative_strength_percent=relative,
            minimum_market_cap=config.minimum_crypto_market_cap if snapshot.asset_type == "crypto" else config.minimum_stock_market_cap,
        )
        stats = backtest_similar_setups(snapshot.candles)
        decision = classify_asset(scored, stats)
        gates: list[dict[str, object]] = []
        limitations = list(scored.limitations)
        alerts = list(scored.alerts)
        if _has_blocking_data_gap(limitations):
            gates.append({"gate": "blocking_data_gap", "source": "advisor/scoring.py:_has_blocking_data_gap", "category": "data_missing", "effect": "blocked"})
        if _has_confidence_limiting_data_gap(limitations):
            gates.append({"gate": "confidence_limiting_data_gap", "source": "advisor/scoring.py:_has_confidence_limiting_data_gap", "category": "data_missing", "effect": "cap_to_watch_buy"})
        for alert in sorted(set(alerts) & {"low_liquidity", "event_risk", "earnings_imminent", "earnings_near", "market_risk_off", "market_not_risk_on", "position_too_small_for_risk", "recent_gap_risk", "small_market_cap"}):
            gates.append({"gate": alert, "source": "advisor/scoring.py:hard_gates", "category": "hard_gate", "effect": "cap_to_wait_or_watch_buy"})
        if _missing_data_severity(limitations) == "high":
            gates.append({"gate": "high_severity_data_not_watchlist", "source": "advisor/scoring.py:159-161", "category": "data_missing", "effect": "cap_to_technical_unvalidated"})
        if "stale_price_data" in decision.limitations:
            gates.append({"gate": "stale_price_data", "source": "advisor/scoring.py:163-164", "category": "freshness", "effect": "cap_to_wait"})
        if _is_technical_unvalidated(scored, decision.limitations, stats, decision.data_quality, decision.missing_data_severity, decision.sample_quality):
            gates.append({"gate": "technical_unvalidated", "source": "advisor/scoring.py:200-201", "category": "confidence", "effect": "cap_to_technical_unvalidated"})
        observed = sorted(set([*limitations, *alerts]))
        for condition in observed:
            if not any(gate["gate"] == condition for gate in gates):
                gates.append({"gate": condition, "source": "advisor/scoring.py:score_asset", "category": "observed_condition", "effect": "input_to_classification"})
        assets[snapshot.symbol] = {
            "base_decision": _base_decision(scored),
            "base_scores": {"investment_quality": scored.investment_quality_score, "swing_trade": scored.swing_trade_score},
            "gates": gates,
            "final_decision": decision.decision,
            "would_change_if_removed": {str(gate["gate"]): _base_decision(scored) for gate in gates if decision.decision != _base_decision(scored)},
            "limitations": sorted(set(decision.limitations)),
            "alerts": sorted(set(decision.alerts)),
            "data_source": snapshot.data_source,
        }
    return {"status": "traced", "assets": assets}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _phase2_capability(capability: dict[str, object]) -> dict[str, object]:
    return {
        "capability": capability["capability"],
        "configured": bool(capability["configured"]),
        "supported_by_plan": bool(capability["supported_by_plan"]),
        "implemented": bool(capability["implemented"]),
        "last_status": capability["last_status"],
        "fallback_available": bool(capability["fallback_available"]),
    }


def build_phase2_provider_validation(
    provider_data: dict[str, object], *, now: str, network_mode: str
) -> dict[str, object]:
    providers: list[dict[str, object]] = []
    for provider_name in sorted(provider_data):
        provider = dict(provider_data[provider_name])
        calls = [
            {
                "endpoint_name": call.get("endpoint_name"),
                "namespace": call.get("namespace"),
                "status": call.get("status"),
                "failure_cause": call.get("failure_cause"),
                "fallback_used": bool(call.get("fallback_used")),
                "fallback_from": call.get("fallback_from"),
                "fallback_to": call.get("fallback_to"),
                "fallback_reason": call.get("fallback_reason"),
                "http_status": call.get("http_status"),
                "error_code": call.get("error_code"),
            }
            for call in provider.get("calls", [])
        ]
        providers.append(
            {
                "provider": provider_name,
                "configured": bool(provider["configured"]),
                "status": provider["status"],
                "capabilities": [_phase2_capability(item) for item in provider["capabilities"]],
                "endpoints": [
                    {
                        "endpoint_name": endpoint["endpoint_name"],
                        "namespace": endpoint["namespace"],
                        "method": endpoint["method"],
                        "auth": endpoint["auth"],
                        "url_sanitized": sanitize_url(str(endpoint["url_sanitized"])),
                    }
                    for endpoint in provider["endpoints"]
                ],
                "calls": calls,
            }
        )
    return {
        "schema_version": PHASE2_SCHEMA_VERSION,
        "audit_generated_at_utc": now,
        "network_mode": network_mode,
        "providers": providers,
        "invalid_or_unimplemented": [dict(item) for item in INTENTIONALLY_UNIMPLEMENTED],
    }


def _source_age_seconds(source_timestamp: object, now: str) -> int | None:
    if source_timestamp is None:
        return None
    normalized = _normalize_source_timestamp(str(source_timestamp))
    try:
        source_time = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if source_time.tzinfo is None:
        source_time = source_time.replace(tzinfo=timezone.utc)
    current_time = datetime.fromisoformat(now)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    return max(0, int((current_time - source_time).total_seconds()))


def build_phase2_cache_validation(cache_data: dict[str, object], *, now: str, network_mode: str) -> dict[str, object]:
    databases: list[dict[str, object]] = []
    for database in cache_data.get("databases", []):
        rows = [
            {
                "namespace": row.get("namespace"),
                "key_sanitized": sanitize_url(str(row.get("key") or "")),
                "original_fetched_at": row.get("fetched_at"),
                "cache_age_seconds": row.get("cache_age_seconds"),
                "cache_expired": row.get("expired"),
                "source_data_latest_timestamp": row.get("latest_source_timestamp"),
                "source_age_seconds": _source_age_seconds(row.get("latest_source_timestamp"), now),
            }
            for row in database.get("rows", [])
        ]
        databases.append(
            {
                "database": database.get("database"),
                "access": "read_only",
                "rows": sorted(rows, key=lambda row: (str(row["namespace"]), str(row["key_sanitized"]))),
            }
        )
    return {
        "schema_version": PHASE2_SCHEMA_VERSION,
        "audit_generated_at_utc": now,
        "network_mode": network_mode,
        "cache_databases": databases,
        "timestamp_semantics": {
            "original_fetched_at": "cache_entry_write_time",
            "cache_age_seconds": "age_since_original_fetched_at",
            "source_data_latest_timestamp": "latest_timestamp_in_source_payload",
            "source_age_seconds": "age_of_source_data_when_available",
        },
    }


def _phase2_timestamp_record(field: dict[str, object]) -> dict[str, object]:
    return {
        "source_provider": field.get("source_provider"),
        "source_endpoint": field.get("source_endpoint"),
        "source_data_timestamp": field.get("source_data_timestamp"),
        "fetched_at": field.get("fetched_at"),
        "cache_fetched_at": field.get("cache_fetched_at"),
        "cache_age_seconds": field.get("cache_age_seconds"),
        "source_age_seconds": field.get("source_age_seconds"),
        "granularity": field.get("granularity"),
        "market_data_kind": field.get("market_data_kind"),
        "is_intraday": bool(field.get("is_intraday")),
        "is_eod": bool(field.get("is_eod")),
        "status": field.get("status"),
    }


def build_phase2_source_timestamps(lineage: dict[str, object], *, now: str, network_mode: str) -> dict[str, object]:
    assets: dict[str, object] = {}
    for symbol in sorted(lineage):
        asset = dict(lineage[symbol])
        fields = dict(asset["fields"])
        if asset["asset_type"] == "stock":
            assets[symbol] = {
                "asset_type": "stock",
                "eod_candle": _phase2_timestamp_record(fields["candles"]),
                "live_quote": _phase2_timestamp_record(fields["live_quote"]),
            }
        else:
            assets[symbol] = {
                "asset_type": "crypto",
                "daily_candle": _phase2_timestamp_record(fields["candles"]),
                "intraday_metrics": {
                    name: _phase2_timestamp_record(fields[name])
                    for name in ("funding", "open_interest", "open_interest_change", "cvd", "coinbase_premium", "liquidations")
                },
            }
    return {
        "schema_version": PHASE2_SCHEMA_VERSION,
        "audit_generated_at_utc": now,
        "network_mode": network_mode,
        "assets": assets,
    }


def build_phase2_capability_matrix(provider_data: dict[str, object], *, now: str, network_mode: str) -> dict[str, object]:
    providers = [
        {
            "provider": provider_name,
            "status": provider_data[provider_name]["status"],
            "capabilities": [_phase2_capability(item) for item in provider_data[provider_name]["capabilities"]],
        }
        for provider_name in sorted(provider_data)
    ]
    return {
        "schema_version": PHASE2_SCHEMA_VERSION,
        "audit_generated_at_utc": now,
        "network_mode": network_mode,
        "providers": providers,
        "intentionally_unimplemented": [dict(item) for item in INTENTIONALLY_UNIMPLEMENTED],
    }


def run_data_audit(
    *,
    config: Any,
    source_db: Path | str | None,
    audit_db: Path | str | None,
    output_dir: Path | str,
    symbols: list[str] | None = None,
    include_discovery: bool = False,
    require_live: bool = False,
    no_network: bool = False,
    trace_gates: bool = False,
    fail_on_schema_drift: bool = False,
) -> dict[str, object]:
    if require_live and no_network:
        raise ValueError("require_live_conflicts_with_no_network")
    now = _now_iso()
    output_path = Path(output_dir)
    requested_symbols = _symbols_for_audit(config, symbols, include_discovery)
    mode = "live" if require_live else "no_network"
    recorder = AuditRecorder()
    snapshots: list[Any] = []
    benchmarks: dict[str, list[Any]] = {}
    errors: list[str] = []
    collection_config = _copy_config_for_symbols(config, symbols, include_discovery)
    effective_audit_db = Path(audit_db) if require_live and audit_db is not None else None
    if require_live:
        validation_errors = collection_config.validate(allow_missing_keys=False)
        if validation_errors:
            errors.extend(validation_errors)
        else:
            from advisor.live_loader import LiveDataLoader

            loader = LiveDataLoader(collection_config, db_path=effective_audit_db, audit_recorder=recorder, http_observer=recorder)
            try:
                snapshots = loader.load_snapshots(include_discovery=include_discovery and symbols is None)
                benchmarks = loader.load_benchmarks()
            except RuntimeError as error:
                errors.append(_sanitize_text(error))
    source_cache = build_cache_audit(
        source_db=source_db,
        audit_db=effective_audit_db,
        freshness_seconds=config.freshness_seconds,
        now=now,
    )
    lineage = build_data_lineage(snapshots, recorder, requested_symbols)
    provider_data = build_provider_audit(collection_config, recorder, network_mode=mode)
    schema_drift = any(call.get("schema_valid") is False for call in recorder.calls)
    gate_data = _trace_gates(snapshots, benchmarks, collection_config) if trace_gates else {"status": "not_run", "reason": "trace_gates_disabled"}
    provider_artifact = {
        "audit_generated_at_utc": now,
        "audit_generated_at_brt": _brt_iso(now),
        "network_mode": mode,
        "symbols_requested": requested_symbols,
        "schema_drift": schema_drift,
        "errors": errors,
        "providers": provider_data,
    }
    lineage_artifact = {
        "audit_generated_at_utc": now,
        "network_mode": mode,
        "symbols_requested": requested_symbols,
        "assets": lineage,
    }
    cache_artifact = {**source_cache, "network_mode": mode, "symbols_requested": requested_symbols}
    gate_artifact = {"audit_generated_at_utc": now, "network_mode": mode, **gate_data}
    phase2_provider_artifact = build_phase2_provider_validation(provider_data, now=now, network_mode=mode)
    phase2_cache_artifact = build_phase2_cache_validation(source_cache, now=now, network_mode=mode)
    phase2_timestamp_artifact = build_phase2_source_timestamps(lineage, now=now, network_mode=mode)
    phase2_capability_artifact = build_phase2_capability_matrix(provider_data, now=now, network_mode=mode)
    summary = {
        "audit_generated_at_utc": now,
        "audit_generated_at_brt": _brt_iso(now),
        "network_mode": mode,
        "symbols_requested": requested_symbols,
        "source_db": str(source_db) if source_db is not None else None,
        "audit_db": str(effective_audit_db) if effective_audit_db is not None else None,
        "config_keys": {
            "fmp": "present" if collection_config.fmp_api_key else "missing",
            "coingecko": "present" if collection_config.coingecko_api_key else "missing",
            "alphavantage": "present" if collection_config.alphavantage_api_key else "missing",
            "coinbase": "present" if collection_config.coinbase_api_key else "missing",
        },
        "schema_drift": schema_drift,
        "errors": errors,
        "artifacts": [
            "provider-audit.json",
            "data-lineage.json",
            "cache-audit.json",
            "gate-analysis.json",
            "audit-summary.json",
            "phase2-provider-validation.json",
            "phase2-cache-validation.json",
            "phase2-source-timestamps.json",
            "phase2-capability-matrix.json",
        ],
    }
    _write_json(output_path / "provider-audit.json", provider_artifact)
    _write_json(output_path / "data-lineage.json", lineage_artifact)
    _write_json(output_path / "cache-audit.json", cache_artifact)
    _write_json(output_path / "gate-analysis.json", gate_artifact)
    _write_json(output_path / "phase2-provider-validation.json", phase2_provider_artifact)
    _write_json(output_path / "phase2-cache-validation.json", phase2_cache_artifact)
    _write_json(output_path / "phase2-source-timestamps.json", phase2_timestamp_artifact)
    _write_json(output_path / "phase2-capability-matrix.json", phase2_capability_artifact)
    _write_json(output_path / "audit-summary.json", summary)
    return {"exit_code": 1 if ((require_live and errors) or (fail_on_schema_drift and schema_drift)) else 0, **summary}
