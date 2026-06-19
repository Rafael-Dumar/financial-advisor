import unittest

from advisor.data_pipeline import (
    binance_crypto_flow_from_payloads,
    crypto_snapshot_from_payloads,
    hyperliquid_crypto_flow_from_payload,
    stock_snapshot_from_payloads,
)


class DataPipelineTests(unittest.TestCase):
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
            liquidation_payload=[
                {"side": "SELL", "executedQty": "2", "averagePrice": "100"},
                {"side": "BUY", "executedQty": "1", "averagePrice": "100"},
            ],
        )

        self.assertEqual(snapshot.asset_type, "crypto")
        self.assertEqual(snapshot.fundamentals.average_volume, 45_000_000_000)
        self.assertEqual(snapshot.fundamentals.market_cap_rank, 1)
        self.assertAlmostEqual(snapshot.funding_rate, 0.01)
        self.assertAlmostEqual(snapshot.cvd_proxy, 0.0)
        self.assertAlmostEqual(snapshot.coinbase_premium, 0.01)
        self.assertAlmostEqual(snapshot.liquidation_imbalance, 1 / 3)
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

    def test_binance_crypto_flow_is_normalized_and_marks_cvd_as_proxy(self):
        flow = binance_crypto_flow_from_payloads(
            funding_payload=[{"fundingRate": "0.0025"}],
            open_interest_payload=[
                {"sumOpenInterest": "10000"},
                {"sumOpenInterest": "12500"},
            ],
            taker_payload=[{"buyVol": "150", "sellVol": "100"}],
            liquidation_payload=[{"side": "SELL", "executedQty": "2", "averagePrice": "100"}],
        )

        self.assertEqual(flow["source"], "binance")
        self.assertEqual(flow["funding_rate_basis"], "8h_equivalent")
        self.assertAlmostEqual(flow["funding_rate"], 0.0025)
        self.assertAlmostEqual(flow["open_interest_change"], 0.25)
        self.assertAlmostEqual(flow["cvd_proxy"], 0.2)
        self.assertTrue(flow["cvd_is_proxy"])
        self.assertIn("liquidations_history_may_be_incomplete", flow["limitations"])

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
