import unittest
import tempfile
from pathlib import Path

from advisor.config import AdvisorConfig
from advisor.live_loader import LiveDataLoader


class LiveLoaderTests(unittest.TestCase):
    def test_live_loader_uses_free_first_sources_with_fake_transport(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append((url, payload, headers or {}))
            if payload and payload.get("type") == "candleSnapshot":
                return [
                    {"t": 1767225600000, "o": "100", "h": "105", "l": "99", "c": "104", "v": "1000"},
                    {"t": 1767312000000, "o": "104", "h": "108", "l": "103", "c": "107", "v": "1200"},
                ]
            if payload and payload.get("type") == "metaAndAssetCtxs":
                return [
                    {"universe": [{"name": "BTC"}, {"name": "HYPE"}]},
                    [
                        {"funding": "0.0001", "openInterest": "100"},
                        {"funding": "0.0002", "openInterest": "250", "markPx": "35.5", "dayNtlVlm": "5000000"},
                    ],
                ]
            if "historical-price-eod/full" in url:
                return {
                    "historical": [
                        {"date": "2026-01-02", "open": 101, "high": 104, "low": 100, "close": 103, "volume": 2000},
                        {"date": "2026-01-01", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1500},
                    ]
                }
            if "stable/profile" in url:
                return [{"mktCap": 3_000_000_000_000, "volAvg": 20_000_000}]
            if "ratios-ttm" in url:
                return [{"priceEarningsRatioTTM": 32, "pegRatioTTM": 2.1, "grossProfitMarginTTM": 0.68}]
            if "key-metrics-ttm" in url:
                return [{"freeCashFlowPerShareTTM": 4.5}]
            if "/stable/key-metrics" in url:
                return [{"peRatio": 30}, {"peRatio": 40}, {"peRatio": 50}]
            if "income-statement-growth" in url:
                return [{"growthRevenue": 0.16, "growthEPS": 0.12}]
            if "earnings-calendar" in url:
                return [{"date": "2026-06-10"}]
            if "coins/markets" in url:
                return [{"id": "bitcoin", "market_cap": 1_500_000_000_000, "total_volume": 45_000_000_000}]
            if "/klines" in url:
                return [[1767225600000, "100", "105", "99", "104", "1000"]]
            if "fundingRate" in url:
                return [{"fundingRate": "0.01"}]
            if "openInterestHist" in url:
                return [{"sumOpenInterest": "10000"}, {"sumOpenInterest": "11000"}]
            if "openInterest" in url:
                return {"openInterest": "12000"}
            if "takerlongshortRatio" in url:
                return [{"buyVol": "120", "sellVol": "100"}]
            if "allForceOrders" in url:
                return [{"side": "SELL", "executedQty": "2", "averagePrice": "100"}]
            if "api.coinbase.com" in url:
                return {"price": "105.04"}
            return {}

        config = AdvisorConfig.default()
        config.stock_watchlist = ["MSFT"]
        config.crypto_watchlist = ["BTC", "HYPE"]
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"
        config.coinbase_api_key = "demo"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-06-01")

        snapshots = loader.load_snapshots()

        self.assertEqual([snapshot.symbol for snapshot in snapshots], ["MSFT", "BTC", "HYPE"])
        btc = next(snapshot for snapshot in snapshots if snapshot.symbol == "BTC")
        msft = next(snapshot for snapshot in snapshots if snapshot.symbol == "MSFT")
        hype = next(snapshot for snapshot in snapshots if snapshot.symbol == "HYPE")
        self.assertEqual(msft.fundamentals.historical_pe, 40)
        self.assertEqual(msft.fundamentals.revenue_growth, 0.16)
        self.assertAlmostEqual(btc.coinbase_premium, 0.01)
        self.assertAlmostEqual(btc.open_interest_change, 0.10)
        self.assertAlmostEqual(hype.funding_rate, 0.0016)
        self.assertIsNotNone(btc.liquidation_imbalance)
        self.assertTrue(any("financialmodelingprep.com" in call[0] for call in calls))
        self.assertTrue(any("/stable/key-metrics" in call[0] for call in calls))
        self.assertTrue(any("income-statement-growth" in call[0] for call in calls))
        self.assertTrue(any("fapi.binance.com" in call[0] for call in calls))
        self.assertTrue(any("openInterestHist" in call[0] for call in calls))
        self.assertTrue(any("allForceOrders" in call[0] for call in calls))
        self.assertTrue(any("api.coingecko.com" in call[0] for call in calls))
        self.assertTrue(any(call[2].get("x-cg-demo-api-key") == "demo" for call in calls))
        self.assertTrue(any("api.coinbase.com" in call[0] for call in calls))
        self.assertTrue(any(call[1] and call[1].get("type") == "candleSnapshot" for call in calls))
        self.assertTrue(any(call[1] and call[1].get("type") == "metaAndAssetCtxs" for call in calls))

    def test_live_loader_attaches_alphavantage_news_to_each_symbol(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append(url)
            if "NEWS_SENTIMENT" in url:
                return {
                    "feed": [
                        {
                            "title": "Chip demand improves",
                            "source": "Example Wire",
                            "time_published": "20260630T120000",
                            "overall_sentiment_label": "Bullish",
                            "ticker_sentiment": [{"ticker": "AMD"}, {"ticker": "CRYPTO:BTC"}],
                        },
                        {
                            "title": "Software demand slows",
                            "source": "Example Wire",
                            "time_published": "20260630T130000",
                            "overall_sentiment_label": "Bearish",
                            "ticker_sentiment": [{"ticker": "MSFT"}],
                        },
                    ]
                }
            if "historical-price-eod/full" in url:
                return {
                    "historical": [
                        {"date": "2026-01-02", "open": 101, "high": 104, "low": 100, "close": 103, "volume": 2000},
                        {"date": "2026-01-01", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1500},
                    ]
                }
            if "stable/profile" in url:
                return [{"mktCap": 3_000_000_000_000, "volAvg": 20_000_000}]
            if "ratios-ttm" in url:
                return [{"priceEarningsRatioTTM": 32, "pegRatioTTM": 2.1, "grossProfitMarginTTM": 0.68}]
            if "key-metrics-ttm" in url:
                return [{"freeCashFlowPerShareTTM": 4.5}]
            if "/stable/key-metrics" in url:
                return [{"peRatio": 30}, {"peRatio": 40}]
            if "income-statement-growth" in url:
                return [{"growthRevenue": 0.16, "growthEPS": 0.12}]
            if "earnings-calendar" in url:
                return [{"date": "2026-06-10"}]
            if "coins/markets" in url:
                return [{"id": "bitcoin", "market_cap": 1_500_000_000_000, "total_volume": 45_000_000_000}]
            if "/klines" in url:
                return [[1767225600000, "100", "105", "99", "104", "1000"]]
            if "fundingRate" in url:
                return [{"fundingRate": "0.01"}]
            if "fundingInfo" in url:
                return []
            if "openInterestHist" in url:
                return [{"sumOpenInterest": "10000"}, {"sumOpenInterest": "11000"}]
            if "takerlongshortRatio" in url:
                return [{"buyVol": "120", "sellVol": "100"}]
            if "allForceOrders" in url:
                return []
            return {}

        config = AdvisorConfig.default()
        config.stock_watchlist = ["AMD", "MSFT"]
        config.crypto_watchlist = ["BTC"]
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"
        config.alphavantage_api_key = "alpha"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-06-01")

        snapshots = loader.load_snapshots()

        by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
        self.assertEqual(by_symbol["AMD"].news_events[0]["market_effect"], "risk_on")
        self.assertEqual(by_symbol["BTC"].news_events[0]["market_effect"], "risk_on")
        self.assertEqual(by_symbol["MSFT"].news_events[0]["market_effect"], "risk_off")
        self.assertEqual(len([url for url in calls if "NEWS_SENTIMENT" in url]), 1)

    def test_live_loader_uses_alphavantage_as_weak_price_fallback(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append(url)
            if "historical-price-eod/full" in url:
                return {}
            if "alphavantage.co" in url:
                return {
                    "Time Series (Daily)": {
                        "2026-01-02": {
                            "1. open": "101",
                            "2. high": "104",
                            "3. low": "100",
                            "4. close": "103",
                            "6. volume": "2000",
                        },
                        "2026-01-01": {
                            "1. open": "100",
                            "2. high": "102",
                            "3. low": "99",
                            "4. close": "101",
                            "6. volume": "1500",
                        },
                    }
                }
            if "stable/profile" in url:
                return [{"mktCap": 3_000_000_000_000, "volAvg": 20_000_000}]
            if "ratios-ttm" in url:
                return [{"priceEarningsRatioTTM": 32, "pegRatioTTM": 2.1}]
            if "key-metrics-ttm" in url:
                return [{"revenueGrowth": 0.16, "epsgrowth": 0.12, "freeCashFlowPerShareTTM": 4.5}]
            if "earnings-calendar" in url:
                return [{"date": "2026-06-10"}]
            return {}

        config = AdvisorConfig.default()
        config.stock_watchlist = ["MSFT"]
        config.crypto_watchlist = []
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"
        config.alphavantage_api_key = "alpha"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-06-01")

        snapshots = loader.load_snapshots()

        self.assertEqual(snapshots[0].candles[0].date, "2026-01-01")
        self.assertIn("alphavantage_price_fallback", snapshots[0].missing_data)
        self.assertTrue(any("alphavantage.co" in url for url in calls))

    def test_live_loader_does_not_repeat_fmp_after_rate_limit_and_uses_fallback(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append(url)
            if "financialmodelingprep.com" in url:
                raise RuntimeError("http_error:429:Limit Reach Retry-After: 60")
            if "alphavantage.co" in url:
                return {
                    "Time Series (Daily)": {
                        "2026-01-02": {
                            "1. open": "101",
                            "2. high": "104",
                            "3. low": "100",
                            "4. close": "103",
                            "6. volume": "2000",
                        },
                        "2026-01-01": {
                            "1. open": "100",
                            "2. high": "102",
                            "3. low": "99",
                            "4. close": "101",
                            "6. volume": "1500",
                        },
                    }
                }
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            config = AdvisorConfig.default()
            config.stock_watchlist = ["MSFT"]
            config.crypto_watchlist = []
            config.fmp_api_key = "demo"
            config.coingecko_api_key = "demo"
            config.alphavantage_api_key = "alpha"
            loader = LiveDataLoader(
                config,
                fetch_json=fake_fetch,
                today="2026-06-01",
                db_path=Path(tmp) / "advisor.db",
            )

            snapshots = loader.load_snapshots()

        fmp_calls = [url for url in calls if "financialmodelingprep.com" in url]
        self.assertEqual(len(fmp_calls), 1)
        self.assertEqual(loader.provider_statuses["fmp"], "rate_limited")
        self.assertEqual(loader.provider_retry_after["fmp"], "60")
        self.assertGreater(loader.skipped_provider_calls_due_to_rate_limit["fmp"], 0)
        self.assertIn("alphavantage_price_fallback", snapshots[0].missing_data)

    def test_live_loader_accepts_stable_fmp_historical_price_rows_without_alpha_fallback(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append(url)
            if "historical-price-eod/full" in url:
                return [
                    {"date": "2026-01-02", "open": 101, "high": 104, "low": 100, "close": 103, "volume": 2000},
                    {"date": "2026-01-01", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1500},
                ]
            if "stable/profile" in url:
                return [{"mktCap": 3_000_000_000_000, "volAvg": 20_000_000}]
            if "ratios-ttm" in url:
                return [{"priceEarningsRatioTTM": 32, "pegRatioTTM": 2.1}]
            if "key-metrics-ttm" in url:
                return [{"freeCashFlowPerShareTTM": 4.5}]
            if "income-statement-growth" in url:
                return [{"growthRevenue": 0.16, "growthEPS": 0.12}]
            if "earnings-calendar" in url:
                return [{"date": "2026-06-10"}]
            return []

        config = AdvisorConfig.default()
        config.stock_watchlist = ["MSFT"]
        config.crypto_watchlist = []
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"
        config.alphavantage_api_key = "alpha"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-06-01")

        snapshots = loader.load_snapshots()

        self.assertEqual(snapshots[0].candles[0].date, "2026-01-01")
        self.assertNotIn("alphavantage_price_fallback", snapshots[0].missing_data)
        self.assertFalse(any("TIME_SERIES_DAILY_ADJUSTED" in url for url in calls))

    def test_live_loader_marks_stock_price_subscription_block_as_missing_and_continues(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append(url)
            if "historical-price-eod/full" in url and "symbol=HIMS" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter")
            if "historical-price-eod/full" in url:
                return [
                    {"date": "2026-01-02", "open": 101, "high": 104, "low": 100, "close": 103, "volume": 2000},
                    {"date": "2026-01-01", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1500},
                ]
            if "stable/profile" in url:
                return [{"mktCap": 3_000_000_000_000, "volAvg": 20_000_000}]
            if "ratios-ttm" in url:
                return [{"priceEarningsRatioTTM": 32, "pegRatioTTM": 2.1}]
            if "key-metrics-ttm" in url:
                return [{"freeCashFlowPerShareTTM": 4.5}]
            if "income-statement-growth" in url:
                return [{"growthRevenue": 0.16, "growthEPS": 0.12}]
            if "earnings-calendar" in url:
                return []
            return []

        config = AdvisorConfig.default()
        config.stock_watchlist = ["HIMS", "MSFT"]
        config.crypto_watchlist = []
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-06-01")

        snapshots = loader.load_snapshots()

        hims = next(snapshot for snapshot in snapshots if snapshot.symbol == "HIMS")
        msft = next(snapshot for snapshot in snapshots if snapshot.symbol == "MSFT")
        self.assertEqual(hims.candles, [])
        self.assertIn("fmp_price_unavailable", hims.missing_data)
        self.assertEqual(msft.candles[-1].close, 103)
        self.assertFalse(any("stable/profile" in url and "symbol=HIMS" in url for url in calls))

    def test_live_loader_marks_unavailable_benchmark_history_as_empty(self):
        def fake_fetch(url, *, payload=None, headers=None):
            if "symbol=SPY" in url:
                return [
                    {"date": "2026-01-02", "open": 101, "high": 104, "low": 100, "close": 103, "volume": 2000},
                ]
            if "symbol=QQQ" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter")
            return []

        config = AdvisorConfig.default()
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-06-01")

        benchmarks = loader.load_benchmarks()

        self.assertEqual(len(benchmarks["SPY"]), 1)
        self.assertEqual(benchmarks["QQQ"], [])

    def test_live_loader_collects_crypto_flow_from_binance_and_hyperliquid(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append((url, payload))
            if payload and payload.get("type") == "metaAndAssetCtxs":
                return [
                    {"universe": [{"name": "BTC"}, {"name": "HYPE"}]},
                    [
                        {"funding": "0.0001", "openInterest": "100"},
                        {"funding": "0.0002", "openInterest": "250", "markPx": "35.5", "dayNtlVlm": "5000000"},
                    ],
                ]
            if "fundingRate" in url:
                return [{"fundingRate": "0.0025"}]
            if "openInterestHist" in url:
                return [{"sumOpenInterest": "10000"}, {"sumOpenInterest": "12500"}]
            if "takerlongshortRatio" in url:
                return [{"buyVol": "150", "sellVol": "100"}]
            if "allForceOrders" in url:
                return []
            return {}

        config = AdvisorConfig.default()
        config.crypto_watchlist = ["BTC", "HYPE"]
        loader = LiveDataLoader(config, fetch_json=fake_fetch)

        flows = loader.collect_crypto_flow()

        self.assertEqual(set(flows), {"BTC", "HYPE"})
        self.assertAlmostEqual(flows["BTC"]["open_interest_change"], 0.25)
        self.assertAlmostEqual(flows["HYPE"]["funding_rate"], 0.0016)
        self.assertTrue(any(call[1] and call[1].get("type") == "metaAndAssetCtxs" for call in calls))

    def test_live_loader_fetches_binance_funding_intervals_once_and_normalizes_rates(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append(url)
            if "fundingInfo" in url:
                return [{"symbol": "BTCUSDT", "fundingIntervalHours": 1}]
            if "fundingRate" in url:
                return [{"fundingRate": "0.0002"}]
            return []

        config = AdvisorConfig.default()
        config.crypto_watchlist = ["BTC", "ETH"]
        loader = LiveDataLoader(config, fetch_json=fake_fetch)

        flows = loader.collect_crypto_flow()

        self.assertAlmostEqual(flows["BTC"]["funding_rate"], 0.0016)
        self.assertAlmostEqual(flows["ETH"]["funding_rate"], 0.0002)
        self.assertEqual(sum("fundingInfo" in url for url in calls), 1)

    def test_live_loader_degrades_optional_crypto_flow_http_error(self):
        def fake_fetch(url, *, payload=None, headers=None):
            if "fundingRate" in url:
                return [{"fundingRate": "0.0025"}]
            if "openInterestHist" in url:
                return [{"sumOpenInterest": "10000"}, {"sumOpenInterest": "12500"}]
            if "takerlongshortRatio" in url:
                return [{"buyVol": "150", "sellVol": "100"}]
            if "allForceOrders" in url:
                raise RuntimeError("http_error:400")
            return {}

        config = AdvisorConfig.default()
        config.crypto_watchlist = ["BTC"]
        loader = LiveDataLoader(config, fetch_json=fake_fetch)

        flows = loader.collect_crypto_flow()

        self.assertIn("liquidations_unavailable", flows["BTC"]["limitations"])

    def test_live_loader_raises_on_required_provider_error_payload(self):
        def fake_fetch(url, *, payload=None, headers=None):
            if "historical-price-eod/full" in url:
                return {"Error Message": "Invalid API call."}
            return {}

        config = AdvisorConfig.default()
        config.stock_watchlist = ["MSFT"]
        config.crypto_watchlist = []
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-06-01")

        with self.assertRaisesRegex(RuntimeError, "provider_api_error:fmp"):
            loader.load_snapshots()

    def test_live_loader_wraps_required_http_error_with_provider_context(self):
        def fake_fetch(url, *, payload=None, headers=None):
            if "coins/markets" in url:
                raise RuntimeError("http_error:403")
            return {}

        config = AdvisorConfig.default()
        config.stock_watchlist = []
        config.crypto_watchlist = ["BTC"]
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-06-01")

        with self.assertRaisesRegex(
            RuntimeError,
            "provider_fetch_error:coingecko:fundamentals:http_error:403",
        ):
            loader.load_snapshots()

    def test_live_loader_uses_coingecko_history_when_binance_is_restricted(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append(url)
            if "coins/markets" in url:
                return [{"id": "bitcoin", "market_cap": 1_500_000_000_000, "total_volume": 45_000_000_000}]
            if "/klines" in url:
                raise RuntimeError(
                    "http_error:451:Service unavailable from a restricted location"
                )
            if "market_chart" in url:
                return {
                    "prices": [
                        [1767225600000, 100.0],
                        [1767312000000, 104.0],
                        [1767398400000, 107.0],
                    ],
                    "total_volumes": [
                        [1767225600000, 1000.0],
                        [1767312000000, 1200.0],
                        [1767398400000, 1400.0],
                    ],
                }
            return {}

        config = AdvisorConfig.default()
        config.stock_watchlist = []
        config.crypto_watchlist = ["BTC"]
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-06-01")

        snapshots = loader.load_snapshots()

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].symbol, "BTC")
        self.assertEqual([candle.close for candle in snapshots[0].candles], [100.0, 104.0, 107.0])
        self.assertIn("binance_restricted_location", snapshots[0].missing_data)
        self.assertIn("binance_flow_unavailable", snapshots[0].missing_data)
        self.assertIn("coingecko_price_history_fallback", snapshots[0].missing_data)
        self.assertNotIn("price_history_unavailable", snapshots[0].missing_data)
        self.assertTrue(any("market_chart" in url for url in calls))

    def test_live_loader_degrades_optional_liquidation_provider_error_payload(self):
        def fake_fetch(url, *, payload=None, headers=None):
            if "coins/markets" in url:
                return [{"id": "bitcoin", "market_cap": 1_500_000_000_000, "total_volume": 45_000_000_000}]
            if "/klines" in url:
                return [[1767225600000, "100", "105", "99", "104", "1000"]]
            if "fundingRate" in url:
                return [{"fundingRate": "0.01"}]
            if "openInterestHist" in url:
                return [{"sumOpenInterest": "10000"}, {"sumOpenInterest": "11000"}]
            if "takerlongshortRatio" in url:
                return [{"buyVol": "120", "sellVol": "100"}]
            if "allForceOrders" in url:
                return {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}
            return {}

        config = AdvisorConfig.default()
        config.stock_watchlist = []
        config.crypto_watchlist = ["BTC"]
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-06-01")

        snapshots = loader.load_snapshots()

        self.assertEqual(snapshots[0].symbol, "BTC")
        self.assertIn("liquidations_unavailable", snapshots[0].missing_data)

    def test_live_loader_uses_cache_and_api_limits(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append((url, payload))
            if "historical-price-eod/full" in url:
                return {
                    "historical": [
                        {"date": "2026-01-01", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1500},
                    ]
                }
            if "stable/profile" in url:
                return [{"mktCap": 3_000_000_000_000, "volAvg": 20_000_000}]
            if "ratios-ttm" in url:
                return [{"priceEarningsRatioTTM": 32, "pegRatioTTM": 2.1}]
            if "key-metrics-ttm" in url:
                return [{"revenueGrowth": 0.16, "epsgrowth": 0.12, "freeCashFlowPerShareTTM": 4.5}]
            if "earnings-calendar" in url:
                return [{"date": "2026-06-10"}]
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            config = AdvisorConfig.default()
            config.stock_watchlist = ["MSFT"]
            config.crypto_watchlist = []
            config.fmp_api_key = "demo"
            config.coingecko_api_key = "demo"
            config.api_limits["fmp"] = 10
            loader = LiveDataLoader(
                config,
                fetch_json=fake_fetch,
                today="2026-06-01",
                db_path=Path(tmp) / "advisor.db",
            )

            loader.load_snapshots()
            loader.load_snapshots()

        self.assertEqual(len(calls), 7)

    def test_live_loader_raises_when_api_limit_is_exhausted_without_cache(self):
        def fake_fetch(url, *, payload=None, headers=None):
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            config = AdvisorConfig.default()
            config.stock_watchlist = ["MSFT"]
            config.crypto_watchlist = []
            config.fmp_api_key = "demo"
            config.coingecko_api_key = "demo"
            config.api_limits["fmp"] = 0
            loader = LiveDataLoader(
                config,
                fetch_json=fake_fetch,
                today="2026-06-01",
                db_path=Path(tmp) / "advisor.db",
            )

            with self.assertRaises(RuntimeError):
                loader.load_snapshots()


if __name__ == "__main__":
    unittest.main()
