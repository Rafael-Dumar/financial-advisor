from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import patch

from advisor.cache import SQLiteCache
from advisor.config import AdvisorConfig
from advisor.audit import AuditRecorder, build_provider_audit, provider_registry, run_data_audit, sanitize_url, validate_schema
from advisor.cli import main as advisor_main
from advisor.http_client import fetch_json
from advisor.live_loader import LiveDataLoader
from advisor.data_pipeline import stock_snapshot_from_payloads
from advisor.data_pipeline import crypto_snapshot_from_payloads
from advisor.live_loader import _fetch_metadata
from advisor.audit import build_data_lineage


class CacheInspectionTests(unittest.TestCase):
    def test_read_only_inspection_reports_real_age_expiry_and_source_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "source.db"
            cache = SQLiteCache(db_path)
            cache.set_json(
                "prices",
                "https://example.test/prices?symbol=AMD",
                {"historical": [{"date": "2026-07-09", "close": 123.45}]},
                fetched_at="2026-07-10T10:00:00+00:00",
            )

            rows = SQLiteCache(db_path, read_only=True).inspect(
                namespace="prices",
                now="2026-07-10T12:00:00+00:00",
                freshness_seconds={"prices": 3600},
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fetched_at"], "2026-07-10T10:00:00+00:00")
        self.assertEqual(rows[0]["cache_age_seconds"], 7200)
        self.assertTrue(rows[0]["expired"])
        self.assertEqual(rows[0]["payload_record_count"], 1)
        self.assertEqual(rows[0]["latest_source_timestamp"], "2026-07-09")

    def test_read_only_inspection_extracts_latest_timestamp_from_binance_kline_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "source.db"
            cache = SQLiteCache(db_path)
            cache.set_json(
                "prices",
                "BTCUSDT",
                [
                    [1767225600000, "100", "105", "99", "104", "1000"],
                    [1767312000000, "104", "108", "103", "107", "1200"],
                ],
                fetched_at="2026-07-10T10:00:00+00:00",
            )

            rows = SQLiteCache(db_path, read_only=True).inspect(namespace="prices")

        self.assertEqual(rows[0]["latest_source_timestamp"], "2026-01-02")

    def test_read_only_inspection_does_not_create_or_modify_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = Path(tmp) / "missing.db"
            rows = SQLiteCache(missing_path, read_only=True).inspect(now="2026-07-10T12:00:00+00:00")
            self.assertEqual(rows, [])
            self.assertFalse(missing_path.exists())

            source_path = Path(tmp) / "source.db"
            SQLiteCache(source_path).set_json("prices", "key", {"historical": []})
            before = source_path.stat().st_mtime_ns
            SQLiteCache(source_path, read_only=True).inspect(now="2026-07-10T12:00:00+00:00")
            after = source_path.stat().st_mtime_ns
            self.assertEqual(before, after)


class _RecordingHttpObserver:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def on_request(self, **metadata: object) -> None:
        self.events.append(("request", metadata))

    def on_response(self, **metadata: object) -> None:
        self.events.append(("response", metadata))

    def on_error(self, **metadata: object) -> None:
        self.events.append(("error", metadata))


class _FakeHttpResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class HttpObservationTests(unittest.TestCase):
    def test_fetch_json_observer_records_sanitized_transport_metadata(self) -> None:
        observer = _RecordingHttpObserver()
        url = "https://example.test/data?apikey=secret-value&symbol=AMD"

        with patch("advisor.http_client.urlopen", return_value=_FakeHttpResponse(b'{"ok": true}')):
            payload = fetch_json(url, observer=observer)

        self.assertEqual(payload, {"ok": True})
        self.assertEqual([event[0] for event in observer.events], ["request", "response"])
        request = observer.events[0][1]
        response = observer.events[1][1]
        self.assertNotIn("secret-value", str(request))
        self.assertEqual(response["http_status"], 200)
        self.assertEqual(response["payload_type"], "dict")
        self.assertEqual(response["payload_size_bytes"], len(b'{"ok": true}'))
        self.assertLessEqual(str(request["started_at"]), str(response["received_at"]))

    def test_fetch_json_observer_records_http_status_and_retry_after_without_secret(self) -> None:
        observer = _RecordingHttpObserver()
        error = HTTPError(
            "https://example.test/data?apikey=secret-value",
            429,
            "Too Many Requests",
            {"Retry-After": "60"},
            None,
        )

        with patch("advisor.http_client.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "http_error:429"):
                fetch_json("https://example.test/data?apikey=secret-value", observer=observer)

        self.assertEqual([event[0] for event in observer.events], ["request", "error"])
        metadata = observer.events[-1][1]
        self.assertEqual(metadata["http_status"], 429)
        self.assertEqual(metadata["retry_after"], "60")
        self.assertNotIn("secret-value", str(metadata))


class _RecordingLoaderAudit:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._next_id = 0

    def start_call(self, provider: str, namespace: str, url: str, **metadata: object) -> str:
        self._next_id += 1
        call_id = f"call-{self._next_id}"
        self.calls.append(
            {
                "call_id": call_id,
                "provider": provider,
                "namespace": namespace,
                "url": url,
                **metadata,
            }
        )
        return call_id

    def finish_call(self, call_id: str, **metadata: object) -> None:
        call = next(item for item in self.calls if item["call_id"] == call_id)
        call.update(metadata)


class FallbackChainTests(unittest.TestCase):
    def test_yahoo_fallback_snapshot_and_lineage_include_normalized_source_age(self) -> None:
        def fake_fetch(url: str, *, payload=None, headers=None):
            if "historical-price-eod/full" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter")
            if "historical-price-eod/light" in url:
                return []
            if "query1.finance.yahoo.com" in url:
                return {
                    "chart": {
                        "result": [
                            {
                                "timestamp": [1767225600],
                                "indicators": {"quote": [{"open": [100], "high": [102], "low": [99], "close": [101], "volume": [1000]}]},
                            }
                        ]
                    }
                }
            return []

        config = AdvisorConfig.default()
        config.stock_watchlist = ["AMD"]
        config.crypto_watchlist = []
        config.fmp_api_key = "present"
        config.coingecko_api_key = "present"
        config.alphavantage_api_key = ""
        recorder = AuditRecorder()
        snapshot = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-10", audit_recorder=recorder).load_stock("AMD")

        field = build_data_lineage([snapshot], recorder, ["AMD"])["AMD"]["fields"]["candles"]

        self.assertEqual(snapshot.data_fetch_metadata.provider, "yahoo")
        self.assertEqual(snapshot.data_fetch_metadata.source_timestamp, "2026-01-01")
        self.assertIsInstance(snapshot.data_fetch_metadata.source_age_seconds, int)
        self.assertIsInstance(field["source_age_seconds"], int)

    def test_stooq_fallback_snapshot_and_lineage_include_normalized_source_age(self) -> None:
        def fake_fetch(url: str, *, payload=None, headers=None):
            if "historical-price-eod/full" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter")
            if "historical-price-eod/light" in url:
                return []
            if "query1.finance.yahoo.com" in url:
                return {"chart": {"result": []}}
            return []

        config = AdvisorConfig.default()
        config.stock_watchlist = ["AMD"]
        config.crypto_watchlist = []
        config.fmp_api_key = "present"
        config.coingecko_api_key = "present"
        config.alphavantage_api_key = ""
        recorder = AuditRecorder()
        snapshot = LiveDataLoader(
            config,
            fetch_json=fake_fetch,
            fetch_text=lambda *args, **kwargs: "Date,Open,High,Low,Close,Volume\n2026-01-01,100,102,99,101,1000\n",
            today="2026-07-10",
            audit_recorder=recorder,
        ).load_stock("AMD")

        field = build_data_lineage([snapshot], recorder, ["AMD"])["AMD"]["fields"]["candles"]

        self.assertEqual(snapshot.data_fetch_metadata.provider, "stooq")
        self.assertEqual(snapshot.data_fetch_metadata.source_timestamp, "2026-01-01")
        self.assertIsInstance(snapshot.data_fetch_metadata.source_age_seconds, int)
        self.assertIsInstance(field["source_age_seconds"], int)

    def test_fmp_full_to_light_fallback_preserves_parent_and_reason(self) -> None:
        def fake_fetch(url: str, *, payload=None, headers=None):
            if "historical-price-eod/full" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter")
            if "historical-price-eod/light" in url:
                return {
                    "historical": [
                        {"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}
                    ]
                }
            if "profile" in url:
                return [{"mktCap": 1_000_000_000_000, "volAvg": 10_000_000}]
            return []

        config = AdvisorConfig.default()
        config.stock_watchlist = ["AMD"]
        config.crypto_watchlist = []
        config.fmp_api_key = "present"
        config.coingecko_api_key = "present"
        recorder = _RecordingLoaderAudit()
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-10", audit_recorder=recorder)

        loader.load_stock("AMD")

        self.assertGreaterEqual(len(recorder.calls), 2)
        full = recorder.calls[0]
        light = recorder.calls[1]
        self.assertEqual(full["provider"], "fmp")
        self.assertEqual(light["provider"], "fmp")
        self.assertEqual(light["parent_call_id"], full["call_id"])
        self.assertEqual(light["fallback_from"], "fmp")
        self.assertEqual(light["fallback_to"], "fmp")
        self.assertEqual(light["attempt_number"], 2)
        self.assertIn("price", str(light["fallback_reason"]))

    def test_recorder_captures_cache_hit_fetched_at_and_age(self) -> None:
        def fake_fetch(url: str, *, payload=None, headers=None):
            if "historical-price-eod/full" in url:
                return {"historical": [{"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}]}
            if "profile" in url:
                return [{"mktCap": 1_000_000_000_000, "volAvg": 10_000_000}]
            return []

        with tempfile.TemporaryDirectory() as tmp:
            config = AdvisorConfig.default()
            config.stock_watchlist = ["AMD"]
            config.crypto_watchlist = []
            config.fmp_api_key = "present"
            config.coingecko_api_key = "present"
            recorder = AuditRecorder()
            loader = LiveDataLoader(
                config,
                fetch_json=fake_fetch,
                today="2026-07-10",
                db_path=Path(tmp) / "audit.db",
                audit_recorder=recorder,
            )
            loader.load_stock("AMD")
            loader.load_stock("AMD")

        historical_calls = [call for call in recorder.calls if call["endpoint_name"] == "historical_prices"]
        self.assertEqual(len(historical_calls), 2)
        self.assertFalse(historical_calls[0]["cache_hit"])
        self.assertTrue(historical_calls[1]["cache_hit"])
        self.assertIsNotNone(historical_calls[1]["cache_fetched_at"])
        self.assertIsNotNone(historical_calls[1]["cache_age_seconds"])
        self.assertEqual(historical_calls[1]["source_data_latest_timestamp"], "2026-07-09")


class AuditCoreTests(unittest.TestCase):
    def test_data_lineage_reports_quote_and_benchmark_provenance_without_reclassifying_eod_candles(self) -> None:
        snapshot = stock_snapshot_from_payloads(
            symbol="AMD",
            theme="semiconductors",
            historical_payload={"historical": [{"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}]},
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[],
            today="2026-07-10",
            quote_status="unavailable",
            quote_source="fmp",
            benchmark_provenance={
                "market": {"symbol": "SPY", "status": "available", "source_timestamp": "2026-07-09", "daily_change_pct": 1.0},
                "sector": {"symbol": "SMH", "status": "unavailable", "source_timestamp": None, "daily_change_pct": None},
                "relative_strength_pct": None,
            },
        )
        recorder = AuditRecorder()
        quote_call = recorder.start_call("fmp", "quotes", "https://example.test/stable/quote?symbol=AMD", symbol="AMD")
        recorder.finish_call(quote_call, error="provider_fetch_error:fmp:quotes:http_error:402:Premium Query Parameter")

        fields = build_data_lineage([snapshot], recorder, ["AMD"])["AMD"]["fields"]

        self.assertTrue(fields["candles"]["is_eod"])
        self.assertFalse(fields["candles"]["is_intraday"])
        self.assertEqual(fields["live_quote"]["quote_status"], "unavailable")
        self.assertEqual(fields["live_quote"]["source_endpoint"], "quote")
        self.assertEqual(fields["benchmark"]["benchmark"]["symbol"], "SPY")
        self.assertEqual(fields["sector_benchmark"]["benchmark"]["symbol"], "SMH")
    def test_binance_kline_array_lineage_uses_normalized_source_time_when_call_metadata_is_missing(self) -> None:
        klines = [[1767225600000, "100", "105", "99", "104", "1000"]]
        metadata = _fetch_metadata(
            provider="binance",
            endpoint="prices",
            payload=klines,
            fetched_at="2026-07-10T12:00:00+00:00",
            cache_fetched_at=None,
            cache_age_seconds=None,
            is_fresh=True,
            cache_hit=False,
            fallback_from=None,
            fallback_to=None,
        )
        snapshot = crypto_snapshot_from_payloads(
            symbol="BTC",
            theme="crypto",
            klines_payload=klines,
            market_payload={},
            funding_payload=[],
            open_interest_payload={},
            taker_payload=[],
            data_fetch_metadata=metadata,
        )
        recorder = AuditRecorder()
        call_id = recorder.start_call("binance", "prices", "https://example.test/klines", symbol="BTC")
        recorder.finish_call(call_id, response=klines)

        field = build_data_lineage([snapshot], recorder, ["BTC"])["BTC"]["fields"]["candles"]

        self.assertEqual(field["source_data_timestamp"], "2026-01-01")
        self.assertIsInstance(field["source_age_seconds"], int)

    def test_data_lineage_preserves_eod_candles_as_non_intraday(self) -> None:
        snapshot = stock_snapshot_from_payloads(
            symbol="AMD",
            theme="semiconductors",
            historical_payload={"historical": [{"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}]},
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[],
            today="2026-07-10",
        )

        field = build_data_lineage([snapshot], AuditRecorder(), ["AMD"])["AMD"]["fields"]["candles"]

        self.assertFalse(field["is_intraday"])
        self.assertTrue(field["is_eod"])
        self.assertEqual(field.get("granularity"), "daily")
        self.assertEqual(field.get("market_data_kind"), "eod_candle")

    def test_sanitize_url_redacts_keys_tokens_and_preserves_endpoint_shape(self) -> None:
        url = "https://example.test/query?apikey=secret-value&token=secret-token&symbol=AMD"

        sanitized = sanitize_url(url)

        self.assertIn("https://example.test/query", sanitized)
        self.assertIn("symbol=AMD", sanitized)
        self.assertNotIn("secret-value", sanitized)
        self.assertNotIn("secret-token", sanitized)
        self.assertEqual(sanitized.count("REDACTED"), 2)

    def test_recorder_separates_fetch_time_from_source_timestamp_and_records_schema(self) -> None:
        recorder = AuditRecorder()
        call_id = recorder.start_call("fmp", "prices", "https://example.test/prices?apikey=secret", symbol="AMD")

        recorder.finish_call(
            call_id,
            response={"historical": [{"date": "2026-07-09", "close": 123.45}]},
            cache_hit=False,
        )

        call = recorder.calls[0]
        self.assertEqual(call["call_id"], call_id)
        self.assertEqual(call["source_data_latest_timestamp"], "2026-07-09")
        self.assertIsNotNone(call["response_received_at"])
        self.assertNotEqual(call["source_data_latest_timestamp"], call["response_received_at"])
        self.assertTrue(call["schema_valid"])
        self.assertNotIn("secret", json.dumps(recorder.calls))

    def test_timestamp_extractors_ignore_non_epoch_values_and_keep_valid_kline_time(self) -> None:
        payload = {"data": [{"t": "56"}, {"t": 1767225600000}]}
        recorder = AuditRecorder()
        call_id = recorder.start_call("hyperliquid", "crypto_flow", "https://example.test/info", symbol="HYPE")
        recorder.finish_call(call_id, response=payload)

        metadata = _fetch_metadata(
            provider="hyperliquid",
            endpoint="crypto_flow",
            payload=payload,
            fetched_at="2026-07-10T12:00:00+00:00",
            cache_fetched_at=None,
            cache_age_seconds=None,
            is_fresh=True,
            cache_hit=False,
            fallback_from=None,
            fallback_to=None,
        )

        self.assertEqual(recorder.calls[0]["source_data_latest_timestamp"], "2026-01-01")
        self.assertEqual(metadata.source_timestamp, "2026-01-01")

    def test_schema_validation_reports_missing_fields(self) -> None:
        result = validate_schema({"historical": []}, ["historical", "marketCap"])

        self.assertFalse(result["schema_valid"])
        self.assertEqual(result["fields_missing"], ["marketCap"])

    def test_provider_registry_contains_required_provider_names(self) -> None:
        providers = provider_registry(AdvisorConfig.default())

        for name in ("fmp", "coingecko", "binance", "hyperliquid", "coinbase", "alphavantage", "sec", "yahoo", "stooq"):
            self.assertIn(name, providers)

    def test_crypto_audit_exposes_metric_statuses_without_advertising_invalid_liquidation_collector(self) -> None:
        snapshot = crypto_snapshot_from_payloads(
            symbol="HYPE",
            theme="crypto",
            klines_payload=[[1767225600000, "35", "36", "34", "35.5", "100"]],
            market_payload={},
            funding_payload=[{"fundingRate": "0.0016"}],
            open_interest_payload={"openInterest": "250"},
            taker_payload=[],
            crypto_metric_provenance={
                "funding": {"status": "available", "provider": "hyperliquid", "endpoint": "metaAndAssetCtxs"},
                "current_open_interest": {"status": "available", "provider": "hyperliquid", "endpoint": "metaAndAssetCtxs"},
                "open_interest_change": {"status": "not_implemented", "provider": "hyperliquid"},
                "cvd": {"status": "not_implemented", "provider": "hyperliquid"},
                "liquidations": {"status": "not_implemented", "provider": "binance"},
            },
        )

        fields = build_data_lineage([snapshot], AuditRecorder(), ["HYPE"])["HYPE"]["fields"]
        endpoints = provider_registry(AdvisorConfig.default())["binance"]["endpoints"]

        self.assertEqual(fields["funding"]["status"], "available")
        self.assertEqual(fields["open_interest_change"]["status"], "not_implemented")
        self.assertEqual(fields["liquidations"]["status"], "not_implemented")
        self.assertEqual(fields["liquidations"]["parser"], "not_implemented_liquidations")
        self.assertFalse(any(endpoint["endpoint_name"] == "liquidation_orders" for endpoint in endpoints))

    def test_provider_audit_normalizes_capability_status_and_preserves_failure_causes(self) -> None:
        recorder = AuditRecorder()
        for error in (
            "provider_fetch_error:fmp:prices:http_error:401",
            "provider_fetch_error:fmp:prices:http_error:402:Premium Query Parameter",
            "provider_fetch_error:fmp:prices:http_error:429",
            "provider_fetch_error:fmp:prices:http_error:404",
            "provider_fetch_error:fmp:prices:network_error:timeout",
        ):
            call_id = recorder.start_call("fmp", "prices", "https://example.test/prices?symbol=AMD")
            recorder.finish_call(call_id, error=error)
        empty_call = recorder.start_call("fmp", "fundamentals", "https://example.test/profile?symbol=AMD")
        recorder.finish_call(empty_call, response=[])

        provider = build_provider_audit(AdvisorConfig.default(), recorder, network_mode="live")["fmp"]
        causes = [call["failure_cause"] for call in provider["calls"]]
        capabilities = {item["capability"]: item for item in provider["capabilities"]}

        self.assertEqual(causes, ["unauthorized", "plan_restricted", "rate_limited", "not_found", "network_error", "empty_payload"])
        self.assertEqual(capabilities["historical_prices"]["last_status"], "unsupported_by_plan")
        self.assertEqual(provider["status"], "partial")

    def test_data_lineage_exposes_independent_event_and_context_statuses(self) -> None:
        snapshot = stock_snapshot_from_payloads(
            symbol="AMD",
            theme="semiconductors",
            historical_payload=[],
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[],
            earnings_status="plan_restricted",
            news_status="not_configured",
            sec_filings_status="available",
            today="2026-07-10",
        )

        fields = build_data_lineage([snapshot], AuditRecorder(), ["AMD"])["AMD"]["fields"]

        self.assertEqual(fields["earnings_date"]["status"], "plan_restricted")
        self.assertEqual(fields["guidance"]["status"], "not_implemented")
        self.assertEqual(fields["macro_regime"]["status"], "not_implemented")
        self.assertEqual(fields["news"]["status"], "not_configured")
        self.assertEqual(fields["sec_filings"]["status"], "available")


class AuditCliTests(unittest.TestCase):
    def test_phase2_artifacts_have_sanitized_stable_schemas_without_cache_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            output_dir = root / "audit"
            SQLiteCache(source_db).set_json(
                "prices",
                "https://example.test/prices?apikey=secret-value&symbol=AMD",
                {"historical": [{"date": "2026-07-09", "close": 123.45}]},
                fetched_at="2026-07-10T10:00:00+00:00",
            )
            before = source_db.stat().st_mtime_ns

            result = run_data_audit(
                config=AdvisorConfig.default(),
                source_db=source_db,
                audit_db=root / "must-not-exist.db",
                output_dir=output_dir,
                symbols=["AMD", "BTC"],
                no_network=True,
            )

            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(source_db.stat().st_mtime_ns, before)
            self.assertFalse((root / "must-not-exist.db").exists())
            artifacts = {
                name: json.loads((output_dir / name).read_text(encoding="utf-8"))
                for name in (
                    "phase2-provider-validation.json",
                    "phase2-cache-validation.json",
                    "phase2-source-timestamps.json",
                    "phase2-capability-matrix.json",
                )
            }
            self.assertEqual({payload["schema_version"] for payload in artifacts.values()}, {"phase2-v1"})
            self.assertEqual(
                set(artifacts["phase2-provider-validation.json"]),
                {"schema_version", "audit_generated_at_utc", "network_mode", "providers", "invalid_or_unimplemented"},
            )
            self.assertEqual(
                set(artifacts["phase2-cache-validation.json"]),
                {"schema_version", "audit_generated_at_utc", "network_mode", "cache_databases", "timestamp_semantics"},
            )
            self.assertEqual(
                set(artifacts["phase2-source-timestamps.json"]),
                {"schema_version", "audit_generated_at_utc", "network_mode", "assets"},
            )
            self.assertEqual(
                set(artifacts["phase2-capability-matrix.json"]),
                {"schema_version", "audit_generated_at_utc", "network_mode", "providers", "intentionally_unimplemented"},
            )
            self.assertEqual(artifacts["phase2-provider-validation.json"]["network_mode"], "no_network")
            self.assertIn("providers", artifacts["phase2-provider-validation.json"])
            self.assertIn("invalid_or_unimplemented", artifacts["phase2-provider-validation.json"])
            self.assertTrue(any(item["status"] == "not_implemented" for item in artifacts["phase2-provider-validation.json"]["invalid_or_unimplemented"]))
            provider = artifacts["phase2-provider-validation.json"]["providers"][0]
            self.assertEqual(set(provider), {"provider", "configured", "status", "capabilities", "endpoints", "calls"})
            self.assertEqual(
                set(provider["endpoints"][0]),
                {"endpoint_name", "namespace", "method", "auth", "url_sanitized"},
            )
            self.assertEqual(
                set(artifacts["phase2-provider-validation.json"]["invalid_or_unimplemented"][0]),
                {"provider", "endpoint_name", "capability", "implemented", "status", "reason"},
            )
            cache_database = artifacts["phase2-cache-validation.json"]["cache_databases"][0]
            self.assertEqual(set(cache_database), {"database", "access", "rows"})
            cache_row = cache_database["rows"][0]
            self.assertEqual(
                set(cache_row),
                {
                    "namespace",
                    "key_sanitized",
                    "original_fetched_at",
                    "cache_age_seconds",
                    "cache_expired",
                    "source_data_latest_timestamp",
                    "source_age_seconds",
                },
            )
            self.assertEqual(cache_row["original_fetched_at"], "2026-07-10T10:00:00+00:00")
            self.assertEqual(cache_row["source_data_latest_timestamp"], "2026-07-09")
            self.assertIn("cache_age_seconds", cache_row)
            self.assertIsInstance(cache_row["source_age_seconds"], int)
            timestamps = artifacts["phase2-source-timestamps.json"]["assets"]
            self.assertEqual(set(timestamps["AMD"]), {"asset_type", "eod_candle", "live_quote"})
            self.assertEqual(set(timestamps["BTC"]), {"asset_type", "daily_candle", "intraday_metrics"})
            self.assertEqual(
                set(timestamps["AMD"]["eod_candle"]),
                {
                    "source_provider",
                    "source_endpoint",
                    "source_data_timestamp",
                    "fetched_at",
                    "cache_fetched_at",
                    "cache_age_seconds",
                    "source_age_seconds",
                    "granularity",
                    "market_data_kind",
                    "is_intraday",
                    "is_eod",
                    "status",
                },
            )
            self.assertEqual(
                set(timestamps["BTC"]["intraday_metrics"]),
                {"funding", "open_interest", "open_interest_change", "cvd", "coinbase_premium", "liquidations"},
            )
            self.assertEqual(timestamps["AMD"]["eod_candle"]["market_data_kind"], None)
            self.assertIn("live_quote", timestamps["AMD"])
            self.assertIn("daily_candle", timestamps["BTC"])
            self.assertIn("intraday_metrics", timestamps["BTC"])
            matrix_provider = artifacts["phase2-capability-matrix.json"]["providers"][0]
            self.assertEqual(set(matrix_provider), {"provider", "status", "capabilities"})
            capability = matrix_provider["capabilities"][0]
            self.assertEqual(set(capability), {"capability", "configured", "fallback_available", "implemented", "last_status", "supported_by_plan"})
            rendered = json.dumps(artifacts)
            self.assertNotIn("secret-value", rendered)
            self.assertNotIn("apikey=secret", rendered)
            self.assertIn("apikey=REDACTED", rendered)

    def test_no_network_writes_audit_artifacts_without_touching_source_or_calling_loader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_db = root / "source.db"
            output_dir = root / "audit"
            SQLiteCache(source_db).set_json(
                "prices",
                "https://example.test/prices?apikey=secret",
                {"historical": [{"date": "2026-07-09", "close": 123.45}]},
                fetched_at="2026-07-10T10:00:00+00:00",
            )
            before = source_db.stat().st_mtime_ns

            with patch("advisor.cli.LiveDataLoader", side_effect=AssertionError("network loader must not be constructed")):
                code = advisor_main(
                    [
                        "audit",
                        "data",
                        "--no-network",
                        "--source-db",
                        str(source_db),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertEqual(source_db.stat().st_mtime_ns, before)
            self.assertFalse((output_dir / "audit.db").exists())
            for filename in ("provider-audit.json", "data-lineage.json", "cache-audit.json", "gate-analysis.json", "audit-summary.json"):
                payload = json.loads((output_dir / filename).read_text(encoding="utf-8"))
                self.assertIsInstance(payload, dict)

    def test_cli_passes_symbols_and_isolated_databases_to_audit_runner(self) -> None:
        with patch("advisor.cli.run_data_audit", return_value={"exit_code": 0}) as run_audit:
            code = advisor_main(
                [
                    "audit",
                    "data",
                    "--require-live",
                    "--symbols",
                    "AMD,NVDA,BTC,ETH",
                    "--source-db",
                    "source.db",
                    "--audit-db",
                    "audit.db",
                    "--trace-gates",
                    "--fail-on-schema-drift",
                ]
            )

        self.assertEqual(code, 0)
        kwargs = run_audit.call_args.kwargs
        self.assertEqual(kwargs["symbols"], ["AMD", "NVDA", "BTC", "ETH"])
        self.assertEqual(kwargs["source_db"], Path("source.db"))
        self.assertEqual(kwargs["audit_db"], Path("audit.db"))
        self.assertTrue(kwargs["trace_gates"])
        self.assertTrue(kwargs["fail_on_schema_drift"])


if __name__ == "__main__":
    unittest.main()
