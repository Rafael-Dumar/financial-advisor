from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from advisor.cache import SQLiteCache
from advisor.signal_observation import compute_observation_hash
from advisor.signal_outcome import evaluate_signal_observation
from tests.test_signal_outcome import _candle, _observation, _series

from advisor.predictive_evaluation import (
    ANCHOR_POLICY_VERSION,
    BENCHMARK_POLICY_VERSION,
    SCHEMA_VERSION,
    PredictiveEvaluationError,
    build_evaluation_artifact,
    main as predictive_evaluation_main,
    write_evaluation_artifact,
)


def _dates(start: str = "2026-08-09", count: int = 5) -> list[str]:
    first = date.fromisoformat(start)
    return [(first + timedelta(days=index)).isoformat() for index in range(count)]


def _asset_candles(
    *,
    start: str = "2026-08-09",
    dates: list[str] | None = None,
    opens: list[float] | None = None,
    closes: list[float] | None = None,
) -> list[dict[str, object]]:
    resolved_dates = dates or _dates(start)
    opens = opens or [100.0 for _ in resolved_dates]
    closes = closes or [100.0 for _ in resolved_dates]
    return [
        _candle(day, open_price=open_price, close=close)
        for day, open_price, close in zip(resolved_dates, opens, closes)
    ]


def _benchmark_candle(day: str, open_price: float, close: float) -> dict[str, object]:
    return _candle(day, open_price=open_price, close=close)


def _benchmark_series(
    *,
    dates: list[str],
    first_open: float = 200.0,
    last_close: float = 220.0,
    provider: str = "fixture",
    price_basis: str = "split_adjusted_ohlc",
    extra_dates: list[str] | None = None,
) -> dict[str, object]:
    all_dates = list(dates) + list(extra_dates or [])
    candles: list[dict[str, object]] = []
    for index, day in enumerate(all_dates):
        if day == dates[0]:
            open_price = first_open
        else:
            open_price = first_open + index
        close = last_close if day == dates[-1] else open_price
        candles.append(_benchmark_candle(day, open_price, close))
    return {
        "asset_type": "stock",
        "provider": provider,
        "price_basis": price_basis,
        "candles": candles,
    }


def _crypto_benchmark_series(
    *,
    dates: list[str],
    first_open: float = 30_000.0,
    last_close: float = 31_000.0,
    price_basis: str = "raw_ohlcv",
) -> dict[str, object]:
    return {
        "asset_type": "crypto",
        "provider": "fixture",
        "price_basis": price_basis,
        "candles": [
            _benchmark_candle(
                day,
                first_open if index == 0 else first_open + index,
                last_close if index == len(dates) - 1 else first_open + index,
            )
            for index, day in enumerate(dates)
        ],
    }


def _benchmark_payload(benchmarks: dict[str, dict[str, object]]) -> dict[str, object]:
    return {"schema_version": "1.0", "benchmarks": benchmarks}


class PredictiveEvaluationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "evaluation.db"
        self.benchmark_path = self.root / "benchmarks.json"
        self.output_path = self.root / "artifact.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_benchmark(self, payload: dict[str, object]) -> None:
        self.benchmark_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )

    def _seed_signal(
        self,
        *,
        symbol: str = "AMD",
        asset_type: str = "stock",
        decision: str = "tradeable",
        sector_benchmark: str | None = "SMH",
        asset_basis: str = "split_adjusted_ohlc",
        asset_candles: list[dict[str, object]] | None = None,
        run_id: str = "evaluation-run",
    ) -> tuple[dict[str, object], dict[str, object]]:
        observation = _observation(
            symbol,
            asset_type=asset_type,
            decision=decision,
            run_id=run_id,
            signal_timestamp_utc="2026-08-09T02:30:00Z",
        )
        if sector_benchmark != observation.sector_benchmark:
            observation = replace(observation, sector_benchmark=sector_benchmark, observation_hash="")
            observation = replace(observation, observation_hash=compute_observation_hash(observation))
        resolved_candles = asset_candles or _asset_candles(
            start="2026-08-09" if asset_type == "stock" else "2026-08-10"
        )
        series = _series(
            symbol,
            asset_type=asset_type,
            candles=resolved_candles,
            price_basis=asset_basis,
        )
        outcomes = evaluate_signal_observation(observation, series).outcomes
        self.assertTrue(outcomes, "fixture must produce a five-bar canonical outcome")
        cache = SQLiteCache(self.db_path)
        self.assertEqual(cache.save_signal_observations([observation]).status, "written")
        self.assertEqual(
            cache.save_signal_forward_outcomes_for_signal(outcomes[:1]).status,
            "written",
        )
        return dict(observation.__dict__), dict(outcomes[0].__dict__)

    def _evaluate(self) -> dict[str, object]:
        return build_evaluation_artifact(
            db_path=self.db_path,
            benchmark_input_path=self.benchmark_path,
        )

    def _row(self, artifact: dict[str, object], index: int = 0) -> dict[str, object]:
        rows = artifact["rows"]
        self.assertIsInstance(rows, list)
        return rows[index]


