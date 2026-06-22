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
    investment_quality_score: float = 70,
    market_session: str = "regular",
    missing_data_severity: str = "medium",
    expected_value_r: float | None = None,
    limitations: list[str] | None = None,
    short_setup_score: float = 0,
    short_status: str = "not_evaluated",
) -> AssetDecision:
    return AssetDecision(
        symbol=symbol,
        asset_type=asset_type,
        decision=decision,
        investment_quality_score=investment_quality_score,
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
        thesis=f"{symbol} has a setup worth review.",
        metrics_summary=["revenue growth: ok", "valuation: not cheap"],
        ideal_entry=100,
        alternative_entry=None,
        hold_suggestion="swing",
        backtest_stats=BacktestStats(
            sample_size=30,
            win_rate_2r=0.5,
            win_rate_3r=None,
            expected_value_r=expected_value_r,
            avg_win_r=1.2 if expected_value_r is not None else None,
            avg_loss_r=-1.0 if expected_value_r is not None else None,
        ),
        sample_quality="medium",
        reason_codes=["setup_present"],
        data_quality="ok",
        missing_data_severity=missing_data_severity,
        news_summary="not_collected",
        event_check_status="not_collected",
        news_status="not_collected",
        market_session=market_session,
        decision_confidence_score=60,
        short_setup_score=short_setup_score,
        short_status=short_status,
        limitations=limitations or ["news_not_verified"],
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
                "cache_reused_from_main": True,
                "close_universe_source": "main_baseline",
                "skipped_provider_calls_due_to_cache": 3,
                "skipped_provider_calls_due_to_rate_limit": 0,
                "fmp_status": "ok",
            },
        )

        self.assertIn("## provider_budget_summary", report)
        self.assertIn("- fmp_calls_estimated: 9", report)
        self.assertIn("- fmp_calls_used: 2", report)
        self.assertIn("- cache_hits: 3", report)
        self.assertIn("- cache_misses: 2", report)
        self.assertIn("- few_assets_reason: `budget_limit`", report)
        self.assertIn("- actions_cache_hit: `true`", report)
        self.assertIn("- cache_reused_from_main: `true`", report)
        self.assertIn("- close_universe_source: `main_baseline`", report)
        self.assertIn("- skipped_provider_calls_due_to_cache: 3", report)
        self.assertIn("- skipped_provider_calls_due_to_rate_limit: 0", report)
        self.assertIn("- fmp_status: `ok`", report)

    def test_report_rate_limit_summary_is_clear(self) -> None:
        report = render_markdown_report(
            [],
            stock_regime="not_verified",
            crypto_regime="not_verified",
            report_type="close",
            data_mode="blocked",
            provider_budget={
                "estimated_calls": {"fmp": 16},
                "used_calls": {"fmp": 1},
                "cache_hits": 0,
                "cache_misses": 1,
                "universe_requested": 0,
                "universe_scanned": 0,
                "discovery_enabled": False,
                "skipped_due_to_api_budget": False,
                "provider_rate_limit_status": "rate_limited",
                "few_assets_reason": "provider_error",
                "actions_cache_hit": "false",
                "close_universe_source": "main_baseline",
                "cache_reused_from_main": False,
                "skipped_provider_calls_due_to_cache": 0,
                "skipped_provider_calls_due_to_rate_limit": 15,
                "fmp_status": "rate_limited",
            },
        )

        self.assertIn("FMP rate limit atingido; relatorio bloqueado ou degradado conforme cache/fallback disponivel.", report)
        self.assertIn("- provider_rate_limit_status: `rate_limited`", report)
        self.assertIn("- fmp_status: `rate_limited`", report)
        self.assertIn("- skipped_provider_calls_due_to_rate_limit: 15", report)
        self.assertNotIn("Decisao geral: `operate`", report)

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
        self.assertIn("technical_unvalidated is not approval to buy", text)
        self.assertIn("cannot approve a trade by itself", text)
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

    def test_main_outside_regular_session_is_diagnostic_not_decision_grade(self) -> None:
        report = render_markdown_report(
            [_decision("NVDA", decision="watch_buy", market_session="closed")],
            stock_regime="neutral",
            crypto_regime="neutral",
            report_type="main",
            data_mode="live",
        )
        analyst = render_analyst_review_input(
            [_decision("NVDA", decision="watch_buy", market_session="closed", investment_quality_score=95)],
            report_type="main",
            data_mode="live",
            stock_regime="neutral",
            crypto_regime="neutral",
        )

        self.assertIn("report_grade: `diagnostic_not_decision_grade`", report)
        self.assertIn("main report fora do horario regular; usar apenas como diagnostico", report)
        self.assertIn("Nenhum ativo em watchlist acionavel", report)
        self.assertIn("No equity candidates for qualitative review", analyst)
        self.assertIn("report_grade: `diagnostic_not_decision_grade`", analyst)
        self.assertIn("## Equity research queue", analyst)
        self.assertIn("pesquisa qualitativa, nao trade", analyst)
        self.assertIn("NVDA", analyst)

    def test_close_outside_regular_session_is_not_next_day_trigger(self) -> None:
        report = render_markdown_report(
            [_decision("NVDA", decision="watch_buy", market_session="closed")],
            stock_regime="neutral",
            crypto_regime="neutral",
            report_type="close",
            data_mode="live",
            generated_at="2026-06-22T20:00:00-03:00",
        )

        self.assertIn("report_grade: `close_diagnostic`", report)
        self.assertIn("Close report fora da janela valida ou sem sessao regular confirmada; usar apenas como diagnostico.", report)

    def test_close_after_market_close_on_trading_day_is_close_decision_grade(self) -> None:
        report = render_markdown_report(
            [_decision("NVDA", decision="watch_buy", market_session="closed")],
            stock_regime="neutral",
            crypto_regime="neutral",
            report_type="close",
            data_mode="live",
            generated_at="2026-06-22T17:15:00-03:00",
        )
        analyst = render_analyst_review_input(
            [_decision("NVDA", decision="watch_buy", market_session="closed", investment_quality_score=95)],
            report_type="close",
            data_mode="live",
            stock_regime="neutral",
            crypto_regime="neutral",
            generated_at="2026-06-22T17:15:00-03:00",
        )

        self.assertIn("report_grade: `close_decision_grade`", report)
        self.assertIn("Relatorio de fechamento valido para preparacao do proximo pregao. Nao e gatilho automatico de ordem.", report)
        self.assertIn("Sem ordem automatica, sem broker e sem compra automatica", report)
        self.assertIn("report_grade: `close_decision_grade`", analyst)
        self.assertIn("## Equity research queue", analyst)

    def test_close_before_market_close_is_close_diagnostic(self) -> None:
        report = render_markdown_report(
            [_decision("NVDA", decision="watch_buy", market_session="regular")],
            stock_regime="neutral",
            crypto_regime="neutral",
            report_type="close",
            data_mode="live",
            generated_at="2026-06-22T15:30:00-03:00",
        )

        self.assertIn("report_grade: `close_diagnostic`", report)
        self.assertIn("Close report fora da janela valida ou sem sessao regular confirmada; usar apenas como diagnostico.", report)

    def test_close_on_weekend_or_holiday_is_close_diagnostic(self) -> None:
        weekend_report = render_markdown_report(
            [_decision("NVDA", decision="watch_buy", market_session="closed")],
            stock_regime="neutral",
            crypto_regime="neutral",
            report_type="close",
            data_mode="live",
            generated_at="2026-06-21T17:15:00-03:00",
        )
        holiday_report = render_markdown_report(
            [_decision("NVDA", decision="watch_buy", market_session="closed")],
            stock_regime="neutral",
            crypto_regime="neutral",
            report_type="close",
            data_mode="live",
            generated_at="2026-06-19T17:15:00-03:00",
        )

        self.assertIn("report_grade: `close_diagnostic`", weekend_report)
        self.assertIn("report_grade: `close_diagnostic`", holiday_report)

    def test_asset_appears_in_only_one_final_bucket(self) -> None:
        report = render_markdown_report(
            [
                _decision("NVDA", decision="wait"),
                _decision("MSFT", decision="avoid", short_setup_score=89, short_status="watch_only"),
                _decision("HYPE", asset_type="crypto", decision="technical_unvalidated"),
            ],
            stock_regime="neutral",
            crypto_regime="neutral",
            report_type="main",
            data_mode="live",
        )

        bucket_sections = [
            "## Tradeable hoje",
            "## Watchlist apenas",
            "## Setup tecnico detectado, mas nao validado",
            "## Research queue",
            "## Wait",
            "## Rejected",
            "## Blocked",
            "## Short watchlist apenas",
        ]
        for symbol in ["NVDA", "MSFT", "HYPE"]:
            appearances = sum(symbol in _section(report, heading) for heading in bucket_sections)
            self.assertEqual(appearances, 1, f"{symbol} appears in {appearances} final buckets")

    def test_technical_unvalidated_with_high_missing_data_uses_conservative_thesis(self) -> None:
        report = render_markdown_report(
            [
                _decision(
                    "HYPE",
                    asset_type="crypto",
                    decision="technical_unvalidated",
                    missing_data_severity="high",
                    expected_value_r=-0.01,
                    limitations=["cvd_proxy_unavailable", "news_not_collected_confidence_limited"],
                )
            ],
            stock_regime="neutral",
            crypto_regime="neutral",
            report_type="main",
            data_mode="live",
        )

        self.assertIn("Setup tecnico detectado, mas dados incompletos/EV/fluxo/noticias nao validam entrada operacional.", report)
        self.assertNotIn("qualidade e setup alinhados", report)

    def test_report_uses_internal_drivers_until_real_news_macro_exists(self) -> None:
        report = render_markdown_report(
            [_decision("NVDA", decision="wait")],
            stock_regime="neutral",
            crypto_regime="neutral",
            report_type="main",
            data_mode="live",
            portfolio_alerts=["market_not_risk_on"],
        )

        self.assertIn("## Drivers internos do modelo", report)
        self.assertIn("Sem news/macro real coletado nesta V1", report)
        self.assertNotIn("## O que moveu o mercado", report)

def _section(markdown: str, heading: str) -> str:
    start = markdown.index(heading)
    next_start = markdown.find("\n## ", start + len(heading))
    return markdown[start : next_start if next_start != -1 else len(markdown)]


if __name__ == "__main__":
    unittest.main()
