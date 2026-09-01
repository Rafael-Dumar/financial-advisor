import os
import argparse
import hashlib
import subprocess
import tempfile
import threading
import time
import unittest
import zlib
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import get_args
from types import SimpleNamespace
from unittest.mock import patch

from advisor.evidence_schema import (
    ArchiveStatus,
    CorporateActionReasonCode,
    CryptoPolicy,
    HorizonStatus,
    SignalBasisStatus,
    SplitPolicy,
    build_observation_sidecar,
    canonical_content_sha256,
    canonical_json_bytes,
    classify_idempotency,
    decompress_single_member_gzip,
    deterministic_gzip,
    payload_sha256,
    resolve_signal_price_basis_status,
    strict_json_loads_bytes,
    validate_canonical_envelope,
)
from advisor.evidence_archive import (
    EvidenceArchive,
    bootstrap_evidence_branch,
    oldest_canonical_provider_by_symbol,
)
from advisor.data_sources import (
    BinanceSource,
    FmpSource,
    HyperliquidSource,
    is_qualified_raw_ohlcv_claim,
    qualified_binance_klines_basis_claim,
    qualified_fmp_full_price_basis_claim,
    qualified_hyperliquid_candle_snapshot_basis_claim,
)
from advisor.config import AdvisorConfig
from advisor.data_pipeline import crypto_snapshot_from_payloads, stock_snapshot_from_payloads
from advisor.live_loader import LiveDataLoader
from advisor.models import (
    AssetDecision,
    AssetSnapshot,
    Candle,
    DataFetchMetadata,
    EventInfo,
    Fundamentals,
    PriceBasisClaim,
    RiskPlan,
)
from advisor import evidence_archive as evidence_archive_module
from advisor import cli as cli_module
from advisor.scoring import classify_asset, score_asset
from advisor.signal_observation import (
    SignalObservation,
    SignalRunMetadata,
    build_signal_observation,
    compute_observation_hash,
)


