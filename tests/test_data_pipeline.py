import unittest

from advisor.data_pipeline import (
    binance_crypto_flow_from_payloads,
    crypto_snapshot_from_payloads,
    hyperliquid_crypto_flow_from_payload,
    stock_snapshot_from_payloads,
)
from advisor.models import DataFetchMetadata


class DataPipelineTests(unittest.TestCase):
    def test_stock_snapshot_keeps_eod_candles_separate_from_timestamped_quote(self):
        snapshot = stock_snapshot_from_payloads(
            symbol="AMD",
            theme="semiconductors",
            historical_payload={
                "historical": [
                    {"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
                ]
            },
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[],
            today="2026-07-10",
            quote_status="available",
            quote_price=104.5,
            quote_timestamp="2026-07-10T14:30:00+00:00",
            quote_source="fmp",
            quote_age_seconds=30,
            quote_is_intraday=True,
            previous_close=101.0,
            daily_change=3.5,
            daily_change_pct=3.465,
        )

        self.assertEqual(snapshot.candles[-1].date, "2026-07-09")
        self.assertEqual(snapshot.candles[-1].close, 101)
        self.assertEqual(snapshot.quote_status, "available")
        self.assertEqual(snapshot.quote_price, 104.5)
        self.assertTrue(snapshot.quote_is_intraday)
        self.assertEqual(snapshot.previous_close, 101.0)
        self.assertEqual(snapshot.daily_change_pct, 3.465)

    def test_stock_snapshot_carries_eod_candle_metadata_without_intraday_classification(self):
        snapshot = stock_snapshot_from_payloads(
            symbol="MSFT",
            theme="software",
            historical_payload={
                "historical": [
                    {"date": "2026-07-09", "open": 101, "high": 104, "low": 100, "close": 103, "volume": 2000},
                ]
            },
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[],
            today="2026-07-10",
        )

        self.assertEqual(snapshot.data_timestamp, "2026-07-09")
        metadata = getattr(snapshot, "data_fetch_metadata", None)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.granularity, "daily")
        self.assertEqual(metadata.market_data_kind, "eod_candle")

    def test_stock_snapshot_parses_fmp_payloads_and_earnings_event(self):
        snapshot = stock_snapshot_from_payloads(
            symbol="MSFT",
            theme="software",
            historical_payload={
                "historical": [
                    {"date": "2026-01-02", "open": 101, "high": 104, "low": 100, "close": 103, "volume": 2000},
                    {"date": "2026-01-01", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1500},
                ]
            },
            profile_payload=[{"mktCap": 3_000_000_000_000, "volAvg": 20_000_000}],
            ratios_payload=[{"priceEarningsRatioTTM": 32, "pegRatioTTM": 2.1, "grossProfitMarginTTM": 0.68}],
            metrics_payload=[{"freeCashFlowPerShareTTM": 4.5}],
            historical_metrics_payload=[
                {"peRatio": 50},
                {"peRatio": 30},
                {"peRatio": 40},
                {"peRatio": -5},
            ],
            growth_payload=[{"growthRevenue": 0.16, "growthEPS": 0.12}],
            earnings_payload=[{"date": "2026-06-10"}],
            today="2026-06-01",
        )

        self.assertEqual(snapshot.symbol, "MSFT")
        self.assertEqual(snapshot.candles[0].date, "2026-01-01")
        self.assertEqual(snapshot.fundamentals.market_cap, 3_000_000_000_000)
        self.assertEqual(snapshot.fundamentals.historical_pe, 40)
        self.assertEqual(snapshot.fundamentals.revenue_growth, 0.16)
        self.assertEqual(snapshot.fundamentals.eps_growth, 0.12)
        self.assertEqual(snapshot.fundamentals.margin_trend, 0.68)
        self.assertTrue(snapshot.fundamentals.free_cash_flow_positive)
        self.assertEqual(snapshot.event.days_to_earnings, 9)
        self.assertIsNone(snapshot.event.guidance_recent)
        self.assertIsNone(snapshot.event.post_earnings_gap_percent)
        self.assertIn("guidance_recent_not_collected", snapshot.missing_data)
        self.assertIn("post_earnings_gap_not_collected", snapshot.missing_data)

    def test_stock_snapshot_distinguishes_verified_and_empty_earnings_calendar_statuses(self):
        common = {
            "symbol": "MSFT",
            "theme": "software",
            "historical_payload": [],
            "profile_payload": [],
            "ratios_payload": [],
            "metrics_payload": [],
            "historical_metrics_payload": [],
            "growth_payload": [],
            "today": "2026-06-01",
        }

        verified = stock_snapshot_from_payloads(earnings_payload=[{"date": "2026-06-10"}], **common)
        empty = stock_snapshot_from_payloads(earnings_payload=[], **common)

        self.assertEqual(verified.earnings_status, "verified")
        self.assertEqual(empty.earnings_status, "no_upcoming_event_found")

    def test_stock_snapshot_preserves_earnings_provider_failure_semantics(self):
        common = {
            "symbol": "MSFT",
            "theme": "software",
            "historical_payload": [],
            "profile_payload": [],
            "ratios_payload": [],
            "metrics_payload": [],
            "historical_metrics_payload": [],
            "growth_payload": [],
            "earnings_payload": [],
            "today": "2026-06-01",
        }

        unavailable = stock_snapshot_from_payloads(earnings_status="provider_unavailable", **common)
        restricted = stock_snapshot_from_payloads(earnings_status="plan_restricted", **common)
        malformed = stock_snapshot_from_payloads(earnings_payload=[{"when": "unknown"}], **{key: value for key, value in common.items() if key != "earnings_payload"})

        self.assertEqual(unavailable.earnings_status, "provider_unavailable")
        self.assertEqual(restricted.earnings_status, "plan_restricted")
        self.assertEqual(malformed.earnings_status, "schema_error")

    def test_stock_snapshot_marks_invalid_nonempty_earnings_date_as_schema_error(self):
        snapshot = stock_snapshot_from_payloads(
            symbol="MSFT",
            theme="software",
            historical_payload=[],
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[{"date": "not-a-date"}],
            today="2026-06-01",
        )

        self.assertEqual(snapshot.earnings_status, "schema_error")
        self.assertIsNone(snapshot.event.next_earnings_date)

    def test_stock_snapshot_marks_guidance_and_macro_as_not_implemented(self):
        snapshot = stock_snapshot_from_payloads(
            symbol="MSFT",
            theme="software",
            historical_payload=[],
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[],
            today="2026-06-01",
        )

        self.assertEqual(snapshot.guidance_status, "not_implemented")
        self.assertEqual(snapshot.macro_status, "not_implemented")

    def test_stock_snapshot_accepts_stable_historical_price_rows(self):
        snapshot = stock_snapshot_from_payloads(
            symbol="MSFT",
            theme="software",
            historical_payload=[
                {"date": "2026-01-02", "open": 101, "high": 104, "low": 100, "close": 103, "volume": 2000},
                {"date": "2026-01-01", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1500},
            ],
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[],
            today="2026-06-01",
        )

        self.assertEqual([candle.date for candle in snapshot.candles], ["2026-01-01", "2026-01-02"])
        self.assertEqual(snapshot.candles[-1].close, 103)

    def test_stock_snapshot_parses_stable_ratio_aliases(self):
        snapshot = stock_snapshot_from_payloads(
            symbol="AMD",
            theme="semiconductors",
            historical_payload=[],
            profile_payload=[{"marketCap": 800_000_000_000, "averageVolume": 30_000_000}],
            ratios_payload=[
                {
                    "priceToEarningsRatioTTM": 166.57,
                    "priceToEarningsGrowthRatioTTM": 1.35,
                    "freeCashFlowPerShareTTM": 5.25,
                    "netProfitMarginTTM": 0.13,
                }
            ],
            metrics_payload=[{"freeCashFlowToEquityTTM": 6_000_000_000}],
            historical_metrics_payload=[
                {"earningsYield": 0.02},
                {"earningsYield": 0.04},
                {"earningsYield": 0},
            ],
            growth_payload=[{"growthRevenue": 0.34, "growthEPS": 1.64}],
            earnings_payload=[],
            today="2026-06-01",
        )

        self.assertEqual(snapshot.fundamentals.pe, 166.57)
        self.assertEqual(snapshot.fundamentals.peg, 1.35)
        self.assertEqual(snapshot.fundamentals.historical_pe, 37.5)
        self.assertTrue(snapshot.fundamentals.free_cash_flow_positive)
        self.assertEqual(snapshot.fundamentals.margin_trend, 0.13)

    def test_crypto_snapshot_parses_binance_and_coingecko_payloads_with_cvd_proxy(self):
        snapshot = crypto_snapshot_from_payloads(
            symbol="BTC",
            theme="crypto",
            klines_payload=[
                [1767225600000, "100", "105", "99", "104", "1000"],
                [1767312000000, "104", "108", "103", "107", "1200"],
            ],
            market_payload={
                "market_cap": 1_500_000_000_000,
                "market_cap_rank": 1,
                "total_volume": 45_000_000_000,
            },
            funding_payload=[{"fundingRate": "0.0100"}],
            open_interest_payload={"openInterest": "12000"},
            taker_payload=[
                {"buyVol": "120", "sellVol": "100"},
                {"buyVol": "80", "sellVol": "100"},
            ],
            coinbase_payload={"price": "108.07"},
        )

        self.assertEqual(snapshot.asset_type, "crypto")
        self.assertEqual(snapshot.fundamentals.average_volume, 45_000_000_000)
        self.assertEqual(snapshot.fundamentals.market_cap_rank, 1)
        self.assertAlmostEqual(snapshot.funding_rate, 0.01)
        self.assertAlmostEqual(snapshot.cvd_proxy, 0.0)
        self.assertIsNone(snapshot.coinbase_premium)
        self.assertIsNone(snapshot.liquidation_imbalance)
        self.assertIn("open_interest_change_unavailable", snapshot.missing_data)

    def test_crypto_snapshot_calculates_open_interest_change_from_history(self):
        snapshot = crypto_snapshot_from_payloads(
            symbol="ETH",
            theme="crypto",
            klines_payload=[[1767225600000, "100", "105", "99", "104", "1000"]],
            market_payload={"market_cap": 500_000_000_000, "total_volume": 20_000_000_000},
            funding_payload=[{"fundingRate": "0.0050"}],
            open_interest_payload=[
                {"sumOpenInterest": "10000"},
                {"sumOpenInterest": "12500"},
            ],
            taker_payload=[{"buyVol": "150", "sellVol": "100"}],
        )

        self.assertAlmostEqual(snapshot.open_interest_change, 0.25)
        self.assertNotIn("open_interest_change_unavailable", snapshot.missing_data)

    def test_crypto_snapshot_marks_optional_liquidations_as_missing(self):
        snapshot = crypto_snapshot_from_payloads(
            symbol="SOL",
            theme="crypto",
            klines_payload=[[1767225600000, "100", "105", "99", "104", "1000"]],
            market_payload={"market_cap": 90_000_000_000, "total_volume": 4_000_000_000},
            funding_payload=[],
            open_interest_payload={},
            taker_payload=[],
        )

        self.assertIsNone(snapshot.liquidation_imbalance)
        self.assertIn("liquidations_unavailable", snapshot.missing_data)

    def test_crypto_snapshot_keeps_available_candle_provenance_over_binance_flow_default(self):
        flow_provenance = binance_crypto_flow_from_payloads(
            funding_payload=[],
            open_interest_payload=[],
            taker_payload=[],
            liquidation_payload=[],
        )["metric_provenance"]

        snapshot = crypto_snapshot_from_payloads(
            symbol="BTC",
            theme="crypto",
            klines_payload=[[1767225600000, "100", "105", "99", "104", "1000"]],
            market_payload={},
            funding_payload=[],
            open_interest_payload=[],
            taker_payload=[],
            data_fetch_metadata=DataFetchMetadata(
                provider="coingecko",
                endpoint="market_chart",
                source_timestamp="2026-01-01",
                granularity="daily",
                market_data_kind="eod_candle",
            ),
            crypto_metric_provenance=flow_provenance,
        )

        self.assertEqual(snapshot.crypto_metric_provenance["candles"]["status"], "available")
        self.assertEqual(snapshot.crypto_metric_provenance["spot"]["status"], "available")
        self.assertEqual(snapshot.crypto_metric_provenance["candles"]["provider"], "coingecko")
        self.assertEqual(snapshot.crypto_metric_provenance["candles"]["endpoint"], "market_chart")

    def test_crypto_snapshot_does_not_fabricate_premium_from_daily_candle_and_current_product_price(self):
        snapshot = crypto_snapshot_from_payloads(
            symbol="BTC",
            theme="crypto",
            klines_payload=[[1767225600000, "100", "105", "99", "104", "1000"]],
            market_payload={},
            funding_payload=[],
            open_interest_payload={},
            taker_payload=[],
            coinbase_payload={"price": "108.07"},
        )

        self.assertIsNone(snapshot.coinbase_premium)
        self.assertEqual(snapshot.crypto_metric_provenance["premium"]["status"], "incompatible_time")
        self.assertEqual(snapshot.crypto_metric_provenance["premium"]["provider"], "coinbase")

    def test_liquidation_rows_never_publish_a_numeric_imbalance(self):
        liquidation_rows = [
            {"side": "SELL", "executedQty": "2", "averagePrice": "100"},
            {"side": "BUY", "executedQty": "1", "averagePrice": "100"},
        ]
        snapshot = crypto_snapshot_from_payloads(
            symbol="BTC",
            theme="crypto",
            klines_payload=[[1767225600000, "100", "105", "99", "104", "1000"]],
            market_payload={},
            funding_payload=[],
            open_interest_payload={},
            taker_payload=[],
            liquidation_payload=liquidation_rows,
        )
        flow = binance_crypto_flow_from_payloads(
            funding_payload=[{"fundingRate": "0.0025"}],
            open_interest_payload=[
                {"sumOpenInterest": "10000"},
                {"sumOpenInterest": "12500"},
            ],
            taker_payload=[{"buyVol": "150", "sellVol": "100"}],
            liquidation_payload=liquidation_rows,
        )

        self.assertIsNone(snapshot.liquidation_imbalance)
        self.assertEqual(snapshot.crypto_metric_provenance["liquidations"]["status"], "not_implemented")
        self.assertIsNone(flow["liquidation_imbalance"])
        self.assertEqual(flow["metric_provenance"]["liquidations"]["status"], "not_implemented")
        self.assertIn("liquidations_unavailable", flow["limitations"])
        self.assertEqual(flow["source"], "binance")
        self.assertEqual(flow["funding_rate_basis"], "8h_equivalent")
        self.assertAlmostEqual(flow["funding_rate"], 0.0025)
        self.assertAlmostEqual(flow["open_interest_change"], 0.25)
        self.assertAlmostEqual(flow["cvd_proxy"], 0.2)
        self.assertTrue(flow["cvd_is_proxy"])

    def test_binance_crypto_flow_normalizes_adjusted_funding_interval_to_eight_hours(self):
        flow = binance_crypto_flow_from_payloads(
            funding_payload=[{"fundingRate": "0.0002"}],
            funding_info_payload=[{"symbol": "BTCUSDT", "fundingIntervalHours": 1}],
            symbol="BTCUSDT",
            open_interest_payload=[],
            taker_payload=[],
            liquidation_payload=[],
        )

        self.assertAlmostEqual(flow["funding_rate"], 0.0016)

    def test_hyperliquid_crypto_flow_finds_hype_asset_context(self):
        flow = hyperliquid_crypto_flow_from_payload(
            [
                {"universe": [{"name": "BTC"}, {"name": "HYPE"}]},
                [
                    {"funding": "0.0001", "openInterest": "100"},
                    {"funding": "0.0002", "openInterest": "250", "markPx": "35.5", "dayNtlVlm": "5000000"},
                ],
            ],
            symbol="HYPE",
        )

        self.assertEqual(flow["source"], "hyperliquid")
        self.assertEqual(flow["funding_rate_basis"], "8h_equivalent")
        self.assertAlmostEqual(flow["funding_rate"], 0.0016)
        self.assertAlmostEqual(flow["open_interest"], 250)
        self.assertAlmostEqual(flow["mark_price"], 35.5)
        self.assertIn("cvd_proxy_unavailable", flow["limitations"])
        self.assertIn("open_interest_change_unavailable", flow["limitations"])


if __name__ == "__main__":
    unittest.main()
