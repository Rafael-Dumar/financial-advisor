import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from advisor.backtest import summarize_backtest_setups
from advisor.cache import SQLiteCache
from advisor.config import AdvisorConfig
from advisor.data_pipeline import stock_snapshot_from_payloads
from advisor.live_loader import LiveDataLoader
from advisor.models import AssetDecision, AssetSnapshot, BacktestStats, Candle, DataFetchMetadata, EventInfo, Fundamentals, ProviderCapability, RiskPlan, ScoredAsset
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
    snapshot_event = event if event is not None else EventInfo(days_to_earnings=45, guidance_recent=False, post_earnings_gap_percent=0)
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
        event=snapshot_event,
        earnings_status=(
            "verified"
            if snapshot_event is not None
            and snapshot_event.days_to_earnings is not None
            and snapshot_event.days_to_earnings >= 0
            else "not_collected"
        ),
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
    def test_unconsumed_fundamental_and_uncollected_context_do_not_cap_an_otherwise_tradeable_stock(self):
        base_snapshot = strong_stock_snapshot("MSFT")
        base_snapshot = replace(
            base_snapshot,
            event=EventInfo(
                days_to_earnings=45,
                guidance_recent=False,
                post_earnings_gap_percent=0.0,
                next_earnings_date="2026-11-24",
            ),
            earnings_status="verified",
        )
        missing_snapshot = replace(
            base_snapshot,
            fundamentals=replace(base_snapshot.fundamentals, revenue_growth=None),
        )
        stats = BacktestStats(
            sample_size=120,
            win_rate_2r=0.66,
            win_rate_3r=0.44,
            expected_value_r=0.5,
            avg_win_r=1.2,
            avg_loss_r=-0.5,
        )

        complete_decision = classify_asset(
            score_asset(base_snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral"),
            stats,
        )
        missing_decision = classify_asset(
            score_asset(missing_snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral"),
            stats,
        )

        self.assertIn("missing_revenue_growth", missing_decision.reason_codes)
        self.assertIn("news_not_collected_confidence_limited", missing_decision.reason_codes)
        self.assertIn("macro_not_collected_confidence_limited", missing_decision.reason_codes)
        self.assertIn("sector_relative_strength_not_collected", missing_decision.reason_codes)
        self.assertEqual(complete_decision.decision, "tradeable")
        self.assertEqual(missing_decision.decision, complete_decision.decision)
        expected_confidence = min(
            missing_decision.data_quality_score,
            int(round((missing_decision.investment_quality_score + missing_decision.swing_trade_score) / 2)),
        )
        self.assertEqual(missing_decision.decision_confidence_score, expected_confidence)

    def test_required_price_history_gap_remains_blocked(self):
        scored = replace(
            strong_scored("ASML"),
            snapshot=replace(strong_stock_snapshot("ASML"), candles=[]),
            limitations=["insufficient_price_history"],
        )

        decision = classify_asset(
            scored,
            BacktestStats(
                sample_size=120,
                win_rate_2r=0.66,
                win_rate_3r=0.44,
                expected_value_r=0.5,
                avg_win_r=1.2,
                avg_loss_r=-0.5,
            ),
        )

        self.assertEqual(decision.decision, "blocked")
        self.assertEqual(decision.data_quality, "blocked")

    def test_report_keeps_valid_backtest_stats_visible_with_unconsumed_missing_field(self):
        decision = classify_asset(
            strong_scored("AMD", limitations=["missing_revenue_growth"]),
            BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.44, expected_value_r=0.5),
        )

        report = render_markdown_report([decision], stock_regime="neutral", crypto_regime="neutral", data_mode="live")

        self.assertIn("Setup win rate estimado: 66%", report)
        self.assertNotIn("Setup win rate estimado: oculto por dados incompletos ou nao-live", report)

    def test_optional_crypto_flow_absence_is_not_an_observed_risk_or_decision_cap(self):
        snapshot = AssetSnapshot(
            symbol="BTC",
            asset_type="crypto",
            theme="crypto",
            candles=candles(),
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
            missing_data=["cvd_proxy_unavailable"],
            data_source="coingecko",
            data_timestamp="2026-10-10T12:00:00Z",
            crypto_metric_provenance={"cvd_proxy": {"status": "not_collected", "provider": "binance"}},
        )
        base_scored = replace(
            strong_scored("BTC", investment=90, swing=90),
            snapshot=snapshot,
            limitations=[],
        )
        missing_flow = replace(base_scored, limitations=["cvd_proxy_unavailable"])
        stats = BacktestStats(
            sample_size=120,
            win_rate_2r=0.66,
            win_rate_3r=0.44,
            expected_value_r=0.5,
            avg_win_r=1.2,
            avg_loss_r=-0.5,
        )

        missing_decision = classify_asset(missing_flow, stats)
        control_decision = classify_asset(base_scored, stats)

        self.assertIn("cvd_proxy_unavailable", missing_decision.reason_codes)
        self.assertIsNone(missing_flow.snapshot.cvd_proxy)
        self.assertNotIn("leverage_risk_funding", missing_decision.alerts)
        self.assertEqual(missing_decision.decision, control_decision.decision)
        self.assertEqual(missing_decision.data_quality, control_decision.data_quality)
        self.assertEqual(missing_decision.decision_confidence_score, control_decision.decision_confidence_score)

        without_funding = score_asset(
            replace(snapshot, missing_data=[], funding_rate=None),
            stock_regime_label="neutral",
            crypto_regime_label="risk_on",
        )
        with_hot_funding = score_asset(
            replace(snapshot, missing_data=[], funding_rate=0.0015),
            stock_regime_label="neutral",
            crypto_regime_label="risk_on",
        )
        self.assertIn("leverage_risk_funding", with_hot_funding.alerts)
        self.assertLess(with_hot_funding.swing_trade_score, without_funding.swing_trade_score)

    def test_optional_crypto_flow_absence_is_audited_without_erasing_measured_risk(self):
        reason_status = {
            "cvd_proxy_unavailable": ("cvd", "crypto.cvd_proxy", "not_collected", "OPTIONAL_CONTEXT"),
            "open_interest_change_unavailable": (
                "open_interest_change",
                "crypto.open_interest_change",
                "not_implemented",
                "OPTIONAL_CONTEXT",
            ),
            "liquidations_unavailable": ("liquidations", "crypto.liquidations", "not_implemented", "UNSUPPORTED"),
            "coinbase_premium_unavailable": (
                "premium",
                "crypto.coinbase_premium",
                "temporary_provider_failure",
                "OPTIONAL_CONTEXT",
            ),
        }
        missing_reasons = list(reason_status)
        snapshot = AssetSnapshot(
            symbol="BTC",
            asset_type="crypto",
            theme="crypto",
            candles=candles(),
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
            missing_data=missing_reasons,
            data_source="coingecko",
            data_timestamp="2026-10-10T12:00:00Z",
            crypto_metric_provenance={
                "cvd": {"provider": "binance", "status": "not_collected"},
                "open_interest_change": {"provider": "hyperliquid", "status": "not_implemented"},
                "liquidations": {"provider": "binance", "status": "not_implemented"},
                "premium": {"provider": "coinbase", "status": "temporary_provider_failure"},
            },
        )
        stats = BacktestStats(
            sample_size=120,
            win_rate_2r=0.66,
            win_rate_3r=0.44,
            expected_value_r=0.5,
            avg_win_r=1.2,
            avg_loss_r=-0.5,
        )
        missing_scored = score_asset(
            snapshot,
            stock_regime_label="neutral",
            crypto_regime_label="risk_on",
        )
        control_scored = score_asset(
            replace(snapshot, missing_data=[], crypto_metric_provenance={}),
            stock_regime_label="neutral",
            crypto_regime_label="risk_on",
        )
        missing_decision = classify_asset(missing_scored, stats)
        control_decision = classify_asset(control_scored, stats)

        self.assertEqual(missing_decision.decision, control_decision.decision)
        self.assertEqual(missing_decision.data_quality, control_decision.data_quality)
        self.assertEqual(missing_decision.decision_confidence_score, control_decision.decision_confidence_score)
        self.assertTrue(set(missing_reasons).issubset(missing_decision.reason_codes))
        self.assertNotIn("leverage_risk_funding", missing_decision.alerts)
        self.assertNotIn("leverage_risk_open_interest", missing_decision.alerts)
        effects_by_reason = {effect.reason_code: effect for effect in missing_scored.evidence_effects}
        for reason, (metric, capability, status, criticality) in reason_status.items():
            with self.subTest(reason=reason):
                effect = effects_by_reason[reason]
                self.assertEqual(effect.capability, capability)
                self.assertEqual(effect.scope, "crypto_asset")
                self.assertEqual(effect.criticality, criticality)
                self.assertEqual(effect.status, status)
                self.assertEqual(effect.evaluation, "not_applicable")
                self.assertEqual(effect.decision_effect, "none")

        without_flow = replace(snapshot, missing_data=[], crypto_metric_provenance={})
        without_flow_scored = score_asset(
            without_flow,
            stock_regime_label="neutral",
            crypto_regime_label="risk_on",
        )
        hot_funding = score_asset(
            replace(without_flow, funding_rate=0.0015),
            stock_regime_label="neutral",
            crypto_regime_label="risk_on",
        )
        hot_open_interest = score_asset(
            replace(without_flow, open_interest_change=0.30),
            stock_regime_label="neutral",
            crypto_regime_label="risk_on",
        )
        material_liquidations = score_asset(
            replace(without_flow, liquidation_imbalance=0.80),
            stock_regime_label="neutral",
            crypto_regime_label="risk_on",
        )
        self.assertIn("leverage_risk_funding", hot_funding.alerts)
        self.assertLess(hot_funding.swing_trade_score, without_flow_scored.swing_trade_score)
        self.assertIn("leverage_risk_open_interest", hot_open_interest.alerts)
        self.assertLess(hot_open_interest.swing_trade_score, without_flow_scored.swing_trade_score)
        self.assertIn("liquidation_pressure", material_liquidations.alerts)

    def test_binance_restriction_keeps_authorized_crypto_fallback_capabilities_available(self):
        config = AdvisorConfig.default(env_file=None)
        config.coingecko_api_key = "test-key"
        end_timestamp_ms = int(datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp() * 1000)
        chart_prices = [
            [end_timestamp_ms - (219 - index) * 86_400_000, 100 + index]
            for index in range(220)
        ]

        def load_with_chart(chart_available):
            def fake_fetch(url, *, payload=None, headers=None):
                if "/coins/markets" in url:
                    return [{"market_cap": 1_500_000_000_000, "total_volume": 5_000_000_000, "market_cap_rank": 1}]
                if "/fapi/v1/klines" in url:
                    raise RuntimeError("http_error:451:restricted location")
                if "/coins/bitcoin/market_chart" in url:
                    return {"prices": chart_prices if chart_available else []}
                if "api.hyperliquid.xyz/info" in url and payload == {"type": "metaAndAssetCtxs"}:
                    return [
                        {"universe": [{"name": "BTC"}]},
                        [{"funding": "0.0001", "openInterest": "100000"}],
                    ]
                if "api.coinbase.com" in url:
                    return {"price": "60000"}
                raise AssertionError(f"unexpected provider request: {url}")

            return LiveDataLoader(config, fetch_json=fake_fetch, today="2026-10-10").load_crypto("BTC")

        fallback_snapshot = load_with_chart(chart_available=True)
        price_provenance = fallback_snapshot.crypto_metric_provenance["candles"]
        with self.subTest(capability="price_provenance"):
            self.assertEqual(price_provenance["status"], "available")
            self.assertEqual(price_provenance["provider"], "coingecko")
            self.assertEqual(price_provenance.get("fallback_from"), "binance")
            self.assertEqual(price_provenance.get("fallback_to"), "coingecko")
            self.assertEqual(price_provenance.get("fallback_reason"), "binance_restricted_location")
        for metric in ("funding", "current_open_interest"):
            with self.subTest(metric=metric):
                provenance = fallback_snapshot.crypto_metric_provenance[metric]
                self.assertEqual(provenance["status"], "available")
                self.assertEqual(provenance["provider"], "hyperliquid")
                self.assertEqual(provenance.get("fallback_from"), "binance")
                self.assertEqual(provenance.get("fallback_to"), "hyperliquid")
                self.assertEqual(provenance.get("fallback_reason"), "binance_flow_unavailable")
        with self.subTest(capability="fallback_diagnostics"):
            self.assertIn("binance_restricted_location", fallback_snapshot.missing_data)
            self.assertIn("binance_flow_unavailable", fallback_snapshot.missing_data)
            self.assertIn("hyperliquid_flow_fallback", fallback_snapshot.missing_data)
            self.assertEqual(len(fallback_snapshot.candles), 220)

        stats = BacktestStats(
            sample_size=120,
            win_rate_2r=0.66,
            win_rate_3r=0.44,
            expected_value_r=0.5,
            avg_win_r=1.2,
            avg_loss_r=-0.5,
        )
        fallback_scored = score_asset(
            fallback_snapshot,
            stock_regime_label="neutral",
            crypto_regime_label="risk_on",
        )
        without_provider_restriction = score_asset(
            replace(
                fallback_snapshot,
                missing_data=[
                    reason
                    for reason in fallback_snapshot.missing_data
                    if reason not in {"binance_restricted_location", "binance_flow_unavailable", "hyperliquid_flow_fallback"}
                ],
            ),
            stock_regime_label="neutral",
            crypto_regime_label="risk_on",
        )
        fallback_decision = classify_asset(fallback_scored, stats)
        no_restriction_decision = classify_asset(without_provider_restriction, stats)
        with self.subTest(capability="valid_fallback_eligibility"):
            self.assertEqual(fallback_decision.decision, no_restriction_decision.decision)
            self.assertEqual(fallback_decision.decision_confidence_score, no_restriction_decision.decision_confidence_score)

        proxy_snapshot = replace(
            fallback_snapshot,
            cvd_proxy=0.2,
            crypto_metric_provenance={
                **fallback_snapshot.crypto_metric_provenance,
                "cvd": {"provider": "binance", "status": "available", "is_proxy": True},
            },
        )
        proxy_scored = score_asset(
            proxy_snapshot,
            stock_regime_label="neutral",
            crypto_regime_label="risk_on",
        )
        with self.subTest(capability="cvd_proxy_disclosure"):
            self.assertIn("cvd_proxy_uses_taker_buy_sell_volume", proxy_scored.limitations)

        no_price_snapshot = load_with_chart(chart_available=False)
        with self.subTest(capability="no_valid_price_fallback"):
            self.assertFalse(no_price_snapshot.candles)
            self.assertIn("price_history_unavailable", no_price_snapshot.missing_data)
        blocked = classify_asset(
            replace(
                strong_scored("BTC"),
                snapshot=no_price_snapshot,
                limitations=no_price_snapshot.missing_data,
            ),
            stats,
        )
        with self.subTest(capability="required_price_failure_still_blocks"):
            self.assertEqual(blocked.decision, "blocked")
            self.assertEqual(blocked.data_quality, "blocked")

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
        short_history_scored = score_asset(
            replace(strong_stock_snapshot("HYPE"), candles=candles(80)),
            stock_regime_label="risk_on",
            crypto_regime_label="neutral",
        )
        unvalidated = classify_asset(
            replace(short_history_scored, investment_quality_score=72, swing_trade_score=82),
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
        freshness_effect = next(
            (effect for effect in decision.evidence_effects if effect.capability == "price_freshness"),
            None,
        )
        self.assertIsNotNone(freshness_effect)
        self.assertEqual(freshness_effect.criticality, "REQUIRED_FOR_DECISION")
        self.assertEqual(freshness_effect.status, "stale")
        self.assertEqual(freshness_effect.evaluation, "unresolved_required_evidence")
        self.assertEqual(freshness_effect.decision_effect, "cap_to_wait")

    def test_market_regime_cap_is_recorded_as_rule_effect(self):
        regime_limited = classify_asset(
            strong_scored("REGIME", alerts=["market_not_risk_on"]),
            BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=None, expected_value_r=0.60),
        )
        regime_effect = next(
            (effect for effect in regime_limited.evidence_effects if effect.capability == "market_regime"),
            None,
        )
        self.assertIsNotNone(regime_effect)
        self.assertEqual(regime_limited.decision, "watch_buy")
        self.assertEqual(regime_effect.criticality, "REQUIRED_FOR_SPECIFIC_RULE")
        self.assertEqual(regime_effect.evaluation, "failed_observed_signal")
        self.assertEqual(regime_effect.decision_effect, "cap_to_watch")

    def test_low_sample_cap_is_recorded_as_rule_effect(self):
        low_sample = classify_asset(
            strong_scored("SAMPLE"),
            BacktestStats(sample_size=0, win_rate_2r=None, win_rate_3r=None, expected_value_r=None),
        )
        sample_effect = next(
            (effect for effect in low_sample.evidence_effects if effect.capability == "backtest.sample_size"),
            None,
        )
        self.assertIsNotNone(sample_effect)
        self.assertEqual(sample_effect.criticality, "REQUIRED_FOR_DECISION")
        self.assertEqual(sample_effect.evaluation, "unresolved_required_evidence")
        self.assertEqual(sample_effect.decision_effect, "cap_to_watch")
        confidence_effect = next(
            (effect for effect in low_sample.evidence_effects if effect.rule_id == "classify_asset.confidence_below_65"),
            None,
        )
        self.assertIsNotNone(confidence_effect)
        self.assertEqual(confidence_effect.capability, "decision_confidence.score")
        self.assertEqual(confidence_effect.decision_effect, "cap_to_watch")
        technical_effect = next(
            (effect for effect in low_sample.evidence_effects if effect.rule_id == "classify_asset.technical_unvalidated_cap"),
            None,
        )
        self.assertIsNotNone(technical_effect)
        self.assertEqual(technical_effect.capability, "technical_setup")
        self.assertEqual(technical_effect.decision_effect, "cap_to_technical_unvalidated")

    def test_measured_win_rate_cap_is_recorded_as_rule_effect(self):
        decision = classify_asset(
            strong_scored("LOW-WIN-RATE"),
            BacktestStats(sample_size=120, win_rate_2r=0.30, win_rate_3r=None, expected_value_r=0.20),
        )

        effect = next(
            (effect for effect in decision.evidence_effects if effect.rule_id == "classify_asset.win_rate_below_35"),
            None,
        )
        self.assertIsNotNone(effect)
        self.assertEqual(effect.capability, "backtest.win_rate_2r")
        self.assertEqual(effect.criticality, "REQUIRED_FOR_SPECIFIC_RULE")
        self.assertEqual(effect.status, "available")
        self.assertEqual(effect.evaluation, "failed_observed_signal")
        self.assertEqual(effect.decision_effect, "cap_to_technical_unvalidated")

    def test_measured_nonpositive_ev_cap_is_recorded_as_rule_effect(self):
        decision = classify_asset(
            strong_scored("NONPOSITIVE-EV"),
            BacktestStats(sample_size=120, win_rate_2r=0.50, win_rate_3r=None, expected_value_r=0.0),
        )

        effect = next(
            (effect for effect in decision.evidence_effects if effect.rule_id == "classify_asset.nonpositive_ev"),
            None,
        )
        self.assertIsNotNone(effect)
        self.assertEqual(effect.capability, "backtest.expected_value_r")
        self.assertEqual(effect.criticality, "REQUIRED_FOR_SPECIFIC_RULE")
        self.assertEqual(effect.status, "available")
        self.assertEqual(effect.evaluation, "failed_observed_signal")
        self.assertEqual(effect.decision_effect, "cap_to_wait")

    def test_high_severity_cap_is_recorded_as_rule_effect(self):
        snapshot = strong_stock_snapshot("UNKNOWN-EARNINGS", event=EventInfo(None, False, 0.0))
        decision = classify_asset(
            score_asset(snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral"),
            BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=None, expected_value_r=0.60),
        )

        effect = next(
            (effect for effect in decision.evidence_effects if effect.rule_id == "classify_asset.high_severity_cap"),
            None,
        )
        self.assertIsNotNone(effect)
        self.assertEqual(effect.capability, "data_quality.missing_data_severity")
        self.assertEqual(effect.criticality, "REQUIRED_FOR_SPECIFIC_RULE")
        self.assertEqual(effect.evaluation, "failed_observed_signal")
        self.assertEqual(effect.decision_effect, "cap_to_technical_unvalidated")

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

    def test_price_fallback_remains_diagnostic_without_a_measured_mismatch(self):
        base_snapshot = strong_stock_snapshot("MSFT", news_events=[{"headline": "verified catalyst", "confirmed_status": "confirmed"}])
        fallback_snapshot = AssetSnapshot(
            **{
                **base_snapshot.__dict__,
                "data_source": "alphavantage",
                "missing_data": ["alphavantage_price_fallback"],
            }
        )

        fmp_decision = classify_asset(
            score_asset(base_snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral"),
            BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.44, expected_value_r=0.80),
        )
        fallback_decision = classify_asset(
            score_asset(fallback_snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral"),
            BacktestStats(sample_size=120, win_rate_2r=0.66, win_rate_3r=0.44, expected_value_r=0.80),
        )

        self.assertIn("mixed_provider_data", fallback_decision.limitations)
        self.assertIn("source_mismatch_possible", fallback_decision.alerts)
        self.assertEqual(fallback_decision.decision_confidence_score, fmp_decision.decision_confidence_score)
        self.assertEqual(fallback_decision.decision, fmp_decision.decision)

    def test_compatible_yahoo_prices_and_fmp_fundamentals_do_not_reduce_eligibility(self):
        source_timestamp = "2026-10-09T20:00:00+00:00"
        base_snapshot = strong_stock_snapshot(
            "MSFT",
            news_events=[{"headline": "verified catalyst", "confirmed_status": "confirmed"}],
        )
        fmp_snapshot = replace(
            base_snapshot,
            data_source="fmp",
            cache_age_seconds=3600,
            data_fetch_metadata=DataFetchMetadata(
                provider="fmp",
                endpoint="historical_prices",
                source_timestamp=source_timestamp,
                cache_age_seconds=3600,
                is_fresh=True,
                granularity="daily",
                market_data_kind="eod_candle",
            ),
            provider_capabilities=[
                ProviderCapability("fmp", "historical_prices", True, True, True, "available", False),
                ProviderCapability("fmp", "fundamentals", True, True, True, "available", False),
            ],
        )
        compatible_mixed_snapshot = replace(
            base_snapshot,
            data_source="yahoo",
            missing_data=["yahoo_price_fallback"],
            cache_age_seconds=3600,
            data_fetch_metadata=DataFetchMetadata(
                provider="yahoo",
                endpoint="historical_prices",
                source_timestamp=source_timestamp,
                cache_age_seconds=3600,
                is_fresh=True,
                fallback_used=True,
                fallback_from="fmp",
                fallback_to="yahoo",
                granularity="daily",
                market_data_kind="eod_candle",
            ),
            provider_capabilities=[
                ProviderCapability("yahoo", "prices", True, True, True, "available", False),
                ProviderCapability("fmp", "fundamentals", True, True, True, "available", False),
            ],
        )
        stats = BacktestStats(
            sample_size=120,
            win_rate_2r=0.66,
            win_rate_3r=0.44,
            expected_value_r=0.55,
            avg_win_r=1.2,
            avg_loss_r=-0.5,
        )

        fmp_decision = classify_asset(
            score_asset(fmp_snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral"),
            stats,
        )
        mixed_decision = classify_asset(
            score_asset(compatible_mixed_snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral"),
            stats,
        )

        self.assertIn("mixed_provider_data", mixed_decision.limitations)
        self.assertIn("source_mismatch_possible", mixed_decision.alerts)
        self.assertEqual(mixed_decision.decision, fmp_decision.decision)
        self.assertEqual(mixed_decision.decision_confidence_score, fmp_decision.decision_confidence_score)

    def test_intraday_price_metadata_conflict_limits_only_price_history_rule(self):
        source_timestamp = "2026-10-09T20:00:00+00:00"
        mixed_snapshot = replace(
            strong_stock_snapshot("MSFT"),
            data_source="yahoo",
            missing_data=["yahoo_price_fallback"],
            cache_age_seconds=3600,
            data_fetch_metadata=DataFetchMetadata(
                provider="yahoo",
                endpoint="historical_prices",
                source_timestamp=source_timestamp,
                cache_age_seconds=3600,
                is_fresh=True,
                fallback_used=True,
                fallback_from="fmp",
                fallback_to="yahoo",
                granularity="daily",
                market_data_kind="eod_candle",
            ),
            provider_capabilities=[
                ProviderCapability("yahoo", "prices", True, True, True, "available", False),
                ProviderCapability("fmp", "fundamentals", True, True, True, "available", False),
            ],
        )
        incompatible_snapshot = replace(
            mixed_snapshot,
            data_fetch_metadata=replace(
                mixed_snapshot.data_fetch_metadata,
                granularity="intraday",
                market_data_kind="intraday_quote",
            ),
        )
        stats = BacktestStats(
            sample_size=120,
            win_rate_2r=0.66,
            win_rate_3r=0.44,
            expected_value_r=0.55,
            avg_win_r=1.2,
            avg_loss_r=-0.5,
        )

        compatible_decision = classify_asset(
            score_asset(mixed_snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral"),
            stats,
        )
        incompatible_decision = classify_asset(
            score_asset(incompatible_snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral"),
            stats,
        )

        self.assertEqual(compatible_decision.decision, "tradeable")
        self.assertNotEqual(incompatible_decision.decision, "tradeable")
        price_effect = next(
            item for item in incompatible_decision.evidence_effects if item.capability == "price_history"
        )
        self.assertEqual(price_effect.scope, "specific_rule")
        self.assertEqual(price_effect.criticality, "REQUIRED_FOR_SPECIFIC_RULE")
        self.assertEqual(price_effect.status, "invalid")
        self.assertEqual(price_effect.evaluation, "unresolved_required_evidence")
        self.assertEqual(price_effect.decision_effect, "cap_to_technical_unvalidated")
        self.assertEqual(price_effect.reason_code, "price_timeframe_incompatible")

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

    def test_stock_approval_requires_verified_earnings_timing_and_preserves_proximity_rules(self):
        stats = BacktestStats(
            sample_size=120,
            win_rate_2r=0.66,
            win_rate_3r=0.44,
            expected_value_r=0.5,
            avg_win_r=1.2,
            avg_loss_r=-0.5,
        )
        effective_now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)

        def decide(snapshot):
            scored = score_asset(snapshot, stock_regime_label="risk_on", crypto_regime_label="neutral")
            return classify_asset(scored, stats, effective_now_utc=effective_now)

        distant = decide(
            replace(
                strong_stock_snapshot("DISTANT"),
                event=EventInfo(
                    days_to_earnings=45,
                    guidance_recent=False,
                    post_earnings_gap_percent=0.0,
                    next_earnings_date="2026-11-23",
                ),
                earnings_status="verified",
            )
        )
        near = decide(
            replace(
                strong_stock_snapshot("NEAR"),
                event=EventInfo(
                    days_to_earnings=10,
                    guidance_recent=False,
                    post_earnings_gap_percent=0.0,
                    next_earnings_date="2026-10-19",
                ),
                earnings_status="verified",
            )
        )
        imminent = decide(
            replace(
                strong_stock_snapshot("IMMINENT"),
                event=EventInfo(
                    days_to_earnings=5,
                    guidance_recent=False,
                    post_earnings_gap_percent=0.0,
                    next_earnings_date="2026-10-14",
                ),
                earnings_status="verified",
            )
        )

        self.assertEqual(distant.decision, "tradeable")
        self.assertEqual(distant.event_check_status, "verified")
        self.assertNotEqual(near.decision, "tradeable")
        self.assertIn("earnings_near", near.alerts)
        self.assertNotIn("earnings_imminent", near.alerts)
        self.assertEqual(imminent.decision, "wait")
        self.assertIn("earnings_near", imminent.alerts)
        self.assertIn("earnings_imminent", imminent.alerts)

        dated_but_unverified = decide(
            replace(
                strong_stock_snapshot("UNVERIFIED"),
                event=EventInfo(
                    days_to_earnings=45,
                    guidance_recent=False,
                    post_earnings_gap_percent=0.0,
                    next_earnings_date="2026-11-23",
                ),
                earnings_status="not_implemented",
            )
        )
        self.assertEqual(dated_but_unverified.event_check_status, "not_implemented")
        self.assertNotEqual(dated_but_unverified.decision, "tradeable")

        unknown = decide(
            replace(
                strong_stock_snapshot("UNKNOWN"),
                event=None,
                earnings_status="not_collected",
            )
        )
        self.assertEqual(unknown.event_check_status, "not_collected")
        self.assertNotEqual(unknown.decision, "tradeable")

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
        decisions = [replace(decision, market_session="regular") for decision in decisions]

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

    def test_missing_ev_components_remain_diagnostic_when_consumed_metrics_are_valid(self):
        scored = strong_scored("AMD", investment=90, swing=90)

        decision = classify_asset(
            scored,
            BacktestStats(sample_size=120, win_rate_2r=0.62, win_rate_3r=0.35, expected_value_r=0.55),
        )

        self.assertGreater(decision.decision_confidence_score, 60)
        self.assertEqual(decision.decision, "tradeable")
        self.assertIn("news_not_collected_confidence_limited", decision.reason_codes)
        self.assertIn("macro_not_collected_confidence_limited", decision.reason_codes)
        self.assertIn("sector_relative_strength_not_collected", decision.reason_codes)
        self.assertIn("ev_components_missing", decision.reason_codes)

    def test_missing_average_decomposition_does_not_cap_valid_backtest_metrics(self):
        scored = strong_scored("AMD", investment=90, swing=90)
        consumed_metrics = {
            "sample_size": 120,
            "win_rate_2r": 0.62,
            "win_rate_3r": 0.35,
            "expected_value_r": 0.55,
        }
        complete = classify_asset(
            scored,
            BacktestStats(**consumed_metrics, avg_win_r=1.1, avg_loss_r=-0.25),
        )
        missing_average = classify_asset(
            scored,
            BacktestStats(**consumed_metrics, avg_win_r=1.1, avg_loss_r=None),
        )

        self.assertEqual(missing_average.decision, complete.decision)
        self.assertEqual(missing_average.decision_confidence_score, complete.decision_confidence_score)
        self.assertIn("ev_components_missing", missing_average.reason_codes)

        report = render_markdown_report(
            [missing_average],
            stock_regime="neutral",
            crypto_regime="neutral",
            data_mode="live",
        )
        self.assertIn("expected_value_r: 0.55", report)
        self.assertNotIn("expected_value_r: 0.55 (limited/model_estimate)", report)

        no_positive_outcomes = classify_asset(
            scored,
            BacktestStats(
                sample_size=120,
                win_rate_2r=0.0,
                win_rate_3r=0.0,
                expected_value_r=-0.10,
                avg_win_r=None,
                avg_loss_r=-0.10,
            ),
        )
        no_positive_report = render_markdown_report(
            [no_positive_outcomes],
            stock_regime="neutral",
            crypto_regime="neutral",
            data_mode="live",
        )
        self.assertIn("expected_value_r: -0.10", no_positive_report)
        self.assertNotIn("expected_value_r: -0.10 (limited/model_estimate)", no_positive_report)

    def test_expected_value_remains_direct_without_avg_win_loss_components(self):
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

        self.assertIn("expected_value_r: 0.52", report)
        self.assertNotIn("expected_value_r: 0.52 (limited/model_estimate)", report)

    def test_adequate_backtest_requires_valid_consumed_statistics(self):
        scored = strong_scored("AMD", investment=90, swing=90)
        valid = classify_asset(
            scored,
            BacktestStats(sample_size=120, win_rate_2r=0.62, win_rate_3r=0.35, expected_value_r=0.55),
        )
        self.assertEqual(valid.decision, "tradeable")

        invalid_stats = [
            ("missing win_rate_2r", BacktestStats(sample_size=120, win_rate_2r=None, win_rate_3r=0.35, expected_value_r=0.55)),
            ("missing expected_value_r", BacktestStats(sample_size=120, win_rate_2r=0.62, win_rate_3r=0.35, expected_value_r=None)),
            ("nonfinite win_rate_2r", BacktestStats(sample_size=120, win_rate_2r=float("nan"), win_rate_3r=0.35, expected_value_r=0.55)),
            ("out-of-range win_rate_2r", BacktestStats(sample_size=120, win_rate_2r=1.2, win_rate_3r=0.35, expected_value_r=0.55)),
            ("nonfinite expected_value_r", BacktestStats(sample_size=120, win_rate_2r=0.62, win_rate_3r=0.35, expected_value_r=float("inf"))),
        ]
        for label, stats in invalid_stats:
            with self.subTest(label=label):
                decision = classify_asset(scored, stats)
                self.assertNotEqual(decision.decision, "tradeable")
                capability = "backtest.win_rate_2r" if "win_rate_2r" in label else "backtest.expected_value_r"
                effect = next(item for item in decision.evidence_effects if item.capability == capability)
                self.assertEqual(effect.scope, "specific_rule")
                self.assertEqual(effect.criticality, "REQUIRED_FOR_SPECIFIC_RULE")
                self.assertEqual(effect.status, "not_collected" if "missing" in label else "invalid")
                self.assertEqual(effect.evaluation, "unresolved_required_evidence")
                self.assertEqual(effect.decision_effect, "cap_to_watch")

        no_backtest = classify_asset(scored, None)
        low_sample = classify_asset(
            scored,
            BacktestStats(sample_size=20, win_rate_2r=None, win_rate_3r=None, expected_value_r=None),
        )
        low_quality = classify_asset(
            scored,
            BacktestStats(
                sample_size=120,
                win_rate_2r=0.62,
                win_rate_3r=0.35,
                expected_value_r=0.55,
                setup_quality="low",
            ),
        )
        self.assertNotEqual(no_backtest.decision, "tradeable")
        self.assertNotEqual(low_sample.decision, "tradeable")
        self.assertNotEqual(low_quality.decision, "tradeable")

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
        self.assertIn("weak_setup_win_rate", decision.alerts)
        self.assertNotIn("negative_ev_with_high_data_severity", decision.alerts)

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

        self.assertEqual(decision.decision, "watch_buy")
        self.assertNotIn("high_severity_data_not_watchlist", decision.alerts)

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
