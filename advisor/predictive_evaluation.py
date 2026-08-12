from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator, Mapping

from advisor.signal_observation import canonical_json_bytes, canonical_utc_timestamp


SCHEMA_VERSION = "1.0"
BENCHMARK_POLICY_VERSION = "1.0"
EVALUATION_POLICY_VERSION = "1.0"
ANCHOR_POLICY_VERSION = "first_forward_open_to_horizon_close_v1"

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
SECONDARY_ALLOWLIST = frozenset({"SMH", "IGV", "QQQ", "XLV"})
PRIMARY_STATUSES = frozenset(
    {
        "available",
        "benchmark_missing",
        "self_benchmark_unavailable",
        "incompatible_price_basis",
        "missing_required_dates",
        "invalid_benchmark_input",
    }
)
SECONDARY_STATUSES = frozenset(
    {
        "available",
        "not_applicable",
        "not_recorded",
        "not_allowlisted",
        "self_benchmark_unavailable",
        "benchmark_missing",
        "incompatible_price_basis",
        "missing_required_dates",
        "invalid_benchmark_input",
    }
)
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_SHA_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_ASSET_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")
_FORBIDDEN_TEXT_PATTERN = re.compile(
    r"(?:api[_-]?key|authorization|bearer\s|https?://|[A-Za-z]:\\|exception|headers?)",
    re.IGNORECASE,
)
_OBSERVATION_TEXT_FIELDS = (
    "run_id",
    "run_origin",
    "universe_origin",
    "market_session",
    "decision_label",
    "bucket",
    "sample_quality",
    "data_quality",
    "missing_data_severity",
    "data_source",
    "data_timestamp",
    "last_price_timestamp",
    "provider",
    "stock_regime",
    "crypto_regime",
)
_OUTCOME_NUMERIC_FIELDS = (
    "reference_price",
    "forward_return_pct",
    "mfe_pct",
    "mae_pct",
    "stop",
    "target_2r",
    "target_3r",
    "alternative_entry",
)
_OUTCOME_BOOLEAN_FIELDS = (
    "stop_touched",
    "target_2r_touched",
    "target_3r_touched",
    "same_bar_stop_target_2r",
    "same_bar_stop_target_3r",
    "alternative_entry_threshold_reached",
)
_TOUCH_FIELDS = (
    "first_stop_bar",
    "first_stop_date",
    "first_target_2r_bar",
    "first_target_2r_date",
    "first_target_3r_bar",
    "first_target_3r_date",
    "first_alternative_entry_bar",
    "first_alternative_entry_date",
)


class PredictiveEvaluationError(RuntimeError):
    """The local database cannot be read as a SQLite evaluation source."""


@dataclass(frozen=True)
class BenchmarkCandle:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "BenchmarkCandle":
        required = {"date", "open", "high", "low", "close", "volume"}
        if not required.issubset(value):
            raise ValueError("invalid_benchmark_candle")
        day = _canonical_date(value["date"])
        open_price = _finite_number(value["open"])
        high = _finite_number(value["high"])
        low = _finite_number(value["low"])
        close = _finite_number(value["close"])
        volume = _finite_number(value["volume"])
        if min(open_price, high, low, close) <= 0 or volume < 0:
            raise ValueError("invalid_benchmark_candle")
        if not (low <= open_price <= high and low <= close <= high and low <= high):
            raise ValueError("invalid_benchmark_candle")
        return cls(day, open_price, high, low, close, volume)

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
class BenchmarkSeries:
    symbol: str
    asset_type: str
    price_basis: str
    candles: tuple[BenchmarkCandle, ...]

    @classmethod
    def from_mapping(cls, symbol: str, value: Mapping[str, object]) -> "BenchmarkSeries":
        if not isinstance(symbol, str) or _ASSET_PATTERN.fullmatch(symbol) is None:
            raise ValueError("invalid_benchmark_symbol")
        if not {"asset_type", "provider", "price_basis", "candles"}.issubset(value):
            raise ValueError("invalid_benchmark_series")
        asset_type = value.get("asset_type")
        if asset_type not in ASSET_TYPES:
            raise ValueError("invalid_benchmark_asset_type")
        if not isinstance(value.get("provider"), str) or not value["provider"].strip():
            raise ValueError("invalid_benchmark_provider")
        if not isinstance(value.get("price_basis"), str) or not value["price_basis"].strip():
            raise ValueError("invalid_benchmark_price_basis")
        raw_candles = value.get("candles")
        if not isinstance(raw_candles, list):
            raise ValueError("invalid_benchmark_candles")
        candles = tuple(
            BenchmarkCandle.from_mapping(candle)
            if isinstance(candle, Mapping)
            else _invalid_benchmark_value()
            for candle in raw_candles
        )
        ordered = tuple(sorted(candles, key=lambda candle: candle.date))
        if len({candle.date for candle in ordered}) != len(ordered):
            raise ValueError("duplicate_benchmark_date")
        return cls(
            symbol=symbol,
            asset_type=asset_type,
            price_basis=_bounded_text(value.get("price_basis"), default="unknown"),
            candles=ordered,
        )

    @property
    def by_date(self) -> dict[str, BenchmarkCandle]:
        return {candle.date: candle for candle in self.candles}


