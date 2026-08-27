from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from pathlib import Path
from typing import Callable, Mapping, Sequence


SCHEMA_VERSION = "1.0"
STATISTICS_POLICY_VERSION = "1.0"
BENCHMARK_POLICY_VERSION = "1.0"
ANCHOR_POLICY_VERSION = "first_forward_open_to_horizon_close_v1"
BOOTSTRAP_REPLICATES = 10000

EVALUATION_ROLES = (
    "trade_candidate",
    "conditional_candidate",
    "observational_candidate",
    "observational_wait",
    "observational_avoid",
    "observational_blocked",
    "observational_other",
)
COMPARISON_PAIRS = (
    ("trade_candidate", "conditional_candidate"),
    ("trade_candidate", "observational_wait"),
    ("trade_candidate", "observational_avoid"),
)
SCORE_FIELDS = (
    "investment_quality_score",
    "swing_trade_score",
    "decision_confidence_score",
    "data_quality_score",
    "expected_value_r",
)
ASSET_TYPES = frozenset({"stock", "crypto"})
REPORT_TYPES = frozenset({"main", "close"})
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
        "benchmark_missing",
        "self_benchmark_unavailable",
        "incompatible_price_basis",
        "missing_required_dates",
        "invalid_benchmark_input",
        "not_applicable",
        "not_recorded",
        "not_allowlisted",
    }
)
DATASET_STATUSES = frozenset(
    {
        "NO_CANONICAL_SAMPLE",
        "CANONICAL_SAMPLE_NO_VALID_BENCHMARK",
        "CANONICAL_EVALUATION_ROWS_AVAILABLE",
    }
)
READINESS_ORDER = {"INSUFFICIENT": 0, "EXPLORATORY": 1, "EVALUATION_READY": 2}
HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class PredictiveStatisticsInputError(ValueError):
    """The 3B.3.1 artifact is not a valid canonical input."""


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    if not isinstance(value, bytes):
        value = _canonical_json_bytes(value)
    return hashlib.sha256(value).hexdigest()


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and HASH_PATTERN.fullmatch(value) is not None


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_date(value: object) -> bool:
    if not isinstance(value, str) or DATE_PATTERN.fullmatch(value) is None:
        return False
    try:
        from datetime import date

        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _expected_evaluation_row_id(row: Mapping[str, object]) -> str:
    identity = {
        "anchor_policy_version": row.get("anchor_policy_version"),
        "benchmark_policy_version": row.get("benchmark_policy_version"),
        "horizon_bars": row.get("horizon_bars"),
        "observation_hash": row.get("observation_hash"),
        "outcome_hash": row.get("outcome_hash"),
        "outcome_id": row.get("outcome_id"),
        "schema_version": row.get("schema_version"),
        "signal_id": row.get("signal_id"),
    }
    return _sha256(identity)