class CanonicalSerializationTests(unittest.TestCase):
    def test_canonical_json_is_deterministic_for_key_order(self):
        first = {
            "z": "ação",
            "a": {"β": 1, "α": 2},
            "list": [3, 2],
        }
        second = {
            "list": [3, 2],
            "a": {"α": 2, "β": 1},
            "z": "ação",
        }

        expected = '{"a":{"α":2,"β":1},"list":[3,2],"z":"ação"}'.encode(
            "utf-8"
        )
        self.assertEqual(canonical_json_bytes(first), expected)
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertNotIn(b"\n", expected)

    def test_strict_json_loader_rejects_duplicate_raw_keys(self):
        raw_json = b'{"symbol":"AAPL","symbol":"NVDA"}'

        with self.assertRaises(ValueError):
            strict_json_loads_bytes(raw_json)

    def test_strict_json_loader_rejects_malformed_utf8_and_json(self):
        for raw_json in (b"\xff", b'{"symbol":'):
            with self.subTest(raw_json=raw_json):
                with self.assertRaises(ValueError):
                    strict_json_loads_bytes(raw_json)

    def test_strict_json_loader_rejects_nonfinite_constants(self):
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                with self.assertRaises(ValueError):
                    strict_json_loads_bytes(b'{"value":' + constant + b"}")

    def test_canonical_json_rejects_nonfinite_numbers(self):
        for number in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    canonical_json_bytes({"value": number})

        unsupported_values = (
            Decimal("1.0"),
            datetime(2026, 8, 29, tzinfo=timezone.utc),
            Path("payload.json"),
            {"unsupported-set-value"},
        )
        for value in unsupported_values:
            with self.subTest(value=repr(value)):
                with self.assertRaises((TypeError, ValueError)):
                    canonical_json_bytes({"value": value})

    def test_deterministic_gzip_is_byte_identical_and_single_member(self):
        json_bytes = b'{"message":"a\xc3\xa7\xc3\xa3o","value":1}'

        compressed = deterministic_gzip(json_bytes)
        self.assertEqual(compressed, deterministic_gzip(json_bytes))
        self.assertEqual(
            compressed[:10], b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff"
        )

        reader = zlib.decompressobj(wbits=31)
        self.assertEqual(reader.decompress(compressed) + reader.flush(), json_bytes)
        self.assertTrue(reader.eof)
        self.assertEqual(reader.unused_data, b"")
        self.assertEqual(
            decompress_single_member_gzip(
                compressed, max_uncompressed_bytes=len(json_bytes)
            ),
            json_bytes,
        )

    def test_malformed_gzip_multi_member_and_oversized_input_are_rejected(self):
        json_bytes = b'{"a":1}'
        compressed = deterministic_gzip(json_bytes)

        invalid_gzip_values = (
            b"not-gzip",
            compressed + b"trailing",
            compressed + compressed,
        )
        for invalid_gzip in invalid_gzip_values:
            with self.subTest(invalid_gzip=invalid_gzip):
                with self.assertRaises(ValueError):
                    decompress_single_member_gzip(
                        invalid_gzip, max_uncompressed_bytes=len(json_bytes)
                    )

        corrupt_crc = bytearray(compressed)
        corrupt_crc[-8] ^= 0x01
        with self.assertRaises(ValueError):
            decompress_single_member_gzip(
                bytes(corrupt_crc), max_uncompressed_bytes=len(json_bytes)
            )

        corrupt_size = bytearray(compressed)
        corrupt_size[-4] ^= 0x01
        with self.assertRaises(ValueError):
            decompress_single_member_gzip(
                bytes(corrupt_size), max_uncompressed_bytes=len(json_bytes)
            )

        with self.assertRaises(ValueError):
            decompress_single_member_gzip(
                compressed, max_uncompressed_bytes=len(json_bytes) - 1
            )

    def test_validate_canonical_envelope_recomputes_hashes_and_rejects_incoherence(
        self,
    ):
        evidence_type = "observation"
        schema_version = "1.0"
        logical_identity = {"signal_id": "signal-1"}
        payload = {"symbol": "AAPL", "close": 123.45}
        semantic_provenance = {"price_provider": "fmp"}
        envelope = {
            "canonical_content_sha256": canonical_content_sha256(
                evidence_type=evidence_type,
                schema_version=schema_version,
                logical_identity=logical_identity,
                payload=payload,
                semantic_provenance=semantic_provenance,
            ),
            "evidence_type": evidence_type,
            "logical_identity": logical_identity,
            "payload": payload,
            "payload_sha256": payload_sha256(payload),
            "schema_version": schema_version,
            "semantic_provenance": semantic_provenance,
            "transport": {"artifact": "run-1", "attempt": 1},
        }

        self.assertIsNone(validate_canonical_envelope(envelope))

        bad_payload_hash = dict(envelope)
        bad_payload_hash["payload_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_canonical_envelope(bad_payload_hash)

        bad_content_hash = dict(envelope)
        bad_content_hash["canonical_content_sha256"] = "1" * 64
        with self.assertRaises(ValueError):
            validate_canonical_envelope(bad_content_hash)

        bad_identity = dict(envelope)
        bad_identity["logical_identity"] = "signal-1"
        with self.assertRaises((TypeError, ValueError)):
            validate_canonical_envelope(bad_identity)


class SharedEvidenceTypeContractTests(unittest.TestCase):
    def test_shared_type_aliases_match_approved_plan(self):
        self.assertEqual(
            frozenset(get_args(ArchiveStatus)),
            frozenset(
                {
                    "committed",
                    "no_op",
                    "conflict",
                    "rejected",
                    "evidence_branch_missing",
                }
            ),
        )
        self.assertEqual(
            frozenset(get_args(HorizonStatus)),
            frozenset(
                {
                    "market_data_unavailable",
                    "pending",
                    "conflict",
                    "signal_basis_unavailable",
                    "feed_unavailable",
                    "split_in_horizon_unavailable",
                    "verified_none",
                    "not_applicable",
                }
            ),
        )
        self.assertEqual(
            frozenset(get_args(CorporateActionReasonCode)),
            frozenset({"corporate_action_revision_conflict"}),
        )
        self.assertEqual(
            frozenset(get_args(SignalBasisStatus)),
            frozenset({"verified_raw_ohlcv", "signal_basis_unavailable"}),
        )
        self.assertEqual(
            frozenset(get_args(SplitPolicy)),
            frozenset({"verified_no_split_in_signal_horizon_v1"}),
        )
        self.assertEqual(
            frozenset(get_args(CryptoPolicy)),
            frozenset({"not_applicable_crypto_raw_ohlcv_v1"}),
        )


class SignalBasisPropagationTests(unittest.TestCase):
    def test_explicit_raw_metadata_claim_qualifies(self):
        claim = PriceBasisClaim(
            price_basis="raw_ohlcv",
            price_basis_policy_version="price_basis_v1",
            source_contract="fmp.historical_price_eod.full.raw_ohlcv_v1",
        )

        self.assertTrue(is_qualified_raw_ohlcv_claim(claim))

    def test_provider_name_alone_does_not_qualify(self):
        claim = PriceBasisClaim(
            price_basis="raw_ohlcv",
            price_basis_policy_version="price_basis_v1",
            source_contract="fmp",
        )

        self.assertFalse(is_qualified_raw_ohlcv_claim(claim))

    def test_unqualified_fallback_source_is_signal_basis_unavailable(self):
        snapshot = _task3_snapshot(claim=None, provider="yahoo")
        observation = _task3_observation(snapshot)
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "observations.json.gz"
            build_observation_sidecar(
                [observation],
                snapshots_by_symbol={snapshot.symbol: snapshot},
                output_path=output_path,
            )
            self.assertEqual(
                resolve_signal_price_basis_status(
                    observation=observation,
                    sidecar=_read_task3_sidecar(output_path),
                ),
                "signal_basis_unavailable",
            )

    def test_basis_claim_propagates_through_live_loader_pipeline(self):
        claim = qualified_fmp_full_price_basis_claim()
        historical_payload = {
            "historical": [
                {
                    "date": "2026-08-28",
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "volume": 1000,
                }
            ]
        }
        loader = LiveDataLoader(
            AdvisorConfig.default(),
            fetch_json=lambda *args, **kwargs: historical_payload,
        )
        payload = loader._fetch(
            "fmp",
            "prices",
            loader.fmp.historical_prices_url("AAPL"),
            price_basis_claim=claim,
        )
        snapshot = stock_snapshot_from_payloads(
            symbol="AAPL",
            theme="hardware",
            historical_payload=payload,
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[],
            today="2026-08-31",
            data_fetch_metadata=loader._last_fetch_metadata,
        )
        self.assertEqual(snapshot.data_fetch_metadata.price_basis_claim, claim)

    def test_basis_metadata_does_not_change_asset_decision_report_scoring_or_risk(self):
        plain = _task3_snapshot(claim=None)
        qualified = _task3_snapshot(claim=qualified_fmp_full_price_basis_claim())
        plain_decision = classify_asset(
            score_asset(
                plain,
                stock_regime_label="bull",
                crypto_regime_label="neutral",
            ),
            None,
            effective_now_utc=datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc),
        )
        qualified_decision = classify_asset(
            score_asset(
                qualified,
                stock_regime_label="bull",
                crypto_regime_label="neutral",
            ),
            None,
            effective_now_utc=datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(plain_decision, qualified_decision)

    def test_qualified_route_cache_hit_preserves_basis_claim(self):
        claim = qualified_fmp_full_price_basis_claim()
        payload = {"historical": [{"date": "2026-08-28", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}]}
        with tempfile.TemporaryDirectory() as temporary_directory:
            loader = LiveDataLoader(
                AdvisorConfig.default(),
                db_path=Path(temporary_directory) / "cache.db",
                fetch_json=lambda *args, **kwargs: payload,
            )
            url = loader.fmp.historical_prices_url("AAPL")
            loader._fetch("fmp", "prices", url, price_basis_claim=claim)
            loader._fetch("fmp", "prices", url, price_basis_claim=claim)
            self.assertTrue(loader._last_fetch_metadata.cache_hit)
            self.assertEqual(loader._last_fetch_metadata.price_basis_claim, claim)

    def test_unqualified_cache_hit_does_not_gain_basis_claim(self):
        payload = {"historical": [{"date": "2026-08-28", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}]}
        with tempfile.TemporaryDirectory() as temporary_directory:
            loader = LiveDataLoader(
                AdvisorConfig.default(),
                db_path=Path(temporary_directory) / "cache.db",
                fetch_json=lambda *args, **kwargs: payload,
            )
            url = loader.fmp.historical_prices_light_url("AAPL")
            loader._fetch("fmp", "prices", url)
            loader._fetch("fmp", "prices", url)
            self.assertTrue(loader._last_fetch_metadata.cache_hit)
            self.assertIsNone(loader._last_fetch_metadata.price_basis_claim)

    def test_provider_name_cache_hit_does_not_qualify(self):
        payload = {"historical": [{"date": "2026-08-28", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}]}
        with tempfile.TemporaryDirectory() as temporary_directory:
            loader = LiveDataLoader(
                AdvisorConfig.default(),
                db_path=Path(temporary_directory) / "cache.db",
                fetch_json=lambda *args, **kwargs: payload,
            )
            url = loader.fmp.historical_prices_url("AAPL")
            loader._fetch("fmp", "prices", url, fallback_from="yahoo")
            loader._fetch("fmp", "prices", url, fallback_from="yahoo")
            self.assertTrue(loader._last_fetch_metadata.cache_hit)
            self.assertIsNone(loader._last_fetch_metadata.price_basis_claim)


class SourceContractQualificationTests(unittest.TestCase):
    def test_fmp_full_fixture_proves_provider_native_raw_ohlcv_without_adjustment(self):
        payload = {
            "historical": [
                {
                    "date": "2026-08-28",
                    "open": 101.0,
                    "high": 105.0,
                    "low": 99.0,
                    "close": 103.0,
                    "volume": 1000,
                    "adjustedClose": 203.0,
                }
            ]
        }
        self.assertIn(
            "/stable/historical-price-eod/full",
            FmpSource("demo").historical_prices_url("MSFT"),
        )
        snapshot = stock_snapshot_from_payloads(
            symbol="MSFT",
            theme="software",
            historical_payload=payload,
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[],
            today="2026-08-31",
            data_fetch_metadata=DataFetchMetadata(
                provider="fmp",
                endpoint="prices",
                price_basis_claim=qualified_fmp_full_price_basis_claim(),
            ),
        )
        self.assertEqual(
            (snapshot.candles[0].open, snapshot.candles[0].high, snapshot.candles[0].low, snapshot.candles[0].close, snapshot.candles[0].volume),
            (101.0, 105.0, 99.0, 103.0, 1000.0),
        )
        self.assertEqual(
            qualified_fmp_full_price_basis_claim(),
            PriceBasisClaim(
                price_basis="raw_ohlcv",
                price_basis_policy_version="price_basis_v1",
                source_contract="fmp.historical_price_eod.full.raw_ohlcv_v1",
            ),
        )

    def test_binance_klines_fixture_proves_provider_native_raw_ohlcv_without_adjustment(self):
        payload = [[
            1_756_944_000_000,
            "101.0",
            "105.0",
            "99.0",
            "103.0",
            "1000.0",
        ]]
        self.assertIn("/fapi/v1/klines", BinanceSource().klines_url("BTCUSDT"))
        snapshot = crypto_snapshot_from_payloads(
            symbol="BTC",
            theme="crypto",
            klines_payload=payload,
            market_payload={},
            funding_payload=[],
            open_interest_payload={},
            taker_payload=[],
            data_fetch_metadata=DataFetchMetadata(
                provider="binance",
                endpoint="prices",
                price_basis_claim=qualified_binance_klines_basis_claim(),
            ),
        )
        self.assertEqual(
            (snapshot.candles[0].open, snapshot.candles[0].high, snapshot.candles[0].low, snapshot.candles[0].close, snapshot.candles[0].volume),
            (101.0, 105.0, 99.0, 103.0, 1000.0),
        )
        self.assertEqual(
            qualified_binance_klines_basis_claim(),
            PriceBasisClaim(
                price_basis="raw_ohlcv",
                price_basis_policy_version="price_basis_v1",
                source_contract="binance.futures_klines.raw_ohlcv_v1",
            ),
        )

    def test_hyperliquid_candle_snapshot_fixture_proves_provider_native_raw_ohlcv_without_adjustment(self):
        payload = [{
            "t": 1_756_944_000_000,
            "o": "201.0",
            "h": "205.0",
            "l": "199.0",
            "c": "203.0",
            "v": "2000.0",
        }]
        self.assertEqual(
            HyperliquidSource()
            .candle_snapshot_payload(
                "HYPE", start_time_ms=1, end_time_ms=2
            )["type"],
            "candleSnapshot",
        )
        loader = LiveDataLoader(
            AdvisorConfig.default(),
            fetch_json=lambda *args, **kwargs: payload,
        )
        rows = loader._hyperliquid_klines("HYPE")
        snapshot = crypto_snapshot_from_payloads(
            symbol="HYPE",
            theme="crypto",
            klines_payload=rows,
            market_payload={},
            funding_payload=[],
            open_interest_payload={},
            taker_payload=[],
            data_fetch_metadata=loader._last_fetch_metadata,
        )
        self.assertEqual(
            (snapshot.candles[0].open, snapshot.candles[0].high, snapshot.candles[0].low, snapshot.candles[0].close, snapshot.candles[0].volume),
            (201.0, 205.0, 199.0, 203.0, 2000.0),
        )
        self.assertEqual(
            qualified_hyperliquid_candle_snapshot_basis_claim(),
            PriceBasisClaim(
                price_basis="raw_ohlcv",
                price_basis_policy_version="price_basis_v1",
                source_contract="hyperliquid.candle_snapshot.raw_ohlcv_v1",
            ),
        )

    def test_fmp_light_without_proof_remains_unqualified(self):
        payload = {
            "historical": [
                {
                    "date": "2026-08-28",
                    "open": 101.0,
                    "high": 105.0,
                    "low": 99.0,
                    "close": 103.0,
                    "volume": 1000,
                }
            ]
        }
        self.assertIn(
            "/stable/historical-price-eod/light",
            FmpSource("demo").historical_prices_light_url("MSFT"),
        )
        snapshot = stock_snapshot_from_payloads(
            symbol="MSFT",
            theme="software",
            historical_payload=payload,
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[],
            today="2026-08-31",
            data_fetch_metadata=DataFetchMetadata(provider="fmp", endpoint="prices"),
        )
        self.assertEqual(snapshot.candles[0].close, 103.0)
        self.assertIsNone(snapshot.data_fetch_metadata.price_basis_claim)
        self.assertFalse(
            is_qualified_raw_ohlcv_claim(
                PriceBasisClaim(
                    price_basis="raw_ohlcv",
                    price_basis_policy_version="price_basis_v1",
                    source_contract="fmp.historical_price_eod.light.raw_ohlcv_v1",
                )
            )
        )


def _task3_snapshot(
    symbol: str = "AAPL",
    *,
    claim: PriceBasisClaim | None = None,
    asset_type: str = "stock",
    provider: str = "fmp",
) -> AssetSnapshot:
    candles = [
        Candle(
            date=f"2026-01-{index:02d}",
            open=100.0 + index,
            high=101.0 + index,
            low=99.0 + index,
            close=100.5 + index,
            volume=1_000_000.0,
        )
        for index in range(1, 31)
    ]
    metadata = DataFetchMetadata(
        provider=provider,
        endpoint="prices",
        fetched_at="2026-08-31T15:00:00+00:00",
        source_timestamp=candles[-1].date,
        is_fresh=True,
        cache_hit=False,
        granularity="daily",
        market_data_kind="eod_candle",
        price_basis_claim=claim,
    )
    event = (
        EventInfo(
            days_to_earnings=None,
            guidance_recent=None,
            post_earnings_gap_percent=None,
        )
        if asset_type == "stock"
        else None
    )
    return AssetSnapshot(
        symbol=symbol,
        asset_type=asset_type,
        theme="hardware" if asset_type == "stock" else "crypto",
        candles=candles,
        fundamentals=Fundamentals(
            pe=None,
            peg=None,
            historical_pe=None,
            revenue_growth=None,
            eps_growth=None,
            margin_trend=None,
            free_cash_flow_positive=None,
            market_cap=1_000_000_000.0,
            average_volume=1_000_000.0,
        ),
        event=event,
        data_source=provider,
        data_timestamp=candles[-1].date,
        data_fetch_metadata=metadata,
    )


def _task3_decision(snapshot: AssetSnapshot) -> AssetDecision:
    risk_plan = RiskPlan(
        entry=130.5,
        stop=125.0,
        target_2r=141.5,
        target_3r=147.0,
        per_unit_risk=5.5,
        risk_amount=250.0,
        risk_fraction=0.005,
        max_position_units=45.0,
        max_position_value=5_872.5,
        risk_reward_2r="2.0R",
        alerts=[],
    )
    return AssetDecision(
        symbol=snapshot.symbol,
        asset_type=snapshot.asset_type,
        decision="watch_buy",
        investment_quality_score=72.0,
        swing_trade_score=68.0,
        risk_plan=risk_plan,
        alerts=[],
        limitations=[],
        thesis="task3 fixture",
        metrics_summary=[],
        ideal_entry=130.5,
        alternative_entry=None,
        hold_suggestion="watch",
        backtest_stats=None,
        sample_quality=None,
        reason_codes=["fixture"],
        data_quality="sufficient",
        missing_data_severity="none",
        data_source=snapshot.data_source,
        data_timestamp=snapshot.data_timestamp,
        cache_age_seconds=None,
        bucket="B",
        market_session="regular",
        last_price_timestamp=snapshot.data_timestamp,
        provider=snapshot.data_fetch_metadata.provider if snapshot.data_fetch_metadata else "unknown",
        universe_origin="watchlist",
        data_quality_score=80,
        decision_confidence_score=75,
    )


def _task3_run_metadata() -> SignalRunMetadata:
    return SignalRunMetadata(
        schema_version="1.0",
        source_sha="a" * 40,
        run_id="123456",
        run_origin="github",
        report_date_brt="2026-08-31",
        report_type="main",
        signal_timestamp_utc="2026-08-31T15:00:00Z",
    )


def _task3_observation(
    snapshot: AssetSnapshot | None = None,
    *,
    claim: PriceBasisClaim | None = None,
) -> SignalObservation:
    resolved_snapshot = snapshot or _task3_snapshot(claim=claim)
    return build_signal_observation(
        _task3_decision(resolved_snapshot),
        resolved_snapshot,
        _task3_run_metadata(),
        stock_regime="bull",
        crypto_regime="neutral",
    )


def _read_task3_sidecar(path: Path) -> dict[str, object]:
    return strict_json_loads_bytes(
        decompress_single_member_gzip(
            path.read_bytes(),
            max_uncompressed_bytes=4 * 1024 * 1024,
        )
    )


def _task3_scan_args(root: Path, *, db_name: str, report_name: str) -> argparse.Namespace:
    return argparse.Namespace(
        db=str(root / db_name),
        fixture_dir=None,
        output_dir=root / report_name,
        include_discovery=False,
        require_live=False,
        report_type="main",
        scan_errors=[],
        provider_budget=None,
        config=AdvisorConfig.default(),
        skip_live_validation=True,
        close_universe_source=None,
        cache_reused_from_main=False,
        runtime_scoring_artifact=False,
        signal_observation_metadata=_task3_run_metadata(),
        signal_observation_error_code="serialization_error",
    )


class ObservationSidecarTests(unittest.TestCase):
    def test_sidecar_binds_basis_by_signal_id_and_observation_hash(self):
        claim = qualified_fmp_full_price_basis_claim()
        snapshot = _task3_snapshot(claim=claim)
        observation = _task3_observation(snapshot)
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "evidence" / "observations.json.gz"
            result = build_observation_sidecar(
                [observation],
                snapshots_by_symbol={snapshot.symbol: snapshot},
                output_path=output_path,
            )

            self.assertEqual(result, output_path)
            sidecar = _read_task3_sidecar(output_path)
            self.assertEqual(sidecar["schema_version"], "1.0")
            self.assertEqual(sidecar["source_sha"], observation.source_sha)
            self.assertEqual(sidecar["run_id"], observation.run_id)
            self.assertEqual(sidecar["report_type"], observation.report_type)
            self.assertEqual(len(sidecar["observations"]), 1)
            self.assertEqual(
                sidecar["observations"][0]["signal_id"], observation.signal_id
            )
            self.assertEqual(
                sidecar["observations"][0]["observation_hash"],
                observation.observation_hash,
            )
            provenance = sidecar["provenance"][0]
            self.assertEqual(
                (provenance["signal_id"], provenance["observation_hash"]),
                (observation.signal_id, observation.observation_hash),
            )
            self.assertEqual(
                resolve_signal_price_basis_status(
                    observation=observation,
                    sidecar=sidecar,
                ),
                "verified_raw_ohlcv",
            )

    def test_sidecar_rejects_unknown_observation_hash_binding(self):
        claim = qualified_fmp_full_price_basis_claim()
        snapshot = _task3_snapshot(claim=claim)
        observation = _task3_observation(snapshot)
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "observations.json.gz"
            build_observation_sidecar(
                [observation],
                snapshots_by_symbol={snapshot.symbol: snapshot},
                output_path=output_path,
            )
            unknown_hash_observation = replace(
                observation,
                observation_hash="f" * 64,
            )

            self.assertEqual(
                resolve_signal_price_basis_status(
                    observation=unknown_hash_observation,
                    sidecar=_read_task3_sidecar(output_path),
                ),
                "signal_basis_unavailable",
            )

    def test_report_sidecar_reuses_the_same_in_memory_observations(self):
        observation = _task3_observation(_task3_snapshot("MSFT"))
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = _task3_scan_args(root, db_name="report.db", report_name="reports")
            with patch.object(
                cli_module,
                "_build_signal_observations",
                return_value=[observation],
            ), patch.object(
                cli_module,
                "_persist_signal_observations",
                return_value="written",
            ) as persist, patch.object(
                cli_module,
                "build_observation_sidecar",
                create=True,
                return_value=root / "reports" / "evidence" / "observations.json.gz",
            ) as sidecar:
                self.assertEqual(cli_module._scan(args), 0)

            self.assertIs(persist.call_args.args[1], sidecar.call_args.args[0])

    def test_signal_observation_schema_and_hash_are_unchanged(self):
        observation = _task3_observation()
        expected_fields = set(SignalObservation.__dataclass_fields__)
        self.assertEqual(set(asdict(observation)), expected_fields)
        self.assertEqual(compute_observation_hash(observation), observation.observation_hash)
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "observations.json.gz"
            build_observation_sidecar(
                [observation],
                snapshots_by_symbol={observation.asset: _task3_snapshot()},
                output_path=output_path,
            )
            row = _read_task3_sidecar(output_path)["observations"][0]
            self.assertEqual(set(row), expected_fields)
            self.assertEqual(row["observation_hash"], observation.observation_hash)
            self.assertIsNone(row["persisted_at_utc"])
            self.assertEqual(row["reason_codes"], list(observation.reason_codes))

    def test_sqlite_unavailable_does_not_prevent_valid_sidecar(self):
        observation = _task3_observation(_task3_snapshot("MSFT"))
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = _task3_scan_args(root, db_name="unavailable.db", report_name="reports")
            with patch.object(
                cli_module,
                "_build_signal_observations",
                return_value=[observation],
            ), patch.object(
                cli_module.SQLiteCache,
                "save_signal_observations",
                return_value=SimpleNamespace(status="unavailable"),
            ):
                self.assertEqual(cli_module._scan(args), 0)

            sidecar = _read_task3_sidecar(
                root / "reports" / "evidence" / "observations.json.gz"
            )
            self.assertEqual(sidecar["observations"][0]["signal_id"], observation.signal_id)
            self.assertEqual(
                sidecar["observations"][0]["observation_hash"],
                observation.observation_hash,
            )

    def test_observation_construction_failure_emits_no_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = _task3_scan_args(root, db_name="construction.db", report_name="reports")
            with patch.object(
                cli_module,
                "_build_signal_observations",
                side_effect=ValueError("invalid observation"),
            ), patch.object(
                cli_module,
                "build_observation_sidecar",
                create=True,
            ) as sidecar:
                self.assertEqual(cli_module._scan(args), 0)

            self.assertFalse(
                (root / "reports" / "evidence" / "observations.json.gz").exists()
            )
            sidecar.assert_not_called()

    def test_sqlite_failure_does_not_change_report_decision_or_observation_hash(self):
        observation = _task3_observation(_task3_snapshot("MSFT"))
        report_decisions: list[list[str]] = []
        sidecar_bytes: list[bytes] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for index, storage_failure in enumerate((False, True)):
                args = _task3_scan_args(
                    root,
                    db_name=f"comparison-{index}.db",
                    report_name=f"reports-{index}",
                )
                save_patch = patch.object(
                    cli_module.SQLiteCache,
                    "save_signal_observations",
                    side_effect=OSError("sqlite unavailable") if storage_failure else None,
                    return_value=SimpleNamespace(status="written"),
                )
                with patch.object(
                    cli_module,
                    "_build_signal_observations",
                    return_value=[observation],
                ), save_patch:
                    self.assertEqual(cli_module._scan(args), 0)
                report = (args.output_dir / "advisor-report.md").read_text(encoding="utf-8")
                report_decisions.append(
                    [
                        line
                        for line in report.splitlines()
                        if line.startswith("- decision_label:")
                        or line.startswith("- Decisao:")
                    ]
                )
                sidecar_bytes.append(
                    (args.output_dir / "evidence" / "observations.json.gz").read_bytes()
                )

        self.assertEqual(report_decisions[0], report_decisions[1])
        self.assertEqual(sidecar_bytes[0], sidecar_bytes[1])


class CanonicalIdempotencyTests(unittest.TestCase):
    def _content_hash(self, *, payload, semantic_provenance):
        return canonical_content_sha256(
            evidence_type="market_bar",
            schema_version="1.0",
            logical_identity={"symbol": "AAPL", "date": "2026-08-28"},
            payload=payload,
            semantic_provenance=semantic_provenance,
        )

    def _envelope(self, *, canonical_hash, payload_sha="payload-hash"):
        return {
            "logical_identity": {"symbol": "AAPL", "date": "2026-08-28"},
            "canonical_content_sha256": canonical_hash,
            "payload_sha256": payload_sha,
        }

    def test_same_logical_identity_same_canonical_content_is_duplicate_same(self):
        canonical_hash = self._content_hash(
            payload={"open": 100.0, "close": 101.0},
            semantic_provenance={"price_provider": "fmp"},
        )

        self.assertEqual(
            classify_idempotency(
                self._envelope(canonical_hash=canonical_hash),
                self._envelope(canonical_hash=canonical_hash),
            ),
            "duplicate_same",
        )

    def test_same_logical_identity_different_canonical_content_is_conflict(self):
        canonical_hash_a = self._content_hash(
            payload={"open": 100.0, "close": 101.0},
            semantic_provenance={"price_provider": "fmp"},
        )
        canonical_hash_b = self._content_hash(
            payload={"open": 100.0, "close": 102.0},
            semantic_provenance={"price_provider": "fmp"},
        )

        self.assertNotEqual(canonical_hash_a, canonical_hash_b)
        self.assertEqual(
            classify_idempotency(
                self._envelope(canonical_hash=canonical_hash_a),
                self._envelope(canonical_hash=canonical_hash_b),
            ),
            "conflict",
        )

    def test_same_ohlc_different_semantic_provenance_is_conflict(self):
        payload = {"open": 100.0, "high": 103.0, "low": 99.0, "close": 101.0}
        canonical_hash_fmp = self._content_hash(
            payload=payload,
            semantic_provenance={"price_provider": "fmp"},
        )
        canonical_hash_yahoo = self._content_hash(
            payload=payload,
            semantic_provenance={"price_provider": "yahoo"},
        )

        self.assertNotEqual(canonical_hash_fmp, canonical_hash_yahoo)
        self.assertEqual(
            classify_idempotency(
                self._envelope(canonical_hash=canonical_hash_fmp),
                self._envelope(canonical_hash=canonical_hash_yahoo),
            ),
            "conflict",
        )

    def test_payload_sha256_is_not_idempotency_authority(self):
        payload = {"close": 101.0}
        payload_hash = payload_sha256(payload)
        self.assertEqual(
            payload_hash,
            hashlib.sha256(b'{"close":101.0}').hexdigest(),
        )

        canonical_hash = self._content_hash(
            payload=payload,
            semantic_provenance={"price_provider": "fmp"},
        )
        self.assertEqual(
            classify_idempotency(
                self._envelope(
                    canonical_hash=canonical_hash, payload_sha=payload_hash
                ),
                self._envelope(
                    canonical_hash=canonical_hash, payload_sha="different-payload-hash"
                ),
            ),
            "duplicate_same",
        )

    def test_transport_metadata_does_not_change_canonical_content_hash(self):
        semantic_fields = {
            "evidence_type": "observation",
            "schema_version": "1.0",
            "logical_identity": {"signal_id": "signal-1"},
            "payload": {"symbol": "AAPL", "close": 123.45},
            "semantic_provenance": {"price_provider": "fmp"},
        }
        transport_a = {"artifact": "run-1", "attempt": 1}
        transport_b = {"artifact": "run-2", "attempt": 2}
        self.assertNotEqual(transport_a, transport_b)

        expected = hashlib.sha256(
            b'{"evidence_type":"observation","logical_identity":{"signal_id":"signal-1"},"payload":{"close":123.45,"symbol":"AAPL"},"schema_version":"1.0","semantic_provenance":{"price_provider":"fmp"}}'
        ).hexdigest()
        self.assertEqual(
            canonical_content_sha256(**semantic_fields),
            expected,
        )
        self.assertEqual(
            canonical_content_sha256(**semantic_fields),
            canonical_content_sha256(**semantic_fields),
        )


def _run_git(cwd: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def _git_bytes(cwd: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    ).stdout


def _market_envelope(
    *,
    symbol: str = "AAPL",
    market_date: str = "2026-08-26",
    close: float = 100.0,
    provider: str = "fmp",
) -> dict[str, object]:
    logical_identity = {
        "asset_type": "stock",
        "interval": "1d",
        "market_date": market_date,
        "market_timezone": "America/New_York",
        "schema_version": "1.0",
        "symbol": symbol,
    }
    payload = {
        "asset_type": "stock",
        "interval": "1d",
        "market_date": market_date,
        "market_timezone": "America/New_York",
        "ohlcv": {
            "close": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "open": close - 0.5,
            "volume": 123456,
        },
        "price_basis": "raw_ohlcv",
        "session_close_type": "regular",
        "session_status": "complete",
    }
    semantic_provenance = {
        "collection_policy_version": "1.0",
        "price_provider": provider,
        "source_response_sha256": "0" * 64,
    }
    return {
        "canonical_content_sha256": canonical_content_sha256(
            evidence_type="market_bar",
            schema_version="1.0",
            logical_identity=logical_identity,
            payload=payload,
            semantic_provenance=semantic_provenance,
        ),
        "evidence_type": "market_bar",
        "logical_identity": logical_identity,
        "payload": payload,
        "payload_sha256": payload_sha256(payload),
        "schema_version": "1.0",
        "semantic_provenance": semantic_provenance,
    }


def _corporate_action_envelope(
    *,
    symbol: str = "AAPL",
    provider: str = "alpha_vantage",
    coverage_start: str = "2020-01-01",
    coverage_end: str = "2026-08-26",
    events: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    normalized_events = events if events is not None else [
        {
            "effective_date": "2025-01-15",
            "split_factor_raw": "2.0000",
            "split_ratio": {"new_shares": "2", "old_shares": "1"},
        }
    ]
    logical_identity = {
        "asset_type": "stock",
        "corporate_action_provider": provider,
        "coverage_end_date": coverage_end,
        "coverage_start_date": coverage_start,
        "function": "SPLITS",
        "schema_version": "1.0",
        "symbol": symbol,
    }
    payload = {
        "data": [],
        "normalized_events": normalized_events,
        "symbol": symbol,
    }
    semantic_provenance = {
        "corporate_action_provider": provider,
        "response_sha256": "1" * 64,
        "status": "ok",
    }
    return {
        "canonical_content_sha256": canonical_content_sha256(
            evidence_type="corporate_action",
            schema_version="1.0",
            logical_identity=logical_identity,
            payload=payload,
            semantic_provenance=semantic_provenance,
        ),
        "evidence_type": "corporate_action",
        "logical_identity": logical_identity,
        "payload": payload,
        "payload_sha256": payload_sha256(payload),
        "schema_version": "1.0",
        "semantic_provenance": semantic_provenance,
    }


class _ArchiveRepositoryMixin:
    branch_name = "advisor-evidence"

    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.remote = self.root / "origin.git"
        self.caller = self.root / "caller"

        _run_git(self.root, "init", "--bare", str(self.remote))
        _run_git(self.root, "init", str(self.caller))
        _run_git(self.caller, "config", "user.name", "Task 2 Test")
        _run_git(self.caller, "config", "user.email", "task2@example.invalid")
        (self.caller / "README.md").write_bytes(b"caller tracked bytes\n")
        _run_git(self.caller, "add", "README.md")
        _run_git(self.caller, "commit", "-m", "initial main")
        _run_git(self.caller, "branch", "-M", "main")
        _run_git(self.caller, "remote", "add", "origin", str(self.remote))
        _run_git(self.caller, "push", "-u", "origin", "main")
        _run_git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        (self.caller / "caller-untracked.bin").write_bytes(b"untracked caller bytes")
        self._transport_number = 0

    def tearDown(self):
        self._temporary_directory.cleanup()

    def _bootstrap(self) -> str:
        return bootstrap_evidence_branch(
            repo_dir=self.caller,
            branch_name=self.branch_name,
        )

    def _write_transport(
        self,
        envelopes: list[dict[str, object]],
        *,
        names: list[str] | None = None,
    ) -> Path:
        self._transport_number += 1
        transport_dir = self.root / f"transport-{self._transport_number}"
        transport_dir.mkdir()
        file_names = names or [
            f"shard-{index}.json.gz" for index in range(len(envelopes))
        ]
        self.assertEqual(len(file_names), len(envelopes))
        for name, envelope in zip(file_names, envelopes):
            path = transport_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(deterministic_gzip(canonical_json_bytes(envelope)))
        return transport_dir

    def _archive(self, envelopes: list[dict[str, object]], **kwargs):
        transport_dir = self._write_transport(envelopes, **kwargs)
        return EvidenceArchive(
            repo_dir=self.caller,
            branch_name=self.branch_name,
        ).archive(transport_dir)

    def _remote_tree(self) -> list[str]:
        output = _run_git(
            self.remote,
            "ls-tree",
            "-r",
            "--name-only",
            self.branch_name,
        ).stdout
        return [line for line in output.splitlines() if line]

    def _remote_commit_count(self) -> int:
        return int(
            _run_git(
                self.remote,
                "rev-list",
                "--count",
                self.branch_name,
            ).stdout.strip()
        )

    def _remote_file(self, path: str) -> bytes:
        return _git_bytes(self.remote, "show", f"{self.branch_name}:{path}")

    def _clone_branch(self, destination: Path) -> Path:
        _run_git(
            self.root,
            "clone",
            "--branch",
            self.branch_name,
            "--single-branch",
            str(self.remote),
            str(destination),
        )
        return destination


class ArchiveGitTests(_ArchiveRepositoryMixin, unittest.TestCase):
    def test_branch_bootstrap_is_orphan_and_evidence_only(self):
        root_sha = self._bootstrap()

        self.assertEqual(len(root_sha), 40)
        self.assertEqual(
            _run_git(self.remote, "rev-parse", self.branch_name).stdout.strip(),
            root_sha,
        )
        self.assertEqual(
            self._remote_tree(),
            ["evidence/branch-schema.json"],
        )
        schema = strict_json_loads_bytes(
            self._remote_file("evidence/branch-schema.json")
        )
        self.assertEqual(
            schema,
            {
                "branch_name": self.branch_name,
                "financial_evidence_count": 0,
                "schema_version": "1.0",
            },
        )
        parents = _run_git(
            self.remote,
            "rev-list",
            "--parents",
            "-n",
            "1",
            self.branch_name,
        ).stdout.split()
        self.assertEqual(parents, [root_sha])
        merge_base = _run_git(
            self.remote,
            "merge-base",
            self.branch_name,
            "main",
            check=False,
        )
        self.assertNotEqual(merge_base.returncode, 0)

    def test_bootstrap_is_one_time_and_refuses_recreation(self):
        first_root = self._bootstrap()
        before_tree = self._remote_tree()

        with self.assertRaises((ValueError, RuntimeError)):
            self._bootstrap()

        self.assertEqual(
            _run_git(self.remote, "rev-parse", self.branch_name).stdout.strip(),
            first_root,
        )
        self.assertEqual(self._remote_tree(), before_tree)

    def test_missing_evidence_branch_returns_evidence_branch_missing_without_auto_create(
        self,
    ):
        result = EvidenceArchive(
            repo_dir=self.caller,
            branch_name=self.branch_name,
        ).archive(
            self._write_transport([_market_envelope()])
        )

        self.assertEqual(result.status, "evidence_branch_missing")
        self.assertFalse(result.durability_confirmed)
        self.assertNotEqual(
            _run_git(
                self.remote,
                "show-ref",
                "--verify",
                f"refs/heads/{self.branch_name}",
                check=False,
            ).returncode,
            0,
        )

    def test_remote_query_failure_is_not_reported_as_evidence_branch_missing(self):
        unavailable_remote = self.root / "unavailable-origin.git"
        _run_git(
            self.caller,
            "remote",
            "set-url",
            "origin",
            str(unavailable_remote),
        )

        result = EvidenceArchive(
            repo_dir=self.caller,
            branch_name=self.branch_name,
        ).archive(
            self._write_transport([_market_envelope()])
        )

        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.durability_confirmed)
        self.assertEqual(result.error_code, "remote_query_error")
        self.assertNotEqual(result.status, "evidence_branch_missing")

    def test_archive_validates_full_batch_before_one_commit(self):
        self._bootstrap()
        before_count = self._remote_commit_count()
        transport = self._write_transport([_market_envelope()])
        (transport / "invalid.json.gz").write_bytes(b"not gzip")

        result = EvidenceArchive(
            repo_dir=self.caller,
            branch_name=self.branch_name,
        ).archive(transport)

        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.durability_confirmed)
        self.assertEqual(self._remote_commit_count(), before_count)
        self.assertEqual(self._remote_tree(), ["evidence/branch-schema.json"])

    def test_archive_never_leaves_partial_canonical_batch(self):
        self._bootstrap()
        before_tree = self._remote_tree()
        valid = _market_envelope(symbol="AAPL")
        invalid = _market_envelope(symbol="MSFT")
        invalid["payload_sha256"] = "f" * 64
        transport = self._write_transport([valid, invalid])

        result = EvidenceArchive(
            repo_dir=self.caller,
            branch_name=self.branch_name,
        ).archive(transport)

        self.assertEqual(result.status, "rejected")
        self.assertEqual(self._remote_tree(), before_tree)
        self.assertFalse(any(path.startswith("evidence/manifests/") for path in self._remote_tree()))
        self.assertFalse(any(path.startswith("evidence/market-bars/") for path in self._remote_tree()))

    def test_storage_failure_is_not_retried_or_reported_as_push_race_exhausted(
        self,
    ):
        self._bootstrap()
        transport = self._write_transport([_market_envelope()])
        original_write_under = evidence_archive_module._write_under
        original_push_commit = evidence_archive_module._push_commit
        calls = {"write": 0, "push": 0}

        def fail_storage(*args, **kwargs):
            calls["write"] += 1
            raise OSError("injected storage failure")

        def count_push(*args, **kwargs):
            calls["push"] += 1
            return original_push_commit(*args, **kwargs)

        evidence_archive_module._write_under = fail_storage
        evidence_archive_module._push_commit = count_push
        try:
            result = EvidenceArchive(
                repo_dir=self.caller,
                branch_name=self.branch_name,
            ).archive(transport)
        finally:
            evidence_archive_module._write_under = original_write_under
            evidence_archive_module._push_commit = original_push_commit

        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.durability_confirmed)
        self.assertEqual(result.error_code, "storage_error")
        self.assertNotEqual(result.error_code, "push_race_exhausted")
        self.assertEqual(calls["write"], 1)
        self.assertEqual(calls["push"], 0)

    def test_identical_batch_retry_returns_no_op_without_second_manifest(self):
        self._bootstrap()
        first = self._archive(
            [
                _market_envelope(symbol="AAPL", market_date="2026-08-26"),
                _market_envelope(symbol="MSFT", market_date="2026-08-27"),
            ],
            names=["z.json.gz", "a.json.gz"],
        )
        commits_before_retry = self._remote_commit_count()
        manifests_before = [
            path for path in self._remote_tree() if path.startswith("evidence/manifests/")
        ]

        second = self._archive(
            [
                _market_envelope(symbol="MSFT", market_date="2026-08-27"),
                _market_envelope(symbol="AAPL", market_date="2026-08-26"),
            ],
            names=["different-name-1.json.gz", "different-name-2.json.gz"],
        )

        self.assertEqual(first.status, "committed")
        self.assertEqual(second.status, "no_op")
        self.assertTrue(second.durability_confirmed)
        self.assertEqual(second.manifest_path, first.manifest_path)
        self.assertEqual(self._remote_commit_count(), commits_before_retry)
        self.assertEqual(
            [
                path
                for path in self._remote_tree()
                if path.startswith("evidence/manifests/")
            ],
            manifests_before,
        )

    def test_same_candidate_commit_published_by_racing_writer_returns_validated_no_op(
        self,
    ):
        self._bootstrap()
        transport = self._write_transport([_market_envelope()])
        original_push_commit = evidence_archive_module._push_commit
        state = {"raced": False, "racing_commit_count": None}
        fixed_date = "@1700000000 +0000"
        previous_dates = {
            key: os.environ.get(key)
            for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")
        }
        os.environ["GIT_AUTHOR_DATE"] = fixed_date
        os.environ["GIT_COMMITTER_DATE"] = fixed_date

        def publish_same_candidate_first(clone, branch_name, paths):
            if not state["raced"]:
                state["raced"] = True
                racing_result = EvidenceArchive(
                    repo_dir=self.caller,
                    branch_name=branch_name,
                ).archive(transport)
                self.assertEqual(racing_result.status, "committed")
                state["racing_commit_count"] = self._remote_commit_count()
            return original_push_commit(clone, branch_name, paths)

        evidence_archive_module._push_commit = publish_same_candidate_first
        try:
            result = EvidenceArchive(
                repo_dir=self.caller,
                branch_name=self.branch_name,
            ).archive(transport)
        finally:
            evidence_archive_module._push_commit = original_push_commit
            for key, value in previous_dates.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(result.status, "no_op")
        self.assertTrue(result.durability_confirmed)
        self.assertEqual(self._remote_commit_count(), state["racing_commit_count"])

    def test_no_op_is_operational_only_without_historical_record(self):
        self._bootstrap()
        self._archive([_market_envelope()])
        result = self._archive([_market_envelope()])

        self.assertEqual(result.status, "no_op")
        self.assertTrue(result.durability_confirmed)
        self.assertEqual(
            [
                path
                for path in self._remote_tree()
                if "no_op" in path.lower()
            ],
            [],
        )
        log = _run_git(self.remote, "log", "--format=%B", self.branch_name).stdout
        self.assertNotIn("no_op", log.lower())

    def test_identical_retry_does_not_overwrite_committed_manifest(self):
        self._bootstrap()
        first = self._archive([_market_envelope()])
        self.assertIsNotNone(first.manifest_path)
        before = self._remote_file(first.manifest_path)

        retry = self._archive(
            [
                {
                    **_market_envelope(),
                    "transport": {
                        "artifact": "retry-artifact",
                        "attempt": 99,
                    },
                }
            ]
        )

        self.assertEqual(retry.status, "no_op")
        self.assertEqual(self._remote_file(first.manifest_path), before)
        self.assertEqual(
            len([path for path in self._remote_tree() if path.startswith("evidence/manifests/")]),
            1,
        )

    def test_conflict_transaction_identity_is_derived_from_conflict_entries(self):
        self._bootstrap()
        self._archive(
            [
                _market_envelope(symbol="AAPL", close=100.0),
                _market_envelope(symbol="MSFT", close=200.0),
            ]
        )
        incoming = [
            _market_envelope(symbol="AAPL", close=101.0),
            _market_envelope(symbol="MSFT", close=201.0),
        ]

        first = self._archive(incoming, names=["one.json.gz", "two.json.gz"])
        commits_after_first = self._remote_commit_count()
        second = self._archive(
            list(reversed(incoming)),
            names=["reordered-a.json.gz", "reordered-b.json.gz"],
        )

        self.assertEqual(first.status, "conflict")
        self.assertEqual(second.status, "conflict")
        self.assertFalse(first.durability_confirmed)
        self.assertFalse(second.durability_confirmed)
        self.assertEqual(set(first.conflict_paths), set(second.conflict_paths))
        self.assertEqual(self._remote_commit_count(), commits_after_first)
        self.assertEqual(
            len([path for path in self._remote_tree() if path.startswith("evidence/conflicts/")]),
            len(first.conflict_paths),
        )

    def test_divergent_same_identity_writes_conflict_only_transaction(self):
        self._bootstrap()
        original = _market_envelope(symbol="AAPL", close=100.0)
        self._archive([original])
        canonical_path = next(
            path for path in self._remote_tree() if path.startswith("evidence/market-bars/")
        )
        original_bytes = self._remote_file(canonical_path)
        before_market_paths = {
            path for path in self._remote_tree() if path.startswith("evidence/market-bars/")
        }

        result = self._archive([_market_envelope(symbol="AAPL", close=101.0)])

        self.assertEqual(result.status, "conflict")
        self.assertFalse(result.durability_confirmed)
        self.assertTrue(result.conflict_paths)
        self.assertTrue(all(path.startswith("evidence/conflicts/") for path in result.conflict_paths))
        self.assertEqual(self._remote_file(canonical_path), original_bytes)
        self.assertEqual(
            {path for path in self._remote_tree() if path.startswith("evidence/market-bars/")},
            before_market_paths,
        )
        conflict = strict_json_loads_bytes(
            decompress_single_member_gzip(
                self._remote_file(result.conflict_paths[0]),
                max_uncompressed_bytes=1024 * 1024,
            )
        )
        self.assertEqual(conflict["reason_code"], "divergent_payload")

    def test_push_retries_fast_forward_at_most_three_times_without_force_or_merge(self):
        self._bootstrap()
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            "archive_count_file=\"$GIT_DIR/task2-archive-push-count\"\n"
            "archive_count=0\n"
            "read old_sha new_sha ref_name\n"
            "subject=\"$(git log -1 --format=%s \"$new_sha\")\"\n"
            "if test \"$subject\" = \"archive evidence batch\"; then\n"
            "  if test -f \"$archive_count_file\"; then archive_count=$(cat \"$archive_count_file\"); fi\n"
            "  archive_count=$((archive_count + 1))\n"
            "  printf '%s' \"$archive_count\" > \"$archive_count_file\"\n"
            "  if test \"$archive_count\" -eq 1; then\n"
            "    : > \"$GIT_DIR/task2-race-triggered\"\n"
            "    sleep 1\n"
            "  fi\n"
            "  if test \"$archive_count\" -le 3; then exit 1; fi\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )

        race_clone = self.root / "race-clone"
        _run_git(
            self.root,
            "clone",
            "--branch",
            self.branch_name,
            "--single-branch",
            str(self.remote),
            str(race_clone),
        )
        _run_git(race_clone, "config", "user.name", "Race Writer")
        _run_git(race_clone, "config", "user.email", "race@example.invalid")

        def advance_remote_after_rejection_request():
            deadline = time.monotonic() + 5.0
            trigger = self.remote / "task2-race-triggered"
            while time.monotonic() < deadline and not trigger.exists():
                time.sleep(0.01)
            if not trigger.exists():
                return
            _run_git(race_clone, "commit", "--allow-empty", "-m", "concurrent writer")
            _run_git(race_clone, "push", "origin", f"HEAD:{self.branch_name}")

        race_thread = threading.Thread(target=advance_remote_after_rejection_request)
        race_thread.start()
        try:
            result = self._archive([_market_envelope()])
        finally:
            race_thread.join(timeout=10)

        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.durability_confirmed)
        self.assertEqual(result.error_code, "push_race_exhausted")
        self.assertEqual(
            int((self.remote / "task2-archive-push-count").read_text(encoding="utf-8")),
            3,
        )
        self.assertEqual(
            self._remote_tree(),
            ["evidence/branch-schema.json"],
        )
        commits = _run_git(
            self.remote,
            "rev-list",
            "--parents",
            self.branch_name,
        ).stdout.splitlines()
        self.assertTrue(all(len(line.split()) <= 2 for line in commits))
        self.assertNotEqual(
            _run_git(
                self.remote,
                "merge-base",
                self.branch_name,
                "main",
                check=False,
            ).returncode,
            0,
        )


