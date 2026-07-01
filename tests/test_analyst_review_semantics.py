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


CRYPTO_BASIC_WITH_BINANCE_RESTRICTED = """# Nightly qualitative review input

## Main summary

- report_type: `main`
- Data mode: `live`
- report_grade: `diagnostic_not_decision_grade`
- market_session: `regular,unknown`
- WARNING: blocked_or_diagnostic

## BTC

- Ativo: `BTC`
- Tipo: `crypto`
- decision_label: `blocked`
- Decisao: `blocked`
- reason_codes: binance_restricted_location, cvd_proxy_unavailable, liquidations_unavailable, open_interest_change_unavailable, coinbase_premium_unavailable, news_not_collected_confidence_limited
- data_quality: `limited`
- missing_data_severity: `high`
- Metricas principais: Last price: 61500.00; Market cap: 1200000000000.00; Average volume: 35000000000.00; Daily change: 1.20%; RSI: 52.00; Open interest change: n/a; CVD proxy: n/a; Coinbase premium: n/a; Liquidation imbalance: n/a
- news_status: `not_verified`
- Data source: coingecko
- provider: `coingecko`

## ETH

- Ativo: `ETH`
- Tipo: `crypto`
- decision_label: `blocked`
- Decisao: `blocked`
- reason_codes: binance_restricted_location, cvd_proxy_unavailable, liquidations_unavailable, open_interest_change_unavailable, news_not_collected_confidence_limited
- data_quality: `limited`
- missing_data_severity: `high`
- Metricas principais: Last price: 3400.00; Market cap: 410000000000.00; Average volume: 18000000000.00; Daily change: 0.80%; Open interest change: n/a; CVD proxy: n/a; Liquidation imbalance: n/a
- news_status: `not_verified`
- Data source: coinbase
- provider: `coinbase`

## SOL

- Ativo: `SOL`
- Tipo: `crypto`
- decision_label: `blocked`
- Decisao: `blocked`
- reason_codes: binance_restricted_location, cvd_proxy_unavailable, liquidations_unavailable, open_interest_change_unavailable, news_not_collected_confidence_limited
- data_quality: `limited`
- missing_data_severity: `high`
- Metricas principais: Last price: 142.00; Market cap: 65000000000.00; Average volume: 3000000000.00; Daily change: 2.10%; Open interest change: n/a; CVD proxy: n/a; Liquidation imbalance: n/a
- news_status: `not_verified`
- Data source: coingecko
- provider: `coingecko`

## DOGE

- Ativo: `DOGE`
- Tipo: `crypto`
- decision_label: `blocked`
- Decisao: `blocked`
- reason_codes: binance_restricted_location, price_history_unavailable, insufficient_price_history
- data_quality: `blocked`
- missing_data_severity: `critical`
- Metricas principais: price history: n/a
- provider: `unknown`
"""


