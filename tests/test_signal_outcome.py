from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import ExitStack, closing, redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from io import StringIO
from inspect import getsource
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from advisor.cache import SQLiteCache
import advisor.cli as cli_module
from advisor.cli import main as advisor_main
from advisor.config import AdvisorConfig
from advisor.signal_observation import (
    SignalRunMetadata,
    build_signal_observation,
)
from advisor.signal_outcome import (
    EVALUATION_POLICY_VERSION,
    HORIZONS,
    OUTCOME_SEMANTICS,
    ForwardCandle,
    ForwardMarketSeries,
    SignalForwardOutcome,
    compute_outcome_hash,
    compute_outcome_id,
    evaluate_signal_observation,
    parse_forward_market_input,
    signal_market_date,
)
from tests.test_signal_observation import _decision, _snapshot


SOURCE_SHA = "c" * 40
SIGNAL_TIMESTAMP = "2026-08-09T02:30:00Z"


def _observation(
    symbol: str = "AMD",
    *,
    decision: str = "tradeable",
    asset_type: str = "stock",
    alternative_entry: float | None = 97,
    run_id: str = "outcome-run",
    signal_timestamp_utc: str = SIGNAL_TIMESTAMP,
):
    metadata = SignalRunMetadata(
        schema_version="1.0",
        source_sha=SOURCE_SHA,
        run_id=run_id,
        run_origin="local",
        report_date_brt="2026-08-08",
        report_type="main",
        signal_timestamp_utc=signal_timestamp_utc,
    )
    return build_signal_observation(
        _decision(
            symbol,
            decision=decision,
            alternative_entry=alternative_entry,
            asset_type=asset_type,
        ),
        _snapshot(symbol, asset_type=asset_type),
        metadata,
        stock_regime="neutral",
        crypto_regime="neutral",
    )