def _validate_row(row: object) -> dict[str, object]:
    if not isinstance(row, Mapping):
        raise PredictiveStatisticsInputError("invalid_evaluation_row")
    normalized = dict(row)
    if normalized.get("schema_version") != SCHEMA_VERSION:
        raise PredictiveStatisticsInputError("invalid_evaluation_row_schema")
    if normalized.get("benchmark_policy_version") != BENCHMARK_POLICY_VERSION:
        raise PredictiveStatisticsInputError("invalid_evaluation_row_policy")
    if normalized.get("anchor_policy_version") != ANCHOR_POLICY_VERSION:
        raise PredictiveStatisticsInputError("invalid_evaluation_row_anchor")
    for name in ("signal_id", "observation_hash", "outcome_id", "outcome_hash"):
        if not _is_hash(normalized.get(name)):
            raise PredictiveStatisticsInputError("invalid_evaluation_row_identity")
    evaluation_row_id = normalized.get("evaluation_row_id")
    if not _is_hash(evaluation_row_id) or evaluation_row_id != _expected_evaluation_row_id(normalized):
        raise PredictiveStatisticsInputError("invalid_evaluation_row_id")
    evaluation_row_hash = normalized.get("evaluation_row_hash")
    if not _is_hash(evaluation_row_hash):
        raise PredictiveStatisticsInputError("invalid_evaluation_row_hash")
    without_hash = {
        key: value for key, value in normalized.items() if key != "evaluation_row_hash"
    }
    try:
        calculated_hash = _sha256(without_hash)
    except (TypeError, ValueError, OverflowError):
        raise PredictiveStatisticsInputError("invalid_evaluation_row_json") from None
    if evaluation_row_hash != calculated_hash:
        raise PredictiveStatisticsInputError("invalid_evaluation_row_hash")
    if normalized.get("asset_type") not in ASSET_TYPES:
        raise PredictiveStatisticsInputError("invalid_evaluation_row_asset_type")
    if normalized.get("report_type") not in REPORT_TYPES:
        raise PredictiveStatisticsInputError("invalid_evaluation_row_report_type")
    horizon = normalized.get("horizon_bars")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
        raise PredictiveStatisticsInputError("invalid_evaluation_row_horizon")
    if normalized.get("evaluation_role") not in EVALUATION_ROLES:
        raise PredictiveStatisticsInputError("invalid_evaluation_row_role")
    if not _is_date(normalized.get("signal_market_date")):
        raise PredictiveStatisticsInputError("invalid_evaluation_row_signal_date")
    primary_status = normalized.get("primary_benchmark_status")
    secondary_status = normalized.get("secondary_benchmark_status")
    if primary_status not in PRIMARY_STATUSES:
        raise PredictiveStatisticsInputError("invalid_primary_benchmark_status")
    if secondary_status not in SECONDARY_STATUSES:
        raise PredictiveStatisticsInputError("invalid_secondary_benchmark_status")
    row_status = normalized.get("row_status")
    if not isinstance(row_status, str) or not row_status:
        raise PredictiveStatisticsInputError("invalid_row_status")
    for name in (
        "aligned_asset_return_pct",
        "primary_excess_aligned_price_return_pct",
        "mfe_pct",
        "mae_pct",
    ):
        value = normalized.get(name)
        if value is not None and not _is_finite_number(value):
            raise PredictiveStatisticsInputError("invalid_evaluation_row_number")
    for name in ("aligned_asset_start_date", "aligned_asset_end_date"):
        value = normalized.get(name)
        if value is not None and not _is_date(value):
            raise PredictiveStatisticsInputError("invalid_evaluation_row_date")
    return normalized


def _validate_input_artifact(value: object) -> tuple[dict[str, object], list[dict[str, object]]]:
    if not isinstance(value, Mapping):
        raise PredictiveStatisticsInputError("invalid_input_artifact")
    artifact = dict(value)
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise PredictiveStatisticsInputError("invalid_input_schema")
    if artifact.get("benchmark_policy_version") != BENCHMARK_POLICY_VERSION:
        raise PredictiveStatisticsInputError("invalid_input_benchmark_policy")
    if artifact.get("anchor_policy_version") != ANCHOR_POLICY_VERSION:
        raise PredictiveStatisticsInputError("invalid_input_anchor_policy")
    if artifact.get("dataset_status") not in DATASET_STATUSES:
        raise PredictiveStatisticsInputError("invalid_input_dataset_status")
    artifact_hash = artifact.get("artifact_hash")
    if not _is_hash(artifact_hash):
        raise PredictiveStatisticsInputError("invalid_input_artifact_hash")
    without_hash = {key: item for key, item in artifact.items() if key != "artifact_hash"}
    try:
        calculated_hash = _sha256(without_hash)
    except (TypeError, ValueError, OverflowError):
        raise PredictiveStatisticsInputError("invalid_input_artifact_json") from None
    if artifact_hash != calculated_hash:
        raise PredictiveStatisticsInputError("invalid_input_artifact_hash")
    raw_rows = artifact.get("rows")
    if not isinstance(raw_rows, list):
        raise PredictiveStatisticsInputError("invalid_input_rows")
    rows = [_validate_row(row) for row in raw_rows]
    seen_row_ids: set[str] = set()
    for row in rows:
        row_id = str(row["evaluation_row_id"])
        if row_id in seen_row_ids:
            raise PredictiveStatisticsInputError("duplicate_evaluation_row_id")
        seen_row_ids.add(row_id)
    return artifact, rows


