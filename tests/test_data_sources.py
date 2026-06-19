import unittest

from advisor.data_sources import (
    AlphaVantageSource,
    BinanceSource,
    CoinGeckoSource,
    CoinbaseSource,
    FmpSource,
    HyperliquidSource,
)


class DataSourceTests(unittest.TestCase):
    def test_sources_expose_expected_free_first_endpoints(self):
        self.assertIn("financialmodelingprep.com", FmpSource("demo").historical_prices_url("MSFT"))
        self.assertIn("/stable/historical-price-eod/full", FmpSource("demo").historical_prices_url("MSFT"))
        self.assertIn("/stable/profile", FmpSource("demo").profile_url("MSFT"))
        self.assertIn("/stable/key-metrics-ttm", FmpSource("demo").key_metrics_url("MSFT"))
        self.assertIn("/stable/ratios-ttm", FmpSource("demo").ratios_url("MSFT"))
        self.assertIn("earnings-calendar", FmpSource("demo").earnings_calendar_url("MSFT"))
        self.assertIn("/stable/key-metrics", FmpSource("demo").historical_key_metrics_url("MSFT"))
        self.assertIn("/stable/income-statement-growth", FmpSource("demo").income_statement_growth_url("MSFT"))
        self.assertIn("/fapi/v1/klines", BinanceSource().klines_url("BTCUSDT"))
        self.assertIn("/fapi/v1/fundingRate", BinanceSource().funding_rate_url("BTCUSDT"))
        self.assertIn("/fapi/v1/fundingInfo", BinanceSource().funding_info_url())
        self.assertIn("/fapi/v1/openInterest", BinanceSource().open_interest_url("BTCUSDT"))
        self.assertIn("/futures/data/openInterestHist", BinanceSource().open_interest_history_url("BTCUSDT"))
        self.assertIn("/fapi/v1/allForceOrders", BinanceSource().liquidation_orders_url("BTCUSDT"))
        self.assertIn("api.coingecko.com", CoinGeckoSource("demo").markets_url(["bitcoin"]))
        self.assertEqual(HyperliquidSource().info_url(), "https://api.hyperliquid.xyz/info")
        self.assertEqual(
            HyperliquidSource().candle_snapshot_payload("HYPE", start_time_ms=1, end_time_ms=2)["type"],
            "candleSnapshot",
        )
        self.assertEqual(HyperliquidSource().meta_and_asset_contexts_payload()["type"], "metaAndAssetCtxs")
        self.assertIn("api.coinbase.com", CoinbaseSource().public_candles_url("BTC-USD"))
        self.assertIn("api.coinbase.com", CoinbaseSource().public_product_url("BTC-USD"))
        self.assertIn("alphavantage.co", AlphaVantageSource("demo").daily_adjusted_url("MSFT"))


if __name__ == "__main__":
    unittest.main()
