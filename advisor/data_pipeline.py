from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any

from advisor.models import AssetSnapshot, Candle, DataFetchMetadata, EventInfo, Fundamentals, ProviderCapability


def stock_snapshot_from_payloads(
    *,
    symbol: str,
    theme: str,
    historical_payload: dict[str, Any],
    profile_payload: list[dict[str, Any]],
    ratios_payload: list[dict[str, Any]],
    metrics_payload: list[dict[str, Any]],
    historical_metrics_payload: list[dict[str, Any]],
    growth_payload: list[dict[str, Any]],
    earnings_payload: list[dict[str, Any]],
    today: str,
    missing_data: list[str] | None = None,
    data_source: str = "fmp",
    data_timestamp: str | None = None,
    cache_age_seconds: int | None = None,
    data_fetch_metadata: DataFetchMetadata | None = None,
    news_events: list[dict[str, Any]] | None = None,
    quote_status: str = "not_requested",
    quote_price: float | None = None,
    quote_timestamp: str | None = None,
    quote_source: str | None = None,
    quote_age_seconds: int | None = None,
    quote_is_intraday: bool = False,
    previous_close: float | None = None,
    daily_change: float | None = None,
    daily_change_pct: float | None = None,
    benchmark_provenance: dict[str, object] | None = None,
    provider_capabilities: list[ProviderCapability] | None = None,
    earnings_status: str | None = None,
    guidance_status: str = "not_implemented",
    macro_status: str = "not_implemented",
    news_status: str = "not_configured",
    sec_filings_status: str = "not_implemented",
) -> AssetSnapshot:
    historical_rows = _historical_rows(historical_payload)
    candles = sorted(
        [
            Candle(
                date=row["date"],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0)),
            )
            for row in historical_rows
        ],
        key=lambda candle: candle.date,
    )
    profile = _first(profile_payload)
    ratios = _first(ratios_payload)
    metrics = _first(metrics_payload)
    growth = _first(growth_payload)
    parseable_earnings_payload = earnings_payload if _earnings_payload_is_parseable(earnings_payload) else []
    earnings_date = _next_earnings_date(parseable_earnings_payload, today)
    last_earnings_date = _last_earnings_date(parseable_earnings_payload, today)
    resolved_earnings_status = earnings_status or _earnings_status(earnings_payload, earnings_date)

    fundamentals = Fundamentals(
        pe=_get_number(ratios, "priceEarningsRatioTTM", "priceToEarningsRatioTTM", "peRatioTTM", "pe"),
        peg=_get_number(
            ratios,
            "pegRatioTTM",
            "priceToEarningsGrowthRatioTTM",
            "forwardPriceToEarningsGrowthRatioTTM",
            "pegRatio",
        ),
        historical_pe=_historical_pe(historical_metrics_payload),
        revenue_growth=_get_number(growth, "growthRevenue", "revenueGrowth"),
        eps_growth=_get_number(growth, "growthEPS", "epsGrowth", "epsgrowth"),
        margin_trend=_get_number(ratios, "grossProfitMarginTTM", "netProfitMarginTTM"),
        free_cash_flow_positive=_positive_or_none(
            _get_number(
                metrics,
                "freeCashFlowPerShareTTM",
                "freeCashFlowTTM",
                "freeCashFlowToEquityTTM",
                "freeCashFlowToFirmTTM",
                "freeCashFlowYieldTTM",
            )
            or _get_number(ratios, "freeCashFlowPerShareTTM")
        ),
        market_cap=_get_number(profile, "mktCap", "marketCap"),
        average_volume=_get_number(profile, "volAvg", "averageVolume"),
    )

    stock_missing_data = list(missing_data or [])
    stock_missing_data.append("guidance_recent_not_collected")
    if not earnings_payload or earnings_date is None:
        stock_missing_data.append("earnings_data_missing")
    post_earnings_gap = _post_earnings_gap(candles, last_earnings_date)
    if post_earnings_gap is None:
        stock_missing_data.append("post_earnings_gap_not_collected")
    event = EventInfo(
        days_to_earnings=(earnings_date - _parse_date(today)).days if earnings_date else None,
        guidance_recent=None,
        post_earnings_gap_percent=post_earnings_gap,
        last_earnings_date=last_earnings_date.isoformat() if last_earnings_date else None,
        next_earnings_date=earnings_date.isoformat() if earnings_date else None,
    )
    price_metadata = _price_fetch_metadata(
        data_fetch_metadata,
        provider=data_source,
        endpoint="historical_prices",
        source_timestamp=candles[-1].date if candles else None,
    )
    return AssetSnapshot(
        symbol=symbol,
        asset_type="stock",
        theme=theme,
        candles=candles,
        fundamentals=fundamentals,
        event=event,
        missing_data=sorted(set(stock_missing_data)),
        data_source=data_source,
        data_timestamp=price_metadata.source_timestamp or data_timestamp,
        cache_age_seconds=price_metadata.cache_age_seconds if price_metadata.cache_age_seconds is not None else cache_age_seconds,
        data_fetch_metadata=price_metadata,
        news_events=list(news_events or []),
        provider_capabilities=list(provider_capabilities or []),
        earnings_status=resolved_earnings_status,
        guidance_status=guidance_status,
        macro_status=macro_status,
        news_status=news_status,
        sec_filings_status=sec_filings_status,
        quote_status=quote_status,
        quote_price=quote_price,
        quote_timestamp=quote_timestamp,
        quote_source=quote_source,
        quote_age_seconds=quote_age_seconds,
        quote_is_intraday=quote_is_intraday,
        previous_close=previous_close,
        daily_change=daily_change,
        daily_change_pct=daily_change_pct,
        benchmark_provenance=dict(benchmark_provenance or {}),
    )