def _canonicalize_semantic_rows(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return sorted(
        (dict(row) for row in rows),
        key=lambda row: str(row["evaluation_row_id"]),
    )


def _semantic_input_hash(
    artifact: Mapping[str, object],
    canonical_rows: Sequence[Mapping[str, object]],
) -> str:
    semantic_payload = {
        "schema_version": artifact["schema_version"],
        "benchmark_policy_version": artifact["benchmark_policy_version"],
        "anchor_policy_version": artifact["anchor_policy_version"],
        "dataset_status": artifact["dataset_status"],
        "coverage": artifact.get("coverage"),
        "rows": [dict(row) for row in canonical_rows],
    }
    return _sha256(semantic_payload)


def _read_input_artifact(input_path: Path | str) -> dict[str, object]:
    try:
        raw = Path(input_path).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError):
        raise PredictiveStatisticsInputError("invalid_input_artifact") from None
    artifact, _ = _validate_input_artifact(value)
    return artifact


def _valid_absolute_row(row: Mapping[str, object]) -> bool:
    if row.get("row_status") != "available":
        return False
    if not _is_finite_number(row.get("aligned_asset_return_pct")):
        return False
    start = row.get("aligned_asset_start_date")
    end = row.get("aligned_asset_end_date")
    return _is_date(start) and _is_date(end) and str(start) <= str(end)


def _valid_primary_row(row: Mapping[str, object]) -> bool:
    return (
        _valid_absolute_row(row)
        and row.get("primary_benchmark_status") == "available"
        and _is_finite_number(row.get("primary_excess_aligned_price_return_pct"))
    )


def _primary_eligible(row: Mapping[str, object]) -> bool:
    return _valid_absolute_row(row) and row.get("primary_benchmark_status") != "self_benchmark_unavailable"


def _unique_asset_dates(rows: Sequence[Mapping[str, object]]) -> set[tuple[object, object]]:
    return {
        (row.get("asset"), row.get("signal_market_date"))
        for row in rows
        if row.get("asset") is not None and row.get("signal_market_date") is not None
    }


