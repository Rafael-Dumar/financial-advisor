from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from advisor.models import AssetDecision
from advisor.risk import evaluate_leverage_policy


def render_markdown_report(
    decisions: list[AssetDecision],
    *,
    stock_regime: str,
    crypto_regime: str,
    report_type: str = "main",
    data_mode: str = "unspecified",
    portfolio_alerts: list[str] | None = None,
    generated_at: str | None = None,
    data_freshness: str = "controlled_by_cache_freshness",
    provider_budget: dict[str, Any] | None = None,
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    non_live_mode = _is_non_live_mode(data_mode)
    blocked_count = sum(1 for decision in decisions if decision.decision == "blocked")
    market_sessions = sorted({decision.market_session for decision in decisions if decision.market_session})
    stale_count = sum(1 for decision in decisions if decision.is_stale)
    general_decision = "no_trade_day" if non_live_mode else _general_decision(decisions)
    report_type = report_type if report_type in {"main", "close"} else "main"
    lines = [
        "# Investment and Swing Trade Advisor",
        "",
        "Uso pessoal e educacional. O relatorio separa qualidade do ativo de qualidade da entrada atual.",
        "",
        f"- Generated at: `{generated_at}`",
        f"- report_type: `{report_type}`",
        f"- Data freshness: `{data_freshness}`",
        f"- Data mode: `{data_mode}`",
        *([f"- AVISO: **Nao usar para decisao real**"] if non_live_mode else []),
        f"- market_session: `{','.join(market_sessions) if market_sessions else 'unknown'}`",
        f"- stale_assets: {stale_count}",
        f"- Stock regime: `{stock_regime}`",
        f"- Crypto regime: `{crypto_regime}`",
        f"- Ativos analisados: {len(decisions)}",
        f"- Blocked por dados: {blocked_count}",
        f"- Decisao geral: `{general_decision}`",
        f"- Alertas de carteira: {_format_list(portfolio_alerts or [])}",
        "",
    ]
    lines.extend(_provider_budget_section(provider_budget))
    ranked_decisions = _rank_decisions(decisions)
    if report_type == "close":
        lines.extend(_close_summary_sections(ranked_decisions, general_decision, portfolio_alerts or []))
    else:
        lines.extend(
            _main_summary_sections(
                ranked_decisions,
                general_decision,
                stock_regime=stock_regime,
                crypto_regime=crypto_regime,
                portfolio_alerts=portfolio_alerts or [],
            )
        )
    lines.extend(_tradeable_today_section(ranked_decisions, actionable=not non_live_mode))
    lines.extend(_watchlist_only_section(ranked_decisions, actionable=not non_live_mode))
    lines.extend(_technical_unvalidated_section(ranked_decisions))
    lines.extend(_research_queue_section(ranked_decisions))
    lines.extend(_wait_section(ranked_decisions))
    lines.extend(_rejected_section(ranked_decisions))
    lines.extend(_avoid_section(ranked_decisions))
    lines.extend(_blocked_section(ranked_decisions))
    lines.extend(_short_watchlist_section(ranked_decisions))
    lines.extend(_ranking_section(ranked_decisions))
    for decision in ranked_decisions:
        lines.extend(_asset_section(decision))
    return "\n".join(lines).rstrip() + "\n"


def render_blocked_report(
    *,
    report_type: str,
    reasons: list[str],
    generated_at: str | None = None,
    provider_budget: dict[str, Any] | None = None,
) -> str:
    return render_markdown_report(
        [],
        stock_regime="not_verified",
        crypto_regime="not_verified",
        report_type=report_type,
        data_mode="blocked",
        portfolio_alerts=sorted(set(["live_validation_failed", *reasons])),
        generated_at=generated_at,
        data_freshness="not_verified",
        provider_budget=provider_budget,
    )


def render_analyst_review_input(
    decisions: list[AssetDecision],
    *,
    report_type: str,
    data_mode: str,
    stock_regime: str,
    crypto_regime: str,
    generated_at: str | None = None,
) -> str:
    generated_at = generated_at or datetime.now(timezone(timedelta(hours=-3))).isoformat(timespec="seconds")
    ranked = _rank_decisions(decisions)
    general_decision = "no_trade_day" if _is_non_live_mode(data_mode) else _general_decision(decisions)
    market_sessions = sorted({decision.market_session for decision in decisions if decision.market_session})
    equity_candidates = [
        decision
        for decision in ranked
        if decision.asset_type == "stock" and decision.decision in {"tradeable", "watch_buy", "technical_unvalidated"}
    ][:3]
    crypto_candidates = [
        decision
        for decision in ranked
        if decision.asset_type == "crypto" and decision.decision in {"tradeable", "watch_buy", "technical_unvalidated"}
    ][:3]
    lines = [
        "# Analyst review input",
        "",
        f"- BRT date: `{generated_at[:10]}`",
        f"- generated_at: `{generated_at}`",
        f"- report_type: `{report_type}`",
        f"- data_mode: `{data_mode}`",
        f"- market_session: `{','.join(market_sessions) if market_sessions else 'unknown'}`",
        f"- bot_general_decision: `{general_decision}`",
        f"- stock_regime: `{stock_regime}`",
        f"- crypto_regime: `{crypto_regime}`",
        "",
        "## Top equity candidates for qualitative review",
        "",
    ]
    if not equity_candidates:
        lines.append("No equity candidates for qualitative review")
    else:
        for decision in equity_candidates:
            lines.extend(_analyst_candidate_lines(decision))
    lines.extend(
        [
            "",
            "## Crypto review needed",
            "",
        ]
    )
    if not crypto_candidates:
        lines.append("No crypto candidates for qualitative review")
    else:
        lines.append("Crypto is separate from equity review; technical_unvalidated is not approval to buy.")
        for decision in crypto_candidates:
            lines.extend(_analyst_candidate_lines(decision))
    lines.extend(
        [
            "",
            "## Questions for analyst/plugin",
            "",
            "- Is the business quality and valuation strong enough to support the setup?",
            "- Are news, earnings, guidance, or regulatory catalysts still not_verified?",
            "- Should any candidate be downgraded to watch_only or research_queue?",
            "- What concrete invalidation would make the setup wrong?",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_html_report(markdown: str) -> str:
    body = "\n".join(f"<p>{escape(line)}</p>" if line else "" for line in markdown.splitlines())
    return f"<!doctype html><html><head><meta charset=\"utf-8\"><title>Advisor Report</title></head><body>{body}</body></html>"


def _provider_budget_section(provider_budget: dict[str, Any] | None) -> list[str]:
    if not provider_budget:
        return []
    estimated = provider_budget.get("estimated_calls", {})
    used = provider_budget.get("used_calls", {})
    lines = [
        "## provider_budget_summary",
        "",
        f"- actions_cache_hit: `{provider_budget.get('actions_cache_hit', 'unknown')}`",
        f"- cache_hits: {int(provider_budget.get('cache_hits', 0) or 0)}",
        f"- cache_misses: {int(provider_budget.get('cache_misses', 0) or 0)}",
        f"- universe_requested: {int(provider_budget.get('universe_requested', 0) or 0)}",
        f"- universe_scanned: {int(provider_budget.get('universe_scanned', 0) or 0)}",
        f"- discovery_enabled: `{str(bool(provider_budget.get('discovery_enabled', False))).lower()}`",
        f"- skipped_due_to_api_budget: `{str(bool(provider_budget.get('skipped_due_to_api_budget', False))).lower()}`",
        f"- provider_rate_limit_status: `{provider_budget.get('provider_rate_limit_status', 'unknown')}`",
        f"- few_assets_reason: `{provider_budget.get('few_assets_reason', 'other')}`",
    ]
    for provider in sorted(set([*estimated.keys(), *used.keys()])):
        lines.append(f"- {provider}_calls_estimated: {int(estimated.get(provider, 0) or 0)}")
        lines.append(f"- {provider}_calls_used: {int(used.get(provider, 0) or 0)}")
    lines.append("")
    return lines


def _main_summary_sections(
    decisions: list[AssetDecision],
    general_decision: str,
    *,
    stock_regime: str,
    crypto_regime: str,
    portfolio_alerts: list[str],
) -> list[str]:
    tradeable = [decision for decision in decisions if decision.decision == "tradeable"]
    watchlist = [decision for decision in decisions if decision.decision == "watch_buy"]
    research = _research_queue(decisions)
    blocked = [decision for decision in decisions if decision.decision == "blocked"]
    rejected = _rejected(decisions)
    return [
        "## Resumo executivo",
        "",
        f"- Ativos tradeable: {len(tradeable)}",
        f"- Watchlist aprovada: {len(watchlist)}",
        f"- Research queue: {len(research)}",
        f"- Blocked: {len(blocked)}",
        f"- Rejected: {len(rejected)}",
        "",
        "## Decisao geral",
        "",
        f"Postura sugerida: `{general_decision}`. Sem ordem automatica, sem broker e sem compra automatica.",
        "",
        "## O que moveu o mercado",
        "",
        _market_driver_summary(decisions, portfolio_alerts),
        "",
        "## Regime de mercado",
        "",
        f"- Stock regime: `{stock_regime}`",
        f"- Crypto regime: `{crypto_regime}`",
        "",
        "## Setores fortes e fracos",
        "",
        f"- Fortes: {_format_list(_themes_for([*tradeable, *watchlist]))}",
        f"- Fracos/bloqueados: {_format_list(_themes_for([*blocked, *rejected]))}",
        "",
        "## Watchlist aprovada",
        "",
        *_symbol_lines(watchlist, empty="Nenhum ativo aprovado para watchlist."),
        "",
        "## Research queue",
        "",
        *_symbol_lines(research, empty="Nenhum ativo em research queue."),
        "",
        "## Shorts observacionais",
        "",
        *_short_lines(decisions),
        "",
        "## Rejected",
        "",
        *_symbol_lines(rejected, empty="Nenhum ativo rejected."),
        "",
        "## Noticias e catalisadores relevantes",
        "",
        _news_verification_summary(decisions),
        "",
        "## Earnings/guidance",
        "",
        _event_verification_summary(decisions),
        "",
        "## Plano de acao sugerido",
        "",
        _action_plan(general_decision),
        "",
        "## Riscos principais",
        "",
        _risk_summary(decisions, portfolio_alerts),
        "",
        "## Dados ausentes",
        "",
        _missing_data_summary(decisions),
        "",
        "## Checklist antes de operar",
        "",
        "- Confirmar que `Data mode` esta `live`.",
        "- Conferir noticias, earnings/guidance e dados not_verified.",
        "- Respeitar stop/invalidation do setup; ajustar tamanho, nao o stop.",
        "- Confirmar limites diario e semanal antes de qualquer operacao manual.",
        "",
    ]


def _close_summary_sections(
    decisions: list[AssetDecision],
    general_decision: str,
    portfolio_alerts: list[str],
) -> list[str]:
    tomorrow = [
        decision
        for decision in decisions
        if decision.decision in {"tradeable", "watch_buy"}
    ]
    blocked_or_remove = [
        decision
        for decision in decisions
        if decision.decision in {"blocked", "avoid", "wait", "technical_unvalidated"}
    ]
    return [
        "## Resumo de fechamento",
        "",
        f"- Ativos para acompanhar: {len(tomorrow)}",
        f"- Ativos para remover/bloquear: {len(blocked_or_remove)}",
        "",
        "## Decisao geral para o proximo dia",
        "",
        f"Postura sugerida para o proximo pregao: `{general_decision}`.",
        "",
        "## Mudancas vs main report",
        "",
        "Comparacao automatica com o main report ainda not_verified nesta V1.",
        "",
        "## O que melhorou",
        "",
        _format_list([decision.symbol for decision in tomorrow]) if tomorrow else "Nada verificado como melhora operacional.",
        "",
        "## O que piorou",
        "",
        _format_list([decision.symbol for decision in blocked_or_remove]) if blocked_or_remove else "Nada verificado como piora operacional.",
        "",
        "## Watchlist para amanha",
        "",
        *_symbol_lines(tomorrow, empty="Nenhum ativo aprovado para amanha."),
        "",
        "## Remover/bloquear da watchlist",
        "",
        *_symbol_lines(blocked_or_remove, empty="Nenhum ativo para remover/bloquear."),
        "",
        "## Stops/invalidation",
        "",
        *_stop_lines(tomorrow),
        "",
        "## Noticias e catalisadores pos-main",
        "",
        _news_verification_summary(decisions),
        "",
        "## Riscos principais",
        "",
        _risk_summary(decisions, portfolio_alerts),
        "",
        "## Dados ausentes",
        "",
        _missing_data_summary(decisions),
        "",
        "## Preparacao para o proximo pregao",
        "",
        "- Revalidar live config antes do proximo relatorio.",
        "- Atualizar watchlist somente com dados verificados.",
        "- Manter shorts apenas observacionais na V1.",
        "",
    ]


def _ranking_section(decisions: list[AssetDecision]) -> list[str]:
    if not decisions:
        return ["## Ranking de oportunidades", "", "Nenhum ativo analisado.", ""]
    lines = ["## Ranking de oportunidades", ""]
    for index, decision in enumerate(decisions, start=1):
        win_rate = _ranking_win_rate(decision)
        lines.append(
            f"{index}. `{decision.symbol}` - `{decision.decision}` | "
            f"Swing {decision.swing_trade_score:.0f} | Investment {decision.investment_quality_score:.0f} | {win_rate}"
        )
    lines.append("")
    return lines


def _research_queue_section(decisions: list[AssetDecision]) -> list[str]:
    items = _research_queue(decisions)
    lines = ["## Research queue", ""]
    lines.extend(_symbol_lines(items, empty="Nenhum ativo em research queue."))
    lines.append("")
    return lines


def _rejected_section(decisions: list[AssetDecision]) -> list[str]:
    items = _rejected(decisions)
    lines = ["## Rejected", ""]
    lines.extend(_symbol_lines(items, empty="Nenhum ativo rejected."))
    lines.append("")
    return lines


def _tradeable_today_section(decisions: list[AssetDecision], *, actionable: bool = True) -> list[str]:
    tradeable = [decision for decision in decisions if decision.decision == "tradeable"]
    lines = ["## Tradeable hoje", ""]
    if not actionable:
        lines.extend(["Nenhum ativo tradeable neste data mode. Nao usar para decisao real.", ""])
        return lines
    if not tradeable:
        lines.extend(["Nenhum ativo tradeable hoje.", ""])
        return lines
    lines.extend(f"- `{decision.symbol}`" for decision in tradeable)
    lines.append("")
    return lines


def _watchlist_only_section(decisions: list[AssetDecision], *, actionable: bool = True) -> list[str]:
    watchlist = [decision for decision in decisions if decision.decision == "watch_buy"]
    lines = ["## Watchlist apenas", ""]
    if not actionable:
        lines.extend(["Nenhum ativo em watchlist acionavel neste data mode. Nao usar para decisao real.", ""])
        return lines
    if not watchlist:
        lines.extend(["Nenhum ativo em watchlist apenas.", ""])
        return lines
    lines.extend(f"- `{decision.symbol}`" for decision in watchlist)
    lines.append("")
    return lines


def _technical_unvalidated_section(decisions: list[AssetDecision]) -> list[str]:
    items = [decision for decision in decisions if decision.decision == "technical_unvalidated"]
    lines = ["## Setup tecnico detectado, mas nao validado", ""]
    if not items:
        lines.extend(["Nenhum setup tecnico nao validado.", ""])
        return lines
    lines.extend(
        f"- `{decision.symbol}`: dados/EV/confianca ainda nao validam entrada operacional"
        for decision in items
    )
    lines.append("")
    return lines


def _wait_section(decisions: list[AssetDecision]) -> list[str]:
    items = [decision for decision in decisions if decision.decision in {"wait", "watch_only"}]
    lines = ["## Wait", ""]
    if not items:
        lines.extend(["Nenhum ativo em wait.", ""])
        return lines
    lines.extend(f"- `{decision.symbol}`" for decision in items)
    lines.append("")
    return lines


def _avoid_section(decisions: list[AssetDecision]) -> list[str]:
    items = [decision for decision in decisions if decision.decision == "avoid"]
    lines = ["## Avoid/Rejected", ""]
    if not items:
        lines.extend(["Nenhum ativo rejeitado.", ""])
        return lines
    lines.extend(f"- `{decision.symbol}`" for decision in items)
    lines.append("")
    return lines


def _blocked_section(decisions: list[AssetDecision]) -> list[str]:
    items = [decision for decision in decisions if decision.decision == "blocked"]
    lines = ["## Blocked", ""]
    if not items:
        lines.extend(["Nenhum ativo blocked por dados.", ""])
        return lines
    lines.extend(f"- `{decision.symbol}`" for decision in items)
    lines.append("")
    return lines


def _short_watchlist_section(decisions: list[AssetDecision]) -> list[str]:
    items = [
        decision
        for decision in decisions
        if decision.short_status == "watch_only" and decision.short_setup_score > 0
    ]
    lines = ["## Short watchlist apenas", ""]
    if not items:
        lines.extend(["Nenhum ativo em short watchlist observacional.", ""])
        return lines
    lines.extend(
        f"- `{decision.symbol}`: short_setup_score {decision.short_setup_score:.0f}; nao operacional"
        for decision in items
    )
    lines.append("")
    return lines


def _research_queue(decisions: list[AssetDecision]) -> list[AssetDecision]:
    return [
        decision
        for decision in decisions
        if decision.decision in {"technical_unvalidated", "speculative_watch"}
    ]


def _rejected(decisions: list[AssetDecision]) -> list[AssetDecision]:
    return [
        decision
        for decision in decisions
        if decision.decision in {"avoid", "wait", "watch_only"}
    ]


def _symbol_lines(decisions: list[AssetDecision], *, empty: str) -> list[str]:
    if not decisions:
        return [empty]
    return [f"- `{decision.symbol}`" for decision in decisions]


def _short_lines(decisions: list[AssetDecision]) -> list[str]:
    items = [
        decision
        for decision in decisions
        if decision.short_status == "watch_only" and decision.short_setup_score > 0
    ]
    if not items:
        return ["Nenhum short operacional. Shorts sao apenas observacionais na V1."]
    return [
        f"- `{decision.symbol}`: short_setup_score {decision.short_setup_score:.0f}; nao operacional"
        for decision in items
    ]


def _stop_lines(decisions: list[AssetDecision]) -> list[str]:
    if not decisions:
        return ["Nenhum stop/invalidation operacional para listar."]
    return [
        f"- `{decision.symbol}`: invalidation {decision.risk_plan.stop:.2f}; tamanho se adapta ao risco."
        for decision in decisions
    ]


def _analyst_candidate_lines(decision: AssetDecision) -> list[str]:
    return [
        f"- `{decision.symbol}`",
        f"  - bot_decision: `{decision.decision}`",
        f"  - reason: {decision.thesis}",
        f"  - risks: {_format_list(sorted(set([*decision.alerts, *decision.limitations])))}",
        f"  - valuation_summary: {'; '.join(decision.metrics_summary) if decision.metrics_summary else 'not_verified'}",
        f"  - earnings_guidance_status: `{_verification_status(decision.event_check_status)}`",
        f"  - news_catalyst_status: `{_verification_status(decision.news_status)}`",
    ]


def _themes_for(decisions: list[AssetDecision]) -> list[str]:
    themes = []
    for decision in decisions:
        benchmark = decision.sector_benchmark or decision.asset_type
        if benchmark:
            themes.append(benchmark)
    return sorted(set(themes))


def _market_driver_summary(decisions: list[AssetDecision], portfolio_alerts: list[str]) -> str:
    alerts = sorted(set([*portfolio_alerts, *[alert for decision in decisions for alert in decision.alerts]]))
    if not alerts:
        return "not_verified: nenhum driver externo coletado nesta V1."
    return _format_list(alerts)


def _news_verification_summary(decisions: list[AssetDecision]) -> str:
    if not decisions:
        return "not_verified"
    not_verified = [
        decision.symbol
        for decision in decisions
        if _verification_status(decision.news_status) == "not_verified"
    ]
    if not_verified:
        return f"not_verified: {_format_list(not_verified)}"
    return "collected"


def _event_verification_summary(decisions: list[AssetDecision]) -> str:
    if not decisions:
        return "not_verified"
    not_verified = [
        decision.symbol
        for decision in decisions
        if _verification_status(decision.event_check_status) == "not_verified"
    ]
    if not_verified:
        return f"not_verified: {_format_list(not_verified)}"
    return "verified where applicable"


def _action_plan(general_decision: str) -> str:
    if general_decision == "operate":
        return "Operar somente manualmente, apos checklist e com risco predefinido."
    if general_decision == "wait":
        return "Aguardar confirmacao; preparar cenarios sem executar ordem automatica."
    return "No trade: revisar dados ausentes e manter observacao."


def _risk_summary(decisions: list[AssetDecision], portfolio_alerts: list[str]) -> str:
    risks = sorted(set([*portfolio_alerts, *[alert for decision in decisions for alert in decision.alerts]]))
    return _format_list(risks) if risks else "not_verified"


def _missing_data_summary(decisions: list[AssetDecision]) -> str:
    missing = sorted(set(limitation for decision in decisions for limitation in decision.limitations))
    return _format_list(missing) if missing else "nenhum"


def _rank_decisions(decisions: list[AssetDecision]) -> list[AssetDecision]:
    priority = {
        "tradeable": 0,
        "strong_buy_candidate": 0,
        "watch_buy": 1,
        "technical_unvalidated": 2,
        "speculative_watch": 2,
        "watch_only": 3,
        "wait": 3,
        "avoid": 4,
        "blocked": 5,
        "no_trade_day": 6,
    }
    return sorted(
        decisions,
        key=lambda decision: (
            priority.get(decision.decision, 4),
            -decision.swing_trade_score,
            -decision.investment_quality_score,
            decision.symbol,
        ),
    )


def _ranking_win_rate(decision: AssetDecision) -> str:
    if not _can_show_backtest_stats(decision):
        return "win rate oculto"
    stats = decision.backtest_stats
    assert stats is not None
    return f"win rate +2R {round(stats.win_rate_2r * 100)}% ({stats.sample_size} setups)"


def _asset_section(decision: AssetDecision) -> list[str]:
    plan = decision.risk_plan
    stats = decision.backtest_stats
    has_confidence_gap = _has_confidence_limiting_limitation(decision.limitations)
    leverage = evaluate_leverage_policy(
        decision_confidence_score=decision.decision_confidence_score,
        missing_data_severity=decision.missing_data_severity,
    )
    if _can_show_backtest_stats(decision):
        assert stats is not None
        win_rate = f"Setup win rate estimado: {round(stats.win_rate_2r * 100)}%"
        sample = f"Amostra: {stats.sample_size} setups parecidos"
        criterion = "Criterio: atingiu +2R antes de -1R dentro do horizonte"
        quality = f"Qualidade da amostra: {decision.sample_quality}"
        if stats.win_rate_3r is not None and stats.sample_size >= 60:
            three_r = f"Win rate +3R: {round(stats.win_rate_3r * 100)}%"
        else:
            three_r = "Win rate +3R: oculto por amostra insuficiente"
    elif has_confidence_gap:
        win_rate = "Setup win rate estimado: oculto por dados incompletos ou nao-live"
        sample = "Amostra: insuficiente ou nao confiavel"
        criterion = "Criterio: +2R antes de -1R"
        quality = "Qualidade da amostra: baixa"
        three_r = "Win rate +3R: oculto por dados incompletos ou amostra insuficiente"
    else:
        win_rate = "Setup win rate estimado: oculto por amostra insuficiente"
        sample = "Amostra: insuficiente"
        criterion = "Criterio: +2R antes de -1R"
        quality = "Qualidade da amostra: baixa"
        three_r = "Win rate +3R: oculto por amostra insuficiente"

    return [
        f"## {decision.symbol}",
        "",
        f"- Ativo: `{decision.symbol}`",
        f"- Tipo: `{decision.asset_type}`",
        f"- decision_label: `{decision.decision}`",
        f"- Decisao: `{decision.decision}`",
        f"- reason_codes: {_format_list(decision.reason_codes or sorted(set([*decision.alerts, *decision.limitations])))}",
        f"- data_quality: `{decision.data_quality}`",
        f"- missing_data_severity: `{decision.missing_data_severity}`",
        f"- data_quality_score: {decision.data_quality_score}",
        f"- decision_confidence_score: {decision.decision_confidence_score}",
        f"- Investment Quality Score: {decision.investment_quality_score:.0f}",
        f"- Swing Trade Score: {decision.swing_trade_score:.0f}",
        f"- expected_value_r: {_format_expected_value(stats)}",
        f"- avg_win_r: {_format_optional_metric(stats.avg_win_r if stats else None)}",
        f"- avg_loss_r: {_format_optional_metric(stats.avg_loss_r if stats else None)}",
        f"- sample_size: {stats.sample_size if stats else 0}",
        f"- confidence_quality: {decision.sample_quality or 'unknown'}",
        f"- {win_rate}",
        f"- {sample}",
        f"- {criterion}",
        f"- {quality}",
        f"- {three_r}",
        f"- Hold sugerido: {decision.hold_suggestion}",
        f"- Tese: {decision.thesis}",
        f"- Metricas principais: {'; '.join(decision.metrics_summary)}",
        f"- Event risk: {_event_risk(decision)}",
        f"- event_check_status: `{_verification_status(decision.event_check_status)}`",
        f"- News/catalyst summary: {decision.news_summary or 'not_collected'}",
        f"- news_status: `{_verification_status(decision.news_status)}`",
        f"- macro_regime: `{decision.macro_regime}`",
        f"- macro_status: `{decision.macro_status}`",
        f"- thesis_status: `{decision.thesis_status}`",
        f"- Data source: {decision.data_source}",
        f"- provider: `{decision.provider}`",
        f"- Data timestamp: {decision.data_timestamp or 'unknown'}",
        f"- last_price_timestamp: {decision.last_price_timestamp or 'unknown'}",
        f"- Cache age: {_format_cache_age(decision.cache_age_seconds)}",
        f"- market_session: `{decision.market_session}`",
        f"- is_stale: `{'yes' if decision.is_stale else 'no'}`",
        f"- stale_reason: {decision.stale_reason or 'not_stale'}",
        f"- relative_strength_vs_spy: {_format_optional_metric(decision.relative_strength_vs_spy)}",
        f"- relative_strength_vs_qqq: {_format_optional_metric(decision.relative_strength_vs_qqq)}",
        f"- relative_strength_vs_sector: {_format_optional_metric(decision.relative_strength_vs_sector)}",
        f"- sector_benchmark: {decision.sector_benchmark or 'not_collected'}",
        f"- Entrada ideal: {decision.ideal_entry:.2f}",
        f"- Entrada alternativa: {_format_optional(decision.alternative_entry)}",
        f"- Stop/invalidation: {plan.stop:.2f}",
        f"- Alvo 2R: {plan.target_2r:.2f}",
        f"- Alvo 3R: {plan.target_3r:.2f}",
        f"- Risco por trade: {plan.risk_amount:.2f} ({_format_percent(plan.risk_fraction)} do capital)",
        f"- Tamanho maximo da posicao: {_format_position_units(plan)} unidades / {plan.max_position_value:.2f}",
        f"- Relacao risco/retorno: {plan.risk_reward_2r}",
        f"- Leverage allowed: `{'yes' if leverage.allowed else 'no'}`",
        f"- Leverage policy reasons: {_format_list(leverage.reasons)}",
        f"- Principais alertas: {_format_list(decision.alerts)}",
        f"- Dados ausentes ou limitacoes: {_format_list(decision.limitations)}",
        f"- short_setup_score: {decision.short_setup_score:.0f}",
        f"- squeeze_risk: `{decision.squeeze_risk}`",
        f"- gap_risk: `{decision.gap_risk}`",
        f"- borrow_data_available: `{str(decision.borrow_data_available).lower()}`",
        f"- short_status: `{decision.short_status}`",
        "",
    ]


def _can_show_backtest_stats(decision: AssetDecision) -> bool:
    stats = decision.backtest_stats
    return (
        stats is not None
        and stats.sample_size >= 30
        and stats.win_rate_2r is not None
        and not _has_confidence_limiting_limitation(decision.limitations)
    )


def _has_confidence_limiting_limitation(limitations: list[str]) -> bool:
    non_blocking = {"cvd_proxy_uses_taker_buy_sell_volume", "fmp_price_light_fallback"}
    explicitly_limiting = {"earnings_data_missing", "news_rumor_not_confirmed", "news_confidence_low"}
    for limitation in limitations:
        if limitation in non_blocking:
            continue
        if limitation in explicitly_limiting:
            return True
        if (
            limitation.startswith("missing_")
            or limitation.startswith("insufficient_")
            or limitation.endswith("_unavailable")
            or limitation.endswith("_not_live")
            or limitation.endswith("_demo")
            or limitation == "data_incomplete_confidence_limited"
        ):
            return True
    return False


def _format_optional(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _format_optional_metric(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _format_expected_value(stats) -> str:
    if stats is None or stats.expected_value_r is None:
        return "n/a"
    value = f"{stats.expected_value_r:.2f}"
    if stats.avg_win_r is None or stats.avg_loss_r is None:
        return f"{value} (limited/model_estimate)"
    return value


def _format_list(values: list[str]) -> str:
    return ", ".join(values) if values else "nenhum"


def _format_percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def _format_position_units(plan) -> str:
    if plan.position_size_display:
        return plan.position_size_display
    if float(plan.max_position_units).is_integer():
        return str(int(plan.max_position_units))
    return f"{plan.max_position_units:.4f}".rstrip("0").rstrip(".")


def _verification_status(status: str) -> str:
    if status in {"", "unknown", "not_collected"}:
        return "not_verified"
    return status


def _event_risk(decision: AssetDecision) -> str:
    if decision.asset_type == "crypto":
        return "not_applicable"
    if decision.decision == "blocked" or {
        "insufficient_price_history",
        "price_history_unavailable",
        "fmp_price_unavailable",
    } & set(decision.limitations):
        return "unknown"
    if "earnings_data_missing" in decision.limitations:
        return "unknown"
    event_codes = [
        code
        for code in ["earnings_near", "event_risk", "earnings_imminent", "earnings_data_missing", "post_earnings_gap", "recent_guidance"]
        if code in decision.alerts or code in decision.limitations
    ]
    return _format_list(event_codes)


def _format_cache_age(cache_age_seconds: int | None) -> str:
    return "unknown" if cache_age_seconds is None else f"{cache_age_seconds}s"


def _general_decision(decisions: list[AssetDecision]) -> str:
    if any(decision.decision == "tradeable" for decision in decisions):
        return "operate"
    if any(decision.decision in {"watch_buy", "technical_unvalidated", "speculative_watch", "watch_only"} for decision in decisions):
        return "wait"
    return "no_trade_day"


def _is_non_live_mode(data_mode: str) -> bool:
    return data_mode.lower() in {"fixture", "demo", "limited", "blocked"}