@dataclass(frozen=True)
class BenchmarkCatalog:
    valid: bool
    benchmarks: dict[str, BenchmarkSeries | None]


@dataclass(frozen=True)
class AssetEvidence:
    bars_json: str
    bars_hash: str
    start_date: str
    end_date: str
    start_open: float
    end_close: float
    aligned_return_pct: float


@dataclass(frozen=True)
class BenchmarkEvidence:
    status: str
    bars_json: str | None = None
    bars_hash: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    start_open: float | None = None
    end_close: float | None = None
    aligned_return_pct: float | None = None
    price_basis: str | None = None


def build_evaluation_artifact(
    *,
    db_path: Path | str,
    benchmark_input_path: Path | str,
) -> dict[str, object]:
    catalog = _load_benchmark_catalog(benchmark_input_path)
    with _open_database(db_path) as connection:
        tables = _table_names(connection)
        observation_rows = _load_table(connection, "signal_observations") if "signal_observations" in tables else []
        outcome_rows = _load_table(connection, "signal_forward_outcomes") if "signal_forward_outcomes" in tables else []

    observations_by_id = {
        row.get("signal_id"): row
        for row in observation_rows
        if isinstance(row.get("signal_id"), str)
    }
    rows: list[dict[str, object]] = []
    for outcome in outcome_rows:
        observation = observations_by_id.get(outcome.get("signal_id"))
        if not _canonical_binding_is_valid(observation, outcome):
            continue
        rows.append(_build_evaluation_row(observation, outcome, catalog))

    rows.sort(key=lambda row: str(row["evaluation_row_id"]))
    coverage = _build_coverage(
        observations_total=len(observation_rows),
        outcomes_total=len(outcome_rows),
        rows=rows,
    )
    if not rows:
        dataset_status = "NO_CANONICAL_SAMPLE"
    elif coverage["primary_benchmark_available"] == 0:
        dataset_status = "CANONICAL_SAMPLE_NO_VALID_BENCHMARK"
    else:
        dataset_status = "CANONICAL_EVALUATION_ROWS_AVAILABLE"

    artifact_without_hash: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_policy_version": BENCHMARK_POLICY_VERSION,
        "anchor_policy_version": ANCHOR_POLICY_VERSION,
        "dataset_status": dataset_status,
        "coverage": coverage,
        "rows": rows,
    }
    artifact = dict(artifact_without_hash)
    artifact["artifact_hash"] = _sha256(canonical_json_bytes(artifact_without_hash))
    return artifact


