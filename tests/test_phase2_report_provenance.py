from __future__ import annotations

import unittest
from dataclasses import asdict

from advisor.models import (
    AssetDecision,
    AssetSnapshot,
    BacktestStats,
    Candle,
    DataFetchMetadata,
    Fundamentals,
    ProviderCapability,
    RiskPlan,
)
from advisor.audit import validate_schema
from advisor.report import render_analyst_review_input, render_markdown_report
from scripts.build_post_phase2_baseline import build_schema_drift


def _decision() -> AssetDecision:
    return AssetDecision(
        symbol="AMD",
        asset_type="stock",
        decision="watch_buy",
        investment_quality_score=70,
        swing_trade_score=65,
        risk_plan=RiskPlan(100, 95, 110, 115, 5, 250, 0.005, 50, 5000, "2.0", []),
        alerts=[],
        limitations=[],
        thesis="Fixture thesis.",
        metrics_summary=["fixture"],
        ideal_entry=100,
        alternative_entry=None,
        hold_suggestion="swing",
        backtest_stats=BacktestStats(30, 0.5, None, None),
        sample_quality="medium",
        market_session="regular",
    )


def _snapshot() -> AssetSnapshot:
    return AssetSnapshot(
        symbol="AMD",
        asset_type="stock",
        theme="semiconductors",
        candles=[Candle("2026-07-10", 100, 102, 99, 101, 1000)],
        fundamentals=Fundamentals(None, None, None, None, None, None, None, None, None),
        data_fetch_metadata=DataFetchMetadata(
            provider="fmp",
            endpoint="historical_prices",
            source_timestamp="2026-07-10",
            cache_age_seconds=45,
            market_data_kind="eod_candle",
        ),
        quote_status="unavailable",
        quote_source="fmp",
        quote_timestamp="2026-07-11T14:30:00+00:00",
        quote_age_seconds=30,
        guidance_status="not_implemented",
        macro_status="not_implemented",
        news_status="not_configured",
        sec_filings_status="temporarily_unavailable",
        benchmark_provenance={"sector": {"symbol": "SMH", "status": "unsupported_by_plan"}},
        provider_capabilities=[
            ProviderCapability("fmp", "quotes", True, False, True, "unsupported_by_plan", True)
        ],
    )


class Phase2ReportProvenanceTests(unittest.TestCase):
    def test_markdown_uses_snapshot_provenance_without_reclassifying_decision(self) -> None:
        decision = _decision()
        before = asdict(decision)

        markdown = render_markdown_report(
            [decision],
            stock_regime="neutral",
            crypto_regime="neutral",
            data_mode="live",
            generated_at="2026-07-10T13:00:00-03:00",
            snapshots_by_symbol={"AMD": _snapshot()},
        )

        self.assertEqual(asdict(decision), before)
        self.assertIn("- quote_status: `unsupported_by_plan`", markdown)
        self.assertIn("- quote_provider: `fmp`", markdown)
        self.assertIn("- quote_timestamp: `2026-07-11T14:30:00+00:00`", markdown)
        self.assertIn("- quote_age_seconds: `30`", markdown)
        self.assertIn("- quote_data_kind: `live_quote`", markdown)
        self.assertIn("- latest_candle_date: `2026-07-10`", markdown)
        self.assertIn("- candle_data_kind: `eod_candle`", markdown)
        self.assertIn("- candle_source_timestamp: `2026-07-10`", markdown)
        self.assertIn("- macro_status: `not_implemented`", markdown)
        self.assertIn("- guidance_status: `not_implemented`", markdown)
        self.assertIn("- news_status: `not_configured`", markdown)
        self.assertIn("- sec_filings_status: `temporarily_unavailable`", markdown)
        self.assertIn("- sector_benchmark_status: `unsupported_by_plan`", markdown)

    def test_analyst_input_keeps_context_statuses_granular(self) -> None:
        analyst_input = render_analyst_review_input(
            [_decision()],
            report_type="main",
            data_mode="live",
            stock_regime="neutral",
            crypto_regime="neutral",
            generated_at="2026-07-10T13:00:00-03:00",
            snapshots_by_symbol={"AMD": _snapshot()},
        )

        self.assertIn("guidance_status: `not_implemented`", analyst_input)
        self.assertIn("macro_status: `not_implemented`", analyst_input)
        self.assertIn("news_status: `not_configured`", analyst_input)
        self.assertIn("sec_filings_status: `temporarily_unavailable`", analyst_input)
        self.assertIn("sector_benchmark_status: `unsupported_by_plan`", analyst_input)
        self.assertNotIn("guidance_status: `not_collected`", analyst_input)

    def test_schema_drift_identifies_empty_response_without_treating_error_as_valid_payload(self) -> None:
        diagnostic = build_schema_drift(
            {
                "providers": {
                    "coingecko": {
                        "calls": [
                            {
                                "provider": "coingecko",
                                "endpoint_name": "markets",
                                "symbol": "AVAX",
                                "schema_valid": False,
                                "fields_present": [],
                                "fields_missing": ["market_cap", "total_volume"],
                                "payload_type": "list",
                                "records_returned": 0,
                                "status": "temporarily_unavailable",
                                "failure_cause": "schema_error",
                            },
                            {
                                "provider": "coingecko",
                                "endpoint_name": "markets",
                                "symbol": "BTC",
                                "schema_valid": True,
                                "payload_type": "dict",
                                "status": "provider_error",
                            },
                        ]
                    }
                }
            }
        )

        self.assertEqual(len(diagnostic["occurrences"]), 1)
        occurrence = diagnostic["occurrences"][0]
        self.assertEqual(occurrence["provider"], "coingecko")
        self.assertEqual(occurrence["endpoint_name"], "markets")
        self.assertEqual(occurrence["actual_top_level_type"], "list")
        self.assertEqual(occurrence["missing_expected_fields"], ["market_cap", "total_volume"])
        self.assertEqual(occurrence["impact"], "snapshot_degraded")

    def test_error_payload_does_not_satisfy_market_schema(self) -> None:
        result = validate_schema({"status": {"error_code": 10005}}, ["market_cap", "total_volume"])

        self.assertFalse(result["schema_valid"])
        self.assertEqual(result["fields_missing"], ["market_cap", "total_volume"])


if __name__ == "__main__":
    unittest.main()
