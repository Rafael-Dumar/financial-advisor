from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from advisor.signal_observation import SignalObservation, canonical_json_bytes, canonical_utc_timestamp


SCHEMA_VERSION = "1.0"
EVALUATION_POLICY_VERSION = "1.0"
HORIZONS = (5, 10, 20, 40)
ASSET_TYPES = frozenset({"stock", "crypto"})
EVALUATION_ROLES = frozenset(
    {
        "trade_candidate",
        "conditional_candidate",
        "observational_candidate",
        "observational_wait",
        "observational_avoid",
        "observational_blocked",
        "observational_other",
    }
)
ENTRY_SEMANTICS = "reference_close_not_fill"
OUTCOME_SEMANTICS = "reference_price_observation_not_execution"
_ASSET_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN_METADATA_PATTERN = re.compile(
    r"(?:api[_-]?key|authorization|bearer\s|https?://|[A-Za-z]:\\|exception|headers?)",
    re.IGNORECASE,
)
_BOOLEAN_FIELDS = (
    "stop_touched",
    "target_2r_touched",
    "target_3r_touched",
    "same_bar_stop_target_2r",
    "same_bar_stop_target_3r",
    "alternative_entry_threshold_reached",
)


class OutcomeInputError(ValueError):
    """A local market-series input failed the policy-1.0 input contract."""


class OutcomeUnavailable(ValueError):
    """A canonical observation cannot be evaluated against the supplied series."""


