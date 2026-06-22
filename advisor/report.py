from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from html import escape
from typing import Any

from advisor.models import AssetDecision
from advisor.risk import evaluate_leverage_policy


CONSERVATIVE_TECHNICAL_THESIS = (
    "Setup tecnico detectado, mas dados incompletos/EV/fluxo/noticias nao validam entrada operacional."
)
BRT = timezone(timedelta(hours=-3))
CLOSE_WINDOW_AFTER_REGULAR_CLOSE = timedelta(hours=2, minutes=30)


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
    report_type = report_type if report_type in {"main", "close"} else "main"
    report_grade = _report_grade(
        report_type=report_type,
        data_mode=data_mode,
        market_sessions=market_sessions,
        generated_at=generated_at,
        has_stale_assets=stale_count > 0,
    )
    diagnostic_main = report_grade == "diagnostic_not_decision_grade"
    actionable_report = report_grade == "decision_grade" and not non_live_mode
    general_decision = "no_trade_day" if non_live_mode or diagnostic_main else _general_decision(decisions)
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
        f"- report_grade: `{report_grade}`",
        *(_report_grade_warnings(report_grade)),
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
        lines.extend(
            _close_summary_sections(
                ranked_decisions,
                general_decision,
                portfolio_alerts or [],
                report_grade=report_grade,
            )
        )
    else:
        lines.extend(
            _main_summary_sections(
                ranked_decisions,
                general_decision,
                stock_regime=stock_regime,
                crypto_regime=crypto_regime,
                portfolio_alerts=portfolio_alerts or [],
                actionable_report=actionable_report,
            )
        )
    lines.extend(_tradeable_today_section(ranked_decisions, actionable=actionable_report))
    lines.extend(_watchlist_only_section(ranked_decisions, actionable=actionable_report))
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
    market_sessions = sorted({decision.market_session for decision in decisions if decision.market_session})
    report_grade = _report_grade(
        report_type=report_type,
        data_mode=data_mode,
        market_sessions=market_sessions,
        generated_at=generated_at,
        has_stale_assets=any(decision.is_stale for decision in decisions),
    )
    diagnostic_main = report_grade == "diagnostic_not_decision_grade"
    general_decision = "no_trade_day" if _is_non_live_mode(data_mode) or diagnostic_main else _general_decision(decisions)
    equity_candidates = [
        decision
        for decision in ranked
        if report_grade == "decision_grade"
        and decision.asset_type == "stock"
        and _final_bucket(decision) in {"tradeable", "watchlist"}
    ][:3]
    equity_research = [
        decision
        for decision in ranked
        if decision.asset_type == "stock"
        and decision.investment_quality_score >= 80
        and _final_bucket(decision) not in {"tradeable", "watchlist", "rejected", "blocked"}
    ][:3]
    if diagnostic_main:
        equity_research = [
            decision
            for decision in ranked
            if decision.asset_type == "stock"
            and decision.investment_quality_score >= 80
            and _final_bucket(decision) not in {"rejected", "blocked"}
        ][:3]
    if report_grade == "close_decision_grade":
        equity_research = [
            decision
            for decision in ranked
            if decision.asset_type == "stock"
            and decision.investment_quality_score >= 80
            and _final_bucket(decision) not in {"rejected", "blocked"}
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
        f"- report_grade: `{report_grade}`",
        *(_report_grade_warnings(report_grade)),
        f"- market_session: `{','.join(market_sessions) if market_sessions else 'unknown'}`",
        f"- bot_general_decision: `{general_decision}`",
        f"- stock_regime: `{stock_regime}`",
        f"- crypto_regime: `{crypto_regime}`",
        "",
        "## Top equity candidates for qualitative review",
        "",
        "technical_unvalidated is not approval to buy. News can explain risk context but cannot approve a trade by itself.",
        "",
    ]
    if not equity_candidates:
        lines.append("No equity candidates for qualitative review")
    else:
        for decision in equity_candidates:
            lines.extend(_analyst_candidate_lines(decision))
    if equity_research:
        lines.extend(
            [
                "",
                "## Equity research queue",
                "",
                "pesquisa qualitativa, nao trade. Usar para entender negocio/valuation; nao e setup aprovado.",
            ]
        )
        for decision in equity_research:
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
        f"- fmp_status: `{provider_budget.get('fmp_status', 'unknown')}`",
        f"- retry_after: `{provider_budget.get('retry_after', 'unknown')}`",
        f"- cache_reused_from_main: `{str(bool(provider_budget.get('cache_reused_from_main', False))).lower()}`",
        f"- close_universe_source: `{provider_budget.get('close_universe_source', 'manual')}`",
        f"- skipped_provider_calls_due_to_cache: {int(provider_budget.get('skipped_provider_calls_due_to_cache', 0) or 0)}",
        f"- skipped_provider_calls_due_to_rate_limit: {int(provider_budget.get('skipped_provider_calls_due_to_rate_limit', 0) or 0)}",
        f"- few_assets_reason: `{provider_budget.get('few_assets_reason', 'other')}`",
    ]
    if provider_budget.get("fmp_status") == "rate_limited":
        lines.extend(
            [
                "",
                "FMP rate limit atingido; relatorio bloqueado ou degradado conforme cache/fallback disponivel.",
            ]
        )
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
    actionable_report: bool,
) -> list[str]:
    tradeable = _bucket(decisions, "tradeable") if actionable_report else []
    watchlist = _bucket(decisions, "watchlist") if actionable_report else []
    research = _bucket(decisions, "research_queue")
    blocked = _bucket(decisions, "blocked")
    rejected = _bucket(decisions, "rejected")
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
        "## Drivers internos do modelo",
        "",
        _internal_driver_summary(decisions, portfolio_alerts),
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
    *,
    report_grade: str,
) -> list[str]:
    tomorrow = [
        decision
        for decision in decisions
        if _final_bucket(decision) in {"tradeable", "watchlist"}
    ]
    blocked_or_remove = [
        decision
        for decision in decisions
        if _final_bucket(decision) in {"blocked", "rejected", "wait", "technical_unvalidated"}
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
        *(
            ["Relatorio de fechamento valido para preparacao do proximo pregao. Nao e gatilho automatico de ordem."]
            if report_grade == "close_decision_grade"
            else []
        ),
        *(
            ["Close report fora da janela valida ou sem sessao regular confirmada; usar apenas como diagnostico."]
            if report_grade == "close_diagnostic"
            else []
        ),
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
    items = _bucket(decisions, "research_queue")
    lines = ["## Research queue", ""]
    lines.extend(_symbol_lines(items, empty="Nenhum ativo em research queue."))
    lines.append("")
    return lines


def _rejected_section(decisions: list[AssetDecision]) -> list[str]:
    items = _bucket(decisions, "rejected")
    lines = ["## Rejected", ""]
    lines.extend(_symbol_lines(items, empty="Nenhum ativo rejected."))
    lines.append("")
    return lines


def _tradeable_today_section(decisions: list[AssetDecision], *, actionable: bool = True) -> list[str]:
    tradeable = _bucket(decisions, "tradeable")
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
    watchlist = _bucket(decisions, "watchlist")
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
    items = _bucket(decisions, "technical_unvalidated")
    lines = ["## Setup tecnico detectado, mas nao validado", ""]
    if not items:
        lines.extend(["Nenhum setup tecnico nao validado.", ""])
        return lines
    lines.extend(
        f"- `{decision.symbol}`: {CONSERVATIVE_TECHNICAL_THESIS}"
        for decision in items
    )
    lines.append("")
    return lines


def _wait_section(decisions: list[AssetDecision]) -> list[str]:
    items = _bucket(decisions, "wait")
    lines = ["## Wait", ""]
    if not items:
        lines.extend(["Nenhum ativo em wait.", ""])
        return lines
    lines.extend(f"- `{decision.symbol}`" for decision in items)
    lines.append("")
    return lines


def _avoid_section(decisions: list[AssetDecision]) -> list[str]:
    items: list[AssetDecision] = []
    lines = ["## Avoid/Rejected", ""]
    if not items:
        lines.extend(["Nenhum ativo rejeitado.", ""])
        return lines
    lines.extend(f"- `{decision.symbol}`" for decision in items)
    lines.append("")
    return lines


def _blocked_section(decisions: list[AssetDecision]) -> list[str]:
    items = _bucket(decisions, "blocked")
    lines = ["## Blocked", ""]
    if not items:
        lines.extend(["Nenhum ativo blocked por dados.", ""])
        return lines
    lines.extend(f"- `{decision.symbol}`" for decision in items)
    lines.append("")
    return lines


def _short_watchlist_section(decisions: list[AssetDecision]) -> list[str]:
    items = _bucket(decisions, "short_observational")
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
    items = _bucket(decisions, "short_observational")
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
        f"  - reason: {_display_thesis(decision)}",
        f"  - risks: {_format_list(sorted(set([*decision.alerts, *decision.limitations])))}",
        f"  - valuation_summary: {'; '.join(decision.metrics_summary) if decision.metrics_summary else 'not_verified'}",
        f"  - earnings_guidance_status: `{_verification_status(decision.event_check_status)}`",
        f"  - news_catalyst_status: `{_verification_status(decision.news_status)}`",
    ]


