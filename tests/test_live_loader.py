import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from advisor.config import AdvisorConfig
from advisor.audit import AuditRecorder, build_provider_audit
from advisor.live_loader import LiveDataLoader, _cache_key


class LiveLoaderTests(unittest.TestCase):
    def test_live_loader_uses_one_batch_quote_and_keeps_eod_candles_separate(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append(url)
            if "/stable/quote" in url:
                return [
                    {"symbol": "AMD", "price": 104.5, "timestamp": 1783776600, "previousClose": 101, "change": 3.5, "changesPercentage": 3.465},
                    {"symbol": "NVDA", "price": 204.5, "timestamp": 1783776600, "previousClose": 201, "change": 3.5, "changesPercentage": 1.741},
                ]
            if "historical-price-eod/full" in url:
                return [{"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}]
            return []

        config = AdvisorConfig.default()
        config.stock_watchlist = ["AMD", "NVDA"]
        config.crypto_watchlist = []
        config.fmp_api_key = "present"
        config.coingecko_api_key = "present"

        snapshots = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-10").load_snapshots()

        quote_calls = [url for url in calls if "/stable/quote" in url]
        amd = next(snapshot for snapshot in snapshots if snapshot.symbol == "AMD")
        self.assertEqual(len(quote_calls), 1)
        self.assertIn("AMD%2CNVDA", quote_calls[0])
        self.assertEqual(amd.candles[-1].date, "2026-07-09")
        self.assertEqual(amd.quote_price, 104.5)
        self.assertTrue(amd.quote_is_intraday)

    def test_live_loader_marks_402_batch_quote_as_unavailable_without_relabeling_candles(self):
        def fake_fetch(url, *, payload=None, headers=None):
            if "/stable/quote" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter")
            if "historical-price-eod/full" in url:
                return [{"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}]
            return []

        config = AdvisorConfig.default()
        config.stock_watchlist = ["AMD"]
        config.crypto_watchlist = []
        config.fmp_api_key = "present"
        config.coingecko_api_key = "present"

        snapshot = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-10").load_snapshots()[0]

        self.assertEqual(snapshot.quote_status, "unavailable")
        self.assertIsNone(snapshot.quote_price)
        self.assertFalse(snapshot.quote_is_intraday)
        self.assertEqual(snapshot.candles[-1].date, "2026-07-09")

    def test_live_loader_suppresses_repeated_plan_restricted_price_capability_within_one_run(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append(url)
            if "historical-price-eod/full" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter")
            if "historical-price-eod/light" in url:
                return [{"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}]
            return []

        config = AdvisorConfig.default()
        config.stock_watchlist = ["AMD", "NVDA"]
        config.crypto_watchlist = []
        config.fmp_api_key = "present"
        config.coingecko_api_key = "present"

        snapshots = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-10").load_snapshots()

        self.assertEqual(len([url for url in calls if "historical-price-eod/full" in url]), 1)
        self.assertTrue(all(snapshot.data_source == "fmp_light" for snapshot in snapshots))
        capability = next(
            item
            for item in snapshots[-1].provider_capabilities
            if item.provider == "fmp" and item.capability == "historical_prices"
        )
        self.assertFalse(capability.supported_by_plan)
        self.assertEqual(capability.last_status, "unsupported_by_plan")

    def test_suppressed_plan_restricted_capability_audit_keeps_plan_restricted_cause(self):
        def fake_fetch(url, *, payload=None, headers=None):
            raise RuntimeError("http_error:402:Premium Query Parameter")

        config = AdvisorConfig.default()
        config.fmp_api_key = "present"
        recorder = AuditRecorder()
        loader = LiveDataLoader(config, fetch_json=fake_fetch, audit_recorder=recorder)
        url = loader.fmp.historical_prices_url("AMD")

        with self.assertRaisesRegex(RuntimeError, "http_error:402"):
            loader._fetch("fmp", "prices", url)
        with self.assertRaisesRegex(RuntimeError, "provider_capability_unavailable"):
            loader._fetch("fmp", "prices", url)

        calls = build_provider_audit(config, recorder, network_mode="live")["fmp"]["calls"]
        suppressed = next(call for call in calls if "provider_capability_unavailable" in str(call["error"]))
        self.assertEqual(suppressed["failure_cause"], "plan_restricted")
        self.assertEqual(suppressed["status"], "unsupported_by_plan")

    def test_live_loader_keeps_alpha_news_and_sec_filings_statuses_separate_without_alpha_key(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append(url)
            if "data.sec.gov/submissions" in url:
                return {
                    "filings": {
                        "recent": {
                            "form": ["8-K"],
                            "filingDate": ["2026-06-28"],
                            "accessionNumber": ["0000002488-26-000001"],
                            "primaryDocument": ["amd-20260628.htm"],
                        }
                    }
                }
            if "historical-price-eod/full" in url:
                return [{"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}]
            return []

        config = AdvisorConfig.default()
        config.stock_watchlist = ["AMD"]
        config.crypto_watchlist = []
        config.fmp_api_key = "present"
        config.coingecko_api_key = "present"

        snapshot = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-10").load_snapshots()[0]

        self.assertEqual(snapshot.news_status, "not_configured")
        self.assertEqual(snapshot.sec_filings_status, "available")
        self.assertTrue(snapshot.news_events)
        self.assertFalse(any("NEWS_SENTIMENT" in url for url in calls))

    def test_live_loader_does_not_label_available_quote_without_timestamp_as_intraday(self):
        def fake_fetch(url, *, payload=None, headers=None):
            if "/stable/quote" in url:
                return [{"symbol": "AMD", "price": 104.5, "previousClose": 101, "change": 3.5, "changesPercentage": 3.465}]
            if "historical-price-eod/full" in url:
                return [{"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}]
            return []

        config = AdvisorConfig.default()
        config.stock_watchlist = ["AMD"]
        config.crypto_watchlist = []
        config.fmp_api_key = "present"
        config.coingecko_api_key = "present"

        snapshot = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-10").load_snapshots()[0]

        self.assertEqual(snapshot.quote_status, "available")
        self.assertIsNone(snapshot.quote_timestamp)
        self.assertIsNone(snapshot.quote_age_seconds)
        self.assertFalse(snapshot.quote_is_intraday)

    def test_cache_key_redacts_provider_key_and_reuses_equivalent_request(self):
        first_url = "https://example.test/prices?symbol=AMD&apikey=first-secret"
        second_url = "https://example.test/prices?apikey=rotated-secret&symbol=AMD"

        first_key = _cache_key(first_url, None)
        second_key = _cache_key(second_url, None)

        self.assertEqual(first_key, second_key)
        self.assertNotIn("first-secret", first_key)
        self.assertNotIn("rotated-secret", second_key)

        with tempfile.TemporaryDirectory() as tmp:
            config = AdvisorConfig.default()
            config.fmp_api_key = "present"
            loader = LiveDataLoader(config, fetch_json=lambda *args, **kwargs: {"ok": True}, db_path=Path(tmp) / "advisor.db")

            loader._fetch("fmp", "prices", first_url)
            stored = loader.cache.get_json("prices", second_key, max_age_seconds=60)

        self.assertEqual(stored, {"ok": True})

    def test_live_loader_cache_hit_keeps_source_candle_date_separate_from_fetch_time(self):
        def fake_fetch(url, *, payload=None, headers=None):
            if "profile" in url:
                return [{"mktCap": 1_000_000_000_000, "volAvg": 10_000_000}]
            return []

        with tempfile.TemporaryDirectory() as tmp:
            config = AdvisorConfig.default()
            config.stock_watchlist = ["AMD"]
            config.crypto_watchlist = []
            config.fmp_api_key = "present"
            config.coingecko_api_key = "present"
            loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-10", db_path=Path(tmp) / "advisor.db")
            price_url = loader.fmp.historical_prices_url("AMD")
            loader.cache.set_json(
                "prices",
                _cache_key(price_url, None),
                {
                    "historical": [
                        {"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}
                    ]
                },
                fetched_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            )

            snapshot = loader.load_stock("AMD")

        self.assertEqual(snapshot.data_timestamp, "2026-07-09")
        self.assertIsNotNone(snapshot.cache_age_seconds)
        self.assertNotEqual(snapshot.data_timestamp, snapshot.data_fetch_metadata.fetched_at)
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
        self.assertIsNone(btc.coinbase_premium)
        self.assertAlmostEqual(btc.open_interest_change, 0.10)
        self.assertAlmostEqual(hype.funding_rate, 0.0016)
        self.assertIsNone(btc.liquidation_imbalance)
        self.assertTrue(any("financialmodelingprep.com" in call[0] for call in calls))
        self.assertTrue(any("/stable/key-metrics" in call[0] for call in calls))
        self.assertTrue(any("income-statement-growth" in call[0] for call in calls))
        self.assertTrue(any("fapi.binance.com" in call[0] for call in calls))
        self.assertTrue(any("openInterestHist" in call[0] for call in calls))
        self.assertFalse(any("allForceOrders" in call[0] for call in calls))
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

    def test_live_loader_attaches_recent_sec_filings_to_stocks(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append((url, headers or {}))
            if "data.sec.gov/submissions" in url:
                return {
                    "filings": {
                        "recent": {
                            "form": ["8-K", "10-Q", "4"],
                            "filingDate": ["2026-06-28", "2026-05-20", "2026-06-29"],
                            "accessionNumber": ["0000002488-26-000001", "0000002488-26-000002", "0000002488-26-000003"],
                            "primaryDocument": ["amd-20260628.htm", "amd-20260520.htm", "xslF345X05/doc4.xml"],
                        }
                    }
                }
            if "historical-price-eod/full" in url:
                return {
                    "historical": [
                        {"date": "2026-01-02", "open": 101, "high": 104, "low": 100, "close": 103, "volume": 2000},
                        {"date": "2026-01-01", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1500},
                    ]
                }
            if "stable/profile" in url:
                return [{"mktCap": 300_000_000_000, "volAvg": 20_000_000}]
            if "ratios-ttm" in url:
                return [{"priceEarningsRatioTTM": 32, "pegRatioTTM": 2.1, "grossProfitMarginTTM": 0.68}]
            if "key-metrics-ttm" in url:
                return [{"freeCashFlowPerShareTTM": 4.5}]
            if "/stable/key-metrics" in url:
                return [{"peRatio": 30}]
            if "income-statement-growth" in url:
                return [{"growthRevenue": 0.16, "growthEPS": 0.12}]
            if "earnings-calendar" in url:
                return [{"date": "2026-08-10"}]
            return {}

        config = AdvisorConfig.default()
        config.stock_watchlist = ["AMD"]
        config.crypto_watchlist = []
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-01")

        snapshot = loader.load_snapshots()[0]

        self.assertEqual([event["news_event_type"] for event in snapshot.news_events], ["sec_8k", "sec_10q"])
        self.assertTrue(any("data.sec.gov/submissions/CIK0000002488.json" in call[0] for call in calls))
        self.assertTrue(any(call[1].get("User-Agent", "").startswith("financial-advisor-v1") for call in calls))

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
            if "historical-price-eod/light" in url and "symbol=MSFT" in url:
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
        self.assertEqual(len([url for url in calls if "historical-price-eod/full" in url]), 1)
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

    def test_live_loader_collects_market_and_sector_benchmark_statuses(self):
        def fake_fetch(url, *, payload=None, headers=None):
            if "symbol=SMH" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter")
            return [
                {"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
                {"date": "2026-07-08", "open": 98, "high": 101, "low": 97, "close": 100, "volume": 1000},
            ]

        config = AdvisorConfig.default()
        config.fmp_api_key = "present"
        config.coingecko_api_key = "present"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-10")

        benchmarks = loader.load_benchmarks()

        self.assertEqual(set(benchmarks), {"SPY", "QQQ", "SMH", "IGV", "XLV"})
        self.assertEqual(benchmarks["SMH"], [])
        self.assertEqual(loader.benchmark_status["SPY"]["status"], "available")
        self.assertEqual(loader.benchmark_status["SMH"]["status"], "unavailable")
        self.assertEqual(loader.benchmark_status["SPY"]["source_timestamp"], "2026-07-09")

    def test_load_snapshots_then_benchmarks_attaches_sector_provenance_and_relative_strength(self):
        def fake_fetch(url, *, payload=None, headers=None):
            if "/stable/quote" in url:
                return []
            if "symbol=AMD" in url:
                return [
                    {"date": "2026-07-09", "open": 103, "high": 106, "low": 102, "close": 105, "volume": 1000},
                    {"date": "2026-07-08", "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1000},
                ]
            if "symbol=SMH" in url:
                return [
                    {"date": "2026-07-09", "open": 201, "high": 203, "low": 200, "close": 202, "volume": 1000},
                    {"date": "2026-07-08", "open": 199, "high": 201, "low": 198, "close": 200, "volume": 1000},
                ]
            return [
                {"date": "2026-07-09", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
                {"date": "2026-07-08", "open": 98, "high": 101, "low": 97, "close": 100, "volume": 1000},
            ]

        config = AdvisorConfig.default()
        config.stock_watchlist = ["AMD"]
        config.crypto_watchlist = []
        config.fmp_api_key = "present"
        config.coingecko_api_key = "present"
        loader = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-10")

        snapshots = loader.load_snapshots()
        loader.load_benchmarks()
        provenance = snapshots[0].benchmark_provenance

        self.assertEqual(provenance["sector"]["symbol"], "SMH")
        self.assertEqual(provenance["sector"]["source_timestamp"], "2026-07-09")
        self.assertAlmostEqual(provenance["sector"]["daily_change_pct"], 1.0)
        self.assertAlmostEqual(provenance["relative_strength_pct"], 4.0)

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

    def test_live_loader_does_not_request_invalid_binance_liquidation_endpoint(self):
        calls = []

        def fake_fetch(url, *, payload=None, headers=None):
            calls.append(url)
            if "coins/markets" in url:
                return [{}]
            if "/klines" in url:
                return [[1767225600000, "100", "105", "99", "104", "1000"]]
            if "fundingRate" in url:
                return [{"fundingRate": "0.0025"}]
            if "openInterestHist" in url:
                return [{"sumOpenInterest": "10000"}, {"sumOpenInterest": "12500"}]
            if "openInterest?" in url:
                return {"openInterest": "12500"}
            if "takerlongshortRatio" in url:
                return [{"buyVol": "150", "sellVol": "100"}]
            if "api.coinbase.com" in url:
                return {"price": "104"}
            return []

        config = AdvisorConfig.default()
        config.stock_watchlist = []
        config.crypto_watchlist = ["BTC"]
        snapshot = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-10").load_snapshots()[0]

        self.assertFalse(any("allForceOrders" in url for url in calls))
        self.assertIsNone(snapshot.liquidation_imbalance)
        self.assertEqual(snapshot.crypto_metric_provenance["liquidations"]["status"], "not_implemented")

    def test_live_loader_preserves_hype_partial_flow_per_metric(self):
        def fake_fetch(url, *, payload=None, headers=None):
            if payload and payload.get("type") == "candleSnapshot":
                return [{"t": 1767225600000, "o": "35", "h": "36", "l": "34", "c": "35.5", "v": "100"}]
            if payload and payload.get("type") == "metaAndAssetCtxs":
                return [
                    {"universe": [{"name": "HYPE"}]},
                    [{"funding": "0.0002", "openInterest": "250"}],
                ]
            if "coins/markets" in url:
                return [{}]
            return {}

        config = AdvisorConfig.default()
        config.stock_watchlist = []
        config.crypto_watchlist = ["HYPE"]
        snapshot = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-07-10").load_snapshots()[0]

        metrics = snapshot.crypto_metric_provenance
        self.assertEqual(metrics["funding"]["status"], "available")
        self.assertEqual(metrics["funding"]["provider"], "hyperliquid")
        self.assertEqual(metrics["current_open_interest"]["status"], "available")
        self.assertEqual(metrics["candles"]["endpoint"], "candleSnapshot")
        self.assertEqual(metrics["cvd"]["status"], "not_implemented")
        self.assertEqual(metrics["open_interest_change"]["status"], "not_implemented")
        self.assertEqual(metrics["liquidations"]["status"], "not_implemented")

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
            if payload and payload.get("type") == "metaAndAssetCtxs":
                return [
                    {"universe": [{"name": "BTC"}]},
                    [{"funding": "0.0001", "openInterest": "250"}],
                ]
            if "api.coinbase.com" in url:
                return {"price": "108.07"}
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
        self.assertIn("hyperliquid_flow_fallback", snapshots[0].missing_data)
        self.assertIn("coingecko_price_history_fallback", snapshots[0].missing_data)
        self.assertNotIn("price_history_unavailable", snapshots[0].missing_data)
        self.assertAlmostEqual(snapshots[0].funding_rate, 0.0008)
        self.assertIsNone(snapshots[0].coinbase_premium)
        metrics = snapshots[0].crypto_metric_provenance
        self.assertEqual(metrics["candles"]["provider"], "coingecko")
        self.assertEqual(metrics["candles"]["granularity"], "daily")
        self.assertEqual(snapshots[0].data_fetch_metadata.market_data_kind, "eod_candle")
        self.assertEqual(metrics["funding"]["provider"], "hyperliquid")
        self.assertEqual(metrics["cvd"]["status"], "not_implemented")
        self.assertTrue(any("market_chart" in url for url in calls))
        self.assertTrue(any("api.hyperliquid.xyz" in url for url in calls))
        self.assertTrue(any("api.coinbase.com" in url for url in calls))

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

        self.assertEqual(len(calls), 9)
        self.assertEqual(sum("/stable/quote" in call[0] for call in calls), 1)
        self.assertEqual(sum("data.sec.gov/submissions" in call[0] for call in calls), 1)

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