class GitIsolationTests(_ArchiveRepositoryMixin, unittest.TestCase):
    def test_bootstrap_archive_in_dedicated_clone_leaves_main_head_worktree_index_unchanged(
        self,
    ):
        tracked_paths = [
            path
            for path in _run_git(self.caller, "ls-files").stdout.splitlines()
            if path
        ]
        before = {
            "head": _run_git(self.caller, "rev-parse", "HEAD").stdout.strip(),
            "branch": _run_git(self.caller, "branch", "--show-current").stdout.strip(),
            "index": _run_git(self.caller, "ls-files", "-s").stdout,
            "status": _run_git(self.caller, "status", "--porcelain").stdout,
            "tracked": {path: (self.caller / path).read_bytes() for path in tracked_paths},
            "untracked": (self.caller / "caller-untracked.bin").read_bytes(),
        }

        self._bootstrap()
        self._archive([_market_envelope()])

        after = {
            "head": _run_git(self.caller, "rev-parse", "HEAD").stdout.strip(),
            "branch": _run_git(self.caller, "branch", "--show-current").stdout.strip(),
            "index": _run_git(self.caller, "ls-files", "-s").stdout,
            "status": _run_git(self.caller, "status", "--porcelain").stdout,
            "tracked": {path: (self.caller / path).read_bytes() for path in tracked_paths},
            "untracked": (self.caller / "caller-untracked.bin").read_bytes(),
        }
        self.assertEqual(after, before)


