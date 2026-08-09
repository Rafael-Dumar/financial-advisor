from __future__ import annotations

import json
import os
import sqlite3
import unittest
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from inspect import getsource
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from advisor.cache import SQLiteCache
from advisor.models import (
    AssetDecision,
    AssetSnapshot,
    BacktestStats,
    Candle,
    DataFetchMetadata,
    Fundamentals,
    RiskPlan,
)
from advisor.signal_observation import (
    SignalRunMetadata,
    build_signal_observation,
    canonical_json_bytes,
    compute_observation_hash,
    compute_signal_id,
    create_run_metadata,
    evaluation_role_for_decision,
    validate_source_sha,
)


SOURCE_SHA = "a" * 40
SIGNAL_TIMESTAMP = "2026-08-09T02:30:00Z"


def _run_metadata(*, report_type: str = "main", run_id: str = "local-run") -> SignalRunMetadata:
    return SignalRunMetadata(
        schema_version="1.0",
        source_sha=SOURCE_SHA,
        run_id=run_id,
        run_origin="local",
        report_date_brt="2026-08-08",
        report_type=report_type,
        signal_timestamp_utc=SIGNAL_TIMESTAMP,
    )


def _snapshot(symbol: str = "AMD", *, asset_type: str = "stock") -> AssetSnapshot:
    return AssetSnapshot(
        symbol=symbol,
        asset_type=asset_type,
        theme="semiconductors" if asset_type == "stock" else "crypto",
        candles=[Candle("2026-08-08", 100, 101, 99, 100, 1_000_000)],
        fundamentals=Fundamentals(
            pe=25,
            peg=1.5,
            historical_pe=28,
            revenue_growth=0.2,
            eps_growth=0.15,
            margin_trend=0.04,
            free_cash_flow_positive=True,
            market_cap=100_000_000_000,
            average_volume=5_000_000,
        ),
        data_source="fmp",
        data_timestamp="2026-08-08",
        cache_age_seconds=12,
        data_fetch_metadata=DataFetchMetadata(
            provider="fmp",
            endpoint="prices",
            fetched_at="2026-08-09T02:30:01Z",
            cache_fetched_at="2026-08-09T02:29:50Z",
            source_timestamp="2026-08-08",
            cache_age_seconds=12,
            source_age_seconds=90_000,
            is_fresh=True,
            cache_hit=True,
            fallback_used=False,
            granularity="daily",
            market_data_kind="eod_candle",
        ),
        quote_status="available",
        quote_price=100.2,
        quote_timestamp="2026-08-09T02:29:59Z",
        quote_source="fmp",
        quote_age_seconds=2,
        quote_is_intraday=True,
    )


def _decision(
    symbol: str = "AMD",
    *,
    decision: str = "tradeable",
    alternative_entry: float | None = 97,
    confidence: int = 82,
    asset_type: str = "stock",
) -> AssetDecision:
    return AssetDecision(
        symbol=symbol,
        asset_type=asset_type,
        decision=decision,
        investment_quality_score=88,
        swing_trade_score=84,
        risk_plan=RiskPlan(
            entry=100,
            stop=95,
            target_2r=110,
            target_3r=115,
            per_unit_risk=5,
            risk_amount=250,
            risk_fraction=0.005,
            max_position_units=50,
            max_position_value=5_000,
            risk_reward_2r="2.00:1",
            alerts=[],
            position_size_display="50",
        ),
        alerts=[],
        limitations=[],
        thesis="technical setup",
        metrics_summary=["RSI: 55"],
        ideal_entry=100,
        alternative_entry=alternative_entry,
        hold_suggestion="1-8 semanas",
        backtest_stats=BacktestStats(
            sample_size=120,
            win_rate_2r=0.6,
            win_rate_3r=0.35,
            expected_value_r=0.4,
            setup_quality="high",
        ),
        sample_quality="high",
        reason_codes=["setup_confirmed"],
        data_quality="ok",
        missing_data_severity="low",
        data_source="fmp",
        data_timestamp="2026-08-08",
        cache_age_seconds=12,
        bucket=decision,
        market_session="regular",
        last_price_timestamp="2026-08-08",
        provider="fmp",
        is_stale=False,
        stale_reason="not_stale",
        event_check_status="verified",
        news_status="not_configured",
        macro_regime="neutral",
        macro_status="not_collected",
        thesis_status="validated",
        data_quality_score=91,
        decision_confidence_score=confidence,
        relative_strength_vs_spy=0.03,
        relative_strength_vs_qqq=0.02,
        relative_strength_vs_sector=0.01,
        sector_benchmark="SMH",
        universe_origin="primary_watchlist",
    )