def write_evaluation_artifact(
    *,
    db_path: Path | str,
    benchmark_input_path: Path | str,
    output_path: Path | str,
) -> dict[str, object]:
    artifact = build_evaluation_artifact(
        db_path=db_path,
        benchmark_input_path=benchmark_input_path,
    )
    output = canonical_json_bytes(artifact) + b"\n"
    Path(output_path).write_bytes(output)
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        artifact = write_evaluation_artifact(
            db_path=args.db,
            benchmark_input_path=args.benchmark_input_path,
            output_path=args.output_path,
        )
    except PredictiveEvaluationError:
        print('{"error_code":"invalid_db"}')
        return 1
    except (OSError, TypeError, ValueError):
        print('{"error_code":"artifact_unavailable"}')
        return 1
    print(
        canonical_json_bytes(
            {
                "artifact_hash": artifact["artifact_hash"],
                "rows_total": artifact["coverage"]["rows_total"],
            }
        ).decode("utf-8")
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the deterministic benchmark-aligned evaluation dataset")
    parser.add_argument("--db", required=True)
    parser.add_argument("--benchmark-input-path", required=True)
    parser.add_argument("--output-path", required=True)
    return parser


def _build_evaluation_row(
    observation: Mapping[str, object],
    outcome: Mapping[str, object],
    catalog: BenchmarkCatalog,
) -> dict[str, object]:
    primary_symbol = _primary_benchmark_symbol(observation)
    secondary_symbol, secondary_selection_status = _secondary_benchmark_selection(observation)

    asset_evidence = _validate_asset_evidence(outcome)
    row = _observation_output(observation)
    row.update(_outcome_output(outcome))
    row.update(
        {
            "schema_version": SCHEMA_VERSION,
            "benchmark_policy_version": BENCHMARK_POLICY_VERSION,
            "anchor_policy_version": ANCHOR_POLICY_VERSION,
            "row_status": "available" if asset_evidence is not None else "invalid_asset_outcome",
            "cash_zero_reference_return_pct": 0.0,
            "absolute_positive_return": bool(
                asset_evidence is not None and asset_evidence.aligned_return_pct > 0
            ),
            "aligned_asset_start_date": asset_evidence.start_date if asset_evidence else None,
            "aligned_asset_end_date": asset_evidence.end_date if asset_evidence else None,
            "aligned_asset_start_open": asset_evidence.start_open if asset_evidence else None,
            "aligned_asset_end_close": asset_evidence.end_close if asset_evidence else None,
            "aligned_asset_return_pct": asset_evidence.aligned_return_pct if asset_evidence else None,
        }
    )
    if asset_evidence is None:
        row["asset_bars_json"] = None
        row["asset_bars_hash"] = None
    else:
        row["asset_bars_json"] = asset_evidence.bars_json
        row["asset_bars_hash"] = asset_evidence.bars_hash

    primary_evidence = _benchmark_evidence(
        symbol=primary_symbol,
        selection_status=None,
        asset_type=_text_value(observation.get("asset_type")),
        asset_price_basis=_text_value(outcome.get("price_basis")),
        required_dates=(
            [asset_evidence.start_date, asset_evidence.end_date]
            if asset_evidence is not None and asset_evidence.start_date == asset_evidence.end_date
            else _asset_required_dates(outcome)
        ),
        catalog=catalog,
        asset_valid=asset_evidence is not None,
    )
    secondary_evidence = _benchmark_evidence(
        symbol=secondary_symbol,
        selection_status=secondary_selection_status,
        asset_type=_text_value(observation.get("asset_type")),
        asset_price_basis=_text_value(outcome.get("price_basis")),
        required_dates=_asset_required_dates(outcome),
        catalog=catalog,
        asset_valid=asset_evidence is not None,
    )
    row.update(_benchmark_output("primary", primary_symbol, primary_evidence, asset_evidence))
    row.update(_benchmark_output("secondary", secondary_symbol, secondary_evidence, asset_evidence))

    row["evaluation_row_id"] = _evaluation_row_id(row, outcome)
    row["evaluation_row_hash"] = _sha256(canonical_json_bytes(row))
    return row


def _observation_output(observation: Mapping[str, object]) -> dict[str, object]:
    output: dict[str, object] = {}
    fields = (
        "signal_id",
        "source_sha",
        "run_id",
        "run_origin",
        "report_date_brt",
        "report_type",
        "signal_timestamp_utc",
        "asset",
        "asset_type",
        "universe_origin",
        "market_session",
        "market_timezone",
        "decision_label",
        "bucket",
        "investment_quality_score",
        "swing_trade_score",
        "decision_confidence_score",
        "data_quality_score",
        "expected_value_r",
        "backtest_sample_size",
        "sample_quality",
        "data_quality",
        "missing_data_severity",
        "ideal_entry",
        "alternative_entry",
        "entry_semantics",
        "alternative_entry_semantics",
        "stop",
        "target_2r",
        "target_3r",
        "per_unit_risk",
        "risk_amount",
        "risk_fraction",
        "max_position_units",
        "max_position_value",
        "data_source",
        "data_timestamp",
        "last_price_timestamp",
        "provider",
        "is_stale",
        "stock_regime",
        "crypto_regime",
        "relative_strength_vs_spy",
        "relative_strength_vs_qqq",
        "relative_strength_vs_sector",
        "sector_benchmark",
        "evaluation_role",
    )
    for name in fields:
        value = observation.get(name)
        if name in _OBSERVATION_TEXT_FIELDS or name == "sector_benchmark":
            value = _bounded_text(value, default=None)
        elif name in {"is_stale"}:
            value = bool(value)
        elif name in {
            "investment_quality_score",
            "swing_trade_score",
            "expected_value_r",
            "ideal_entry",
            "alternative_entry",
            "stop",
            "target_2r",
            "target_3r",
            "per_unit_risk",
            "risk_amount",
            "risk_fraction",
            "max_position_units",
            "max_position_value",
            "relative_strength_vs_spy",
            "relative_strength_vs_qqq",
            "relative_strength_vs_sector",
        }:
            value = _finite_or_none(value)
        elif name in {"decision_confidence_score", "data_quality_score", "backtest_sample_size"}:
            value = _integer_or_none(value)
        output[name] = value

    raw_reason_codes = observation.get("reason_codes")
    if isinstance(raw_reason_codes, str):
        try:
            raw_reason_codes = json.loads(raw_reason_codes)
        except (TypeError, ValueError):
            raw_reason_codes = []
    output["reason_codes"] = [
        _bounded_text(value, default="unknown")
        for value in raw_reason_codes
        if isinstance(raw_reason_codes, list)
    ]
    output["provenance_json"] = _safe_provenance_json(observation.get("provenance_json"))
    return output


def _outcome_output(outcome: Mapping[str, object]) -> dict[str, object]:
    fields = (
        "outcome_id",
        "outcome_hash",
        "evaluation_policy_version",
        "observation_hash",
        "asset_bars_json",
        "asset_bars_hash",
        "forward_return_pct",
        "mfe_pct",
        "mae_pct",
        "reference_price",
        "entry_semantics",
        "outcome_semantics",
        "stop",
        "target_2r",
        "target_3r",
        "stop_touched",
        "first_stop_bar",
        "first_stop_date",
        "target_2r_touched",
        "first_target_2r_bar",
        "first_target_2r_date",
        "target_3r_touched",
        "first_target_3r_bar",
        "first_target_3r_date",
        "same_bar_stop_target_2r",
        "same_bar_stop_target_3r",
        "alternative_entry",
        "alternative_entry_threshold_reached",
        "first_alternative_entry_bar",
        "first_alternative_entry_date",
        "price_basis",
        "horizon_bars",
        "signal_market_date",
        "horizon_start_date",
        "horizon_end_date",
    )
    output: dict[str, object] = {}
    for name in fields:
        value = outcome.get(name)
        if name in _OUTCOME_NUMERIC_FIELDS:
            value = _finite_or_none(value)
        elif name in _OUTCOME_BOOLEAN_FIELDS:
            value = bool(value)
        elif name in _TOUCH_FIELDS:
            if name.endswith("_bar"):
                value = _integer_or_none(value)
            else:
                value = _bounded_text(value, default=None)
        elif name == "price_basis":
            value = _bounded_text(value, default="unknown")
        elif name == "asset_bars_json":
            value = value if isinstance(value, str) and len(value) <= 2_000_000 else None
        elif name == "horizon_bars":
            value = _integer_or_none(value)
        elif name in {"signal_market_date", "horizon_start_date", "horizon_end_date"}:
            value = _bounded_text(value, default=None)
        output[name] = value
    return output


def _benchmark_output(
    prefix: str,
    symbol: str | None,
    evidence: BenchmarkEvidence,
    asset_evidence: AssetEvidence | None,
) -> dict[str, object]:
    result: dict[str, object] = {
        f"{prefix}_benchmark": symbol,
        f"{prefix}_benchmark_status": evidence.status,
        f"{prefix}_benchmark_price_basis": evidence.price_basis,
        f"{prefix}_benchmark_bars_json": evidence.bars_json,
        f"{prefix}_benchmark_bars_hash": evidence.bars_hash,
        f"{prefix}_benchmark_start_date": evidence.start_date,
        f"{prefix}_benchmark_end_date": evidence.end_date,
        f"{prefix}_benchmark_start_open": evidence.start_open,
        f"{prefix}_benchmark_end_close": evidence.end_close,
        f"{prefix}_benchmark_aligned_return_pct": evidence.aligned_return_pct,
        f"{prefix}_excess_aligned_price_return_pct": (
            asset_evidence.aligned_return_pct - evidence.aligned_return_pct
            if asset_evidence is not None
            and evidence.status == "available"
            and evidence.aligned_return_pct is not None
            else None
        ),
    }
    return result


def _benchmark_evidence(
    *,
    symbol: str | None,
    selection_status: str | None,
    asset_type: str,
    asset_price_basis: str,
    required_dates: list[str],
    catalog: BenchmarkCatalog,
    asset_valid: bool,
) -> BenchmarkEvidence:
    if selection_status is not None:
        if selection_status not in SECONDARY_STATUSES:
            raise ValueError("invalid_secondary_status")
        if selection_status != "available":
            return BenchmarkEvidence(selection_status)
    if symbol is None:
        return BenchmarkEvidence("self_benchmark_unavailable")
    if not asset_valid:
        return BenchmarkEvidence("invalid_benchmark_input")
    if not catalog.valid:
        return BenchmarkEvidence("invalid_benchmark_input")
    if symbol in catalog.benchmarks and catalog.benchmarks[symbol] is None:
        return BenchmarkEvidence("invalid_benchmark_input")
    series = catalog.benchmarks.get(symbol)
    if series is None:
        return BenchmarkEvidence("benchmark_missing")
    expected_basis = "split_adjusted_ohlc" if asset_type == "stock" else "raw_ohlcv"
    if (
        asset_type not in ASSET_TYPES
        or series.asset_type != asset_type
        or asset_price_basis != expected_basis
        or series.price_basis != expected_basis
        or asset_price_basis != series.price_basis
    ):
        return BenchmarkEvidence("incompatible_price_basis", price_basis=series.price_basis)
    series_by_date = series.by_date
    if not required_dates or any(day not in series_by_date for day in required_dates):
        return BenchmarkEvidence("missing_required_dates", price_basis=series.price_basis)
    selected = [series_by_date[day] for day in required_dates]
    bars_json = canonical_json_bytes([candle.to_dict() for candle in selected]).decode("utf-8")
    start = selected[0]
    end = selected[-1]
    return BenchmarkEvidence(
        status="available",
        bars_json=bars_json,
        bars_hash=_sha256(bars_json.encode("utf-8")),
        start_date=start.date,
        end_date=end.date,
        start_open=start.open,
        end_close=end.close,
        aligned_return_pct=(end.close / start.open) - 1,
        price_basis=series.price_basis,
    )


def _validate_asset_evidence(outcome: Mapping[str, object]) -> AssetEvidence | None:
    raw_json = outcome.get("asset_bars_json")
    if not isinstance(raw_json, str):
        return None
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, list):
        return None
    try:
        bars = [BenchmarkCandle.from_mapping(item) if isinstance(item, Mapping) else _invalid_benchmark_value() for item in payload]
    except ValueError:
        return None
    horizon_bars = outcome.get("horizon_bars")
    if isinstance(horizon_bars, bool) or not isinstance(horizon_bars, int) or len(bars) != horizon_bars:
        return None
    if not bars or [bar.date for bar in bars] != sorted(bar.date for bar in bars):
        return None
    if len({bar.date for bar in bars}) != len(bars):
        return None
    if bars[0].date != outcome.get("horizon_start_date") or bars[-1].date != outcome.get("horizon_end_date"):
        return None
    canonical = canonical_json_bytes([bar.to_dict() for bar in bars]).decode("utf-8")
    stored_hash = outcome.get("asset_bars_hash")
    if not isinstance(stored_hash, str) or not _HASH_PATTERN.fullmatch(stored_hash):
        return None
    if canonical != raw_json or _sha256(canonical.encode("utf-8")) != stored_hash:
        return None
    return AssetEvidence(
        bars_json=canonical,
        bars_hash=stored_hash,
        start_date=bars[0].date,
        end_date=bars[-1].date,
        start_open=bars[0].open,
        end_close=bars[-1].close,
        aligned_return_pct=(bars[-1].close / bars[0].open) - 1,
    )


