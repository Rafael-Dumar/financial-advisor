from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from advisor.analyst_review import generate_analyst_final_review
from advisor.telegram_notify import build_analyst_final_telegram_message, extract_telegram_summary


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


def _telegram_text(review: str) -> str:
    return extract_telegram_summary(review)


def _source_asset(
    ticker: str,
    source_decision: str,
    *,
    asset_type: str = "stock",
    entry: str = "",
    stop: str = "",
    sizing: str = "",
    blocking_reasons: str = "",
    reason_codes: str = "",
    metrics: str = "Last price: 100.00; Daily change: 1.00%; Relative strength: 5.00%; Average volume: 1234567.00",
    data_quality: str = "limited",
    missing_data_severity: str = "low",
) -> str:
    provider = "coingecko" if asset_type == "crypto" else "fmp"
    lines = [
        f"## {ticker}",
        f"- Ativo: {ticker}",
        f"- Tipo: {asset_type}",
        "- universe_origin: primary_watchlist",
        f"- decision_label: {source_decision}",
        f"- Decisao: {source_decision}",
        f"- blocking_reasons: {blocking_reasons or 'nenhum'}",
        f"- reason_codes: {reason_codes or 'observacao_pendente'}",
        f"- data_quality: {data_quality}",
        f"- missing_data_severity: {missing_data_severity}",
        "- Investment Quality Score: 70",
        f"- Metricas principais: {metrics}",
        "- news_status: not_verified",
        f"- provider: {provider}",
    ]
    for label, value in (
        ("Entrada ideal", entry),
        ("Stop/invalidation", stop),
        ("Tamanho maximo da posicao", sizing),
    ):
        if value:
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def _authoritative_main_review(*assets: str) -> str:
    nightly = """# Nightly qualitative review input

## Main summary

- main: run_id=424242
- main_head_sha: abcdef123456
- brt_date: 2026-10-06
- generated_at: 2026-10-06T21:00:00-03:00
- report_type: main
- Data mode: live
- report_grade: decision_grade
- market_session: regular
- market_session_conflict: false
- artifact_valid: true
- blocking_reasons: nenhum
- provider_rate_limit_status: ok
- stale_asset_count_primary: 0
- fresh_price_count: 4
- missing_price_count: 0
"""
    main = """# Investment and Swing Trade Advisor

- report_type: main
- generated_at: 2026-10-06T21:00:00-03:00
- Data mode: live
- report_grade: decision_grade
- market_session: regular
- market_session_conflict: false

""" + "\n\n".join(assets)
    return generate_analyst_final_review(nightly, extra_markdowns=[main])


def _market_context_v1(*, missing_smh: bool = False) -> dict[str, dict[str, object]]:
    context: dict[str, dict[str, object]] = {}
    for symbol, price, change, source, currency in (
        ("SPY", 500.0, 1.0, "fmp", "USD"),
        ("QQQ", 600.0, 1.1, "fmp", "USD"),
        ("SMH", 300.0, 1.2, "fmp", "USD"),
        ("BTC", 61500.0, 1.2, "binance", "USDT"),
        ("ETH", 3400.0, 0.8, "coingecko", "USD"),
    ):
        if symbol == "SMH" and missing_smh:
            context[symbol] = {
                "status": "unavailable",
                "source": None,
                "observed_at": None,
                "price_currency": currency,
                "daily_change_pct_unit": "percent",
                "granularity": "daily",
                "time_basis": "eod_candle",
            }
            continue
        context[symbol] = {
            "status": "available",
            "source": source,
            "observed_at": "2026-10-09T20:00:00Z",
            "price_currency": currency,
            "daily_change_pct_unit": "percent",
            "granularity": "daily",
            "time_basis": "eod_candle",
            "price": price,
            "daily_change_pct": change,
        }
    context["stock_regime"] = {
        "status": "available",
        "source": "advisor.scan_engine",
        "observed_at": None,
        "label": "neutral",
    }
    context["crypto_regime"] = {
        "status": "available",
        "source": "advisor.scan_engine",
        "observed_at": None,
        "label": "risk_on",
    }
    return context


