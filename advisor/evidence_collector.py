from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence

from advisor.config import AdvisorConfig
from advisor.data_sources import (
    AlphaVantageSource,
    BinanceSource,
    FmpSource,
    HyperliquidSource,
    qualified_binance_klines_basis_claim,
    qualified_fmp_full_price_basis_claim,
    qualified_hyperliquid_candle_snapshot_basis_claim,
)
from advisor.evidence_schema import canonical_json_bytes


PRICE_PROVIDER_ASSIGNMENT_POLICY_VERSION = "price_provider_assignment_v1"
US_EQUITIES_SESSION_POLICY_VERSION = "us_equities_session_v1"
SessionCandleStatus = Literal["accepted", "rejected"]


@dataclass(frozen=True)
class CollectionAsset:
    symbol: str
    asset_type: Literal["stock", "etf", "crypto"]


@dataclass(frozen=True)
class CanonicalSplitRatio:
    new_shares: str
    old_shares: str


def assigned_price_provider(
    *, asset: CollectionAsset, existing_provider: str | None
) -> str | None:
    """Return the v1 provider assignment without probing any provider."""
    if existing_provider is not None:
        return existing_provider
    if asset.asset_type in {"stock", "etf"}:
        return "fmp"
    if asset.asset_type == "crypto":
        return "hyperliquid" if _normalized_symbol(asset) == "HYPE" else "binance"
    return None