def fmp_historical_from_alphavantage(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    series = payload.get("Time Series (Daily)", {})
    historical = []
    for day, row in series.items():
        historical.append(
            {
                "date": day,
                "open": row.get("1. open"),
                "high": row.get("2. high"),
                "low": row.get("3. low"),
                "close": row.get("4. close"),
                "volume": row.get("6. volume") or row.get("5. volume"),
            }
        )
    return {"historical": historical}


def _historical_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("historical", [])
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _earnings_status(payload: Any, next_earnings_date: date | None) -> str:
    if not _earnings_payload_is_parseable(payload):
        return "schema_error"
    if not payload:
        return "no_upcoming_event_found"
    return "verified" if next_earnings_date is not None else "no_upcoming_event_found"


def _earnings_payload_is_parseable(payload: Any) -> bool:
    if not isinstance(payload, list):
        return False
    for row in payload:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            return False
        try:
            date.fromisoformat(row["date"][:10])
        except ValueError:
            return False
    return True


def crypto_snapshot_from_payloads(
    *,
    symbol: str,
    theme: str,
    klines_payload: list[list[Any]],
    market_payload: dict[str, Any],
    funding_payload: list[dict[str, Any]],
    open_interest_payload: Any,
    taker_payload: list[dict[str, Any]],
    coinbase_payload: dict[str, Any] | None = None,
    liquidation_payload: list[dict[str, Any]] | None = None,
    missing_data: list[str] | None = None,
    data_source: str = "binance/coingecko",
    data_timestamp: str | None = None,
    cache_age_seconds: int | None = None,
    data_fetch_metadata: DataFetchMetadata | None = None,
    news_events: list[dict[str, Any]] | None = None,
    provider_capabilities: list[ProviderCapability] | None = None,
    news_status: str = "not_configured",
    crypto_metric_provenance: dict[str, dict[str, object]] | None = None,
) -> AssetSnapshot:
    candles = [
        Candle(
            date=datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc).date().isoformat(),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in klines_payload
    ]
    funding = _get_number(_last(funding_payload), "fundingRate")
    open_interest_change = _open_interest_change(open_interest_payload)
    cvd_proxy = _cvd_proxy(taker_payload)
    # Retain the argument for caller compatibility, but no validated public
    # liquidation collector is available to turn arbitrary rows into a signal.
    liquidation_imbalance = None
    metric_provenance = _crypto_metric_provenance(
        supplied=crypto_metric_provenance,
        data_source=data_source,
        price_metadata=data_fetch_metadata,
        candles=candles,
        funding_rate=funding,
        current_open_interest=_current_open_interest(open_interest_payload),
        open_interest_change=open_interest_change,
        cvd_proxy=cvd_proxy,
        coinbase_payload=coinbase_payload or {},
        liquidation_imbalance=liquidation_imbalance,
    )
    coinbase_premium = _coinbase_premium(
        coinbase_payload or {},
        candles[-1].close if candles else None,
        timestamps_compatible=bool(metric_provenance["premium"].get("timestamps_compatible")),
    )
    metric_provenance["premium"]["value"] = coinbase_premium
    missing_data = list(missing_data or [])
    if not taker_payload:
        missing_data.append("cvd_proxy_unavailable")
    if not open_interest_payload:
        missing_data.append("open_interest_unavailable")
    if open_interest_change is None:
        missing_data.append("open_interest_change_unavailable")
    if coinbase_premium is None:
        missing_data.append("coinbase_premium_unavailable")
    if liquidation_imbalance is None:
        missing_data.append("liquidations_unavailable")

    fundamentals = Fundamentals(
        pe=None,
        peg=None,
        historical_pe=None,
        revenue_growth=None,
        eps_growth=None,
        margin_trend=None,
        free_cash_flow_positive=None,
        market_cap=_get_number(market_payload, "market_cap"),
        average_volume=_get_number(market_payload, "total_volume"),
        market_cap_rank=_get_int(market_payload, "market_cap_rank"),
    )
    price_metadata = _price_fetch_metadata(
        data_fetch_metadata,
        provider=data_source,
        endpoint="klines",
        source_timestamp=candles[-1].date if candles else None,
    )
    return AssetSnapshot(
        symbol=symbol,
        asset_type="crypto",
        theme=theme,
        candles=candles,
        fundamentals=fundamentals,
        event=None,
        funding_rate=funding,
        open_interest_change=open_interest_change,
        cvd_proxy=cvd_proxy,
        coinbase_premium=coinbase_premium,
        liquidation_imbalance=liquidation_imbalance,
        missing_data=missing_data,
        data_source=data_source,
        data_timestamp=price_metadata.source_timestamp or data_timestamp,
        cache_age_seconds=price_metadata.cache_age_seconds if price_metadata.cache_age_seconds is not None else cache_age_seconds,
        data_fetch_metadata=price_metadata,
        news_events=list(news_events or []),
        provider_capabilities=list(provider_capabilities or []),
        news_status=news_status,
        crypto_metric_provenance=metric_provenance,
    )


def _price_fetch_metadata(
    metadata: DataFetchMetadata | None,
    *,
    provider: str,
    endpoint: str,
    source_timestamp: str | None,
) -> DataFetchMetadata:
    if metadata is None:
        return DataFetchMetadata(
            provider=provider,
            endpoint=endpoint,
            source_timestamp=source_timestamp,
            source_age_seconds=_source_age_seconds(source_timestamp),
            granularity="daily",
            market_data_kind="eod_candle",
        )
    normalized_source_timestamp = source_timestamp or metadata.source_timestamp
    return replace(
        metadata,
        source_timestamp=normalized_source_timestamp,
        source_age_seconds=_source_age_seconds(normalized_source_timestamp),
        granularity=metadata.granularity or "daily",
        market_data_kind=metadata.market_data_kind or "eod_candle",
    )


def _source_age_seconds(source_timestamp: str | None) -> int | None:
    if not source_timestamp:
        return None
    try:
        source_time = datetime.fromisoformat(source_timestamp)
    except ValueError:
        return None
    if source_time.tzinfo is None:
        source_time = source_time.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - source_time).total_seconds()))