def _date_units(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        if not _valid_absolute_row(row):
            continue
        day = row.get("signal_market_date")
        if not isinstance(day, str):
            continue
        grouped.setdefault(day, []).append(dict(row))
    units: list[dict[str, object]] = []
    for day, unit_rows in grouped.items():
        ordered_rows = sorted(unit_rows, key=lambda item: str(item.get("evaluation_row_id")))
        starts = [str(row["aligned_asset_start_date"]) for row in ordered_rows]
        ends = [str(row["aligned_asset_end_date"]) for row in ordered_rows]
        units.append(
            {
                "signal_market_date": day,
                "start_date": min(starts),
                "end_date": max(ends),
                "rows": ordered_rows,
            }
        )
    return units


def _independent_units(units: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    candidates = sorted(
        (dict(unit) for unit in units),
        key=lambda unit: (
            str(unit["end_date"]),
            str(unit["start_date"]),
            str(unit["signal_market_date"]),
        ),
    )
    selected: list[dict[str, object]] = []
    last_end: str | None = None
    for unit in candidates:
        if last_end is None or str(unit["start_date"]) > last_end:
            selected.append(unit)
            last_end = str(unit["end_date"])
    return selected


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _boolean_rate(rows: Sequence[Mapping[str, object]], field: str) -> float | None:
    values = [row.get(field) for row in rows if isinstance(row.get(field), bool)]
    return sum(1 for value in values if value) / len(values) if values else None


def _absolute_summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    valid_rows = [row for row in rows if _valid_absolute_row(row)]
    returns = [float(row["aligned_asset_return_pct"]) for row in valid_rows]
    mfe = [float(row["mfe_pct"]) for row in valid_rows if _is_finite_number(row.get("mfe_pct"))]
    mae = [float(row["mae_pct"]) for row in valid_rows if _is_finite_number(row.get("mae_pct"))]
    return {
        "count": len(valid_rows),
        "mean_aligned_asset_return_pct": _mean(returns),
        "median_aligned_asset_return_pct": _median(returns),
        "positive_return_rate": (
            sum(1 for value in returns if value > 0) / len(returns) if returns else None
        ),
        "median_mfe_pct": _median(mfe),
        "median_mae_pct": _median(mae),
        "stop_touch_rate": _boolean_rate(valid_rows, "stop_touched"),
        "target_2r_touch_rate": _boolean_rate(valid_rows, "target_2r_touched"),
        "target_3r_touch_rate": _boolean_rate(valid_rows, "target_3r_touched"),
    }


def _market_summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    valid_rows = [row for row in rows if _valid_primary_row(row)]
    excess = [float(row["primary_excess_aligned_price_return_pct"]) for row in valid_rows]
    return {
        "count": len(valid_rows),
        "mean_primary_excess_aligned_price_return_pct": _mean(excess),
        "median_primary_excess_aligned_price_return_pct": _median(excess),
        "benchmark_outperformance_rate": (
            sum(1 for value in excess if value > 0) / len(excess) if excess else None
        ),
    }


def _seed_for(input_hash: str, cell_key: str, metric_key: str) -> int:
    payload = {
        "statistics_policy_version": STATISTICS_POLICY_VERSION,
        "input_artifact_hash": input_hash,
        "cell_key": cell_key,
        "metric_key": metric_key,
    }
    return int(_sha256(payload), 16)


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = quantile * (len(ordered) - 1)
    index = math.floor(position) if quantile == 0.025 else math.ceil(position)
    return ordered[index]


def _bootstrap_single_metric(
    units: Sequence[Mapping[str, object]],
    statistic: Callable[[Sequence[Mapping[str, object]]], float | None],
    *,
    input_hash: str,
    cell_key: str,
    metric_key: str,
) -> tuple[float | None, float | None, int]:
    if len(units) < 4:
        return None, None, 0
    rng = random.Random(_seed_for(input_hash, cell_key, metric_key))
    values: list[float] = []
    for _ in range(BOOTSTRAP_REPLICATES):
        sampled_rows: list[Mapping[str, object]] = []
        for _ in range(len(units)):
            sampled_rows.extend(units[rng.randrange(len(units))]["rows"])
        result = statistic(sampled_rows)
        if result is not None and _is_finite_number(result):
            values.append(float(result))
    if not values:
        return None, None, 0
    return _percentile(values, 0.025), _percentile(values, 0.975), len(values)


def _spearman(values_x: Sequence[float], values_y: Sequence[float]) -> float | None:
    if len(values_x) != len(values_y) or not values_x:
        return None
    ranks_x = _average_ranks(values_x)
    ranks_y = _average_ranks(values_y)
    mean_x = sum(ranks_x) / len(ranks_x)
    mean_y = sum(ranks_y) / len(ranks_y)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(ranks_x, ranks_y))
    denominator_x = math.sqrt(sum((x - mean_x) ** 2 for x in ranks_x))
    denominator_y = math.sqrt(sum((y - mean_y) ** 2 for y in ranks_y))
    if denominator_x == 0 or denominator_y == 0:
        return None
    return numerator / (denominator_x * denominator_y)


def _average_ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + 1 + end) / 2
        for position in range(index, end):
            ranks[ordered[position][0]] = rank
        index = end
    return ranks


def _relation_status(values_x: Sequence[float], values_y: Sequence[float]) -> tuple[str, float | None]:
    if len(values_x) < 20:
        return "insufficient_sample", None
    if len(set(values_x)) < 3 or len(set(values_y)) < 3:
        return "insufficient_variation", None
    return "available", _spearman(values_x, values_y)


