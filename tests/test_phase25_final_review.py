from __future__ import annotations

import unittest
from unittest.mock import patch

from advisor.analyst_review import MainReviewContext, generate_analyst_final_review, main_blocks_operation, parse_review_package


def _nightly(*, primary_grade: str = "decision_grade", artifact_valid: bool = True) -> str:
    return f"""# Nightly qualitative review input

- brt_date: `2026-07-14`
- main_run_id: `10`
- close_run_id: `20`
- main_head_sha: `8497c24`
- close_head_sha: `8497c24`
- main_event: `schedule`
- close_event: `schedule`
- main_generated_at: `2026-07-14T15:46:49Z`
- close_generated_at: `2026-07-14T21:13:56Z`
- artifact_selection_status: `valid_current_day`
- artifact_valid: `{str(artifact_valid).lower()}`

## Main summary

- report_type: `main`
- Data mode: `live`
- primary_report_grade: `{primary_grade}`
- overall_report_grade: `diagnostic_not_decision_grade`
- primary_market_session: `regular`
- discovery_coverage_grade: `degraded`
- stale_asset_count_primary: 0
- provider_rate_limit_status: `ok`
- blocking_reasons: `nenhum`

## Close summary

- report_type: `close`
- primary_report_grade: `close_decision_grade`
"""


def _asset(
    ticker: str,
    decision: str,
    *,
    origin: str = "primary_watchlist",
    report_type: str = "main",
    extra: str = "",
) -> str:
    session = "regular" if report_type == "main" else "after_hours"
    return f"""# Investment and Swing Trade Advisor

- report_type: `{report_type}`

## {ticker}

- Ativo: `{ticker}`
- Tipo: `stock`
- universe_origin: `{origin}`
- decision_label: `{decision}`
- Decisao: `{decision}`
- data_quality: `ok`
- missing_data_severity: `low`
- Investment Quality Score: 90
- Swing Trade Score: 90
- market_session: `{session}`
- is_stale: `no`
- provider: `fmp`
- event_check_status: `not_implemented`
- news_status: `collected`
- guidance_status: `not_implemented`
- Entrada ideal: 100.00
- Stop/invalidation: 95.00
- Tamanho maximo da posicao: 10 unidades / 1000.00
- Dados ausentes ou limitacoes: {extra or 'nenhum'}
"""