class PredictiveEvaluationRedTests(unittest.TestCase):
    def test_module_api_is_the_new_isolated_entrypoint(self):
        self.assertEqual(SCHEMA_VERSION, "1.0")
        self.assertEqual(BENCHMARK_POLICY_VERSION, "1.0")
        self.assertEqual(ANCHOR_POLICY_VERSION, "first_forward_open_to_horizon_close_v1")

    def test_source_has_no_network_or_cache_writer_dependency(self):
        import advisor.predictive_evaluation as module

        source = inspect.getsource(module)
        self.assertNotIn("SQLiteCache", source)
        self.assertNotIn("LiveDataLoader", source)
        self.assertNotIn("urllib", source)
        self.assertNotIn("socket", source)
        self.assertNotIn("http.client", source)


class PredictiveEvaluationPolicyTests(PredictiveEvaluationFixture):
    def test_stock_amd_has_valid_primary_spy_comparison(self):
        self._seed_signal(symbol="AMD")
        dates = _dates()
        self._write_benchmark(
            _benchmark_payload(
                {
                    "SPY": _benchmark_series(dates=dates),
                    "SMH": _benchmark_series(dates=dates, first_open=400, last_close=440),
                }
            )
        )

        artifact = self._evaluate()
        row = self._row(artifact)
        self.assertEqual(artifact["dataset_status"], "CANONICAL_EVALUATION_ROWS_AVAILABLE")
        self.assertEqual(row["primary_benchmark"], "SPY")
        self.assertEqual(row["primary_benchmark_status"], "available")
        self.assertEqual(
            [bar["date"] for bar in json.loads(row["primary_benchmark_bars_json"])],
            [bar["date"] for bar in json.loads(row["asset_bars_json"])],
        )
        self.assertIsNotNone(row["primary_excess_aligned_price_return_pct"])

    def test_crypto_eth_has_valid_primary_btc_comparison(self):
        self._seed_signal(symbol="ETH", asset_type="crypto", asset_basis="raw_ohlcv")
        dates = _dates("2026-08-10")
        self._write_benchmark(
            _benchmark_payload({"BTC": _crypto_benchmark_series(dates=dates)})
        )

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark"], "BTC")
        self.assertEqual(row["primary_benchmark_status"], "available")
        self.assertIsNotNone(row["primary_excess_aligned_price_return_pct"])

    def test_spy_is_not_compared_against_itself(self):
        self._seed_signal(symbol="SPY")
        dates = _dates()
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=dates)}))

        row = self._row(self._evaluate())
        self.assertIsNone(row["primary_benchmark"])
        self.assertEqual(row["primary_benchmark_status"], "self_benchmark_unavailable")
        self.assertIsNone(row["primary_benchmark_aligned_return_pct"])
        self.assertIsNone(row["primary_excess_aligned_price_return_pct"])

    def test_btc_is_not_compared_against_itself(self):
        self._seed_signal(symbol="BTC", asset_type="crypto", asset_basis="raw_ohlcv")
        dates = _dates("2026-08-10")
        self._write_benchmark(_benchmark_payload({"BTC": _crypto_benchmark_series(dates=dates)}))

        row = self._row(self._evaluate())
        self.assertIsNone(row["primary_benchmark"])
        self.assertEqual(row["primary_benchmark_status"], "self_benchmark_unavailable")
        self.assertIsNone(row["primary_excess_aligned_price_return_pct"])

    def test_semiconductor_secondary_smh_is_diagnostic(self):
        self._seed_signal(symbol="AMD", sector_benchmark="SMH")
        dates = _dates()
        self._write_benchmark(
            _benchmark_payload(
                {
                    "SPY": _benchmark_series(dates=dates),
                    "SMH": _benchmark_series(dates=dates, first_open=300, last_close=330),
                }
            )
        )

        row = self._row(self._evaluate())
        self.assertEqual(row["secondary_benchmark"], "SMH")
        self.assertEqual(row["secondary_benchmark_status"], "available")
        self.assertIsNotNone(row["secondary_excess_aligned_price_return_pct"])

    def test_invalid_secondary_series_does_not_contaminate_valid_primary(self):
        self._seed_signal(symbol="AMD", sector_benchmark="SMH")
        dates = _dates()
        invalid_smh = _benchmark_series(dates=dates, first_open=300, last_close=330)
        invalid_smh["candles"][0] = {"malformed": True}
        self._write_benchmark(
            _benchmark_payload(
                {
                    "SPY": _benchmark_series(dates=dates),
                    "SMH": invalid_smh,
                }
            )
        )

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark"], "SPY")
        self.assertEqual(row["primary_benchmark_status"], "available")
        self.assertIsNotNone(row["primary_excess_aligned_price_return_pct"])
        self.assertEqual(row["secondary_benchmark"], "SMH")
        self.assertEqual(row["secondary_benchmark_status"], "invalid_benchmark_input")
        self.assertIsNone(row["secondary_benchmark_aligned_return_pct"])
        self.assertIsNone(row["secondary_excess_aligned_price_return_pct"])
        self.assertEqual(
            self._evaluate()["dataset_status"],
            "CANONICAL_EVALUATION_ROWS_AVAILABLE",
        )

    def test_secondary_not_recorded_does_not_change_primary(self):
        self._seed_signal(symbol="AMD", sector_benchmark=None)
        dates = _dates()
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=dates)}))

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark_status"], "available")
        self.assertEqual(row["secondary_benchmark_status"], "not_recorded")
        self.assertIsNone(row["secondary_benchmark"])
        self.assertIsNotNone(row["primary_excess_aligned_price_return_pct"])

    def test_missing_primary_is_not_replaced_by_secondary(self):
        self._seed_signal(symbol="AMD", sector_benchmark="SMH")
        dates = _dates()
        self._write_benchmark(_benchmark_payload({"SMH": _benchmark_series(dates=dates)}))

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark"], "SPY")
        self.assertEqual(row["primary_benchmark_status"], "benchmark_missing")
        self.assertIsNone(row["primary_excess_aligned_price_return_pct"])
        self.assertEqual(row["secondary_benchmark_status"], "available")
        self.assertIsNotNone(row["secondary_excess_aligned_price_return_pct"])
        self.assertEqual(
            self._evaluate()["dataset_status"],
            "CANONICAL_SAMPLE_NO_VALID_BENCHMARK",
        )

        self.assertEqual(
            self._evaluate()["dataset_status"],
            "CANONICAL_SAMPLE_NO_VALID_BENCHMARK",
        )

    def test_invalid_primary_series_does_not_contaminate_valid_secondary(self):
        self._seed_signal(symbol="AMD", sector_benchmark="SMH")
        dates = _dates()
        invalid_spy = _benchmark_series(dates=dates)
        invalid_spy["candles"][0] = {"malformed": True}
        self._write_benchmark(
            _benchmark_payload(
                {
                    "SPY": invalid_spy,
                    "SMH": _benchmark_series(dates=dates, first_open=300, last_close=330),
                }
            )
        )

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark_status"], "invalid_benchmark_input")
        self.assertIsNone(row["primary_excess_aligned_price_return_pct"])
        self.assertEqual(row["secondary_benchmark_status"], "available")
        self.assertIsNotNone(row["secondary_excess_aligned_price_return_pct"])
        self.assertEqual(
            self._evaluate()["dataset_status"],
            "CANONICAL_SAMPLE_NO_VALID_BENCHMARK",
        )

    def test_structurally_invalid_primary_price_basis_does_not_contaminate_secondary(self):
        self._seed_signal(symbol="AMD", sector_benchmark="SMH")
        dates = _dates()
        invalid_spy = _benchmark_series(dates=dates)
        invalid_spy["price_basis"] = []
        self._write_benchmark(
            _benchmark_payload(
                {
                    "SPY": invalid_spy,
                    "SMH": _benchmark_series(dates=dates, first_open=300, last_close=330),
                }
            )
        )

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark_status"], "invalid_benchmark_input")
        self.assertEqual(row["secondary_benchmark_status"], "available")
        self.assertEqual(
            self._evaluate()["dataset_status"],
            "CANONICAL_SAMPLE_NO_VALID_BENCHMARK",
        )

    def test_invalid_unrelated_btc_does_not_contaminate_stock_primary(self):
        self._seed_signal(symbol="AMD", sector_benchmark=None)
        dates = _dates()
        invalid_btc = _crypto_benchmark_series(dates=dates, price_basis="raw_ohlcv")
        invalid_btc["candles"][0] = {"malformed": True}
        self._write_benchmark(
            _benchmark_payload(
                {
                    "SPY": _benchmark_series(dates=dates),
                    "BTC": invalid_btc,
                }
            )
        )

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark_status"], "available")
        self.assertIsNotNone(row["primary_excess_aligned_price_return_pct"])
        self.assertEqual(
            self._evaluate()["dataset_status"],
            "CANONICAL_EVALUATION_ROWS_AVAILABLE",
        )

    def test_invalid_unrelated_spy_does_not_contaminate_crypto_primary(self):
        self._seed_signal(symbol="ETH", asset_type="crypto", asset_basis="raw_ohlcv")
        dates = _dates("2026-08-10")
        invalid_spy = _benchmark_series(dates=dates)
        invalid_spy["candles"][0] = {"malformed": True}
        self._write_benchmark(
            _benchmark_payload(
                {
                    "BTC": _crypto_benchmark_series(dates=dates),
                    "SPY": invalid_spy,
                }
            )
        )

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark"], "BTC")
        self.assertEqual(row["primary_benchmark_status"], "available")
        self.assertIsNotNone(row["primary_excess_aligned_price_return_pct"])
        self.assertEqual(
            self._evaluate()["dataset_status"],
            "CANONICAL_EVALUATION_ROWS_AVAILABLE",
        )

    def test_incompatible_primary_basis_does_not_contaminate_valid_secondary(self):
        self._seed_signal(symbol="AMD", sector_benchmark="SMH")
        dates = _dates()
        self._write_benchmark(
            _benchmark_payload(
                {
                    "SPY": _benchmark_series(dates=dates, price_basis="raw_ohlcv"),
                    "SMH": _benchmark_series(dates=dates, first_open=300, last_close=330),
                }
            )
        )

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark_status"], "incompatible_price_basis")
        self.assertIsNone(row["primary_excess_aligned_price_return_pct"])
        self.assertEqual(row["secondary_benchmark_status"], "available")
        self.assertIsNotNone(row["secondary_excess_aligned_price_return_pct"])
        self.assertEqual(
            self._evaluate()["dataset_status"],
            "CANONICAL_SAMPLE_NO_VALID_BENCHMARK",
        )

    def test_invalid_top_level_schema_remains_global_input_failure(self):
        self._seed_signal(symbol="AMD", sector_benchmark="SMH")
        payload = _benchmark_payload(
            {
                "SPY": _benchmark_series(dates=_dates()),
                "SMH": _benchmark_series(dates=_dates()),
            }
        )
        payload["schema_version"] = "2.0"
        self._write_benchmark(payload)

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark_status"], "invalid_benchmark_input")
        self.assertEqual(row["secondary_benchmark_status"], "invalid_benchmark_input")

    def test_non_object_benchmarks_remains_global_input_failure(self):
        self._seed_signal(symbol="AMD", sector_benchmark="SMH")
        self._write_benchmark({"schema_version": "1.0", "benchmarks": []})

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark_status"], "invalid_benchmark_input")
        self.assertEqual(row["secondary_benchmark_status"], "invalid_benchmark_input")

    def test_non_allowlisted_secondary_is_not_selected(self):
        self._seed_signal(symbol="AMD", sector_benchmark="IWM")
        dates = _dates()
        self._write_benchmark(
            _benchmark_payload(
                {
                    "SPY": _benchmark_series(dates=dates),
                    "IWM": _benchmark_series(dates=dates),
                }
            )
        )

        row = self._row(self._evaluate())
        self.assertIsNone(row["secondary_benchmark"])
        self.assertEqual(row["secondary_benchmark_status"], "not_allowlisted")

    def test_crypto_has_no_secondary_benchmark(self):
        self._seed_signal(symbol="ETH", asset_type="crypto", asset_basis="raw_ohlcv")
        dates = _dates("2026-08-10")
        self._write_benchmark(_benchmark_payload({"BTC": _crypto_benchmark_series(dates=dates)}))

        row = self._row(self._evaluate())
        self.assertIsNone(row["secondary_benchmark"])
        self.assertEqual(row["secondary_benchmark_status"], "not_applicable")

    def test_unknown_price_basis_blocks_excess(self):
        self._seed_signal(symbol="AMD", asset_basis="unknown")
        dates = _dates()
        self._write_benchmark(
            _benchmark_payload(
                {
                    "SPY": _benchmark_series(dates=dates, price_basis="unknown"),
                }
            )
        )

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark_status"], "incompatible_price_basis")
        self.assertIsNone(row["primary_benchmark_aligned_return_pct"])
        self.assertIsNone(row["primary_excess_aligned_price_return_pct"])

    def test_mismatched_price_basis_blocks_excess(self):
        self._seed_signal(symbol="AMD", asset_basis="split_adjusted_ohlc")
        dates = _dates()
        self._write_benchmark(
            _benchmark_payload(
                {
                    "SPY": _benchmark_series(dates=dates, price_basis="raw_ohlcv"),
                }
            )
        )

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark_status"], "incompatible_price_basis")
        self.assertIsNone(row["primary_excess_aligned_price_return_pct"])

    def test_stock_raw_bases_are_not_accepted_as_expected_basis(self):
        for basis in ("raw_ohlcv", "raw_unadjusted"):
            with self.subTest(basis=basis):
                self._seed_signal(symbol="AMD", asset_basis=basis, run_id=f"run-{basis}")
                dates = _dates()
                self._write_benchmark(
                    _benchmark_payload({"SPY": _benchmark_series(dates=dates, price_basis=basis)})
                )
                row = self._row(self._evaluate())
                self.assertEqual(row["primary_benchmark_status"], "incompatible_price_basis")
                self.assertIsNone(row["primary_excess_aligned_price_return_pct"])

    def test_missing_benchmark_keeps_asset_row_and_fields(self):
        self._seed_signal(symbol="AMD")
        self._write_benchmark(_benchmark_payload({}))

        artifact = self._evaluate()
        row = self._row(artifact)
        self.assertEqual(artifact["dataset_status"], "CANONICAL_SAMPLE_NO_VALID_BENCHMARK")
        self.assertEqual(artifact["coverage"]["rows_total"], 1)
        self.assertEqual(row["primary_benchmark_status"], "benchmark_missing")
        self.assertEqual(row["aligned_asset_return_pct"], 0.0)
        self.assertIsNone(row["primary_excess_aligned_price_return_pct"])

    def test_missing_start_middle_and_end_dates_are_distinct_invalid_coverages(self):
        for missing_position in (0, 2, 4):
            with self.subTest(missing_position=missing_position):
                self._seed_signal(symbol="AMD", run_id=f"missing-{missing_position}")
                dates = _dates()
                missing_day = dates.pop(missing_position)
                self._write_benchmark(
                    _benchmark_payload(
                        {"SPY": _benchmark_series(dates=dates)}
                    )
                )
                row = self._row(self._evaluate())
                self.assertEqual(row["primary_benchmark_status"], "missing_required_dates")
                self.assertIsNone(row["primary_benchmark_aligned_return_pct"])
                self.assertIsNone(row["primary_excess_aligned_price_return_pct"])
                self.assertNotIn(missing_day, row["primary_benchmark_bars_json"] or "")