def _asset_required_dates(outcome: Mapping[str, object]) -> list[str]:
    raw_json = outcome.get("asset_bars_json")
    if not isinstance(raw_json, str):
        return []
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    dates: list[str] = []
    for item in payload:
        if isinstance(item, Mapping) and isinstance(item.get("date"), str):
            dates.append(item["date"])
    return dates


def _primary_benchmark_symbol(observation: Mapping[str, object]) -> str | None:
    asset = observation.get("asset")
    asset_type = observation.get("asset_type")
    if asset_type == "stock":
        return None if asset == "SPY" else "SPY"
    if asset_type == "crypto":
        return None if asset == "BTC" else "BTC"
    return None


def _secondary_benchmark_selection(observation: Mapping[str, object]) -> tuple[str | None, str]:
    if observation.get("asset_type") != "stock":
        return None, "not_applicable"
    value = observation.get("sector_benchmark")
    if value is None or value == "":
        return None, "not_recorded"
    if value in SECONDARY_ALLOWLIST:
        if value == observation.get("asset"):
            return None, "self_benchmark_unavailable"
        return value, "available"
    return None, "not_allowlisted"


def _evaluation_row_id(row: Mapping[str, object], outcome: Mapping[str, object]) -> str:
    identity = {
        "anchor_policy_version": ANCHOR_POLICY_VERSION,
        "benchmark_policy_version": BENCHMARK_POLICY_VERSION,
        "horizon_bars": outcome.get("horizon_bars"),
        "observation_hash": row.get("observation_hash"),
        "outcome_hash": outcome.get("outcome_hash"),
        "outcome_id": outcome.get("outcome_id"),
        "schema_version": SCHEMA_VERSION,
        "signal_id": row.get("signal_id"),
    }
    return _sha256(canonical_json_bytes(identity))


