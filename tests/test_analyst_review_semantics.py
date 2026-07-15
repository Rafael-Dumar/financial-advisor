from __future__ import annotations

import unittest

from advisor.analyst_review import generate_analyst_final_review


NIGHTLY_INPUT = """# Nightly qualitative review input

## Main summary

- generated_at: `2026-06-22T12:00:00-03:00`
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

- generated_at: `2026-06-22T12:00:00-03:00`
- report_type: `main`
- Data mode: `live`
- report_grade: `diagnostic_not_decision_grade`
- market_session: `regular,unknown`
- WARNING: blocked_or_diagnostic
- data_freshness: `controlled_by_cache_freshness`
- fresh_price_count: 4
- stale_price_count: 0
- missing_price_count: 11
- provider_rate_limit_status: `ok`
- fmp_status: `ok`
- coingecko_status: `ok`
- reason_codes: `market_session_not_regular`
- possible_session_detection_bug: true

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
        self.assertIn("Esta e uma revisao baseada em regras locais", review)
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
        self.assertIn("Report data grade:", telegram)
        self.assertIn("Trade readiness: no_trade", telegram)
        self.assertIn("Top equities: AMD", telegram)
        self.assertIn("Top crypto: HYPE", telegram)
        self.assertIn("Melhor equity: AMD - watch_pending_checks", telegram)
        self.assertIn("Melhor crypto: HYPE - crypto_research_only", telegram)
        self.assertIn("Bloqueio para trade:", telegram)

    def test_duplicate_main_close_assets_are_listed_once(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT + "\n\n" + NIGHTLY_INPUT)

        telegram = review.split("## Telegram summary", 1)[1]
        self.assertEqual(telegram.count("AMD"), 2)
        self.assertEqual(telegram.count("HYPE"), 2)

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
        self.assertIn("ticker: BTC", review)
        self.assertIn("label: crypto_watch_context", review)
        self.assertIn("ticker: ETH", review)
        self.assertIn("ticker: SOL", review)
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
        self.assertIn("Melhor crypto: BTC/ETH/SOL - crypto_watch_context", telegram)
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

        self.assertIn("Decisao operacional: no_trade", telegram)
        self.assertIn("Report data grade:", telegram)
        self.assertIn("Trade readiness: no_trade", telegram)
        self.assertIn("Top equities: AMD", telegram)
        self.assertIn("Top crypto: HYPE, BTC, ETH, SOL", telegram)
        self.assertIn("Melhor equity: AMD - watch_pending_checks", telegram)
        self.assertIn("Melhor crypto: HYPE - crypto_research_only", telegram)
        self.assertIn("Bloqueio para trade: news/earnings/flow/crypto_flow_pending", telegram)
        self.assertNotIn("NVDA", telegram)
        self.assertNotIn("HIMS", telegram)
        self.assertNotIn("MU", telegram)
        self.assertNotIn("MSFT", telegram)
        self.assertNotIn("USAR", telegram)
        self.assertNotIn("CRDO", telegram)
        self.assertNotIn("DELL", telegram)
        self.assertNotIn("MRVL", telegram)
        self.assertNotIn("HOOD", telegram)

    def test_telegram_summary_explains_ranking_instead_of_only_listing_labels(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        telegram = review.split("## Telegram summary", 1)[1]

        self.assertIn("Report data grade:", telegram)
        self.assertIn("Market brief:", telegram)
        self.assertIn("Top equities:", telegram)
        self.assertIn("Top crypto:", telegram)
        self.assertIn("Proximo passo: aguardar proximo main decision-grade", telegram)
        self.assertNotIn("Status completo:", telegram)

    def test_final_review_starts_with_objective_reading(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)

        self.assertIn("## Leitura objetiva", review)
        objective = review.split("## Leitura objetiva", 1)[1].split("## Resumo do dia", 1)[0]
        self.assertIn("Ranking inicial:", objective)
        self.assertIn("Evitar/rejeitados:", objective)
        self.assertIn("Proximo passo pratico:", objective)

    def test_news_earnings_flow_not_verified_does_not_block_report_data_grade(self) -> None:
        clean_main = FULL_UNIVERSE_INPUT.replace(
            "- report_grade: `diagnostic_not_decision_grade`",
            "- report_grade: `decision_grade`",
        ).replace(
            "- market_session: `regular,unknown`",
            "- market_session: `regular`",
        ).replace(
            "- WARNING: blocked_or_diagnostic\n",
            "",
        ).replace(
            "- missing_price_count: 11",
            "- missing_price_count: 0",
        ).replace(
            "- reason_codes: `market_session_not_regular`",
            "- reason_codes: `news_not_collected_confidence_limited,earnings_data_missing,crypto_flow_missing`",
        )

        review = generate_analyst_final_review(clean_main)

        self.assertIn("- report_data_grade: `decision_grade`", review)
        self.assertIn("- trade_readiness: `no_trade`", review)
        self.assertIn("Bloqueio para trade: news/earnings/flow/crypto_flow_pending", review)

    def test_live_coverage_with_session_conflict_is_partial_not_blocked_data(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)

        self.assertIn("- report_data_grade: `partial_data`", review)
        self.assertIn("- trade_readiness: `no_trade`", review)

    def test_input_without_coverage_emits_nightly_input_incomplete(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT)
        incomplete = _section(review, "## Nightly input completeness")

        self.assertIn("- nightly_input_incomplete: true", incomplete)
        self.assertIn("- main_found: true", incomplete)
        self.assertIn("- close_found: true", incomplete)
        self.assertIn("- equities_count: 2", incomplete)
        self.assertIn("- crypto_count: 2", incomplete)
        self.assertIn("- coverage_count: not_present_in_input", incomplete)
        self.assertIn("coverage_universe_missing", incomplete)

    def test_market_brief_missing_when_proxy_data_is_missing(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        brief = _section(review, "## Market brief")

        self.assertIn("- market_brief_status: missing", brief)
        self.assertIn("- SPY/S&P proxy: missing", brief)
        self.assertIn("- QQQ/Nasdaq proxy: missing", brief)
        self.assertIn("- SMH/semi proxy: missing", brief)
        self.assertIn("- BTC: price=61500.00", brief)
        self.assertIn("- ETH: price=3400.00", brief)

    def test_coverage_universe_includes_all_configured_tickers(self) -> None:
        review = generate_analyst_final_review("## Main summary\n\n- Data mode: `live`\n")
        coverage = _section(review, "## Coverage universe")

        for ticker in ("INTC", "AMD", "NVDA", "HIMS", "MU", "MSFT", "USAR", "CRDO", "DELL", "MRVL", "HOOD", "SOL", "HYPE", "BTC", "ETH"):
            self.assertIn(f"| {ticker} |", coverage)
        self.assertIn("| AMD | stock | missing | missing | missing | missing | missing | not_present_in_input |", coverage)

    def test_top_equities_and_crypto_are_separate(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        equities_section = _section(review, "## Top equities to watch tomorrow")
        crypto_section = _section(review, "## Top crypto to watch tomorrow")

        self.assertIn("ticker: AMD", equities_section)
        self.assertNotIn("ticker: HYPE", equities_section)
        for ticker in ("HYPE", "BTC", "ETH", "SOL"):
            self.assertIn(f"ticker: {ticker}", crypto_section)
        self.assertLessEqual(equities_section.count("- ticker:"), 5)
        self.assertLessEqual(crypto_section.count("- ticker:"), 4)

    def test_report_never_says_bare_melhores_sinais_nenhum(self) -> None:
        review = generate_analyst_final_review("## Main summary\n\n- Data mode: `live`\n")

        self.assertNotIn("Melhores sinais: nenhum", review)
        self.assertIn("Ranking inicial: indisponivel porque nightly_input_incomplete=true.", review)

    def test_market_rising_with_coverage_assets_generates_watch_ranking(self) -> None:
        risk_on = FULL_UNIVERSE_INPUT + "\n\n- stock_regime: `risk_on`\n- crypto_regime: `risk_on`\n"
        review = generate_analyst_final_review(risk_on)
        equities_section = _section(review, "## Top equities to watch tomorrow")

        self.assertIn("ticker: AMD", equities_section)
        self.assertNotIn("Nenhuma equity prioritaria", equities_section)

    def test_report_with_main_diagnostic_still_generates_top_candidates_to_watch(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        top_section = _section(review, "## Top candidates to watch tomorrow")

        self.assertIn("ticker: AMD", top_section)
        self.assertIn("label: watch_pending_checks", top_section)
        self.assertIn("ticker: HYPE", top_section)
        self.assertIn("label: crypto_research_only", top_section)
        self.assertIn("Nenhum esta aprovado para entrada", review)

    def test_top_crypto_to_watch_includes_eligible_crypto_and_limits_to_four(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        crypto_section = _section(review, "## Top crypto to watch tomorrow")

        for ticker in ("HYPE", "BTC", "ETH", "SOL"):
            self.assertIn(f"ticker: {ticker}", crypto_section)
        self.assertIn("label: crypto_research_only", crypto_section)
        self.assertIn("label: crypto_watch_context", crypto_section)
        self.assertLessEqual(crypto_section.count("- ticker:"), 4)

    def test_crypto_watch_context_is_best_crypto_when_research_only_absent(self) -> None:
        no_hype = FULL_UNIVERSE_INPUT.replace("## HYPE\n- Ativo: `HYPE`", "## HYPE\n- ignored: `HYPE`")
        review = generate_analyst_final_review(no_hype)
        telegram = review.split("## Telegram summary", 1)[1]

        self.assertIn("Melhor crypto: BTC/ETH/SOL - crypto_watch_context", telegram)
        self.assertNotIn("Melhor crypto: nenhum", telegram)

    def test_best_crypto_is_none_only_without_eligible_crypto(self) -> None:
        equities_only = FULL_UNIVERSE_INPUT.split("## BTC", 1)[0]
        review = generate_analyst_final_review(equities_only)
        telegram = review.split("## Telegram summary", 1)[1]

        self.assertIn("Melhor crypto: nenhum", telegram)

    def test_top_candidates_has_at_most_five_assets(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        top_section = _section(review, "## Top candidates to watch tomorrow")

        self.assertLessEqual(top_section.count("- ticker:"), 5)

    def test_decision_grade_failure_shows_main_diagnostic_in_final_review(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        diagnostic = _section(review, "## Por que o main nao foi decision-grade")

        self.assertIn("- report_grade: `diagnostic_not_decision_grade`", diagnostic)
        self.assertNotIn("regular,unknown", diagnostic)
        self.assertIn("- market_session: `regular`", diagnostic)
        self.assertIn("- market_session_primary: `regular`", diagnostic)
        self.assertIn("- market_session_sources: `[regular, unknown]`", diagnostic)
        self.assertIn("- market_session_conflict: true", diagnostic)
        self.assertIn("- generated_at BRT: `2026-06-22T12:00:00-03:00`", diagnostic)
        self.assertIn("- generated_at UTC: `2026-06-22T15:00:00+00:00`", diagnostic)
        self.assertIn("- expected market window: `2026-06-22T10:30:00-03:00 to 2026-06-22T17:00:00-03:00`", diagnostic)
        self.assertIn("- data_mode: `live`", diagnostic)
        self.assertIn("- data_freshness: `controlled_by_cache_freshness`", diagnostic)
        self.assertIn("- fresh_price_count: 4", diagnostic)
        self.assertIn("- stale_price_count: 0", diagnostic)
        self.assertIn("- missing_price_count: 11", diagnostic)
        self.assertIn("- provider_rate_limit_status: `ok`", diagnostic)
        self.assertIn("- fmp_status: `ok`", diagnostic)
        self.assertIn("- coingecko_status: `ok`", diagnostic)
        self.assertIn("- reason_codes: `market_session_conflict`", diagnostic)
        self.assertNotIn("market_session_not_regular", diagnostic)
        self.assertIn("- possible_session_detection_bug: true", diagnostic)
        self.assertIn(
            "Main foi bloqueado porque a sessao veio conflitante: sources=[regular, unknown].",
            diagnostic,
        )

    def test_no_trade_does_not_prevent_watch_or_research_ranking(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)

        self.assertIn("* no_trade", review)
        self.assertIn("## Top candidates to watch tomorrow", review)
        self.assertIn("prioridade: high", review)

    def test_operational_safety_language_is_not_repeated_excessively(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)

        self.assertLessEqual(review.lower().count("sem broker"), 2)
        self.assertLessEqual(review.lower().count("sem ordem automatica"), 2)

    def test_public_equity_not_executed_is_explicitly_local_rules_review(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT, public_equity_executed=False)

        self.assertIn(
            "Esta e uma revisao baseada em regras locais, nao uma analise externa/plugin.",
            review,
        )

    def test_missing_main_diagnostic_fields_are_not_present_in_input(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT)
        diagnostic = _section(review, "## Por que o main nao foi decision-grade")

        self.assertIn("- data_freshness: `not_present_in_input`", diagnostic)
        self.assertIn("- fresh_price_count: not_present_in_input", diagnostic)
        self.assertIn("- provider_rate_limit_status: `not_present_in_input`", diagnostic)
        self.assertIn("- fmp_status: `not_present_in_input`", diagnostic)
        self.assertIn("- coingecko_status: `not_present_in_input`", diagnostic)

    def test_telegram_summary_mentions_session_conflict_when_present(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        telegram = review.split("## Telegram summary", 1)[1]

        self.assertIn("Erro de dados, se houver: nightly_input_incomplete,possible_session_detection_bug,market_session_conflict", telegram)

    def test_regular_session_with_not_regular_reason_marks_possible_session_bug(self) -> None:
        markdown = """# Nightly qualitative review input

## Main summary

- generated_at: `2026-06-22T12:00:00-03:00`
- report_type: `main`
- Data mode: `live`
- report_grade: `diagnostic_not_decision_grade`
- market_session: `regular`
- reason_codes: `market_session_not_regular`
- WARNING: blocked_or_diagnostic
"""

        review = generate_analyst_final_review(markdown)
        diagnostic = _section(review, "## Por que o main nao foi decision-grade")

        self.assertIn("- market_session_primary: `regular`", diagnostic)
        self.assertNotIn("reason_codes: `market_session_not_regular`", diagnostic)
        self.assertIn("- possible_session_detection_bug: true", diagnostic)

def _section(markdown: str, heading: str) -> str:
    start = markdown.index(heading)
    next_start = markdown.find("\n## ", start + len(heading))
    return markdown[start : next_start if next_start != -1 else len(markdown)]


if __name__ == "__main__":
    unittest.main()