class PredictiveEvaluationAnchorAndReturnTests(PredictiveEvaluationFixture):
    def test_aligned_asset_uses_first_open_and_last_close_not_ideal_entry(self):
        candles = _asset_candles(opens=[90, 91, 92, 93, 94], closes=[91, 92, 93, 94, 110])
        self._seed_signal(symbol="AMD", asset_candles=candles)
        dates = _dates()
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=dates)}))

        row = self._row(self._evaluate())
        self.assertEqual(row["aligned_asset_return_pct"], (110 / 90) - 1)
        self.assertNotEqual(row["aligned_asset_return_pct"], (110 / 100) - 1)
        self.assertEqual(row["cash_zero_reference_return_pct"], 0.0)
        self.assertTrue(row["absolute_positive_return"])

    def test_benchmark_uses_same_first_date_open_and_last_date_close(self):
        self._seed_signal(symbol="AMD")
        dates = _dates()
        benchmark = _benchmark_series(dates=dates, first_open=200, last_close=260)
        self._write_benchmark(_benchmark_payload({"SPY": benchmark}))

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark_start_date"], dates[0])
        self.assertEqual(row["primary_benchmark_end_date"], dates[-1])
        self.assertEqual(row["primary_benchmark_start_open"], 200.0)
        self.assertEqual(row["primary_benchmark_end_close"], 260.0)
        self.assertEqual(row["primary_benchmark_aligned_return_pct"], (260 / 200) - 1)

    def test_forward_return_remains_separate_from_aligned_asset_return(self):
        candles = _asset_candles(opens=[90, 91, 92, 93, 94], closes=[91, 92, 93, 94, 110])
        _, outcome = self._seed_signal(symbol="AMD", asset_candles=candles)
        dates = _dates()
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=dates)}))

        row = self._row(self._evaluate())
        self.assertEqual(row["forward_return_pct"], outcome["forward_return_pct"])
        self.assertEqual(row["aligned_asset_return_pct"], (110 / 90) - 1)
        self.assertNotEqual(row["forward_return_pct"], row["aligned_asset_return_pct"])

    def test_benchmark_dates_are_exact_asset_dates_not_nth_benchmark_bars(self):
        asset_dates = ["2026-08-09", "2026-08-11", "2026-08-13", "2026-08-15", "2026-08-17"]
        candles = _asset_candles(dates=asset_dates)
        self._seed_signal(symbol="AMD", asset_candles=candles)
        benchmark_dates = _dates("2026-08-09", 9)
        benchmark = _benchmark_series(dates=benchmark_dates, first_open=200, last_close=300)
        self._write_benchmark(_benchmark_payload({"SPY": benchmark}))

        row = self._row(self._evaluate())
        selected = json.loads(row["primary_benchmark_bars_json"])
        self.assertEqual([bar["date"] for bar in selected], asset_dates)
        self.assertEqual(row["primary_benchmark_end_close"], selected[-1]["close"])

    def test_signal_date_close_is_never_used_as_benchmark_start(self):
        self._seed_signal(symbol="AMD")
        dates = _dates()
        benchmark = _benchmark_series(
            dates=dates,
            first_open=200,
            last_close=220,
        )
        benchmark["candles"].append(_benchmark_candle("2026-08-08", 900, 999))
        self._write_benchmark(_benchmark_payload({"SPY": benchmark}))

        row = self._row(self._evaluate())
        self.assertEqual(row["primary_benchmark_start_open"], 200.0)
        self.assertNotEqual(row["primary_benchmark_start_open"], 999.0)

    def test_avoid_and_blocked_keep_asset_return_direction(self):
        for decision in ("avoid", "blocked"):
            with self.subTest(decision=decision):
                self._seed_signal(
                    symbol=f"AMD{decision[0].upper()}",
                    decision=decision,
                    run_id=f"{decision}-run",
                    asset_candles=_asset_candles(
                        opens=[90, 91, 92, 93, 94],
                        closes=[91, 92, 93, 94, 110],
                    ),
                )
                dates = _dates()
                self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=dates)}))
                artifact = self._evaluate()
                row = next(
                    candidate
                    for candidate in artifact["rows"]
                    if candidate["decision_label"] == decision
                )
                self.assertEqual(row["decision_label"], decision)
                self.assertEqual(row["aligned_asset_return_pct"], (110 / 90) - 1)
                self.assertTrue(row["absolute_positive_return"])