def binance_crypto_flow_from_payloads(
    *,
    funding_payload: Any,
    funding_info_payload: Any = None,
    symbol: str | None = None,
    open_interest_payload: Any,
    taker_payload: Any,
    liquidation_payload: Any,
    current_open_interest_payload: Any = None,
) -> dict[str, Any]:
    funding_rate = binance_funding_rate_8h_from_payloads(
        funding_payload=funding_payload,
        funding_info_payload=funding_info_payload,
        symbol=symbol,
    )
    open_interest_change = _open_interest_change(open_interest_payload)
    open_interest = _current_open_interest(current_open_interest_payload) or _get_number(_last(open_interest_payload), "sumOpenInterest", "openInterest")
    cvd_proxy = _cvd_proxy(taker_payload if isinstance(taker_payload, list) else [])
    # Retain the argument for caller compatibility, but never publish a
    # liquidation signal from unvalidated rows.
    liquidation_imbalance = None
    limitations = []
    if funding_rate is None:
        limitations.append("funding_rate_unavailable")
    if open_interest is None:
        limitations.append("open_interest_unavailable")
    if open_interest_change is None:
        limitations.append("open_interest_change_unavailable")
    if cvd_proxy is None:
        limitations.append("cvd_proxy_unavailable")
    if liquidation_imbalance is None:
        limitations.append("liquidations_unavailable")
    return {
        "source": "binance",
        "funding_rate": funding_rate,
        "funding_rate_basis": "8h_equivalent",
        "open_interest": open_interest,
        "open_interest_change": open_interest_change,
        "cvd_proxy": cvd_proxy,
        "cvd_is_proxy": True,
        "liquidation_imbalance": liquidation_imbalance,
        "limitations": sorted(limitations),
        "metric_provenance": _crypto_metric_provenance(
            supplied=None,
            data_source="binance",
            price_metadata=None,
            candles=[],
            funding_rate=funding_rate,
            current_open_interest=open_interest,
            open_interest_change=open_interest_change,
            cvd_proxy=cvd_proxy,
            coinbase_payload={},
            liquidation_imbalance=liquidation_imbalance,
        ),
    }


