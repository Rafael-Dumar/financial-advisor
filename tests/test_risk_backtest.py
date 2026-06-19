import unittest

from advisor.backtest import backtest_r_multiple, backtest_similar_setups, summarize_backtest_setups
from advisor.models import Candle
from advisor.risk import (
    calculate_trade_plan,
    detect_return_correlation,
    detect_theme_concentration,
    evaluate_leverage_policy,
    rate_sample_quality,
)


class RiskAndBacktestTests(unittest.TestCase):
    def test_trade_plan_calculates_stop_targets_and_position_size(self):
        plan = calculate_trade_plan(
            entry=100,
            stop=95,
            account_capital=50_000,
            risk_fraction=0.005,
            atr_value=3,
            average_volume=2_000_000,
        )

        self.assertEqual(plan.risk_amount, 250)
        self.assertEqual(plan.risk_fraction, 0.005)
        self.assertEqual(plan.max_position_units, 50)
        self.assertEqual(plan.max_position_value, 5_000)
        self.assertEqual(plan.target_2r, 110)
        self.assertEqual(plan.target_3r, 115)
        self.assertEqual(plan.risk_reward_2r, "2.00:1")

    def test_trade_plan_rejects_excessive_configured_risk(self):
        with self.assertRaises(ValueError):
            calculate_trade_plan(
                entry=100,
                stop=95,
                account_capital=50_000,
                risk_fraction=0.02,
                atr_value=3,
                average_volume=2_000_000,
            )

    def test_trade_plan_caps_position_at_available_capital_and_reports_actual_risk(self):
        plan = calculate_trade_plan(
            entry=100,
            stop=99.9,
            account_capital=50_000,
            risk_fraction=0.005,
            atr_value=1,
            average_volume=2_000_000,
        )

        self.assertEqual(plan.max_position_units, 500)
        self.assertEqual(plan.max_position_value, 50_000)
        self.assertAlmostEqual(plan.risk_amount, 50)
        self.assertAlmostEqual(plan.risk_fraction, 0.001)
        self.assertIn("position_capped_by_account_capital", plan.alerts)

    def test_trade_plan_does_not_move_invalidation_to_fit_position_size(self):
        plan = calculate_trade_plan(
            entry=100,
            stop=80,
            account_capital=50_000,
            risk_fraction=0.005,
            atr_value=8,
            average_volume=2_000_000,
        )

        self.assertEqual(plan.stop, 80)
        self.assertEqual(plan.per_unit_risk, 20)
        self.assertEqual(plan.max_position_units, 12)
        self.assertLessEqual(plan.risk_amount, 250)

    def test_leverage_is_blocked_for_low_confidence_or_blocking_missing_data(self):
        low_confidence = evaluate_leverage_policy(
            decision_confidence_score=45,
            missing_data_severity="low",
        )
        blocking_missing_data = evaluate_leverage_policy(
            decision_confidence_score=85,
            missing_data_severity="blocking",
        )
        allowed = evaluate_leverage_policy(
            decision_confidence_score=85,
            missing_data_severity="low",
        )

        self.assertFalse(low_confidence.allowed)
        self.assertIn("low_decision_confidence", low_confidence.reasons)
        self.assertFalse(blocking_missing_data.allowed)
        self.assertIn("blocking_missing_data", blocking_missing_data.reasons)
        self.assertTrue(allowed.allowed)

    def test_backtest_counts_plus_2r_before_minus_1r(self):
        candles = [
            Candle("2026-01-01", 100, 101, 99, 100, 1000),
            Candle("2026-01-02", 101, 106, 100, 105, 1000),
            Candle("2026-01-03", 105, 111, 104, 110, 1000),
        ]

        outcome = backtest_r_multiple(candles, entry=100, stop=95, max_days=30)

        self.assertTrue(outcome.hit_2r)
        self.assertFalse(outcome.stopped)
        self.assertEqual(outcome.days_held, 2)

    def test_backtest_continues_after_two_r_to_measure_three_r_and_timing(self):
        candles = [
            Candle("2026-01-01", 100, 101, 99, 100, 1000),
            Candle("2026-01-02", 101, 111, 100, 110, 1000),
            Candle("2026-01-03", 110, 116, 109, 115, 1000),
        ]

        outcome = backtest_r_multiple(candles, entry=100, stop=95, max_days=30)

        self.assertTrue(outcome.hit_2r)
        self.assertTrue(outcome.hit_3r)
        self.assertEqual(outcome.days_to_2r, 1)
        self.assertEqual(outcome.days_to_3r, 2)
        self.assertEqual(outcome.days_held, 2)

    def test_backtest_preserves_two_r_win_when_stop_happens_before_three_r(self):
        candles = [
            Candle("2026-01-01", 100, 101, 99, 100, 1000),
            Candle("2026-01-02", 101, 111, 100, 110, 1000),
            Candle("2026-01-03", 96, 98, 94, 95, 1000),
        ]

        outcome = backtest_r_multiple(candles, entry=100, stop=95, max_days=30)

        self.assertTrue(outcome.hit_2r)
        self.assertFalse(outcome.hit_3r)
        self.assertTrue(outcome.stopped)
        self.assertEqual(outcome.days_to_2r, 1)

    def test_backtest_expires_when_neither_stop_nor_target_is_hit(self):
        candles = [
            Candle("2026-01-01", 100, 101, 99, 100, 1000),
            Candle("2026-01-02", 101, 105, 99, 102, 1000),
            Candle("2026-01-03", 102, 106, 100, 103, 1000),
        ]

        outcome = backtest_r_multiple(candles, entry=100, stop=95, max_days=2)

        self.assertTrue(outcome.expired)
        self.assertFalse(outcome.hit_2r)
        self.assertFalse(outcome.stopped)
        self.assertEqual(outcome.days_held, 2)

    def test_backtest_treats_gap_below_stop_as_worse_than_planned_stop(self):
        candles = [
            Candle("2026-01-01", 100, 101, 99, 100, 1000),
            Candle("2026-01-02", 90, 92, 88, 91, 1000),
        ]

        outcome = backtest_r_multiple(candles, entry=100, stop=95, max_days=30)

        self.assertTrue(outcome.stopped)
        self.assertFalse(outcome.hit_2r)
        self.assertEqual(outcome.exit_reason, "gap_stop")
        self.assertEqual(outcome.r_multiple, -2.0)

    def test_backtest_applies_costs_and_slippage_to_r_multiple(self):
        candles = [
            Candle("2026-01-01", 100, 101, 99, 100, 1000),
            Candle("2026-01-02", 101, 111, 100, 110, 1000),
        ]

        outcome = backtest_r_multiple(candles, entry=100, stop=95, max_days=30, cost_r=0.05, slippage_r=0.05)

        self.assertTrue(outcome.hit_2r)
        self.assertAlmostEqual(outcome.r_multiple, 1.9)

    def test_backtest_uses_conservative_stop_first_for_ambiguous_candle(self):
        candles = [
            Candle("2026-01-01", 100, 101, 99, 100, 1000),
            Candle("2026-01-02", 100, 116, 94, 105, 1000),
        ]

        outcome = backtest_r_multiple(candles, entry=100, stop=95, max_days=30)

        self.assertTrue(outcome.stopped)
        self.assertFalse(outcome.hit_2r)
        self.assertFalse(outcome.hit_3r)

    def test_backtest_summary_reports_sample_size_and_win_rates(self):
        winner = [
            Candle("2026-01-01", 100, 101, 99, 100, 1000),
            Candle("2026-01-02", 101, 106, 100, 105, 1000),
            Candle("2026-01-03", 105, 111, 104, 110, 1000),
        ]
        three_r_winner = [
            Candle("2026-01-01", 100, 101, 99, 100, 1000),
            Candle("2026-01-02", 101, 106, 100, 105, 1000),
            Candle("2026-01-03", 105, 116, 104, 115, 1000),
        ]
        loser = [
            Candle("2026-01-01", 100, 101, 99, 100, 1000),
            Candle("2026-01-02", 99, 100, 94, 95, 1000),
        ]

        stats = summarize_backtest_setups(
            [
                {"candles": winner, "entry": 100, "stop": 95, "max_days": 30},
                {"candles": three_r_winner, "entry": 100, "stop": 95, "max_days": 30},
                {"candles": loser, "entry": 100, "stop": 95, "max_days": 30},
            ]
        )

        self.assertEqual(stats.sample_size, 3)
        self.assertAlmostEqual(stats.win_rate_2r, 2 / 3)
        self.assertAlmostEqual(stats.win_rate_3r, 1 / 3)
        self.assertEqual(getattr(stats, "median_days_to_2r", None), 2)
        self.assertEqual(getattr(stats, "median_days_to_3r", None), 2)

    def test_backtest_summary_reports_drawdown_period_and_bias_warnings(self):
        winner = [
            Candle("2026-01-01", 100, 101, 99, 100, 1000),
            Candle("2026-01-02", 101, 111, 100, 110, 1000),
        ]
        loser = [
            Candle("2026-02-01", 100, 101, 99, 100, 1000),
            Candle("2026-02-02", 99, 100, 94, 95, 1000),
        ]

        stats = summarize_backtest_setups(
            [
                {"candles": loser, "entry": 100, "stop": 95, "max_days": 30, "benchmark": "SPY", "benchmark_return": -0.02},
                {"candles": winner, "entry": 100, "stop": 95, "max_days": 30, "benchmark": "SPY", "benchmark_return": 0.01},
            ],
            cost_r=0.05,
            slippage_r=0.05,
            out_of_sample_fraction=0.5,
        )

        self.assertEqual(stats.period_start, "2026-01-01")
        self.assertEqual(stats.period_end, "2026-02-02")
        self.assertLess(stats.max_drawdown_r, 0)
        self.assertIn("SPY", stats.benchmark_comparison)
        self.assertIn("possible_lookahead_bias_check_required", stats.warnings)
        self.assertIn("out_of_sample_fraction=0.50", stats.warnings)

    def test_backtest_similar_setups_uses_historical_candles_not_subjective_probability(self):
        candles = [
            Candle(
                f"2026-01-{(index % 28) + 1:02d}",
                100 + index * 2,
                100 + index * 2 + 1,
                100 + index * 2 - 1,
                100 + index * 2,
                1_000_000,
            )
            for index in range(140)
        ]

        stats = backtest_similar_setups(candles, max_days=30)

        self.assertGreaterEqual(stats.sample_size, 30)
        self.assertIsNotNone(stats.win_rate_2r)
        self.assertIsNotNone(stats.win_rate_3r)

    def test_sample_quality_limits_low_sample_confidence(self):
        self.assertEqual(rate_sample_quality(8), "low")
        self.assertEqual(rate_sample_quality(45), "medium")
        self.assertEqual(rate_sample_quality(150), "high")

    def test_detect_theme_concentration_flags_crowded_themes(self):
        warnings = detect_theme_concentration(
            {"NVDA": "semiconductors", "AMD": "semiconductors", "MU": "semiconductors"},
            max_same_theme=2,
        )

        self.assertIn("theme_concentration:semiconductors", warnings)

    def test_detect_return_correlation_flags_highly_correlated_assets(self):
        first = [
            Candle(f"2026-01-{index + 1:02d}", close, close + 1, close - 1, close, 1_000_000)
            for index, close in enumerate([100, 102, 101, 104, 103, 107, 106, 110, 108, 112])
        ]
        second = [
            Candle(f"2026-01-{index + 1:02d}", close, close + 1, close - 1, close, 1_000_000)
            for index, close in enumerate([200, 204, 202, 208, 206, 214, 212, 220, 216, 224])
        ]
        inverse = [
            Candle(f"2026-01-{index + 1:02d}", close, close + 1, close - 1, close, 1_000_000)
            for index, close in enumerate([200, 196, 198, 192, 194, 186, 188, 180, 184, 176])
        ]

        warnings = detect_return_correlation(
            {"AMD": first, "NVDA": second, "DEFENSIVE": inverse},
            minimum_observations=8,
            threshold=0.90,
        )

        self.assertTrue(any(warning.startswith("return_correlation:AMD:NVDA:") for warning in warnings))
        self.assertFalse(any("DEFENSIVE" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