class PredictiveEvaluationBenchmarkEvidenceTests(PredictiveEvaluationFixture):
    def test_extra_benchmark_dates_do_not_change_selected_bars_or_hash(self):
        self._seed_signal(symbol="AMD")
        dates = _dates()
        base = _benchmark_payload({"SPY": _benchmark_series(dates=dates)})
        self._write_benchmark(base)
        first = self._evaluate()
        first_row = self._row(first)

        extra = _benchmark_payload(
            {"SPY": _benchmark_series(dates=dates, extra_dates=["2026-08-08", "2026-08-20"])}
        )
        self._write_benchmark(extra)
        second = self._evaluate()
        second_row = self._row(second)
        self.assertEqual(first_row["primary_benchmark_bars_json"], second_row["primary_benchmark_bars_json"])
        self.assertEqual(first_row["primary_benchmark_bars_hash"], second_row["primary_benchmark_bars_hash"])
        self.assertEqual(first["artifact_hash"], second["artifact_hash"])

    def test_reversed_benchmark_input_order_replays_identical_bytes(self):
        self._seed_signal(symbol="AMD")
        dates = _dates()
        payload = _benchmark_payload(
            {
                "SPY": _benchmark_series(dates=dates),
                "SMH": _benchmark_series(dates=dates, first_open=300, last_close=330),
            }
        )
        self._write_benchmark(payload)
        write_evaluation_artifact(
            db_path=self.db_path,
            benchmark_input_path=self.benchmark_path,
            output_path=self.output_path,
        )
        first_bytes = self.output_path.read_bytes()

        reversed_payload = {
            "benchmarks": {
                name: {
                    **series,
                    "candles": list(reversed(series["candles"])),
                }
                for name, series in reversed(list(payload["benchmarks"].items()))
            },
            "schema_version": payload["schema_version"],
        }
        self._write_benchmark(reversed_payload)
        write_evaluation_artifact(
            db_path=self.db_path,
            benchmark_input_path=self.benchmark_path,
            output_path=self.output_path,
        )
        self.assertEqual(first_bytes, self.output_path.read_bytes())
        self.assertTrue(first_bytes.endswith(b"\n"))
        self.assertEqual(first_bytes.count(b"\n"), 1)

    def test_benchmark_bars_are_canonical_sorted_compact_and_hashed(self):
        self._seed_signal(symbol="AMD")
        dates = _dates()
        self._write_benchmark(
            _benchmark_payload(
                {"SPY": _benchmark_series(dates=list(reversed(dates)))}
            )
        )

        row = self._row(self._evaluate())
        bars_json = row["primary_benchmark_bars_json"]
        self.assertEqual(bars_json, json.dumps(json.loads(bars_json), sort_keys=True, separators=(",", ":")))
        self.assertEqual(
            row["primary_benchmark_bars_hash"],
            hashlib.sha256(bars_json.encode("utf-8")).hexdigest(),
        )
        self.assertEqual([bar["date"] for bar in json.loads(bars_json)], dates)