def _candle(
    day: str,
    *,
    open_price: float = 100,
    high: float | None = None,
    low: float | None = None,
    close: float = 100,
    volume: float = 1_000,
) -> dict[str, object]:
    high = max(open_price, close) + 1 if high is None else high
    low = min(open_price, close) - 1 if low is None else low
    return {
        "date": day,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _future_candles(
    count: int,
    *,
    start: str = "2026-08-09",
    close: float = 100,
    high: float | None = None,
    low: float | None = None,
) -> list[dict[str, object]]:
    first = date.fromisoformat(start)
    return [
        _candle(
            (first + timedelta(days=index)).isoformat(),
            open_price=close,
            high=high if high is not None else close + 1,
            low=low if low is not None else close - 1,
            close=close,
        )
        for index in range(count)
    ]


def _series(
    asset: str = "AMD",
    *,
    asset_type: str = "stock",
    candles: list[dict[str, object]] | None = None,
    provider: str = "fixture",
    price_basis: str = "unknown",
) -> ForwardMarketSeries:
    payload = {
        "schema_version": "1.0",
        "assets": {
            asset: {
                "asset_type": asset_type,
                "provider": provider,
                "price_basis": price_basis,
                "candles": candles if candles is not None else _future_candles(45),
            }
        },
    }
    return parse_forward_market_input(payload)[asset]


def _row_outcomes(cache: SQLiteCache) -> list[dict[str, object]]:
    return cache.load_signal_forward_outcomes()


class ForwardOutcomePureEvaluationTests(unittest.TestCase):
    def test_policy_has_only_fixed_forward_bar_horizons(self):
        self.assertEqual(EVALUATION_POLICY_VERSION, "1.0")
        self.assertEqual(HORIZONS, (5, 10, 20, 40))

    def test_stock_market_date_uses_persisted_us_eastern_timezone_and_dst(self):
        self.assertEqual(signal_market_date("2026-08-09T03:30:00Z", "America/New_York"), "2026-08-08")
        self.assertEqual(signal_market_date("2026-12-09T04:30:00Z", "America/New_York"), "2026-12-08")
        self.assertEqual(signal_market_date("2026-03-08T04:30:00Z", "America/New_York"), "2026-03-07")
        self.assertEqual(signal_market_date("2026-03-08T05:30:00Z", "America/New_York"), "2026-03-08")

    def test_crypto_market_date_uses_utc(self):
        self.assertEqual(signal_market_date("2026-08-09T00:30:00Z", "UTC"), "2026-08-09")

    def test_same_day_and_prior_candles_are_not_forward_eligible(self):
        candles = [
            _candle("2026-08-07", close=50),
            _candle("2026-08-08", close=50),
            *_future_candles(5),
        ]
        evaluation = evaluate_signal_observation(_observation(), _series(candles=candles))
        self.assertEqual([outcome.horizon_bars for outcome in evaluation.outcomes], [5])
        self.assertEqual(evaluation.outcomes[0].horizon_start_date, "2026-08-09")
        self.assertEqual(evaluation.outcomes[0].horizon_end_date, "2026-08-13")
        self.assertEqual(json.loads(evaluation.outcomes[0].asset_bars_json)[0]["date"], "2026-08-09")

    def test_forward_return_uses_the_nth_complete_bar_not_calendar_days(self):
        candles = [
            _candle("2026-08-09", close=101),
            _candle("2026-08-12", close=102),
            _candle("2026-08-20", close=103),
            _candle("2026-09-01", close=104),
            _candle("2026-10-15", close=105),
        ]
        evaluation = evaluate_signal_observation(_observation(), _series(candles=candles))
        self.assertEqual(len(evaluation.outcomes), 1)
        outcome = evaluation.outcomes[0]
        self.assertEqual(outcome.horizon_bars, 5)
        self.assertEqual(outcome.horizon_end_date, "2026-10-15")
        self.assertAlmostEqual(outcome.forward_return_pct, 0.05)

    def test_5_10_20_and_40_outcomes_use_exact_bar_counts(self):
        evaluation = evaluate_signal_observation(
            _observation(),
            _series(candles=_future_candles(40, close=100)),
        )
        self.assertEqual([outcome.horizon_bars for outcome in evaluation.outcomes], [5, 10, 20, 40])
        self.assertEqual([len(json.loads(outcome.asset_bars_json)) for outcome in evaluation.outcomes], [5, 10, 20, 40])

    def test_short_series_is_pending_without_partial_outcome(self):
        evaluation = evaluate_signal_observation(_observation(), _series(candles=_future_candles(3)))
        self.assertEqual(evaluation.outcomes, ())
        self.assertEqual(evaluation.pending_horizons, HORIZONS)

    def test_7_12_27_and_45_bars_complete_only_the_available_horizons(self):
        expected = {7: ((5,), (10, 20, 40)), 12: ((5, 10), (20, 40)), 27: ((5, 10, 20), (40,)), 45: ((5, 10, 20, 40), ())}
        for count, (horizons, pending) in expected.items():
            with self.subTest(count=count):
                evaluation = evaluate_signal_observation(_observation(), _series(candles=_future_candles(count)))
                self.assertEqual(tuple(outcome.horizon_bars for outcome in evaluation.outcomes), horizons)
                self.assertEqual(evaluation.pending_horizons, pending)

    def test_bars_after_the_40th_do_not_change_horizon_40(self):
        outcome_45 = evaluate_signal_observation(_observation(), _series(candles=_future_candles(45))).outcomes[-1]
        outcome_100 = evaluate_signal_observation(_observation(), _series(candles=_future_candles(100))).outcomes[-1]
        self.assertEqual(outcome_45.asset_bars_hash, outcome_100.asset_bars_hash)
        self.assertEqual(outcome_45.outcome_hash, outcome_100.outcome_hash)

    def test_reverse_input_order_is_canonicalized_to_the_same_bars_and_hashes(self):
        candles = _future_candles(45)
        forward = evaluate_signal_observation(_observation(), _series(candles=candles)).outcomes
        reverse = evaluate_signal_observation(_observation(), _series(candles=list(reversed(candles)))).outcomes
        self.assertEqual(
            [(outcome.horizon_bars, outcome.asset_bars_json, outcome.outcome_hash) for outcome in forward],
            [(outcome.horizon_bars, outcome.asset_bars_json, outcome.outcome_hash) for outcome in reverse],
        )

    def test_duplicate_candle_date_is_invalid_input(self):
        candles = _future_candles(5)
        candles.append(dict(candles[0]))
        with self.assertRaises(ValueError):
            _series(candles=candles)

    def test_candle_validation_rejects_nonfinite_or_invalid_ohlcv(self):
        invalid_cases = [
            {**_candle("2026-08-09"), "close": float("nan")},
            {**_candle("2026-08-09"), "volume": -1},
            {**_candle("2026-08-09"), "low": 102},
            {**_candle("2026-08-09"), "open": 102},
        ]
        for candle in invalid_cases:
            with self.subTest(candle=candle):
                with self.assertRaises(ValueError):
                    _series(candles=[candle])

    def test_mfe_and_mae_are_observational_excursions(self):
        candles = [
            _candle("2026-08-09", high=103, low=99, close=101),
            _candle("2026-08-10", high=108, low=97, close=102),
            *_future_candles(3, start="2026-08-11"),
        ]
        outcome = evaluate_signal_observation(_observation(), _series(candles=candles)).outcomes[0]
        self.assertAlmostEqual(outcome.mfe_pct, 0.08)
        self.assertAlmostEqual(outcome.mae_pct, -0.03)

    def test_barriers_scan_all_bars_and_preserve_first_touch_per_level(self):
        candles = _future_candles(20)
        candles[1] = _candle("2026-08-10", high=101, low=94, close=100)
        candles[2] = _candle("2026-08-11", high=110, low=99, close=100)
        candles[7] = _candle("2026-08-16", high=115, low=99, close=100)
        candles[14] = _candle("2026-08-23", high=101, low=93, close=100)
        outcome = evaluate_signal_observation(_observation(), _series(candles=candles)).outcomes[2]
        self.assertEqual(outcome.horizon_bars, 20)
        self.assertTrue(outcome.stop_touched)
        self.assertTrue(outcome.target_2r_touched)
        self.assertTrue(outcome.target_3r_touched)
        self.assertEqual((outcome.first_stop_bar, outcome.first_stop_date), (2, "2026-08-10"))
        self.assertEqual((outcome.first_target_2r_bar, outcome.first_target_2r_date), (3, "2026-08-11"))
        self.assertEqual((outcome.first_target_3r_bar, outcome.first_target_3r_date), (8, "2026-08-16"))

    def test_same_bar_stop_and_target_2r_is_ambiguous_without_order(self):
        candles = _future_candles(5)
        candles[1] = _candle("2026-08-10", high=110, low=94, close=100)
        outcome = evaluate_signal_observation(_observation(), _series(candles=candles)).outcomes[0]
        self.assertTrue(outcome.same_bar_stop_target_2r)
        self.assertEqual(outcome.first_stop_bar, 2)
        self.assertEqual(outcome.first_target_2r_bar, 2)

    def test_same_bar_stop_and_target_3r_is_ambiguous_without_order(self):
        candles = _future_candles(5)
        candles[1] = _candle("2026-08-10", high=115, low=94, close=100)
        outcome = evaluate_signal_observation(_observation(), _series(candles=candles)).outcomes[0]
        self.assertTrue(outcome.same_bar_stop_target_3r)
        self.assertEqual(outcome.first_stop_bar, 2)
        self.assertEqual(outcome.first_target_3r_bar, 2)

    def test_alternative_entry_is_only_a_threshold_observation(self):
        candles = _future_candles(5)
        candles[3] = _candle("2026-08-12", high=101, low=96, close=98)
        outcome = evaluate_signal_observation(_observation(), _series(candles=candles)).outcomes[0]
        self.assertTrue(outcome.alternative_entry_threshold_reached)
        self.assertEqual((outcome.first_alternative_entry_bar, outcome.first_alternative_entry_date), (4, "2026-08-12"))
        self.assertNotIn("filled", repr(outcome).lower())
        self.assertNotIn("actual_entry", repr(outcome).lower())

    def test_missing_alternative_entry_has_false_and_null_fields(self):
        outcome = evaluate_signal_observation(
            _observation(alternative_entry=None),
            _series(candles=_future_candles(5)),
        ).outcomes[0]
        self.assertFalse(outcome.alternative_entry_threshold_reached)
        self.assertIsNone(outcome.first_alternative_entry_bar)
        self.assertIsNone(outcome.first_alternative_entry_date)

    def test_avoid_and_blocked_keep_the_same_forward_return_direction(self):
        for decision in ("avoid", "blocked"):
            with self.subTest(decision=decision):
                outcome = evaluate_signal_observation(
                    _observation(decision=decision),
                    _series(candles=_future_candles(5, close=105)),
                ).outcomes[0]
                self.assertAlmostEqual(outcome.forward_return_pct, 0.05)

    def test_outcome_is_not_a_realized_trade_result(self):
        outcome = evaluate_signal_observation(_observation(), _series(candles=_future_candles(5))).outcomes[0]
        self.assertEqual(outcome.outcome_semantics, OUTCOME_SEMANTICS)
        for forbidden in (
            "realized_r",
            "realized_pnl",
            "fill",
            "actual_entry",
            "actual_exit",
            "win_loss",
            "profit_factor",
            "strategy_expectancy",
            "position_return",
        ):
            self.assertFalse(hasattr(outcome, forbidden), forbidden)

    def test_persisted_at_is_not_part_of_outcome_id_or_hash(self):
        outcome = evaluate_signal_observation(_observation(), _series(candles=_future_candles(5))).outcomes[0]
        changed_time = replace(outcome, persisted_at_utc="2026-08-09T03:00:00.000000Z")
        self.assertEqual(outcome.outcome_id, changed_time.outcome_id)
        self.assertEqual(compute_outcome_hash(outcome), compute_outcome_hash(changed_time))

    def test_provider_metadata_is_bounded_and_forbidden_values_are_not_persisted(self):
        outcome = evaluate_signal_observation(
            _observation(),
            _series(
                candles=_future_candles(5),
                provider="https://provider.test/?apikey=secret",
                price_basis="C:\\Users\\secret\\headers",
            ),
        ).outcomes[0]
        self.assertEqual(outcome.provider, "unknown")
        self.assertEqual(outcome.price_basis, "unknown")
        self.assertNotIn("https://", repr(outcome))
        self.assertNotIn("apikey", repr(outcome).lower())
        self.assertNotIn("C:\\Users", repr(outcome))


class ForwardOutcomePersistenceTests(unittest.TestCase):
    def _save_observation(self, cache: SQLiteCache, observation=None):
        observation = observation or _observation()
        self.assertEqual(cache.save_signal_observations([observation]).status, "written")
        return observation

    def test_schema_has_identity_policy_and_fixed_horizon_constraints(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outcomes.db"
            SQLiteCache(db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                columns = {row[1] for row in connection.execute("pragma table_info(signal_forward_outcomes)")}
                sql = connection.execute(
                    "select sql from sqlite_master where type = 'table' and name = 'signal_forward_outcomes'"
                ).fetchone()[0]
                triggers = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'trigger' and tbl_name = 'signal_forward_outcomes'"
                    )
                }
            self.assertIn("outcome_id", columns)
            self.assertIn("outcome_hash", columns)
            self.assertIn("asset_bars_json", columns)
            self.assertIn("evaluation_policy_version", columns)
            for forbidden in (
                "realized_r",
                "realized_pnl",
                "fill",
                "actual_entry",
                "actual_exit",
                "benchmark_return",
                "alpha",
                "result_final",
            ):
                self.assertNotIn(forbidden, columns)
            self.assertIn("horizon_bars in (5, 10, 20, 40)", sql.lower())
            self.assertEqual(
                triggers,
                {"signal_forward_outcomes_no_update", "signal_forward_outcomes_no_delete"},
            )

    def test_first_write_then_retry_is_duplicate_same_without_extra_row(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "outcomes.db")
            observation = self._save_observation(cache)
            outcomes = evaluate_signal_observation(observation, _series(candles=_future_candles(45))).outcomes
            first = cache.save_signal_forward_outcomes_for_signal(outcomes)
            second = cache.save_signal_forward_outcomes_for_signal(outcomes)
            self.assertEqual(first.status, "written")
            self.assertEqual(first.outcomes_written, 4)
            self.assertEqual(second.status, "duplicate_same")
            self.assertEqual(second.duplicate_same, 4)
            self.assertEqual(cache.count_signal_forward_outcomes(), 4)

    def test_divergent_history_is_conflict_and_original_outcome_remains(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outcomes.db"
            cache = SQLiteCache(db_path)
            observation = self._save_observation(cache)
            original = evaluate_signal_observation(observation, _series(candles=_future_candles(10))).outcomes
            self.assertEqual(cache.save_signal_forward_outcomes_for_signal(original).status, "written")
            changed = _future_candles(10)
            changed[9] = _candle("2026-08-18", high=140, low=99, close=130)
            divergent = evaluate_signal_observation(observation, _series(candles=changed)).outcomes
            result = cache.save_signal_forward_outcomes_for_signal(divergent)
            self.assertEqual(result.status, "conflict")
            saved = _row_outcomes(cache)
            self.assertEqual(len(saved), 2)
            self.assertEqual(
                {row["horizon_bars"]: row["outcome_hash"] for row in saved},
                {outcome.horizon_bars: outcome.outcome_hash for outcome in original},
            )

    def test_conflict_in_one_horizon_rolls_back_new_horizons_for_that_signal(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "outcomes.db")
            observation = self._save_observation(cache)
            first_five = evaluate_signal_observation(observation, _series(candles=_future_candles(5))).outcomes
            self.assertEqual(cache.save_signal_forward_outcomes_for_signal(first_five).status, "written")
            changed = _future_candles(10)
            changed[0] = _candle("2026-08-09", high=101, low=90, close=100)
            retry = evaluate_signal_observation(observation, _series(candles=changed)).outcomes
            result = cache.save_signal_forward_outcomes_for_signal(retry)
            self.assertEqual(result.status, "conflict")
            self.assertEqual(cache.count_signal_forward_outcomes(), 1)
            self.assertEqual(_row_outcomes(cache)[0]["horizon_bars"], 5)

    def test_different_signals_are_independent_when_one_conflicts(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "outcomes.db")
            amd = self._save_observation(cache, _observation("AMD"))
            btc = self._save_observation(cache, _observation("BTC", asset_type="crypto", run_id="btc-run"))
            amd_original = evaluate_signal_observation(amd, _series(candles=_future_candles(5))).outcomes
            self.assertEqual(cache.save_signal_forward_outcomes_for_signal(amd_original).status, "written")
            changed_amd = _future_candles(5)
            changed_amd[0] = _candle("2026-08-09", high=101, low=90, close=100)
            conflict = cache.save_signal_forward_outcomes_for_signal(
                evaluate_signal_observation(amd, _series(candles=changed_amd)).outcomes
            )
            self.assertEqual(conflict.status, "conflict")
            btc_result = cache.save_signal_forward_outcomes_for_signal(
                evaluate_signal_observation(
                    btc,
                    _series("BTC", asset_type="crypto", candles=_future_candles(5, start="2026-08-10")),
                ).outcomes
            )
            self.assertEqual(btc_result.status, "written")
            self.assertEqual(cache.count_signal_forward_outcomes(), 2)

    def test_update_and_delete_are_blocked_by_append_only_triggers(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outcomes.db"
            cache = SQLiteCache(db_path)
            observation = self._save_observation(cache)
            outcomes = evaluate_signal_observation(observation, _series(candles=_future_candles(5))).outcomes
            cache.save_signal_forward_outcomes_for_signal(outcomes)
            with closing(sqlite3.connect(db_path)) as connection:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("update signal_forward_outcomes set forward_return_pct = 99")
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("delete from signal_forward_outcomes")
            self.assertEqual(cache.count_signal_forward_outcomes(), 1)

    def test_missing_canonical_signal_is_unavailable_and_cannot_create_outcome(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "outcomes.db")
            outcome = evaluate_signal_observation(_observation(), _series(candles=_future_candles(5))).outcomes[0]
            result = cache.save_signal_forward_outcomes_for_signal((outcome,))
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(cache.count_signal_forward_outcomes(), 0)

    def test_existing_signal_with_divergent_observation_hash_is_unavailable(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "outcomes.db")
            observation = self._save_observation(cache)
            outcome = evaluate_signal_observation(observation, _series(candles=_future_candles(5))).outcomes[0]
            divergent = replace(
                outcome,
                observation_hash="d" * 64,
                outcome_id=compute_outcome_id(
                    schema_version=outcome.schema_version,
                    evaluation_policy_version=outcome.evaluation_policy_version,
                    signal_id=outcome.signal_id,
                    observation_hash="d" * 64,
                    horizon_bars=outcome.horizon_bars,
                ),
            )
            divergent = replace(divergent, outcome_hash=compute_outcome_hash(divergent))
            result = cache.save_signal_forward_outcomes_for_signal((divergent,))
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(cache.count_signal_forward_outcomes(), 0)

    def test_outcome_storage_does_not_modify_signal_observations(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "outcomes.db"
            cache = SQLiteCache(db_path)
            observation = self._save_observation(cache)
            before = cache.load_signal_observations()
            cache.save_signal_forward_outcomes_for_signal(
                evaluate_signal_observation(observation, _series(candles=_future_candles(5))).outcomes
            )
            after = cache.load_signal_observations()
            self.assertEqual(before, after)

    def test_forward_persistence_has_no_replace_or_upsert_write(self):
        source = getsource(SQLiteCache.save_signal_forward_outcomes_for_signal).upper()
        self.assertNotIn("INSERT OR REPLACE", source)
        self.assertNotIn("ON CONFLICT DO UPDATE", source)


class ForwardOutcomeCliTests(unittest.TestCase):
    def test_outcomes_namespace_evaluates_local_json_and_prints_sanitized_counts(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "outcomes.db"
            cache = SQLiteCache(db_path)
            observation = _observation()
            self.assertEqual(cache.save_signal_observations([observation]).status, "written")
            input_path = root / "market.json"
            input_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "assets": {
                            "AMD": {
                                "asset_type": "stock",
                                "provider": "fixture",
                                "price_basis": "unknown",
                                "candles": _future_candles(45),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            with patch("advisor.cli.LiveDataLoader", side_effect=AssertionError("network access")):
                with redirect_stdout(stdout):
                    code = advisor_main(
                        [
                            "outcomes",
                            "evaluate",
                            "--input-path",
                            str(input_path),
                            "--db",
                            str(db_path),
                        ]
                    )
            self.assertEqual(code, 0)
            summary = json.loads(stdout.getvalue())
            self.assertEqual(
                summary,
                {
                    "observations_considered": 1,
                    "signals_conflict": 0,
                    "signals_duplicate_same": 0,
                    "signals_pending": 0,
                    "signals_unavailable": 0,
                    "signals_written": 1,
                    "outcomes_written": 4,
                },
            )
            self.assertEqual(cache.count_signal_forward_outcomes(), 4)
            self.assertNotIn(str(input_path), stdout.getvalue())
            self.assertNotIn(str(input_path), json.dumps(_row_outcomes(cache), sort_keys=True))

    def test_invalid_input_has_nonzero_exit_without_path_or_exception(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "invalid.json"
            input_path.write_text('{"schema_version":"9.9","assets":{}}', encoding="utf-8")
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = advisor_main(
                    [
                        "outcomes",
                        "evaluate",
                        "--input-path",
                        str(input_path),
                        "--db",
                        str(root / "outcomes.db"),
                    ]
                )
            self.assertNotEqual(code, 0)
            self.assertNotIn(str(input_path), stdout.getvalue())
            self.assertNotIn("Traceback", stdout.getvalue())
            self.assertNotIn("exception", stdout.getvalue().lower())

    def test_cli_completion_behavior_writes_only_completed_new_horizons(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "outcomes.db"
            cache = SQLiteCache(db_path)
            observation = _observation()
            self.assertEqual(cache.save_signal_observations([observation]).status, "written")
            input_path = root / "market.json"

            expected = {
                7: ({"signals_written": 1, "signals_duplicate_same": 0, "signals_pending": 1, "outcomes_written": 1}, 1),
                12: ({"signals_written": 1, "signals_duplicate_same": 0, "signals_pending": 1, "outcomes_written": 1}, 2),
                43: ({"signals_written": 1, "signals_duplicate_same": 0, "signals_pending": 0, "outcomes_written": 2}, 4),
                100: ({"signals_written": 0, "signals_duplicate_same": 1, "signals_pending": 0, "outcomes_written": 0}, 4),
            }
            for count, (expected_counts, expected_rows) in expected.items():
                input_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "assets": {
                                "AMD": {
                                    "asset_type": "stock",
                                    "provider": "fixture",
                                    "price_basis": "unknown",
                                    "candles": _future_candles(count),
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                stdout = StringIO()
                with redirect_stdout(stdout):
                    code = advisor_main(
                        [
                            "outcomes",
                            "evaluate",
                            "--input-path",
                            str(input_path),
                            "--db",
                            str(db_path),
                        ]
                    )
                self.assertEqual(code, 0)
                summary = json.loads(stdout.getvalue())
                for name, value in expected_counts.items():
                    self.assertEqual(summary[name], value, (count, name))
                self.assertEqual(summary["observations_considered"], 1)
                self.assertEqual(summary["signals_conflict"], 0)
                self.assertEqual(summary["signals_unavailable"], 0)
                self.assertEqual(cache.count_signal_forward_outcomes(), expected_rows)

    def test_empty_or_full_outcome_table_does_not_change_main_or_close_authority(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty_db = root / "empty.db"
            full_db = root / "full.db"
            full_cache = SQLiteCache(full_db)
            full_observation = _observation("OUTCOME_ONLY", run_id="outcome-only-run")
            self.assertEqual(full_cache.save_signal_observations([full_observation]).status, "written")
            self.assertEqual(
                full_cache.save_signal_forward_outcomes_for_signal(
                    evaluate_signal_observation(
                        full_observation,
                        _series("OUTCOME_ONLY", candles=_future_candles(5)),
                    ).outcomes
                ).status,
                "written",
            )

            real_markdown = cli_module.render_markdown_report
            real_analyst = cli_module.render_analyst_review_input
            real_assign = cli_module._assign_universe_origins

            def fixed_markdown(decisions, **kwargs):
                kwargs["generated_at"] = "2026-08-09T02:30:00+00:00"
                return real_markdown(decisions, **kwargs)

            def fixed_analyst(decisions, **kwargs):
                kwargs["generated_at"] = "2026-08-08T23:30:00-03:00"
                return real_analyst(decisions, **kwargs)

            def run_report(db_path: Path, output_dir: Path, report_type: str):
                captured = []

                def assign(decisions, config):
                    assigned = real_assign(decisions, config)
                    captured.extend(assigned)
                    return assigned

                def fixed_metadata(**kwargs):
                    return SignalRunMetadata(
                        schema_version="1.0",
                        source_sha=SOURCE_SHA,
                        run_id="authority-run",
                        run_origin="local",
                        report_date_brt="2026-08-08",
                        report_type=str(kwargs["report_type"]),
                        signal_timestamp_utc=SIGNAL_TIMESTAMP,
                    )

                with ExitStack() as stack:
                    stack.enter_context(patch("advisor.cli.AdvisorConfig.default", side_effect=lambda: AdvisorConfig()))
                    stack.enter_context(patch("advisor.cli._resolve_source_sha", return_value=SOURCE_SHA))
                    stack.enter_context(patch("advisor.cli.create_run_metadata", side_effect=fixed_metadata))
                    stack.enter_context(patch.object(cli_module, "render_markdown_report", side_effect=fixed_markdown))
                    stack.enter_context(patch.object(cli_module, "render_analyst_review_input", side_effect=fixed_analyst))
                    stack.enter_context(patch.object(cli_module, "_assign_universe_origins", side_effect=assign))
                    stdout = StringIO()
                    with redirect_stdout(stdout):
                        code = advisor_main(
                            [
                                "report",
                                report_type,
                                "--db",
                                str(db_path),
                                "--output-dir",
                                str(output_dir),
                            ]
                        )
                fingerprint = [
                    (
                        decision.symbol,
                        decision.decision,
                        decision.investment_quality_score,
                        decision.swing_trade_score,
                        decision.decision_confidence_score,
                        decision.data_quality_score,
                        tuple(decision.reason_codes),
                        repr(decision.risk_plan),
                    )
                    for decision in captured
                ]
                return code, fingerprint, (output_dir / "advisor-report.md").read_bytes(), (output_dir / "analyst-review-input.md").read_bytes()

            for report_type in ("main", "close"):
                with self.subTest(report_type=report_type):
                    empty = run_report(empty_db, root / f"empty-{report_type}", report_type)
                    full = run_report(full_db, root / f"full-{report_type}", report_type)
                    self.assertEqual(empty[0], 0)
                    self.assertEqual(full[0], 0)
                    self.assertEqual(empty[1:], full[1:])

    def test_report_main_and_close_do_not_execute_outcome_engine(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("advisor.cli.evaluate_outcomes_from_json", side_effect=AssertionError("automatic outcome evaluation")),
                patch("advisor.cli.AdvisorConfig.default", return_value=AdvisorConfig()),
                patch("advisor.cli._resolve_source_sha", return_value=None),
            ):
                self.assertEqual(
                    advisor_main(
                        [
                            "report",
                            "main",
                            "--db",
                            str(root / "main.db"),
                            "--output-dir",
                            str(root / "main"),
                        ]
                    ),
                    0,
                )
                self.assertEqual(SQLiteCache(root / "main.db").count_signal_forward_outcomes(), 0)
                self.assertEqual(
                    advisor_main(
                        [
                            "report",
                            "close",
                            "--db",
                            str(root / "close.db"),
                            "--output-dir",
                            str(root / "close"),
                        ]
                    ),
                    0,
                )
                self.assertEqual(SQLiteCache(root / "close.db").count_signal_forward_outcomes(), 0)


if __name__ == "__main__":
    unittest.main()