FULL_UNIVERSE_INPUT = """# Nightly qualitative review input

## Main summary

- report_type: `main`
- Data mode: `live`
- report_grade: `diagnostic_not_decision_grade`
- market_session: `regular,unknown`
- WARNING: blocked_or_diagnostic

## INTC
- Ativo: `INTC`
- Tipo: `stock`
- decision_label: `avoid`
- Decisao: `avoid`
- expected_value_r: -0.10
- Metricas principais: RSI: 40.00; PE: -10.00

## AMD
- Ativo: `AMD`
- Tipo: `stock`
- decision_label: `technical_unvalidated`
- Decisao: `technical_unvalidated`
- Investment Quality Score: 70
- Metricas principais: RSI: 55.00; EMA 9: 10.00; EMA 21: 9.00; Average volume: 1000000.00; Revenue growth: 20.00%; EPS growth: 30.00%
- news_status: `not_verified`

## NVDA
- Ativo: `NVDA`
- Tipo: `stock`
- decision_label: `wait`
- Decisao: `wait`
- Metricas principais: RSI: 60.00; PE: 50.00

## HIMS
- Ativo: `HIMS`
- Tipo: `stock`
- decision_label: `wait`
- Decisao: `wait`
- Metricas principais: RSI: 48.00; PE: 80.00

## MU
- Ativo: `MU`
- Tipo: `stock`
- decision_label: `wait`
- Decisao: `wait`
- Metricas principais: RSI: 50.00; PE: 25.00

## MSFT
- Ativo: `MSFT`
- Tipo: `stock`
- decision_label: `wait`
- Decisao: `wait`
- Metricas principais: RSI: 53.00; PE: 35.00

## USAR
- Ativo: `USAR`
- Tipo: `stock`
- decision_label: `wait`
- Decisao: `wait`
- Metricas principais: RSI: 51.00; PE: 30.00

## CRDO
- Ativo: `CRDO`
- Tipo: `stock`
- decision_label: `wait`
- Decisao: `wait`
- Metricas principais: RSI: 52.00; PE: 45.00

## DELL
- Ativo: `DELL`
- Tipo: `stock`
- decision_label: `wait`
- Decisao: `wait`
- Metricas principais: RSI: 49.00; PE: 20.00

## MRVL
- Ativo: `MRVL`
- Tipo: `stock`
- decision_label: `wait`
- Decisao: `wait`
- Metricas principais: RSI: 47.00; PE: 40.00

## HOOD
- Ativo: `HOOD`
- Tipo: `stock`
- decision_label: `wait`
- Decisao: `wait`
- Metricas principais: RSI: 54.00; PE: 60.00

## BTC
- Ativo: `BTC`
- Tipo: `crypto`
- decision_label: `blocked`
- Decisao: `blocked`
- reason_codes: cvd_proxy_unavailable, liquidations_unavailable, open_interest_change_unavailable, coinbase_premium_unavailable, news_not_collected_confidence_limited
- data_quality: `limited`
- missing_data_severity: `high`
- Metricas principais: Last price: 61500.00; Market cap: 1200000000000.00; Average volume: 35000000000.00; Daily change: 1.20%; Open interest change: n/a; CVD proxy: n/a; Coinbase premium: n/a; Liquidation imbalance: n/a
- news_status: `not_verified`
- provider: `coingecko`

## ETH
- Ativo: `ETH`
- Tipo: `crypto`
- decision_label: `blocked`
- Decisao: `blocked`
- reason_codes: cvd_proxy_unavailable, liquidations_unavailable, open_interest_change_unavailable, coinbase_premium_unavailable, news_not_collected_confidence_limited
- data_quality: `limited`
- missing_data_severity: `high`
- Metricas principais: Last price: 3400.00; Market cap: 410000000000.00; Average volume: 18000000000.00; Daily change: 0.80%; Open interest change: n/a; CVD proxy: n/a; Coinbase premium: n/a; Liquidation imbalance: n/a
- news_status: `not_verified`
- provider: `coingecko`

## SOL
- Ativo: `SOL`
- Tipo: `crypto`
- decision_label: `blocked`
- Decisao: `blocked`
- reason_codes: cvd_proxy_unavailable, liquidations_unavailable, open_interest_change_unavailable, coinbase_premium_unavailable, news_not_collected_confidence_limited
- data_quality: `limited`
- missing_data_severity: `high`
- Metricas principais: Last price: 142.00; Market cap: 65000000000.00; Average volume: 3000000000.00; Daily change: 2.10%; Open interest change: n/a; CVD proxy: n/a; Coinbase premium: n/a; Liquidation imbalance: n/a
- news_status: `not_verified`
- provider: `coingecko`

## HYPE
- Ativo: `HYPE`
- Tipo: `crypto`
- decision_label: `technical_unvalidated`
- Decisao: `technical_unvalidated`
- reason_codes: cvd_proxy_unavailable, liquidations_unavailable, open_interest_change_unavailable, coinbase_premium_unavailable, news_not_collected_confidence_limited
- data_quality: `limited`
- missing_data_severity: `high`
- Metricas principais: Last price: 40.00; Market cap: 13795509248.00; Average volume: 809812260.00; Daily change: 0.50%; Open interest change: n/a; CVD proxy: n/a; Coinbase premium: n/a; Liquidation imbalance: n/a
- news_status: `not_verified`
- provider: `hyperliquid`
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

    def test_binance_restricted_does_not_block_major_crypto_with_basic_data(self) -> None:
        review = generate_analyst_final_review(CRYPTO_BASIC_WITH_BINANCE_RESTRICTED)

        for ticker in ("BTC", "ETH", "SOL"):
            self.assertIn(f"### {ticker}", review)
            self.assertIn("basic_data_status: live", review)
            self.assertIn("flow_data_status: not_verified", review)
            self.assertIn("binance_status: restricted", review)
        self.assertIn("BTC: crypto_watch_context", review)
        self.assertIn("ETH: crypto_watch_context", review)
        self.assertIn("SOL: crypto_watch_context", review)
        self.assertNotIn("BTC: blocked", review)
        self.assertNotIn("ETH: blocked", review)
        self.assertNotIn("SOL: blocked", review)

    def test_binance_restricted_keeps_flow_not_verified(self) -> None:
        review = generate_analyst_final_review(CRYPTO_BASIC_WITH_BINANCE_RESTRICTED)

        self.assertIn("flow_data_status: not_verified", review)
        self.assertIn("flow/derivatives nao verificados", review)

    def test_basic_data_absent_still_blocks_crypto(self) -> None:
        review = generate_analyst_final_review(CRYPTO_BASIC_WITH_BINANCE_RESTRICTED)

        self.assertIn("DOGE", review)
        self.assertIn("blocked", review)
        self.assertIn("basic_data_status: not_verified", review)

    def test_hype_with_basic_data_and_missing_flow_is_crypto_research_only(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT)

        self.assertIn("HYPE", review)
        self.assertIn("basic_data_status: live", review)
        self.assertIn("flow_data_status: not_verified", review)
        self.assertIn("crypto_research_only", review)

    def test_crypto_research_only_never_becomes_tradeable(self) -> None:
        review = generate_analyst_final_review(CRYPTO_BASIC_WITH_BINANCE_RESTRICTED)

        self.assertIn("Nenhum ativo aprovado como tradeable", review)
        self.assertNotIn("crypto_research_only: tradeable", review)
        self.assertNotIn("crypto_watch_context: tradeable", review)

    def test_telegram_summary_differentiates_basic_data_from_missing_flow(self) -> None:
        review = generate_analyst_final_review(CRYPTO_BASIC_WITH_BINANCE_RESTRICTED)

        telegram = review.split("## Telegram summary", 1)[1]
        self.assertIn("Cripto: BTC/ETH/SOL apenas contexto/research", telegram)
        self.assertIn("flow/derivatives nao verificados", telegram)

    def test_crypto_basic_status_can_be_cache_or_fallback(self) -> None:
        cached_input = CRYPTO_BASIC_WITH_BINANCE_RESTRICTED.replace(
            "- provider: `coingecko`",
            "- provider: `coingecko`\n- is_stale: `yes`\n- stale_reason: cache_age_exceeds_24h",
            1,
        )
        fallback_input = CRYPTO_BASIC_WITH_BINANCE_RESTRICTED.replace(
            "- Data source: coingecko\n- provider: `coingecko`",
            "- Data source: fallback\n- provider: `fallback`",
            1,
        )

        self.assertIn("basic_data_status: cache", generate_analyst_final_review(cached_input))
        self.assertIn("basic_data_status: fallback", generate_analyst_final_review(fallback_input))

    def test_crypto_flow_status_can_be_live_when_flow_fields_are_present(self) -> None:
        flow_input = CRYPTO_BASIC_WITH_BINANCE_RESTRICTED.replace(
            "reason_codes: binance_restricted_location, cvd_proxy_unavailable, liquidations_unavailable, open_interest_change_unavailable, coinbase_premium_unavailable, news_not_collected_confidence_limited",
            "reason_codes: flow_present",
            1,
        ).replace(
            "Open interest change: n/a; CVD proxy: n/a; Coinbase premium: n/a; Liquidation imbalance: n/a",
            "Open interest change: 4.20%; CVD proxy: 1.10; Coinbase premium: 0.30%; Liquidation imbalance: 0.80",
            1,
        ).replace(
            "- news_status: `not_verified`",
            "- news_status: `verified`",
            1,
        )

        review = generate_analyst_final_review(flow_input)

        self.assertIn("flow_data_status: live", review)

    def test_telegram_summary_lists_every_named_stock_and_crypto(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        telegram = review.split("## Telegram summary", 1)[1]

        for ticker in ("INTC", "AMD", "NVDA", "HIMS", "MU", "MSFT", "USAR", "CRDO", "DELL", "MRVL", "HOOD", "BTC", "ETH", "SOL", "HYPE"):
            self.assertIn(ticker, telegram)
        self.assertIn("Acoes:", telegram)
        self.assertIn("Cripto:", telegram)
        self.assertIn("no_trade", telegram)


if __name__ == "__main__":
    unittest.main()
