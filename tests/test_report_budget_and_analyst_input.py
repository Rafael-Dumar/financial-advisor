from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from advisor.models import AssetDecision, BacktestStats, RiskPlan
from advisor.cli import _provider_budget_summary
from advisor.config import AdvisorConfig
from advisor.report import render_analyst_review_input, render_markdown_report


def _decision(
    symbol: str,
    *,
    asset_type: str = "stock",
    decision: str = "watch_buy",
    swing_trade_score: float = 65,
) -> AssetDecision:
    return AssetDecision(
        symbol=symbol,
        asset_type=asset_type,
        decision=decision,
        investment_quality_score=70,
        swing_trade_score=swing_trade_score,
        risk_plan=RiskPlan(
            entry=100,
            stop=95,
            target_2r=110,
            target_3r=115,
            per_unit_risk=5,
            risk_amount=250,
            risk_fraction=0.005,
            max_position_units=50,
            max_position_value=5000,
            risk_reward_2r="2.0",
            alerts=[],
        ),
        alerts=["earnings_data_missing"] if asset_type == "stock" else [],
        limitations=["news_not_verified"],
        thesis=f"{symbol} has a setup worth review.",
        metrics_summary=["revenue growth: ok", "valuation: not cheap"],
        ideal_entry=100,
        alternative_entry=None,
        hold_suggestion="swing",
        backtest_stats=BacktestStats(sample_size=30, win_rate_2r=0.5, win_rate_3r=None),
        sample_quality="medium",
        reason_codes=["setup_present"],
        data_quality="ok",
        missing_data_severity="medium",
        news_summary="not_collected",
        event_check_status="not_collected",
        news_status="not_collected",
        market_session="regular",
        decision_confidence_score=60,
    )


class ReportBudgetAndAnalystInputTests(unittest.TestCase):
    def test_report_includes_provider_budget_summary_and_few_asset_reason(self) -> None:
        report = render_markdown_report(
            [_decision("MSFT")],
            stock_regime="neutral",
            crypto_regime="risk_off",
            provider_budget={
                "estimated_calls": {"fmp": 9, "coingecko": 1},
                "used_calls": {"fmp": 2},
                "cache_hits": 3,
                "cache_misses": 2,
                "universe_requested": 3,
                "universe_scanned": 1,
                "discovery_enabled": False,
                "skipped_due_to_api_budget": False,
                "provider_rate_limit_status": "ok",
                "few_assets_reason": "budget_limit",
                "actions_cache_hit": "true",
            },
        )

        self.assertIn("## provider_budget_summary", report)
        self.assertIn("- fmp_calls_estimated: 9", report)
        self.assertIn("- fmp_calls_used: 2", report)
        self.assertIn("- cache_hits: 3", report)
        self.assertIn("- cache_misses: 2", report)
        self.assertIn("- few_assets_reason: `budget_limit`", report)
        self.assertIn("- actions_cache_hit: `true`", report)

    def test_analyst_review_input_keeps_only_top_equity_candidates(self) -> None:
        decisions = [
            _decision("MSFT", swing_trade_score=90),
            _decision("NVDA", swing_trade_score=80),
            _decision("AMD", swing_trade_score=70),
            _decision("AVGO", swing_trade_score=10),
        ]

        text = render_analyst_review_input(
            decisions,
            report_type="main",
            data_mode="live",
            stock_regime="neutral",
            crypto_regime="risk_off",
            generated_at="2026-06-21T12:00:00-03:00",
        )

        self.assertIn("# Analyst review input", text)
        self.assertIn("report_type: `main`", text)
        self.assertIn("Top equity candidates for qualitative review", text)
        self.assertIn("MSFT", text)
        self.assertIn("NVDA", text)
        self.assertIn("AMD", text)
        self.assertNotIn("AVGO", text)

    def test_analyst_review_input_has_exact_no_candidate_phrase(self) -> None:
        text = render_analyst_review_input(
            [_decision("HYPE", asset_type="crypto", decision="technical_unvalidated")],
            report_type="main",
            data_mode="live",
            stock_regime="neutral",
            crypto_regime="risk_off",
            generated_at="2026-06-21T12:00:00-03:00",
        )

        self.assertIn("No equity candidates for qualitative review", text)
        self.assertIn("## Crypto review needed", text)
        self.assertIn("HYPE", text)
        self.assertIn("technical_unvalidated is not approval to buy", text)

    def test_report_command_writes_analyst_review_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"
            env = os.environ.copy()
            env["ADVISOR_ENV_FILE"] = str(Path(tmp) / "missing.env")
            env.pop("FMP_API_KEY", None)
            env.pop("COINGECKO_API_KEY", None)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "report",
                    "main",
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((output_dir / "analyst-review-input.md").exists())
            self.assertIn("# Analyst review input", (output_dir / "analyst-review-input.md").read_text(encoding="utf-8"))

    def test_provider_budget_universe_requested_is_before_stock_cap(self) -> None:
        config = AdvisorConfig.default()
        config.stock_watchlist = ["MSFT", "NVDA", "AMD", "AVGO"]
        config.crypto_watchlist = ["HYPE"]
        config.max_stocks_per_run = 2

        summary = _provider_budget_summary(
            config,
            include_discovery=False,
            universe_scanned=3,
        )

        self.assertEqual(summary["universe_requested"], 5)
        self.assertEqual(summary["universe_scanned"], 3)
        self.assertEqual(summary["few_assets_reason"], "budget_limit")


if __name__ == "__main__":
    unittest.main()