class PredictiveEvaluationBindingAndDatasetTests(PredictiveEvaluationFixture):
    def test_orphan_outcome_is_not_an_evaluation_row(self):
        self._seed_signal(symbol="AMD")
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("drop trigger signal_observations_no_delete")
            connection.execute("delete from signal_observations")
            connection.commit()
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=_dates())}))

        artifact = self._evaluate()
        self.assertEqual(artifact["coverage"]["observations_total"], 0)
        self.assertEqual(artifact["coverage"]["outcomes_total"], 1)
        self.assertEqual(artifact["coverage"]["rows_total"], 0)
        self.assertEqual(artifact["rows"], [])
        self.assertEqual(artifact["dataset_status"], "NO_CANONICAL_SAMPLE")

    def test_missing_tables_are_valid_zero_sample(self):
        with closing(sqlite3.connect(self.db_path)):
            pass
        self._write_benchmark(_benchmark_payload({}))

        artifact = self._evaluate()
        self.assertEqual(artifact["dataset_status"], "NO_CANONICAL_SAMPLE")
        self.assertEqual(artifact["rows"], [])
        self.assertEqual(artifact["coverage"]["observations_total"], 0)
        self.assertEqual(artifact["coverage"]["outcomes_total"], 0)

    def test_one_missing_ledger_table_is_still_zero_sample(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("create table signal_observations(signal_id text)")
            connection.commit()
        self._write_benchmark(_benchmark_payload({}))

        artifact = self._evaluate()
        self.assertEqual(artifact["dataset_status"], "NO_CANONICAL_SAMPLE")
        self.assertEqual(artifact["coverage"]["rows_total"], 0)

    def test_invalid_benchmark_file_keeps_rows_with_closed_status(self):
        self._seed_signal(symbol="AMD")
        self.benchmark_path.write_text("not-json", encoding="utf-8")

        artifact = self._evaluate()
        row = self._row(artifact)
        self.assertEqual(row["primary_benchmark_status"], "invalid_benchmark_input")
        self.assertIsNone(row["primary_excess_aligned_price_return_pct"])
        self.assertEqual(artifact["dataset_status"], "CANONICAL_SAMPLE_NO_VALID_BENCHMARK")

    def test_invalid_asset_bars_are_not_used_for_excess(self):
        self._seed_signal(symbol="AMD")
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("drop trigger signal_forward_outcomes_no_update")
            connection.execute(
                "update signal_forward_outcomes set asset_bars_hash = ?",
                ("0" * 64,),
            )
            connection.commit()
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=_dates())}))

        row = self._row(self._evaluate())
        self.assertEqual(row["row_status"], "invalid_asset_outcome")
        self.assertIsNone(row["aligned_asset_return_pct"])
        self.assertIsNone(row["primary_excess_aligned_price_return_pct"])

    def test_coverage_contains_only_objective_counts_and_no_statistical_fields(self):
        self._seed_signal(symbol="AMD")
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=_dates())}))

        coverage = self._evaluate()["coverage"]
        self.assertEqual(
            set(coverage),
            {
                "observations_total",
                "outcomes_total",
                "rows_total",
                "primary_benchmark_available",
                "primary_benchmark_unavailable",
                "secondary_benchmark_available",
                "secondary_benchmark_unavailable",
                "by_horizon",
                "by_asset_type",
                "by_report_type",
                "by_evaluation_role",
            },
        )
        serialized = json.dumps(coverage).lower()
        for forbidden in ("mean", "median", "spearman", "bootstrap", "confidence", "readiness", "bucket"):
            self.assertNotIn(forbidden, serialized)


