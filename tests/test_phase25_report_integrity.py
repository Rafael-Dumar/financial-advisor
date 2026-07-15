from __future__ import annotations

import unittest
from dataclasses import asdict

from advisor.cli import _assign_universe_origins
from advisor.config import AdvisorConfig
from advisor.models import AssetDecision, RiskPlan
from advisor.report import ReportGradeInputs, evaluate_report_grades, render_analyst_review_input, render_markdown_report


def _section(markdown: str, heading: str) -> str:
    start = markdown.index(heading)
    remainder = markdown[start + len(heading) :]
    end = remainder.find("\n## ")
    return remainder if end < 0 else remainder[:end]


def _decision(
    symbol: str,
    *,
    origin: str,
    session: str = "regular",
    decision: str = "wait",
    stale: bool = False,
    provider: str = "fmp",
) -> AssetDecision:
    return AssetDecision(
        symbol=symbol,
        asset_type="crypto" if symbol in {"BTC", "ETH", "SOL", "AVAX", "BNB", "LINK", "XRP"} else "stock",
        decision=decision,
        investment_quality_score=70,
        swing_trade_score=60,
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
        alerts=[],
        limitations=[],
        thesis="fixture",
        metrics_summary=["fixture"],
        ideal_entry=100,
        alternative_entry=None,
        hold_suggestion="swing",
        backtest_stats=None,
        sample_quality="medium",
        bucket=decision,
        market_session=session,
        last_price_timestamp="2026-07-14",
        provider=provider,
        is_stale=stale,
        universe_origin=origin,
    )