class Phase25FinalReviewTests(unittest.TestCase):
    def test_main_context_blocks_from_structured_fields_only(self) -> None:
        context = MainReviewContext(
            run_id="10",
            head_sha="8497c24",
            brt_date="2026-07-14",
            generated_at="2026-07-14T15:46:49Z",
            data_mode="live",
            primary_report_grade="decision_grade",
            overall_report_grade="diagnostic_not_decision_grade",
            primary_market_session="regular",
            discovery_coverage_grade="degraded",
            stale_asset_count_primary=0,
            provider_status="ok",
            artifact_valid=True,
            blocking_reasons=(),
        )

        self.assertFalse(main_blocks_operation(context))
        self.assertFalse(main_blocks_operation(context, close_markdown="blocked_or_diagnostic market_session: `closed`"))

    def test_substring_in_thesis_or_close_does_not_block_main(self) -> None:
        main = _asset("AMD", "wait", extra="thesis mentions blocked_or_diagnostic and not_collected")
        close = _asset("AMD", "avoid", report_type="close", extra="market_session: `closed`")

        review = generate_analyst_final_review(_nightly(), extra_markdowns=[main, close])

        self.assertIn("- main_primary_blocked: false", review)
        self.assertNotIn("main_primary_not_decision_grade", review)

    def test_main_and_close_are_not_last_wins(self) -> None:
        package = parse_review_package(
            _nightly(),
            extra_markdowns=[_asset("AMD", "technical_unvalidated"), _asset("AMD", "avoid", report_type="close")],
        )

        self.assertEqual(package.main_assets[0].source_decision, "technical_unvalidated")
        self.assertEqual(package.close_assets[0].source_decision, "avoid")
        self.assertIsNotNone(package.close_context)
        self.assertEqual(package.close_context.run_id, "20")
        review = generate_analyst_final_review(
            _nightly(),
            extra_markdowns=[_asset("AMD", "technical_unvalidated"), _asset("AMD", "avoid", report_type="close")],
        )
        self.assertIn("main_decision: `technical_unvalidated`", review)
        self.assertIn("close_decision: `avoid`", review)
        self.assertIn("decision_change: `changed_at_close`", review)
        self.assertIn("change_reason: `source_decision_changed_in_close`", review)

    def test_source_decision_is_preserved_and_not_implemented_is_field_specific(self) -> None:
        review = generate_analyst_final_review(_nightly(), extra_markdowns=[_asset("AMD", "wait")])

        self.assertIn("source_decision: `wait`", review)
        self.assertIn("review_status: `wait_from_main`", review)
        self.assertNotIn("source_decision: `research_only`", review)
        self.assertIn("legacy_label:", review)

    def test_tradeable_from_valid_main_reaches_final_review(self) -> None:
        review = generate_analyst_final_review(_nightly(), extra_markdowns=[_asset("AMD", "tradeable")])

        self.assertIn("* tradeable", review)
        self.assertIn("- tradeable_count: 1", review)
        self.assertIn("- tradeable_assets: `AMD`", review)
        self.assertIn("review_status: `tradeable_confirmed_from_main`", review)
        self.assertIn("decisao originada no scoring do main", review)
        self.assertIn("entry_from_main: `100.00`", review)
        self.assertIn("stop_invalidation_from_main: `95.00`", review)
        self.assertIn("sizing_from_main: `10 unidades / 1000.00`", review)
        self.assertNotIn("Nenhum ativo aprovado como tradeable", review)
        self.assertNotIn("Nao ha entrada aprovada", review)
        self.assertNotIn("Proximo passo: aguardar proximo main decision-grade", review)
        self.assertIn("Bloqueio para trade: nenhum", review)

    def test_overall_diagnostic_from_discovery_does_not_override_primary_tradeable(self) -> None:
        primary = _asset("AMD", "tradeable")
        discovery = _asset("AVAX", "blocked", origin="discovery", extra="empty_provider_response")

        review = generate_analyst_final_review(_nightly(), extra_markdowns=[primary + "\n" + discovery])

        self.assertIn("- primary_report_grade: `decision_grade`", review)
        self.assertIn("- overall_report_grade: `diagnostic_not_decision_grade`", review)
        self.assertIn("- discovery_coverage_grade: `degraded`", review)
        self.assertIn("* tradeable", review)
        self.assertIn("review_status: `tradeable_confirmed_from_main`", review)
        self.assertNotIn("Observacao sem entrada", review)

    def test_zero_tradeable_message_is_calculated(self) -> None:
        review = generate_analyst_final_review(_nightly(), extra_markdowns=[_asset("AMD", "wait")])

        self.assertIn("- tradeable_count: 0", review)
        self.assertIn("Nenhum tradeable no main selecionado", review)

    def test_missing_legacy_provenance_fails_closed_even_with_tradeable_text(self) -> None:
        legacy = """# Nightly qualitative review input

## Main summary
- Data mode: `live`
- report_grade: `decision_grade`
- market_session: `regular`
- provider_rate_limit_status: `ok`
"""
        review = generate_analyst_final_review(legacy, extra_markdowns=[_asset("AMD", "tradeable")])

        self.assertIn("- artifact_valid: false", review)
        self.assertIn("artifact_mismatch", review)
        self.assertIn("* no_trade", review)
        self.assertNotIn("review_status: `tradeable_confirmed_from_main`", review)

    def test_current_summary_without_raw_main_fails_closed(self) -> None:
        review = generate_analyst_final_review(_nightly())

        self.assertIn("- artifact_valid: false", review)
        self.assertIn("artifact_mismatch", review)
        self.assertIn("* no_trade", review)

    def test_discovery_is_separate_and_does_not_change_primary_decision(self) -> None:
        primary = _asset("AMD", "technical_unvalidated")
        discovery = _asset("AVAX", "blocked", origin="discovery", extra="empty_provider_response")

        review = generate_analyst_final_review(_nightly(), extra_markdowns=[primary + "\n" + discovery])

        self.assertIn("## Discovery coverage", review)
        self.assertIn("AVAX", review)
        self.assertIn("impact_on_primary_report: false", review)
        self.assertIn("collection_status: `empty_provider_response`", review)
        self.assertIn("provider: `fmp`", review)
        self.assertIn("- main_primary_blocked: false", review)

    def test_human_summary_answers_required_counts_and_close_change(self) -> None:
        review = generate_analyst_final_review(
            _nightly(),
            extra_markdowns=[
                _asset("AMD", "technical_unvalidated") + "\n" + _asset("AVAX", "blocked", origin="discovery"),
                _asset("AMD", "wait", report_type="close"),
            ],
        )

        self.assertIn("1. O main principal foi decision-grade? sim", review)
        self.assertIn("5. Quantos ativos eram technical_unvalidated? 1.", review)
        self.assertIn("9. Quais problemas estavam apenas no discovery? AVAX:blocked", review)
        self.assertIn("10. O que mudou no close? AMD.", review)

    def test_public_equity_false_is_honest_rule_based_review(self) -> None:
        review = generate_analyst_final_review(_nightly(), extra_markdowns=[_asset("AMD", "wait")])

        self.assertIn("# Rule-Based Final Review", review)
        self.assertIn("public_equity_executed: false", review)
        self.assertIn("nenhuma validacao externa/plugin foi executada", review)

    @patch("advisor.telegram_notify.notify_from_report")
    @patch("advisor.scoring.classify_asset")
    @patch("advisor.scoring.score_asset")
    def test_final_review_does_not_rescore_send_telegram_or_call_broker(
        self,
        score_asset_mock,
        classify_asset_mock,
        telegram_mock,
    ) -> None:
        review = generate_analyst_final_review(_nightly(), extra_markdowns=[_asset("AMD", "wait")])

        self.assertIn("source_decision: `wait`", review)
        score_asset_mock.assert_not_called()
        classify_asset_mock.assert_not_called()
        telegram_mock.assert_not_called()
        self.assertNotIn("broker_call", review)


if __name__ == "__main__":
    unittest.main()
