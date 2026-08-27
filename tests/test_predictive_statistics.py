from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import math
import random
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


SCHEMA_VERSION = "1.0"
BENCHMARK_POLICY_VERSION = "1.0"
ANCHOR_POLICY_VERSION = "first_forward_open_to_horizon_close_v1"
STATISTICS_POLICY_VERSION = "1.0"
ROLES = (
    "trade_candidate",
    "conditional_candidate",
    "observational_candidate",
    "observational_wait",
    "observational_avoid",
    "observational_blocked",
    "observational_other",
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(
        value if isinstance(value, bytes) else _canonical(value)
    ).hexdigest()


def _load_statistics_module():
    try:
        return importlib.import_module("advisor.predictive_statistics")
    except ModuleNotFoundError as exc:
        raise AssertionError("predictive_statistics module is missing") from exc


def _evaluation_row_id(row: dict[str, object]) -> str:
    identity = {
        "anchor_policy_version": row["anchor_policy_version"],
        "benchmark_policy_version": row["benchmark_policy_version"],
        "horizon_bars": row["horizon_bars"],
        "observation_hash": row["observation_hash"],
        "outcome_hash": row["outcome_hash"],
        "outcome_id": row["outcome_id"],
        "schema_version": row["schema_version"],
        "signal_id": row["signal_id"],
    }
    return _sha(identity)


def _row(
    key: str,
    *,
    asset: str | None = None,
    asset_type: str = "stock",
    report_type: str = "main",
    horizon_bars: int = 20,
    signal_market_date: str = "2026-01-01",
    horizon_end_date: str | None = None,
    role: str = "trade_candidate",
    aligned_return: float = 0.10,
    primary_status: str = "available",
    primary_excess: float | None = 0.05,
    secondary_status: str = "not_recorded",
    stock_regime: str | None = "stock_regime",
    crypto_regime: str | None = None,
    investment_quality_score: float | None = 1.0,
    swing_trade_score: float | None = 2.0,
    decision_confidence_score: float | None = 3.0,
    data_quality_score: float | None = 4.0,
    expected_value_r: float | None = 0.5,
    mfe_pct: float | None = 0.20,
    mae_pct: float | None = -0.05,
    stop_touched: bool = False,
    target_2r_touched: bool = True,
    target_3r_touched: bool = False,
    row_status: str = "available",
) -> dict[str, object]:
    asset = asset or f"ASSET_{key}"
    end_date = horizon_end_date or (
        date.fromisoformat(signal_market_date) + timedelta(days=horizon_bars - 1)
    ).isoformat()
    signal_id = _sha(f"signal:{key}")
    observation_hash = _sha(f"observation:{key}")
    outcome_id = _sha(f"outcome-id:{key}")
    outcome_hash = _sha(f"outcome-hash:{key}")
    row: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_policy_version": BENCHMARK_POLICY_VERSION,
        "anchor_policy_version": ANCHOR_POLICY_VERSION,
        "signal_id": signal_id,
        "observation_hash": observation_hash,
        "outcome_id": outcome_id,
        "outcome_hash": outcome_hash,
        "asset": asset,
        "asset_type": asset_type,
        "report_type": report_type,
        "horizon_bars": horizon_bars,
        "signal_market_date": signal_market_date,
        "aligned_asset_start_date": signal_market_date,
        "aligned_asset_end_date": end_date,
        "aligned_asset_return_pct": aligned_return if row_status == "available" else None,
        "row_status": row_status,
        "evaluation_role": role,
        "decision_label": "avoid" if role == "observational_avoid" else "tradeable",
        "primary_benchmark": "SPY" if asset_type == "stock" else "BTC",
        "primary_benchmark_status": primary_status,
        "primary_excess_aligned_price_return_pct": primary_excess,
        "secondary_benchmark_status": secondary_status,
        "secondary_excess_aligned_price_return_pct": None,
        "stock_regime": stock_regime if asset_type == "stock" else None,
        "crypto_regime": crypto_regime if asset_type == "crypto" else None,
        "investment_quality_score": investment_quality_score,
        "swing_trade_score": swing_trade_score,
        "decision_confidence_score": decision_confidence_score,
        "data_quality_score": data_quality_score,
        "expected_value_r": expected_value_r,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "stop_touched": stop_touched,
        "target_2r_touched": target_2r_touched,
        "target_3r_touched": target_3r_touched,
    }
    row["evaluation_row_id"] = _evaluation_row_id(row)
    row["evaluation_row_hash"] = _sha(
        {key: value for key, value in row.items() if key != "evaluation_row_hash"}
    )
    return row