@dataclass(frozen=True)
class ForwardCandle:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        normalized_date = _canonical_date(self.date)
        object.__setattr__(self, "date", normalized_date)
        open_price = _finite_number(self.open, "invalid_candle_open")
        high = _finite_number(self.high, "invalid_candle_high")
        low = _finite_number(self.low, "invalid_candle_low")
        close = _finite_number(self.close, "invalid_candle_close")
        volume = _finite_number(self.volume, "invalid_candle_volume")
        if min(open_price, high, low, close) <= 0:
            raise OutcomeInputError("invalid_candle_price")
        if volume < 0:
            raise OutcomeInputError("invalid_candle_volume")
        if not (low <= open_price <= high and low <= close <= high and low <= high):
            raise OutcomeInputError("invalid_candle_ohlc_order")
        object.__setattr__(self, "open", open_price)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "volume", volume)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ForwardCandle":
        required = {"date", "open", "high", "low", "close", "volume"}
        if not isinstance(value, Mapping) or not required.issubset(value):
            raise OutcomeInputError("invalid_candle_fields")
        return cls(
            date=value["date"],  # type: ignore[arg-type]
            open=value["open"],  # type: ignore[arg-type]
            high=value["high"],  # type: ignore[arg-type]
            low=value["low"],  # type: ignore[arg-type]
            close=value["close"],  # type: ignore[arg-type]
            volume=value["volume"],  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "close": self.close,
            "date": self.date,
            "high": self.high,
            "low": self.low,
            "open": self.open,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class ForwardMarketSeries:
    asset: str
    asset_type: str
    provider: str
    price_basis: str
    candles: tuple[ForwardCandle, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.asset, str) or _ASSET_PATTERN.fullmatch(self.asset) is None:
            raise OutcomeInputError("invalid_asset")
        if self.asset_type not in ASSET_TYPES:
            raise OutcomeInputError("invalid_asset_type")
        candles = tuple(self.candles)
        if not all(isinstance(candle, ForwardCandle) for candle in candles):
            raise OutcomeInputError("invalid_candles")
        ordered = tuple(sorted(candles, key=lambda candle: candle.date))
        if len({candle.date for candle in ordered}) != len(ordered):
            raise OutcomeInputError("duplicate_candle_date")
        object.__setattr__(self, "provider", _sanitize_metadata_text(self.provider))
        object.__setattr__(self, "price_basis", _sanitize_metadata_text(self.price_basis))
        object.__setattr__(self, "candles", ordered)


@dataclass(frozen=True)
class SignalForwardOutcome:
    outcome_id: str
    schema_version: str
    evaluation_policy_version: str
    signal_id: str
    observation_hash: str
    asset: str
    asset_type: str
    decision_label: str
    evaluation_role: str
    market_timezone: str
    signal_market_date: str
    horizon_bars: int
    horizon_start_date: str
    horizon_end_date: str
    reference_price: float
    entry_semantics: str
    outcome_semantics: str
    asset_bars_json: str
    asset_bars_hash: str
    forward_return_pct: float
    mfe_pct: float
    mae_pct: float
    stop: float
    target_2r: float
    target_3r: float
    stop_touched: bool
    first_stop_bar: int | None
    first_stop_date: str | None
    target_2r_touched: bool
    first_target_2r_bar: int | None
    first_target_2r_date: str | None
    target_3r_touched: bool
    first_target_3r_bar: int | None
    first_target_3r_date: str | None
    same_bar_stop_target_2r: bool
    same_bar_stop_target_3r: bool
    alternative_entry: float | None
    alternative_entry_threshold_reached: bool
    first_alternative_entry_bar: int | None
    first_alternative_entry_date: str | None
    provider: str
    price_basis: str
    outcome_hash: str = ""
    persisted_at_utc: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported_outcome_schema_version")
        if self.evaluation_policy_version != EVALUATION_POLICY_VERSION:
            raise ValueError("unsupported_evaluation_policy_version")
        if self.horizon_bars not in HORIZONS:
            raise ValueError("invalid_horizon_bars")
        if _HASH_PATTERN.fullmatch(self.outcome_id) is None:
            raise ValueError("invalid_outcome_id")
        if _HASH_PATTERN.fullmatch(self.signal_id) is None:
            raise ValueError("invalid_signal_id")
        if _HASH_PATTERN.fullmatch(self.observation_hash) is None:
            raise ValueError("invalid_observation_hash")
        if self.outcome_hash and _HASH_PATTERN.fullmatch(self.outcome_hash) is None:
            raise ValueError("invalid_outcome_hash")
        if self.asset_type not in ASSET_TYPES:
            raise ValueError("invalid_asset_type")
        if self.evaluation_role not in EVALUATION_ROLES:
            raise ValueError("invalid_evaluation_role")
        if self.entry_semantics != ENTRY_SEMANTICS:
            raise ValueError("invalid_entry_semantics")
        if self.outcome_semantics != OUTCOME_SEMANTICS:
            raise ValueError("invalid_outcome_semantics")
        if self.market_timezone != ("America/New_York" if self.asset_type == "stock" else "UTC"):
            raise ValueError("invalid_market_timezone")
        for value, error_code in (
            (self.reference_price, "invalid_reference_price"),
            (self.stop, "invalid_stop"),
            (self.target_2r, "invalid_target_2r"),
            (self.target_3r, "invalid_target_3r"),
            (self.forward_return_pct, "invalid_forward_return"),
            (self.mfe_pct, "invalid_mfe"),
            (self.mae_pct, "invalid_mae"),
        ):
            _finite_number(value, error_code)
        if self.reference_price <= 0:
            raise ValueError("invalid_reference_price")
        if self.persisted_at_utc is not None:
            canonical_utc_timestamp(self.persisted_at_utc)


@dataclass(frozen=True)
class SignalForwardEvaluation:
    outcomes: tuple[SignalForwardOutcome, ...]
    pending_horizons: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", tuple(self.outcomes))
        object.__setattr__(self, "pending_horizons", tuple(self.pending_horizons))


@dataclass(frozen=True)
class SignalForwardOutcomeWriteResult:
    status: str
    outcomes_written: int = 0
    duplicate_same: int = 0
    conflicts: tuple[str, ...] = ()
    error_code: str | None = None


def load_forward_market_input(path: Path | str) -> dict[str, ForwardMarketSeries]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise OutcomeInputError("invalid_input") from None
    return parse_forward_market_input(payload)


def parse_forward_market_input(payload: object) -> dict[str, ForwardMarketSeries]:
    if not isinstance(payload, Mapping) or payload.get("schema_version") != SCHEMA_VERSION:
        raise OutcomeInputError("invalid_input")
    assets = payload.get("assets")
    if not isinstance(assets, Mapping):
        raise OutcomeInputError("invalid_assets")
    series_by_asset: dict[str, ForwardMarketSeries] = {}
    for asset, raw in assets.items():
        if not isinstance(asset, str) or not isinstance(raw, Mapping):
            raise OutcomeInputError("invalid_asset_series")
        if not {"asset_type", "provider", "price_basis", "candles"}.issubset(raw):
            raise OutcomeInputError("invalid_asset_series")
        candles_payload = raw.get("candles")
        if not isinstance(candles_payload, list):
            raise OutcomeInputError("invalid_candles")
        candles = tuple(
            ForwardCandle.from_mapping(candle) if isinstance(candle, Mapping) else _invalid_candle()
            for candle in candles_payload
        )
        series_by_asset[asset] = ForwardMarketSeries(
            asset=asset,
            asset_type=raw.get("asset_type"),  # type: ignore[arg-type]
            provider=raw.get("provider"),  # type: ignore[arg-type]
            price_basis=raw.get("price_basis"),  # type: ignore[arg-type]
            candles=candles,
        )
    return series_by_asset


def signal_market_date(signal_timestamp_utc: str, market_timezone: str) -> str:
    parsed = _parse_utc_timestamp(signal_timestamp_utc)
    if market_timezone == "UTC":
        return parsed.date().isoformat()
    if market_timezone != "America/New_York":
        raise ValueError("invalid_market_timezone")
    offset = _us_eastern_offset(parsed)
    return (parsed + offset).date().isoformat()


def evaluate_signal_observation(
    observation: SignalObservation | Mapping[str, object],
    series: ForwardMarketSeries,
) -> SignalForwardEvaluation:
    values = _observation_values(observation)
    asset = _required_text(values, "asset")
    asset_type = _required_text(values, "asset_type")
    if series.asset != asset:
        raise OutcomeUnavailable("asset_series_unavailable")
    if series.asset_type != asset_type:
        raise OutcomeUnavailable("asset_type_mismatch")
    expected_timezone = "America/New_York" if asset_type == "stock" else "UTC"
    if values.get("market_timezone") != expected_timezone:
        raise OutcomeUnavailable("market_timezone_mismatch")
    if asset_type not in ASSET_TYPES or values.get("evaluation_role") not in EVALUATION_ROLES:
        raise OutcomeUnavailable("invalid_canonical_observation")
    if values.get("entry_semantics") != ENTRY_SEMANTICS:
        raise OutcomeUnavailable("invalid_entry_semantics")

    signal_id = _required_hash(values, "signal_id")
    observation_hash = _required_hash(values, "observation_hash")
    market_timezone = expected_timezone
    signal_date = signal_market_date(_required_text(values, "signal_timestamp_utc"), market_timezone)
    reference_price = _finite_number(values.get("ideal_entry"), "invalid_reference_price")
    if reference_price <= 0:
        raise OutcomeUnavailable("invalid_reference_price")
    stop = _finite_number(values.get("stop"), "invalid_stop")
    target_2r = _finite_number(values.get("target_2r"), "invalid_target_2r")
    target_3r = _finite_number(values.get("target_3r"), "invalid_target_3r")
    alternative_entry = _optional_number(values.get("alternative_entry"), "invalid_alternative_entry")
    eligible = tuple(candle for candle in series.candles if candle.date > signal_date)

    outcomes: list[SignalForwardOutcome] = []
    pending: list[int] = []
    for horizon_bars in HORIZONS:
        if len(eligible) < horizon_bars:
            pending.append(horizon_bars)
            continue
        outcomes.append(
            _build_outcome(
                values=values,
                series=series,
                signal_id=signal_id,
                observation_hash=observation_hash,
                market_timezone=market_timezone,
                signal_date=signal_date,
                reference_price=reference_price,
                stop=stop,
                target_2r=target_2r,
                target_3r=target_3r,
                alternative_entry=alternative_entry,
                candles=eligible[:horizon_bars],
                horizon_bars=horizon_bars,
            )
        )
    return SignalForwardEvaluation(outcomes=tuple(outcomes), pending_horizons=tuple(pending))


def compute_outcome_id(
    *,
    schema_version: str,
    evaluation_policy_version: str,
    signal_id: str,
    observation_hash: str,
    horizon_bars: int,
) -> str:
    identity = {
        "evaluation_policy_version": evaluation_policy_version,
        "horizon_bars": horizon_bars,
        "observation_hash": observation_hash,
        "schema_version": schema_version,
        "signal_id": signal_id,
    }
    return _sha256(canonical_json_bytes(identity))


def compute_outcome_hash(outcome: SignalForwardOutcome) -> str:
    return _sha256(canonical_json_bytes(outcome_immutable_dict(outcome)))


def outcome_immutable_dict(outcome: SignalForwardOutcome) -> dict[str, object]:
    values = asdict(outcome)
    values.pop("outcome_hash", None)
    values.pop("persisted_at_utc", None)
    return values


def outcome_record(outcome: SignalForwardOutcome, *, persisted_at_utc: str) -> dict[str, object]:
    persisted_at_utc = canonical_utc_timestamp(persisted_at_utc)
    record = asdict(outcome)
    record["persisted_at_utc"] = persisted_at_utc
    for field_name in _BOOLEAN_FIELDS:
        record[field_name] = int(bool(record[field_name]))
    return record


def _build_outcome(
    *,
    values: Mapping[str, object],
    series: ForwardMarketSeries,
    signal_id: str,
    observation_hash: str,
    market_timezone: str,
    signal_date: str,
    reference_price: float,
    stop: float,
    target_2r: float,
    target_3r: float,
    alternative_entry: float | None,
    candles: tuple[ForwardCandle, ...],
    horizon_bars: int,
) -> SignalForwardOutcome:
    bars_json = canonical_json_bytes([candle.to_dict() for candle in candles]).decode("utf-8")
    bars_hash = _sha256(bars_json.encode("utf-8"))
    first_stop: tuple[int, str] | None = None
    first_target_2r: tuple[int, str] | None = None
    first_target_3r: tuple[int, str] | None = None
    first_alternative: tuple[int, str] | None = None
    same_bar_stop_target_2r = False
    same_bar_stop_target_3r = False
    for bar_index, candle in enumerate(candles, start=1):
        stop_hit = candle.low <= stop
        target_2r_hit = candle.high >= target_2r
        target_3r_hit = candle.high >= target_3r
        if stop_hit and first_stop is None:
            first_stop = (bar_index, candle.date)
        if target_2r_hit and first_target_2r is None:
            first_target_2r = (bar_index, candle.date)
        if target_3r_hit and first_target_3r is None:
            first_target_3r = (bar_index, candle.date)
        if stop_hit and target_2r_hit:
            same_bar_stop_target_2r = True
        if stop_hit and target_3r_hit:
            same_bar_stop_target_3r = True
        if alternative_entry is not None and candle.low <= alternative_entry and first_alternative is None:
            first_alternative = (bar_index, candle.date)

    outcome = SignalForwardOutcome(
        outcome_id=compute_outcome_id(
            schema_version=SCHEMA_VERSION,
            evaluation_policy_version=EVALUATION_POLICY_VERSION,
            signal_id=signal_id,
            observation_hash=observation_hash,
            horizon_bars=horizon_bars,
        ),
        schema_version=SCHEMA_VERSION,
        evaluation_policy_version=EVALUATION_POLICY_VERSION,
        signal_id=signal_id,
        observation_hash=observation_hash,
        asset=series.asset,
        asset_type=series.asset_type,
        decision_label=_required_text(values, "decision_label"),
        evaluation_role=_required_text(values, "evaluation_role"),
        market_timezone=market_timezone,
        signal_market_date=signal_date,
        horizon_bars=horizon_bars,
        horizon_start_date=candles[0].date,
        horizon_end_date=candles[-1].date,
        reference_price=reference_price,
        entry_semantics=ENTRY_SEMANTICS,
        outcome_semantics=OUTCOME_SEMANTICS,
        asset_bars_json=bars_json,
        asset_bars_hash=bars_hash,
        forward_return_pct=(candles[-1].close / reference_price) - 1,
        mfe_pct=max(0.0, max((candle.high / reference_price) - 1 for candle in candles)),
        mae_pct=min(0.0, min((candle.low / reference_price) - 1 for candle in candles)),
        stop=stop,
        target_2r=target_2r,
        target_3r=target_3r,
        stop_touched=first_stop is not None,
        first_stop_bar=first_stop[0] if first_stop else None,
        first_stop_date=first_stop[1] if first_stop else None,
        target_2r_touched=first_target_2r is not None,
        first_target_2r_bar=first_target_2r[0] if first_target_2r else None,
        first_target_2r_date=first_target_2r[1] if first_target_2r else None,
        target_3r_touched=first_target_3r is not None,
        first_target_3r_bar=first_target_3r[0] if first_target_3r else None,
        first_target_3r_date=first_target_3r[1] if first_target_3r else None,
        same_bar_stop_target_2r=same_bar_stop_target_2r,
        same_bar_stop_target_3r=same_bar_stop_target_3r,
        alternative_entry=alternative_entry,
        alternative_entry_threshold_reached=first_alternative is not None,
        first_alternative_entry_bar=first_alternative[0] if first_alternative else None,
        first_alternative_entry_date=first_alternative[1] if first_alternative else None,
        provider=series.provider,
        price_basis=series.price_basis,
    )
    return replace(outcome, outcome_hash=compute_outcome_hash(outcome))


def _observation_values(observation: SignalObservation | Mapping[str, object]) -> dict[str, object]:
    if isinstance(observation, SignalObservation):
        return asdict(observation)
    if isinstance(observation, Mapping):
        return dict(observation)
    raise OutcomeUnavailable("invalid_canonical_observation")


def _required_text(values: Mapping[str, object], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise OutcomeUnavailable(f"invalid_observation_{name}")
    return value


def _required_hash(values: Mapping[str, object], name: str) -> str:
    value = _required_text(values, name)
    if _HASH_PATTERN.fullmatch(value) is None:
        raise OutcomeUnavailable(f"invalid_observation_{name}")
    return value


def _optional_number(value: object, error_code: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, error_code)


def _finite_number(value: object, error_code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OutcomeInputError(error_code)
    number = float(value)
    if not math.isfinite(number):
        raise OutcomeInputError(error_code)
    return number


def _canonical_date(value: object) -> str:
    if not isinstance(value, str) or _DATE_PATTERN.fullmatch(value) is None:
        raise OutcomeInputError("invalid_candle_date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise OutcomeInputError("invalid_candle_date") from None
    return parsed.isoformat()


def _sanitize_metadata_text(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip()
    if not normalized or len(normalized) > 64 or _FORBIDDEN_METADATA_PATTERN.search(normalized):
        return "unknown"
    return normalized


def _invalid_candle() -> ForwardCandle:
    raise OutcomeInputError("invalid_candle_fields")


def _parse_utc_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid_utc_timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise ValueError("invalid_utc_timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("utc_timestamp_required")
    return parsed.astimezone(timezone.utc)


def _us_eastern_offset(timestamp_utc: datetime) -> timedelta:
    year = timestamp_utc.year
    if year >= 2007:
        start_date = _nth_sunday(year, 3, 2)
        end_date = _nth_sunday(year, 11, 1)
        start_hour_utc = 7
        end_hour_utc = 6
    elif year >= 1987:
        start_date = _nth_sunday(year, 4, 1)
        end_date = _last_sunday(year, 10)
        start_hour_utc = 7
        end_hour_utc = 6
    elif year >= 1967:
        start_date = _last_sunday(year, 4)
        end_date = _last_sunday(year, 10)
        start_hour_utc = 7
        end_hour_utc = 6
    else:
        return timedelta(hours=-5)
    start = datetime.combine(start_date, time(start_hour_utc), tzinfo=timezone.utc)
    end = datetime.combine(end_date, time(end_hour_utc), tzinfo=timezone.utc)
    return timedelta(hours=-4 if start <= timestamp_utc < end else -5)


def _nth_sunday(year: int, month: int, ordinal: int) -> date:
    first = date(year, month, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(days=7 * (ordinal - 1))


def _last_sunday(year: int, month: int) -> date:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 6) % 7)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


OUTCOME_TABLE_SQL = """
create table if not exists signal_forward_outcomes(
    outcome_id text primary key not null,
    schema_version text not null check (schema_version = '1.0'),
    evaluation_policy_version text not null check (evaluation_policy_version = '1.0'),
    signal_id text not null,
    observation_hash text not null,
    asset text not null,
    asset_type text not null check (asset_type in ('stock', 'crypto')),
    decision_label text not null,
    evaluation_role text not null check (
        evaluation_role in (
            'trade_candidate',
            'conditional_candidate',
            'observational_candidate',
            'observational_wait',
            'observational_avoid',
            'observational_blocked',
            'observational_other'
        )
    ),
    market_timezone text not null check (market_timezone in ('America/New_York', 'UTC')),
    signal_market_date text not null,
    horizon_bars integer not null check (horizon_bars in (5, 10, 20, 40)),
    horizon_start_date text not null,
    horizon_end_date text not null,
    reference_price real not null,
    entry_semantics text not null check (entry_semantics = 'reference_close_not_fill'),
    outcome_semantics text not null check (outcome_semantics = 'reference_price_observation_not_execution'),
    asset_bars_json text not null,
    asset_bars_hash text not null,
    forward_return_pct real not null,
    mfe_pct real not null,
    mae_pct real not null,
    stop real not null,
    target_2r real not null,
    target_3r real not null,
    stop_touched integer not null check (stop_touched in (0, 1)),
    first_stop_bar integer,
    first_stop_date text,
    target_2r_touched integer not null check (target_2r_touched in (0, 1)),
    first_target_2r_bar integer,
    first_target_2r_date text,
    target_3r_touched integer not null check (target_3r_touched in (0, 1)),
    first_target_3r_bar integer,
    first_target_3r_date text,
    same_bar_stop_target_2r integer not null check (same_bar_stop_target_2r in (0, 1)),
    same_bar_stop_target_3r integer not null check (same_bar_stop_target_3r in (0, 1)),
    alternative_entry real,
    alternative_entry_threshold_reached integer not null check (alternative_entry_threshold_reached in (0, 1)),
    first_alternative_entry_bar integer,
    first_alternative_entry_date text,
    provider text not null,
    price_basis text not null,
    outcome_hash text not null,
    persisted_at_utc text not null,
    foreign key(signal_id) references signal_observations(signal_id),
    unique(signal_id, observation_hash, evaluation_policy_version, horizon_bars)
)
"""

OUTCOME_UPDATE_TRIGGER_SQL = """
create trigger if not exists signal_forward_outcomes_no_update
before update on signal_forward_outcomes
begin
    select raise(abort, 'signal_forward_outcomes_append_only');
end
"""

OUTCOME_DELETE_TRIGGER_SQL = """
create trigger if not exists signal_forward_outcomes_no_delete
before delete on signal_forward_outcomes
begin
    select raise(abort, 'signal_forward_outcomes_append_only');
end
"""