class PredictiveEvaluationHashAndSanitizationTests(PredictiveEvaluationFixture):
    def test_evaluation_row_id_uses_exact_grain_and_excludes_performance(self):
        self._seed_signal(symbol="AMD")
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=_dates())}))

        row = self._row(self._evaluate())
        identity = {
            "anchor_policy_version": ANCHOR_POLICY_VERSION,
            "benchmark_policy_version": BENCHMARK_POLICY_VERSION,
            "horizon_bars": row["horizon_bars"],
            "observation_hash": row["observation_hash"],
            "outcome_hash": row["outcome_hash"],
            "outcome_id": row["outcome_id"],
            "schema_version": SCHEMA_VERSION,
            "signal_id": row["signal_id"],
        }
        expected = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(row["evaluation_row_id"], expected)
        self.assertNotIn("aligned_asset_return_pct", identity)
        self.assertIn("evaluation_policy_version", row)

    def test_evaluation_row_hash_excludes_only_itself_and_no_persisted_time(self):
        self._seed_signal(symbol="AMD")
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=_dates())}))

        row = self._row(self._evaluate())
        self.assertNotIn("persisted_at_utc", row)
        immutable = dict(row)
        immutable.pop("evaluation_row_hash")
        expected = hashlib.sha256(
            json.dumps(immutable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(row["evaluation_row_hash"], expected)

    def test_artifact_hash_is_replayable_and_excludes_only_artifact_hash(self):
        self._seed_signal(symbol="AMD")
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=_dates())}))

        first = self._evaluate()
        second = self._evaluate()
        self.assertEqual(first, second)
        immutable = dict(first)
        immutable.pop("artifact_hash")
        expected = hashlib.sha256(
            json.dumps(immutable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(first["artifact_hash"], expected)

    def test_output_sanitizes_paths_secrets_headers_urls_and_exceptions(self):
        self._seed_signal(symbol="AMD")
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("drop trigger signal_observations_no_update")
            connection.execute("drop trigger signal_forward_outcomes_no_update")
            connection.execute(
                "update signal_observations set source_sha = ?, provider = ?, data_source = ?, provenance_json = ?",
                (
                    "C:\\secret\\source",
                    "C:\\secret\\provider",
                    "Authorization: Bearer secret",
                    json.dumps({"url": "https://example.test?q=secret", "header": "Authorization"}),
                ),
            )
            connection.execute(
                "update signal_forward_outcomes set asset_bars_json = ?",
                ("https://example.test/Authorization?api_key=secret",),
            )
            connection.commit()
        self._write_benchmark(
            _benchmark_payload(
                {
                    "SPY": _benchmark_series(
                        dates=_dates(), provider="https://provider.test/api?key=secret"
                    )
                }
            )
        )

        serialized = json.dumps(self._evaluate(), ensure_ascii=False)
        for forbidden in (
            "C:\\secret",
            "secret",
            "Authorization",
            "Bearer",
            "https://",
            "exception",
            "api_key",
            "?key=",
        ):
            self.assertNotIn(forbidden.lower(), serialized.lower())


class PredictiveEvaluationDatabaseAndCliTests(PredictiveEvaluationFixture):
    def test_evaluator_opens_db_read_only_and_sets_query_only(self):
        self._seed_signal(symbol="AMD")
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=_dates())}))

        import advisor.predictive_evaluation as module

        real_connect = sqlite3.connect
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def capture(*args: object, **kwargs: object):
            calls.append((args, kwargs))
            return real_connect(*args, **kwargs)

        with patch.object(module.sqlite3, "connect", side_effect=capture):
            self._evaluate()
        self.assertTrue(calls)
        self.assertTrue(any("mode=ro" in str(args[0]) and kwargs.get("uri") is True for args, kwargs in calls))
        with closing(real_connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("pragma query_only").fetchone()[0], 0)

    def test_database_fingerprint_is_unchanged(self):
        self._seed_signal(symbol="AMD")
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=_dates())}))
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self._evaluate()
        after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_corrupt_database_is_nonzero_and_does_not_leak_exception_or_path(self):
        self.db_path.write_bytes(b"not a sqlite database")
        self._write_benchmark(_benchmark_payload({}))

        with self.assertRaises(PredictiveEvaluationError):
            self._evaluate()
        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = predictive_evaluation_main(
                [
                    "--db",
                    str(self.db_path),
                    "--benchmark-input-path",
                    str(self.benchmark_path),
                    "--output-path",
                    str(self.output_path),
                ]
            )
        self.assertNotEqual(exit_code, 0)
        self.assertNotIn(str(self.db_path), stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue())

    def test_cli_writes_compact_utf8_lf_artifact_without_paths(self):
        self._seed_signal(symbol="AMD")
        self._write_benchmark(_benchmark_payload({"SPY": _benchmark_series(dates=_dates())}))

        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = predictive_evaluation_main(
                [
                    "--db",
                    str(self.db_path),
                    "--benchmark-input-path",
                    str(self.benchmark_path),
                    "--output-path",
                    str(self.output_path),
                ]
            )
        self.assertEqual(exit_code, 0)
        output = self.output_path.read_bytes()
        self.assertTrue(output.endswith(b"\n"))
        self.assertEqual(output.count(b"\n"), 1)
        self.assertEqual(output.decode("utf-8"), json.dumps(json.loads(output), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        self.assertNotIn(str(self.db_path), output.decode("utf-8"))
        self.assertNotIn(str(self.benchmark_path), output.decode("utf-8"))
        self.assertNotIn("exception", output.decode("utf-8").lower())

    def test_cli_is_the_only_new_entrypoint_and_does_not_touch_advisor_cli(self):
        import advisor.cli as existing_cli

        source = inspect.getsource(existing_cli)
        self.assertNotIn("predictive_evaluation", source)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "advisor.predictive_evaluation",
                "--help",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--benchmark-input-path", result.stdout)


if __name__ == "__main__":
    unittest.main()