class SignalObservationContractTests(unittest.TestCase):
    def test_source_sha_requires_lowercase_40_or_64_hex(self):
        self.assertEqual(validate_source_sha(SOURCE_SHA), SOURCE_SHA)
        self.assertEqual(validate_source_sha("b" * 64), "b" * 64)
        self.assertIsNone(validate_source_sha("A" * 40))
        self.assertIsNone(validate_source_sha("unknown"))
        self.assertIsNone(validate_source_sha("a" * 39))

    def test_signal_id_is_deterministic_and_path_independent(self):
        identity = {
            "schema_version": "1.0",
            "source_sha": SOURCE_SHA,
            "run_id": "local-run",
            "report_type": "main",
            "symbol": "AMD",
        }
        reordered = dict(reversed(list(identity.items())))
        self.assertEqual(compute_signal_id(identity), compute_signal_id(reordered))
        self.assertNotEqual(compute_signal_id(identity), compute_signal_id({**identity, "symbol": "NVDA"}))

    def test_signal_observation_uses_honest_entry_semantics_and_roles(self):
        observation = build_signal_observation(
            _decision(alternative_entry=97),
            _snapshot(),
            _run_metadata(),
            stock_regime="bull",
            crypto_regime="neutral",
        )
        self.assertEqual(observation.entry_semantics, "reference_close_not_fill")
        self.assertEqual(observation.alternative_entry_semantics, "conditional_untracked")
        self.assertEqual(observation.evaluation_role, "trade_candidate")
        self.assertEqual(observation.market_timezone, "America/New_York")
        self.assertEqual(observation.stock_regime, "bull")
        self.assertEqual(observation.crypto_regime, "neutral")
        self.assertEqual(observation.decision_confidence_score, 82)

    def test_missing_alternative_entry_is_not_present(self):
        observation = build_signal_observation(
            _decision(alternative_entry=None),
            _snapshot(),
            _run_metadata(),
            stock_regime="neutral",
            crypto_regime="neutral",
        )
        self.assertEqual(observation.alternative_entry_semantics, "not_present")

    def test_all_decision_labels_map_to_observational_roles(self):
        expected = {
            "tradeable": "trade_candidate",
            "watch_buy": "conditional_candidate",
            "technical_unvalidated": "observational_candidate",
            "wait": "observational_wait",
            "avoid": "observational_avoid",
            "blocked": "observational_blocked",
        }
        for decision, role in expected.items():
            self.assertEqual(evaluation_role_for_decision(decision), role)

    def test_all_decision_classes_are_recorded_without_becoming_fills_or_outcomes(self):
        expected = {
            "tradeable": "trade_candidate",
            "watch_buy": "conditional_candidate",
            "technical_unvalidated": "observational_candidate",
            "wait": "observational_wait",
            "avoid": "observational_avoid",
            "blocked": "observational_blocked",
        }
        for index, (decision, role) in enumerate(expected.items()):
            observation = build_signal_observation(
                _decision(f"ASSET{index}", decision=decision),
                _snapshot(f"ASSET{index}"),
                _run_metadata(),
                stock_regime="neutral",
                crypto_regime="neutral",
            )
            self.assertEqual(observation.evaluation_role, role)
            self.assertEqual(observation.entry_semantics, "reference_close_not_fill")
            self.assertNotIn("filled", repr(observation))
            self.assertFalse(hasattr(observation, "realized_r"))

    def test_crypto_observation_uses_utc_market_timezone(self):
        observation = build_signal_observation(
            _decision("BTC", asset_type="crypto"),
            _snapshot("BTC", asset_type="crypto"),
            _run_metadata(),
            stock_regime="neutral",
            crypto_regime="bull",
        )
        self.assertEqual(observation.market_timezone, "UTC")

    def test_observation_is_frozen_and_has_no_outcome_fields(self):
        observation = build_signal_observation(
            _decision(),
            _snapshot(),
            _run_metadata(),
            stock_regime="neutral",
            crypto_regime="neutral",
        )
        with self.assertRaises(FrozenInstanceError):
            observation.decision_label = "avoid"  # type: ignore[misc]
        for forbidden in (
            "return_5d",
            "return_10d",
            "return_20d",
            "return_40d",
            "hit_stop",
            "hit_2r",
            "hit_3r",
            "realized_r",
            "mfe",
            "mae",
            "exit_price",
        ):
            self.assertFalse(hasattr(observation, forbidden), forbidden)

    def test_observation_hash_excludes_persisted_at_but_covers_content(self):
        observation = build_signal_observation(
            _decision(),
            _snapshot(),
            _run_metadata(),
            stock_regime="neutral",
            crypto_regime="neutral",
            persisted_at_utc="2026-08-09T02:30:02Z",
        )
        same_content = replace(observation, persisted_at_utc="2026-08-09T02:31:02Z")
        same_date_clock = replace(observation, signal_timestamp_utc="2026-08-09T02:31:02Z")
        changed = replace(observation, decision_confidence_score=83)
        self.assertEqual(compute_observation_hash(observation), compute_observation_hash(same_content))
        self.assertEqual(compute_observation_hash(observation), compute_observation_hash(same_date_clock))
        self.assertNotEqual(compute_observation_hash(observation), compute_observation_hash(changed))

    def test_provenance_is_allowlisted_and_deterministic(self):
        observation = build_signal_observation(
            _decision(),
            _snapshot(),
            _run_metadata(),
            stock_regime="neutral",
            crypto_regime="neutral",
        )
        provenance = json.loads(observation.provenance_json)
        self.assertEqual(list(provenance), sorted(provenance))
        serialized = observation.provenance_json
        for forbidden in (
            "SECRET_API_KEY_MUST_NOT_PERSIST",
            "Authorization",
            "Bearer abc",
            "C:\\Users\\",
            "apikey=SECRET",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("provider", provenance)
        self.assertNotIn("endpoint", provenance)

    def test_provider_metadata_markers_are_not_serialized(self):
        poisoned_metadata = replace(
            _snapshot().data_fetch_metadata,
            endpoint="https://provider.test/path?apikey=SECRET",
            fallback_from="C:\\Users\\Administrator\\secret.json",
        )
        poisoned_snapshot = replace(_snapshot(), data_fetch_metadata=poisoned_metadata)
        observation = build_signal_observation(
            _decision(),
            poisoned_snapshot,
            _run_metadata(),
            stock_regime="neutral",
            crypto_regime="neutral",
        )
        serialized = json.dumps(observation.__dict__, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("https://provider.test/path?apikey=SECRET", serialized)
        self.assertNotIn("C:\\Users\\Administrator\\secret.json", serialized)
        self.assertNotIn("endpoint", observation.provenance_json)

    def test_canonical_json_rejects_non_finite_values(self):
        with self.assertRaises(ValueError):
            canonical_json_bytes({"value": float("nan")})

    def test_run_id_uses_github_identity_or_one_local_uuid_per_invocation(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "987654"}, clear=True):
            github = create_run_metadata(
                source_sha=SOURCE_SHA,
                report_type="main",
                signal_timestamp_utc=SIGNAL_TIMESTAMP,
            )
        self.assertEqual(github.run_id, "987654")
        self.assertEqual(github.run_origin, "github")
        with patch.dict(os.environ, {}, clear=True):
            local_a = create_run_metadata(
                source_sha=SOURCE_SHA,
                report_type="main",
                signal_timestamp_utc=SIGNAL_TIMESTAMP,
            )
            local_b = create_run_metadata(
                source_sha=SOURCE_SHA,
                report_type="main",
                signal_timestamp_utc=SIGNAL_TIMESTAMP,
            )
        self.assertTrue(local_a.run_id.startswith("local-"))
        self.assertEqual(local_a.run_origin, "local")
        self.assertNotEqual(local_a.run_id, local_b.run_id)


class SignalObservationPersistenceTests(unittest.TestCase):
    def _observation(self, symbol: str = "AMD", *, run_id: str = "local-run", report_type: str = "main"):
        return build_signal_observation(
            _decision(symbol),
            _snapshot(symbol),
            _run_metadata(report_type=report_type, run_id=run_id),
            stock_regime="neutral",
            crypto_regime="neutral",
        )

    def test_insert_then_identical_retry_is_one_row_and_duplicate_same(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "ledger.db")
            observation = self._observation()
            first = cache.save_signal_observations([observation])
            second = cache.save_signal_observations([observation])
            self.assertEqual(first.status, "written")
            self.assertEqual(second.status, "duplicate_same")
            self.assertEqual(cache.count_signal_observations(), 1)

    def test_divergent_retry_is_conflict_and_original_bytes_remain(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "ledger.db")
            original = self._observation()
            cache.save_signal_observations([original])
            divergent = replace(original, decision_confidence_score=99)
            result = cache.save_signal_observations([divergent])
            self.assertEqual(result.status, "conflict")
            self.assertEqual(cache.count_signal_observations(), 1)
            saved = cache.load_signal_observations()[0]
            self.assertEqual(saved["observation_hash"], original.observation_hash)
            self.assertEqual(saved["decision_confidence_score"], 82)

    def test_new_run_and_report_type_create_distinct_observations(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "ledger.db")
            cache.save_signal_observations(
                [self._observation(run_id="run-a"), self._observation(run_id="run-b"), self._observation(report_type="close")]
            )
            self.assertEqual(cache.count_signal_observations(), 3)

    def test_batch_conflict_rolls_back_new_rows(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "ledger.db")
            original = self._observation()
            cache.save_signal_observations([original])
            new_observation = self._observation("NVDA")
            divergent = replace(original, decision_confidence_score=99)
            result = cache.save_signal_observations([new_observation, divergent])
            self.assertEqual(result.status, "conflict")
            self.assertEqual(cache.count_signal_observations(), 1)

    def test_update_and_delete_are_blocked_by_triggers(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ledger.db"
            cache = SQLiteCache(db_path)
            cache.save_signal_observations([self._observation()])
            with closing(sqlite3.connect(db_path)) as connection:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "update signal_observations set decision_label = 'avoid' where asset = 'AMD'"
                    )
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("delete from signal_observations where asset = 'AMD'")
            self.assertEqual(cache.count_signal_observations(), 1)

    def test_schema_has_identity_and_no_outcome_columns(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ledger.db"
            cache = SQLiteCache(db_path)
            cache.save_signal_observations([self._observation()])
            with closing(sqlite3.connect(db_path)) as connection:
                columns = {row[1] for row in connection.execute("pragma table_info(signal_observations)")}
                indexes = list(connection.execute("pragma index_list(signal_observations)"))
                logical_unique_indexes = [
                    [row[2] for row in connection.execute(f"pragma index_info('{index[1]}')")]
                    for index in indexes
                    if index[2]
                ]
            self.assertIn("signal_id", columns)
            self.assertIn("observation_hash", columns)
            self.assertNotIn("return_5d", columns)
            self.assertNotIn("hit_stop", columns)
            self.assertTrue(any(row[2] for row in indexes))
            self.assertIn(["source_sha", "run_id", "report_type", "asset"], logical_unique_indexes)

    def test_github_retry_cross_date_is_conflict_not_second_observation(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "ledger.db")
            run_a = replace(
                _run_metadata(run_id="123456"),
                run_origin="github",
                signal_timestamp_utc="2026-08-10T02:59:59Z",
                report_date_brt="2026-08-09",
            )
            run_b = replace(
                run_a,
                signal_timestamp_utc="2026-08-10T03:00:01Z",
                report_date_brt="2026-08-10",
            )
            observation_a = build_signal_observation(
                _decision(),
                _snapshot(),
                run_a,
                stock_regime="neutral",
                crypto_regime="neutral",
            )
            observation_b = build_signal_observation(
                _decision(),
                _snapshot(),
                run_b,
                stock_regime="neutral",
                crypto_regime="neutral",
            )
            first = cache.save_signal_observations([observation_a])
            second = cache.save_signal_observations([observation_b])
            self.assertEqual(observation_a.signal_id, observation_b.signal_id)
            self.assertNotEqual(observation_a.observation_hash, observation_b.observation_hash)
            self.assertEqual(first.status, "written")
            self.assertEqual(second.status, "conflict")
            self.assertEqual(cache.count_signal_observations(), 1)
            saved = cache.load_signal_observations()[0]
            self.assertEqual(saved["report_date_brt"], "2026-08-09")
            self.assertEqual(saved["observation_hash"], observation_a.observation_hash)

    def test_github_retry_same_date_clock_change_is_duplicate_same(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "ledger.db")
            run_a = replace(
                _run_metadata(run_id="123456"),
                run_origin="github",
                signal_timestamp_utc="2026-08-09T02:00:00Z",
                report_date_brt="2026-08-08",
            )
            run_b = replace(
                run_a,
                signal_timestamp_utc="2026-08-09T02:30:00Z",
            )
            observation_a = build_signal_observation(
                _decision(), _snapshot(), run_a, stock_regime="neutral", crypto_regime="neutral"
            )
            observation_b = build_signal_observation(
                _decision(), _snapshot(), run_b, stock_regime="neutral", crypto_regime="neutral"
            )
            self.assertEqual(observation_a.signal_id, observation_b.signal_id)
            self.assertEqual(observation_a.observation_hash, observation_b.observation_hash)
            self.assertEqual(cache.save_signal_observations([observation_a]).status, "written")
            self.assertEqual(cache.save_signal_observations([observation_b]).status, "duplicate_same")
            self.assertEqual(cache.count_signal_observations(), 1)

    def test_different_local_invocation_uuids_create_new_observations(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            cache = SQLiteCache(Path(tmp) / "ledger.db")
            run_a = create_run_metadata(
                source_sha=SOURCE_SHA,
                report_type="main",
                signal_timestamp_utc=SIGNAL_TIMESTAMP,
            )
            run_b = create_run_metadata(
                source_sha=SOURCE_SHA,
                report_type="main",
                signal_timestamp_utc=SIGNAL_TIMESTAMP,
            )
            observation_a = build_signal_observation(
                _decision(), _snapshot(), run_a, stock_regime="neutral", crypto_regime="neutral"
            )
            observation_b = build_signal_observation(
                _decision(), _snapshot(), run_b, stock_regime="neutral", crypto_regime="neutral"
            )
            self.assertNotEqual(observation_a.signal_id, observation_b.signal_id)
            self.assertEqual(cache.save_signal_observations([observation_a, observation_b]).status, "written")
            self.assertEqual(cache.count_signal_observations(), 2)

    def test_storage_failure_rolls_back_the_entire_batch(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ledger.db"
            cache = SQLiteCache(db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    create trigger fail_second_observation
                    before insert on signal_observations
                    when new.asset = 'NVDA'
                    begin
                        select raise(abort, 'synthetic_storage_failure');
                    end
                    """
                )
            result = cache.save_signal_observations(
                [self._observation("AMD"), self._observation("NVDA")]
            )
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(cache.count_signal_observations(), 0)

    def test_persistence_path_has_no_replace_or_update_conflict_write(self):
        source = getsource(SQLiteCache.save_signal_observations).upper()
        self.assertNotIn("INSERT OR REPLACE", source)
        self.assertNotIn("UPDATE ON CONFLICT", source)

    def test_batch_order_does_not_change_each_identity_or_hash(self):
        observations = [self._observation("AMD"), self._observation("NVDA")]
        with TemporaryDirectory() as tmp:
            first_cache = SQLiteCache(Path(tmp) / "first.db")
            second_cache = SQLiteCache(Path(tmp) / "second.db")
            first_cache.save_signal_observations(observations)
            second_cache.save_signal_observations(list(reversed(observations)))
            first = {
                row["asset"]: (row["signal_id"], row["observation_hash"])
                for row in first_cache.load_signal_observations()
            }
            second = {
                row["asset"]: (row["signal_id"], row["observation_hash"])
                for row in second_cache.load_signal_observations()
            }
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