def _artifact(rows: list[dict[str, object]], *, dataset_status: str | None = None) -> dict[str, object]:
    if dataset_status is None:
        dataset_status = (
            "CANONICAL_EVALUATION_ROWS_AVAILABLE"
            if any(row["primary_benchmark_status"] == "available" for row in rows)
            else "CANONICAL_SAMPLE_NO_VALID_BENCHMARK"
        )
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_policy_version": BENCHMARK_POLICY_VERSION,
        "anchor_policy_version": ANCHOR_POLICY_VERSION,
        "dataset_status": dataset_status,
        "coverage": {},
        "rows": rows,
    }
    payload["artifact_hash"] = _sha(payload)
    return payload


def _cell(result: dict[str, object], *, asset_type: str = "stock", report_type: str = "main", horizon_bars: int = 20) -> dict[str, object]:
    cells = result["cells"]
    assert isinstance(cells, list)
    for cell in cells:
        if (
            cell["asset_type"] == asset_type
            and cell["report_type"] == report_type
            and cell["horizon_bars"] == horizon_bars
        ):
            return cell
    raise AssertionError("expected cell is missing")


def _bootstrap_order_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    roles = (
        "trade_candidate",
        "conditional_candidate",
        "observational_wait",
        "observational_avoid",
    )
    for unit in range(4):
        signal_market_date = (date(2026, 1, 1) + timedelta(days=10 * unit)).isoformat()
        for role_index, role in enumerate(roles):
            for asset_index in range(4):
                value = float(unit * 16 + role_index * 4 + asset_index)
                rows.append(
                    _row(
                        f"order-{unit}-{role_index}-{asset_index}",
                        asset=f"ORDER_{unit}_{role_index}_{asset_index}",
                        horizon_bars=5,
                        signal_market_date=signal_market_date,
                        role=role,
                        aligned_return=(value + 1.0) / 100.0,
                        primary_excess=(value - 20.0) / 100.0,
                        investment_quality_score=value + 1.0,
                        swing_trade_score=(value % 7.0) + 0.5,
                        decision_confidence_score=(value % 5.0) + 1.0,
                        data_quality_score=(value % 6.0) + 1.0,
                        expected_value_r=(value / 10.0) - 1.0,
                    )
                )
    return rows


