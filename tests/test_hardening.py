import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from advisor.backtest import summarize_backtest_setups
from advisor.cache import SQLiteCache
from advisor.config import AdvisorConfig
from advisor.data_pipeline import stock_snapshot_from_payloads
from advisor.live_loader import LiveDataLoader
from advisor.models import AssetDecision, AssetSnapshot, BacktestStats, Candle, EventInfo, Fundamentals, RiskPlan, ScoredAsset
from advisor.report import render_markdown_report
from advisor.risk import calculate_trade_plan
from advisor.scoring import classify_asset, score_asset


def candles(count=220, *, start=100.0, step=1.0):
    return [
        Candle(
            f"2026-01-{(index % 28) + 1:02d}",
            start + index * step - 0.2,
            start + index * step + 1.0,
            start + index * step - 1.0,
            start + index * step,
            1_000_000,
        )
        for index in range(count)
    ]


def strong_stock_snapshot(symbol="GOOD", *, event=None, fundamentals=None, news_events=None):
    return AssetSnapshot(
        symbol=symbol,
        asset_type="stock",
        theme="software",
        candles=candles(),
        fundamentals=fundamentals
        or Fundamentals(
            pe=28,
            peg=1.4,
            historical_pe=32,
            revenue_growth=0.22,
            eps_growth=0.18,
            margin_trend=0.06,
            free_cash_flow_positive=True,
            market_cap=500_000_000_000,
            average_volume=10_000_000,
        ),
        event=event if event is not None else EventInfo(days_to_earnings=45, guidance_recent=False, post_earnings_gap_percent=0),
        news_events=news_events or [],
    )


def strong_scored(symbol="GOOD", *, alerts=None, limitations=None, investment=90, swing=88):
    return ScoredAsset(
        snapshot=strong_stock_snapshot(symbol),
        investment_quality_score=investment,
        swing_trade_score=swing,
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
        ),
        alerts=alerts or [],
        limitations=limitations or [],
        thesis="Teste.",
        metrics_summary=["RSI: 55.00"],
        ideal_entry=100,
        alternative_entry=97,
        hold_suggestion="1-8 semanas",
    )