def _score_relationships(
    rows: Sequence[Mapping[str, object]],
    units: Sequence[Mapping[str, object]],
    *,
    input_hash: str,
    cell_key: str,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for score_name in SCORE_FIELDS:
        valid_score_rows = [row for row in rows if _is_finite_number(row.get(score_name))]
        aligned_pairs = [
            (float(row[score_name]), float(row["aligned_asset_return_pct"]))
            for row in valid_score_rows
            if _valid_absolute_row(row)
        ]
        primary_pairs = [
            (
                float(row[score_name]),
                float(row["primary_excess_aligned_price_return_pct"]),
            )
            for row in valid_score_rows
            if _valid_primary_row(row)
        ]
        aligned_x = [pair[0] for pair in aligned_pairs]
        aligned_y = [pair[1] for pair in aligned_pairs]
        primary_x = [pair[0] for pair in primary_pairs]
        primary_y = [pair[1] for pair in primary_pairs]
        aligned_status, aligned_rho = _relation_status(aligned_x, aligned_y)
        primary_status, primary_rho = _relation_status(primary_x, primary_y)
        relationship: dict[str, object] = {
            "valid_count": len(valid_score_rows),
            "missing_count": len(rows) - len(valid_score_rows),
            "aligned_pair_count": len(aligned_pairs),
            "primary_excess_pair_count": len(primary_pairs),
            "status_aligned_asset_return": aligned_status,
            "spearman_aligned_asset_return": aligned_rho,
            "status_primary_excess": primary_status,
            "spearman_primary_excess": primary_rho,
            "spearman_primary_excess_ci95_lower": None,
            "spearman_primary_excess_ci95_upper": None,
            "spearman_primary_excess_bootstrap_replicates_effective": 0,
        }
        if primary_status == "available":
            primary_units = [
                unit
                for unit in units
                if any(_valid_primary_row(row) and _is_finite_number(row.get(score_name)) for row in unit["rows"])
            ]

            def score_rho(sample_rows: Sequence[Mapping[str, object]]) -> float | None:
                pairs = [
                    (float(row[score_name]), float(row["primary_excess_aligned_price_return_pct"]))
                    for row in sample_rows
                    if _valid_primary_row(row) and _is_finite_number(row.get(score_name))
                ]
                status, rho = _relation_status(
                    [pair[0] for pair in pairs],
                    [pair[1] for pair in pairs],
                )
                return rho if status == "available" else None

            lower, upper, effective = _bootstrap_single_metric(
                primary_units,
                score_rho,
                input_hash=input_hash,
                cell_key=cell_key,
                metric_key=f"spearman_primary_excess:{score_name}",
            )
            relationship["spearman_primary_excess_ci95_lower"] = lower
            relationship["spearman_primary_excess_ci95_upper"] = upper
            relationship["spearman_primary_excess_bootstrap_replicates_effective"] = effective
        result[score_name] = relationship
    return result


def _comparison(
    rows: Sequence[Mapping[str, object]],
    independent_units: Sequence[Mapping[str, object]],
    lhs_role: str,
    rhs_role: str,
    *,
    input_hash: str,
    cell_key: str,
) -> dict[str, object]:
    lhs_rows = [row for row in rows if row.get("evaluation_role") == lhs_role and _valid_primary_row(row)]
    rhs_rows = [row for row in rows if row.get("evaluation_role") == rhs_role and _valid_primary_row(row)]
    lhs_values = [float(row["primary_excess_aligned_price_return_pct"]) for row in lhs_rows]
    rhs_values = [float(row["primary_excess_aligned_price_return_pct"]) for row in rhs_rows]
    lhs_dates = len(_unique_asset_dates(lhs_rows))
    rhs_dates = len(_unique_asset_dates(rhs_rows))
    lhs_units = [
        unit
        for unit in independent_units
        if any(row.get("evaluation_role") == lhs_role and _valid_primary_row(row) for row in unit["rows"])
    ]
    rhs_units = [
        unit
        for unit in independent_units
        if any(row.get("evaluation_role") == rhs_role and _valid_primary_row(row) for row in unit["rows"])
    ]
    independent_count = len(
        {
            str(unit["signal_market_date"])
            for unit in lhs_units + rhs_units
        }
    )
    if not lhs_values or not rhs_values:
        status = "missing_group"
    elif lhs_dates < 15 or rhs_dates < 15:
        status = "insufficient_group_sample"
    elif independent_count < 4:
        status = "insufficient_independent_time_units"
    else:
        status = "available"
    median_difference = (
        _median(lhs_values) - _median(rhs_values)
        if lhs_values and rhs_values
        else None
    )
    mean_difference = (
        _mean(lhs_values) - _mean(rhs_values)
        if lhs_values and rhs_values
        else None
    )
    comparison: dict[str, object] = {
        "comparison_key": f"{lhs_role}_vs_{rhs_role}",
        "lhs_role": lhs_role,
        "rhs_role": rhs_role,
        "status": status,
        "lhs_unique_asset_date_count": lhs_dates,
        "rhs_unique_asset_date_count": rhs_dates,
        "lhs_independent_time_unit_count": len(lhs_units),
        "rhs_independent_time_unit_count": len(rhs_units),
        "independent_time_unit_count": independent_count,
        "difference_in_median_primary_excess": median_difference,
        "difference_in_mean_primary_excess": mean_difference,
        "difference_in_median_primary_excess_ci95_lower": None,
        "difference_in_median_primary_excess_ci95_upper": None,
        "difference_in_mean_primary_excess_ci95_lower": None,
        "difference_in_mean_primary_excess_ci95_upper": None,
        "bootstrap_replicates_requested": BOOTSTRAP_REPLICATES,
        "bootstrap_replicates_effective": 0,
    }
    if status != "available":
        return comparison
    comparison_units = [
        unit
        for unit in independent_units
        if any(_valid_primary_row(row) and row.get("evaluation_role") in {lhs_role, rhs_role} for row in unit["rows"])
    ]

    def bootstrap_difference(sample_rows: Sequence[Mapping[str, object]], statistic: Callable[[Sequence[float]], float | None]) -> float | None:
        lhs = [
            float(row["primary_excess_aligned_price_return_pct"])
            for row in sample_rows
            if row.get("evaluation_role") == lhs_role and _valid_primary_row(row)
        ]
        rhs = [
            float(row["primary_excess_aligned_price_return_pct"])
            for row in sample_rows
            if row.get("evaluation_role") == rhs_role and _valid_primary_row(row)
        ]
        if not lhs or not rhs:
            return None
        left = statistic(lhs)
        right = statistic(rhs)
        return left - right if left is not None and right is not None else None

    lower_median, upper_median, effective_median = _bootstrap_single_metric(
        comparison_units,
        lambda values: bootstrap_difference(values, _median),
        input_hash=input_hash,
        cell_key=cell_key,
        metric_key=f"comparison:{lhs_role}_vs_{rhs_role}:difference_in_median_primary_excess",
    )
    lower_mean, upper_mean, effective_mean = _bootstrap_single_metric(
        comparison_units,
        lambda values: bootstrap_difference(values, _mean),
        input_hash=input_hash,
        cell_key=cell_key,
        metric_key=f"comparison:{lhs_role}_vs_{rhs_role}:difference_in_mean_primary_excess",
    )
    comparison["difference_in_median_primary_excess_ci95_lower"] = lower_median
    comparison["difference_in_median_primary_excess_ci95_upper"] = upper_median
    comparison["difference_in_mean_primary_excess_ci95_lower"] = lower_mean
    comparison["difference_in_mean_primary_excess_ci95_upper"] = upper_mean
    comparison["bootstrap_replicates_effective"] = min(effective_median, effective_mean)
    return comparison


def _regime_counts(rows: Sequence[Mapping[str, object]]) -> dict[str, int]:
    grouped: dict[str, set[tuple[object, object]]] = {}
    for row in rows:
        if not _valid_absolute_row(row):
            continue
        if row.get("asset_type") == "stock":
            regime = row.get("stock_regime")
        else:
            regime = row.get("crypto_regime")
        if not isinstance(regime, str) or not regime:
            continue
        grouped.setdefault(regime, set()).add((row.get("asset"), row.get("signal_market_date")))
    return {key: len(grouped[key]) for key in sorted(grouped)}


def _readiness(
    *,
    independent_count: int,
    absolute_unique_dates: int,
    eligible_count: int,
    eligible_unique_dates: int,
    available_count: int,
    available_unique_dates: int,
    coverage: float | None,
    regimes: Mapping[str, int],
    comparisons: Sequence[Mapping[str, object]],
) -> tuple[str, str]:
    if (
        independent_count >= 12
        and eligible_count > 0
        and eligible_unique_dates >= 200
        and available_unique_dates >= 200
        and coverage is not None
        and coverage >= 0.95
        and sum(1 for count in regimes.values() if count >= 30) >= 2
        and any(
            comparison.get("status") == "available"
            and int(comparison.get("lhs_unique_asset_date_count", 0)) >= 50
            and int(comparison.get("rhs_unique_asset_date_count", 0)) >= 50
            for comparison in comparisons
        )
    ):
        market = "EVALUATION_READY"
    elif (
        independent_count >= 4
        and eligible_count > 0
        and eligible_unique_dates >= 50
        and available_unique_dates >= 50
        and coverage is not None
        and coverage >= 0.90
        and sum(1 for count in regimes.values() if count >= 10) >= 2
    ):
        market = "EXPLORATORY"
    else:
        market = "INSUFFICIENT"
    if independent_count >= 4 and absolute_unique_dates >= 50 and sum(
        1 for count in regimes.values() if count >= 10
    ) >= 2:
        absolute = "EXPLORATORY"
    else:
        absolute = "INSUFFICIENT"
    return market, absolute


def _build_cell(
    key: tuple[str, str, int],
    rows: Sequence[Mapping[str, object]],
    *,
    input_hash: str,
) -> dict[str, object]:
    asset_type, report_type, horizon_bars = key
    ordered_rows = sorted((dict(row) for row in rows), key=lambda row: str(row["evaluation_row_id"]))
    absolute_rows = [row for row in ordered_rows if _valid_absolute_row(row)]
    primary_rows = [row for row in ordered_rows if _valid_primary_row(row)]
    units = _date_units(ordered_rows)
    independent_units = _independent_units(units)
    signal_dates = {str(unit["signal_market_date"]) for unit in units}
    primary_eligible_rows = [row for row in ordered_rows if _primary_eligible(row)]
    primary_available_rows = [row for row in ordered_rows if _valid_primary_row(row)]
    eligible_unique = _unique_asset_dates(primary_eligible_rows)
    available_unique = _unique_asset_dates(primary_available_rows)
    coverage = (
        len(primary_available_rows) / len(primary_eligible_rows)
        if primary_eligible_rows
        else None
    )
    self_benchmark_count = sum(
        1
        for row in ordered_rows
        if row.get("primary_benchmark_status") == "self_benchmark_unavailable"
    )
    cell_key = f"{asset_type}/{report_type}/{horizon_bars}"
    market = _market_summary(ordered_rows)
    market_units = [
        unit for unit in independent_units if any(_valid_primary_row(row) for row in unit["rows"])
    ]
    lower, upper, effective_mean = _bootstrap_single_metric(
        market_units,
        lambda sample_rows: _mean(
            [
                float(row["primary_excess_aligned_price_return_pct"])
                for row in sample_rows
                if _valid_primary_row(row)
            ]
        ),
        input_hash=input_hash,
        cell_key=cell_key,
        metric_key="mean_primary_excess_aligned_price_return_pct",
    )
    market["mean_primary_excess_aligned_price_return_pct_ci95_lower"] = lower
    market["mean_primary_excess_aligned_price_return_pct_ci95_upper"] = upper
    lower, upper, effective_median = _bootstrap_single_metric(
        market_units,
        lambda sample_rows: _median(
            [
                float(row["primary_excess_aligned_price_return_pct"])
                for row in sample_rows
                if _valid_primary_row(row)
            ]
        ),
        input_hash=input_hash,
        cell_key=cell_key,
        metric_key="median_primary_excess_aligned_price_return_pct",
    )
    market["median_primary_excess_aligned_price_return_pct_ci95_lower"] = lower
    market["median_primary_excess_aligned_price_return_pct_ci95_upper"] = upper
    lower, upper, effective_rate = _bootstrap_single_metric(
        market_units,
        lambda sample_rows: _mean(
            [
                1.0
                if float(row["primary_excess_aligned_price_return_pct"]) > 0
                else 0.0
                for row in sample_rows
                if _valid_primary_row(row)
            ]
        ),
        input_hash=input_hash,
        cell_key=cell_key,
        metric_key="benchmark_outperformance_rate",
    )
    market["benchmark_outperformance_rate_ci95_lower"] = lower
    market["benchmark_outperformance_rate_ci95_upper"] = upper
    market["bootstrap_replicates_requested"] = BOOTSTRAP_REPLICATES
    market["bootstrap_replicates_effective"] = (
        min(effective_mean, effective_median, effective_rate)
        if market_units and len(market_units) >= 4
        else 0
    )
    comparisons = [
        _comparison(
            ordered_rows,
            independent_units,
            lhs_role,
            rhs_role,
            input_hash=input_hash,
            cell_key=cell_key,
        )
        for lhs_role, rhs_role in COMPARISON_PAIRS
    ]
    regimes = _regime_counts(ordered_rows)
    market_readiness, absolute_readiness = _readiness(
        independent_count=len(independent_units),
        absolute_unique_dates=len(_unique_asset_dates(absolute_rows)),
        eligible_count=len(primary_eligible_rows),
        eligible_unique_dates=len(eligible_unique),
        available_count=len(primary_available_rows),
        available_unique_dates=len(available_unique),
        coverage=coverage,
        regimes=regimes,
        comparisons=comparisons,
    )
    role_summaries: dict[str, object] = {}
    for role in sorted({str(row["evaluation_role"]) for row in ordered_rows}):
        role_rows = [row for row in ordered_rows if row.get("evaluation_role") == role]
        role_summaries[role] = {
            "count": len([row for row in role_rows if _valid_absolute_row(row)]),
            "unique_asset_date_count": len(_unique_asset_dates(role_rows)),
            "absolute": _absolute_summary(role_rows),
            "market_relative": _market_summary(role_rows),
        }
    return {
        "asset_type": asset_type,
        "report_type": report_type,
        "horizon_bars": horizon_bars,
        "raw_signal_count": len({row.get("signal_id") for row in ordered_rows}),
        "unique_asset_date_count": len(_unique_asset_dates(ordered_rows)),
        "duplicate_analysis_unit_count": _duplicate_unit_count(ordered_rows),
        "signal_date_count": len(signal_dates),
        "independent_time_unit_count": len(independent_units),
        "overlapping_signal_date_count": len(signal_dates) - len(independent_units),
        "primary_eligible_count": len(primary_eligible_rows),
        "primary_available_count": len(primary_available_rows),
        "primary_eligible_unique_asset_date_count": len(eligible_unique),
        "primary_available_unique_asset_date_count": len(available_unique),
        "primary_coverage": coverage,
        "self_benchmark_unavailable_count": self_benchmark_count,
        "count": len(absolute_rows),
        "absolute": _absolute_summary(ordered_rows),
        "market_relative": market,
        "role_summaries": role_summaries,
        "comparisons": comparisons,
        "score_relationships": _score_relationships(
            ordered_rows,
            independent_units,
            input_hash=input_hash,
            cell_key=cell_key,
        ),
        "regime_unique_asset_date_counts": regimes,
        "market_relative_readiness": market_readiness,
        "absolute_sample_status": absolute_readiness,
    }


def _duplicate_unit_count(rows: Sequence[Mapping[str, object]]) -> int:
    counts: dict[tuple[object, object, object, object], int] = {}
    for row in rows:
        key = (
            row.get("asset"),
            row.get("signal_market_date"),
            row.get("report_type"),
            row.get("horizon_bars"),
        )
        counts[key] = counts.get(key, 0) + 1
    return sum(1 for count in counts.values() if count > 1)


def build_statistics_artifact(input_artifact: Mapping[str, object]) -> dict[str, object]:
    artifact, rows = _validate_input_artifact(input_artifact)
    canonical_rows = _canonicalize_semantic_rows(rows)
    input_hash = _semantic_input_hash(artifact, canonical_rows)
    groups: dict[tuple[str, str, int], list[dict[str, object]]] = {}
    for row in canonical_rows:
        key = (str(row["asset_type"]), str(row["report_type"]), int(row["horizon_bars"]))
        groups.setdefault(key, []).append(row)
    cells = [
        _build_cell(key, groups[key], input_hash=input_hash)
        for key in sorted(groups, key=lambda item: (item[0], item[1], item[2]))
    ]
    eligible_cells = [
        cell for cell in cells if int(cell["primary_eligible_count"]) > 0
    ]
    if not eligible_cells:
        overall = "INSUFFICIENT"
    else:
        overall = min(
            (str(cell["market_relative_readiness"]) for cell in eligible_cells),
            key=lambda status: READINESS_ORDER[status],
        )
    output_without_hash: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "statistics_policy_version": STATISTICS_POLICY_VERSION,
        "input_artifact_hash": input_hash,
        "input_dataset_status": artifact["dataset_status"],
        "statistics_status": overall,
        "overall_market_relative_readiness": overall,
        "calibration_authorized": False,
        "cells": cells,
    }
    output = dict(output_without_hash)
    output["artifact_hash"] = _sha256(output_without_hash)
    return output


def write_statistics_artifact(
    *,
    input_path: Path | str,
    output_path: Path | str,
) -> dict[str, object]:
    input_artifact = _read_input_artifact(input_path)
    output = build_statistics_artifact(input_artifact)
    Path(output_path).write_bytes(_canonical_json_bytes(output) + b"\n")
    return output


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m advisor.predictive_statistics")
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--output-path", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        write_statistics_artifact(input_path=args.input_path, output_path=args.output_path)
    except (OSError, TypeError, ValueError, UnicodeError, OverflowError):
        print('{"error_code":"invalid_predictive_evaluation_artifact"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
