from __future__ import annotations

import unittest

from advisor.analyst_review import generate_analyst_final_review


NIGHTLY_INPUT = """# Nightly qualitative review input

## Main summary

- report_type: `main`
- Data mode: `live`
- report_grade: `diagnostic_not_decision_grade`
- market_session: `regular,unknown`
- Decisao geral: `no_trade_day`
- WARNING: blocked_or_diagnostic

## Close summary

- report_type: `close`
- Data mode: `live`
- report_grade: `close_decision_grade`
- market_session: `after_hours`
- Decisao geral: `wait`

## Top equity candidates for qualitative review

No equity candidates for qualitative review

## Crypto review needed

- `HYPE`
  - bot_decision: `technical_unvalidated`
  - risks: cvd_proxy_unavailable, liquidations_unavailable, open_interest_change_unavailable, news_not_collected_confidence_limited
  - news_catalyst_status: `not_verified`

## AMD

- Ativo: `AMD`
- Tipo: `stock`
- decision_label: `technical_unvalidated`
- Decisao: `technical_unvalidated`
- reason_codes: data_incomplete_confidence_limited, earnings_data_missing, guidance_recent_not_collected, news_not_collected_confidence_limited, recent_gap_risk, sector_relative_strength_not_collected, valuation_extreme
- data_quality: `limited`
- missing_data_severity: `high`
- decision_confidence_score: 55
- Investment Quality Score: 70
- Swing Trade Score: 73
- expected_value_r: 0.16
- Tese: Setup tecnico detectado, mas dados incompletos/EV/fluxo/noticias nao validam entrada operacional.
- Metricas principais: RSI: 55.40; EMA 9: 521.42; EMA 21: 503.57; SMA 50: 439.06; Average volume: 37910643.00; Recent gap: 7.71%; Relative strength: 17.16%; PE: 169.34; PEG: 1.37; Revenue growth: 34.34%; EPS growth: 164.36%
- Event risk: unknown
- event_check_status: `not_verified`
- news_status: `not_verified`
- Data source: fmp
- provider: `fmp`

## INTC

- Ativo: `INTC`
- Tipo: `stock`
- decision_label: `avoid`
- Decisao: `avoid`
- reason_codes: negative_ev_with_high_data_severity, negative_or_invalid_pe, negative_or_invalid_peg, weak_setup_win_rate
- data_quality: `limited`
- missing_data_severity: `high`
- Investment Quality Score: 34
- expected_value_r: -0.13
- Metricas principais: PE: -208.98; PEG: -0.03; Revenue growth: -0.47%
- provider: `fmp`

## HYPE

- Ativo: `HYPE`
- Tipo: `crypto`
- decision_label: `technical_unvalidated`
- Decisao: `technical_unvalidated`
- reason_codes: coinbase_premium_unavailable, cvd_proxy_unavailable, liquidations_unavailable, open_interest_change_unavailable, news_not_collected_confidence_limited
- data_quality: `limited`
- missing_data_severity: `high`
- Investment Quality Score: 75
- expected_value_r: 1.69
- Metricas principais: RSI: 51.76; Market cap: 13795509248.00; Average volume: 809812260.00; Funding rate (8h normalized): 0.01%; Open interest change: n/a; CVD proxy: n/a; Coinbase premium: n/a; Liquidation imbalance: n/a
- news_status: `not_verified`
- provider: `hyperliquid`

## SOL

- Ativo: `SOL`
- Tipo: `crypto`
- decision_label: `blocked`
- Decisao: `blocked`
- reason_codes: binance_restricted_location, insufficient_price_history, price_history_unavailable
- data_quality: `blocked`
- missing_data_severity: `critical`
- Investment Quality Score: 0
- Metricas principais: price history: n/a
- provider: `unknown`
"""


class AnalystReviewSemanticsTests(unittest.TestCase):
    def test_main_diagnostic_keeps_no_trade_but_allows_watch_pending_checks(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT)

        self.assertIn("Public Equity Investing executed: false", review)
        self.assertIn("Public Equity Investing note: not executed automatically in this environment", review)
        self.assertIn("* no_trade", review)
        self.assertIn("AMD", review)
        self.assertIn("watch_pending_checks", review)
        self.assertNotIn("AMD: blocked", review)

    def test_technical_unvalidated_with_positive_signals_is_not_auto_blocked(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT)

        self.assertIn("AMD tem sinais tecnicos/fundamentais parciais", review)
        self.assertIn("watch_pending_checks", review)
        self.assertIn("news, earnings/guidance, valuation, risco de gap", review)
        self.assertNotIn("AMD segue technical_unvalidated/blocked", review)

    def test_negative_ev_or_bad_thesis_is_rejected_not_watch(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT)

        self.assertIn("INTC", review)
        self.assertIn("rejected", review)
        self.assertNotIn("INTC em watch_pending_checks", review)

    def test_crypto_missing_flow_is_crypto_research_only_not_tradeable(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT)

        self.assertIn("HYPE", review)
        self.assertIn("crypto_research_only", review)
        self.assertIn("flow/news not_verified", review)
        self.assertNotIn("HYPE: tradeable", review)

    def test_provider_blocked_or_missing_price_remains_blocked(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT)

        self.assertIn("SOL", review)
        self.assertIn("blocked", review)
        self.assertIn("provider/preco minimo indisponivel", review)

    def test_telegram_summary_separates_operational_decision_from_observation(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT)

        telegram = review.split("## Telegram summary", 1)[1]
        self.assertIn("Decisao operacional: no_trade", telegram)
        self.assertIn("Nenhum ativo aprovado para entrada", telegram)
        self.assertIn("AMD em watch_pending_checks", telegram)
        self.assertIn("HYPE em crypto_research_only", telegram)
        self.assertIn("main nao decision-grade", telegram)

    def test_duplicate_main_close_assets_are_listed_once(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT + "\n\n" + NIGHTLY_INPUT)

        telegram = review.split("## Telegram summary", 1)[1]
        self.assertEqual(telegram.count("AMD em watch_pending_checks"), 1)
        self.assertEqual(telegram.count("HYPE em crypto_research_only"), 1)

    def test_watch_and_research_labels_never_become_tradeable(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT)

        self.assertIn("Nenhum ativo aprovado como tradeable", review)
        self.assertNotIn("watch_pending_checks: tradeable", review)
        self.assertNotIn("research_only: tradeable", review)


if __name__ == "__main__":
    unittest.main()
