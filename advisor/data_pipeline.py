from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from advisor.models import AssetSnapshot, Candle, EventInfo, Fundamentals


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
    earnings_date = _next_earnings_date(earnings_payload, today)
    last_earnings_date = _last_earnings_date(earnings_payload, today)

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
    return AssetSnapshot(
        symbol=symbol,
        asset_type="stock",
        theme=theme,
        candles=candles,
        fundamentals=fundamentals,
        event=event,
        missing_data=sorted(set(stock_missing_data)),
        data_source=data_source,
        data_timestamp=data_timestamp,
        cache_age_seconds=cache_age_seconds,
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
    coinbase_premium = _coinbase_premium(coinbase_payload or {}, candles[-1].close if candles else None)
    liquidation_imbalance = _liquidation_imbalance(liquidation_payload or [])
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
        data_timestamp=data_timestamp,
        cache_age_seconds=cache_age_seconds,
    )


def binance_crypto_flow_from_payloads(
    *,
    funding_payload: Any,
    funding_info_payload: Any = None,
    symbol: str | None = None,
    open_interest_payload: Any,
    taker_payload: Any,
    liquidation_payload: Any,
) -> dict[str, Any]:
    funding_rate = binance_funding_rate_8h_from_payloads(
        funding_payload=funding_payload,
        funding_info_payload=funding_info_payload,
        symbol=symbol,
    )
    open_interest_change = _open_interest_change(open_interest_payload)
    open_interest = _get_number(_last(open_interest_payload), "sumOpenInterest", "openInterest")
    cvd_proxy = _cvd_proxy(taker_payload if isinstance(taker_payload, list) else [])
    liquidation_rows = liquidation_payload if isinstance(liquidation_payload, list) else []
    liquidation_imbalance = _liquidation_imbalance(liquidation_rows)
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
    else:
        limitations.append("liquidations_history_may_be_incomplete")
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


def _coinbase_premium(payload: dict[str, Any], reference_close: float | None) -> float | None:
    if not reference_close:
        return None
    coinbase_price = _get_number(payload, "price", "last", "close")
    if coinbase_price is None:
        return None
    return (coinbase_price - reference_close) / reference_close


def _liquidation_imbalance(rows: list[dict[str, Any]]) -> float | None:
    long_liquidations = 0.0
    short_liquidations = 0.0
    for row in rows:
        qty = _get_number(row, "executedQty", "origQty", "quantity") or 0.0
        price = _get_number(row, "averagePrice", "avgPrice", "price") or 0.0
        notional = qty * price
        side = str(row.get("side", "")).upper()
        if side == "SELL":
            long_liquidations += notional
        elif side == "BUY":
            short_liquidations += notional
    total = long_liquidations + short_liquidations
    if total == 0:
        return None
    return (long_liquidations - short_liquidations) / total