class CorporateArchiveConflictTests(_ArchiveRepositoryMixin, unittest.TestCase):
    def test_same_identity_equivalent_normalized_corporate_action_is_not_generic_conflict(
        self,
    ):
        self._bootstrap()
        existing = _corporate_action_envelope()
        self._archive([existing])
        canonical_path = next(
            path
            for path in self._remote_tree()
            if path.startswith("evidence/corporate-actions/")
        )
        canonical_before = self._remote_file(canonical_path)
        commits_before = self._remote_commit_count()
        incoming = _corporate_action_envelope()
        incoming["transport"] = {"artifact": "different-raw-transport", "attempt": 2}
        self.assertEqual(
            existing["canonical_content_sha256"],
            incoming["canonical_content_sha256"],
        )

        result = self._archive([incoming])

        self.assertEqual(result.status, "no_op")
        self.assertTrue(result.durability_confirmed)
        self.assertEqual(result.conflict_paths, ())
        self.assertEqual(self._remote_commit_count(), commits_before)
        self.assertEqual(self._remote_file(canonical_path), canonical_before)
        self.assertFalse(
            any(path.startswith("evidence/conflicts/") for path in self._remote_tree())
        )

    def test_same_identity_equivalent_events_but_different_semantic_provenance_is_generic_conflict(
        self,
    ):
        self._bootstrap()
        existing = _corporate_action_envelope()
        self._archive([existing])
        incoming = _corporate_action_envelope()
        semantic_provenance = dict(incoming["semantic_provenance"])
        semantic_provenance["status"] = "error"
        incoming["semantic_provenance"] = semantic_provenance
        incoming["canonical_content_sha256"] = canonical_content_sha256(
            evidence_type="corporate_action",
            schema_version="1.0",
            logical_identity=incoming["logical_identity"],
            payload=incoming["payload"],
            semantic_provenance=semantic_provenance,
        )
        self.assertNotEqual(
            existing["canonical_content_sha256"],
            incoming["canonical_content_sha256"],
        )

        result = self._archive([incoming])

        self.assertEqual(result.status, "conflict")
        self.assertFalse(result.durability_confirmed)
        self.assertTrue(result.conflict_paths)
        conflict = strict_json_loads_bytes(
            decompress_single_member_gzip(
                self._remote_file(result.conflict_paths[0]),
                max_uncompressed_bytes=1024 * 1024,
            )
        )
        self.assertEqual(conflict["reason_code"], "divergent_payload")

    def test_same_identity_presence_absence_revision_uses_corporate_action_revision_conflict(
        self,
    ):
        self._bootstrap()
        existing = _corporate_action_envelope(events=[])
        self._archive([existing])
        incoming = _corporate_action_envelope()

        result = self._archive([incoming])

        self.assertEqual(result.status, "conflict")
        self.assertFalse(result.durability_confirmed)
        self.assertTrue(result.conflict_paths)
        conflict = strict_json_loads_bytes(
            decompress_single_member_gzip(
                self._remote_file(result.conflict_paths[0]),
                max_uncompressed_bytes=1024 * 1024,
            )
        )
        self.assertEqual(
            conflict["reason_code"],
            "corporate_action_revision_conflict",
        )

    def test_existing_canonical_overlap_revision_creates_conflict_transaction(self):
        self._bootstrap()
        existing = _corporate_action_envelope()
        self._archive([existing])
        canonical_path = next(
            path
            for path in self._remote_tree()
            if path.startswith("evidence/corporate-actions/")
        )
        before = self._remote_file(canonical_path)
        incoming = _corporate_action_envelope(
            coverage_start="2024-01-01",
            coverage_end="2027-01-01",
            events=[
                {
                    "effective_date": "2025-01-15",
                    "split_factor_raw": "3.0000",
                    "split_ratio": {"new_shares": "3", "old_shares": "1"},
                }
            ],
        )

        result = self._archive([incoming])

        self.assertEqual(result.status, "conflict")
        self.assertFalse(result.durability_confirmed)
        self.assertEqual(self._remote_file(canonical_path), before)
        conflict = strict_json_loads_bytes(
            decompress_single_member_gzip(
                self._remote_file(result.conflict_paths[0]),
                max_uncompressed_bytes=1024 * 1024,
            )
        )
        self.assertEqual(
            conflict["reason_code"],
            "corporate_action_revision_conflict",
        )

    def test_equal_overlap_against_canonical_evidence_is_accepted(self):
        self._bootstrap()
        existing = _corporate_action_envelope()
        self._archive([existing])
        incoming = _corporate_action_envelope(
            coverage_start="2024-01-01",
            coverage_end="2027-01-01",
        )

        result = self._archive([incoming])

        self.assertEqual(result.status, "committed")
        self.assertTrue(result.durability_confirmed)
        self.assertEqual(result.conflict_paths, ())
        self.assertEqual(
            len(
                [
                    path
                    for path in self._remote_tree()
                    if path.startswith("evidence/corporate-actions/")
                ]
            ),
            2,
        )