def _build_coverage(
    *,
    observations_total: int,
    outcomes_total: int,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    by_horizon: dict[str, int] = {}
    by_asset_type: dict[str, int] = {}
    by_report_type: dict[str, int] = {}
    by_evaluation_role: dict[str, int] = {}
    primary_available = 0
    secondary_available = 0
    for row in rows:
        horizon = str(row.get("horizon_bars"))
        by_horizon[horizon] = by_horizon.get(horizon, 0) + 1
        asset_type = str(row.get("asset_type"))
        by_asset_type[asset_type] = by_asset_type.get(asset_type, 0) + 1
        report_type = str(row.get("report_type"))
        by_report_type[report_type] = by_report_type.get(report_type, 0) + 1
        role = str(row.get("evaluation_role"))
        by_evaluation_role[role] = by_evaluation_role.get(role, 0) + 1
        if row.get("primary_benchmark_status") == "available":
            primary_available += 1
        if row.get("secondary_benchmark_status") == "available":
            secondary_available += 1
    return {
        "observations_total": observations_total,
        "outcomes_total": outcomes_total,
        "rows_total": len(rows),
        "primary_benchmark_available": primary_available,
        "primary_benchmark_unavailable": len(rows) - primary_available,
        "secondary_benchmark_available": secondary_available,
        "secondary_benchmark_unavailable": len(rows) - secondary_available,
        "by_horizon": dict(sorted(by_horizon.items())),
        "by_asset_type": dict(sorted(by_asset_type.items())),
        "by_report_type": dict(sorted(by_report_type.items())),
        "by_evaluation_role": dict(sorted(by_evaluation_role.items())),
    }


def _canonical_binding_is_valid(
    observation: Mapping[str, object] | None,
    outcome: Mapping[str, object],
) -> bool:
    if observation is None:
        return False
    signal_id = observation.get("signal_id")
    observation_hash = observation.get("observation_hash")
    if not isinstance(signal_id, str) or not _HASH_PATTERN.fullmatch(signal_id):
        return False
    if not isinstance(observation_hash, str) or not _HASH_PATTERN.fullmatch(observation_hash):
        return False
    if observation.get("schema_version") != SCHEMA_VERSION:
        return False
    if not isinstance(observation.get("source_sha"), str) or not _SOURCE_SHA_PATTERN.fullmatch(observation["source_sha"]):
        return False
    if not isinstance(observation.get("run_id"), str) or not _RUN_ID_PATTERN.fullmatch(observation["run_id"]):
        return False
    if observation.get("run_origin") not in {"github", "local"}:
        return False
    if observation.get("report_type") not in {"main", "close"}:
        return False
    report_date = observation.get("report_date_brt")
    if not isinstance(report_date, str) or _DATE_PATTERN.fullmatch(report_date) is None:
        return False
    timestamp = observation.get("signal_timestamp_utc")
    if not isinstance(timestamp, str):
        return False
    try:
        if canonical_utc_timestamp(timestamp) != timestamp:
            return False
    except ValueError:
        return False
    if not isinstance(observation.get("asset"), str) or _ASSET_PATTERN.fullmatch(observation["asset"]) is None:
        return False
    asset_type = observation.get("asset_type")
    if asset_type not in ASSET_TYPES:
        return False
    if observation.get("market_timezone") != (
        "America/New_York" if asset_type == "stock" else "UTC"
    ):
        return False
    if observation.get("evaluation_role") not in EVALUATION_ROLES:
        return False
    if observation.get("entry_semantics") != "reference_close_not_fill":
        return False
    if observation.get("alternative_entry_semantics") not in {"conditional_untracked", "not_present"}:
        return False
    if outcome.get("signal_id") != signal_id or outcome.get("observation_hash") != observation_hash:
        return False
    if not isinstance(outcome.get("outcome_id"), str) or not _HASH_PATTERN.fullmatch(outcome["outcome_id"]):
        return False
    if not isinstance(outcome.get("outcome_hash"), str) or not _HASH_PATTERN.fullmatch(outcome["outcome_hash"]):
        return False
    if outcome.get("asset") != observation.get("asset") or outcome.get("asset_type") != observation.get("asset_type"):
        return False
    if outcome.get("schema_version") != SCHEMA_VERSION:
        return False
    if outcome.get("evaluation_policy_version") != EVALUATION_POLICY_VERSION:
        return False
    if outcome.get("horizon_bars") not in {5, 10, 20, 40}:
        return False
    if outcome.get("evaluation_role") != observation.get("evaluation_role"):
        return False
    if outcome.get("decision_label") != observation.get("decision_label"):
        return False
    if outcome.get("market_timezone") != observation.get("market_timezone"):
        return False
    if outcome.get("entry_semantics") != "reference_close_not_fill":
        return False
    if outcome.get("outcome_semantics") != "reference_price_observation_not_execution":
        return False
    return True


@contextmanager
def _open_database(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    try:
        resolved = Path(db_path).resolve()
        uri = f"file:{resolved.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            connection.close()
            raise PredictiveEvaluationError
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise PredictiveEvaluationError from None
    try:
        connection.row_factory = sqlite3.Row
        yield connection
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise PredictiveEvaluationError from None
    finally:
        connection.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "select name from sqlite_master where type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _load_table(connection: sqlite3.Connection, table_name: str) -> list[dict[str, object]]:
    rows = connection.execute(f"select * from {table_name}").fetchall()
    return [dict(row) for row in rows]


def _load_benchmark_catalog(path: Path | str) -> BenchmarkCatalog:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError):
        return BenchmarkCatalog(False, {})
    if not isinstance(payload, Mapping) or payload.get("schema_version") != SCHEMA_VERSION:
        return BenchmarkCatalog(False, {})
    raw_benchmarks = payload.get("benchmarks")
    if not isinstance(raw_benchmarks, Mapping):
        return BenchmarkCatalog(False, {})
    benchmarks: dict[str, BenchmarkSeries | None] = {}
    for symbol, raw_series in raw_benchmarks.items():
        if not isinstance(symbol, str) or not isinstance(raw_series, Mapping):
            if isinstance(symbol, str):
                benchmarks[symbol] = None
            continue
        try:
            series = BenchmarkSeries.from_mapping(symbol, raw_series)
        except (TypeError, ValueError):
            benchmarks[symbol] = None
            continue
        benchmarks[series.symbol] = series
    return BenchmarkCatalog(True, benchmarks)


def _safe_provenance_json(value: object) -> str:
    if not isinstance(value, str):
        return "{}"
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return "{}"
    sanitized = _sanitize_json_value(parsed)
    return canonical_json_bytes(sanitized).decode("utf-8")


def _sanitize_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _FORBIDDEN_TEXT_PATTERN.search(key):
                continue
            result[key] = _sanitize_json_value(item)
        return result
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, str):
        return _bounded_text(value, default="unknown")
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return value
    return None


def _bounded_text(value: object, *, default: str | None = "unknown") -> str | None:
    if not isinstance(value, str):
        return default
    normalized = value.strip()
    if not normalized or len(normalized) > 256 or _FORBIDDEN_TEXT_PATTERN.search(normalized):
        return default
    return normalized


def _canonical_date(value: object) -> str:
    if not isinstance(value, str) or _DATE_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid_date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("invalid_date") from None


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid_number")
    return number


def _finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return _finite_number(value)
    except ValueError:
        return None


def _integer_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text_value(value: object) -> str:
    return value if isinstance(value, str) else "unknown"


def _invalid_benchmark_value() -> BenchmarkCandle:
    raise ValueError("invalid_benchmark_candle")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