def _report_grade(
    *,
    report_type: str,
    data_mode: str,
    market_sessions: list[str],
    generated_at: str,
    has_stale_assets: bool,
) -> str:
    if _is_non_live_mode(data_mode):
        return "not_decision_grade"
    normalized = {session.lower() for session in market_sessions if session}
    if has_stale_assets:
        return "close_diagnostic" if report_type == "close" else "diagnostic_not_decision_grade"
    if report_type == "main":
        if normalized != {"regular"}:
            return "diagnostic_not_decision_grade"
        return "decision_grade"
    if report_type == "close":
        if not normalized or not normalized <= {"closed", "after_hours"}:
            return "close_diagnostic"
        if _is_valid_close_window(generated_at):
            return "close_decision_grade"
        return "close_diagnostic"
    return "decision_grade"


def _report_grade_warnings(report_grade: str) -> list[str]:
    if report_grade == "diagnostic_not_decision_grade":
        return ["- AVISO: main report fora do horario regular; usar apenas como diagnostico"]
    if report_grade == "close_decision_grade":
        return [
            "- Nota: Relatorio de fechamento valido para preparacao do proximo pregao. "
            "Nao e gatilho automatico de ordem. Sem ordem automatica, sem broker e sem compra automatica."
        ]
    if report_grade == "close_diagnostic":
        return ["- AVISO: Close report fora da janela valida ou sem sessao regular confirmada; usar apenas como diagnostico."]
    return []