def binance_funding_rate_8h_from_payloads(
    *,
    funding_payload: Any,
    funding_info_payload: Any,
    symbol: str | None,
) -> float | None:
    funding_rate = _get_number(_last(funding_payload), "fundingRate")
    if funding_rate is None:
        return None
    interval_hours = 8
    if symbol and isinstance(funding_info_payload, list):
        adjusted = next(
            (
                row
                for row in funding_info_payload
                if isinstance(row, dict) and row.get("symbol") == symbol
            ),
            None,
        )
        adjusted_hours = _get_int(adjusted, "fundingIntervalHours") if adjusted else None
        if adjusted_hours is not None and adjusted_hours > 0:
            interval_hours = adjusted_hours
    return funding_rate * (8 / interval_hours)


def hyperliquid_crypto_flow_from_payload(payload: Any, *, symbol: str) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if isinstance(payload, list) and len(payload) >= 2:
        metadata, contexts = payload[0], payload[1]
        universe = metadata.get("universe", []) if isinstance(metadata, dict) else []
        if isinstance(universe, list) and isinstance(contexts, list):
            for index, asset in enumerate(universe):
                if isinstance(asset, dict) and asset.get("name") == symbol and index < len(contexts):
                    context = contexts[index] if isinstance(contexts[index], dict) else {}
                    break

    hourly_funding_rate = _get_number(context, "funding")
    funding_rate = hourly_funding_rate * 8 if hourly_funding_rate is not None else None
    open_interest = _get_number(context, "openInterest")
    limitations = [
        "cvd_proxy_unavailable",
        "liquidations_unavailable",
        "open_interest_change_unavailable",
    ]
    if not context:
        limitations.append("hyperliquid_asset_context_unavailable")
    if funding_rate is None:
        limitations.append("funding_rate_unavailable")
    if open_interest is None:
        limitations.append("open_interest_unavailable")
    return {
        "source": "hyperliquid",
        "funding_rate": funding_rate,
        "funding_rate_basis": "8h_equivalent",
        "open_interest": open_interest,
        "open_interest_change": None,
        "cvd_proxy": None,
        "cvd_is_proxy": False,
        "liquidation_imbalance": None,
        "mark_price": _get_number(context, "markPx"),
        "day_notional_volume": _get_number(context, "dayNtlVlm"),
        "limitations": sorted(limitations),
        "metric_provenance": _crypto_metric_provenance(
            supplied={
                "funding": {"provider": "hyperliquid", "endpoint": "metaAndAssetCtxs", "status": "available" if funding_rate is not None else "provider_unavailable"},
                "current_open_interest": {"provider": "hyperliquid", "endpoint": "metaAndAssetCtxs", "status": "available" if open_interest is not None else "provider_unavailable"},
                "open_interest_change": {"provider": "hyperliquid", "endpoint": None, "status": "not_implemented"},
                "cvd": {"provider": "hyperliquid", "endpoint": None, "status": "not_implemented"},
                "liquidations": {"provider": "hyperliquid", "endpoint": None, "status": "not_implemented"},
            },
            data_source="hyperliquid",
            price_metadata=None,
            candles=[],
            funding_rate=funding_rate,
            current_open_interest=open_interest,
            open_interest_change=None,
            cvd_proxy=None,
            coinbase_payload={},
            liquidation_imbalance=None,
        ),
    }