def _write_market_checkout_shard(
    checkout: Path,
    envelope: dict[str, object],
) -> Path:
    identity = envelope["logical_identity"]
    assert isinstance(identity, dict)
    market_date = identity["market_date"]
    assert isinstance(market_date, str)
    identity_sha = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    path = (
        checkout
        / "evidence"
        / "market-bars"
        / market_date[:4]
        / market_date[5:7]
        / market_date[8:10]
        / f"{identity_sha}.json.gz"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(deterministic_gzip(canonical_json_bytes(envelope)))
    return path


class CanonicalProviderReaderTests(unittest.TestCase):
    def test_oldest_canonical_provider_by_symbol_returns_oldest_provider(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            _write_market_checkout_shard(
                checkout,
                _market_envelope(
                    symbol="AAPL",
                    market_date="2026-08-26",
                    provider="fmp",
                ),
            )
            _write_market_checkout_shard(
                checkout,
                _market_envelope(
                    symbol="AAPL",
                    market_date="2026-08-27",
                    provider="fmp",
                ),
            )
            _write_market_checkout_shard(
                checkout,
                _market_envelope(
                    symbol="MSFT",
                    market_date="2026-08-26",
                    provider="yahoo",
                ),
            )

            result = oldest_canonical_provider_by_symbol(
                evidence_checkout=checkout,
                symbols=["AAPL", "MISSING"],
            )

            self.assertEqual(result, {"AAPL": "fmp"})

    def test_oldest_canonical_provider_by_symbol_rejects_mixed_provider_series(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            _write_market_checkout_shard(
                checkout,
                _market_envelope(
                    symbol="AAPL",
                    market_date="2026-08-26",
                    provider="fmp",
                ),
            )
            _write_market_checkout_shard(
                checkout,
                _market_envelope(
                    symbol="AAPL",
                    market_date="2026-08-27",
                    provider="yahoo",
                ),
            )

            with self.assertRaisesRegex(ValueError, "mixed_price_provider"):
                oldest_canonical_provider_by_symbol(
                    evidence_checkout=checkout,
                    symbols=["AAPL"],
                )


if __name__ == "__main__":
    unittest.main()