class PredictiveStatisticsRedTests(unittest.TestCase):
    def test_module_entrypoint_and_policy_constants_exist(self):
        module = _load_statistics_module()
        self.assertEqual(module.SCHEMA_VERSION, "1.0")
        self.assertEqual(module.STATISTICS_POLICY_VERSION, "1.0")
        self.assertEqual(module.BOOTSTRAP_REPLICATES, 10000)

    def test_source_has_zero_outside_input_dependencies(self):
        module = _load_statistics_module()
        source = inspect.getsource(module)
        for forbidden in (
            "sqlite3.connect",
            "socket",
            "urllib",
            "LiveDataLoader",
            "fetch_json",
            "fetch_text",
        ):
            self.assertNotIn(forbidden, source)

    def test_zero_sample_is_valid_and_never_authorizes_calibration(self):
        module = _load_statistics_module()
        result = module.build_statistics_artifact(_artifact([], dataset_status="NO_CANONICAL_SAMPLE"))
        self.assertEqual(result["statistics_status"], "INSUFFICIENT")
        self.assertEqual(result["cells"], [])
        self.assertFalse(result["calibration_authorized"])

    def test_invalid_input_artifact_hash_is_rejected(self):
        module = _load_statistics_module()
        invalid = _artifact([])
        invalid["artifact_hash"] = "0" * 64
        with self.assertRaises(module.PredictiveStatisticsInputError):
            module.build_statistics_artifact(invalid)

    def test_invalid_evaluation_row_hash_is_rejected(self):
        module = _load_statistics_module()
        row = _row("tampered")
        row["aligned_asset_return_pct"] = 0.99
        invalid = _artifact([row])
        with self.assertRaises(module.PredictiveStatisticsInputError):
            module.build_statistics_artifact(invalid)

    def test_invalid_evaluation_row_id_is_rejected(self):
        module = _load_statistics_module()
        row = _row("invalid-id")
        row["evaluation_row_id"] = "0" * 64
        with self.assertRaises(module.PredictiveStatisticsInputError):
            module.build_statistics_artifact(_artifact([row]))

    def test_duplicate_evaluation_row_id_is_rejected_without_deduplication(self):
        module = _load_statistics_module()
        row = _row("duplicate-row-id")
        duplicate = dict(row)
        with self.assertRaises(module.PredictiveStatisticsInputError):
            module.build_statistics_artifact(_artifact([row, duplicate]))

    def test_one_hundred_overlapping_rows_are_not_one_hundred_time_units(self):
        module = _load_statistics_module()
        rows = [_row(str(index), signal_market_date="2026-01-01") for index in range(100)]
        cell = _cell(module.build_statistics_artifact(_artifact(rows)))
        self.assertEqual(cell["raw_signal_count"], 100)
        self.assertEqual(cell["unique_asset_date_count"], 100)
        self.assertEqual(cell["signal_date_count"], 1)
        self.assertEqual(cell["independent_time_unit_count"], 1)

    def test_overlapping_signal_dates_are_not_independent(self):
        module = _load_statistics_module()
        rows = [
            _row(
                str(index),
                horizon_bars=5,
                signal_market_date=(date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            )
            for index in range(10)
        ]
        cell = _cell(module.build_statistics_artifact(_artifact(rows)), horizon_bars=5)
        self.assertEqual(cell["signal_date_count"], 10)
        self.assertEqual(cell["independent_time_unit_count"], 2)
        self.assertEqual(cell["overlapping_signal_date_count"], 8)

    def test_non_overlapping_windows_increase_independent_unit_count(self):
        module = _load_statistics_module()
        rows = [
            _row(str(index), signal_market_date=(date(2026, 1, 1) + timedelta(days=20 * index)).isoformat())
            for index in range(4)
        ]
        cell = _cell(module.build_statistics_artifact(_artifact(rows)))
        self.assertEqual(cell["signal_date_count"], 4)
        self.assertEqual(cell["independent_time_unit_count"], 4)
        self.assertEqual(cell["overlapping_signal_date_count"], 0)

    def test_same_signal_date_with_multiple_assets_is_one_time_unit(self):
        module = _load_statistics_module()
        rows = [
            _row("one", asset="AAA", signal_market_date="2026-01-01"),
            _row("two", asset="BBB", signal_market_date="2026-01-01"),
        ]
        cell = _cell(module.build_statistics_artifact(_artifact(rows)))
        self.assertEqual(cell["unique_asset_date_count"], 2)
        self.assertEqual(cell["signal_date_count"], 1)
        self.assertEqual(cell["independent_time_unit_count"], 1)

    def test_duplicate_asset_date_units_are_reported_without_deduplication(self):
        module = _load_statistics_module()
        rows = [
            _row("duplicate-a", asset="AAA", signal_market_date="2026-01-01"),
            _row("duplicate-b", asset="AAA", signal_market_date="2026-01-01"),
        ]
        cell = _cell(module.build_statistics_artifact(_artifact(rows)))
        self.assertEqual(cell["duplicate_analysis_unit_count"], 1)
        self.assertEqual(cell["raw_signal_count"], 2)
        self.assertEqual(cell["absolute"]["count"], 2)

    def test_main_close_and_horizons_are_separate_cells(self):
        module = _load_statistics_module()
        rows = [
            _row("main20", report_type="main", horizon_bars=20),
            _row("close20", report_type="close", horizon_bars=20),
            _row("main5", report_type="main", horizon_bars=5),
        ]
        result = module.build_statistics_artifact(_artifact(rows))
        self.assertEqual(len(result["cells"]), 3)
        self.assertEqual(_cell(result, report_type="main", horizon_bars=20)["count"], 1)
        self.assertEqual(_cell(result, report_type="close", horizon_bars=20)["count"], 1)
        self.assertEqual(_cell(result, report_type="main", horizon_bars=5)["count"], 1)

    def test_self_benchmark_is_excluded_from_primary_coverage_denominator(self):
        module = _load_statistics_module()
        rows = [
            _row("btc", asset="BTC", asset_type="crypto", primary_status="self_benchmark_unavailable", primary_excess=None, crypto_regime="crypto_regime"),
            _row("eth-missing", asset="ETH", asset_type="crypto", primary_status="benchmark_missing", primary_excess=None, crypto_regime="crypto_regime"),
        ]
        cell = _cell(module.build_statistics_artifact(_artifact(rows)), asset_type="crypto")
        self.assertEqual(cell["self_benchmark_unavailable_count"], 1)
        self.assertEqual(cell["primary_eligible_count"], 1)
        self.assertEqual(cell["primary_available_count"], 0)
        self.assertEqual(cell["primary_coverage"], 0.0)

    def test_self_benchmark_count_reports_rows_even_when_asset_outcome_is_invalid(self):
        module = _load_statistics_module()
        rows = [
            _row(
                "btc-invalid",
                asset="BTC",
                asset_type="crypto",
                primary_status="self_benchmark_unavailable",
                primary_excess=None,
                crypto_regime="crypto_regime",
                row_status="invalid_asset_outcome",
            ),
            _row(
                "btc-valid",
                asset="BTC2",
                asset_type="crypto",
                primary_status="self_benchmark_unavailable",
                primary_excess=None,
                crypto_regime="crypto_regime",
            ),
        ]
        cell = _cell(module.build_statistics_artifact(_artifact(rows)), asset_type="crypto")
        self.assertEqual(cell["self_benchmark_unavailable_count"], 2)

    def test_missing_primary_enters_coverage_and_secondary_does_not(self):
        module = _load_statistics_module()
        rows = [
            _row("missing", primary_status="benchmark_missing", primary_excess=0.02, secondary_status="available"),
            _row("available", primary_status="available", primary_excess=0.10, secondary_status="not_recorded"),
            _row("secondary", primary_status="benchmark_missing", primary_excess=0.03, secondary_status="available"),
        ]
        cell = _cell(module.build_statistics_artifact(_artifact(rows)))
        self.assertEqual(cell["primary_eligible_count"], 3)
        self.assertEqual(cell["primary_available_count"], 1)
        self.assertEqual(cell["primary_coverage"], 1 / 3)
        self.assertEqual(cell["primary_available_unique_asset_date_count"], 1)
        self.assertEqual(cell["market_relative"]["count"], 1)

    def test_absolute_metrics_keep_positive_avoid_direction(self):
        module = _load_statistics_module()
        rows = [
            _row("avoid", role="observational_avoid", aligned_return=0.10, primary_excess=0.10),
            _row("trade", role="trade_candidate", aligned_return=-0.05, primary_excess=-0.05),
        ]
        result = module.build_statistics_artifact(_artifact(rows))
        cell = _cell(result)
        self.assertEqual(cell["absolute"]["positive_return_rate"], 0.5)
        avoid = cell["role_summaries"]["observational_avoid"]
        self.assertEqual(avoid["absolute"]["mean_aligned_asset_return_pct"], 0.10)
        self.assertEqual(avoid["market_relative"]["mean_primary_excess_aligned_price_return_pct"], 0.10)

    def test_all_existing_roles_have_separate_summaries(self):
        module = _load_statistics_module()
        rows = [_row(role, role=role) for role in ROLES]
        result = module.build_statistics_artifact(_artifact(rows))
        self.assertEqual(set(_cell(result)["role_summaries"]), set(ROLES))
        for role in ROLES:
            self.assertEqual(_cell(result)["role_summaries"][role]["count"], 1)

    def test_fixed_trade_vs_avoid_comparison_preserves_lhs_minus_rhs(self):
        module = _load_statistics_module()
        rows: list[dict[str, object]] = []
        for index in range(16):
            day = (date(2026, 1, 1) + timedelta(days=5 * index)).isoformat()
            rows.append(_row(f"trade-{index}", asset=f"TRADE_{index}", role="trade_candidate", horizon_bars=5, signal_market_date=day, primary_excess=0.20))
            rows.append(_row(f"avoid-{index}", asset=f"AVOID_{index}", role="observational_avoid", horizon_bars=5, signal_market_date=day, primary_excess=-0.10))
        cell = _cell(module.build_statistics_artifact(_artifact(rows)), horizon_bars=5)
        comparison = next(item for item in cell["comparisons"] if item["comparison_key"] == "trade_candidate_vs_observational_avoid")
        self.assertEqual(comparison["status"], "available")
        self.assertAlmostEqual(comparison["difference_in_median_primary_excess"], 0.30)
        self.assertAlmostEqual(comparison["difference_in_mean_primary_excess"], 0.30)

    def test_missing_and_small_comparison_groups_have_explicit_status(self):
        module = _load_statistics_module()
        result = module.build_statistics_artifact(_artifact([_row("trade", role="trade_candidate")]))
        cell = _cell(result)
        missing = next(item for item in cell["comparisons"] if item["comparison_key"] == "trade_candidate_vs_observational_avoid")
        self.assertEqual(missing["status"], "missing_group")

        rows = [_row(str(index), role="trade_candidate") for index in range(15)]
        rows.append(_row("avoid", role="observational_avoid"))
        result = module.build_statistics_artifact(_artifact(rows))
        small = next(item for item in _cell(result)["comparisons"] if item["comparison_key"] == "trade_candidate_vs_observational_avoid")
        self.assertEqual(small["status"], "insufficient_group_sample")

    def test_spearman_perfect_positive_negative_ties_and_variation(self):
        module = _load_statistics_module()
        rows: list[dict[str, object]] = []
        for index in range(20):
            rows.append(
                _row(
                    f"score-{index}",
                    investment_quality_score=float(index),
                    swing_trade_score=float(20 - index),
                    decision_confidence_score=float(index % 3),
                    data_quality_score=1.0,
                    expected_value_r=None if index == 0 else float(index),
                    aligned_return=float(index),
                    primary_excess=float(index) / 10,
                )
            )
        relationships = _cell(module.build_statistics_artifact(_artifact(rows)))["score_relationships"]
        self.assertEqual(relationships["investment_quality_score"]["spearman_aligned_asset_return"], 1.0)
        self.assertEqual(relationships["swing_trade_score"]["spearman_aligned_asset_return"], -1.0)
        self.assertEqual(relationships["decision_confidence_score"]["status_aligned_asset_return"], "available")
        self.assertEqual(relationships["data_quality_score"]["status_aligned_asset_return"], "insufficient_variation")
        self.assertEqual(relationships["expected_value_r"]["missing_count"], 1)
        self.assertIsNone(relationships["expected_value_r"]["spearman_aligned_asset_return"])

    def test_score_relationship_requires_twenty_values_and_three_distinct_outcomes(self):
        module = _load_statistics_module()
        rows = [_row(str(index), investment_quality_score=float(index), aligned_return=0.1, primary_excess=0.1) for index in range(20)]
        relationship = _cell(module.build_statistics_artifact(_artifact(rows)))["score_relationships"]["investment_quality_score"]
        self.assertEqual(relationship["status_aligned_asset_return"], "insufficient_variation")
        self.assertIsNone(relationship["spearman_aligned_asset_return"])

    def test_bootstrap_is_deterministic_and_uses_exact_requested_replicates(self):
        module = _load_statistics_module()
        rows = [
            _row(str(index), signal_market_date=(date(2026, 1, 1) + timedelta(days=20 * index)).isoformat(), primary_excess=0.01 * index)
            for index in range(5)
        ]
        first = module.build_statistics_artifact(_artifact(rows))
        second = module.build_statistics_artifact(_artifact(rows))
        self.assertEqual(first, second)
        market = _cell(first)["market_relative"]
        self.assertEqual(market["bootstrap_replicates_requested"], 10000)
        self.assertEqual(market["bootstrap_replicates_effective"], 10000)
        self.assertIsNotNone(market["mean_primary_excess_aligned_price_return_pct_ci95_lower"])
        self.assertIsNotNone(market["median_primary_excess_aligned_price_return_pct_ci95_upper"])

    def test_bootstrap_resamples_complete_temporal_units(self):
        module = _load_statistics_module()
        units = [
            {"rows": [{"marker": float(index)}, {"marker": float(index)}]}
            for index in range(4)
        ]

        def unit_sum(rows):
            return sum(float(row["marker"]) for row in rows)

        lower, upper, effective = module._bootstrap_single_metric(
            units,
            unit_sum,
            input_hash="a" * 64,
            cell_key="stock/main/20",
            metric_key="unit-preservation",
        )
        rng = random.Random(module._seed_for("a" * 64, "stock/main/20", "unit-preservation"))
        expected = []
        for _ in range(10000):
            sampled = []
            for _ in range(len(units)):
                sampled.extend(units[rng.randrange(len(units))]["rows"])
            expected.append(unit_sum(sampled))
        expected.sort()
        self.assertEqual(effective, 10000)
        self.assertEqual(lower, expected[math.floor(0.025 * (len(expected) - 1))])
        self.assertEqual(upper, expected[math.ceil(0.975 * (len(expected) - 1))])

    def test_insufficient_independent_units_have_no_confidence_intervals(self):
        module = _load_statistics_module()
        rows = [_row(str(index), signal_market_date="2026-01-01", primary_excess=0.1) for index in range(25)]
        market = _cell(module.build_statistics_artifact(_artifact(rows)))["market_relative"]
        self.assertEqual(market["bootstrap_replicates_effective"], 0)
        self.assertIsNone(market["mean_primary_excess_aligned_price_return_pct_ci95_lower"])

    def test_key_order_invariance_and_canonical_output_replay(self):
        module = _load_statistics_module()
        row = _row("order")
        first = _artifact([row])
        second = {key: first[key] for key in reversed(list(first))}
        second["rows"] = [{key: row[key] for key in reversed(list(row))}]
        result_one = module.build_statistics_artifact(first)
        result_two = module.build_statistics_artifact(second)
        self.assertEqual(result_one, result_two)
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"
            output_path = Path(directory) / "output.json"
            input_path.write_bytes(_canonical(second))
            self.assertEqual(module.main(["--input-path", str(input_path), "--output-path", str(output_path)]), 0)
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), result_one)
            self.assertEqual(output_path.read_bytes().count(b"\n"), 1)

    def test_list_row_order_invariance_includes_bootstrap_and_confidence_intervals(self):
        module = _load_statistics_module()
        rows = _bootstrap_order_rows()
        first = _artifact(rows)
        second = _artifact(list(reversed(rows)))
        self.assertNotEqual(first["artifact_hash"], second["artifact_hash"])
        with tempfile.TemporaryDirectory() as directory:
            input_one = Path(directory) / "input-one.json"
            input_two = Path(directory) / "input-two.json"
            output_one = Path(directory) / "output-one.json"
            output_two = Path(directory) / "output-two.json"
            input_one.write_bytes(_canonical(first))
            input_two.write_bytes(_canonical(second))
            self.assertEqual(module.main(["--input-path", str(input_one), "--output-path", str(output_one)]), 0)
            self.assertEqual(module.main(["--input-path", str(input_two), "--output-path", str(output_two)]), 0)
            self.assertEqual(output_one.read_bytes(), output_two.read_bytes())
            first_result = json.loads(output_one.read_text(encoding="utf-8"))
            second_result = json.loads(output_two.read_text(encoding="utf-8"))
            self.assertEqual(first_result["input_artifact_hash"], second_result["input_artifact_hash"])
            self.assertEqual(first_result["artifact_hash"], second_result["artifact_hash"])
            first_cell = _cell(first_result, horizon_bars=5)
            second_cell = _cell(second_result, horizon_bars=5)
            self.assertEqual(first_cell["independent_time_unit_count"], second_cell["independent_time_unit_count"])
            self.assertEqual(first_cell["market_relative"], second_cell["market_relative"])
            self.assertEqual(first_cell["comparisons"], second_cell["comparisons"])
            self.assertEqual(first_cell["score_relationships"], second_cell["score_relationships"])

    def test_row_content_mutation_changes_semantic_input_and_output_hashes(self):
        module = _load_statistics_module()
        first = _artifact([_row("content", aligned_return=0.10)])
        second = _artifact([_row("content", aligned_return=0.11)])
        first_result = module.build_statistics_artifact(first)
        second_result = module.build_statistics_artifact(second)
        self.assertNotEqual(first_result["input_artifact_hash"], second_result["input_artifact_hash"])
        self.assertNotEqual(first_result["artifact_hash"], second_result["artifact_hash"])

    def test_market_relative_readiness_is_insufficient_for_small_sample(self):
        module = _load_statistics_module()
        result = module.build_statistics_artifact(_artifact([_row("small")]))
        self.assertEqual(_cell(result)["market_relative_readiness"], "INSUFFICIENT")
        self.assertEqual(result["overall_market_relative_readiness"], "INSUFFICIENT")

    def test_absolute_readiness_for_btc_is_separate_and_never_evaluation_ready(self):
        module = _load_statistics_module()
        rows = [
            _row(
                str(index),
                asset=f"BTC_{index}",
                asset_type="crypto",
                primary_status="self_benchmark_unavailable",
                primary_excess=None,
                crypto_regime="crypto_regime",
            )
            for index in range(55)
        ]
        cell = _cell(module.build_statistics_artifact(_artifact(rows)), asset_type="crypto")
        self.assertIn(cell["absolute_sample_status"], {"INSUFFICIENT", "EXPLORATORY"})
        self.assertNotEqual(cell["market_relative_readiness"], "EVALUATION_READY")

    def test_evaluation_ready_never_authorizes_calibration(self):
        module = _load_statistics_module()
        rows: list[dict[str, object]] = []
        roles = ("trade_candidate", "observational_avoid", "conditional_candidate", "observational_wait")
        for unit in range(12):
            day = (date(2026, 1, 1) + timedelta(days=20 * unit)).isoformat()
            regime = "risk_on" if unit < 6 else "risk_off"
            for asset_index in range(18):
                role = roles[asset_index % len(roles)]
                rows.append(
                    _row(
                        f"ready-{unit}-{asset_index}",
                        asset=f"READY_{unit}_{asset_index}",
                        signal_market_date=day,
                        role=role,
                        stock_regime=regime,
                        primary_excess=0.05 if role == "trade_candidate" else -0.01,
                    )
                )
        result = module.build_statistics_artifact(_artifact(rows))
        cell = _cell(result)
        self.assertEqual(cell["market_relative_readiness"], "EVALUATION_READY")
        self.assertEqual(result["overall_market_relative_readiness"], "EVALUATION_READY")
        self.assertFalse(result["calibration_authorized"])

    def test_readiness_requires_independent_time_units_even_with_row_coverage(self):
        module = _load_statistics_module()
        rows = [
            _row(
                str(index),
                asset=f"COVERAGE_{index}",
                signal_market_date="2026-01-01",
                stock_regime="risk_on" if index < 30 else "risk_off",
                primary_excess=0.05,
            )
            for index in range(60)
        ]
        cell = _cell(module.build_statistics_artifact(_artifact(rows)))
        self.assertEqual(cell["independent_time_unit_count"], 1)
        self.assertEqual(cell["market_relative_readiness"], "INSUFFICIENT")

    def test_source_does_not_use_global_random_or_clock_seed(self):
        module = _load_statistics_module()
        source = inspect.getsource(module)
        self.assertNotIn("random.seed(", source)
        self.assertNotIn("time.time(", source)
        self.assertNotIn("uuid.", source)
        with patch.object(random, "random", side_effect=AssertionError("global RNG used")):
            result = module.build_statistics_artifact(_artifact([_row("seed")]))
        self.assertEqual(result["calibration_authorized"], False)
        self.assertNotEqual(module._seed_for("a" * 64, "cell-a", "metric"), module._seed_for("b" * 64, "cell-a", "metric"))
        self.assertNotEqual(module._seed_for("a" * 64, "cell-a", "metric"), module._seed_for("a" * 64, "cell-b", "metric"))
        self.assertEqual(module._seed_for("a" * 64, "cell-a", "metric"), module._seed_for("a" * 64, "cell-a", "metric"))


if __name__ == "__main__":
    unittest.main()