def _first(values: Any) -> dict[str, Any]:
    if isinstance(values, list) and values and isinstance(values[0], dict):
        return values[0]
    if isinstance(values, dict):
        return values
    return {}


def _last(values: Any) -> dict[str, Any]:
    if isinstance(values, list) and values and isinstance(values[-1], dict):
        return values[-1]
    if isinstance(values, dict):
        return values
    return {}


def _get_number(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _current_open_interest(payload: Any) -> float | None:
    return _get_number(payload, "openInterest") if isinstance(payload, dict) else _get_number(_last(payload), "sumOpenInterest", "openInterest")


def _get_int(payload: dict[str, Any], *keys: str) -> int | None:
    number = _get_number(payload, *keys)
    return int(number) if number is not None else None


def _median_positive_number(rows: Any, *keys: str) -> float | None:
    if not isinstance(rows, list):
        return None
    values = [
        number
        for row in rows
        if isinstance(row, dict)
        for number in [_get_number(row, *keys)]
        if number is not None and number > 0
    ]
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _historical_pe(rows: Any) -> float | None:
    direct = _median_positive_number(
        rows,
        "peRatio",
        "peRatioTTM",
        "priceEarningsRatio",
        "priceToEarningsRatio",
        "priceToEarningsRatioTTM",
    )
    if direct is not None:
        return direct
    if not isinstance(rows, list):
        return None
    derived = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        earnings_yield = _get_number(row, "earningsYield", "earningsYieldTTM")
        if earnings_yield is not None and earnings_yield > 0:
            derived.append(1 / earnings_yield)
    if not derived:
        return None
    ordered = sorted(derived)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _positive_or_none(value: float | None) -> bool | None:
    if value is None:
        return None
    return value > 0


def _next_earnings_date(rows: list[dict[str, Any]], today: str) -> date | None:
    today_date = _parse_date(today)
    candidates = [_parse_date(row["date"]) for row in rows if row.get("date")]
    future = [candidate for candidate in candidates if candidate >= today_date]
    return min(future) if future else None


def _last_earnings_date(rows: list[dict[str, Any]], today: str) -> date | None:
    today_date = _parse_date(today)
    candidates = [_parse_date(row["date"]) for row in rows if row.get("date")]
    past = [candidate for candidate in candidates if candidate < today_date]
    return max(past) if past else None


def _post_earnings_gap(candles: list[Candle], earnings_date: date | None) -> float | None:
    if earnings_date is None or len(candles) < 2:
        return None
    ordered = sorted(candles, key=lambda candle: candle.date)
    after_index = next(
        (
            index
            for index, candle in enumerate(ordered)
            if _parse_date(candle.date) >= earnings_date
        ),
        None,
    )
    if after_index is None or after_index == 0:
        return None
    before = ordered[after_index - 1].close
    after = ordered[after_index].open
    if before == 0:
        return None
    return (after - before) / before


def _parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _cvd_proxy(rows: list[dict[str, Any]]) -> float | None:
    total_buy = 0.0
    total_sell = 0.0
    for row in rows:
        buy = _get_number(row, "buyVol", "buyVolValue", "buyVolume") or 0.0
        sell = _get_number(row, "sellVol", "sellVolValue", "sellVolume") or 0.0
        total_buy += buy
        total_sell += sell
    total = total_buy + total_sell
    if total == 0:
        return None
    return (total_buy - total_sell) / total


def _open_interest_change(payload: Any) -> float | None:
    if not isinstance(payload, list) or len(payload) < 2:
        return None
    first = _get_number(payload[0], "sumOpenInterest", "openInterest")
    last = _get_number(payload[-1], "sumOpenInterest", "openInterest")
    if first is None or first == 0 or last is None:
        return None
    return (last - first) / first


def _coinbase_premium(
    payload: dict[str, Any],
    reference_close: float | None,
    *,
    timestamps_compatible: bool = False,
) -> float | None:
    if not reference_close or not timestamps_compatible:
        return None
    coinbase_price = _get_number(payload, "price", "last", "close")
    if coinbase_price is None:
        return None
    return (coinbase_price - reference_close) / reference_close


def _crypto_metric_provenance(
    *,
    supplied: dict[str, dict[str, object]] | None,
    data_source: str,
    price_metadata: DataFetchMetadata | None,
    candles: list[Candle],
    funding_rate: float | None,
    current_open_interest: float | None,
    open_interest_change: float | None,
    cvd_proxy: float | None,
    coinbase_payload: dict[str, Any],
    liquidation_imbalance: float | None,
) -> dict[str, dict[str, object]]:
    price_provider = price_metadata.provider if price_metadata is not None else data_source
    price_endpoint = price_metadata.endpoint if price_metadata is not None else "klines"
    price_kind = price_metadata.market_data_kind if price_metadata is not None else "eod_candle"
    price_granularity = price_metadata.granularity if price_metadata is not None else "daily"
    default = {
        "candles": {"provider": price_provider, "endpoint": price_endpoint, "status": "available" if candles else "provider_unavailable", "granularity": price_granularity, "market_data_kind": price_kind},
        "spot": {"provider": price_provider, "endpoint": price_endpoint, "status": "available" if candles else "provider_unavailable", "granularity": price_granularity, "market_data_kind": price_kind},
        "funding": {"provider": "binance", "endpoint": "funding_rate", "status": "available" if funding_rate is not None else "provider_unavailable", "value": funding_rate},
        "current_open_interest": {"provider": "binance", "endpoint": "open_interest", "status": "available" if current_open_interest is not None else "provider_unavailable", "value": current_open_interest},
        "open_interest_change": {"provider": "binance", "endpoint": "open_interest_history", "status": "available" if open_interest_change is not None else "provider_unavailable", "value": open_interest_change},
        "cvd": {"provider": "binance", "endpoint": "taker_long_short_ratio", "status": "available" if cvd_proxy is not None else "provider_unavailable", "value": cvd_proxy, "is_proxy": True},
        "premium": {"provider": "coinbase", "endpoint": "coinbase_product", "status": "incompatible_time" if _get_number(coinbase_payload, "price", "last", "close") is not None and candles else "provider_unavailable", "timestamps_compatible": False},
        "liquidations": {"provider": "binance", "endpoint": None, "status": "not_implemented" if liquidation_imbalance is None else "available", "value": liquidation_imbalance},
    }
    for metric, values in (supplied or {}).items():
        if metric in {"candles", "spot"} and candles and values.get("status") == "provider_unavailable":
            continue
        default.setdefault(metric, {}).update(values)
    return default
