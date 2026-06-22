import unittest
from dataclasses import replace

from advisor.backtest import summarize_backtest_setups
from advisor.models import AssetDecision, AssetSnapshot, BacktestStats, Candle, EventInfo, Fundamentals, RiskPlan, ScoredAsset
from advisor.regime import classify_crypto_regime, classify_stock_regime
from advisor.report import render_markdown_report
from advisor.scoring import classify_asset, score_asset


def uptrend_candles(symbol_prefix="", count=80):
    base = 100
    return [
        Candle(f"2026-01-{index + 1:02d}", base + index, base + index + 1, base + index - 1, base + index, 1_000_000)
        for index in range(count)
    ]


class ScoringRegimeReportTests(unittest.TestCase):
    def test_market_regime_classifies_stock_risk_on(self):
        spy = uptrend_candles(count=220)
        qqq = uptrend_candles(count=220)
        breadth = {"NVDA": True, "AMD": True, "MSFT": True, "INTC": False}

        regime = classify_stock_regime(spy, qqq, breadth, recent_gap_percent=0.02)

        self.assertEqual(regime.label, "risk_on")
        self.assertIn("SPY_above_sma50_sma200", regime.reasons)

    def test_market_regime_classifies_crypto_risk_off_when_leverage_hot(self):
        btc = list(reversed(uptrend_candles(count=220)))

        regime = classify_crypto_regime(
            btc_candles=btc,
            eth_btc_relative_strength=-0.04,
            sol_btc_relative_strength=-0.06,
            funding_rate=0.09,
            open_interest_change=0.35,
        )

        self.assertEqual(regime.label, "risk_off")
        self.assertIn("leverage_hot", regime.reasons)

    def test_market_regime_marks_realistic_eight_hour_funding_as_hot_leverage(self):
        regime = classify_crypto_regime(
            btc_candles=uptrend_candles(count=220),
            eth_btc_relative_strength=0.04,
            sol_btc_relative_strength=0.06,
            funding_rate=0.0015,
            open_interest_change=0.30,
        )

        self.assertIn("leverage_hot", regime.reasons)

    def test_scores_separate_investment_quality_from_swing_entry(self):
        snapshot = AssetSnapshot(
            symbol="MSFT",
            asset_type="stock",
            theme="software",
            candles=uptrend_candles(),
            fundamentals=Fundamentals(
                pe=32,
                peg=2.1,
                historical_pe=30,
                revenue_growth=0.16,
                eps_growth=0.12,
                margin_trend=0.04,
                free_cash_flow_positive=True,
                market_cap=3_000_000_000_000,
                average_volume=20_000_000,
            ),
            event=EventInfo(days_to_earnings=5, guidance_recent=False, post_earnings_gap_percent=0.0),
        )

        scored = score_asset(snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral")

        self.assertGreater(scored.investment_quality_score, scored.swing_trade_score)
        self.assertIn("earnings_near", scored.alerts)

    def test_score_is_deterministic_for_the_same_snapshot_and_context(self):
        snapshot = AssetSnapshot(
            symbol="MSFT",
            asset_type="stock",
            theme="software",
            candles=uptrend_candles(count=220),
            fundamentals=Fundamentals(
                pe=32,
                peg=2.1,
                historical_pe=30,
                revenue_growth=0.16,
                eps_growth=0.12,
                margin_trend=0.04,
                free_cash_flow_positive=True,
                market_cap=3_000_000_000_000,
                average_volume=20_000_000,
            ),
            event=EventInfo(days_to_earnings=30, guidance_recent=None, post_earnings_gap_percent=None),
        )

        first = score_asset(snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral")
        second = score_asset(snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral")

        self.assertEqual(first.investment_quality_score, second.investment_quality_score)
        self.assertEqual(first.swing_trade_score, second.swing_trade_score)
        self.assertEqual(first.risk_plan, second.risk_plan)
        self.assertEqual(first.alerts, second.alerts)
        self.assertEqual(first.limitations, second.limitations)

    def test_crypto_score_flags_realistic_eight_hour_funding_and_open_interest_risk(self):
        snapshot = AssetSnapshot(
            symbol="BTC",
            asset_type="crypto",
            theme="crypto",
            candles=uptrend_candles(count=220),
            fundamentals=Fundamentals(
                pe=None,
                peg=None,
                historical_pe=None,
                revenue_growth=None,
                eps_growth=None,
                margin_trend=None,
                free_cash_flow_positive=None,
                market_cap=1_500_000_000_000,
                average_volume=5_000_000_000,
            ),
            funding_rate=0.0015,
            open_interest_change=0.30,
        )

        scored = score_asset(snapshot, stock_regime_label="neutral", crypto_regime_label="risk_on")

        self.assertIn("leverage_risk_funding", scored.alerts)
        self.assertIn("leverage_risk_open_interest", scored.alerts)
        self.assertIn("Funding rate (8h normalized): 0.15%", scored.metrics_summary)

    def test_report_shows_earnings_guidance_and_post_earnings_gap(self):
        snapshot = AssetSnapshot(
            symbol="MSFT",
            asset_type="stock",
            theme="software",
            candles=uptrend_candles(),
            fundamentals=Fundamentals(
                pe=32,
                peg=2.1,
                historical_pe=30,
                revenue_growth=0.16,
                eps_growth=0.12,
                margin_trend=0.04,
                free_cash_flow_positive=True,
                market_cap=3_000_000_000_000,
                average_volume=20_000_000,
            ),
            event=EventInfo(days_to_earnings=5, guidance_recent=True, post_earnings_gap_percent=0.09),
        )
        scored = score_asset(snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral")
        classified = classify_asset(scored, BacktestStats(sample_size=90, win_rate_2r=0.64, win_rate_3r=0.41))

        report = render_markdown_report(
            [classified],
            stock_regime="risk_on",
            crypto_regime="neutral",
        )

        self.assertIn("Days to earnings: 5", report)
        self.assertIn("Guidance recent: yes", report)
        self.assertIn("Post earnings gap: 9.00%", report)
        self.assertIn("earnings_near", report)
        self.assertIn("recent_guidance", report)
        self.assertIn("post_earnings_gap", report)

    def test_relative_strength_changes_swing_score_not_investment_score(self):
        snapshot = AssetSnapshot(
            symbol="NVDA",
            asset_type="stock",
            theme="semiconductors",
            candles=uptrend_candles(),
            fundamentals=Fundamentals(
                pe=38,
                peg=1.9,
                historical_pe=40,
                revenue_growth=0.20,
                eps_growth=0.18,
                margin_trend=0.05,
                free_cash_flow_positive=True,
                market_cap=3_000_000_000_000,
                average_volume=35_000_000,
            ),
            event=EventInfo(days_to_earnings=45, guidance_recent=False, post_earnings_gap_percent=0.0),
        )

        leading = score_asset(
            snapshot,
            stock_regime_label="risk_on",
            crypto_regime_label="neutral",
            relative_strength_percent=0.08,
        )
        lagging = score_asset(
            snapshot,
            stock_regime_label="risk_on",
            crypto_regime_label="neutral",
            relative_strength_percent=-0.08,
        )

        self.assertEqual(leading.investment_quality_score, lagging.investment_quality_score)
        self.assertGreater(leading.swing_trade_score, lagging.swing_trade_score)
        self.assertIn("relative_strength_weak", lagging.alerts)
        self.assertIn("Relative strength: 8.00%", "; ".join(leading.metrics_summary))

    def test_negative_pe_and_peg_are_penalized_instead_of_rewarded(self):
        base = dict(
            symbol="LOSS",
            asset_type="stock",
            theme="software",
            candles=uptrend_candles(count=220),
            event=EventInfo(days_to_earnings=45, guidance_recent=False, post_earnings_gap_percent=0.0),
        )
        negative = score_asset(
            AssetSnapshot(
                **base,
                fundamentals=Fundamentals(
                    pe=-20,
                    peg=-1.2,
                    historical_pe=30,
                    revenue_growth=0.20,
                    eps_growth=0.15,
                    margin_trend=0.05,
                    free_cash_flow_positive=True,
                    market_cap=50_000_000_000,
                    average_volume=5_000_000,
                ),
            ),
            stock_regime_label="risk_on",
            crypto_regime_label="neutral",
        )
        positive = score_asset(
            AssetSnapshot(
                **base,
                fundamentals=Fundamentals(
                    pe=20,
                    peg=1.2,
                    historical_pe=30,
                    revenue_growth=0.20,
                    eps_growth=0.15,
                    margin_trend=0.05,
                    free_cash_flow_positive=True,
                    market_cap=50_000_000_000,
                    average_volume=5_000_000,
                ),
            ),
            stock_regime_label="risk_on",
            crypto_regime_label="neutral",
        )

        self.assertLess(negative.investment_quality_score, positive.investment_quality_score)
        self.assertIn("negative_or_invalid_pe", negative.alerts)
        self.assertIn("negative_or_invalid_peg", negative.alerts)

    def test_classification_applies_risk_and_data_gates(self):
        scored = score_asset(
            AssetSnapshot(
                symbol="THIN",
                asset_type="stock",
                theme="software",
                candles=uptrend_candles(),
                fundamentals=Fundamentals(
                    pe=20,
                    peg=1.2,
                    historical_pe=22,
                    revenue_growth=0.20,
                    eps_growth=0.15,
                    margin_trend=0.05,
                    free_cash_flow_positive=True,
                    market_cap=500_000_000,
                    average_volume=50_000,
                ),
                event=EventInfo(days_to_earnings=None, guidance_recent=False, post_earnings_gap_percent=0.0),
            ),
            stock_regime_label="risk_on",
            crypto_regime_label="neutral",
        )

        decision = classify_asset(scored, BacktestStats(sample_size=90, win_rate_2r=0.64, win_rate_3r=0.41))

        self.assertNotEqual(decision.decision, "strong_buy_candidate")
        self.assertIn("low_liquidity", decision.alerts)

    def test_small_market_cap_limits_strong_buy_even_when_liquid(self):
        scored = score_asset(
            AssetSnapshot(
                symbol="TINY",
                asset_type="stock",
                theme="software",
                candles=uptrend_candles(),
                fundamentals=Fundamentals(
                    pe=18,
                    peg=1.1,
                    historical_pe=22,
                    revenue_growth=0.30,
                    eps_growth=0.25,
                    margin_trend=0.08,
                    free_cash_flow_positive=True,
                    market_cap=750_000_000,
                    average_volume=5_000_000,
                ),
                event=EventInfo(days_to_earnings=45, guidance_recent=False, post_earnings_gap_percent=0.0),
            ),
            stock_regime_label="risk_on",
            crypto_regime_label="neutral",
        )

        decision = classify_asset(scored, BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.44))

        self.assertEqual(decision.decision, "watch_buy")
        self.assertIn("small_market_cap", decision.alerts)

    def test_below_configured_market_cap_is_avoided_even_with_strong_setup(self):
        scored = score_asset(
            AssetSnapshot(
                symbol="SMALLCOIN",
                asset_type="crypto",
                theme="crypto",
                candles=uptrend_candles(count=220),
                fundamentals=Fundamentals(
                    pe=None,
                    peg=None,
                    historical_pe=None,
                    revenue_growth=None,
                    eps_growth=None,
                    margin_trend=None,
                    free_cash_flow_positive=None,
                    market_cap=2_000_000_000,
                    average_volume=500_000_000,
                ),
                funding_rate=0.001,
                open_interest_change=0.05,
            ),
            stock_regime_label="neutral",
            crypto_regime_label="risk_on",
            minimum_market_cap=5_000_000_000,
        )

        decision = classify_asset(scored, BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.44))

        self.assertEqual(decision.decision, "avoid")
        self.assertIn("below_minimum_market_cap", decision.alerts)

    def test_market_risk_off_blocks_strong_buy_even_with_high_scores(self):
        scored = ScoredAsset(
            snapshot=AssetSnapshot(
                symbol="NVDA",
                asset_type="stock",
                theme="semiconductors",
                candles=uptrend_candles(),
                fundamentals=Fundamentals(
                    pe=35,
                    peg=1.7,
                    historical_pe=38,
                    revenue_growth=0.22,
                    eps_growth=0.18,
                    margin_trend=0.05,
                    free_cash_flow_positive=True,
                    market_cap=3_000_000_000_000,
                    average_volume=35_000_000,
                ),
                event=EventInfo(days_to_earnings=45, guidance_recent=False, post_earnings_gap_percent=0.0),
            ),
            investment_quality_score=92,
            swing_trade_score=92,
            risk_plan=_risk_plan(),
            alerts=["market_risk_off"],
            limitations=[],
            thesis="Teste de risk-off.",
            metrics_summary=["RSI: 55.00"],
            ideal_entry=100,
            alternative_entry=97,
            hold_suggestion="1-8 semanas",
        )

        decision = classify_asset(scored, BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.44))

        self.assertEqual(decision.decision, "watch_buy")
        self.assertIn("market_risk_off", decision.alerts)

    def test_market_neutral_limits_strong_buy_to_watch_buy(self):
        snapshot = AssetSnapshot(
            symbol="AMD",
            asset_type="stock",
            theme="semiconductors",
            candles=uptrend_candles(count=220),
            fundamentals=Fundamentals(
                pe=35,
                peg=1.5,
                historical_pe=38,
                revenue_growth=0.22,
                eps_growth=0.18,
                margin_trend=0.05,
                free_cash_flow_positive=True,
                market_cap=800_000_000_000,
                average_volume=35_000_000,
            ),
            event=EventInfo(days_to_earnings=45, guidance_recent=False, post_earnings_gap_percent=0.0),
        )

        scored = score_asset(snapshot, stock_regime_label="neutral", crypto_regime_label="risk_on")
        decision = classify_asset(scored, BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.44))

        self.assertEqual(decision.decision, "watch_buy")
        self.assertIn("market_not_risk_on", decision.alerts)

    def test_missing_data_limits_aggressive_confidence_but_cvd_proxy_disclosure_does_not(self):
        scored_with_missing_data = ScoredAsset(
            snapshot=AssetSnapshot(
                symbol="MSFT",
                asset_type="stock",
                theme="software",
                candles=uptrend_candles(),
                fundamentals=Fundamentals(
                    pe=32,
                    peg=1.8,
                    historical_pe=35,
                    revenue_growth=None,
                    eps_growth=0.18,
                    margin_trend=0.05,
                    free_cash_flow_positive=True,
                    market_cap=3_000_000_000_000,
                    average_volume=20_000_000,
                ),
                event=EventInfo(days_to_earnings=45, guidance_recent=False, post_earnings_gap_percent=0.0),
            ),
            investment_quality_score=92,
            swing_trade_score=92,
            risk_plan=_risk_plan(),
            alerts=[],
            limitations=["missing_revenue_growth"],
            thesis="Teste de dados incompletos.",
            metrics_summary=["RSI: 55.00"],
            ideal_entry=100,
            alternative_entry=97,
            hold_suggestion="1-8 semanas",
        )
        scored_with_cvd_proxy_only = ScoredAsset(
            snapshot=AssetSnapshot(
                symbol="BTC",
                asset_type="crypto",
                theme="crypto",
                candles=uptrend_candles(),
                fundamentals=Fundamentals(
                    pe=None,
                    peg=None,
                    historical_pe=None,
                    revenue_growth=None,
                    eps_growth=None,
                    margin_trend=None,
                    free_cash_flow_positive=None,
                    market_cap=1_500_000_000_000,
                    average_volume=5_000_000_000,
                ),
            ),
            investment_quality_score=85,
            swing_trade_score=85,
            risk_plan=_risk_plan(),
            alerts=[],
            limitations=["cvd_proxy_uses_taker_buy_sell_volume"],
            thesis="Teste de CVD proxy.",
            metrics_summary=["RSI: 55.00"],
            ideal_entry=100,
            alternative_entry=97,
            hold_suggestion="1-8 semanas",
        )

        missing_data_decision = classify_asset(
            scored_with_missing_data,
            BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.44),
        )
        cvd_proxy_decision = classify_asset(
            scored_with_cvd_proxy_only,
            BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.44),
        )

        self.assertEqual(missing_data_decision.decision, "technical_unvalidated")
        self.assertIn("data_incomplete_confidence_limited", missing_data_decision.limitations)
        self.assertEqual(cvd_proxy_decision.decision, "tradeable")

    def test_missing_sma200_history_limits_aggressive_recommendation(self):
        scored = score_asset(
            AssetSnapshot(
                symbol="MSFT",
                asset_type="stock",
                theme="software",
                candles=uptrend_candles(),
                fundamentals=Fundamentals(
                    pe=30,
                    peg=1.4,
                    historical_pe=34,
                    revenue_growth=0.25,
                    eps_growth=0.20,
                    margin_trend=0.08,
                    free_cash_flow_positive=True,
                    market_cap=3_000_000_000_000,
                    average_volume=20_000_000,
                ),
                event=EventInfo(days_to_earnings=45, guidance_recent=False, post_earnings_gap_percent=0.0),
            ),
            stock_regime_label="risk_on",
            crypto_regime_label="neutral",
            relative_strength_percent=0.08,
        )

        decision = classify_asset(scored, BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.44))

        self.assertEqual(decision.decision, "watch_buy")
        self.assertIn("insufficient_sma200_history", decision.limitations)
        self.assertIn("data_incomplete_confidence_limited", decision.limitations)

    def test_missing_sma200_history_does_not_receive_full_trend_score(self):
        fundamentals = Fundamentals(
            pe=30,
            peg=1.4,
            historical_pe=34,
            revenue_growth=0.25,
            eps_growth=0.20,
            margin_trend=0.08,
            free_cash_flow_positive=True,
            market_cap=3_000_000_000_000,
            average_volume=20_000_000,
        )
        short_history = score_asset(
            AssetSnapshot(
                symbol="MSFT",
                asset_type="stock",
                theme="software",
                candles=uptrend_candles(count=80),
                fundamentals=fundamentals,
                event=EventInfo(days_to_earnings=45, guidance_recent=False, post_earnings_gap_percent=0.0),
            ),
            stock_regime_label="risk_on",
            crypto_regime_label="neutral",
        )
        full_history = score_asset(
            AssetSnapshot(
                symbol="MSFT",
                asset_type="stock",
                theme="software",
                candles=uptrend_candles(count=220),
                fundamentals=fundamentals,
                event=EventInfo(days_to_earnings=45, guidance_recent=False, post_earnings_gap_percent=0.0),
            ),
            stock_regime_label="risk_on",
            crypto_regime_label="neutral",
        )

        self.assertGreater(full_history.swing_trade_score, short_history.swing_trade_score)
        self.assertIn("insufficient_sma200_history", short_history.limitations)

    def test_recent_large_gap_adds_alert_and_blocks_strong_buy(self):
        candles = uptrend_candles()
        previous = candles[-2]
        candles[-1] = Candle(
            "2026-03-21",
            previous.close * 1.12,
            previous.close * 1.15,
            previous.close * 1.10,
            previous.close * 1.13,
            2_000_000,
        )
        snapshot = AssetSnapshot(
            symbol="NVDA",
            asset_type="stock",
            theme="semiconductors",
            candles=candles,
            fundamentals=Fundamentals(
                pe=35,
                peg=1.7,
                historical_pe=38,
                revenue_growth=0.22,
                eps_growth=0.18,
                margin_trend=0.05,
                free_cash_flow_positive=True,
                market_cap=3_000_000_000_000,
                average_volume=35_000_000,
            ),
            event=EventInfo(days_to_earnings=45, guidance_recent=False, post_earnings_gap_percent=0.0),
        )

        scored = score_asset(snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral")
        decision = classify_asset(scored, BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.44))
        report = render_markdown_report([decision], stock_regime="risk_on", crypto_regime="neutral")

        self.assertEqual(decision.decision, "watch_buy")
        self.assertIn("recent_gap_risk", decision.alerts)
        self.assertIn("Recent gap: 12.00%", report)

    def test_report_includes_required_fields_and_probability_language(self):
        snapshot = AssetSnapshot(
            symbol="BTC",
            asset_type="crypto",
            theme="crypto",
            candles=uptrend_candles(count=220),
            fundamentals=Fundamentals(
                pe=None,
                peg=None,
                historical_pe=None,
                revenue_growth=None,
                eps_growth=None,
                margin_trend=None,
                free_cash_flow_positive=None,
                market_cap=1_500_000_000_000,
                average_volume=5_000_000_000,
                market_cap_rank=1,
            ),
            event=None,
            funding_rate=0.01,
            open_interest_change=0.05,
            cvd_proxy=0.12,
            coinbase_premium=0.002,
            liquidation_imbalance=0.2,
        )
        scored = score_asset(snapshot, stock_regime_label="neutral", crypto_regime_label="risk_on")
        classified = classify_asset(scored, BacktestStats(sample_size=74, win_rate_2r=0.62, win_rate_3r=0.38))

        report = render_markdown_report(
            [classified],
            stock_regime="neutral",
            crypto_regime="risk_on",
            portfolio_alerts=["theme_concentration:crypto"],
        )

        self.assertIn("Investment Quality Score", report)
        self.assertIn("Swing Trade Score", report)
        self.assertIn("Metricas principais", report)
        self.assertIn("EMA 9", report)
        self.assertIn("EMA 21", report)
        self.assertIn("SMA 50", report)
        self.assertIn("SMA 200", report)
        self.assertIn("Coinbase premium", report)
        self.assertIn("CVD proxy", report)
        self.assertIn("Liquidation imbalance", report)
        self.assertIn("Market cap: 1500000000000.00", report)
        self.assertIn("Market cap rank: 1", report)
        self.assertIn("Average volume: 5000000000.00", report)
        self.assertIn("theme_concentration:crypto", report)
        self.assertIn("Setup win rate estimado: 62%", report)
        self.assertIn("Amostra: 74 setups parecidos", report)
        self.assertIn("Risco por trade: 250.00 (0.50% do capital)", report)
        self.assertNotIn("garantia", report.lower())

    def test_report_header_shows_data_mode(self):
        decision = _decision("MSFT", "watch_buy", swing=74, investment=91)

        report = render_markdown_report(
            [decision],
            stock_regime="risk_on",
            crypto_regime="neutral",
            data_mode="live",
        )

        self.assertIn("Data mode: `live`", report)

    def test_report_hides_win_rate_when_data_is_not_live(self):
        base = _decision("MSFT", "watch_buy", swing=74, investment=91)
        decision = AssetDecision(
            symbol=base.symbol,
            asset_type=base.asset_type,
            decision=base.decision,
            investment_quality_score=base.investment_quality_score,
            swing_trade_score=base.swing_trade_score,
            risk_plan=base.risk_plan,
            alerts=base.alerts,
            limitations=["demo_data_not_live", "data_incomplete_confidence_limited"],
            thesis=base.thesis,
            metrics_summary=base.metrics_summary,
            ideal_entry=base.ideal_entry,
            alternative_entry=base.alternative_entry,
            hold_suggestion=base.hold_suggestion,
            backtest_stats=base.backtest_stats,
            sample_quality=base.sample_quality,
        )

        report = render_markdown_report(
            [decision],
            stock_regime="risk_on",
            crypto_regime="neutral",
            data_mode="demo",
        )

        self.assertIn("win rate oculto", report)
        self.assertIn("Setup win rate estimado: oculto por dados incompletos ou nao-live", report)
        self.assertNotIn("Setup win rate estimado: 62%", report)
        self.assertNotIn("win rate +2R 62%", report)

    def test_report_uses_historical_hold_when_backtest_sample_has_timing(self):
        snapshot = AssetSnapshot(
            symbol="ETH",
            asset_type="crypto",
            theme="crypto",
            candles=uptrend_candles(),
            fundamentals=Fundamentals(
                pe=None,
                peg=None,
                historical_pe=None,
                revenue_growth=None,
                eps_growth=None,
                margin_trend=None,
                free_cash_flow_positive=None,
                market_cap=450_000_000_000,
                average_volume=2_000_000_000,
            ),
            event=None,
            funding_rate=0.01,
            open_interest_change=0.04,
            cvd_proxy=0.08,
            coinbase_premium=None,
            liquidation_imbalance=None,
        )
        scored = score_asset(snapshot, stock_regime_label="neutral", crypto_regime_label="risk_on")
        stats = _stats_with_2r_hold_median()
        classified = classify_asset(scored, stats)

        report = render_markdown_report(
            [classified],
            stock_regime="neutral",
            crypto_regime="risk_on",
        )

        self.assertIn("Hold sugerido: 2 dias medianos ate +2R; max 1-8 semanas", report)

    def test_report_starts_with_ranked_opportunities(self):
        strong = _decision("NVDA", "strong_buy_candidate", swing=84, investment=88)
        watch = _decision("MSFT", "watch_buy", swing=74, investment=91)
        avoid = _decision("THIN", "avoid", swing=30, investment=40)

        report = render_markdown_report(
            [avoid, watch, strong],
            stock_regime="risk_on",
            crypto_regime="neutral",
        )

        ranking_position = report.index("## Ranking de oportunidades")
        strong_position = report.index("1. `NVDA`")
        watch_position = report.index("2. `MSFT`")
        avoid_position = report.index("3. `THIN`")

        self.assertLess(ranking_position, report.index("## NVDA"))
        self.assertLess(strong_position, watch_position)
        self.assertLess(watch_position, avoid_position)
        self.assertIn("strong_buy_candidate", report)

    def test_main_report_uses_shared_sections_and_separates_research_queue(self):
        watch = _decision("MSFT", "watch_buy", swing=74, investment=91)
        research = _decision("AMD", "technical_unvalidated", swing=78, investment=40)

        report = render_markdown_report(
            [research, watch],
            stock_regime="neutral",
            crypto_regime="neutral",
            data_mode="live",
            report_type="main",
        )

        self.assertIn("## Resumo executivo", report)
        self.assertIn("## Decisao geral", report)
        self.assertIn("## Drivers internos do modelo", report)
        self.assertIn("Sem news/macro real coletado nesta V1", report)
        self.assertIn("## Regime de mercado", report)
        self.assertIn("## Setores fortes e fracos", report)
        self.assertIn("## Watchlist aprovada", report)
        self.assertIn("- `MSFT`", report)
        self.assertIn("## Research queue", report)
        self.assertIn("## Setup tecnico detectado, mas nao validado", report)
        research_section = report.split("## Research queue", 1)[1].split("##", 1)[0]
        technical_section = report.split("## Setup tecnico detectado, mas nao validado", 1)[1].split("##", 1)[0]
        watchlist_section = report.split("## Watchlist aprovada", 1)[1].split("##", 1)[0]
        self.assertIn("`AMD`", technical_section)
        self.assertNotIn("`AMD`", research_section)
        self.assertNotIn("`AMD`", watchlist_section)

    def test_close_report_uses_close_sections(self):
        decision = _decision("MSFT", "watch_buy", swing=74, investment=91)

        report = render_markdown_report(
            [decision],
            stock_regime="risk_on",
            crypto_regime="neutral",
            report_type="close",
        )

        self.assertIn("## Resumo de fechamento", report)
        self.assertIn("## Decisao geral para o proximo dia", report)
        self.assertIn("## Mudancas vs main report", report)
        self.assertIn("## O que melhorou", report)
        self.assertIn("## O que piorou", report)
        self.assertIn("## Watchlist para amanha", report)
        self.assertIn("## Remover/bloquear da watchlist", report)
        self.assertIn("## Stops/invalidation", report)
        self.assertIn("## Preparacao para o proximo pregao", report)

    def test_short_watchlist_is_observational_not_operational(self):
        short_watch = replace(
            _decision("WEAK", "avoid", swing=20, investment=30),
            short_setup_score=80,
            short_status="watch_only",
        )

        report = render_markdown_report(
            [short_watch],
            stock_regime="risk_off",
            crypto_regime="neutral",
            data_mode="live",
            report_type="main",
        )

        self.assertIn("## Shorts observacionais", report)
        self.assertIn("nao operacional", report)
        self.assertNotIn("recomendacao operacional de short", report.lower())

    def test_unverified_news_and_events_do_not_claim_absent_risk(self):
        decision = replace(
            _decision("MSFT", "watch_buy", swing=74, investment=91),
            event_check_status="not_collected",
            news_status="not_collected",
            limitations=["news_not_collected_confidence_limited"],
        )

        report = render_markdown_report(
            [decision],
            stock_regime="risk_on",
            crypto_regime="neutral",
            data_mode="live",
            report_type="main",
        )

        self.assertIn("event_check_status: `not_verified`", report)
        self.assertIn("news_status: `not_verified`", report)
        self.assertNotIn("sem risco de noticia", report.lower())
        self.assertNotIn("sem risco de evento", report.lower())