def _is_valid_close_window(generated_at: str) -> bool:
    generated_brt = _parse_generated_at_to_brt(generated_at)
    if generated_brt is None:
        return False
    market_date = generated_brt.date()
    if not _is_us_market_trading_day(market_date):
        return False
    regular_close_brt = datetime.combine(market_date, _regular_close_time_brt(market_date), tzinfo=BRT)
    return regular_close_brt <= generated_brt <= regular_close_brt + CLOSE_WINDOW_AFTER_REGULAR_CLOSE


def _regular_close_time_brt(day: date) -> time:
    return time(17, 0) if _is_us_dst(day) else time(18, 0)


def _is_us_dst(day: date) -> bool:
    starts = _nth_weekday(day.year, 3, 6, 2)
    ends = _nth_weekday(day.year, 11, 6, 1)
    return starts <= day < ends


def _parse_generated_at_to_brt(generated_at: str) -> datetime | None:
    try:
        normalized = generated_at.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRT)
    return parsed.astimezone(BRT)


def _is_us_market_trading_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    return day not in _us_market_holidays(day.year)


def _us_market_holidays(year: int) -> set[date]:
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    return {holiday for holiday in holidays if holiday.year == year}


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(days=7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    current = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _bucket(decisions: list[AssetDecision], name: str) -> list[AssetDecision]:
    return [decision for decision in decisions if _final_bucket(decision) == name]


def _final_bucket(decision: AssetDecision) -> str:
    if decision.decision == "blocked":
        return "blocked"
    if decision.decision in {"avoid", "rejected"}:
        return "rejected"
    if decision.decision == "technical_unvalidated":
        return "technical_unvalidated"
    if decision.decision in {"wait", "watch_only"}:
        return "wait"
    if decision.decision == "speculative_watch":
        return "research_queue"
    if decision.decision == "watch_buy":
        return "watchlist"
    if decision.decision in {"tradeable", "strong_buy_candidate"}:
        return "tradeable"
    if decision.short_status == "watch_only" and decision.short_setup_score > 0:
        return "short_observational"
    return "wait"


def _display_thesis(decision: AssetDecision) -> str:
    if decision.decision == "technical_unvalidated" and _needs_conservative_technical_thesis(decision):
        return CONSERVATIVE_TECHNICAL_THESIS
    return decision.thesis


def _needs_conservative_technical_thesis(decision: AssetDecision) -> bool:
    stats = decision.backtest_stats
    expected_value = stats.expected_value_r if stats is not None else None
    limitations = set(decision.limitations)
    has_missing_flow_or_news = any(
        "flow" in limitation
        or "cvd" in limitation
        or "open_interest" in limitation
        or "liquidation" in limitation
        or "news" in limitation
        for limitation in limitations
    )
    return (
        decision.missing_data_severity in {"high", "blocking", "critical"}
        or (expected_value is not None and expected_value < 0)
        or "negative_ev_with_high_data_severity" in decision.alerts
        or "negative_ev_with_high_data_severity" in limitations
        or has_missing_flow_or_news
    )


def _themes_for(decisions: list[AssetDecision]) -> list[str]:
    themes = []
    for decision in decisions:
        benchmark = decision.sector_benchmark or decision.asset_type
        if benchmark:
            themes.append(benchmark)
    return sorted(set(themes))


def _internal_driver_summary(decisions: list[AssetDecision], portfolio_alerts: list[str]) -> str:
    alerts = sorted(set([*portfolio_alerts, *[alert for decision in decisions for alert in decision.alerts]]))
    if not alerts:
        return "Sem news/macro real coletado nesta V1; nenhum driver interno relevante."
    return f"Sem news/macro real coletado nesta V1; drivers internos do modelo: {_format_list(alerts)}"


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
        f"- Tese: {_display_thesis(decision)}",
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
        *(
            ["- short_note: short observacional; nao operacional"]
            if decision.short_status == "watch_only" and decision.short_setup_score > 0
            else []
        ),
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