def _market_context_section(context: dict[str, dict[str, object]]) -> str:
    serialized = json.dumps(context, separators=(",", ":"), allow_nan=False)
    return "\n".join(
        [
            "## Market context observations v1",
            "",
            "- market_context_version: `1`",
            f"- market_context_v1: `{serialized}`",
        ]
    )


def _market_context_main(context: dict[str, dict[str, object]]) -> str:
    return "\n\n".join(
        [
            "# Investment and Swing Trade Advisor\n\n- report_type: `main`\n- generated_at: `2026-10-10T10:00:00-03:00`\n- Data mode: `live`\n- report_grade: `decision_grade`\n- primary_report_grade: `decision_grade`\n- overall_report_grade: `decision_grade`\n- market_session: `regular`\n- market_session_conflict: false",
            _market_context_section(context),
            _source_asset("NVDA", "wait"),
        ]
    )


def _market_context_nightly(context: dict[str, dict[str, object]] | None = None) -> str:
    sections = [
        "# Nightly qualitative review input\n\n## Main summary\n\n- main: run_id=424242\n- main_head_sha: abcdef123456\n- brt_date: 2026-10-10\n- generated_at: 2026-10-10T10:00:00-03:00\n- report_type: main\n- Data mode: live\n- report_grade: decision_grade\n- primary_report_grade: decision_grade\n- overall_report_grade: decision_grade\n- market_session: regular\n- market_session_conflict: false\n- artifact_valid: true\n- blocking_reasons: nenhum\n- provider_rate_limit_status: ok\n\n## Close summary\n\n- report_type: close\n- Data mode: live\n- report_grade: close_decision_grade",
    ]
    if context is not None:
        sections.append(_market_context_section(context))
    return "\n\n".join(sections)