def _decision(symbol: str, decision: str, *, swing: float, investment: float) -> AssetDecision:
    return AssetDecision(
        symbol=symbol,
        asset_type="stock",
        decision=decision,
        investment_quality_score=investment,
        swing_trade_score=swing,
        risk_plan=_risk_plan(),
        alerts=[],
        limitations=[],
        thesis="Teste de ranking.",
        metrics_summary=["RSI: 55.00"],
        ideal_entry=100,
        alternative_entry=97,
        hold_suggestion="1-8 semanas",
        backtest_stats=BacktestStats(sample_size=74, win_rate_2r=0.62, win_rate_3r=0.38),
        sample_quality="medium",
        market_session="regular",
    )


def _risk_plan() -> RiskPlan:
    return RiskPlan(
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
    )


def _stats_with_2r_hold_median() -> BacktestStats:
    winner = [
        Candle("2026-01-01", 100, 101, 99, 100, 1000),
        Candle("2026-01-02", 101, 106, 100, 105, 1000),
        Candle("2026-01-03", 105, 111, 104, 110, 1000),
    ]
    return summarize_backtest_setups(
        [
            {"candles": winner, "entry": 100, "stop": 95, "max_days": 30}
            for _ in range(30)
        ]
    )


if __name__ == "__main__":
    unittest.main()
