from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from advisor.models import AssetSnapshot, Candle, EventInfo, Fundamentals


def load_scan_fixture(path: Path) -> dict[str, Any]:
    fixture_file = path / "scan.json"
    with fixture_file.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def snapshots_from_fixture(payload: dict[str, Any]) -> list[AssetSnapshot]:
    return [_snapshot_from_item(item) for item in payload.get("assets", [])]


def benchmarks_from_fixture(payload: dict[str, Any]) -> dict[str, list[Candle]]:
    return {
        symbol: [_candle_from_row(row) for row in rows]
        for symbol, rows in payload.get("benchmarks", {}).items()
    }


def _snapshot_from_item(item: dict[str, Any]) -> AssetSnapshot:
    candles = [
        _candle_from_row(row)
        for row in item.get("candles", _synthetic_candles())
    ]
    fundamentals = Fundamentals(
        pe=item.get("pe"),
        peg=item.get("peg"),
        historical_pe=item.get("historical_pe"),
        revenue_growth=item.get("revenue_growth"),
        eps_growth=item.get("eps_growth"),
        margin_trend=item.get("margin_trend"),
        free_cash_flow_positive=item.get("free_cash_flow_positive"),
        market_cap=item.get("market_cap"),
        average_volume=item.get("average_volume"),
        market_cap_rank=item.get("market_cap_rank"),
    )
    missing_data = list(item.get("missing_data", []))
    event = None
    if item.get("asset_type") == "stock":
        if "guidance_recent" not in item:
            missing_data.append("guidance_recent_not_collected")
        if "post_earnings_gap_percent" not in item:
            missing_data.append("post_earnings_gap_not_collected")
        event = EventInfo(
            days_to_earnings=item.get("days_to_earnings"),
            guidance_recent=bool(item["guidance_recent"]) if "guidance_recent" in item else None,
            post_earnings_gap_percent=(
                float(item["post_earnings_gap_percent"])
                if item.get("post_earnings_gap_percent") is not None
                else None
            ),
            last_earnings_date=item.get("last_earnings_date"),
            next_earnings_date=item.get("next_earnings_date"),
        )
    return AssetSnapshot(
        symbol=item["symbol"],
        asset_type=item["asset_type"],
        theme=item.get("theme", "unknown"),
        candles=candles,
        fundamentals=fundamentals,
        event=event,
        funding_rate=item.get("funding_rate"),
        open_interest_change=item.get("open_interest_change"),
        cvd_proxy=item.get("cvd_proxy"),
        coinbase_premium=item.get("coinbase_premium"),
        liquidation_imbalance=item.get("liquidation_imbalance"),
        missing_data=sorted(set(missing_data)),
        news_events=list(item.get("news_events", [])),
        data_source=item.get("data_source", "fixture"),
        data_timestamp=item.get("data_timestamp"),
        cache_age_seconds=item.get("cache_age_seconds"),
    )


def _synthetic_candles() -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    for index in range(220):
        close = 100 + index * 0.4
        rows.append(
            {
                "date": f"2026-01-{(index % 28) + 1:02d}",
                "open": close - 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_000_000,
            }
        )
    return rows


def _candle_from_row(row: dict[str, Any]) -> Candle:
    return Candle(
        row["date"],
        float(row["open"]),
        float(row["high"]),
        float(row["low"]),
        float(row["close"]),
        float(row["volume"]),
    )
