import argparse
import hashlib
import inspect
import json
import os
from contextlib import redirect_stdout
from io import StringIO
import subprocess
import tempfile
import threading
import time
import unittest
import zlib
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time as datetime_time, timedelta, timezone
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
    ObservationEvidenceRecord,
    ObservationSourceBinding,
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
    snapshot_projection_v1,
    snapshot_sha256_v1,
    strict_json_loads_bytes,
    validate_canonical_envelope,
)
from advisor.cache import SQLiteCache
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
from advisor.evidence_collector import (
    PRICE_PROVIDER_ASSIGNMENT_POLICY_VERSION,
    CanonicalSplitRatio,
    CollectionAsset,
    EvidenceCollector,
    assigned_price_provider,
    normalize_split_factor,
    us_early_close_dates,
    us_eastern_local,
    us_eastern_offset_for_utc,
    us_equity_session_close,
    validate_crypto_candle,
    validate_us_equity_candle,
)
from advisor.evidence_materializer import (
    EvidenceMaterializer,
    HorizonQualification,
    MaterializationError,
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
    ProviderCapability,
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


def _task4_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _task4_transport_records(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["records"]


class ProviderAssignmentTests(unittest.TestCase):
    def test_price_provider_assignment_v1_is_deterministic(self):
        self.assertEqual(PRICE_PROVIDER_ASSIGNMENT_POLICY_VERSION, "price_provider_assignment_v1")
        self.assertEqual(assigned_price_provider(asset=CollectionAsset("aapl", "stock"), existing_provider=None), "fmp")
        self.assertEqual(assigned_price_provider(asset=CollectionAsset("IGV", "etf"), existing_provider=None), "fmp")
        self.assertEqual(assigned_price_provider(asset=CollectionAsset("hype", "crypto"), existing_provider=None), "hyperliquid")
        self.assertEqual(assigned_price_provider(asset=CollectionAsset("BTC", "crypto"), existing_provider=None), "binance")

    def test_oldest_canonical_provider_is_sticky_for_existing_series(self):
        asset = CollectionAsset("HYPE", "crypto")
        self.assertEqual(assigned_price_provider(asset=asset, existing_provider="binance"), "binance")
        self.assertEqual(assigned_price_provider(asset=CollectionAsset("AAPL", "stock"), existing_provider="fmp"), "fmp")

    def test_unknown_or_unsupported_symbol_is_market_data_unavailable(self):
        calls: list[dict[str, object]] = []

        def fetch_json(**kwargs):
            calls.append(kwargs)
            return {}

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = EvidenceCollector(
                fetch_json=fetch_json,
                transport_root=Path(temporary_directory),
                now_utc=_task4_utc("2026-09-08T21:00:00Z"),
            ).collect_market(
                assets=[CollectionAsset("UNKNOWN", "crypto"), CollectionAsset("AAPL", "fund")],
                existing_provider_by_symbol={},
            )
            records = _task4_transport_records(path)

        self.assertEqual([record["status"] for record in records], ["market_data_unavailable", "market_data_unavailable"])
        self.assertEqual(calls, [])

    def test_assigned_provider_unavailable_does_not_fallback(self):
        calls: list[str] = []

        def fetch_json(**kwargs):
            calls.append(kwargs["provider"])
            raise RuntimeError("assigned provider unavailable")

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = EvidenceCollector(
                fetch_json=fetch_json,
                transport_root=Path(temporary_directory),
                now_utc=_task4_utc("2026-09-08T21:00:00Z"),
            ).collect_market(
                assets=[CollectionAsset("HYPE", "crypto")],
                existing_provider_by_symbol={},
            )
            records = _task4_transport_records(path)

        self.assertEqual(calls, ["hyperliquid"])
        self.assertEqual(records[0]["status"], "market_data_unavailable")

    def test_incomplete_market_ohlcv_is_unavailable(self):
        calls: list[dict[str, object]] = []

        def fetch_json(**kwargs):
            calls.append(kwargs)
            return {
                "historical": [
                    {
                        "date": "2026-09-08",
                        "open": 100.0,
                        "high": 102.0,
                        "low": 99.0,
                        "close": 101.0,
                    }
                ]
            }

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch(
                "advisor.evidence_collector.AdvisorConfig.default",
                return_value=AdvisorConfig(stock_watchlist=["AAPL"], fmp_api_key="fmp-key"),
            ):
                path = EvidenceCollector(
                    fetch_json=fetch_json,
                    transport_root=Path(temporary_directory),
                    now_utc=_task4_utc("2026-09-08T21:00:00Z"),
                ).collect_market(
                    assets=[CollectionAsset("AAPL", "stock")],
                    existing_provider_by_symbol={},
                )
            record = _task4_transport_records(path)[0]

        self.assertEqual([call["provider"] for call in calls], ["fmp"])
        self.assertEqual(record["status"], "market_data_unavailable")
        self.assertEqual(record["bars"], [])
        self.assertNotIn('"volume":0.0', json.dumps(record))

    def test_fmp_unavailable_does_not_call_yahoo_as_evidence_fallback(self):
        calls: list[dict[str, object]] = []

        def fetch_json(**kwargs):
            calls.append(kwargs)
            raise OSError("fmp unavailable")

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch(
                "advisor.evidence_collector.AdvisorConfig.default",
                return_value=AdvisorConfig(stock_watchlist=["AAPL"], fmp_api_key="fmp-key"),
            ):
                path = EvidenceCollector(
                    fetch_json=fetch_json,
                    transport_root=Path(temporary_directory),
                    now_utc=_task4_utc("2026-09-08T21:00:00Z"),
                ).collect_market(
                    assets=[CollectionAsset("AAPL", "stock")],
                    existing_provider_by_symbol={},
                )
            records = _task4_transport_records(path)

        self.assertEqual([call["provider"] for call in calls], ["fmp"])
        self.assertIn("apikey=fmp-key", calls[0]["url"])
        self.assertNotIn("yahoo", json.dumps(calls))
        self.assertEqual(records[0]["status"], "market_data_unavailable")

    def test_fmp_success_uses_full_raw_ohlcv_request(self):
        calls: list[dict[str, object]] = []
        payload = {
            "historical": [
                {
                    "date": "2026-09-08",
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "volume": 1000.0,
                }
            ]
        }

        def fetch_json(**kwargs):
            calls.append(dict(kwargs))
            return payload

        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            with patch(
                "advisor.evidence_collector.AdvisorConfig.default",
                return_value=AdvisorConfig(stock_watchlist=["AAPL"], fmp_api_key="fmp-key"),
            ):
                first_path = EvidenceCollector(
                    fetch_json=fetch_json,
                    transport_root=Path(first_directory),
                    now_utc=_task4_utc("2026-09-08T21:00:00Z"),
                ).collect_market(
                    assets=[CollectionAsset("AAPL", "stock")],
                    existing_provider_by_symbol={},
                )
                second_path = EvidenceCollector(
                    fetch_json=fetch_json,
                    transport_root=Path(second_directory),
                    now_utc=_task4_utc("2026-09-08T21:00:00Z"),
                ).collect_market(
                    assets=[CollectionAsset("AAPL", "stock")],
                    existing_provider_by_symbol={},
                )
            first_record = _task4_transport_records(first_path)[0]
            second_record = _task4_transport_records(second_path)[0]
            first_bytes = first_path.read_bytes()
            second_bytes = second_path.read_bytes()

        expected_request = {
            "provider": "fmp",
            "symbol": "AAPL",
            "url": "https://financialmodelingprep.com/stable/historical-price-eod/full?symbol=AAPL&apikey=fmp-key",
        }
        self.assertEqual(calls, [expected_request, expected_request])
        self.assertEqual(first_record["status"], "available")
        self.assertEqual(
            first_record["coverage_window"],
            {"start_date": "2026-09-08", "end_date": "2026-09-08"},
        )
        self.assertEqual(
            first_record["source_request"],
            {
                "interval": "1d",
                "provider": "fmp",
                "source_contract": "fmp.historical_price_eod.full.raw_ohlcv_v1",
            },
        )
        self.assertEqual(
            first_record["semantic_provenance"]["price_basis_claim"],
            {
                "price_basis": "raw_ohlcv",
                "price_basis_policy_version": "price_basis_v1",
                "source_contract": "fmp.historical_price_eod.full.raw_ohlcv_v1",
            },
        )
        self.assertEqual(first_record, second_record)
        self.assertEqual(first_bytes, second_bytes)


class CorporateActionCollectionTests(unittest.TestCase):
    def test_corporate_provider_runtime_error_is_feed_unavailable(self):
        def fetch_json(**kwargs):
            raise RuntimeError("provider unavailable")

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = EvidenceCollector(
                fetch_json=fetch_json,
                transport_root=Path(temporary_directory),
            ).collect_corporate_actions(
                assets=[CollectionAsset("AAPL", "stock")],
                coverage_windows={"AAPL": (date(2020, 1, 1), date(2026, 12, 31))},
            )
            record = _task4_transport_records(path)[0]

        self.assertEqual(record["status"], "feed_unavailable")

    def test_corporate_normalization_runtime_error_is_not_feed_unavailable(self):
        def fetch_json(**kwargs):
            return {"symbol": "AAPL", "data": []}

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch(
                "advisor.evidence_collector._normalized_split_events",
                side_effect=RuntimeError("normalization defect"),
            ):
                with self.assertRaisesRegex(RuntimeError, "normalization defect"):
                    EvidenceCollector(
                        fetch_json=fetch_json,
                        transport_root=Path(temporary_directory),
                    ).collect_corporate_actions(
                        assets=[CollectionAsset("AAPL", "stock")],
                        coverage_windows={"AAPL": (date(2020, 1, 1), date(2026, 12, 31))},
                    )

    def test_alpha_vantage_splits_fixture_normalizes_decimal_split_factor(self):
        payload = {"symbol": "AAPL", "data": [{"effective_date": "2020-08-31", "split_factor": "4.0000"}]}
        calls: list[dict[str, object]] = []

        def fetch_json(**kwargs):
            calls.append(kwargs)
            return payload

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch(
                "advisor.evidence_collector.AdvisorConfig.default",
                return_value=AdvisorConfig(alphavantage_api_key="alpha-key"),
                ):
                path = EvidenceCollector(
                    fetch_json=fetch_json,
                    transport_root=Path(temporary_directory),
                ).collect_corporate_actions(
                    assets=[CollectionAsset("AAPL", "stock")],
                    coverage_windows={"AAPL": (date(2020, 1, 1), date(2020, 12, 31))},
                )
            record = _task4_transport_records(path)[0]

        self.assertEqual(record["source_request"]["function"], "SPLITS")
        self.assertIn("apikey=alpha-key", calls[0]["url"])
        self.assertEqual(record["normalized_events"][0]["split_ratio"], {"new_shares": "4", "old_shares": "1"})

    def test_corporate_transport_preserves_provider_semantic_payload(self):
        payload = {
            "symbol": "AAPL",
            "data": [
                {"effective_date": "2020-08-31", "split_factor": "4.0000"}
            ],
        }

        def fetch_json(**kwargs):
            return payload

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = EvidenceCollector(
                fetch_json=fetch_json,
                transport_root=Path(temporary_directory),
            ).collect_corporate_actions(
                assets=[CollectionAsset("AAPL", "stock")],
                coverage_windows={"AAPL": (date(2020, 1, 1), date(2020, 12, 31))},
            )
            record = _task4_transport_records(path)[0]

        self.assertEqual(
            record["payload"],
            {
                "data": payload["data"],
                "normalized_events": [
                    {
                        "effective_date": "2020-08-31",
                        "split_factor_raw": "4.0000",
                        "split_ratio": {"new_shares": "4", "old_shares": "1"},
                    }
                ],
                "symbol": "AAPL",
            },
        )
        self.assertEqual(record["normalized_events"], record["payload"]["normalized_events"])
        self.assertEqual(
            record["semantic_provenance"]["source_response_sha256"],
            hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        )

    def test_aapl_nvda_tsla_and_igv_split_fixtures_are_supported(self):
        payloads = {
            "AAPL": "4.0000",
            "NVDA": "10.0000",
            "TSLA": "5.0000",
            "IGV": "5.0000",
        }

        def fetch_json(**kwargs):
            symbol = kwargs["symbol"]
            return {"symbol": symbol, "data": [{"effective_date": "2024-06-10", "split_factor": payloads[symbol]}]}

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = EvidenceCollector(fetch_json=fetch_json, transport_root=Path(temporary_directory)).collect_corporate_actions(
                assets=[CollectionAsset(symbol, "etf" if symbol == "IGV" else "stock") for symbol in payloads],
                coverage_windows={symbol: (date(2020, 1, 1), date(2026, 1, 1)) for symbol in payloads},
            )
            records = {record["symbol"]: record for record in _task4_transport_records(path)}

        self.assertEqual(records["AAPL"]["normalized_events"][0]["split_ratio"], {"new_shares": "4", "old_shares": "1"})
        self.assertEqual(records["NVDA"]["normalized_events"][0]["split_ratio"], {"new_shares": "10", "old_shares": "1"})
        self.assertEqual(records["TSLA"]["normalized_events"][0]["split_ratio"], {"new_shares": "5", "old_shares": "1"})
        self.assertEqual(records["IGV"]["normalized_events"][0]["split_ratio"], {"new_shares": "5", "old_shares": "1"})

    def test_reverse_split_point_two_five_normalizes_to_one_over_four(self):
        self.assertEqual(normalize_split_factor(split_factor="0.25"), CanonicalSplitRatio(new_shares="1", old_shares="4"))
        self.assertEqual(normalize_split_factor(split_factor=Decimal("0.2500")), CanonicalSplitRatio(new_shares="1", old_shares="4"))

    def test_collector_emits_windowed_transport_without_history_comparison(self):
        calls: list[dict[str, object]] = []

        def fetch_json(**kwargs):
            calls.append(kwargs)
            return {"symbol": "AAPL", "data": []}

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = EvidenceCollector(fetch_json=fetch_json, transport_root=Path(temporary_directory)).collect_corporate_actions(
                assets=[CollectionAsset("AAPL", "stock")],
                coverage_windows={"AAPL": (date(2020, 1, 1), date(2026, 9, 1))},
            )
            record = _task4_transport_records(path)[0]

        self.assertEqual([call["provider"] for call in calls], ["alpha_vantage"])
        self.assertEqual(record["coverage_window"], {"start_date": "2020-01-01", "end_date": "2026-09-01"})
        self.assertEqual(record["normalized_events"], [])
        self.assertNotIn("canonical_history", record)


class SessionCompletenessTests(unittest.TestCase):
    def test_weekend_stock_date_is_rejected(self):
        self.assertEqual(validate_us_equity_candle(market_date=date(2026, 8, 29), now_utc=_task4_utc("2026-08-31T21:00:00Z"), synthetic=False), "rejected")

    def test_us_market_holiday_is_rejected(self):
        self.assertEqual(validate_us_equity_candle(market_date=date(2026, 9, 7), now_utc=_task4_utc("2026-09-08T21:00:00Z"), synthetic=False), "rejected")

    def test_cross_year_observed_new_year_is_rejected(self):
        self.assertEqual(
            validate_us_equity_candle(
                market_date=date(2021, 12, 31),
                now_utc=_task4_utc("2022-01-03T21:00:00Z"),
                synthetic=False,
            ),
            "rejected",
        )

    def test_valid_early_close_session_is_accepted(self):
        self.assertIn(date(2026, 11, 27), us_early_close_dates(2026))
        self.assertEqual(validate_us_equity_candle(market_date=date(2026, 11, 27), now_utc=_task4_utc("2026-11-27T18:01:00Z"), synthetic=False), "accepted")

    def test_current_incomplete_us_session_candle_is_not_archived(self):
        self.assertEqual(validate_us_equity_candle(market_date=date(2026, 9, 8), now_utc=_task4_utc("2026-09-08T19:59:00Z"), synthetic=False), "rejected")

    def test_synthetic_fill_is_rejected(self):
        self.assertEqual(validate_us_equity_candle(market_date=date(2026, 9, 4), now_utc=_task4_utc("2026-09-08T21:00:00Z"), synthetic=True), "rejected")

    def test_current_utc_crypto_day_candle_is_rejected(self):
        self.assertEqual(validate_crypto_candle(market_date=date(2026, 9, 8), now_utc=_task4_utc("2026-09-08T21:00:00Z"), synthetic=False), "rejected")

    def test_completed_prior_utc_crypto_candle_is_accepted(self):
        self.assertEqual(validate_crypto_candle(market_date=date(2026, 9, 7), now_utc=_task4_utc("2026-09-08T00:00:00Z"), synthetic=False), "accepted")

    def test_us_eastern_dst_before_march_transition(self):
        self.assertEqual(us_eastern_offset_for_utc(_task4_utc("2026-03-08T06:59:00Z")), timedelta(hours=-5))

    def test_us_eastern_dst_after_march_transition(self):
        self.assertEqual(us_eastern_offset_for_utc(_task4_utc("2026-03-08T07:00:00Z")), timedelta(hours=-4))

    def test_us_eastern_dst_before_november_transition(self):
        self.assertEqual(us_eastern_offset_for_utc(_task4_utc("2026-11-01T05:59:00Z")), timedelta(hours=-4))

    def test_us_eastern_dst_after_november_transition(self):
        self.assertEqual(us_eastern_offset_for_utc(_task4_utc("2026-11-01T06:00:00Z")), timedelta(hours=-5))

    def test_regular_close_in_est(self):
        self.assertEqual(us_eastern_local(_task4_utc("2026-01-05T21:00:00Z")).time(), datetime_time(16, 0))
        self.assertEqual(us_equity_session_close(date(2026, 1, 5)), datetime_time(16, 0))

    def test_regular_close_in_edt(self):
        self.assertEqual(us_eastern_local(_task4_utc("2026-07-06T20:00:00Z")).time(), datetime_time(16, 0))
        self.assertEqual(us_equity_session_close(date(2026, 7, 6)), datetime_time(16, 0))


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
        record = cli_module._build_signal_observation_records(
            [_task3_decision(snapshot)],
            snapshots_by_symbol={snapshot.symbol: snapshot},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )[0]
        self.assertEqual(
            resolve_signal_price_basis_status(record=record),
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


def _task3_projection_snapshot() -> AssetSnapshot:
    return AssetSnapshot(
        symbol="AMD",
        asset_type="stock",
        theme="semiconductors",
        candles=[
            Candle("2026-08-27", 100.0, 103.0, 99.0, 102.0, 1_000_000.0),
            Candle("2026-08-28", 102.0, 105.0, 101.0, 104.0, 1_200_000.0),
        ],
        fundamentals=Fundamentals(
            pe=30.0,
            peg=1.5,
            historical_pe=28.0,
            revenue_growth=0.2,
            eps_growth=0.25,
            margin_trend=0.1,
            free_cash_flow_positive=True,
            market_cap=1_000_000_000.0,
            average_volume=1_100_000.0,
            market_cap_rank=12,
        ),
        event=EventInfo(5, False, 0.02, "2026-05-28", "2026-09-01"),
        missing_data=["macro_not_collected", "news_not_collected"],
        news_events=[
            {"headline": "confirmed catalyst", "news_event_type": "news"},
            {"headline": "SEC filing", "news_event_type": "sec_filing"},
        ],
        provider_capabilities=[
            ProviderCapability(
                "fmp", "historical_price_eod_full", True, True, True,
                "available", False,
            )
        ],
        earnings_status="available",
        guidance_status="not_implemented",
        macro_status="not_implemented",
        news_status="available",
        sec_filings_status="available",
        data_source="fmp",
        data_timestamp="2026-08-28T20:00:00Z",
        cache_age_seconds=0,
        data_fetch_metadata=DataFetchMetadata(
            provider="fmp",
            endpoint="/stable/historical-price-eod/full",
            fetched_at="2026-08-28T20:00:03Z",
            source_timestamp="2026-08-28",
            cache_age_seconds=0,
            source_age_seconds=0,
            is_fresh=True,
            cache_hit=False,
            fallback_used=False,
            granularity="1d",
            market_data_kind="historical",
            price_basis_claim=qualified_fmp_full_price_basis_claim(),
        ),
        quote_status="available",
        quote_price=104.0,
        quote_timestamp="2026-08-28T20:00:04Z",
        quote_source="fmp",
        quote_age_seconds=2,
        quote_is_intraday=False,
        previous_close=102.0,
        daily_change=2.0,
        daily_change_pct=0.0196078431372549,
        benchmark_provenance={
            "sector": {"symbol": "SMH", "status": "available", "relative_strength": 0.12}
        },
        crypto_metric_provenance={},
    )


def _task3_literal_projection() -> dict[str, object]:
    return {
        "projection_version": "snapshot_projection_v1",
        "symbol": "AMD", "asset_type": "stock", "theme": "semiconductors",
        "candles": [
            {"date": "2026-08-27", "open": 100.0, "high": 103.0, "low": 99.0, "close": 102.0, "volume": 1_000_000.0},
            {"date": "2026-08-28", "open": 102.0, "high": 105.0, "low": 101.0, "close": 104.0, "volume": 1_200_000.0},
        ],
        "fundamentals": {"pe": 30.0, "peg": 1.5, "historical_pe": 28.0, "revenue_growth": 0.2, "eps_growth": 0.25, "margin_trend": 0.1, "free_cash_flow_positive": True, "market_cap": 1_000_000_000.0, "average_volume": 1_100_000.0, "market_cap_rank": 12},
        "event": {"days_to_earnings": 5, "guidance_recent": False, "post_earnings_gap_percent": 0.02, "last_earnings_date": "2026-05-28", "next_earnings_date": "2026-09-01"},
        "funding_rate": None, "open_interest_change": None, "cvd_proxy": None,
        "coinbase_premium": None, "liquidation_imbalance": None,
        "missing_data": ["macro_not_collected", "news_not_collected"],
        "news_events": [
            {"headline": "confirmed catalyst", "news_event_type": "news"},
            {"headline": "SEC filing", "news_event_type": "sec_filing"},
        ],
        "provider_capabilities": [{"provider": "fmp", "capability": "historical_price_eod_full", "configured": True, "supported_by_plan": True, "implemented": True, "last_status": "available", "fallback_available": False}],
        "earnings_status": "available", "guidance_status": "not_implemented",
        "macro_status": "not_implemented", "news_status": "available",
        "sec_filings_status": "available", "data_source": "fmp",
        "data_timestamp": "2026-08-28T20:00:00Z", "cache_age_seconds": 0,
        "data_fetch_metadata": {
            "provider": "fmp", "endpoint": "/stable/historical-price-eod/full",
            "fetched_at": "2026-08-28T20:00:03Z", "cache_fetched_at": None,
            "source_timestamp": "2026-08-28", "cache_age_seconds": 0,
            "source_age_seconds": 0, "is_fresh": True, "cache_hit": False,
            "fallback_used": False, "fallback_from": None, "fallback_to": None,
            "granularity": "1d", "market_data_kind": "historical",
            "price_basis_claim": {"price_basis": "raw_ohlcv", "price_basis_policy_version": "price_basis_v1", "source_contract": "fmp.historical_price_eod.full.raw_ohlcv_v1"},
        },
        "quote_status": "available", "quote_price": 104.0,
        "quote_timestamp": "2026-08-28T20:00:04Z", "quote_source": "fmp",
        "quote_age_seconds": 2, "quote_is_intraday": False,
        "previous_close": 102.0, "daily_change": 2.0,
        "daily_change_pct": 0.0196078431372549,
        "benchmark_provenance": {"sector": {"symbol": "SMH", "status": "available", "relative_strength": 0.12}},
        "crypto_metric_provenance": {},
    }


class SnapshotProjectionTests(unittest.TestCase):
    def test_snapshot_projection_v1_matches_literal_expected_projection(self):
        snapshot = _task3_projection_snapshot()
        expected_projection = _task3_literal_projection()

        self.assertEqual(snapshot_projection_v1(snapshot), expected_projection)
        self.assertEqual(
            snapshot_sha256_v1(snapshot),
            hashlib.sha256(canonical_json_bytes(expected_projection)).hexdigest(),
        )
        self.assertNotEqual(
            snapshot_sha256_v1(replace(snapshot, candles=list(reversed(snapshot.candles)))),
            snapshot_sha256_v1(snapshot),
        )
        changed_claim = replace(
            snapshot.data_fetch_metadata.price_basis_claim,
            source_contract=qualified_binance_klines_basis_claim().source_contract,
        )
        self.assertNotEqual(
            snapshot_sha256_v1(replace(snapshot, data_fetch_metadata=replace(snapshot.data_fetch_metadata, price_basis_claim=changed_claim))),
            snapshot_sha256_v1(snapshot),
        )

    def test_snapshot_projection_v1_is_closed_world(self):
        @dataclass(frozen=True)
        class FutureAssetSnapshot(AssetSnapshot):
            future_field: str = "sentinel"

        snapshot = _task3_projection_snapshot()
        future_snapshot = FutureAssetSnapshot(
            **{
                name: getattr(snapshot, name)
                for name in AssetSnapshot.__dataclass_fields__
            }
        )

        self.assertEqual(snapshot_projection_v1(future_snapshot), _task3_literal_projection())
        self.assertEqual(
            canonical_json_bytes(snapshot_projection_v1(future_snapshot)),
            canonical_json_bytes(snapshot_projection_v1(snapshot)),
        )
        self.assertNotIn("future_field", snapshot_projection_v1(future_snapshot))


class ObservationSourceBindingTests(unittest.TestCase):
    def test_atomic_record_captures_observation_and_binding_from_same_snapshot(self):
        snapshot = _task3_projection_snapshot()
        decision = _task3_decision(snapshot)

        records = cli_module._build_signal_observation_records(
            [decision],
            snapshots_by_symbol={snapshot.symbol: snapshot},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertIsInstance(record, ObservationEvidenceRecord)
        self.assertIsInstance(record.source_binding, ObservationSourceBinding)
        self.assertEqual(record.observation.signal_id, record.source_binding.signal_id)
        self.assertEqual(
            record.observation.observation_hash,
            record.source_binding.observation_hash,
        )
        self.assertEqual(
            record.source_binding.snapshot_sha256,
            hashlib.sha256(canonical_json_bytes(_task3_literal_projection())).hexdigest(),
        )

    def test_binding_captures_exact_price_basis_claim(self):
        snapshot = _task3_projection_snapshot()

        record = cli_module._build_signal_observation_records(
            [_task3_decision(snapshot)],
            snapshots_by_symbol={snapshot.symbol: snapshot},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )[0]

        self.assertEqual(
            record.source_binding.price_basis_claim,
            snapshot.data_fetch_metadata.price_basis_claim,
        )

    def test_resolver_accepts_only_observation_evidence_record(self):
        qualified_snapshot = _task3_snapshot(
            claim=qualified_fmp_full_price_basis_claim(),
        )
        qualified_record = cli_module._build_signal_observation_records(
            [_task3_decision(qualified_snapshot)],
            snapshots_by_symbol={qualified_snapshot.symbol: qualified_snapshot},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )[0]
        self.assertEqual(
            resolve_signal_price_basis_status(record=qualified_record),
            "verified_raw_ohlcv",
        )

        unqualified_snapshot = _task3_snapshot()
        unqualified_record = cli_module._build_signal_observation_records(
            [_task3_decision(unqualified_snapshot)],
            snapshots_by_symbol={unqualified_snapshot.symbol: unqualified_snapshot},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )[0]
        self.assertEqual(
            resolve_signal_price_basis_status(record=unqualified_record),
            "signal_basis_unavailable",
        )

        parameters = inspect.signature(resolve_signal_price_basis_status).parameters
        self.assertEqual(list(parameters), ["record"])
        self.assertEqual(parameters["record"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_sidecar_orders_multiple_records_by_signal_id(self):
        snapshot_high = _task3_snapshot(
            symbol="ZZZ",
            claim=qualified_fmp_full_price_basis_claim(),
        )
        snapshot_low = _task3_snapshot(
            symbol="AAA",
            claim=qualified_fmp_full_price_basis_claim(),
        )
        records = cli_module._build_signal_observation_records(
            [_task3_decision(snapshot_high), _task3_decision(snapshot_low)],
            snapshots_by_symbol={
                snapshot_high.symbol: snapshot_high,
                snapshot_low.symbol: snapshot_low,
            },
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )
        record_high, record_low = records
        self.assertGreater(
            record_high.observation.signal_id,
            record_low.observation.signal_id,
        )
        expected_signal_ids = sorted(
            [
                record_high.observation.signal_id,
                record_low.observation.signal_id,
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            high_first_path = root / "high-first.json.gz"
            low_first_path = root / "low-first.json.gz"
            build_observation_sidecar(
                records=[record_high, record_low],
                output_path=high_first_path,
            )
            build_observation_sidecar(
                records=[record_low, record_high],
                output_path=low_first_path,
            )
            high_first_payload = _read_task3_sidecar(high_first_path)
            low_first_payload = _read_task3_sidecar(low_first_path)
            high_first_bytes = high_first_path.read_bytes()
            low_first_bytes = low_first_path.read_bytes()

        self.assertEqual(
            [
                entry["observation"]["signal_id"]
                for entry in high_first_payload["records"]
            ],
            expected_signal_ids,
        )
        self.assertEqual(high_first_payload, low_first_payload)
        self.assertEqual(high_first_bytes, low_first_bytes)

    def test_mutating_snapshot_state_after_record_construction_does_not_change_binding(self):
        snapshot_x = _task3_projection_snapshot()
        snapshots_by_symbol = {snapshot_x.symbol: snapshot_x}
        record = cli_module._build_signal_observation_records(
            [_task3_decision(snapshot_x)],
            snapshots_by_symbol=snapshots_by_symbol,
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )[0]
        captured_binding = record.source_binding
        snapshot_y = replace(
            snapshot_x,
            candles=[*snapshot_x.candles[:-1], replace(snapshot_x.candles[-1], close=999.0)],
            data_fetch_metadata=replace(
                snapshot_x.data_fetch_metadata,
                price_basis_claim=qualified_binance_klines_basis_claim(),
            ),
        )

        snapshots_by_symbol[snapshot_x.symbol] = snapshot_y

        self.assertEqual(record.source_binding, captured_binding)
        self.assertNotEqual(snapshot_sha256_v1(snapshot_y), captured_binding.snapshot_sha256)
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "observations.json.gz"
            build_observation_sidecar(records=[record], output_path=output_path)
            serialized_binding = _read_task3_sidecar(output_path)["records"][0][
                "source_binding"
            ]

        self.assertEqual(serialized_binding["snapshot_sha256"], captured_binding.snapshot_sha256)
        self.assertEqual(
            serialized_binding["price_basis_claim"]["source_contract"],
            captured_binding.price_basis_claim.source_contract,
        )

    def test_sidecar_builder_accepts_records_without_snapshots_by_symbol(self):
        snapshot = _task3_projection_snapshot()
        record = cli_module._build_signal_observation_records(
            [_task3_decision(snapshot)],
            snapshots_by_symbol={snapshot.symbol: snapshot},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )[0]
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "observations.json.gz"
            self.assertEqual(
                build_observation_sidecar(records=[record], output_path=output_path),
                output_path,
            )
            sidecar = _read_task3_sidecar(output_path)

        self.assertEqual(
            set(sidecar), {"schema_version", "source_sha", "run_id", "report_type", "records"}
        )
        self.assertEqual(len(sidecar["records"]), 1)
        self.assertEqual(sidecar["records"][0]["source_binding"]["signal_id"], record.observation.signal_id)

    def test_sidecar_serializes_prebuilt_binding_without_snapshot_recomputation(self):
        snapshot = _task3_projection_snapshot()
        record = cli_module._build_signal_observation_records(
            [_task3_decision(snapshot)],
            snapshots_by_symbol={snapshot.symbol: snapshot},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )[0]
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "observations.json.gz"
            with patch(
                "advisor.evidence_schema.snapshot_sha256_v1",
                side_effect=AssertionError("snapshot_sha256_v1 must not be called by sidecar builder"),
            ):
                build_observation_sidecar(records=[record], output_path=output_path)
            sidecar = _read_task3_sidecar(output_path)

        self.assertEqual(
            sidecar["records"][0]["source_binding"]["snapshot_sha256"],
            record.source_binding.snapshot_sha256,
        )

    def test_same_record_serializes_deterministically(self):
        snapshot = _task3_projection_snapshot()
        record = cli_module._build_signal_observation_records(
            [_task3_decision(snapshot)],
            snapshots_by_symbol={snapshot.symbol: snapshot},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )[0]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_path = root / "first.json.gz"
            second_path = root / "second.json.gz"
            build_observation_sidecar(records=[record], output_path=first_path)
            build_observation_sidecar(records=[record], output_path=second_path)

            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
            self.assertEqual(
                decompress_single_member_gzip(
                    first_path.read_bytes(), max_uncompressed_bytes=4 * 1024 * 1024
                ),
                decompress_single_member_gzip(
                    second_path.read_bytes(), max_uncompressed_bytes=4 * 1024 * 1024
                ),
            )

    def test_sqlite_failure_preserves_prebuilt_binding(self):
        snapshot = _task3_projection_snapshot()
        record = cli_module._build_signal_observation_records(
            [_task3_decision(snapshot)],
            snapshots_by_symbol={snapshot.symbol: snapshot},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )[0]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = _task3_scan_args(root, db_name="sqlite.db", report_name="reports")
            with patch.object(
                cli_module,
                "_build_signal_observation_records",
                return_value=[record],
            ), patch.object(
                cli_module.SQLiteCache,
                "save_signal_observations",
                side_effect=OSError("sqlite unavailable"),
            ):
                self.assertEqual(cli_module._scan(args), 0)
            sidecar = _read_task3_sidecar(
                root / "reports" / "evidence" / "observations.json.gz"
            )

        binding = sidecar["records"][0]["source_binding"]
        self.assertEqual(binding["snapshot_sha256"], record.source_binding.snapshot_sha256)
        self.assertEqual(binding["observation_hash"], record.observation.observation_hash)

    def test_record_construction_failure_emits_no_partial_sidecar(self):
        snapshot_x = _task3_snapshot("D1", claim=qualified_fmp_full_price_basis_claim())
        snapshot_y = replace(snapshot_x, symbol="D2")
        decision_x = _task3_decision(snapshot_x)
        decision_y = _task3_decision(snapshot_y)
        nominal_records = cli_module._build_signal_observation_records(
            [decision_x],
            snapshots_by_symbol={snapshot_x.symbol: snapshot_x},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )
        self.assertEqual(len(nominal_records), 1)
        normal_observation = nominal_records[0].observation
        calls = {"count": 0}

        def fail_second_observation(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return normal_observation
            raise ValueError("invalid observation D2")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = _task3_scan_args(root, db_name="construction.db", report_name="reports")
            regimes = SimpleNamespace(
                stock=SimpleNamespace(label="bull"),
                crypto=SimpleNamespace(label="neutral"),
            )
            output = StringIO()
            with patch.object(cli_module, "snapshots_from_fixture", return_value=[snapshot_x, snapshot_y]), patch.object(
                cli_module, "benchmarks_from_fixture", return_value=[]
            ), patch.object(cli_module, "derive_market_regimes", return_value=regimes), patch.object(
                cli_module, "build_signal_observation", side_effect=fail_second_observation
            ), patch.object(
                cli_module,
                "_build_signal_observation_records",
                wraps=cli_module._build_signal_observation_records,
            ) as record_builder, patch.object(
                cli_module, "_persist_signal_observations"
            ) as persist, patch.object(
                cli_module, "build_observation_sidecar"
            ) as sidecar, patch.object(cli_module, "LiveDataLoader") as live_loader, redirect_stdout(output):
                self.assertEqual(cli_module._scan(args), 0)

            self.assertTrue((root / "reports" / "advisor-report.md").exists())
            self.assertFalse((root / "reports" / "evidence" / "observations.json.gz").exists())
            record_builder.assert_called_once()
            persist.assert_not_called()
            sidecar.assert_not_called()
            live_loader.assert_not_called()
            self.assertIn(
                "signal_observation_status=unavailable error_code=serialization_error",
                output.getvalue(),
            )


class ObservationSidecarTests(unittest.TestCase):
    def test_sidecar_binds_basis_by_signal_id_and_observation_hash(self):
        snapshot = _task3_snapshot(claim=qualified_fmp_full_price_basis_claim())
        record = cli_module._build_signal_observation_records(
            [_task3_decision(snapshot)],
            snapshots_by_symbol={snapshot.symbol: snapshot},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )[0]
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "observations.json.gz"
            self.assertEqual(
                build_observation_sidecar(records=[record], output_path=output_path),
                output_path,
            )
            sidecar = _read_task3_sidecar(output_path)

        self.assertEqual(sidecar["records"][0]["observation"]["signal_id"], record.observation.signal_id)
        self.assertEqual(
            resolve_signal_price_basis_status(
                record=record,
            ),
            "verified_raw_ohlcv",
        )

    def test_resolver_rejects_unknown_observation_hash_binding(self):
        snapshot = _task3_snapshot(claim=qualified_fmp_full_price_basis_claim())
        record = cli_module._build_signal_observation_records(
            [_task3_decision(snapshot)],
            snapshots_by_symbol={snapshot.symbol: snapshot},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )[0]
        self.assertEqual(
            resolve_signal_price_basis_status(
                record=replace(
                    record,
                    observation=replace(record.observation, observation_hash="f" * 64),
                ),
            ),
            "signal_basis_unavailable",
        )


_TASK5_DEFAULT_CLAIM = object()


def _task5_record(
    *,
    symbol: str = "AAPL",
    asset_type: str = "stock",
    claim: PriceBasisClaim | None | object = _TASK5_DEFAULT_CLAIM,
) -> ObservationEvidenceRecord:
    if claim is _TASK5_DEFAULT_CLAIM:
        claim = (
            qualified_fmp_full_price_basis_claim()
            if asset_type == "stock"
            else qualified_binance_klines_basis_claim()
        )
    provider = "fmp" if asset_type == "stock" else "binance"
    snapshot = _task3_snapshot(
        symbol,
        asset_type=asset_type,
        provider=provider,
        claim=claim if isinstance(claim, PriceBasisClaim) else None,
    )
    observation = _task3_observation(snapshot)
    return ObservationEvidenceRecord(
        observation=observation,
        source_binding=ObservationSourceBinding(
            signal_id=observation.signal_id,
            observation_hash=observation.observation_hash,
            snapshot_sha256=snapshot_sha256_v1(snapshot),
            price_basis_claim=claim if isinstance(claim, PriceBasisClaim) else None,
        ),
    )


def _task5_observation_envelope(record: ObservationEvidenceRecord) -> dict[str, object]:
    observation = asdict(record.observation)
    observation["reason_codes"] = list(record.observation.reason_codes)
    observation["persisted_at_utc"] = None
    claim = record.source_binding.price_basis_claim
    source_binding = {
        "signal_id": record.source_binding.signal_id,
        "observation_hash": record.source_binding.observation_hash,
        "snapshot_sha256": record.source_binding.snapshot_sha256,
        "price_basis_claim": (
            None
            if claim is None
            else {
                "price_basis": claim.price_basis,
                "price_basis_policy_version": claim.price_basis_policy_version,
                "source_contract": claim.source_contract,
            }
        ),
    }
    payload = observation
    logical_identity = {
        "report_type": record.observation.report_type,
        "run_id": record.observation.run_id,
        "schema_version": record.observation.schema_version,
        "source_sha": record.observation.source_sha,
        "symbol": record.observation.asset,
    }
    semantic_provenance = {
        "binding_contract": "observation_snapshot_binding_v1",
        "source_binding": source_binding,
    }
    return {
        "canonical_content_sha256": canonical_content_sha256(
            evidence_type="observation",
            schema_version="1.0",
            logical_identity=logical_identity,
            payload=payload,
            semantic_provenance=semantic_provenance,
        ),
        "evidence_type": "observation",
        "logical_identity": logical_identity,
        "payload": payload,
        "payload_sha256": payload_sha256(payload),
        "schema_version": "1.0",
        "semantic_provenance": semantic_provenance,
    }


def _task5_market_dates(count: int = 40) -> list[str]:
    values: list[str] = []
    current = date(2026, 9, 1)
    while len(values) < count:
        if current.weekday() < 5 and current != date(2026, 9, 7):
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _task5_market_envelope(
    *,
    symbol: str,
    market_date: str,
    asset_type: str = "stock",
    provider: str = "fmp",
    close: float = 100.0,
) -> dict[str, object]:
    market_timezone = "UTC" if asset_type == "crypto" else "America/New_York"
    logical_identity = {
        "asset_type": asset_type,
        "interval": "1d",
        "market_date": market_date,
        "market_timezone": market_timezone,
        "schema_version": "1.0",
        "symbol": symbol,
    }
    payload = {
        "asset_type": asset_type,
        "interval": "1d",
        "market_date": market_date,
        "market_timezone": market_timezone,
        "ohlcv": {
            "close": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "open": close - 0.5,
            "volume": 123456.0,
        },
        "price_basis": "raw_ohlcv",
        "session_close_type": "regular",
        "session_status": "complete",
    }
    claim = (
        qualified_hyperliquid_candle_snapshot_basis_claim()
        if provider == "hyperliquid"
        else qualified_binance_klines_basis_claim()
        if provider == "binance"
        else qualified_fmp_full_price_basis_claim()
    )
    semantic_provenance = {
        "collection_policy_version": "price_provider_assignment_v1",
        "price_basis_claim": {
            "price_basis": claim.price_basis,
            "price_basis_policy_version": claim.price_basis_policy_version,
            "source_contract": claim.source_contract,
        },
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


def _task5_conflict_document(
    *,
    symbol: str,
    coverage_start: str,
    coverage_end: str,
) -> tuple[str, dict[str, object]]:
    logical_identity = {
        "asset_type": "stock",
        "corporate_action_provider": "alpha_vantage",
        "coverage_end_date": coverage_end,
        "coverage_start_date": coverage_start,
        "function": "SPLITS",
        "schema_version": "1.0",
        "symbol": symbol,
    }
    identity = {
        "evidence_type": "corporate_action",
        "existing_canonical_sha256": "a" * 64,
        "incoming_canonical_sha256": "b" * 64,
        "logical_identity": logical_identity,
        "reason_code": "corporate_action_revision_conflict",
        "schema_version": "1.0",
    }
    identity_sha = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    content = {
        **identity,
        "existing_bytes_sha256": "c" * 64,
        "incoming_bytes_sha256": "d" * 64,
        "logical_identity_sha256": hashlib.sha256(
            canonical_json_bytes(logical_identity)
        ).hexdigest(),
    }
    path = (
        f"evidence/conflicts/{coverage_start[:4]}/{coverage_start[5:7]}/"
        f"{coverage_start[8:10]}/{identity_sha}.json.gz"
    )
    return path, content


def _task5_write_checkout(
    root: Path,
    envelopes: list[dict[str, object]],
    *,
    conflicts: list[tuple[str, dict[str, object]]] | None = None,
) -> Path:
    evidence_root = root / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / "branch-schema.json").write_bytes(
        canonical_json_bytes(
            {
                "branch_name": "advisor-evidence",
                "financial_evidence_count": 0,
                "schema_version": "1.0",
            }
        )
    )
    directory_by_type = {
        "observation": "observations",
        "market_bar": "market-bars",
        "corporate_action": "corporate-actions",
        "horizon_proof": "horizon-proofs",
        "outcome": "outcomes",
    }
    for envelope in envelopes:
        evidence_type = envelope["evidence_type"]
        logical_identity = envelope["logical_identity"]
        if evidence_type == "observation":
            partition_date = envelope["payload"]["report_date_brt"]
        elif evidence_type == "market_bar":
            partition_date = logical_identity["market_date"]
        elif evidence_type == "corporate_action":
            partition_date = logical_identity["coverage_end_date"]
        else:
            partition_date = envelope["payload"]["horizon_end_date"]
        identity_sha = hashlib.sha256(canonical_json_bytes(logical_identity)).hexdigest()
        relative = (
            f"evidence/{directory_by_type[evidence_type]}/{partition_date[:4]}/"
            f"{partition_date[5:7]}/{partition_date[8:10]}/{identity_sha}.json.gz"
        )
        path = root.joinpath(*Path(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(deterministic_gzip(canonical_json_bytes(envelope)))
    for relative, content in conflicts or []:
        path = root.joinpath(*Path(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(deterministic_gzip(canonical_json_bytes(content)))
    return root


def _task5_stock_envelopes(
    record: ObservationEvidenceRecord,
    *,
    events: list[dict[str, object]] | None = None,
    include_corporate: bool = True,
    market_dates: list[str] | None = None,
) -> tuple[list[dict[str, object]], list[str]]:
    dates = market_dates or _task5_market_dates()
    envelopes = [_task5_observation_envelope(record)]
    envelopes.extend(
        _task5_market_envelope(
            symbol=record.observation.asset,
            market_date=market_date,
            close=100.0 + index,
        )
        for index, market_date in enumerate(dates)
    )
    if include_corporate:
        envelopes.append(
            _corporate_action_envelope(
                symbol=record.observation.asset,
                coverage_start="2026-08-31",
                coverage_end=dates[-1],
                events=events if events is not None else [],
            )
        )
    return envelopes, dates


def _task6_all_stock_qualifications() -> dict[int, HorizonQualification]:
    return {
        horizon: HorizonQualification(
            status="verified_none",
            policy="verified_no_split_in_signal_horizon_v1",
            reason_code=None,
            proof={},
        )
        for horizon in (5, 10, 20, 40)
    }


def _task6_pending_after_h5_qualifications() -> dict[int, HorizonQualification]:
    return {
        5: HorizonQualification(
            status="verified_none",
            policy="verified_no_split_in_signal_horizon_v1",
            reason_code=None,
            proof={},
        ),
        10: HorizonQualification(
            status="pending",
            policy=None,
            reason_code=None,
            proof=None,
        ),
        20: HorizonQualification(
            status="pending",
            policy=None,
            reason_code=None,
            proof=None,
        ),
        40: HorizonQualification(
            status="pending",
            policy=None,
            reason_code=None,
            proof=None,
        ),
    }


class HorizonQualificationTests(unittest.TestCase):
    def _materializer(self, root: Path) -> EvidenceMaterializer:
        return EvidenceMaterializer(
            evidence_checkout=root,
            db_path=root.parent / "materialization.db",
        )

    def _qualified(self, root: Path, record: ObservationEvidenceRecord, horizon: int):
        materializer = self._materializer(root)
        canonical_record = materializer.read_observation_evidence_record(
            signal_id=record.observation.signal_id,
            observation_hash=record.observation.observation_hash,
        )
        return materializer.qualify_horizon(
            record=canonical_record,
            horizon=horizon,
        )

    def test_unknown_signal_basis_blocks_stock_maturation_before_3b2(self):
        record = _task5_record(claim=None)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            envelopes, _ = _task5_stock_envelopes(record)
            _task5_write_checkout(root, envelopes)
            with patch("advisor.signal_outcome.evaluate_signal_observation") as evaluator:
                qualification = self._qualified(root, record, 5)

        self.assertEqual(qualification.status, "signal_basis_unavailable")
        self.assertIsNone(qualification.proof)
        evaluator.assert_not_called()

    def test_verified_raw_ohlcv_is_required_for_stock_verified_none(self):
        record = _task5_record(claim=qualified_fmp_full_price_basis_claim())
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            envelopes, _ = _task5_stock_envelopes(record)
            _task5_write_checkout(root, envelopes)
            qualification = self._qualified(root, record, 5)

        self.assertEqual(qualification.status, "verified_none")
        self.assertEqual(qualification.policy, "verified_no_split_in_signal_horizon_v1")

    def test_split_on_signal_market_date_blocks_horizon(self):
        record = _task5_record()
        event = {
            "effective_date": "2026-08-31",
            "split_factor_raw": "2.0000",
            "split_ratio": {"new_shares": "2", "old_shares": "1"},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            envelopes, _ = _task5_stock_envelopes(record, events=[event])
            _task5_write_checkout(root, envelopes)
            qualification = self._qualified(root, record, 5)

        self.assertEqual(qualification.status, "split_in_horizon_unavailable")

    def test_split_on_horizon_end_date_blocks_horizon(self):
        record = _task5_record()
        event = {
            "effective_date": "2026-09-08",
            "split_factor_raw": "2.0000",
            "split_ratio": {"new_shares": "2", "old_shares": "1"},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            envelopes, _ = _task5_stock_envelopes(record, events=[event])
            _task5_write_checkout(root, envelopes)
            qualification = self._qualified(root, record, 5)

        self.assertEqual(qualification.status, "split_in_horizon_unavailable")

    def test_split_after_h5_before_h10_blocks_only_h10_and_later(self):
        record = _task5_record()
        event = {
            "effective_date": "2026-09-09",
            "split_factor_raw": "2.0000",
            "split_ratio": {"new_shares": "2", "old_shares": "1"},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            envelopes, _ = _task5_stock_envelopes(record, events=[event])
            _task5_write_checkout(root, envelopes)
            h5 = self._qualified(root, record, 5)
            h10 = self._qualified(root, record, 10)
            h20 = self._qualified(root, record, 20)

        self.assertEqual(h5.status, "verified_none")
        self.assertEqual(h10.status, "split_in_horizon_unavailable")
        self.assertEqual(h20.status, "split_in_horizon_unavailable")

    def test_no_split_produces_verified_none_with_split_policy(self):
        record = _task5_record()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            envelopes, _ = _task5_stock_envelopes(record, events=[])
            observation_envelope = envelopes[0]
            _task5_write_checkout(root, envelopes)
            qualification = self._qualified(root, record, 5)

        self.assertEqual(qualification.status, "verified_none")
        self.assertEqual(
            qualification.proof["corporate_action_policy"],
            "verified_no_split_in_signal_horizon_v1",
        )
        self.assertEqual(
            qualification.proof["evidence_hashes"]["observation_shard"],
            observation_envelope["canonical_content_sha256"],
        )

    def test_status_and_proof_policy_are_distinct(self):
        stock_record = _task5_record()
        crypto_record = _task5_record(
            symbol="BTC",
            asset_type="crypto",
            claim=qualified_binance_klines_basis_claim(),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            stock_envelopes, _ = _task5_stock_envelopes(stock_record, events=[])
            crypto_dates = [
                value.isoformat()
                for value in (date(2026, 9, 1) + timedelta(days=index) for index in range(40))
            ]
            crypto_envelopes = [_task5_observation_envelope(crypto_record)]
            crypto_envelopes.extend(
                _task5_market_envelope(
                    symbol="BTC",
                    asset_type="crypto",
                    provider="binance",
                    market_date=market_date,
                    close=200.0 + index,
                )
                for index, market_date in enumerate(crypto_dates)
            )
            _task5_write_checkout(root, stock_envelopes + crypto_envelopes)
            stock_qualification = self._qualified(root, stock_record, 5)
            crypto_qualification = self._qualified(root, crypto_record, 5)

        self.assertEqual(stock_qualification.status, "verified_none")
        self.assertEqual(stock_qualification.policy, "verified_no_split_in_signal_horizon_v1")
        self.assertNotEqual(stock_qualification.status, stock_qualification.policy)
        self.assertEqual(crypto_qualification.status, "not_applicable")
        self.assertEqual(crypto_qualification.policy, "not_applicable_crypto_raw_ohlcv_v1")

    def test_feed_unavailable_is_not_verified_none(self):
        record = _task5_record()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            envelopes, dates = _task5_stock_envelopes(record, include_corporate=False)
            _task5_write_checkout(root, envelopes)
            feed_qualification = self._qualified(root, record, 5)
            missing_bar_envelopes, _ = _task5_stock_envelopes(
                record,
                events=[],
                market_dates=dates[:4],
            )
            missing_root = root.parent / "missing-bars"
            _task5_write_checkout(missing_root, missing_bar_envelopes)
            pending_qualification = self._materializer(missing_root).qualify_horizon(
                record=self._materializer(missing_root).read_observation_evidence_record(
                    signal_id=record.observation.signal_id,
                    observation_hash=record.observation.observation_hash,
                ),
                horizon=5,
            )

        self.assertEqual(feed_qualification.status, "feed_unavailable")
        self.assertIsNone(feed_qualification.proof)
        self.assertNotEqual(feed_qualification.status, "verified_none")
        self.assertEqual(pending_qualification.status, "pending")

    def test_relevant_corporate_action_conflict_blocks_new_verified_none(self):
        record = _task5_record()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            envelopes, _ = _task5_stock_envelopes(record, events=[])
            conflict = _task5_conflict_document(
                symbol=record.observation.asset,
                coverage_start="2026-08-31",
                coverage_end="2026-10-26",
            )
            _task5_write_checkout(root, envelopes, conflicts=[conflict])
            qualification = self._qualified(root, record, 5)

        self.assertEqual(qualification.status, "conflict")
        self.assertEqual(qualification.reason_code, "corporate_action_revision_conflict")
        self.assertIsNone(qualification.proof)

    def test_frozen_outcome_is_not_rewritten_by_new_corporate_revision(self):
        record = _task5_record()
        outcome_identity = {
            "evaluation_policy_version": "1.0",
            "horizon_bars": 5,
            "observation_hash": record.observation.observation_hash,
            "schema_version": "1.0",
            "signal_id": record.observation.signal_id,
        }
        outcome_payload = {
            "outcome_id": "outcome-fixture",
            "outcome_hash": "e" * 64,
            "horizon_end_date": "2026-09-08",
        }
        outcome_envelope = {
            "canonical_content_sha256": canonical_content_sha256(
                evidence_type="outcome",
                schema_version="1.0",
                logical_identity=outcome_identity,
                payload=outcome_payload,
                semantic_provenance={"source": "frozen"},
            ),
            "evidence_type": "outcome",
            "logical_identity": outcome_identity,
            "payload": outcome_payload,
            "payload_sha256": payload_sha256(outcome_payload),
            "schema_version": "1.0",
            "semantic_provenance": {"source": "frozen"},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            envelopes, _ = _task5_stock_envelopes(record, events=[])
            conflict = _task5_conflict_document(
                symbol=record.observation.asset,
                coverage_start="2026-08-31",
                coverage_end="2026-10-26",
            )
            _task5_write_checkout(root, envelopes + [outcome_envelope], conflicts=[conflict])
            outcome_path = next((root / "evidence" / "outcomes").rglob("*.json.gz"))
            before = outcome_path.read_bytes()
            qualification = self._qualified(root, record, 5)
            after = outcome_path.read_bytes()

        self.assertEqual(qualification.status, "conflict")
        self.assertEqual(before, after)

    def test_etf_evidence_never_enters_frozen_3b2_asset_types(self):
        record = _task5_record()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            envelopes, dates = _task5_stock_envelopes(record, events=[])
            envelopes.extend(
                _task5_market_envelope(
                    symbol="IGV",
                    asset_type="etf",
                    provider="fmp",
                    market_date=market_date,
                )
                for market_date in dates
            )
            _task5_write_checkout(root, envelopes)
            materializer = self._materializer(root)
            with self.assertRaises(ValueError) as error:
                materializer._forward_market_series(symbol="IGV", asset_type="etf")

        self.assertEqual(str(error.exception), "unsupported_frozen_asset_type")

    def test_materializer_has_no_unarchived_transport_input(self):
        parameters = inspect.signature(EvidenceMaterializer.__init__).parameters
        self.assertEqual(
            list(parameters),
            ["self", "evidence_checkout", "db_path"],
        )
        self.assertNotIn("transport_dir", parameters)
        self.assertNotIn("transport_path", parameters)

        record = _task5_record()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            canonical_envelopes, _ = _task5_stock_envelopes(record, events=[])
            self.assertNotIn("source_binding", canonical_envelopes[0]["payload"])
            self.assertIn("source_binding", canonical_envelopes[0]["semantic_provenance"])
            _task5_write_checkout(root, canonical_envelopes)
            with tempfile.TemporaryDirectory() as transport_directory:
                transport_root = Path(transport_directory)
                divergent = replace(
                    record,
                    source_binding=replace(
                        record.source_binding,
                        snapshot_sha256="f" * 64,
                    ),
                )
                (transport_root / "observations.json.gz").write_bytes(
                    deterministic_gzip(
                        canonical_json_bytes(_task5_observation_envelope(divergent))
                    )
                )
                materializer = self._materializer(root)
                canonical_record = materializer.read_observation_evidence_record(
                    signal_id=record.observation.signal_id,
                    observation_hash=record.observation.observation_hash,
                )
                qualification = materializer.qualify_horizon(
                    record=canonical_record,
                    horizon=5,
                )

        self.assertEqual(
            qualification.status,
            "verified_none",
        )


class _OutcomeMaturationMethods:
    def _materializer_and_record(self, root: Path) -> tuple[EvidenceMaterializer, ObservationEvidenceRecord]:
        record = _task5_record()
        envelopes, _ = _task5_stock_envelopes(record, events=[])
        _task5_write_checkout(root, envelopes)
        return (
            EvidenceMaterializer(evidence_checkout=root, db_path=root.parent / "maturation.db"),
            record,
        )

    def test_maturation_calls_frozen_evaluator_at_most_once_per_observation_cycle(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            materializer, record = self._materializer_and_record(Path(temporary_directory))
            series = materializer._forward_market_series(
                symbol=record.observation.asset,
                asset_type=record.observation.asset_type,
            )
            with patch(
                "advisor.signal_outcome.evaluate_signal_observation",
                wraps=__import__(
                    "advisor.signal_outcome", fromlist=["evaluate_signal_observation"]
                ).evaluate_signal_observation,
            ) as evaluator:
                result = materializer.evaluate_observation_once(
                    observation=record.observation,
                    series=series,
                    qualifications=_task6_all_stock_qualifications(),
                )

        self.assertEqual(evaluator.call_count, 1)
        self.assertEqual(len(result.outcomes), 4)

    def test_largest_continuously_eligible_prefix_limits_forward_series(self):
        qualifications = _task6_all_stock_qualifications()
        qualifications[10] = HorizonQualification(
            status="split_in_horizon_unavailable",
            policy="verified_no_split_in_signal_horizon_v1",
            reason_code=None,
            proof=None,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            materializer, record = self._materializer_and_record(Path(temporary_directory))
            series = materializer._forward_market_series(
                symbol=record.observation.asset,
                asset_type=record.observation.asset_type,
            )
            with patch(
                "advisor.signal_outcome.evaluate_signal_observation",
                return_value=__import__(
                    "advisor.signal_outcome", fromlist=["SignalForwardEvaluation"]
                ).SignalForwardEvaluation(outcomes=(), pending_horizons=(10, 20, 40)),
            ) as evaluator:
                materializer.evaluate_observation_once(
                    observation=record.observation,
                    series=series,
                    qualifications=qualifications,
                )

        self.assertEqual(materializer.largest_continuously_eligible_horizon(qualifications=qualifications), 5)
        self.assertEqual(len(evaluator.call_args.args[1].candles), 5)

    def test_maturation_does_not_call_scoring(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            materializer, record = self._materializer_and_record(Path(temporary_directory))
            series = materializer._forward_market_series(
                symbol=record.observation.asset,
                asset_type=record.observation.asset_type,
            )
            with patch("advisor.scoring.score_asset") as scoring, patch(
                "advisor.signal_outcome.evaluate_signal_observation",
                return_value=__import__(
                    "advisor.signal_outcome", fromlist=["SignalForwardEvaluation"]
                ).SignalForwardEvaluation(outcomes=(), pending_horizons=(5, 10, 20, 40)),
            ):
                materializer.evaluate_observation_once(
                    observation=record.observation,
                    series=series,
                    qualifications=_task6_all_stock_qualifications(),
                )

        scoring.assert_not_called()

    def test_maturation_does_not_call_risk(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            materializer, record = self._materializer_and_record(Path(temporary_directory))
            series = materializer._forward_market_series(
                symbol=record.observation.asset,
                asset_type=record.observation.asset_type,
            )
            with patch("advisor.risk.rate_sample_quality") as risk, patch(
                "advisor.signal_outcome.evaluate_signal_observation",
                return_value=__import__(
                    "advisor.signal_outcome", fromlist=["SignalForwardEvaluation"]
                ).SignalForwardEvaluation(outcomes=(), pending_horizons=(5, 10, 20, 40)),
            ):
                materializer.evaluate_observation_once(
                    observation=record.observation,
                    series=series,
                    qualifications=_task6_all_stock_qualifications(),
                )

        risk.assert_not_called()

    def test_maturation_stores_exact_frozen_evaluator_result(self):
        import advisor.signal_outcome as signal_outcome

        with tempfile.TemporaryDirectory() as temporary_directory:
            materializer, record = self._materializer_and_record(Path(temporary_directory))
            series = materializer._forward_market_series(
                symbol=record.observation.asset,
                asset_type=record.observation.asset_type,
            )
            expected = signal_outcome.evaluate_signal_observation(record.observation, series)
            result = materializer.evaluate_observation_once(
                observation=record.observation,
                series=series,
                qualifications=_task6_all_stock_qualifications(),
            )

        self.assertEqual(result, expected)
        self.assertEqual(result.outcomes, expected.outcomes)

    def test_ineligible_horizon_is_not_sent_to_frozen_evaluator(self):
        qualifications = _task6_all_stock_qualifications()
        qualifications[10] = HorizonQualification(
            status="split_in_horizon_unavailable",
            policy="verified_no_split_in_signal_horizon_v1",
            reason_code=None,
            proof=None,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            materializer, record = self._materializer_and_record(Path(temporary_directory))
            series = materializer._forward_market_series(
                symbol=record.observation.asset,
                asset_type=record.observation.asset_type,
            )
            with patch(
                "advisor.signal_outcome.evaluate_signal_observation",
                wraps=__import__(
                    "advisor.signal_outcome", fromlist=["evaluate_signal_observation"]
                ).evaluate_signal_observation,
            ) as evaluator:
                result = materializer.evaluate_observation_once(
                    observation=record.observation,
                    series=series,
                    qualifications=qualifications,
                )

        self.assertEqual(evaluator.call_count, 1)
        self.assertEqual(len(evaluator.call_args.args[1].candles), 5)
        self.assertNotIn(10, [outcome.horizon_bars for outcome in result.outcomes])

    def test_outcomes_for_externally_blocked_horizons_are_rejected(self):
        import advisor.signal_outcome as signal_outcome

        qualifications = _task6_all_stock_qualifications()
        qualifications[10] = HorizonQualification(
            status="split_in_horizon_unavailable",
            policy="verified_no_split_in_signal_horizon_v1",
            reason_code=None,
            proof=None,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            materializer, record = self._materializer_and_record(Path(temporary_directory))
            series = materializer._forward_market_series(
                symbol=record.observation.asset,
                asset_type=record.observation.asset_type,
            )
            evaluator_result = signal_outcome.evaluate_signal_observation(record.observation, series)
            with patch(
                "advisor.signal_outcome.evaluate_signal_observation",
                return_value=signal_outcome.SignalForwardEvaluation(
                    outcomes=evaluator_result.outcomes,
                    pending_horizons=(),
                ),
            ):
                result = materializer.evaluate_observation_once(
                    observation=record.observation,
                    series=series,
                    qualifications=qualifications,
                )

        self.assertNotIn(10, [outcome.horizon_bars for outcome in result.outcomes])
        self.assertIn(10, result.pending_horizons)

    def test_pending_horizons_are_preserved_without_recalculation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            materializer, record = self._materializer_and_record(Path(temporary_directory))
            series = materializer._forward_market_series(
                symbol=record.observation.asset,
                asset_type=record.observation.asset_type,
            )
            with patch(
                "advisor.signal_outcome.evaluate_signal_observation",
                wraps=__import__(
                    "advisor.signal_outcome", fromlist=["evaluate_signal_observation"]
                ).evaluate_signal_observation,
            ) as evaluator:
                result = materializer.evaluate_observation_once(
                    observation=record.observation,
                    series=series,
                    qualifications=_task6_pending_after_h5_qualifications(),
                )

        self.assertEqual(evaluator.call_count, 1)
        self.assertEqual(result.pending_horizons, (10, 20, 40))
        self.assertEqual([outcome.horizon_bars for outcome in result.outcomes], [5])


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


def _large_market_envelopes(count: int = 500) -> list[dict[str, object]]:
    start = date(2025, 1, 1)
    return [
        _market_envelope(
            symbol="AAPL",
            market_date=(start + timedelta(days=index)).isoformat(),
            close=100.0 + index,
        )
        for index in range(count)
    ]


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


def _task7_market_transport_record() -> dict[str, object]:
    return {
        "asset_type": "stock",
        "assigned_provider": "fmp",
        "assignment_policy_version": "price_provider_assignment_v1",
        "bars": [
            {
                "market_date": "2026-08-26",
                "ohlcv": {
                    "close": 101.0,
                    "high": 102.0,
                    "low": 99.0,
                    "open": 100.0,
                    "volume": 1000.0,
                },
                "price_basis": "raw_ohlcv",
                "session_close_type": "regular",
                "session_status": "complete",
            }
        ],
        "coverage_window": {
            "start_date": "2026-08-26",
            "end_date": "2026-08-26",
        },
        "interval": "1d",
        "market_timezone": "America/New_York",
        "semantic_provenance": {
            "collection_policy_version": "price_provider_assignment_v1",
            "price_basis_claim": {
                "price_basis": "raw_ohlcv",
                "price_basis_policy_version": "price_basis_v1",
                "source_contract": "fmp.historical_price_eod.full.raw_ohlcv_v1",
            },
            "price_provider": "fmp",
            "source_response_sha256": "0" * 64,
        },
        "source_request": {
            "interval": "1d",
            "provider": "fmp",
            "source_contract": "fmp.historical_price_eod.full.raw_ohlcv_v1",
        },
        "status": "available",
        "symbol": "AAPL",
    }


def _task7_corporate_transport_record() -> dict[str, object]:
    return {
        "asset_type": "stock",
        "coverage_window": {
            "start_date": "2020-01-01",
            "end_date": "2026-08-26",
        },
        "corporate_action_provider": "alpha_vantage",
        "function": "SPLITS",
        "normalized_events": [],
        "payload": {
            "data": [],
            "normalized_events": [],
            "symbol": "AAPL",
        },
        "semantic_provenance": {
            "corporate_action_provider": "alpha_vantage",
            "source_response_sha256": "1" * 64,
        },
        "source_request": {
            "function": "SPLITS",
            "provider": "alpha_vantage",
            "source_contract": "alpha_vantage.splits.v1",
        },
        "status": "available",
        "symbol": "AAPL",
    }


def _write_task7_raw_transport(
    directory: Path,
    *,
    market: bool = False,
    corporate: bool = False,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if market:
        (directory / "market-transport.json").write_text(
            json.dumps(
                {"records": [_task7_market_transport_record()]},
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    if corporate:
        (directory / "corporate-actions-transport.json").write_text(
            json.dumps(
                {"records": [_task7_corporate_transport_record()]},
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    return directory


class PackagingTests(_ArchiveRepositoryMixin, unittest.TestCase):
    @staticmethod
    def _package(transport_dir: Path, output_dir: Path) -> tuple[Path, ...]:
        try:
            from advisor.evidence_packager import package_evidence_transport
        except ModuleNotFoundError as error:
            raise AssertionError(
                "advisor.evidence_packager is not implemented"
            ) from error
        return package_evidence_transport(
            transport_dir=transport_dir,
            output_dir=output_dir,
        )

    def test_task3_observation_sidecar_is_packaged_and_recoverable_by_real_archive(self):
        snapshot = _task3_projection_snapshot()
        record = cli_module._build_signal_observation_records(
            [_task3_decision(snapshot)],
            snapshots_by_symbol={snapshot.symbol: snapshot},
            stock_regime="bull",
            crypto_regime="neutral",
            run_metadata=_task3_run_metadata(),
        )[0]

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sidecar_dir = root / "sidecar"
            sidecar_path = sidecar_dir / "observations.json.gz"
            build_observation_sidecar(records=[record], output_path=sidecar_path)

            self._bootstrap()
            raw_result = EvidenceArchive(
                repo_dir=self.caller,
                branch_name=self.branch_name,
            ).archive(sidecar_dir)
            self.assertEqual(raw_result.status, "rejected")
            self.assertEqual(raw_result.error_code, "invalid_transport_shard")
            self.assertFalse(raw_result.durability_confirmed)

            from advisor.evidence_packager import package_evidence_transport

            candidate_dir = root / "canonical-candidates"
            candidate_paths = package_evidence_transport(
                transport_dir=sidecar_dir,
                output_dir=candidate_dir,
            )
            self.assertEqual(len(candidate_paths), 1)
            self.assertEqual(candidate_paths[0].suffixes, [".json", ".gz"])

            result = EvidenceArchive(
                repo_dir=self.caller,
                branch_name=self.branch_name,
            ).archive(candidate_dir)
            self.assertIn(result.status, {"committed", "no_op"})
            self.assertTrue(result.durability_confirmed)

            checkout = self._clone_branch(root / "fresh-checkout")
            recovered = EvidenceMaterializer(
                evidence_checkout=checkout,
                db_path=root / "evidence.db",
            ).read_observation_evidence_record(
                signal_id=record.observation.signal_id,
                observation_hash=record.observation.observation_hash,
            )

        self.assertEqual(recovered.observation, record.observation)
        self.assertEqual(recovered.source_binding, record.source_binding)
        self.assertEqual(
            recovered.observation.observation_hash,
            record.observation.observation_hash,
        )
        self.assertEqual(
            recovered.source_binding.snapshot_sha256,
            record.source_binding.snapshot_sha256,
        )

    def test_market_transport_is_accepted_by_real_archive(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            transport_dir = _write_task7_raw_transport(
                Path(temporary_directory) / "transport",
                market=True,
            )
            candidate_dir = Path(temporary_directory) / "candidates"
            self._bootstrap()

            candidate_paths = self._package(transport_dir, candidate_dir)
            self.assertEqual(len(candidate_paths), 1)
            self.assertTrue(all(path.suffixes == [".json", ".gz"] for path in candidate_paths))
            envelope = strict_json_loads_bytes(
                decompress_single_member_gzip(
                    (candidate_dir / candidate_paths[0]).read_bytes(),
                    max_uncompressed_bytes=4 * 1024 * 1024,
                )
            )
            self.assertIsInstance(envelope, dict)
            self.assertEqual(envelope["evidence_type"], "market_bar")
            validate_canonical_envelope(envelope)

            result = EvidenceArchive(
                repo_dir=self.caller,
                branch_name=self.branch_name,
            ).archive(candidate_dir)

        self.assertEqual(result.status, "committed")
        self.assertTrue(result.durability_confirmed)
        self.assertTrue(any("evidence/market-bars/" in path for path in result.committed_paths))

    def test_corporate_transport_is_accepted_by_real_archive(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            transport_dir = _write_task7_raw_transport(
                Path(temporary_directory) / "transport",
                corporate=True,
            )
            candidate_dir = Path(temporary_directory) / "candidates"
            self._bootstrap()

            candidate_paths = self._package(transport_dir, candidate_dir)
            self.assertEqual(len(candidate_paths), 1)
            envelope = strict_json_loads_bytes(
                decompress_single_member_gzip(
                    (candidate_dir / candidate_paths[0]).read_bytes(),
                    max_uncompressed_bytes=4 * 1024 * 1024,
                )
            )
            self.assertIsInstance(envelope, dict)
            self.assertEqual(envelope["evidence_type"], "corporate_action")
            validate_canonical_envelope(envelope)

            result = EvidenceArchive(
                repo_dir=self.caller,
                branch_name=self.branch_name,
            ).archive(candidate_dir)

        self.assertEqual(result.status, "committed")
        self.assertTrue(result.durability_confirmed)
        self.assertTrue(any("evidence/corporate-actions/" in path for path in result.committed_paths))

    def test_packaging_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_transport = _write_task7_raw_transport(root / "transport-a", market=True, corporate=True)
            second_transport = _write_task7_raw_transport(root / "transport-b", market=True, corporate=True)
            first_output = root / "candidates-a"
            second_output = root / "candidates-b"

            first_paths = self._package(first_transport, first_output)
            second_paths = self._package(second_transport, second_output)

            self.assertEqual(first_paths, second_paths)
            self.assertTrue(all(not path.is_absolute() for path in first_paths))
            first_bytes = [(path, (first_output / path).read_bytes()) for path in first_paths]
            second_bytes = [(path, (second_output / path).read_bytes()) for path in second_paths]

        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(
            [
                strict_json_loads_bytes(
                    decompress_single_member_gzip(
                        data,
                        max_uncompressed_bytes=4 * 1024 * 1024,
                    )
                )
                for _, data in first_bytes
            ],
            [
                strict_json_loads_bytes(
                    decompress_single_member_gzip(
                        data,
                        max_uncompressed_bytes=4 * 1024 * 1024,
                    )
                )
                for _, data in second_bytes
            ],
        )

    def test_packaging_fails_closed_without_partial_candidates(self):
        cases = ("unknown", "malformed")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                transport_dir = _write_task7_raw_transport(root / "transport", market=True)
                invalid_path = transport_dir / (
                    "unknown-transport.json"
                    if case == "unknown"
                    else "corporate-actions-transport.json"
                )
                invalid_path.write_text(
                    '{"records":[]}' if case == "unknown" else "{",
                    encoding="utf-8",
                )
                output_dir = root / "candidates"

                with self.assertRaises(ValueError):
                    self._package(transport_dir, output_dir)

                self.assertEqual(list(output_dir.rglob("*.json.gz")), [])

    def test_collect_package_archive_operational_integration(self):
        def fetch_json(**kwargs):
            if kwargs["provider"] == "fmp":
                return {
                    "historical": [
                        {
                            "date": "2026-08-26",
                            "open": 100.0,
                            "high": 102.0,
                            "low": 99.0,
                            "close": 101.0,
                            "volume": 1000.0,
                        }
                    ]
                }
            if kwargs["provider"] == "alpha_vantage":
                return {"symbol": "AAPL", "data": []}
            raise AssertionError(f"unexpected provider: {kwargs['provider']}")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw_transport = root / "transport"
            with patch(
                "advisor.evidence_collector.AdvisorConfig.default",
                return_value=AdvisorConfig(
                    stock_watchlist=["AAPL"],
                    fmp_api_key="fmp-key",
                    alphavantage_api_key="alpha-key",
                ),
            ):
                collector = EvidenceCollector(
                    fetch_json=fetch_json,
                    transport_root=raw_transport,
                    now_utc=_task4_utc("2026-08-27T21:00:00Z"),
                )
                market_path = collector.collect_market(
                    assets=[CollectionAsset("AAPL", "stock")],
                    existing_provider_by_symbol={},
                )
                market_records = _task4_transport_records(market_path)
                coverage_windows = {
                    "AAPL": (
                        date.fromisoformat(market_records[0]["coverage_window"]["start_date"]),
                        date.fromisoformat(market_records[0]["coverage_window"]["end_date"]),
                    )
                }
                collector.collect_corporate_actions(
                    assets=[CollectionAsset("AAPL", "stock")],
                    coverage_windows=coverage_windows,
                )

            candidate_dir = root / "candidates"
            candidate_paths = self._package(raw_transport, candidate_dir)
            self.assertEqual(len(candidate_paths), 2)
            self._bootstrap()
            self.assertEqual(self._remote_tree(), ["evidence/branch-schema.json"])

            result = EvidenceArchive(
                repo_dir=self.caller,
                branch_name=self.branch_name,
            ).archive(candidate_dir)

        self.assertEqual(result.status, "committed")
        self.assertTrue(result.durability_confirmed)
        self.assertTrue(any("evidence/market-bars/" in path for path in result.committed_paths))
        self.assertTrue(any("evidence/corporate-actions/" in path for path in result.committed_paths))


def _task6_transport_envelopes(record: ObservationEvidenceRecord) -> list[dict[str, object]]:
    envelopes, _ = _task5_stock_envelopes(record, events=[])
    return envelopes


def _task6_outcome_conflict_envelope(
    record: ObservationEvidenceRecord,
    *,
    horizon_end_date: str = "2026-09-08",
) -> dict[str, object]:
    logical_identity = {
        "evaluation_policy_version": "1.0",
        "horizon_bars": 5,
        "observation_hash": record.observation.observation_hash,
        "schema_version": "1.0",
        "signal_id": record.observation.signal_id,
    }
    payload = {
        "outcome_hash": "a" * 64,
        "outcome_id": "b" * 64,
        "horizon_end_date": horizon_end_date,
        "source": "preexisting-conflict-fixture",
    }
    semantic_provenance = {"source": "preexisting-conflict-fixture"}
    return {
        "canonical_content_sha256": canonical_content_sha256(
            evidence_type="outcome",
            schema_version="1.0",
            logical_identity=logical_identity,
            payload=payload,
            semantic_provenance=semantic_provenance,
        ),
        "evidence_type": "outcome",
        "logical_identity": logical_identity,
        "payload": payload,
        "payload_sha256": payload_sha256(payload),
        "schema_version": "1.0",
        "semantic_provenance": semantic_provenance,
    }


def _task6_exact_horizon_proof_envelope() -> dict[str, object]:
    logical_identity = {
        "horizon_bars": 5,
        "observation_hash": "a" * 64,
        "schema_version": "1.0",
        "signal_id": "b" * 64,
    }
    payload = {
        "signal_market_date": "2026-08-31",
        "horizon_start_date": "2026-09-01",
        "horizon_end_date": "2026-09-08",
        "horizon_bars": 5,
        "corporate_action_policy": "verified_no_split_in_signal_horizon_v1",
        "corporate_action_provider": "alpha_vantage",
        "split_check_status": "verified_none",
        "split_event_count": 0,
        "price_basis": "split_adjusted_ohlc",
        "raw_price_basis": "raw_ohlcv",
        "signal_price_basis_status": "verified_raw_ohlcv",
        "evidence_hashes": {
            "observation_shard": "c" * 64,
            "market_bar_shards": ["d" * 64] * 5,
            "corporate_action_shard": "e" * 64,
        },
        "proof_status": "verified_none",
    }
    semantic_provenance = {"source": "task6-partition-fixture"}
    return {
        "canonical_content_sha256": canonical_content_sha256(
            evidence_type="horizon_proof",
            schema_version="1.0",
            logical_identity=logical_identity,
            payload=payload,
            semantic_provenance=semantic_provenance,
        ),
        "evidence_type": "horizon_proof",
        "logical_identity": logical_identity,
        "payload": payload,
        "payload_sha256": payload_sha256(payload),
        "schema_version": "1.0",
        "semantic_provenance": semantic_provenance,
    }


def _task6_exact_outcome_envelope() -> dict[str, object]:
    logical_identity = {
        "evaluation_policy_version": "1.0",
        "horizon_bars": 5,
        "observation_hash": "a" * 64,
        "schema_version": "1.0",
        "signal_id": "b" * 64,
    }
    payload = {
        "outcome_id": "f" * 64,
        "outcome_hash": "1" * 64,
        "horizon_end_date": "2026-09-08",
    }
    semantic_provenance = {
        "horizon_proof_shard": "2" * 64,
        "market_bar_shards": ["3" * 64] * 5,
    }
    return {
        "canonical_content_sha256": canonical_content_sha256(
            evidence_type="outcome",
            schema_version="1.0",
            logical_identity=logical_identity,
            payload=payload,
            semantic_provenance=semantic_provenance,
        ),
        "evidence_type": "outcome",
        "logical_identity": logical_identity,
        "payload": payload,
        "payload_sha256": payload_sha256(payload),
        "schema_version": "1.0",
        "semantic_provenance": semantic_provenance,
    }


def _task6_mature(
    *,
    repo_dir: Path,
    first_transport_dir: Path,
    evidence_checkout: Path,
    outcome_transport_dir: Path,
    db_path: Path,
):
    from advisor.evidence_materializer import mature_evidence_cycle

    return mature_evidence_cycle(
        repo_dir=repo_dir,
        first_transport_dir=first_transport_dir,
        evidence_checkout=evidence_checkout,
        outcome_transport_dir=outcome_transport_dir,
        db_path=db_path,
    )


def _task6_read_transport_records(directory: Path) -> list[dict[str, object]]:
    return [
        strict_json_loads_bytes(
            decompress_single_member_gzip(
                path.read_bytes(),
                max_uncompressed_bytes=4 * 1024 * 1024,
            )
        )
        for path in sorted(directory.glob("*.json.gz"))
    ]


class ArchiveDurabilityTests(_ArchiveRepositoryMixin, unittest.TestCase):
    def test_local_transport_cannot_satisfy_maturation_before_first_archive(self):
        record = _task5_record()
        first_transport = self._write_transport(_task6_transport_envelopes(record))
        outcome_transport = self.root / "outcome-transport"
        with patch("advisor.signal_outcome.evaluate_signal_observation") as evaluator:
            with self.assertRaises(MaterializationError) as error:
                _task6_mature(
                    repo_dir=self.caller,
                    first_transport_dir=first_transport,
                    evidence_checkout=self.root / "fresh-checkout",
                    outcome_transport_dir=outcome_transport,
                    db_path=self.root / "evidence.db",
                )

        self.assertEqual(str(error.exception), "first_archive_not_durable")
        evaluator.assert_not_called()
        self.assertFalse(outcome_transport.exists())

    def test_push_confirmation_and_fresh_read_are_required_before_maturation(self):
        self._bootstrap()
        record = _task5_record()
        first_transport = self._write_transport(_task6_transport_envelopes(record))
        db_path = self.root / "evidence.db"
        self.assertEqual(
            SQLiteCache(db_path).save_signal_observations((record.observation,)).status,
            "written",
        )
        outcome_transport = self.root / "outcome-transport"
        import advisor.signal_outcome as signal_outcome

        with patch(
            "advisor.signal_outcome.evaluate_signal_observation",
            wraps=signal_outcome.evaluate_signal_observation,
        ) as evaluator:
            result = _task6_mature(
                repo_dir=self.caller,
                first_transport_dir=first_transport,
                evidence_checkout=self.root / "fresh-checkout",
                outcome_transport_dir=outcome_transport,
                db_path=db_path,
            )

        self.assertEqual(evaluator.call_count, 1)
        self.assertTrue(result.outcome_transport)
        self.assertTrue(result.outcome_transport.exists())
        self.assertGreaterEqual(len(result.completed_horizons), 1)
        self.assertEqual(SQLiteCache(db_path).count_signal_forward_outcomes(), 4)


class ArchivePartitionCompatibilityTests(_ArchiveRepositoryMixin, unittest.TestCase):
    def test_horizon_proof_partitions_from_payload_date_without_identity_pollution(self):
        self._bootstrap()
        envelope = _task6_exact_horizon_proof_envelope()

        result = self._archive([envelope])

        self.assertEqual(result.status, "committed")
        self.assertTrue(result.durability_confirmed)
        self.assertIn(
            "evidence/horizon-proofs/2026/09/08/",
            "\n".join(self._remote_tree()),
        )
        self.assertNotIn("horizon_end_date", envelope["logical_identity"])

    def test_outcome_partitions_from_payload_date_without_identity_pollution(self):
        self._bootstrap()
        envelope = _task6_exact_outcome_envelope()

        result = self._archive([envelope])

        self.assertEqual(result.status, "committed")
        self.assertTrue(result.durability_confirmed)
        self.assertIn(
            "evidence/outcomes/2026/09/08/",
            "\n".join(self._remote_tree()),
        )
        self.assertNotIn("horizon_end_date", envelope["logical_identity"])

    def test_missing_or_invalid_horizon_partition_date_fails_closed(self):
        self._bootstrap()
        missing = _task6_exact_horizon_proof_envelope()
        del missing["payload"]["horizon_end_date"]
        missing["canonical_content_sha256"] = canonical_content_sha256(
            evidence_type="horizon_proof",
            schema_version="1.0",
            logical_identity=missing["logical_identity"],
            payload=missing["payload"],
            semantic_provenance=missing["semantic_provenance"],
        )
        missing["payload_sha256"] = payload_sha256(missing["payload"])
        invalid = _task6_exact_outcome_envelope()
        invalid["payload"]["horizon_end_date"] = "not-a-date"
        invalid["canonical_content_sha256"] = canonical_content_sha256(
            evidence_type="outcome",
            schema_version="1.0",
            logical_identity=invalid["logical_identity"],
            payload=invalid["payload"],
            semantic_provenance=invalid["semantic_provenance"],
        )
        invalid["payload_sha256"] = payload_sha256(invalid["payload"])

        missing_result = self._archive([missing])
        invalid_result = self._archive([invalid])

        self.assertEqual(missing_result.status, "rejected")
        self.assertEqual(missing_result.error_code, "invalid_partition_date")
        self.assertEqual(invalid_result.status, "rejected")
        self.assertEqual(invalid_result.error_code, "invalid_partition_date")


class OutcomeMaturationTests(_ArchiveRepositoryMixin, _OutcomeMaturationMethods, unittest.TestCase):
    def test_horizon_proof_uses_frozen_identity_and_direct_payload_fields(self):
        self._bootstrap()
        record = _task5_record()
        first_transport = self._write_transport(_task6_transport_envelopes(record))
        db_path = self.root / "evidence.db"
        SQLiteCache(db_path).save_signal_observations((record.observation,))

        result = _task6_mature(
            repo_dir=self.caller,
            first_transport_dir=first_transport,
            evidence_checkout=self.root / "fresh-checkout",
            outcome_transport_dir=self.root / "outcome-transport",
            db_path=db_path,
        )

        proofs = _task6_read_transport_records(result.proof_transport)
        proof = next(
            item for item in proofs if item["logical_identity"]["horizon_bars"] == 5
        )
        self.assertEqual(
            proof["logical_identity"],
            {
                "horizon_bars": 5,
                "observation_hash": record.observation.observation_hash,
                "schema_version": "1.0",
                "signal_id": record.observation.signal_id,
            },
        )
        self.assertNotIn("horizon_end_date", proof["logical_identity"])
        payload = proof["payload"]
        self.assertEqual(
            {
                "signal_market_date": payload["signal_market_date"],
                "horizon_start_date": payload["horizon_start_date"],
                "horizon_end_date": payload["horizon_end_date"],
                "horizon_bars": payload["horizon_bars"],
                "corporate_action_policy": payload["corporate_action_policy"],
                "corporate_action_provider": payload["corporate_action_provider"],
                "split_check_status": payload["split_check_status"],
                "split_event_count": payload["split_event_count"],
                "price_basis": payload["price_basis"],
                "raw_price_basis": payload["raw_price_basis"],
                "signal_price_basis_status": payload["signal_price_basis_status"],
                "evidence_hashes": payload["evidence_hashes"],
                "proof_status": payload["proof_status"],
            },
            payload,
        )
        self.assertNotIn("policy", payload)
        self.assertNotIn("proof", payload)

    def test_outcome_uses_frozen_identity_and_evidence_provenance(self):
        self._bootstrap()
        record = _task5_record()
        first_transport = self._write_transport(_task6_transport_envelopes(record))
        db_path = self.root / "evidence.db"
        SQLiteCache(db_path).save_signal_observations((record.observation,))

        result = _task6_mature(
            repo_dir=self.caller,
            first_transport_dir=first_transport,
            evidence_checkout=self.root / "fresh-checkout",
            outcome_transport_dir=self.root / "outcome-transport",
            db_path=db_path,
        )

        proofs = _task6_read_transport_records(result.proof_transport)
        proof = next(
            item for item in proofs if item["logical_identity"]["horizon_bars"] == 5
        )
        outcomes = _task6_read_transport_records(result.outcome_transport)
        outcome = next(
            item for item in outcomes if item["logical_identity"]["horizon_bars"] == 5
        )
        self.assertEqual(
            outcome["logical_identity"],
            {
                "evaluation_policy_version": "1.0",
                "horizon_bars": 5,
                "observation_hash": record.observation.observation_hash,
                "schema_version": "1.0",
                "signal_id": record.observation.signal_id,
            },
        )
        self.assertNotIn("horizon_end_date", outcome["logical_identity"])
        self.assertEqual(
            outcome["semantic_provenance"],
            {
                "horizon_proof_shard": proof["canonical_content_sha256"],
                "market_bar_shards": proof["payload"]["evidence_hashes"][
                    "market_bar_shards"
                ],
            },
        )

    def test_split_in_horizon_archives_proof_without_outcome(self):
        self._bootstrap()
        record = _task5_record()
        split_event = {
            "effective_date": "2026-09-03",
            "split_factor_raw": "2.0000",
            "split_ratio": {"new_shares": "2", "old_shares": "1"},
        }
        envelopes, _ = _task5_stock_envelopes(record, events=[split_event])
        first_transport = self._write_transport(envelopes)
        db_path = self.root / "evidence.db"
        SQLiteCache(db_path).save_signal_observations((record.observation,))

        with patch("advisor.signal_outcome.evaluate_signal_observation") as evaluator:
            result = _task6_mature(
                repo_dir=self.caller,
                first_transport_dir=first_transport,
                evidence_checkout=self.root / "fresh-checkout",
                outcome_transport_dir=self.root / "outcome-transport",
                db_path=db_path,
            )

        self.assertIsNotNone(result.proof_transport)
        proofs = _task6_read_transport_records(result.proof_transport)
        proof = next(
            item for item in proofs if item["logical_identity"]["horizon_bars"] == 5
        )
        self.assertEqual(
            proof["payload"]["split_check_status"],
            "split_in_horizon_unavailable",
        )
        self.assertEqual(
            proof["payload"]["proof_status"],
            "split_in_horizon_unavailable",
        )
        self.assertIsNotNone(result.outcome_transport)
        self.assertEqual(_task6_read_transport_records(result.outcome_transport), [])
        evaluator.assert_not_called()

    def test_first_archive_failure_prevents_maturation_and_second_archive(self):
        record = _task5_record()
        first_transport = self._write_transport(_task6_transport_envelopes(record))
        calls: list[Path] = []
        original_archive = EvidenceArchive.archive

        def spy_archive(instance, transport_dir):
            calls.append(transport_dir)
            return original_archive(instance, transport_dir)

        EvidenceArchive.archive = spy_archive
        try:
            with self.assertRaises(MaterializationError) as error:
                _task6_mature(
                    repo_dir=self.caller,
                    first_transport_dir=first_transport,
                    evidence_checkout=self.root / "fresh-checkout",
                    outcome_transport_dir=self.root / "outcome-transport",
                    db_path=self.root / "evidence.db",
                )
        finally:
            EvidenceArchive.archive = original_archive

        self.assertEqual(str(error.exception), "first_archive_not_durable")
        self.assertEqual(len(calls), 1)

    def test_second_archive_failure_writes_zero_new_operational_outcome_rows(self):
        self._bootstrap()
        record = _task5_record()
        first_transport = self._write_transport(_task6_transport_envelopes(record))
        self.assertEqual(self._archive(_task6_transport_envelopes(record)).status, "committed")
        self.assertEqual(
            self._archive([_task6_outcome_conflict_envelope(record)]).status,
            "committed",
        )
        db_path = self.root / "evidence.db"
        self.assertEqual(
            SQLiteCache(db_path).save_signal_observations((record.observation,)).status,
            "written",
        )
        with self.assertRaises(MaterializationError) as error:
            _task6_mature(
                repo_dir=self.caller,
                first_transport_dir=first_transport,
                evidence_checkout=self.root / "fresh-checkout",
                outcome_transport_dir=self.root / "outcome-transport",
                db_path=db_path,
            )

        self.assertEqual(str(error.exception), "second_archive_not_durable")
        self.assertEqual(SQLiteCache(db_path).count_signal_forward_outcomes(), 0)


class ArchiveAuthorityTransitionTests(_ArchiveRepositoryMixin, unittest.TestCase):
    def test_different_source_binding_for_same_observation_conflicts_after_first_archive(self):
        self._bootstrap()
        record_x = _task5_record()
        first = self._archive([_task5_observation_envelope(record_x)])
        self.assertEqual(first.status, "committed")
        self.assertTrue(first.durability_confirmed)

        canonical_path = next(
            path for path in self._remote_tree() if path.startswith("evidence/observations/")
        )
        canonical_bytes = self._remote_file(canonical_path)
        divergent = replace(
            record_x,
            source_binding=replace(record_x.source_binding, snapshot_sha256="f" * 64),
        )
        commands: list[tuple[str, ...]] = []
        original_run_git = evidence_archive_module._run_git

        def spy_run_git(cwd, *args, **kwargs):
            commands.append(tuple(args))
            return original_run_git(cwd, *args, **kwargs)

        evidence_archive_module._run_git = spy_run_git
        try:
            second = self._archive([_task5_observation_envelope(divergent)])
        finally:
            evidence_archive_module._run_git = original_run_git

        self.assertEqual(second.status, "conflict")
        self.assertFalse(second.durability_confirmed)
        self.assertEqual(self._remote_file(canonical_path), canonical_bytes)
        self.assertEqual(
            len([path for path in self._remote_tree() if path.startswith("evidence/observations/")]),
            1,
        )
        self.assertTrue(second.conflict_paths)
        forbidden = {"--force", "--force-with-lease", "merge", "rebase", "pull"}
        self.assertFalse(
            any(token in forbidden for command in commands for token in command)
        )


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

    def test_large_canonical_archive_batches_windows_safe_git_add_commands(self):
        self._bootstrap()
        before_count = self._remote_commit_count()
        transport = self._write_transport(_large_market_envelopes())
        incoming = evidence_archive_module._read_transport(transport)
        batch_identity = evidence_archive_module._batch_identity(incoming)
        manifest_path, _, _ = evidence_archive_module._manifest_for_batch(
            shards=incoming,
            batch_identity=batch_identity,
        )
        intended_paths = sorted([shard.path for shard in incoming] + [manifest_path])
        command_budget = 24_000
        self.assertGreater(
            len(subprocess.list2cmdline(["git", "add", "--", *intended_paths])),
            command_budget,
        )

        original_run_git = evidence_archive_module._run_git
        add_commands: list[tuple[str, ...]] = []

        def record_add(cwd, *args, **kwargs):
            if args[:2] == ("add", "--"):
                add_commands.append(tuple(args[2:]))
            return original_run_git(cwd, *args, **kwargs)

        with patch.object(evidence_archive_module, "_run_git", side_effect=record_add):
            result = EvidenceArchive(
                repo_dir=self.caller,
                branch_name=self.branch_name,
            ).archive(transport)

        self.assertEqual(result.status, "committed")
        self.assertTrue(result.durability_confirmed)
        self.assertEqual(result.committed_paths, tuple(intended_paths))
        self.assertEqual(self._remote_commit_count(), before_count + 1)
        self.assertGreater(len(add_commands), 1)
        self.assertTrue(
            all(
                len(subprocess.list2cmdline(["git", "add", "--", *paths]))
                <= command_budget
                for paths in add_commands
            )
        )
        self.assertEqual(
            set(self._remote_tree()),
            {"evidence/branch-schema.json", *intended_paths},
        )

    def test_git_add_batching_preserves_deterministic_explicit_path_set(self):
        start = date(2025, 1, 1)
        intended_paths = [
            (
                "evidence/market-bars/"
                f"{(start + timedelta(days=index)).year:04d}/"
                f"{(start + timedelta(days=index)).month:02d}/"
                f"{(start + timedelta(days=index)).day:02d}/"
                f"{hashlib.sha256(f'AAPL-{index}'.encode()).hexdigest()}.json.gz"
            )
            for index in range(500)
        ]
        input_paths = list(reversed(intended_paths)) + [intended_paths[0], intended_paths[-1]]
        expected_paths = sorted(set(input_paths))
        commit_sha = "c" * 40
        add_commands: list[tuple[str, ...]] = []

        def fake_run_git(cwd, *args, **kwargs):
            if args[:2] == ("add", "--"):
                add_commands.append(tuple(args[2:]))
            if args[0] == "rev-parse":
                return subprocess.CompletedProcess(
                    ["git", *args], 0, stdout=f"{commit_sha}\n".encode(), stderr=b""
                )
            if args[0] == "ls-remote":
                return subprocess.CompletedProcess(
                    ["git", *args],
                    0,
                    stdout=f"{commit_sha}\trefs/heads/{self.branch_name}\n".encode(),
                    stderr=b"",
                )
            return subprocess.CompletedProcess(
                ["git", *args], 0, stdout=b"", stderr=b""
            )

        with patch.object(evidence_archive_module, "_run_git", side_effect=fake_run_git):
            committed, error_code = evidence_archive_module._push_commit(
                self.caller,
                self.branch_name,
                input_paths,
            )

        flattened_paths = [path for batch in add_commands for path in batch]
        self.assertTrue(committed)
        self.assertEqual(error_code, commit_sha)
        self.assertGreater(len(add_commands), 1)
        self.assertEqual(flattened_paths, expected_paths)
        self.assertEqual(
            {path: flattened_paths.count(path) for path in expected_paths},
            {path: 1 for path in expected_paths},
        )
        self.assertTrue(
            all(
                len(subprocess.list2cmdline(["git", "add", "--", *batch])) <= 24_000
                for batch in add_commands
            )
        )

    def test_git_add_batch_failure_fails_closed_without_remote_authority(self):
        self._bootstrap()
        before_sha = _run_git(self.remote, "rev-parse", self.branch_name).stdout.strip()
        before_tree = self._remote_tree()
        before_count = self._remote_commit_count()
        transport = self._write_transport(_large_market_envelopes())
        original_run_git = evidence_archive_module._run_git
        add_batches: list[tuple[str, ...]] = []

        def fail_second_add(cwd, *args, **kwargs):
            if args[:2] == ("add", "--"):
                add_batches.append(tuple(args[2:]))
                if len(add_batches) == 2:
                    raise evidence_archive_module._ArchiveError("git_error")
            return original_run_git(cwd, *args, **kwargs)

        with patch.object(
            evidence_archive_module,
            "_run_git",
            side_effect=fail_second_add,
        ):
            result = EvidenceArchive(
                repo_dir=self.caller,
                branch_name=self.branch_name,
            ).archive(transport)

        self.assertGreaterEqual(len(add_batches), 2)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.error_code, "storage_error")
        self.assertFalse(result.durability_confirmed)
        self.assertEqual(_run_git(self.remote, "rev-parse", self.branch_name).stdout.strip(), before_sha)
        self.assertEqual(self._remote_tree(), before_tree)
        self.assertEqual(self._remote_commit_count(), before_count)

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


class WorkflowContractTests(unittest.TestCase):
    project_root = Path(__file__).resolve().parents[1]
    evidence_workflow = project_root / ".github" / "workflows" / "financial-advisor-evidence.yml"
    reports_workflow = project_root / ".github" / "workflows" / "financial-advisor-reports.yml"
    nightly_workflow = project_root / ".github" / "workflows" / "financial-advisor-nightly-review.yml"
    base_revision = "49bae01217e95d849ee82cfcb6d0aa406a576156"

    @staticmethod
    def _job_block(content: str, job_name: str) -> str:
        lines = content.splitlines()
        marker = f"  {job_name}:"
        start = next(index for index, line in enumerate(lines) if line == marker)
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if lines[index].startswith("  ") and not lines[index].startswith("    "):
                end = index
                break
        return "\n".join(lines[start:end])

    @classmethod
    def _step_block(cls, content: str, job_name: str, step_name: str) -> str:
        job = cls._job_block(content, job_name)
        marker = f"      - name: {step_name}"
        start = job.index(marker)
        end = job.find("\n      - name:", start + len(marker))
        if end == -1:
            return job[start:]
        return job[start:end]

    def test_reports_job_has_contents_read_and_provider_secrets_only(self):
        content = self.reports_workflow.read_text(encoding="utf-8")
        report_job = self._job_block(content, "report")

        self.assertIn("contents: read", report_job)
        self.assertNotIn("contents: write", report_job)
        for secret_name in (
            "FMP_API_KEY",
            "COINGECKO_API_KEY",
            "ALPHAVANTAGE_API_KEY",
            "COINBASE_API_KEY",
        ):
            self.assertIn(f"{secret_name}: ${{{{ secrets.{secret_name} }}}}", report_job)
        self.assertIn("*/evidence/observations.json.gz", content)
        self.assertNotIn("*/reports/evidence/observations.json.gz", content)
        self.assertIn("uses: actions/upload-artifact@v4", content)
        self.assertIn("path: reports/", content)

    def test_canonical_writer_commands_propagate_checkout_auth_to_child_git(self):
        auth_setup = (
            'auth_header="$(git config --local --get http.https://github.com/.extraheader)"',
            'if [ -z "$auth_header" ]; then',
            'echo "github_checkout_auth_header_unavailable"',
            "export GIT_CONFIG_COUNT=1",
            "export GIT_CONFIG_KEY_0='http.https://github.com/.extraheader'",
            'export GIT_CONFIG_VALUE_0="$auth_header"',
        )
        writer_steps = (
            (
                self.reports_workflow,
                "archive-observation",
                "Archive packaged observation sidecar",
                "python -m advisor evidence archive",
            ),
            (
                self.evidence_workflow,
                "archive",
                "Archive canonical evidence transport",
                "python -m advisor evidence archive",
            ),
            (
                self.evidence_workflow,
                "publish-mature",
                "First archive canonical collector transport",
                "python -m advisor evidence archive",
            ),
            (
                self.evidence_workflow,
                "publish-mature",
                "Mature from canonical evidence",
                "python -m advisor evidence mature",
            ),
        )

        for workflow_path, job_name, step_name, command in writer_steps:
            with self.subTest(workflow=workflow_path.name, job=job_name, step=step_name):
                content = workflow_path.read_text(encoding="utf-8")
                step = self._step_block(content, job_name, step_name)
                for line in auth_setup:
                    self.assertIn(line, step)
                self.assertIn(command, step)
                self.assertLess(
                    step.index('export GIT_CONFIG_VALUE_0="$auth_header"'),
                    step.index(command),
                )
                self.assertNotIn('echo "$auth_header"', step)

    def test_archive_job_has_contents_write_and_no_provider_secrets(self):
        content = self.evidence_workflow.read_text(encoding="utf-8")
        job = self._job_block(content, "archive")

        self.assertIn("contents: write", job)
        self.assertNotIn("contents: read", job)
        for secret_name in (
            "FMP_API_KEY",
            "COINGECKO_API_KEY",
            "ALPHAVANTAGE_API_KEY",
            "COINBASE_API_KEY",
        ):
            self.assertNotIn(secret_name, job)
        self.assertIn("actions/download-artifact@v4", job)
        self.assertIn("python -m advisor evidence archive", job)
        self.assertIn("python -m advisor evidence package", job)
        self.assertIn("--transport-dir", job)
        self.assertIn("--output-dir .tmp/evidence-canonical-transport", job)
        self.assertIn("--repo-dir", job)
        self.assertLess(
            job.index("python -m advisor evidence package"),
            job.index("python -m advisor evidence archive"),
        )

    def test_collector_job_has_contents_read_and_provider_secrets(self):
        content = self.evidence_workflow.read_text(encoding="utf-8")
        job = self._job_block(content, "collector")

        self.assertIn("contents: read", job)
        self.assertNotIn("contents: write", job)
        for secret_name in (
            "FMP_API_KEY",
            "COINGECKO_API_KEY",
            "ALPHAVANTAGE_API_KEY",
            "COINBASE_API_KEY",
        ):
            self.assertIn(f"{secret_name}: ${{{{ secrets.{secret_name} }}}}", job)
        self.assertIn("python -m advisor evidence collect", job)
        self.assertIn("actions/upload-artifact@v4", job)

    def test_collector_job_reads_oldest_provider_from_read_only_evidence_checkout(self):
        content = self.evidence_workflow.read_text(encoding="utf-8")
        job = self._job_block(content, "collector")

        self.assertIn("ref: advisor-evidence", job)
        self.assertIn("path: .tmp/evidence-checkout", job)
        self.assertIn("--evidence-checkout .tmp/evidence-checkout", job)
        self.assertIn("--assets-file .tmp/evidence-assets.json", job)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assets_path = root / "assets.json"
            assets_path.write_text(
                json.dumps(
                    {
                        "assets": [
                            {"symbol": "AAPL", "asset_type": "stock"},
                            {"symbol": "SPY", "asset_type": "etf"},
                            {"symbol": "HYPE", "asset_type": "crypto"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            checkout = root / "evidence-checkout"
            checkout.mkdir()
            market_transport = root / "market-transport.json"
            market_transport.write_text(json.dumps({"records": []}), encoding="utf-8")
            corporate_transport = root / "corporate-actions-transport.json"
            corporate_transport.write_text(json.dumps({"records": []}), encoding="utf-8")

            with patch(
                "advisor.cli.oldest_canonical_provider_by_symbol",
                return_value={"AAPL": "fmp"},
            ) as read_provider, patch("advisor.cli.EvidenceCollector") as collector_type:
                collector = collector_type.return_value
                collector.collect_market.return_value = market_transport
                collector.collect_corporate_actions.return_value = corporate_transport
                output = StringIO()
                with redirect_stdout(output):
                    status = cli_module.main(
                        [
                            "evidence",
                            "collect",
                            "--assets-file",
                            str(assets_path),
                            "--transport-dir",
                            str(root / "transport"),
                            "--evidence-checkout",
                            str(checkout),
                        ]
                    )

            self.assertEqual(status, 0)
            read_provider.assert_called_once_with(
                evidence_checkout=checkout,
                symbols=("AAPL", "SPY", "HYPE"),
            )
            collector.collect_market.assert_called_once()
            self.assertEqual(
                collector.collect_market.call_args.kwargs["existing_provider_by_symbol"],
                {"AAPL": "fmp"},
            )
            self.assertEqual(
                collector.collect_market.call_args.kwargs["assets"],
                (
                    CollectionAsset("AAPL", "stock"),
                    CollectionAsset("SPY", "etf"),
                    CollectionAsset("HYPE", "crypto"),
                ),
            )

    def test_publish_mature_job_has_contents_write_and_no_provider_secrets(self):
        content = self.evidence_workflow.read_text(encoding="utf-8")
        job = self._job_block(content, "publish-mature")

        self.assertIn("contents: write", job)
        self.assertNotIn("contents: read", job)
        for secret_name in (
            "FMP_API_KEY",
            "COINGECKO_API_KEY",
            "ALPHAVANTAGE_API_KEY",
            "COINBASE_API_KEY",
        ):
            self.assertNotIn(secret_name, job)
        self.assertIn("actions/download-artifact@v4", job)
        self.assertIn("python -m advisor evidence mature", job)
        self.assertIn("--repo-dir", job)
        self.assertIn("--first-transport-dir", job)
        self.assertIn("--evidence-checkout", job)
        self.assertIn("--db", job)

    def test_publish_mature_order_is_first_archive_fresh_read_mature_second_archive(self):
        content = self.evidence_workflow.read_text(encoding="utf-8")
        job = self._job_block(content, "publish-mature")

        markers = (
            "First archive canonical collector transport",
            "Fresh canonical evidence checkout",
            "Mature from canonical evidence",
            "Second archive confirmation",
        )
        positions = [job.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("python -m advisor evidence archive", job)
        self.assertIn("python -m advisor evidence mature", job)
        self.assertIn("--first-transport-dir .tmp/evidence-canonical-transport", job)
        self.assertIn("financial-advisor-evidence-canonical-transport-", job)
        self.assertIn("first archive -> fresh read -> materialization -> second archive", job)

    def test_evidence_writer_concurrency_does_not_cancel_in_progress(self):
        content = self.evidence_workflow.read_text(encoding="utf-8")

        self.assertIn("group: advisor-evidence-writer", content)
        self.assertIn("cancel-in-progress: false", content)
        self.assertNotIn("cancel-in-progress: true", content)

    def test_nightly_workflow_is_not_modified_or_consumed_by_evidence_flow(self):
        nightly_bytes = self.nightly_workflow.read_bytes()
        base = subprocess.run(
            ["git", "show", f"{self.base_revision}:.github/workflows/financial-advisor-nightly-review.yml"],
            capture_output=True,
            check=True,
        ).stdout
        self.assertEqual(nightly_bytes.replace(b"\r\n", b"\n"), base.replace(b"\r\n", b"\n"))

        evidence_content = self.evidence_workflow.read_text(encoding="utf-8")
        self.assertNotIn("financial-advisor-nightly-review.yml", evidence_content)
        self.assertNotIn("nightly-review-input", evidence_content)
        self.assertNotIn("runtime-manifest", evidence_content)

    def test_evidence_cli_propagates_branch_missing_and_archive_failure(self):
        missing = SimpleNamespace(
            status="evidence_branch_missing",
            batch_identity="a" * 64,
            manifest_path=None,
            committed_paths=(),
            conflict_paths=(),
            error_code="evidence_branch_missing",
            durability_confirmed=False,
        )
        rejected = SimpleNamespace(
            status="rejected",
            batch_identity="b" * 64,
            manifest_path=None,
            committed_paths=(),
            conflict_paths=(),
            error_code="validation_failed",
            durability_confirmed=False,
        )
        with patch("advisor.cli.EvidenceArchive") as archive_type:
            archive = archive_type.return_value
            archive.archive.return_value = missing
            output = StringIO()
            with redirect_stdout(output):
                missing_status = cli_module.main(
                    [
                        "evidence",
                        "archive",
                        "--transport-dir",
                        ".tmp/evidence-transport",
                        "--repo-dir",
                        ".",
                    ]
                )
            self.assertEqual(missing_status, 1)
            self.assertIn("evidence_branch_missing", output.getvalue())

            archive.archive.return_value = rejected
            output = StringIO()
            with redirect_stdout(output):
                rejected_status = cli_module.main(
                    [
                        "evidence",
                        "archive",
                        "--transport-dir",
                        ".tmp/evidence-transport",
                        "--repo-dir",
                        ".",
                    ]
                )
            self.assertEqual(rejected_status, 1)
            self.assertIn("validation_failed", output.getvalue())

            package_transport = Path(".tmp/evidence-transport")
            package_output = Path(".tmp/evidence-canonical-transport")
            with patch(
                "advisor.cli.package_evidence_transport",
                return_value=(Path("evidence/market-bars/example.json.gz"),),
            ) as package:
                output = StringIO()
                with redirect_stdout(output):
                    package_status = cli_module.main(
                        [
                            "evidence",
                            "package",
                            "--transport-dir",
                            str(package_transport),
                            "--output-dir",
                            str(package_output),
                        ]
                    )

            self.assertEqual(package_status, 0)
            package.assert_called_once_with(
                transport_dir=package_transport,
                output_dir=package_output,
            )
            self.assertIn('"status":"ok"', output.getvalue())

            with patch(
                "advisor.cli.package_evidence_transport",
                side_effect=ValueError("secret transport details"),
            ):
                output = StringIO()
                with redirect_stdout(output):
                    failed_package_status = cli_module.main(
                        [
                            "evidence",
                            "package",
                            "--transport-dir",
                            str(package_transport),
                            "--output-dir",
                            str(package_output),
                        ]
                    )

            self.assertEqual(failed_package_status, 1)
            self.assertIn("packaging_failed", output.getvalue())
            self.assertNotIn("secret transport details", output.getvalue())


if __name__ == "__main__":
    unittest.main()