class Phase25ReportIntegrityTests(unittest.TestCase):
    def test_cli_assigns_origin_after_scoring_without_changing_decision_fields(self) -> None:
        config = AdvisorConfig.default()
        decisions = [
            _decision(config.stock_watchlist[0], origin="unknown", decision="technical_unvalidated"),
            _decision(config.discovery_crypto_candidates[0], origin="unknown", decision="blocked"),
            _decision("UNMAPPED", origin="unknown", decision="wait"),
        ]

        assigned = _assign_universe_origins(decisions, config)

        self.assertEqual(assigned[0].universe_origin, "primary_watchlist")
        self.assertEqual(assigned[1].universe_origin, "discovery")
        self.assertEqual(assigned[2].universe_origin, "unknown")
        for before, after in zip(decisions, assigned, strict=True):
            before_fields = asdict(before)
            after_fields = asdict(after)
            before_fields.pop("universe_origin")
            after_fields.pop("universe_origin")
            self.assertEqual(before_fields, after_fields)

    def test_primary_regular_and_discovery_unknown_is_primary_decision_grade(self) -> None:
        decisions = [
            *[_decision(f"P{i}", origin="primary_watchlist") for i in range(15)],
            *[
                _decision(symbol, origin="discovery", session="unknown", decision="blocked", provider="unknown")
                for symbol in ("AVAX", "BNB", "LINK", "XRP")
            ],
        ]

        result = evaluate_report_grades(
            ReportGradeInputs(
                report_type="main",
                data_mode="live",
                generated_at="2026-07-14T15:46:49+00:00",
                decisions=decisions,
                enforce_regular_window=True,
            )
        )

        self.assertEqual(result.primary_report_grade, "decision_grade")
        self.assertEqual(result.overall_report_grade, "diagnostic_not_decision_grade")
        self.assertEqual(result.primary_market_session.primary, "regular")
        self.assertEqual(result.discovery_market_sessions.sources, ["unknown"])
        self.assertEqual(result.discovery_coverage_grade, "degraded")
        self.assertIn("discovery_coverage_degraded", result.overall_data_warnings)
        self.assertEqual(result.blocking_reasons, [])

    def test_primary_unknown_remains_diagnostic(self) -> None:
        decisions = [
            _decision("AMD", origin="primary_watchlist"),
            _decision("NVDA", origin="primary_watchlist", session="unknown"),
        ]

        result = evaluate_report_grades(
            ReportGradeInputs("main", "live", "2026-07-14T15:46:49+00:00", decisions)
        )

        self.assertEqual(result.primary_report_grade, "diagnostic_not_decision_grade")
        self.assertIn("invalid_market_session", result.blocking_reasons)

    def test_required_benchmark_unknown_blocks_primary_grade(self) -> None:
        result = evaluate_report_grades(
            ReportGradeInputs(
                "main",
                "live",
                "2026-07-14T15:46:49+00:00",
                [_decision("AMD", origin="primary_watchlist")],
                required_benchmark_sessions=("unknown",),
            )
        )

        self.assertEqual(result.primary_report_grade, "diagnostic_not_decision_grade")
        self.assertIn("required_benchmark_invalid", result.blocking_reasons)

    def test_primary_stale_blocks_but_discovery_stale_does_not(self) -> None:
        primary_stale = evaluate_report_grades(
            ReportGradeInputs(
                "main",
                "live",
                "2026-07-14T15:46:49+00:00",
                [_decision("AMD", origin="primary_watchlist", stale=True)],
            )
        )
        discovery_stale = evaluate_report_grades(
            ReportGradeInputs(
                "main",
                "live",
                "2026-07-14T15:46:49+00:00",
                [
                    _decision("AMD", origin="primary_watchlist"),
                    _decision("AVAX", origin="discovery", session="unknown", decision="blocked", stale=True),
                ],
            )
        )

        self.assertEqual(primary_stale.primary_report_grade, "diagnostic_not_decision_grade")
        self.assertIn("stale_primary_data", primary_stale.blocking_reasons)
        self.assertEqual(discovery_stale.primary_report_grade, "decision_grade")
        self.assertEqual(discovery_stale.discovery_coverage_grade, "degraded")

    def test_execution_outside_regular_window_is_diagnostic_when_enforced(self) -> None:
        result = evaluate_report_grades(
            ReportGradeInputs(
                "main",
                "live",
                "2026-07-14T22:00:00+00:00",
                [_decision("AMD", origin="primary_watchlist")],
                enforce_regular_window=True,
            )
        )

        self.assertEqual(result.primary_report_grade, "diagnostic_not_decision_grade")
        self.assertIn("outside_regular_market_window", result.blocking_reasons)

    def test_report_and_analyst_input_expose_origin_and_split_grades(self) -> None:
        decisions = [
            _decision("AMD", origin="primary_watchlist", decision="technical_unvalidated"),
            _decision("AVAX", origin="discovery", session="unknown", decision="blocked", provider="unknown"),
        ]

        report = render_markdown_report(
            decisions,
            stock_regime="neutral",
            crypto_regime="neutral",
            data_mode="live",
            generated_at="2026-07-14T15:46:49+00:00",
        )
        analyst_input = render_analyst_review_input(
            decisions,
            report_type="main",
            data_mode="live",
            stock_regime="neutral",
            crypto_regime="neutral",
            generated_at="2026-07-14T15:46:49+00:00",
        )

        for text in (report, analyst_input):
            self.assertIn("- primary_report_grade: `decision_grade`", text)
            self.assertIn("- overall_report_grade: `diagnostic_not_decision_grade`", text)
            self.assertIn("- primary_market_session: `regular`", text)
            self.assertIn("- discovery_market_sessions: `[unknown]`", text)
            self.assertIn("- discovery_coverage_grade: `degraded`", text)
            self.assertIn("- stale_asset_count_primary: 0", text)
            self.assertIn("- universe_origin: `primary_watchlist`", text)
            self.assertIn("- universe_origin: `discovery`", text)
        self.assertIn("## Discovery coverage", report)
        self.assertIn("impact_on_primary_report=false", report)

    def test_discovery_tradeable_never_changes_primary_operational_sections(self) -> None:
        decisions = [
            _decision("AMD", origin="primary_watchlist", decision="wait"),
            _decision("AVAX", origin="discovery", decision="tradeable"),
        ]

        report = render_markdown_report(
            decisions,
            stock_regime="neutral",
            crypto_regime="neutral",
            data_mode="live",
            generated_at="2026-07-14T15:46:49+00:00",
        )
        analyst_input = render_analyst_review_input(
            decisions,
            report_type="main",
            data_mode="live",
            stock_regime="neutral",
            crypto_regime="neutral",
            generated_at="2026-07-14T15:46:49+00:00",
        )

        self.assertIn("- Decisao geral: `no_trade_day`", report)
        self.assertNotIn("AVAX", _section(report, "## Tradeable hoje"))
        self.assertNotIn("AVAX", _section(report, "## Ranking de oportunidades"))
        self.assertIn("- bot_general_decision: `no_trade_day`", analyst_input)
        self.assertNotIn("`AVAX`", _section(analyst_input, "## Crypto review needed"))

    def test_primary_tradeable_remains_operational_when_overall_is_diagnostic_from_discovery(self) -> None:
        decisions = [
            _decision("AMD", origin="primary_watchlist", decision="tradeable"),
            _decision("AVAX", origin="discovery", session="unknown", decision="blocked", provider="unknown"),
        ]

        report = render_markdown_report(
            decisions,
            stock_regime="risk_on",
            crypto_regime="neutral",
            data_mode="live",
            generated_at="2026-07-14T15:46:49+00:00",
        )

        self.assertIn("- primary_report_grade: `decision_grade`", report)
        self.assertIn("- overall_report_grade: `diagnostic_not_decision_grade`", report)
        self.assertIn("- discovery_coverage_grade: `degraded`", report)
        self.assertIn("- Decisao geral: `operate`", report)
        self.assertIn("`AMD`", _section(report, "## Tradeable hoje"))
        self.assertNotIn("AVAX", _section(report, "## Ranking de oportunidades"))

    def test_stale_discovery_same_session_is_warning_not_session_conflict(self) -> None:
        report = render_markdown_report(
            [
                _decision("AMD", origin="primary_watchlist"),
                _decision("AVAX", origin="discovery", stale=True, decision="blocked"),
            ],
            stock_regime="neutral",
            crypto_regime="neutral",
            data_mode="live",
            generated_at="2026-07-14T15:46:49+00:00",
        )

        self.assertIn("- primary_report_grade: `decision_grade`", report)
        self.assertIn("- discovery_coverage_grade: `degraded`", report)
        self.assertIn("- session_conflict_warning: false", report)


if __name__ == "__main__":
    unittest.main()