def normalize_split_factor(*, split_factor: str | Decimal) -> CanonicalSplitRatio:
    """Represent an Alpha Vantage decimal split factor as an exact ratio."""
    if isinstance(split_factor, bool) or not isinstance(split_factor, (str, Decimal)):
        raise TypeError("split_factor must be a string or Decimal")
    try:
        decimal_value = Decimal(split_factor)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid_split_factor") from exc
    if not decimal_value.is_finite() or decimal_value <= 0:
        raise ValueError("invalid_split_factor")

    sign, digits, exponent = decimal_value.normalize().as_tuple()
    numerator = 0
    for digit in digits:
        numerator = numerator * 10 + digit
    if sign or numerator <= 0:
        raise ValueError("invalid_split_factor")
    if exponent >= 0:
        numerator *= 10**exponent
        denominator = 1
    else:
        denominator = 10 ** (-exponent)
    divisor = math.gcd(numerator, denominator)
    return CanonicalSplitRatio(
        new_shares=str(numerator // divisor),
        old_shares=str(denominator // divisor),
    )


class EvidenceCollector:
    """Collect raw assigned-provider transport without archive authority."""

    def __init__(
        self,
        *,
        fetch_json: Callable[..., object],
        transport_root: Path,
        now_utc: datetime | None = None,
    ):
        self._fetch_json = fetch_json
        self._transport_root = Path(transport_root)
        self._now_utc = _require_aware_utc(now_utc or datetime.now(timezone.utc))
        self._config = AdvisorConfig.default()

    def collect_market(
        self,
        *,
        assets: Sequence[CollectionAsset],
        existing_provider_by_symbol: Mapping[str, str],
    ) -> Path:
        records: list[dict[str, object]] = []
        for asset in assets:
            symbol = _normalized_symbol(asset)
            existing_provider = existing_provider_by_symbol.get(symbol)
            provider = assigned_price_provider(asset=asset, existing_provider=existing_provider)
            record = _market_record_base(asset=asset, symbol=symbol, provider=provider)
            if not _is_supported_asset(asset=asset, symbol=symbol) or provider not in {
                "fmp",
                "binance",
                "hyperliquid",
            }:
                record["status"] = "market_data_unavailable"
                records.append(record)
                continue
            try:
                payload, source_request, claim = self._fetch_market_payload(
                    symbol=symbol, provider=provider
                )
            except (OSError, RuntimeError, ValueError, TypeError):
                record["status"] = "market_data_unavailable"
                records.append(record)
                continue

            bars = _valid_market_bars(
                payload=payload,
                provider=provider,
                asset_type=asset.asset_type,
                now_utc=self._now_utc,
            )
            record["source_request"] = source_request
            record["semantic_provenance"] = {
                "collection_policy_version": PRICE_PROVIDER_ASSIGNMENT_POLICY_VERSION,
                "price_basis_claim": _claim_projection(claim),
                "price_provider": provider,
                "source_response_sha256": _response_sha256(payload),
            }
            if not bars:
                record["status"] = "market_data_unavailable"
                records.append(record)
                continue
            record["bars"] = bars
            record["coverage_window"] = {
                "start_date": bars[0]["market_date"],
                "end_date": bars[-1]["market_date"],
            }
            record["status"] = "available"
            records.append(record)
        return self._write_transport("market-transport.json", records)

    def collect_corporate_actions(
        self,
        *,
        assets: Sequence[CollectionAsset],
        coverage_windows: Mapping[str, tuple[date, date]],
    ) -> Path:
        records: list[dict[str, object]] = []
        for asset in assets:
            symbol = _normalized_symbol(asset)
            if asset.asset_type not in {"stock", "etf"}:
                continue
            window = coverage_windows.get(symbol)
            if window is None:
                continue
            start_date, end_date = window
            record: dict[str, object] = {
                "asset_type": asset.asset_type,
                "coverage_window": {
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
                "corporate_action_provider": "alpha_vantage",
                "function": "SPLITS",
                "normalized_events": [],
                "source_request": {
                    "function": "SPLITS",
                    "provider": "alpha_vantage",
                    "source_contract": "alpha_vantage.splits.v1",
                },
                "symbol": symbol,
            }
            try:
                payload = self._fetch_json(
                    provider="alpha_vantage",
                    symbol=symbol,
                    url=AlphaVantageSource(self._config.alphavantage_api_key).splits_url(symbol),
                    function="SPLITS",
                )
            except (OSError, RuntimeError, ValueError, TypeError):
                record["status"] = "feed_unavailable"
                records.append(record)
                continue
            try:
                normalized_events = _normalized_split_events(payload)
            except (ValueError, TypeError):
                record["status"] = "feed_unavailable"
                records.append(record)
                continue
            record["normalized_events"] = normalized_events
            record["semantic_provenance"] = {
                "corporate_action_provider": "alpha_vantage",
                "source_response_sha256": _response_sha256(payload),
            }
            record["status"] = "available"
            records.append(record)
        return self._write_transport("corporate-actions-transport.json", records)

    def _fetch_market_payload(
        self, *, symbol: str, provider: str
    ) -> tuple[object, dict[str, object], object]:
        if provider == "fmp":
            payload = self._fetch_json(
                provider="fmp",
                symbol=symbol,
                url=FmpSource(self._config.fmp_api_key).historical_prices_url(symbol),
            )
            return (
                payload,
                {
                    "provider": "fmp",
                    "source_contract": "fmp.historical_price_eod.full.raw_ohlcv_v1",
                    "interval": "1d",
                },
                qualified_fmp_full_price_basis_claim(),
            )
        if provider == "binance":
            payload = self._fetch_json(
                provider="binance",
                symbol=symbol,
                url=BinanceSource().klines_url(f"{symbol}USDT"),
                interval="1d",
            )
            return (
                payload,
                {
                    "provider": "binance",
                    "source_contract": "binance.futures_klines.raw_ohlcv_v1",
                    "interval": "1d",
                    "pair": f"{symbol}USDT",
                },
                qualified_binance_klines_basis_claim(),
            )
        if provider == "hyperliquid":
            end_time_ms = int(self._now_utc.timestamp() * 1000)
            start_time_ms = int((self._now_utc - timedelta(days=500)).timestamp() * 1000)
            payload = self._fetch_json(
                provider="hyperliquid",
                symbol=symbol,
                url=HyperliquidSource().info_url(),
                payload=HyperliquidSource().candle_snapshot_payload(
                    symbol,
                    start_time_ms=start_time_ms,
                    end_time_ms=end_time_ms,
                ),
            )
            return (
                payload,
                {
                    "provider": "hyperliquid",
                    "source_contract": "hyperliquid.candle_snapshot.raw_ohlcv_v1",
                    "interval": "1d",
                },
                qualified_hyperliquid_candle_snapshot_basis_claim(),
            )
        raise ValueError("unsupported_assigned_provider")

    def _write_transport(self, filename: str, records: list[dict[str, object]]) -> Path:
        self._transport_root.mkdir(parents=True, exist_ok=True)
        destination = self._transport_root / filename
        destination.write_text(
            json.dumps({"records": records}, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return destination


def us_eastern_dst_transition_utc(
    year: int, *, transition: Literal["start", "end"]
) -> datetime:
    if transition == "start":
        return datetime.combine(_nth_weekday(year, 3, weekday=6, occurrence=2), time(7), tzinfo=timezone.utc)
    if transition == "end":
        return datetime.combine(_nth_weekday(year, 11, weekday=6, occurrence=1), time(6), tzinfo=timezone.utc)
    raise ValueError("invalid_dst_transition")


def us_eastern_offset_for_utc(utc_datetime: datetime) -> timedelta:
    utc_value = _require_aware_utc(utc_datetime)
    start = us_eastern_dst_transition_utc(utc_value.year, transition="start")
    end = us_eastern_dst_transition_utc(utc_value.year, transition="end")
    return timedelta(hours=-4) if start <= utc_value < end else timedelta(hours=-5)


def us_eastern_local(utc_datetime: datetime) -> datetime:
    utc_value = _require_aware_utc(utc_datetime)
    offset = us_eastern_offset_for_utc(utc_value)
    return utc_value.astimezone(timezone(offset))


def us_market_holidays(year: int) -> frozenset[date]:
    holidays = {
        _observed_fixed_holiday(date(year, 1, 1)),
        _nth_weekday(year, 1, weekday=0, occurrence=3),
        _nth_weekday(year, 2, weekday=0, occurrence=3),
        _western_easter(year) - timedelta(days=2),
        _last_weekday(year, 5, weekday=0),
        _observed_fixed_holiday(date(year, 6, 19)),
        _observed_fixed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, weekday=0, occurrence=1),
        _nth_weekday(year, 11, weekday=3, occurrence=4),
        _observed_fixed_holiday(date(year, 12, 25)),
    }
    next_new_year = _observed_fixed_holiday(date(year + 1, 1, 1))
    if next_new_year.year == year:
        holidays.add(next_new_year)
    return frozenset(holidays)


def us_early_close_dates(year: int) -> frozenset[date]:
    holidays = us_market_holidays(year)
    candidates = {
        _preceding_weekday(date(year, 7, 4)),
        _preceding_weekday(date(year, 12, 25)),
        _nth_weekday(year, 11, weekday=3, occurrence=4) + timedelta(days=1),
    }
    return frozenset(candidate for candidate in candidates if candidate not in holidays)


def us_equity_session_close(session_date: date) -> time:
    return time(13) if session_date in us_early_close_dates(session_date.year) else time(16)


def validate_us_equity_candle(
    *, market_date: date, now_utc: datetime, synthetic: bool
) -> SessionCandleStatus:
    now = _require_aware_utc(now_utc)
    if synthetic or market_date.weekday() >= 5 or market_date in us_market_holidays(market_date.year):
        return "rejected"
    session_offset = timedelta(hours=-4) if _session_uses_edt(market_date) else timedelta(hours=-5)
    close_local = datetime.combine(market_date, us_equity_session_close(market_date))
    close_utc = (close_local - session_offset).replace(tzinfo=timezone.utc)
    return "accepted" if now >= close_utc else "rejected"


def validate_crypto_candle(
    *, market_date: date, now_utc: datetime, synthetic: bool
) -> SessionCandleStatus:
    now = _require_aware_utc(now_utc)
    return "accepted" if not synthetic and market_date < now.date() else "rejected"


def _normalized_symbol(asset: CollectionAsset) -> str:
    return asset.symbol.strip().upper() if isinstance(asset.symbol, str) else ""


def _is_supported_asset(*, asset: CollectionAsset, symbol: str) -> bool:
    if asset.asset_type not in {"stock", "etf", "crypto"} or not symbol:
        return False
    configured = AdvisorConfig.default()
    configured_stocks, configured_cryptos = configured.symbols_for_scan(include_discovery=True)
    etfs = {"IGV", "QQQ", "SMH", "SPY"}
    if asset.asset_type == "stock":
        return symbol in set(configured_stocks)
    if asset.asset_type == "etf":
        return symbol in etfs
    return symbol in set(configured_cryptos)


def _market_record_base(
    *, asset: CollectionAsset, symbol: str, provider: str | None
) -> dict[str, object]:
    return {
        "asset_type": asset.asset_type,
        "assigned_provider": provider,
        "assignment_policy_version": PRICE_PROVIDER_ASSIGNMENT_POLICY_VERSION,
        "bars": [],
        "coverage_window": None,
        "interval": "1d",
        "market_timezone": "UTC" if asset.asset_type == "crypto" else "America/New_York",
        "symbol": symbol,
    }


def _valid_market_bars(
    *, payload: object, provider: str, asset_type: str, now_utc: datetime
) -> list[dict[str, object]]:
    rows = _market_rows(payload=payload, provider=provider)
    bars: list[dict[str, object]] = []
    for row in rows:
        parsed = _parse_bar(row=row, provider=provider)
        if parsed is None:
            continue
        market_date, open_price, high, low, close, volume, synthetic = parsed
        session_status = (
            validate_crypto_candle(market_date=market_date, now_utc=now_utc, synthetic=synthetic)
            if asset_type == "crypto"
            else validate_us_equity_candle(market_date=market_date, now_utc=now_utc, synthetic=synthetic)
        )
        if session_status != "accepted":
            continue
        bars.append(
            {
                "market_date": market_date.isoformat(),
                "ohlcv": {
                    "close": close,
                    "high": high,
                    "low": low,
                    "open": open_price,
                    "volume": volume,
                },
                "price_basis": "raw_ohlcv",
                "session_close_type": "early" if asset_type != "crypto" and us_equity_session_close(market_date) == time(13) else "regular",
                "session_status": "complete",
            }
        )
    return sorted(bars, key=lambda bar: str(bar["market_date"]))


def _market_rows(*, payload: object, provider: str) -> list[object]:
    if provider == "fmp" and isinstance(payload, Mapping):
        rows = payload.get("historical")
        return list(rows) if isinstance(rows, list) else []
    return list(payload) if isinstance(payload, list) else []


def _parse_bar(
    *, row: object, provider: str
) -> tuple[date, float, float, float, float, float, bool] | None:
    try:
        if provider == "fmp":
            if not isinstance(row, Mapping):
                return None
            market_date = date.fromisoformat(str(row["date"])[:10])
            open_price, high, low, close, volume = (
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
            )
            synthetic = bool(row.get("synthetic") or row.get("forward_filled"))
        elif provider == "binance":
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 6:
                return None
            market_date = datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc).date()
            open_price, high, low, close, volume = (float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
            synthetic = False
        else:
            if not isinstance(row, Mapping):
                return None
            market_date = datetime.fromtimestamp(int(row["t"]) / 1000, timezone.utc).date()
            open_price, high, low, close, volume = (float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"]), float(row["v"]))
            synthetic = bool(row.get("synthetic") or row.get("forward_filled"))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    values = (open_price, high, low, close, volume)
    if not all(math.isfinite(value) for value in values):
        return None
    if open_price <= 0 or high <= 0 or low <= 0 or close <= 0 or volume < 0:
        return None
    if low > open_price or open_price > high or low > close or close > high:
        return None
    return market_date, open_price, high, low, close, volume, synthetic


def _normalized_split_events(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, Mapping):
        raise ValueError("invalid_splits_payload")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("invalid_splits_payload")
    events: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("invalid_splits_payload")
        effective_date = date.fromisoformat(str(row["effective_date"])[:10]).isoformat()
        raw_factor = row["split_factor"]
        if not isinstance(raw_factor, str):
            raise ValueError("invalid_splits_payload")
        ratio = normalize_split_factor(split_factor=raw_factor)
        events.append(
            {
                "effective_date": effective_date,
                "split_factor_raw": raw_factor,
                "split_ratio": {
                    "new_shares": ratio.new_shares,
                    "old_shares": ratio.old_shares,
                },
            }
        )
    return sorted(events, key=lambda event: str(event["effective_date"]))


def _claim_projection(claim: object) -> dict[str, object]:
    return {
        "price_basis": getattr(claim, "price_basis"),
        "price_basis_policy_version": getattr(claim, "price_basis_policy_version"),
        "source_contract": getattr(claim, "source_contract"),
    }


def _response_sha256(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _require_aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("aware_utc_datetime_required")
    return value.astimezone(timezone.utc)


def _nth_weekday(year: int, month: int, *, weekday: int, occurrence: int) -> date:
    candidate = date(year, month, 1)
    candidate += timedelta(days=(weekday - candidate.weekday()) % 7 + (occurrence - 1) * 7)
    return candidate


def _last_weekday(year: int, month: int, *, weekday: int) -> date:
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    candidate = next_month - timedelta(days=1)
    return candidate - timedelta(days=(candidate.weekday() - weekday) % 7)


def _observed_fixed_holiday(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def _preceding_weekday(value: date) -> date:
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _western_easter(year: int) -> date:
    golden = year % 19
    century = year // 100
    remainder = year % 100
    leap_century = century // 4
    century_remainder = century % 4
    correction = (century + 8) // 25
    adjustment = (century - correction + 1) // 3
    epact = (19 * golden + century - leap_century - adjustment + 15) % 30
    year_quarter = remainder // 4
    year_remainder = remainder % 4
    weekday_adjustment = (32 + 2 * century_remainder + 2 * year_quarter - epact - year_remainder) % 7
    correction_day = (golden + 11 * epact + 22 * weekday_adjustment) // 451
    month = (epact + weekday_adjustment - 7 * correction_day + 114) // 31
    day = (epact + weekday_adjustment - 7 * correction_day + 114) % 31 + 1
    return date(year, month, day)


def _session_uses_edt(session_date: date) -> bool:
    start_date = us_eastern_dst_transition_utc(session_date.year, transition="start").date()
    end_date = us_eastern_dst_transition_utc(session_date.year, transition="end").date()
    return start_date <= session_date < end_date