def _session_gate_review(conflict: str | None, *, blocking_reason: str = "nenhum") -> str:
    conflict_line = f"- market_session_conflict: {conflict}\n" if conflict is not None else ""
    nightly = (
        "# Nightly qualitative review input\n\n## Main summary\n\n"
        "- main: run_id=424242\n- main_head_sha: abcdef123456\n- brt_date: 2026-10-10\n"
        "- generated_at: 2026-10-10T10:00:00-03:00\n- report_type: main\n- Data mode: live\n"
        "- report_grade: decision_grade\n- primary_report_grade: decision_grade\n"
        "- overall_report_grade: decision_grade\n- market_session: regular\n"
        "- fresh_price_count: 15\n- missing_price_count: 0\n"
        + conflict_line
        + f"- artifact_valid: true\n- blocking_reasons: {blocking_reason}\n- provider_rate_limit_status: ok\n\n"
        "## Close summary\n\n- report_type: close\n- Data mode: live\n- report_grade: close_decision_grade"
    )
    main = (
        "# Investment and Swing Trade Advisor\n\n- report_type: `main`\n- generated_at: `2026-10-10T10:00:00-03:00`\n"
        "- Data mode: `live`\n- report_grade: `decision_grade`\n- primary_report_grade: `decision_grade`\n"
        "- overall_report_grade: `decision_grade`\n- market_session: `regular`\n"
        + conflict_line
        + "\n"
        + _source_asset("NVDA", "wait")
    )
    return generate_analyst_final_review(nightly, extra_markdowns=[main])


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
        self.assertIn("source_decision=technical_unvalidated permanece a decisao do main", review)
        self.assertNotIn("news, earnings/guidance, valuation, risco de gap", review)
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
        self.assertIn("flow_data_status: not_verified", review)
        self.assertIn("ausentes neste artifact; ausencia nao e observacao adversa", review)
        self.assertNotIn("flow/news not_verified impedem trade", review)
        self.assertNotIn("HYPE: tradeable", review)

    def test_provider_blocked_or_missing_price_remains_blocked(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT)

        self.assertIn("SOL", review)
        self.assertIn("blocked", review)
        self.assertIn("provider/preco minimo indisponivel", review)

    def test_telegram_summary_separates_operational_decision_from_observation(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT)

        telegram = _telegram_text(review).lower()
        self.assertIn("nao ha entrada aprovada agora", telegram)
        self.assertIn("contexto de mercado: unavailable", telegram)
        self.assertIn("amd", telegram)
        self.assertIn("hype", telegram)
        self.assertIn("prioridades de observacao", telegram)
        self.assertIn("rejeitados/bloqueados", telegram)
        self.assertNotIn("melhor equity", telegram)
        self.assertNotIn("melhor crypto", telegram)

    def test_v2_no_trade_brief_explains_decision_market_candidates_and_call(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        telegram = build_analyst_final_telegram_message(_telegram_text(review))
        lowered = telegram.lower()

        self.assertRegex(lowered, r"nao ha entrada aprovada|nenhuma entrada esta aprovada")
        self.assertIn("motivo principal", lowered)
        self.assertIn("contexto de mercado", lowered)
        self.assertIn("amd", lowered)
        self.assertIn("tese", lowered)
        self.assertIn("confirma", lowered)
        self.assertRegex(lowered, r"risco|invalidacao|verificacoes pendentes")
        self.assertIn("chamada pratica", lowered)
        self.assertIn("execucao manual", lowered)

    def test_v2_partial_market_context_keeps_crypto_facts_and_names_missing_proxies(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        telegram = _telegram_text(review).lower()

        self.assertIn("contexto de mercado: partial", telegram)
        self.assertIn("61500.00", telegram)
        self.assertIn("1.20%", telegram)
        self.assertIn("3400.00", telegram)
        self.assertIn("0.80%", telegram)
        for proxy in ("spy", "qqq", "smh"):
            self.assertIn(proxy, telegram)
        self.assertIn("ausentes", telegram)
        self.assertIn("partial_data", review)
        self.assertIn("trade_readiness", review)
        self.assertIn("* no_trade", review)

    def test_v2_crypto_summary_separates_basic_data_and_unverified_flow(self) -> None:
        review = generate_analyst_final_review(CRYPTO_BASIC_WITH_BINANCE_RESTRICTED)
        telegram = _telegram_text(review).lower()

        self.assertIn("btc", telegram)
        self.assertIn("dados basicos disponiveis", telegram)
        self.assertIn("fluxo/derivativos nao verificados", telegram)
        self.assertNotIn("trade candidate confirmado pelo main: btc", telegram)

    def test_v2_confirmed_tradeable_displays_exact_main_terms_and_missing_terms(self) -> None:
        entry = "MAIN_ENTRY_RIVER_147.26"
        stop = "MAIN_STOP_CLIFF_138.04"
        sizing = "MAIN_SIZE_2.35_PERCENT_NAV"
        complete = _authoritative_main_review(
            _source_asset("TST", "tradeable", entry=entry, stop=stop, sizing=sizing)
        )
        complete_telegram = _telegram_text(complete)

        self.assertIn("trade candidate confirmado pelo main: tst", complete_telegram.lower())
        for term in (entry, stop, sizing):
            self.assertIn(term, complete_telegram)
        self.assertNotIn("confidence", complete_telegram.lower())
        self.assertNotIn("confianca", complete_telegram.lower())

        missing = _authoritative_main_review(
            _source_asset("MISS", "tradeable", entry="MAIN_ENTRY_ONLY_201.37", sizing="MAIN_SIZE_ONLY_1.10_PERCENT")
        )
        missing_telegram = _telegram_text(missing).lower()
        self.assertIn("main_entry_only_201.37", missing_telegram)
        self.assertIn("stop/invalidation nao informado no main", missing_telegram)
        self.assertNotIn("confidence", missing_telegram)
        self.assertNotIn("confianca", missing_telegram)

    def test_v2_confirmed_tradeable_card_omits_observation_guidance_but_keeps_recorded_risk(self) -> None:
        entry = "MAIN_ENTRY_CONFIRM_147.26"
        stop = "MAIN_STOP_CONFIRM_138.04"
        sizing = "MAIN_SIZE_CONFIRM_2.35_PERCENT_NAV"
        operation_path = "Validar news e main decision-grade antes de qualquer entrada"
        with patch("advisor.analyst_review._path_to_operation", return_value=operation_path):
            review = _authoritative_main_review(
                _source_asset(
                    "TST",
                    "tradeable",
                    entry=entry,
                    stop=stop,
                    sizing=sizing,
                    reason_codes="high_volatility",
                )
            )
        payload = build_analyst_final_telegram_message(_telegram_text(review))
        card = payload.split("Trade candidate confirmado pelo main: TST.", 1)[1].split(
            "Nenhuma prioridade de observacao", 1
        )[0]
        lowered_card = card.lower()

        self.assertIn("trade candidate confirmado pelo main: tst", payload.lower())
        for term in (entry, stop, sizing):
            self.assertIn(term, payload)
        self.assertIn("motivo registrado: high volatility", lowered_card)
        for phrase in (
            "antes de reconsiderar",
            "faltam validacoes",
            "main decision-grade",
            "antes de qualquer entrada",
            "risco/invalidation: dados ainda",
        ):
            self.assertNotIn(phrase, lowered_card)

    def test_v2_multi_priority_call_keeps_checks_and_risk_scoped_to_each_card(self) -> None:
        equity_check = "EQUITY_CHECK_X"
        crypto_check = "CRYPTO_CHECK_Y"
        with patch(
            "advisor.analyst_review._path_to_operation",
            side_effect=lambda _decision, asset_type: equity_check if asset_type == "stock" else crypto_check,
        ):
            review = _authoritative_main_review(
                _source_asset("AMD", "technical_unvalidated"),
                _source_asset(
                    "HYPE",
                    "technical_unvalidated",
                    asset_type="crypto",
                    reason_codes="cvd_proxy_unavailable",
                ),
            )
        payload = build_analyst_final_telegram_message(_telegram_text(review))
        call = next(line for line in payload.splitlines() if line.startswith("Chamada pratica:"))
        lowered_call = call.lower()

        self.assertIn("prioridades de observacao (ordem de revisao, sem ranking financeiro): amd, hype.", payload.lower())
        self.assertIn(f"Antes de reconsiderar: {equity_check.replace('_', ' ')}", payload)
        self.assertIn(f"Antes de reconsiderar: {crypto_check.replace('_', ' ')}", payload)
        self.assertIn("cada ativo depende dos checks listados no respectivo card", lowered_call)
        self.assertNotIn(equity_check.lower(), lowered_call)
        self.assertNotIn(crypto_check.lower(), lowered_call)
        self.assertNotIn("risco/invalidation:", lowered_call)
        self.assertNotIn("dados/eventos/noticias ainda nao verificados", lowered_call)

    def test_v2_blocked_reason_prefers_operational_blocker_then_one_recorded_reason(self) -> None:
        review = _authoritative_main_review(
            _source_asset(
                "BLK",
                "blocked",
                blocking_reasons="provider_price_missing",
                reason_codes="fmp_request_timeout,secondary_reason_do_not_show",
            )
        )
        telegram = _telegram_text(review).lower()

        self.assertIn("provider price missing", telegram)
        self.assertIn("fmp request timeout", telegram)
        self.assertLess(telegram.index("provider price missing"), telegram.index("fmp request timeout"))
        self.assertNotIn("secondary reason do not show", telegram)
        self.assertNotIn("fabricated_cause", telegram)

    def test_v2_observation_priorities_do_not_claim_an_authoritative_leader(self) -> None:
        telegram = _telegram_text(generate_analyst_final_review(FULL_UNIVERSE_INPUT)).lower()

        self.assertIn("prioridades de observacao", telegram)
        self.assertNotIn("melhor equity", telegram)
        self.assertNotIn("melhor crypto", telegram)
        self.assertNotIn("melhor oportunidade", telegram)

    def test_v2_session_detection_warning_keeps_uncertainty_and_safety_consequence(self) -> None:
        telegram = _telegram_text(generate_analyst_final_review(FULL_UNIVERSE_INPUT)).lower()

        self.assertNotIn("classificacao da sessao pode estar incorreta", telegram)
        self.assertNotIn("nao foi tratado como base de decisao", telegram)
        self.assertNotIn("bug confirmado", telegram)
        self.assertNotIn("possible_session_detection_bug", telegram)

    def test_v2_high_volume_brief_reduces_observation_detail_without_cutting_trade_terms(self) -> None:
        trade1 = ("MAIN_ENTRY_TRADE1_101.25", "MAIN_STOP_TRADE1_97.40", "MAIN_SIZE_TRADE1_1.25_PERCENT")
        trade2 = ("MAIN_ENTRY_TRADE2_204.50", "MAIN_STOP_TRADE2_198.00", "MAIN_SIZE_TRADE2_2.00_PERCENT")
        assets = [
            _source_asset("TRADE1", "tradeable", entry=trade1[0], stop=trade1[1], sizing=trade1[2]),
            _source_asset("TRADE2", "tradeable", entry=trade2[0], stop=trade2[1], sizing=trade2[2]),
        ]
        for index in range(8):
            detail = f" Last price: {80 + index}.00; Daily change: 1.00%; Relative strength: 8.00%; Average volume: 900000.00; Secondary evidence: " + (
                f"optional-detail-{index}-" * 60
            )
            assets.append(
                _source_asset(
                    f"OBS{index}",
                    "technical_unvalidated",
                    metrics=detail.strip(),
                    reason_codes="news_not_collected_confidence_limited",
                )
            )
        assets.append(
            _source_asset(
                "BTC",
                "blocked",
                asset_type="crypto",
                reason_codes="cvd_proxy_unavailable,open_interest_change_unavailable,news_not_collected",
                metrics="Last price: 61500.00; Market cap: 1200000000000.00; Average volume: 35000000000.00; Daily change: 1.20%; Funding rate: -0.05%; Open interest change: n/a; CVD proxy: n/a; Coinbase premium: n/a; Liquidation imbalance: n/a",
            )
        )

        telegram = _telegram_text(_authoritative_main_review(*assets))
        payload = build_analyst_final_telegram_message(telegram)
        lowered = payload.lower()

        self.assertLessEqual(len(payload), 3500)
        for ticker in ("TRADE1", "TRADE2"):
            self.assertIn(ticker, payload)
        for term in (*trade1, *trade2):
            self.assertIn(term, payload)
        self.assertIn("trade candidate confirmado pelo main", lowered)
        self.assertIn("fluxo/derivativos nao verificados", lowered)
        self.assertIn("chamada pratica", lowered)
        self.assertIn("execucao manual", lowered)

        oversized_terms = (
            "ENTRY_OVERFLOW_" + ("E" * 1400),
            "STOP_OVERFLOW_" + ("S" * 1400),
            "SIZING_OVERFLOW_" + ("Z" * 1400),
        )
        oversized_review = _authoritative_main_review(
            _source_asset(
                "OVERFLOW",
                "tradeable",
                entry=oversized_terms[0],
                stop=oversized_terms[1],
                sizing=oversized_terms[2],
            )
        )
        oversized_payload = build_analyst_final_telegram_message(_telegram_text(oversized_review))

        self.assertLessEqual(len(oversized_payload), 3500)
        self.assertIn("OVERFLOW", oversized_payload)
        self.assertIn("relatorio completo", oversized_payload.lower())
        for term in oversized_terms:
            self.assertTrue(term in oversized_payload or term[:120] not in oversized_payload)

    def test_v2_nontradeable_source_states_stay_unapproved_beside_a_confirmed_tradeable(self) -> None:
        review = _authoritative_main_review(
            _source_asset("APPROVED", "tradeable", entry="APPROVED_ENTRY_100.00", stop="APPROVED_STOP_95.00", sizing="APPROVED_SIZE_1.00_PERCENT"),
            _source_asset("WATCH", "watch_buy"),
            _source_asset("RESEARCH", "technical_unvalidated", metrics="RSI: 51.00"),
            _source_asset("BLOCKED", "blocked", blocking_reasons="provider_not_configured"),
            _source_asset("REJECTED", "avoid", reason_codes="negative_ev_with_high_data_severity"),
        )
        telegram = _telegram_text(review).lower()

        self.assertIn("trade candidate confirmado pelo main: approved", telegram)
        for ticker in ("watch", "research", "blocked", "rejected"):
            self.assertNotIn(f"trade candidate confirmado pelo main: {ticker}", telegram)
            self.assertNotIn(f"entrada aprovada agora: {ticker}", telegram)

    def test_duplicate_main_close_assets_are_listed_once(self) -> None:
        review = generate_analyst_final_review(NIGHTLY_INPUT + "\n\n" + NIGHTLY_INPUT)

        telegram = _telegram_text(review)
        self.assertEqual(telegram.count("* AMD:"), 1)
        self.assertEqual(telegram.count("* HYPE:"), 1)

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

        telegram = _telegram_text(review).lower()
        self.assertIn("btc", telegram)
        self.assertIn("dados basicos disponiveis (live)", telegram)
        self.assertIn("fluxo/derivativos nao verificados", telegram)
        self.assertNotIn("trade candidate confirmado pelo main: btc", telegram)

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

    def test_telegram_summary_uses_bounded_observation_priorities_without_best_claims(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        telegram = _telegram_text(review).lower()

        self.assertIn("nao ha entrada aprovada agora", telegram)
        self.assertIn("contexto de mercado: partial", telegram)
        self.assertIn("prioridades de observacao", telegram)
        self.assertIn("amd", telegram)
        self.assertIn("hype", telegram)
        self.assertIn("rejeitados/bloqueados", telegram)
        self.assertNotIn("melhor equity", telegram)
        self.assertNotIn("melhor crypto", telegram)
        self.assertNotIn("nvda", telegram)
        self.assertNotIn("hims", telegram)
        self.assertNotIn("mu", telegram)
        self.assertNotIn("msft", telegram)
        self.assertNotIn("usar", telegram)
        self.assertNotIn("crdo", telegram)
        self.assertNotIn("dell", telegram)
        self.assertNotIn("mrvl", telegram)
        self.assertNotIn("hood", telegram)

    def test_telegram_summary_explains_decision_and_practical_call_without_raw_flags(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        telegram = _telegram_text(review).lower()

        self.assertIn("motivo principal", telegram)
        self.assertIn("contexto de mercado: partial", telegram)
        self.assertIn("chamada pratica", telegram)
        self.assertIn("execucao manual", telegram)
        self.assertNotIn("report data grade", telegram)
        self.assertNotIn("trade readiness", telegram)
        self.assertNotIn("melhor crypto", telegram)

    def test_final_review_starts_with_objective_reading(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)

        self.assertIn("## Leitura objetiva", review)
        objective = review.split("## Leitura objetiva", 1)[1].split("## Resumo do dia", 1)[0]
        self.assertIn("Ranking inicial:", objective)
        self.assertIn("Evitar/rejeitados:", objective)
        self.assertIn("Proximo passo pratico:", objective)

    def test_news_earnings_flow_not_verified_does_not_block_report_data_grade(self) -> None:
        review = _authoritative_main_review(
            _source_asset(
                "AMD",
                "technical_unvalidated",
                reason_codes="news_not_collected_confidence_limited,earnings_data_missing",
            ),
            _source_asset(
                "BTC",
                "technical_unvalidated",
                asset_type="crypto",
                reason_codes="crypto_flow_missing,cvd_proxy_unavailable",
            ),
        )

        self.assertIn("- report_data_grade: `decision_grade`", review)
        self.assertIn("- trade_readiness: `no_trade`", review)
        self.assertIn("nao ha entrada aprovada agora", _telegram_text(review).lower())
        self.assertIn("news_not_collected_confidence_limited", review)
        self.assertIn("cvd_proxy_unavailable", review)

    def test_diagnostic_primary_grade_cannot_be_promoted_by_final_data_grade(self) -> None:
        diagnostic_main = FULL_UNIVERSE_INPUT.replace(
            "- market_session: `regular,unknown`",
            "- market_session: `regular`",
        ).replace(
            "- missing_price_count: 11",
            "- missing_price_count: 0",
        )
        review = generate_analyst_final_review(diagnostic_main)

        self.assertIn("- report_data_grade: `partial_data`", review)
        self.assertIn("- primary_report_grade: `diagnostic_not_decision_grade`", review)
        self.assertIn("- main_primary_blocked: true", review)

    def test_decision_grade_no_trade_is_explained_by_the_asset_decision(self) -> None:
        review = _session_gate_review("false")
        objective = _section(review, "## Leitura objetiva")

        self.assertIn("- primary_report_grade: `decision_grade`", review)
        self.assertIn("- report_data_grade: `decision_grade`", review)
        self.assertIn("- main_primary_blocked: false", review)
        self.assertIn("source_decision: `wait`", review)
        self.assertIn("main utilizavel", objective.lower())
        self.assertNotIn("esperar main decision-grade", review.lower())

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

    def test_market_brief_is_partial_when_crypto_proxy_data_is_available(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        brief = _section(review, "## Market brief")

        self.assertIn("- market_brief_status: partial", brief)
        self.assertIn("- SPY/S&P proxy: missing", brief)
        self.assertIn("- QQQ/Nasdaq proxy: missing", brief)
        self.assertIn("- SMH/semi proxy: missing", brief)
        self.assertIn("- BTC: price=61500.00", brief)
        self.assertIn("- ETH: price=3400.00", brief)

    def test_market_context_v1_reaches_final_review_from_selected_main(self) -> None:
        context = _market_context_v1()
        review = generate_analyst_final_review(
            _market_context_nightly(context),
            extra_markdowns=[_market_context_main(context)],
        )
        brief = _section(review, "## Market brief")
        telegram = _telegram_text(review)

        self.assertIn("- market_context_contract_status: `observed`", brief)
        self.assertIn("- market_brief_status: complete", brief)
        for line in (
            "- BTC: price=61500.00 USDT; daily_change_pct=1.20%; source=binance; observed_at=2026-10-09T20:00:00Z",
            "- ETH: price=3400.00 USD; daily_change_pct=0.80%; source=coingecko; observed_at=2026-10-09T20:00:00Z",
            "- SPY/S&P proxy: price=500.00 USD; daily_change_pct=1.00%; source=fmp; observed_at=2026-10-09T20:00:00Z",
            "- QQQ/Nasdaq proxy: price=600.00 USD; daily_change_pct=1.10%; source=fmp; observed_at=2026-10-09T20:00:00Z",
            "- SMH/semi proxy: price=300.00 USD; daily_change_pct=1.20%; source=fmp; observed_at=2026-10-09T20:00:00Z",
        ):
            self.assertIn(line, brief)
        self.assertIn("- stock_regime: `neutral`", brief)
        self.assertIn("- crypto_regime: `risk_on`", brief)
        self.assertIn("BTC: price=61500.00 USDT", telegram)
        self.assertIn("source=binance", telegram)

    def test_market_context_copy_conflict_hides_conflicting_main_value(self) -> None:
        main_context = _market_context_v1()
        copied_context = _market_context_v1()
        copied_context["BTC"] = {**copied_context["BTC"], "price": 62000.0}
        review = generate_analyst_final_review(
            _market_context_nightly(copied_context),
            extra_markdowns=[_market_context_main(main_context)],
        )
        brief = _section(review, "## Market brief")

        self.assertIn("- market_context_contract_status: `transport_conflict`", brief)
        self.assertIn("- BTC: transport_conflict", brief)
        self.assertIn("- ETH: price=3400.00 USD", brief)
        self.assertNotIn("61500.00", brief)
        self.assertNotIn("62000.00", brief)
        self.assertIn("Context copy conflicts with selected main: BTC.", brief)

    def test_market_context_unavailable_and_missing_copy_remain_truthful(self) -> None:
        context = _market_context_v1(missing_smh=True)
        review = generate_analyst_final_review(
            _market_context_nightly(context),
            extra_markdowns=[_market_context_main(context)],
        )
        brief = _section(review, "## Market brief")

        self.assertIn("- SMH/semi proxy: unavailable", brief)
        self.assertNotIn("price=300.00", brief)
        self.assertIn("Proxies unavailable in the selected main: SMH.", brief)
        self.assertIn("- market_brief_status: partial", brief)

    def test_market_context_without_raw_main_is_conflict_but_legacy_stays_unknown(self) -> None:
        context = _market_context_v1()
        main_without_context = (
            "# Investment and Swing Trade Advisor\n\n- report_type: `main`\n- Data mode: `live`\n- report_grade: `decision_grade`\n\n"
            + _source_asset("NVDA", "wait")
        )
        copied_only = generate_analyst_final_review(
            _market_context_nightly(context),
            extra_markdowns=[main_without_context],
        )
        old_artifact = generate_analyst_final_review(NIGHTLY_INPUT)

        self.assertIn("- market_context_contract_status: `transport_conflict`", _section(copied_only, "## Market brief"))
        self.assertIn("- BTC: transport_conflict", _section(copied_only, "## Market brief"))
        self.assertIn("- market_context_contract_status: `legacy_unknown`", _section(old_artifact, "## Market brief"))

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

    def test_crypto_watch_context_is_observation_only_when_research_only_absent(self) -> None:
        no_hype = FULL_UNIVERSE_INPUT.replace("## HYPE\n- Ativo: `HYPE`", "## HYPE\n- ignored: `HYPE`")
        review = generate_analyst_final_review(no_hype)
        telegram = _telegram_text(review).lower()

        self.assertIn("prioridades de observacao", telegram)
        self.assertNotIn("melhor crypto", telegram)
        self.assertNotIn("trade candidate confirmado pelo main: btc", telegram)

    def test_equities_only_brief_does_not_claim_a_best_crypto(self) -> None:
        equities_only = FULL_UNIVERSE_INPUT.split("## BTC", 1)[0]
        review = generate_analyst_final_review(equities_only)
        telegram = _telegram_text(review).lower()

        self.assertNotIn("melhor crypto", telegram)

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
        self.assertIn("- market_session_conflict: unknown", diagnostic)
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
            "Main nao foi decision-grade pelos reason_codes acima.",
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

    def test_missing_session_conflict_does_not_make_telegram_claim_conflict(self) -> None:
        review = generate_analyst_final_review(FULL_UNIVERSE_INPUT)
        telegram = _telegram_text(review).lower()

        self.assertIn("incerteza material", telegram)
        self.assertNotIn("classificacao da sessao pode estar incorreta", telegram)
        self.assertNotIn("nao foi tratado como base de decisao", telegram)
        self.assertIn("market_session_conflict_unknown", telegram)

    def test_false_market_session_conflict_does_not_warn(self) -> None:
        review = _session_gate_review("false")
        telegram = _telegram_text(review).lower()

        self.assertIn("- main_primary_blocked: false", review)
        self.assertNotIn("classificacao da sessao pode estar incorreta", telegram)
        self.assertNotIn("por seguranca, o main nao foi tratado como base de decisao", telegram)

    def test_true_market_session_conflict_remains_a_gate(self) -> None:
        review = _session_gate_review("true", blocking_reason="market_session_conflict")
        telegram = _telegram_text(review).lower()

        self.assertIn("- main_primary_blocked: true", review)
        self.assertIn("classificacao da sessao pode estar incorreta", telegram)
        self.assertIn("por seguranca, o main nao foi tratado como base de decisao", telegram)

    def test_missing_market_session_conflict_is_unknown(self) -> None:
        review = _session_gate_review(None, blocking_reason="unrelated_existing_blocker")
        diagnostic = _section(review, "## Por que o main nao foi decision-grade")

        self.assertIn("- market_session_conflict: unknown", diagnostic)
        self.assertNotIn("- market_session_conflict: true", diagnostic)
        self.assertNotIn("- market_session_conflict: false", diagnostic)

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