class HardeningTests(unittest.TestCase):
    def test_earnings_payload_without_next_date_is_unknown_not_none(self):
        snapshot = stock_snapshot_from_payloads(
            symbol="MSFT",
            theme="software",
            historical_payload=[
                {"date": "2026-05-29", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
                {"date": "2026-06-02", "open": 102, "high": 103, "low": 101, "close": 102, "volume": 1000},
            ],
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[{"date": "2026-05-30"}],
            today="2026-06-15",
        )
        scored = score_asset(snapshot, stock_regime_label="neutral", crypto_regime_label="neutral")
        decision = classify_asset(scored, BacktestStats(sample_size=90, win_rate_2r=0.62, win_rate_3r=0.31, expected_value_r=0.5))
        report = render_markdown_report([decision], stock_regime="neutral", crypto_regime="neutral")

        self.assertIn("earnings_data_missing", snapshot.missing_data)
        self.assertIn("earnings_data_missing", decision.reason_codes)
        self.assertIn("Event risk: unknown", report)
        self.assertNotIn("Event risk: nenhum", report)

    def test_fundamentals_unavailable_uses_honest_technical_thesis(self):
        scored = strong_scored(
            "CRDO",
            investment=38,
            swing=78,
            limitations=["fundamentals_unavailable", "missing_revenue_growth"],
        )

        decision = classify_asset(scored, BacktestStats(sample_size=90, win_rate_2r=0.55, win_rate_3r=0.30, expected_value_r=0.35))

        self.assertEqual(
            decision.thesis,
            "Setup tecnico detectado, mas dados fundamentais insuficientes impedem validacao.",
        )

    def test_earnings_missing_uses_honest_event_thesis(self):
        scored = strong_scored(
            "TSM",
            investment=92,
            swing=82,
            limitations=["earnings_data_missing"],
        )

        decision = classify_asset(scored, BacktestStats(sample_size=120, win_rate_2r=0.58, win_rate_3r=0.34, expected_value_r=0.52))

        self.assertEqual(
            decision.thesis,
            "Setup tecnico detectado, mas earnings/eventos nao verificados limitam validacao.",
        )

    def test_technical_unvalidated_section_is_separate_from_watchlist(self):
        watch = classify_asset(
            strong_scored("AMD", investment=75, swing=72, alerts=["market_not_risk_on"]),
            BacktestStats(sample_size=120, win_rate_2r=0.55, win_rate_3r=0.35, expected_value_r=0.45),
        )
        unvalidated = classify_asset(
            strong_scored("HYPE", investment=72, swing=82, limitations=["fundamentals_unavailable"]),
            BacktestStats(sample_size=90, win_rate_2r=0.58, win_rate_3r=0.31, expected_value_r=0.25),
        )

        report = render_markdown_report([unvalidated, watch], stock_regime="neutral", crypto_regime="neutral")

        self.assertIn("## Setup tecnico detectado, mas nao validado", report)
        self.assertIn("`HYPE`", report[report.index("## Setup tecnico detectado, mas nao validado") : report.index("## Wait")])
        self.assertNotIn("`HYPE`", report[report.index("## Watchlist apenas") : report.index("## Setup tecnico detectado, mas nao validado")])
        self.assertEqual(unvalidated.decision, "technical_unvalidated")

    def test_bucket_order_keeps_limited_data_below_clean_watchlist(self):
        wait = _asset_decision_for_report("WAIT", "wait", swing=60, investment=60)
        blocked = _asset_decision_for_report("BLOCK", "blocked", swing=0, investment=0)
        avoid = _asset_decision_for_report("AVOID", "avoid", swing=20, investment=30)
        watch = _asset_decision_for_report("AMD", "watch_buy", swing=70, investment=75)
        unvalidated = _asset_decision_for_report(
            "ASML",
            "technical_unvalidated",
            swing=95,
            investment=80,
            data_quality="limited",
            missing_data_severity="high",
        )

        report = render_markdown_report([blocked, unvalidated, avoid, wait, watch], stock_regime="neutral", crypto_regime="neutral")

        self.assertLess(report.index("## Watchlist apenas"), report.index("## Setup tecnico detectado, mas nao validado"))
        self.assertLess(report.index("## Setup tecnico detectado, mas nao validado"), report.index("## Wait"))
        self.assertLess(report.index("## Wait"), report.index("## Avoid/Rejected"))
        self.assertLess(report.index("## Avoid/Rejected"), report.index("## Blocked"))
        self.assertLess(report.index("2. `ASML`"), report.index("3. `WAIT`"))

    def test_report_includes_freshness_market_session_and_stale_gate(self):
        scored = strong_scored("AMD", investment=80, swing=86, limitations=["stale_price_data"])

        decision = classify_asset(scored, BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.38, expected_value_r=0.60))
        report = render_markdown_report([decision], stock_regime="risk_on", crypto_regime="neutral")

        self.assertEqual(decision.decision, "wait")
        self.assertIn("market_session", report)
        self.assertIn("last_price_timestamp", report)
        self.assertIn("is_stale: `yes`", report)
        self.assertIn("stale_reason", report)

    def test_adr_and_mixed_provider_add_mismatch_alerts(self):
        snapshot = strong_stock_snapshot("TSM")
        snapshot = AssetSnapshot(
            **{**snapshot.__dict__, "data_source": "yahoo", "missing_data": ["yahoo_price_fallback"]}
        )

        scored = score_asset(snapshot, stock_regime_label="neutral", crypto_regime_label="neutral")
        decision = classify_asset(scored, BacktestStats(sample_size=120, win_rate_2r=0.58, win_rate_3r=0.32, expected_value_r=0.45))

        self.assertIn("adr_or_foreign_listing", decision.alerts)
        self.assertIn("provider_market_cap_mismatch_possible", decision.alerts)
        self.assertIn("mixed_provider_data", decision.limitations)

    def test_signal_journal_persists_decisions_for_tracking(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "advisor.db")
            decision = _asset_decision_for_report("AMD", "watch_buy", swing=72, investment=76)

            cache.save_signal_journal([decision], report_file="reports/advisor-report.md")
            rows = cache.load_signal_journal()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["asset"], "AMD")
            self.assertEqual(rows[0]["bucket"], "watch_buy")
            self.assertEqual(rows[0]["status"], "open_for_tracking")

    def test_signal_update_results_calculates_tracking_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "advisor.db")
            decision = _asset_decision_for_report("AMD", "watch_buy", swing=72, investment=76)
            cache.save_signal_journal([decision], report_file="reports/advisor-report.md")
            rows = [
                Candle("2026-01-01", 100, 101, 99, 100, 1000),
                Candle("2026-01-02", 101, 106, 100, 105, 1000),
                Candle("2026-01-03", 105, 111, 104, 110, 1000),
            ]

            updated = cache.update_signal_results({"AMD": rows})

            self.assertEqual(updated, 1)
            saved = cache.load_signal_journal()[0]
            self.assertEqual(saved["result_final"], "hit_2r")
            self.assertEqual(saved["days_to_2r"], 2)

    def test_report_shows_data_and_decision_confidence_scores(self):
        decision = _asset_decision_for_report(
            "AMD",
            "watch_buy",
            swing=72,
            investment=76,
            data_quality_score=92,
            decision_confidence_score=68,
        )

        report = render_markdown_report([decision], stock_regime="neutral", crypto_regime="neutral")

        self.assertIn("data_quality_score: 92", report)
        self.assertIn("decision_confidence_score: 68", report)

    def test_short_watchlist_is_observational_only(self):
        decision = _asset_decision_for_report("WEAK", "avoid", swing=20, investment=30, short_setup_score=78)

        report = render_markdown_report([decision], stock_regime="neutral", crypto_regime="neutral")

        self.assertIn("## Short watchlist apenas", report)
        self.assertIn("short_status: `watch_only`", report)
        self.assertNotIn("WEAK` - `tradeable`", report)

    def test_fmp_price_subscription_block_falls_back_to_light_history(self):
        config = AdvisorConfig.default(env_file=None)
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"
        price_rows = [
            {"date": f"2026-01-{(index % 28) + 1:02d}", "open": 100, "high": 102, "low": 99, "close": 101 + index, "volume": 1000}
            for index in range(220)
        ]

        def fake_fetch(url, *, payload=None, headers=None):
            if "historical-price-eod/full" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter: Special Endpoint")
            if "historical-price-eod/light" in url:
                return price_rows
            if "profile" in url:
                return [{"marketCap": 1_000_000_000_000, "averageVolume": 10_000_000}]
            if "ratios-ttm" in url:
                return [{"priceToEarningsRatioTTM": 30, "priceToEarningsGrowthRatioTTM": 1.5}]
            if "key-metrics" in url:
                return [{"earningsYield": 0.03, "freeCashFlowToEquityTTM": 1_000_000_000}]
            if "income-statement-growth" in url:
                return [{"growthRevenue": 0.2, "growthEPS": 0.15}]
            if "earnings-calendar" in url:
                return [{"date": "2026-12-01"}]
            return []

        snapshot = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-06-01").load_stock("ASML")

        self.assertEqual(len(snapshot.candles), 220)
        self.assertNotIn("fmp_price_unavailable", snapshot.missing_data)
        self.assertIn("fmp_price_light_fallback", snapshot.missing_data)

    def test_fmp_price_subscription_block_falls_back_to_stooq_csv_history(self):
        config = AdvisorConfig.default(env_file=None)
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"

        def fake_fetch(url, *, payload=None, headers=None):
            if "historical-price-eod/full" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter: Special Endpoint")
            if "historical-price-eod/light" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter: Special Endpoint")
            if "profile" in url:
                return [{"marketCap": 1_000_000_000_000, "averageVolume": 10_000_000}]
            if "ratios-ttm" in url:
                return [{"priceToEarningsRatioTTM": 30, "priceToEarningsGrowthRatioTTM": 1.5}]
            if "key-metrics" in url:
                return [{"earningsYield": 0.03, "freeCashFlowToEquityTTM": 1_000_000_000}]
            if "income-statement-growth" in url:
                return [{"growthRevenue": 0.2, "growthEPS": 0.15}]
            if "earnings-calendar" in url:
                return [{"date": "2026-12-01"}]
            return []

        def fake_fetch_text(url, *, headers=None):
            self.assertIn("stooq.com", url)
            rows = ["Date,Open,High,Low,Close,Volume"]
            rows.extend(f"2026-01-{(index % 28) + 1:02d},100,102,99,{101 + index},1000" for index in range(220))
            return "\n".join(rows)

        snapshot = LiveDataLoader(
            config,
            fetch_json=fake_fetch,
            fetch_text=fake_fetch_text,
            today="2026-06-01",
        ).load_stock("HIMS")

        self.assertEqual(len(snapshot.candles), 220)
        self.assertNotIn("fmp_price_unavailable", snapshot.missing_data)
        self.assertIn("stooq_price_fallback", snapshot.missing_data)

    def test_fmp_price_subscription_block_falls_back_to_yahoo_chart_history_before_stooq(self):
        config = AdvisorConfig.default(env_file=None)
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"

        def fake_fetch(url, *, payload=None, headers=None):
            if "historical-price-eod/full" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter: Special Endpoint")
            if "historical-price-eod/light" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter: Special Endpoint")
            if "query1.finance.yahoo.com" in url:
                return {
                    "chart": {
                        "result": [
                            {
                                "timestamp": [1767225600 + (index * 86400) for index in range(220)],
                                "indicators": {
                                    "quote": [
                                        {
                                            "open": [100] * 220,
                                            "high": [102] * 220,
                                            "low": [99] * 220,
                                            "close": [101 + index for index in range(220)],
                                            "volume": [1000] * 220,
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            if "profile" in url:
                return [{"marketCap": 1_000_000_000_000, "averageVolume": 10_000_000}]
            if "ratios-ttm" in url:
                return [{"priceToEarningsRatioTTM": 30, "priceToEarningsGrowthRatioTTM": 1.5}]
            if "key-metrics" in url:
                return [{"earningsYield": 0.03, "freeCashFlowToEquityTTM": 1_000_000_000}]
            if "income-statement-growth" in url:
                return [{"growthRevenue": 0.2, "growthEPS": 0.15}]
            if "earnings-calendar" in url:
                return [{"date": "2026-12-01"}]
            return []

        def fake_fetch_text(url, *, headers=None):
            raise AssertionError("stooq should not be called when yahoo fallback succeeds")

        snapshot = LiveDataLoader(
            config,
            fetch_json=fake_fetch,
            fetch_text=fake_fetch_text,
            today="2026-06-01",
        ).load_stock("HIMS")

        self.assertEqual(len(snapshot.candles), 220)
        self.assertNotIn("fmp_price_unavailable", snapshot.missing_data)
        self.assertIn("yahoo_price_fallback", snapshot.missing_data)

    def test_yahoo_price_fallback_degrades_blocked_fmp_fundamentals_without_failing_scan(self):
        config = AdvisorConfig.default(env_file=None)
        config.fmp_api_key = "demo"
        config.coingecko_api_key = "demo"

        def fake_fetch(url, *, payload=None, headers=None):
            if "historical-price-eod/full" in url or "historical-price-eod/light" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter: Special Endpoint")
            if "query1.finance.yahoo.com" in url:
                return {
                    "chart": {
                        "result": [
                            {
                                "timestamp": [1767225600 + (index * 86400) for index in range(220)],
                                "indicators": {
                                    "quote": [
                                        {
                                            "open": [100] * 220,
                                            "high": [102] * 220,
                                            "low": [99] * 220,
                                            "close": [101 + index for index in range(220)],
                                            "volume": [1000] * 220,
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            if "financialmodelingprep.com/stable/" in url:
                raise RuntimeError("http_error:402:Premium Query Parameter: Special Endpoint")
            return []

        snapshot = LiveDataLoader(config, fetch_json=fake_fetch, today="2026-06-01").load_stock("HIMS")

        self.assertEqual(len(snapshot.candles), 220)
        self.assertIn("yahoo_price_fallback", snapshot.missing_data)
        self.assertIn("fundamentals_unavailable", snapshot.missing_data)
        self.assertIn("earnings_data_missing", snapshot.missing_data)

    def test_crypto_position_sizing_accepts_fractional_quantity(self):
        plan = calculate_trade_plan(
            entry=65_000,
            stop=60_000,
            account_capital=50_000,
            risk_fraction=0.005,
            atr_value=2_000,
            average_volume=10_000_000_000,
            allow_fractional=True,
        )

        self.assertAlmostEqual(plan.max_position_units, 0.05)
        self.assertEqual(plan.position_size_display, "0.0500")
        self.assertNotIn("position_too_small_for_risk", plan.alerts)

    def test_backtest_stats_include_expected_value_components(self):
        winner = {
            "candles": [
                Candle("2026-01-01", 100, 101, 99, 100, 1000),
                Candle("2026-01-02", 100, 111, 99, 108, 1000),
            ],
            "entry": 100,
            "stop": 95,
            "max_days": 30,
        }
        loser = {
            "candles": [
                Candle("2026-01-01", 100, 101, 99, 100, 1000),
                Candle("2026-01-02", 100, 101, 94, 95, 1000),
            ],
            "entry": 100,
            "stop": 95,
            "max_days": 30,
        }

        stats = summarize_backtest_setups([winner, loser])

        self.assertEqual(stats.sample_size, 2)
        self.assertEqual(stats.avg_win_r, 2.0)
        self.assertEqual(stats.avg_loss_r, -1.0)
        self.assertEqual(stats.expected_value_r, 0.5)
        self.assertEqual(stats.setup_quality, "low")

    def test_weak_win_rate_and_negative_ev_limits_decision_to_wait(self):
        decision = classify_asset(
            strong_scored(),
            BacktestStats(
                sample_size=120,
                win_rate_2r=0.44,
                win_rate_3r=0.20,
                expected_value_r=-0.05,
                avg_win_r=1.1,
                avg_loss_r=-1.0,
            ),
        )

        self.assertEqual(decision.decision, "wait")
        self.assertIn("weak_or_negative_expected_value", decision.alerts)

    def test_intc_like_case_is_speculative_not_normal_watch_buy(self):
        decision = classify_asset(
            strong_scored(
                "INTC",
                alerts=["negative_or_invalid_pe", "negative_or_invalid_peg", "recent_gap_risk"],
                investment=36,
                swing=89,
            ),
            BacktestStats(
                sample_size=319,
                win_rate_2r=0.32,
                win_rate_3r=0.21,
                expected_value_r=-0.04,
                avg_win_r=1.7,
                avg_loss_r=-1.0,
            ),
        )

        self.assertIn(decision.decision, {"technical_unvalidated", "avoid", "wait"})
        self.assertNotEqual(decision.decision, "watch_buy")

    def test_extreme_valuation_caps_investment_quality(self):
        snapshot = strong_stock_snapshot(
            fundamentals=Fundamentals(
                pe=140,
                peg=6.0,
                historical_pe=35,
                revenue_growth=0.20,
                eps_growth=0.15,
                margin_trend=0.05,
                free_cash_flow_positive=True,
                market_cap=1_000_000_000_000,
                average_volume=10_000_000,
            )
        )

        scored = score_asset(snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral")

        self.assertLessEqual(scored.investment_quality_score, 70)
        self.assertIn("valuation_extreme", scored.alerts)

    def test_missing_earnings_data_limits_confidence(self):
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

        self.assertIn("earnings_data_missing", snapshot.missing_data)
        self.assertIsNotNone(snapshot.event)
        self.assertIsNone(snapshot.event.next_earnings_date)

    def test_imminent_earnings_limits_decision_to_wait(self):
        snapshot = strong_stock_snapshot(event=EventInfo(days_to_earnings=3, guidance_recent=False, post_earnings_gap_percent=0))
        scored = score_asset(snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral")
        decision = classify_asset(
            scored,
            BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.44, expected_value_r=0.8),
        )

        self.assertEqual(decision.decision, "wait")
        self.assertIn("earnings_imminent", decision.alerts)

    def test_tradeable_replaces_strong_buy_candidate_for_clean_positive_ev_setup(self):
        decision = classify_asset(
            strong_scored(),
            BacktestStats(
                sample_size=120,
                win_rate_2r=0.58,
                win_rate_3r=0.33,
                expected_value_r=0.55,
                avg_win_r=2.0,
                avg_loss_r=-1.0,
            ),
        )

        self.assertEqual(decision.decision, "tradeable")

    def test_report_header_includes_hardening_summary_and_no_trade_day(self):
        decision = AssetDecision(
            symbol="MSFT",
            asset_type="stock",
            decision="wait",
            investment_quality_score=85,
            swing_trade_score=50,
            risk_plan=strong_scored().risk_plan,
            alerts=[],
            limitations=["earnings_data_missing"],
            thesis="Aguardando gatilho.",
            metrics_summary=["RSI: 50.00"],
            ideal_entry=100,
            alternative_entry=97,
            hold_suggestion="1-8 semanas",
            backtest_stats=BacktestStats(sample_size=20, win_rate_2r=None, win_rate_3r=None),
            sample_quality="low",
        )

        report = render_markdown_report([decision], stock_regime="neutral", crypto_regime="risk_off", data_mode="fixture")

        self.assertIn("Generated at:", report)
        self.assertIn("Data freshness:", report)
        self.assertIn("Ativos analisados: 1", report)
        self.assertIn("Blocked por dados: 0", report)
        self.assertIn("Decisao geral: `no_trade_day`", report)
        self.assertIn("decision_label: `wait`", report)
        self.assertIn("reason_codes:", report)
        self.assertIn("expected_value_r:", report)
        self.assertIn("data_quality:", report)

    def test_report_uses_unknown_event_risk_when_earnings_was_not_collected(self):
        decision = AssetDecision(
            symbol="MSFT",
            asset_type="stock",
            decision="blocked",
            investment_quality_score=0,
            swing_trade_score=0,
            risk_plan=strong_scored().risk_plan,
            alerts=[],
            limitations=["earnings_data_missing"],
            thesis="Sem dados criticos.",
            metrics_summary=["Post earnings gap: n/a"],
            ideal_entry=0,
            alternative_entry=None,
            hold_suggestion="n/a",
            backtest_stats=BacktestStats(sample_size=0, win_rate_2r=None, win_rate_3r=None),
            sample_quality="low",
        )

        report = render_markdown_report([decision], stock_regime="neutral", crypto_regime="risk_off")

        self.assertIn("Event risk: unknown", report)

    def test_blocked_stock_without_earnings_verification_uses_unknown_event_risk(self):
        decision = AssetDecision(
            symbol="BLOCK",
            asset_type="stock",
            decision="blocked",
            investment_quality_score=0,
            swing_trade_score=0,
            risk_plan=strong_scored().risk_plan,
            alerts=["insufficient_price_history"],
            limitations=["insufficient_price_history"],
            thesis="Sem dados criticos.",
            metrics_summary=["price history: n/a"],
            ideal_entry=0,
            alternative_entry=None,
            hold_suggestion="n/a",
            backtest_stats=BacktestStats(sample_size=0, win_rate_2r=None, win_rate_3r=None),
            sample_quality="low",
        )

        report = render_markdown_report([decision], stock_regime="neutral", crypto_regime="neutral")

        self.assertIn("Event risk: unknown", report)
        self.assertNotIn("Event risk: nenhum", report)

    def test_report_uses_not_collected_for_missing_news_module_data(self):
        decision = classify_asset(
            strong_scored(),
            BacktestStats(sample_size=120, win_rate_2r=0.58, win_rate_3r=0.33, expected_value_r=0.55),
        )

        report = render_markdown_report([decision], stock_regime="risk_on", crypto_regime="neutral")

        self.assertIn("News/catalyst summary: not_collected", report)

    def test_report_has_tradeable_today_and_watchlist_only_sections(self):
        tsm = strong_scored("TSM", alerts=["market_not_risk_on"])
        amd = strong_scored("AMD", investment=70, alerts=["market_not_risk_on"])
        blocked = strong_scored("ASML", limitations=["insufficient_price_history"])
        decisions = [
            classify_asset(tsm, BacktestStats(sample_size=120, win_rate_2r=0.43, win_rate_3r=0.31, expected_value_r=0.63)),
            classify_asset(amd, BacktestStats(sample_size=120, win_rate_2r=0.42, win_rate_3r=0.35, expected_value_r=0.61)),
            classify_asset(blocked, BacktestStats(sample_size=0, win_rate_2r=None, win_rate_3r=None)),
        ]

        report = render_markdown_report(decisions, stock_regime="neutral", crypto_regime="risk_off")

        self.assertIn("## Tradeable hoje", report)
        self.assertIn("Nenhum ativo tradeable hoje.", report)
        self.assertIn("## Watchlist apenas", report)
        self.assertIn("`TSM`", report)
        self.assertIn("`AMD`", report)
        self.assertNotIn("`ASML` - blocked", report[report.index("## Watchlist apenas") : report.index("## Ranking")])

    def test_report_includes_asset_data_timestamp_source_and_cache_age(self):
        decision = AssetDecision(
            symbol="AMD",
            asset_type="stock",
            decision="watch_buy",
            investment_quality_score=70,
            swing_trade_score=89,
            risk_plan=strong_scored().risk_plan,
            alerts=[],
            limitations=[],
            thesis="Teste.",
            metrics_summary=["RSI: 55.00"],
            ideal_entry=100,
            alternative_entry=97,
            hold_suggestion="1-8 semanas",
            backtest_stats=BacktestStats(sample_size=120, win_rate_2r=0.42, win_rate_3r=0.35, expected_value_r=0.61),
            sample_quality="high",
            data_source="fmp",
            data_timestamp="2026-06-15T03:00:00+00:00",
            cache_age_seconds=3600,
        )

        report = render_markdown_report([decision], stock_regime="neutral", crypto_regime="risk_off")

        self.assertIn("Data source: fmp", report)
        self.assertIn("Data timestamp: 2026-06-15T03:00:00+00:00", report)
        self.assertIn("Cache age: 3600s", report)

    def test_non_live_data_mode_forces_no_trade_day_and_empty_actionable_watchlist(self):
        decision = _asset_decision_for_report("AMD", "watch_buy", swing=72, investment=76)

        report = render_markdown_report([decision], stock_regime="neutral", crypto_regime="neutral", data_mode="fixture")

        watchlist_block = report[report.index("## Watchlist apenas") : report.index("## Setup tecnico detectado")]
        self.assertIn("Decisao geral: `no_trade_day`", report)
        self.assertIn("Nao usar para decisao real", report)
        self.assertIn("Nenhum ativo em watchlist acionavel neste data mode.", watchlist_block)
        self.assertNotIn("`AMD`", watchlist_block)

    def test_report_includes_event_check_status(self):
        stock = _asset_decision_for_report("AMD", "watch_buy", swing=72, investment=76, event_check_status="verified")
        crypto = _asset_decision_for_report("BTC", "wait", swing=50, investment=70, event_check_status="not_applicable")
        crypto = AssetDecision(**{**crypto.__dict__, "asset_type": "crypto"})

        report = render_markdown_report([stock, crypto], stock_regime="neutral", crypto_regime="neutral", data_mode="live")

        self.assertIn("event_check_status: `verified`", report)
        self.assertIn("event_check_status: `not_applicable`", report)

    def test_decision_confidence_reduces_for_uncollected_context_and_missing_ev_components(self):
        scored = strong_scored("AMD", investment=90, swing=90)

        decision = classify_asset(
            scored,
            BacktestStats(sample_size=120, win_rate_2r=0.62, win_rate_3r=0.35, expected_value_r=0.55),
        )

        self.assertLessEqual(decision.decision_confidence_score, 60)
        self.assertIn("news_not_collected_confidence_limited", decision.reason_codes)
        self.assertIn("macro_not_collected_confidence_limited", decision.reason_codes)
        self.assertIn("sector_relative_strength_not_collected", decision.reason_codes)
        self.assertIn("ev_components_missing", decision.reason_codes)

    def test_expected_value_is_marked_limited_without_avg_win_loss_components(self):
        decision = _asset_decision_for_report("AMD", "watch_buy", swing=72, investment=76)
        decision = AssetDecision(
            **{
                **decision.__dict__,
                "backtest_stats": BacktestStats(
                    sample_size=120,
                    win_rate_2r=0.58,
                    win_rate_3r=0.34,
                    expected_value_r=0.52,
                    avg_win_r=None,
                    avg_loss_r=None,
                ),
            }
        )

        report = render_markdown_report([decision], stock_regime="neutral", crypto_regime="neutral", data_mode="live")

        self.assertIn("expected_value_r: 0.52 (limited/model_estimate)", report)

    def test_negative_ev_with_high_severity_data_downgrades_to_technical_unvalidated(self):
        scored = strong_scored(
            "HYPE",
            investment=75,
            swing=71,
            limitations=["data_incomplete_confidence_limited", "cvd_proxy_unavailable"],
        )

        decision = classify_asset(
            scored,
            BacktestStats(sample_size=63, win_rate_2r=0.30, win_rate_3r=0.16, expected_value_r=-0.21),
        )

        self.assertEqual(decision.decision, "technical_unvalidated")
        self.assertIn("negative_ev_with_high_data_severity", decision.alerts)

    def test_high_severity_limited_data_cannot_remain_watch_buy(self):
        scored = strong_scored(
            "ZEC",
            investment=63,
            swing=73,
            limitations=["data_incomplete_confidence_limited", "coinbase_premium_unavailable"],
        )

        decision = classify_asset(
            scored,
            BacktestStats(sample_size=57, win_rate_2r=0.56, win_rate_3r=0.35, expected_value_r=1.18),
        )

        self.assertEqual(decision.decision, "technical_unvalidated")
        self.assertIn("high_severity_data_not_watchlist", decision.alerts)

    def test_post_earnings_gap_not_collected_removed_when_gap_is_calculated(self):
        rows = [
            {"date": "2026-05-30", "open": 98, "high": 101, "low": 97, "close": 100, "volume": 1000},
            {"date": "2026-06-02", "open": 110, "high": 112, "low": 108, "close": 111, "volume": 1000},
        ]

        snapshot = stock_snapshot_from_payloads(
            symbol="MSFT",
            theme="software",
            historical_payload=rows,
            profile_payload=[],
            ratios_payload=[],
            metrics_payload=[],
            historical_metrics_payload=[],
            growth_payload=[],
            earnings_payload=[{"date": "2026-06-01"}],
            today="2026-06-15",
        )

        self.assertAlmostEqual(snapshot.event.post_earnings_gap_percent, 0.10)
        self.assertNotIn("post_earnings_gap_not_collected", snapshot.missing_data)

    def test_news_rumor_can_limit_confidence_but_not_create_tradeable(self):
        snapshot = strong_stock_snapshot(
            "NVDA",
            news_events=[
                {
                    "news_event_type": "product_launch",
                    "source": "fixture",
                    "published_at": "2026-06-01T12:00:00Z",
                    "confirmed_status": "rumor",
                    "affected_assets": ["NVDA"],
                    "market_effect": "risk_on",
                    "already_priced": "unclear",
                    "news_confidence": "low",
                }
            ],
        )

        scored = score_asset(snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral")
        decision = classify_asset(scored, BacktestStats(sample_size=120, win_rate_2r=0.58, win_rate_3r=0.33, expected_value_r=0.55))
        report = render_markdown_report([decision], stock_regime="risk_on", crypto_regime="neutral")

        self.assertNotEqual(decision.decision, "tradeable")
        self.assertIn("news_rumor_confidence_limited", decision.alerts)
        self.assertIn("News/catalyst summary", report)

    def test_price_history_failure_is_blocked_with_probable_cause_in_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp) / "fixtures"
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"
            fixture_dir.mkdir()
            (fixture_dir / "scan.json").write_text(
                json.dumps(
                    {
                        "account_capital": 50000,
                        "assets": [
                            {
                                "symbol": "ASML",
                                "asset_type": "stock",
                                "theme": "semiconductors",
                                "candles": [],
                                "missing_data": ["fmp_price_unavailable"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            scan = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "scan",
                    "--fixture-dir",
                    str(fixture_dir),
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
            )

            report = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertEqual(scan.returncode, 0, scan.stderr)
            self.assertIn("decision_label: `blocked`", report)
            self.assertIn("probable_cause:fmp_plan_or_price_endpoint_unavailable", report)


if __name__ == "__main__":
    unittest.main()


def _asset_decision_for_report(
    symbol,
    decision,
    *,
    swing,
    investment,
    data_quality="ok",
    missing_data_severity="low",
    data_quality_score=90,
    decision_confidence_score=70,
    short_setup_score=0,
    event_check_status="not_collected",
):
    return AssetDecision(
        symbol=symbol,
        asset_type="stock",
        decision=decision,
        investment_quality_score=investment,
        swing_trade_score=swing,
        risk_plan=strong_scored().risk_plan,
        alerts=[],
        limitations=[],
        thesis="Teste.",
        metrics_summary=["RSI: 55.00"],
        ideal_entry=100,
        alternative_entry=97,
        hold_suggestion="1-8 semanas",
        backtest_stats=BacktestStats(sample_size=120, win_rate_2r=0.58, win_rate_3r=0.34, expected_value_r=0.50),
        sample_quality="high",
        data_quality=data_quality,
        missing_data_severity=missing_data_severity,
        data_quality_score=data_quality_score,
        decision_confidence_score=decision_confidence_score,
        event_check_status=event_check_status,
        short_setup_score=short_setup_score,
        short_status="watch_only" if short_setup_score else "not_evaluated",
    )
