from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from advisor.report import (
    _expected_regular_market_window,
    _format_market_session_sources,
    _format_datetime_diagnostic,
    _parse_generated_at_to_brt,
    _possible_session_detection_bug,
    normalize_market_session,
)

CRYPTO_WATCH_DECISIONS = {"crypto_research_only", "crypto_watch_context", "watch_pending_flow_confirmation"}
CRYPTO_DECISION_PRIORITY = {
    "crypto_research_only": 0,
    "crypto_watch_context": 1,
    "watch_pending_flow_confirmation": 2,
}
CRYPTO_TICKER_PRIORITY = {"HYPE": 0, "BTC": 1, "ETH": 2, "SOL": 3}
CONFIGURED_STOCKS = ("INTC", "AMD", "NVDA", "HIMS", "MU", "MSFT", "USAR", "CRDO", "DELL", "MRVL", "HOOD")
CONFIGURED_CRYPTOS = ("SOL", "HYPE", "BTC", "ETH")
CONFIGURED_TICKERS = (*CONFIGURED_STOCKS, *CONFIGURED_CRYPTOS)


@dataclass(frozen=True)
class AssetReview:
    ticker: str
    asset_type: str
    decision: str
    thesis: str
    confirms: str
    contradicts: str
    valuation: str
    events_news: str
    risks: str
    path_to_operation: str
    basic_data_status: str = ""
    flow_data_status: str = ""
    binance_status: str = ""
    metrics: str = ""
    data_quality: str = ""
    source_decision: str = ""
    review_status: str = ""
    review_reason: str = ""
    blockers: tuple[str, ...] = ()
    source_report: str = "main"
    universe_origin: str = "unknown"
    is_stale: bool = False
    provider: str = "unknown"
    collection_status: str = "unknown"
    entry: str = "not_present_in_input"
    stop_invalidation: str = "not_present_in_input"
    position_sizing: str = "not_present_in_input"


@dataclass(frozen=True)
class MainReviewContext:
    run_id: str
    head_sha: str
    brt_date: str
    generated_at: str
    data_mode: str
    primary_report_grade: str
    overall_report_grade: str
    primary_market_session: str
    discovery_coverage_grade: str
    stale_asset_count_primary: int
    provider_status: str
    artifact_valid: bool
    blocking_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ReviewPackage:
    main_context: MainReviewContext
    close_context: MainReviewContext | None
    main_assets: list[AssetReview]
    close_assets: list[AssetReview]
    main_markdown: str
    close_markdown: str


def parse_review_package(
    nightly_markdown: str,
    *,
    extra_markdowns: list[str] | None = None,
) -> ReviewPackage:
    main_markdown = ""
    close_markdown = ""
    for markdown in extra_markdowns or []:
        report_type = _field_any(markdown, "report_type").lower()
        if report_type == "main" and not main_markdown:
            main_markdown = markdown
        elif report_type == "close" and not close_markdown:
            close_markdown = markdown
    raw_main_available = bool(main_markdown)
    main_context = _parse_main_review_context(
        nightly_markdown,
        main_markdown,
        raw_main_available=raw_main_available,
    )
    return ReviewPackage(
        main_context=main_context,
        close_context=_parse_close_review_context(nightly_markdown, close_markdown) if close_markdown else None,
        main_assets=_parse_source_assets(main_markdown or nightly_markdown, "main"),
        close_assets=_parse_source_assets(close_markdown, "close") if close_markdown else [],
        main_markdown=main_markdown,
        close_markdown=close_markdown,
    )


def _parse_close_review_context(nightly_markdown: str, close_markdown: str) -> MainReviewContext:
    summary = _section_text(nightly_markdown, "Close summary")
    context_text = "\n".join([summary, close_markdown])
    artifact_valid_field = _field_any(nightly_markdown, "artifact_valid")
    artifact_valid = artifact_valid_field.lower() == "true"
    report_grade = _field_any(summary, "primary_report_grade", "report_grade") or _field_any(
        close_markdown, "primary_report_grade", "report_grade"
    )
    session = normalize_market_session(
        _field_any(summary, "primary_market_session", "market_session_primary", "market_session")
        or _field_any(close_markdown, "primary_market_session", "market_session_primary", "market_session")
        or "unknown"
    ).primary
    return MainReviewContext(
        run_id=_field_any(nightly_markdown, "close_run_id") or _run_id_from_workflow_line(nightly_markdown, "close"),
        head_sha=_field_any(nightly_markdown, "close_head_sha"),
        brt_date=_field_any(nightly_markdown, "brt_date", "BRT date"),
        generated_at=_field_any(nightly_markdown, "close_generated_at") or _field_any(
            context_text, "generated_at", "Generated at"
        ),
        data_mode=_field_any(summary, "Data mode", "data_mode") or _field_any(close_markdown, "Data mode", "data_mode"),
        primary_report_grade=report_grade,
        overall_report_grade=_field_any(summary, "overall_report_grade")
        or _field_any(close_markdown, "overall_report_grade")
        or report_grade,
        primary_market_session=session,
        discovery_coverage_grade=_field_any(summary, "discovery_coverage_grade")
        or _field_any(close_markdown, "discovery_coverage_grade")
        or "not_present_in_input",
        stale_asset_count_primary=_int_field_any(summary, "stale_asset_count_primary"),
        provider_status=_field_any(summary, "provider_rate_limit_status")
        or _field_any(close_markdown, "provider_rate_limit_status")
        or "not_present_in_input",
        artifact_valid=artifact_valid,
        blocking_reasons=(),
    )


def _parse_main_review_context(
    nightly_markdown: str,
    main_markdown: str,
    *,
    raw_main_available: bool,
) -> MainReviewContext:
    summary = _section_text(nightly_markdown, "Main summary")
    context_text = "\n".join([summary, main_markdown])
    run_id = _field_any(nightly_markdown, "main_run_id") or _run_id_from_workflow_line(nightly_markdown, "main")
    head_sha = _field_any(nightly_markdown, "main_head_sha")
    brt_date = _field_any(nightly_markdown, "brt_date", "BRT date")
    generated_at = _field_any(nightly_markdown, "main_generated_at") or _field_any(
        context_text, "generated_at", "Generated at"
    )
    data_mode = _field_any(summary, "Data mode", "data_mode") or _field_any(main_markdown, "Data mode", "data_mode")
    primary_grade = _field_any(summary, "primary_report_grade") or _field_any(
        summary, "report_grade"
    ) or _field_any(main_markdown, "primary_report_grade", "report_grade")
    overall_grade = _field_any(summary, "overall_report_grade") or _field_any(
        main_markdown, "overall_report_grade"
    ) or primary_grade
    primary_session_raw = _field_any(summary, "primary_market_session", "market_session_primary", "market_session") or _field_any(
        main_markdown, "primary_market_session", "market_session_primary", "market_session"
    )
    primary_session = normalize_market_session(primary_session_raw or "unknown").primary
    discovery_grade = _field_any(summary, "discovery_coverage_grade") or _field_any(
        main_markdown, "discovery_coverage_grade"
    ) or "not_present_in_input"
    stale_primary = _int_field_any(summary, "stale_asset_count_primary")
    if stale_primary == 0 and not _field_any(summary, "stale_asset_count_primary"):
        stale_primary = _int_field_any(main_markdown, "stale_asset_count_primary", "stale_price_count", "stale_assets")
    provider_status = _field_any(summary, "provider_rate_limit_status") or _field_any(
        main_markdown, "provider_rate_limit_status"
    ) or "not_present_in_input"
    artifact_valid_field = _field_any(nightly_markdown, "artifact_valid")
    artifact_valid = artifact_valid_field.lower() == "true"
    reasons = _split_compact_field(_field_any(summary, "blocking_reasons"))
    if not artifact_valid:
        reasons.append("artifact_mismatch")
    if not raw_main_available:
        artifact_valid = False
        reasons.append("artifact_mismatch")
    if primary_grade != "decision_grade":
        reasons.append("main_primary_not_decision_grade")
    if primary_session != "regular":
        reasons.append("invalid_market_session")
    if stale_primary > 0:
        reasons.append("stale_primary_data")
    if provider_status not in {"ok", "not_used"}:
        reasons.append("missing_required_provider")
    required = (run_id, head_sha, brt_date, generated_at, data_mode, primary_grade, primary_session)
    if any(not value for value in required):
        artifact_valid = False
        reasons.append("artifact_mismatch")
    return MainReviewContext(
        run_id=run_id,
        head_sha=head_sha,
        brt_date=brt_date,
        generated_at=generated_at,
        data_mode=data_mode,
        primary_report_grade=primary_grade,
        overall_report_grade=overall_grade,
        primary_market_session=primary_session,
        discovery_coverage_grade=discovery_grade,
        stale_asset_count_primary=stale_primary,
        provider_status=provider_status,
        artifact_valid=artifact_valid,
        blocking_reasons=tuple(dict.fromkeys(reason for reason in reasons if reason and reason != "nenhum")),
    )


def _parse_source_assets(markdown: str, source_report: str) -> list[AssetReview]:
    parsed: list[AssetReview] = []
    for block in _asset_blocks(markdown):
        legacy = _classify_asset(block)
        if legacy is None:
            continue
        source_decision = (_field(block, "decision_label") or _field(block, "Decisao")).lower()
        blockers = _structured_asset_blockers(block)
        parsed.append(
            replace(
                legacy,
                source_decision=source_decision,
                review_status=_review_status_from_source(source_decision),
                review_reason=_review_reason_from_source(source_decision),
                blockers=tuple(blockers),
                source_report=source_report,
                universe_origin=_field(block, "universe_origin") or "unknown",
                is_stale=_field(block, "is_stale").lower() in {"yes", "true"},
                provider=_field(block, "provider") or _field(block, "Data source") or "unknown",
                collection_status=_collection_status(block, source_decision),
                entry=_field(block, "Entrada ideal") or "not_present_in_input",
                stop_invalidation=_field(block, "Stop/invalidation") or "not_present_in_input",
                position_sizing=_field(block, "Tamanho maximo da posicao") or "not_present_in_input",
            )
        )
    return _dedupe_source_assets(parsed)


def _dedupe_source_assets(assets: list[AssetReview]) -> list[AssetReview]:
    by_ticker: dict[str, AssetReview] = {}
    order: list[str] = []
    for asset in assets:
        if asset.ticker not in by_ticker:
            order.append(asset.ticker)
        by_ticker[asset.ticker] = asset
    return [by_ticker[ticker] for ticker in order]


def _structured_asset_blockers(block: str) -> list[str]:
    blockers = _split_compact_field(_field(block, "blocking_reasons"))
    if _field(block, "is_stale").lower() in {"yes", "true"}:
        blockers.append("stale_asset")
    if _field(block, "data_quality").lower() == "blocked":
        blockers.append("blocked_data_quality")
    if _field(block, "missing_data_severity").lower() == "critical":
        blockers.append("critical_missing_data")
    for field_name in ("earnings_status", "guidance_status", "news_status"):
        value = _field(block, field_name).lower()
        if value in {"schema_error", "provider_error", "invalid_payload"}:
            blockers.append(f"{field_name}:{value}")
    return list(dict.fromkeys(reason for reason in blockers if reason and reason != "nenhum"))


def _collection_status(block: str, source_decision: str) -> str:
    lowered = block.lower()
    if "empty_provider_response" in lowered:
        return "empty_provider_response"
    if "asset_not_resolved" in lowered:
        return "asset_not_resolved"
    if source_decision == "blocked":
        return "blocked"
    return "collected"


def _review_status_from_source(source_decision: str) -> str:
    mapping = {
        "tradeable": "tradeable_from_main_pending_integrity",
        "watch_buy": "watch_buy_from_main",
        "technical_unvalidated": "technical_unvalidated_from_main",
        "wait": "wait_from_main",
        "avoid": "avoid_from_main",
        "blocked": "blocked_from_main",
    }
    return mapping.get(source_decision, f"{source_decision or 'unknown'}_from_main")


def _review_reason_from_source(source_decision: str) -> str:
    return f"Preserva a decisao {source_decision or 'unknown'} produzida pelo scoring do main."


def _split_compact_field(value: str) -> list[str]:
    return [piece.strip() for piece in value.strip("` []").split(",") if piece.strip()]


def _run_id_from_workflow_line(markdown: str, report_type: str) -> str:
    match = re.search(rf"(?m)^- {re.escape(report_type)}:\s*run_id=([0-9]+)", markdown)
    return match.group(1) if match else ""


def main_blocks_operation(context: MainReviewContext, *, close_markdown: str = "") -> bool:
    del close_markdown
    return bool(context.blocking_reasons) or not context.artifact_valid


def _main_blocks_operation(context: MainReviewContext) -> bool:
    return main_blocks_operation(context)


def generate_analyst_final_review(
    nightly_markdown: str,
    *,
    extra_markdowns: list[str] | None = None,
    public_equity_executed: bool = False,
) -> str:
    package = parse_review_package(nightly_markdown, extra_markdowns=extra_markdowns)
    context = package.main_context
    main_primary_blocked = main_blocks_operation(context)
    primary_assets = [asset for asset in package.main_assets if asset.universe_origin != "discovery"]
    discovery_assets = [asset for asset in package.main_assets if asset.universe_origin == "discovery"]
    confirmed_tradeables = [
        replace(
            asset,
            review_status="tradeable_confirmed_from_main",
            review_reason="decisao originada no scoring do main e confirmada pela integridade do artefato",
        )
        for asset in primary_assets
        if asset.source_decision == "tradeable" and not asset.blockers and not asset.is_stale and not main_primary_blocked
    ]
    confirmed_tickers = {asset.ticker for asset in confirmed_tradeables}
    rendered_primary = [
        next((confirmed for confirmed in confirmed_tradeables if confirmed.ticker == asset.ticker), asset)
        for asset in primary_assets
    ]
    final_decision = "tradeable" if confirmed_tradeables else "no_trade"
    tradeable_assets = ",".join(asset.ticker for asset in confirmed_tradeables) or "nenhum"
    decision_counts = _source_decision_counts(package.main_assets)
    public_equity_note = (
        "execucao externa declarada pelo chamador"
        if public_equity_executed
        else "nenhuma validacao externa/plugin foi executada"
    )

    lines: list[str] = [
        "# Rule-Based Final Review",
        "",
        f"- public_equity_executed: {str(public_equity_executed).lower()}",
        f"Public Equity Investing executed: {str(public_equity_executed).lower()}",
        f"- review_method: `local_structured_rules`",
        f"- review_note: {public_equity_note}",
        "Esta e uma revisao baseada em regras locais, nao uma analise externa/plugin.",
        "",
        "## Decisao geral para o proximo pregao",
        "",
        f"* {final_decision}",
        "",
        f"- tradeable_count: {len(confirmed_tradeables)}",
        f"- tradeable_assets: `{tradeable_assets}`",
        f"- main_primary_blocked: {str(main_primary_blocked).lower()}",
        f"- primary_report_grade: `{context.primary_report_grade or 'missing'}`",
        f"- overall_report_grade: `{context.overall_report_grade or 'missing'}`",
        f"- discovery_coverage_grade: `{context.discovery_coverage_grade or 'missing'}`",
        f"- artifact_valid: {str(context.artifact_valid).lower()}",
        f"- blocking_reasons: `{','.join(context.blocking_reasons) or 'nenhum'}`",
        "",
        (
            "Tradeables confirmados: " + ", ".join(asset.ticker for asset in confirmed_tradeables) + "."
            if confirmed_tradeables
            else "Nenhum tradeable no main selecionado."
        ),
        "Seguranca operacional: sem broker; sem ordem automatica; sem compra automatica.",
        "",
        "## Proveniencia do main",
        "",
        f"- main_run_id: `{context.run_id or 'missing'}`",
        f"- main_head_sha: `{context.head_sha or 'missing'}`",
        f"- brt_date: `{context.brt_date or 'missing'}`",
        f"- generated_at: `{context.generated_at or 'missing'}`",
        f"- data_mode: `{context.data_mode or 'missing'}`",
        f"- primary_market_session: `{context.primary_market_session or 'missing'}`",
        f"- stale_asset_count_primary: {context.stale_asset_count_primary}",
        f"- provider_status: `{context.provider_status or 'missing'}`",
        "",
        "## Decisoes originais do main",
        "",
        *[f"- {decision}: {count}" for decision, count in decision_counts.items()],
        "",
    ]
    lines.extend(_structured_asset_lines(rendered_primary, confirmed_tickers))
    lines.extend(["", "## Comparacao main vs close", ""])
    lines.extend(_main_close_comparison_lines(package.main_assets, package.close_assets))
    lines.extend(["", "## Discovery coverage", "", "- impact_on_primary_report: false"])
    if discovery_assets:
        lines.extend(_discovery_asset_lines(discovery_assets))
    else:
        lines.append("- assets: `nenhum`")
    lines.extend(["", "## Dez perguntas operacionais", ""])
    lines.extend(_operational_question_lines(context, package.main_assets, package.close_assets))
    lines.extend(
        [
            "",
            "## Limites desta revisao",
            "",
            "- Preserva `source_decision`; nao executa scoring nem classify_asset.",
            "- Labels legadas aparecem apenas como `legacy_label`, sem substituir a decisao do main.",
            "- O close serve apenas para comparacao temporal e nunca bloqueia o main.",
            "- Discovery e diagnostico de cobertura; nao rebaixa o grade primario.",
            "- `not_implemented` e status de campo, nao bloqueio generico.",
        ]
    )
    lines.extend(["", *_legacy_compatibility_lines(package, nightly_markdown, final_decision)])
    return "\n".join(lines).strip() + "\n"


def _source_decision_counts(assets: list[AssetReview]) -> dict[str, int]:
    counts = {decision: 0 for decision in ("tradeable", "watch_buy", "technical_unvalidated", "wait", "avoid", "blocked")}
    for asset in assets:
        counts.setdefault(asset.source_decision or "unknown", 0)
        counts[asset.source_decision or "unknown"] += 1
    return counts


def _legacy_compatibility_lines(
    package: ReviewPackage,
    nightly_markdown: str,
    final_decision: str,
) -> list[str]:
    """Keep the useful human-oriented V2 sections without using them as decision authority."""
    assets = package.main_assets
    equities = [asset for asset in assets if asset.asset_type == "stock"]
    cryptos = [asset for asset in assets if asset.asset_type == "crypto"]
    rejected_or_blocked = [asset for asset in assets if asset.decision in {"rejected", "blocked"}]
    top_equities = _top_equities_to_watch(assets)
    top_crypto = _top_crypto_to_watch(assets)
    top_candidates = _top_candidates_to_watch(assets)
    context_markdown = nightly_markdown
    if package.main_markdown != nightly_markdown:
        context_markdown += "\n\n" + package.main_markdown
    report_data_grade = _report_data_grade(context_markdown, assets)
    trade_readiness = "tradeable" if final_decision == "tradeable" else "no_trade"
    input_completeness = _nightly_input_completeness(nightly_markdown, equities, cryptos)
    market_brief_status = _market_brief_status(assets)
    main_diagnostic_lines = _main_decision_grade_diagnostic_lines(context_markdown)
    main_blocked = main_blocks_operation(package.main_context)
    lines = [
        "## Data/readiness summary",
        "",
        f"- report_data_grade: `{report_data_grade}`",
        f"- trade_readiness: `{trade_readiness}`",
        f"- operational_decision: `{final_decision}`",
        f"- main_report_grade_blocked_operation: {str(main_blocked).lower()}",
        "",
        "## Leitura objetiva",
        "",
        *_objective_reading_lines(final_decision, assets, top_candidates, main_blocked, input_completeness["incomplete"]),
        "",
        "## Resumo do dia",
        "",
        f"* Decisao operacional: {final_decision}.",
        f"* Report data grade: {report_data_grade}.",
        f"* Trade readiness: {trade_readiness}.",
        f"* Top equities: {_watch_summary(top_equities)}.",
        f"* Top crypto: {_watch_summary(top_crypto)}.",
        "",
        *_nightly_input_completeness_lines(input_completeness),
        "",
        *_market_brief_lines(context_markdown, assets),
        "",
        *_coverage_universe_lines(assets),
        "",
    ]
    if main_blocked and main_diagnostic_lines:
        lines.extend([*main_diagnostic_lines, ""])
    if final_decision == "tradeable":
        lines.extend(
            [
                "## Legacy presentation",
                "",
                "Legacy watch/research labels omitted because the main has a confirmed tradeable; source_decision remains authoritative.",
                "",
                "## Telegram summary",
                "",
                _telegram_summary(
                    final_decision,
                    assets,
                    report_data_grade=report_data_grade,
                    trade_readiness=trade_readiness,
                    market_brief_status=market_brief_status,
                    input_incomplete=input_completeness["incomplete"],
                    main_diagnostic_lines=main_diagnostic_lines,
                ),
            ]
        )
        return lines
    lines.extend(["## Equity review", ""])
    lines.extend(_asset_lines(equities) if equities else ["Nenhuma equity candidata para review."])
    lines.extend(["", "## Crypto review", "", "Separado de equities; as labels abaixo sao legadas e apenas descritivas.", ""])
    lines.extend(_asset_lines(cryptos) if cryptos else ["Nenhum cripto ativo para review."])
    lines.extend(["", "## Top candidates to watch tomorrow", ""])
    if top_candidates:
        lines.append(
            f"Decisao operacional: {final_decision}. Porem, para observacao amanha, os melhores candidatos sao "
            f"{_format_top_ticker_list(top_candidates)}. "
            + ("A aprovacao operacional permanece a decisao original do main." if final_decision == "tradeable" else "Nenhum esta aprovado para entrada.")
        )
        lines.append("")
        for asset in top_candidates:
            lines.extend(_top_candidate_lines(asset))
    else:
        lines.append("Nenhum ativo prioritario para watch/research.")
    lines.extend(["", "## Top equities to watch tomorrow", ""])
    for asset in top_equities:
        lines.extend(_top_candidate_lines(asset))
    if not top_equities:
        lines.append("Nenhuma equity prioritaria para watch/research.")
    lines.extend(["", "## Top crypto to watch tomorrow", ""])
    for asset in top_crypto:
        lines.extend(_top_candidate_lines(asset))
    if not top_crypto:
        lines.append("Nenhum cripto elegivel para watch/research.")
    lines.extend(["", "## Rejected/blocked", ""])
    if rejected_or_blocked:
        lines.extend(f"* {asset.ticker}: {asset.decision}. {asset.contradicts}" for asset in rejected_or_blocked[:5])
    else:
        lines.append("Nenhum ativo rejected/blocked.")
    if final_decision != "tradeable":
        lines.extend(["", "Nenhum ativo aprovado como tradeable."])
    lines.extend(
        [
            "",
            "## Telegram summary",
            "",
            _telegram_summary(
                final_decision,
                assets,
                report_data_grade=report_data_grade,
                trade_readiness=trade_readiness,
                market_brief_status=market_brief_status,
                input_incomplete=input_completeness["incomplete"],
                main_diagnostic_lines=main_diagnostic_lines,
            ),
        ]
    )
    return lines


def _structured_asset_lines(assets: list[AssetReview], confirmed_tickers: set[str]) -> list[str]:
    lines: list[str] = []
    for asset in assets:
        lines.extend(
            [
                f"### {asset.ticker}",
                "",
                f"- source_decision: `{asset.source_decision or 'unknown'}`",
                f"- review_status: `{asset.review_status}`",
                f"- review_reason: {asset.review_reason}",
                f"- legacy_label: `{asset.decision or 'none'}`",
                f"- source_report: `{asset.source_report}`",
                f"- universe_origin: `{asset.universe_origin}`",
                f"- stale: {str(asset.is_stale).lower()}",
                f"- blockers: `{','.join(asset.blockers) or 'nenhum'}`",
                f"- tradeable_confirmed: {str(asset.ticker in confirmed_tickers).lower()}",
                f"- entry_from_main: `{asset.entry}`",
                f"- stop_invalidation_from_main: `{asset.stop_invalidation}`",
                f"- sizing_from_main: `{asset.position_sizing}`",
                "",
            ]
        )
    return lines or ["Nenhum ativo primario encontrado no main."]


def _discovery_asset_lines(assets: list[AssetReview]) -> list[str]:
    lines: list[str] = []
    for asset in assets:
        reason = ",".join(asset.blockers) or asset.contradicts or asset.review_reason
        lines.extend(
            [
                f"### {asset.ticker}",
                f"- origin: `{asset.universe_origin}`",
                f"- source_decision: `{asset.source_decision or 'unknown'}`",
                f"- collection_status: `{asset.collection_status}`",
                f"- provider: `{asset.provider}`",
                f"- reason: {reason}",
                "- impact_on_primary_report: false",
                "",
            ]
        )
    return lines


def _main_close_comparison_lines(main_assets: list[AssetReview], close_assets: list[AssetReview]) -> list[str]:
    close_by_ticker = {asset.ticker: asset for asset in close_assets}
    lines: list[str] = []
    for asset in main_assets:
        close = close_by_ticker.get(asset.ticker)
        close_decision = close.source_decision if close else "not_present"
        change = "unchanged" if close and close_decision == asset.source_decision else "changed_at_close" if close else "not_comparable"
        change_reason = (
            "source_decision_unchanged"
            if change == "unchanged"
            else "source_decision_changed_in_close"
            if change == "changed_at_close"
            else "asset_not_present_in_close"
        )
        lines.extend(
            [
                f"### {asset.ticker}",
                f"- main_decision: `{asset.source_decision or 'unknown'}`",
                f"- close_decision: `{close_decision}`",
                f"- decision_change: `{change}`",
                f"- change_reason: `{change_reason}`",
                "",
            ]
        )
    return lines or ["Nenhuma decisao do main disponivel para comparacao."]


def _operational_question_lines(
    context: MainReviewContext,
    main_assets: list[AssetReview],
    close_assets: list[AssetReview],
) -> list[str]:
    primary_assets = [asset for asset in main_assets if asset.universe_origin != "discovery"]
    counts = _source_decision_counts(main_assets)
    tradeables = [
        asset.ticker
        for asset in primary_assets
        if asset.source_decision == "tradeable"
        and not asset.blockers
        and not asset.is_stale
        and not main_blocks_operation(context)
    ]
    discovery = [asset for asset in main_assets if asset.universe_origin == "discovery"]
    close_by_ticker = {asset.ticker: asset for asset in close_assets}
    close_changes = [
        asset.ticker
        for asset in main_assets
        if asset.ticker in close_by_ticker and asset.source_decision != close_by_ticker[asset.ticker].source_decision
    ]
    return [
        f"1. O main principal foi decision-grade? {'sim' if context.primary_report_grade == 'decision_grade' else 'nao'} (`{context.primary_report_grade or 'missing'}`).",
        f"2. A selecao dos artifacts foi valida? {'sim' if context.artifact_valid else 'nao'}.",
        f"3. Quantos ativos eram tradeable? {counts.get('tradeable', 0)} ({', '.join(tradeables) or 'nenhum'}).",
        f"4. Quantos ativos eram watch_buy? {counts.get('watch_buy', 0)}.",
        f"5. Quantos ativos eram technical_unvalidated? {counts.get('technical_unvalidated', 0)}.",
        f"6. Quantos ativos eram wait? {counts.get('wait', 0)}.",
        f"7. Quantos ativos eram avoid? {counts.get('avoid', 0)}.",
        f"8. Quantos ativos eram blocked? {counts.get('blocked', 0)}.",
        "9. Quais problemas estavam apenas no discovery? "
        + (", ".join(f"{asset.ticker}:{asset.collection_status}" for asset in discovery) or "nenhum")
        + "; impacto no grade primario=false.",
        f"10. O que mudou no close? {', '.join(close_changes) or 'nenhuma source_decision'}.",
    ]


def _objective_reading_lines(
    final_decision: str,
    assets: list[AssetReview],
    top_candidates: list[AssetReview],
    main_not_decision_grade: bool,
    input_incomplete: bool,
) -> list[str]:
    if final_decision == "tradeable":
        tradeables = [asset.ticker for asset in assets if asset.source_decision == "tradeable"]
        return [
            f"* Decisao pratica: tradeable confirmado a partir do main ({', '.join(tradeables) or 'ativo listado acima'}).",
            "* Origem da aprovacao: scoring do main com artifact e contexto primario validos.",
            "* Execucao: exclusivamente manual; esta revisao nao envia ordem nem altera o plano de risco.",
            "* Stop/invalidation e sizing: usar exatamente os campos preservados do main.",
            "* Proximo passo pratico: revisar o plano do main e decidir manualmente; sem broker ou compra automatica.",
        ]
    reason = "main nao decision-grade e dados de news/earnings/flow ainda nao verificados" if main_not_decision_grade else "checks pendentes ainda nao liberam entrada"
    if top_candidates:
        ranking = _telegram_candidate_summary(top_candidates)
    elif input_incomplete:
        ranking = "indisponivel porque nightly_input_incomplete=true"
    else:
        ranking = "nenhum candidato qualificado pelo pacote atual"
    return [
        f"* Decisao pratica: {final_decision}. Nao ha entrada aprovada.",
        f"* Por que nao operar: {reason}.",
        f"* Ranking inicial: {ranking}.",
        f"* Evitar/rejeitados: {_telegram_rejected_summary(assets)}.",
        f"* Proximo passo pratico: esperar main decision-grade, validar noticias/eventos e liberar fluxo cripto antes de transformar watch/research em operacao manual.",
    ]


def _top_equities_to_watch(assets: list[AssetReview]) -> list[AssetReview]:
    allowed = {"watch_pending_checks", "research_only"}
    priority = {
        "watch_pending_checks": 0,
        "research_only": 1,
    }
    candidates = [asset for asset in assets if asset.asset_type == "stock" and asset.decision in allowed]
    return sorted(candidates, key=lambda asset: (priority.get(asset.decision, 9), _ticker_priority(asset.ticker), asset.ticker))[:5]


def _top_candidates_to_watch(assets: list[AssetReview]) -> list[AssetReview]:
    return [*_top_equities_to_watch(assets), *_top_crypto_to_watch(assets)][:5]


def _top_crypto_to_watch(assets: list[AssetReview]) -> list[AssetReview]:
    candidates = [asset for asset in assets if asset.asset_type == "crypto" and asset.decision in CRYPTO_WATCH_DECISIONS]
    return sorted(
        candidates,
        key=lambda asset: (
            CRYPTO_DECISION_PRIORITY.get(asset.decision, 9),
            CRYPTO_TICKER_PRIORITY.get(asset.ticker, 99),
            asset.ticker,
        ),
    )[:4]


def _top_candidate_lines(asset: AssetReview) -> list[str]:
    return [
        f"- ticker: {asset.ticker}",
        f"  label: {asset.decision}",
        f"  por que entrou no top: {asset.thesis}",
        f"  o que falta verificar: {_missing_check_for(asset)}",
        f"  o que invalidaria: {asset.contradicts}",
        f"  proximo check objetivo: {_next_objective_check(asset)}",
        f"  prioridade: {_candidate_priority(asset)}",
        "",
    ]


def _missing_check_for(asset: AssetReview) -> str:
    if asset.asset_type == "crypto":
        return "flow/derivatives, news e regime cripto ainda precisam ser confirmados."
    return "news, earnings/guidance, valuation, risco de gap e main decision-grade."


def _next_objective_check(asset: AssetReview) -> str:
    if asset.asset_type == "crypto":
        return "Confirmar CVD, OI, liquidations, premium e news no proximo main."
    return "Confirmar news/earnings, valuation e sessao regular no proximo main."


def _candidate_priority(asset: AssetReview) -> str:
    if asset.decision == "watch_pending_checks":
        return "high"
    if asset.decision == "crypto_research_only":
        return "high"
    if asset.decision == "crypto_watch_context":
        return "medium"
    return "low"


def _format_top_ticker_list(assets: list[AssetReview]) -> str:
    return ", ".join(asset.ticker for asset in assets)


def _ticker_priority(ticker: str) -> int:
    if ticker in CONFIGURED_TICKERS:
        return CONFIGURED_TICKERS.index(ticker)
    return 99


def _report_data_grade(markdown: str, assets: list[AssetReview]) -> str:
    main_summary = _section_text(markdown, "Main summary")
    if not main_summary:
        return "blocked_data"
    data_mode = (_field_any(main_summary, "data_mode", "Data mode") or "").lower()
    if data_mode in {"blocked", "demo", "fixture"}:
        return "blocked_data"
    session_info = normalize_market_session(
        _field_any(main_summary, "market_session_sources")
        or _field_any(main_summary, "market_session_primary")
        or _field_any(main_summary, "market_session")
        or "unknown"
    )
    provider_status = (_field_any(main_summary, "provider_rate_limit_status") or "ok").lower()
    fmp_status = (_field_any(main_summary, "fmp_status") or "ok").lower()
    coingecko_status = (_field_any(main_summary, "coingecko_status") or "ok").lower()
    fresh_price_count = _int_field_any(main_summary, "fresh_price_count")
    missing_price_count = _int_field_any(main_summary, "missing_price_count")
    report_grade = (_field_any(main_summary, "report_grade") or "").lower()
    provider_ok = all(status in {"ok", "not_present_in_input", "not_used"} for status in (provider_status, fmp_status, coingecko_status))
    coverage_ok = _has_configured_coverage(assets) or (fresh_price_count > 0 and missing_price_count == 0)

    if (
        data_mode == "live"
        and session_info.primary == "regular"
        and not session_info.conflict
        and provider_ok
        and coverage_ok
        and report_grade in {"decision_grade", "diagnostic_not_decision_grade", ""}
    ):
        return "decision_grade"
    if data_mode == "live" and provider_ok and (fresh_price_count > 0 or bool(assets)):
        return "partial_data"
    return "blocked_data"


def _has_configured_coverage(assets: list[AssetReview]) -> bool:
    present = {asset.ticker for asset in assets}
    return all(ticker in present for ticker in CONFIGURED_TICKERS)


def _trade_readiness(report_data_grade: str, assets: list[AssetReview]) -> str:
    if report_data_grade != "decision_grade":
        return "no_trade"
    if any(asset.decision == "tradeable" for asset in assets):
        return "tradeable"
    return "no_trade"


def _nightly_input_completeness(markdown: str, equities: list[AssetReview], cryptos: list[AssetReview]) -> dict[str, object]:
    main_summary = _section_text(markdown, "Main summary")
    close_summary = _section_text(markdown, "Close summary")
    coverage_value = _field_any(main_summary, "Coverage universe") or _field_any(close_summary, "Coverage universe")
    coverage_count = _parse_int(coverage_value)
    artifact_run_ids = _artifact_run_ids(markdown)
    reasons: list[str] = []
    if not main_summary:
        reasons.append("main_summary_missing")
    if not close_summary:
        reasons.append("close_summary_missing")
    if coverage_count is None:
        reasons.append("coverage_universe_missing")
    elif coverage_count < len(CONFIGURED_TICKERS):
        reasons.append("coverage_universe_below_configured")
    if not equities:
        reasons.append("equities_missing")
    if not cryptos:
        reasons.append("crypto_missing")
    return {
        "incomplete": bool(reasons),
        "main_found": bool(main_summary),
        "close_found": bool(close_summary),
        "equities_count": len(equities),
        "crypto_count": len(cryptos),
        "coverage_count": coverage_count,
        "artifact_run_ids": artifact_run_ids,
        "probable_reason": ",".join(reasons) if reasons else "complete",
    }


def _nightly_input_completeness_lines(diagnostic: dict[str, object]) -> list[str]:
    coverage_count = diagnostic["coverage_count"]
    return [
        "## Nightly input completeness",
        "",
        f"- nightly_input_incomplete: {str(diagnostic['incomplete']).lower()}",
        f"- main_found: {str(diagnostic['main_found']).lower()}",
        f"- close_found: {str(diagnostic['close_found']).lower()}",
        f"- equities_count: {diagnostic['equities_count']}",
        f"- crypto_count: {diagnostic['crypto_count']}",
        f"- coverage_count: {coverage_count if coverage_count is not None else 'not_present_in_input'}",
        f"- artifact_run_ids: `{diagnostic['artifact_run_ids'] or 'not_present_in_input'}`",
        f"- probable_reason: `{diagnostic['probable_reason']}`",
    ]


def _artifact_run_ids(markdown: str) -> str:
    values = []
    for field in ("run_id", "artifact_run_id", "main_run_id", "close_run_id"):
        value = _field_any(markdown, field)
        if value:
            values.append(value)
    return ",".join(dict.fromkeys(values))


def _market_brief_status(assets: list[AssetReview]) -> str:
    by_ticker = {asset.ticker: asset for asset in assets}
    missing = [ticker for ticker in ("SPY", "QQQ", "SMH", "BTC", "ETH") if ticker not in by_ticker]
    if any(ticker in missing for ticker in ("SPY", "QQQ", "SMH")):
        return "missing"
    if missing:
        return "partial"
    return "ok"


def _market_brief_lines(markdown: str, assets: list[AssetReview]) -> list[str]:
    by_ticker = {asset.ticker: asset for asset in assets}
    status = _market_brief_status(assets)
    stock_regime = _field_any(markdown, "stock_regime", "Stock regime") or "unknown"
    crypto_regime = _field_any(markdown, "crypto_regime", "Crypto regime") or "unknown"
    lines = [
        "## Market brief",
        "",
        f"- market_brief_status: {status}",
        f"- SPY/S&P proxy: {_brief_asset_summary(by_ticker.get('SPY'))}",
        f"- QQQ/Nasdaq proxy: {_brief_asset_summary(by_ticker.get('QQQ'))}",
        f"- SMH/semi proxy: {_brief_asset_summary(by_ticker.get('SMH'))}",
        f"- BTC: {_brief_asset_summary(by_ticker.get('BTC'))}",
        f"- ETH: {_brief_asset_summary(by_ticker.get('ETH'))}",
        f"- stock_regime: `{stock_regime}`",
        f"- crypto_regime: `{crypto_regime}`",
        "- summary:",
    ]
    if status == "missing":
        lines.extend(
            [
                "  - Equity proxy data missing, so index/sector context is not decision-grade.",
                "  - BTC/ETH context is shown only when basic crypto data is present.",
                "  - Use the watch ranking as observation priority, not an entry signal.",
            ]
        )
    else:
        lines.extend(
            [
                "  - Market proxies are present for directional context.",
                "  - Crypto majors are present for cross-asset risk context.",
                "  - Rankings remain blocked for trading until checks clear.",
            ]
        )
    return lines


def _brief_asset_summary(asset: AssetReview | None) -> str:
    if asset is None:
        return "missing"
    price = _metric_value(asset.metrics, "Last price")
    change = _metric_value(asset.metrics, "Daily change")
    if price == "missing" and change == "missing":
        return "present_without_price_change"
    return f"price={price}; daily_change_pct={change}"


def _coverage_universe_lines(assets: list[AssetReview]) -> list[str]:
    by_ticker = {asset.ticker: asset for asset in assets}
    lines = [
        "## Coverage universe",
        "",
        "| ticker | asset_type | last_price | daily_change_pct | relative_strength | label | data_status | principal_reason |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for ticker in CONFIGURED_TICKERS:
        asset = by_ticker.get(ticker)
        asset_type = "crypto" if ticker in CONFIGURED_CRYPTOS else "stock"
        if asset is None:
            lines.append(f"| {ticker} | {asset_type} | missing | missing | missing | missing | missing | not_present_in_input |")
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    ticker,
                    asset_type,
                    _metric_value(asset.metrics, "Last price"),
                    _metric_value(asset.metrics, "Daily change"),
                    _metric_value(asset.metrics, "Relative strength"),
                    asset.decision or "missing",
                    _asset_data_status(asset),
                    _table_safe(asset.contradicts or asset.risks or "not_present_in_input"),
                ]
            )
            + " |"
        )
    return lines


def _asset_data_status(asset: AssetReview) -> str:
    if asset.asset_type == "crypto":
        return asset.basic_data_status or "missing"
    if asset.decision == "blocked":
        return "blocked"
    if asset.data_quality:
        return asset.data_quality
    return "present" if asset.metrics else "missing"


def _metric_value(metrics: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}:\s*([^;]+)", metrics)
    if not match:
        return "missing"
    return _table_safe(match.group(1).strip().rstrip("%"))


def _table_safe(value: str) -> str:
    return value.replace("|", "/").replace("\n", " ").strip() or "missing"


def _parse_int(value: str) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def _main_decision_grade_diagnostic_lines(markdown: str) -> list[str]:
    explicit = _section_text(markdown, "Por que o main nao foi decision-grade")
    if explicit:
        return _normalize_existing_main_diagnostic(explicit)
    main_summary = _section_text(markdown, "Main summary")
    if not main_summary:
        return []
    main_context = "\n".join([main_summary, _section_text(markdown, "Main analyst-review-input"), _main_raw_report_context(markdown)])
    report_grade = _field_any(main_summary, "report_grade") or "unknown"
    session_info = normalize_market_session(
        _field_any(main_summary, "market_session_sources")
        or _field_any(main_summary, "market_session_primary")
        or _field_any(main_summary, "market_session")
        or "unknown"
    )
    generated_at = _field_any(main_context, "generated_at_brt", "generated_at", "Generated at")
    generated_brt = _parse_generated_at_to_brt(generated_at) if generated_at else None
    generated_utc = generated_brt.astimezone(timezone.utc) if generated_brt else None
    data_mode = _field_any(main_summary, "data_mode", "Data mode") or "unknown"
    existing_reasons = _field_any(main_summary, "reason_codes")
    reason_codes = _normalize_main_reason_codes(existing_reasons, report_grade, data_mode, session_info)
    possible_bug = str(
        _had_regular_session_not_regular_reason(existing_reasons, session_info)
        or
        _possible_session_detection_bug(
            generated_brt,
            session_info,
            data_mode=data_mode,
            report_type=_field_any(main_summary, "report_type") or "main",
            fresh_price_count=_int_field_any(main_context, "fresh_price_count"),
            reason_codes=[reason.strip() for reason in reason_codes.split(",") if reason.strip()],
        )
    ).lower()
    return [
        "## Por que o main nao foi decision-grade",
        "",
        f"- report_grade: `{report_grade}`",
        f"- market_session: `{session_info.primary}`",
        f"- market_session_primary: `{session_info.primary}`",
        f"- market_session_sources: `{_format_market_session_sources(session_info.sources)}`",
        f"- market_session_conflict: {str(session_info.conflict).lower()}",
        f"- generated_at BRT: `{_format_datetime_diagnostic(generated_brt)}`",
        f"- generated_at UTC: `{_format_datetime_diagnostic(generated_utc)}`",
        f"- expected market window: `{_expected_regular_market_window(generated_brt)}`",
        f"- data_mode: `{data_mode}`",
        f"- data_freshness: `{_field_or_not_present(main_context, 'data_freshness', 'Data freshness')}`",
        f"- fresh_price_count: {_field_or_not_present(main_context, 'fresh_price_count')}",
        f"- stale_price_count: {_field_or_not_present(main_context, 'stale_price_count')}",
        f"- missing_price_count: {_field_or_not_present(main_context, 'missing_price_count')}",
        f"- provider_rate_limit_status: `{_field_or_not_present(main_context, 'provider_rate_limit_status')}`",
        f"- fmp_status: `{_field_or_not_present(main_context, 'fmp_status')}`",
        f"- coingecko_status: `{_field_or_not_present(main_context, 'coingecko_status')}`",
        f"- reason_codes: `{reason_codes}`",
        f"- possible_session_detection_bug: {possible_bug}",
        "",
        _main_decision_grade_human_explanation(session_info, reason_codes),
    ]


def _normalize_existing_main_diagnostic(section: str) -> list[str]:
    report_grade = _field_any(section, "report_grade") or "unknown"
    session_info = normalize_market_session(
        _field_any(section, "market_session_sources")
        or _field_any(section, "market_session_primary")
        or _field_any(section, "market_session")
        or "unknown"
    )
    generated_at = _field_any(section, "generated_at BRT", "generated_at_brt", "generated_at", "Generated at")
    generated_brt = _parse_generated_at_to_brt(generated_at) if generated_at else None
    generated_utc = generated_brt.astimezone(timezone.utc) if generated_brt else None
    data_mode = _field_any(section, "data_mode", "Data mode") or "unknown"
    existing_reasons = _field_any(section, "reason_codes")
    reason_codes = _normalize_main_reason_codes(existing_reasons, report_grade, data_mode, session_info)
    possible_bug = str(
        _had_regular_session_not_regular_reason(existing_reasons, session_info)
        or
        _possible_session_detection_bug(
            generated_brt,
            session_info,
            data_mode=data_mode,
            report_type=_field_any(section, "report_type") or "main",
            fresh_price_count=_int_field_any(section, "fresh_price_count"),
            reason_codes=[reason.strip() for reason in reason_codes.split(",") if reason.strip()],
        )
    ).lower()
    return [
        "## Por que o main nao foi decision-grade",
        "",
        f"- report_grade: `{report_grade}`",
        f"- market_session: `{session_info.primary}`",
        f"- market_session_primary: `{session_info.primary}`",
        f"- market_session_sources: `{_format_market_session_sources(session_info.sources)}`",
        f"- market_session_conflict: {str(session_info.conflict).lower()}",
        f"- generated_at BRT: `{_format_datetime_diagnostic(generated_brt)}`",
        f"- generated_at UTC: `{_format_datetime_diagnostic(generated_utc)}`",
        f"- expected market window: `{_expected_regular_market_window(generated_brt)}`",
        f"- data_mode: `{data_mode}`",
        f"- data_freshness: `{_field_or_not_present(section, 'data_freshness', 'Data freshness')}`",
        f"- fresh_price_count: {_field_or_not_present(section, 'fresh_price_count')}",
        f"- stale_price_count: {_field_or_not_present(section, 'stale_price_count')}",
        f"- missing_price_count: {_field_or_not_present(section, 'missing_price_count')}",
        f"- provider_rate_limit_status: `{_field_or_not_present(section, 'provider_rate_limit_status')}`",
        f"- fmp_status: `{_field_or_not_present(section, 'fmp_status')}`",
        f"- coingecko_status: `{_field_or_not_present(section, 'coingecko_status')}`",
        f"- reason_codes: `{reason_codes}`",
        f"- possible_session_detection_bug: {possible_bug}",
        "",
        _main_decision_grade_human_explanation(session_info, reason_codes),
    ]


def _normalize_main_reason_codes(existing: str, report_grade: str, data_mode: str, session_info) -> str:
    reasons = [reason.strip() for reason in existing.split(",") if reason.strip()]
    if session_info.conflict:
        reasons = [reason for reason in reasons if reason != "market_session_not_regular"]
        if "market_session_conflict" not in reasons:
            reasons.append("market_session_conflict")
    elif session_info.primary == "regular":
        reasons = [reason for reason in reasons if reason != "market_session_not_regular"]
    if not reasons:
        return _main_summary_reason_codes(report_grade, data_mode, session_info)
    return ",".join(reasons)


def _main_summary_reason_codes(report_grade: str, data_mode: str, session_info) -> str:
    if report_grade == "decision_grade":
        return "nenhum"
    reasons = []
    if data_mode in {"blocked", "demo", "fixture", "limited"}:
        reasons.append("data_mode_not_live")
    if session_info.conflict:
        reasons.append("market_session_conflict")
    elif session_info.primary != "regular":
        reasons.append("market_session_not_regular")
    return ",".join(reasons) if reasons else "unknown_decision_grade_failure"


def _had_regular_session_not_regular_reason(existing_reasons: str, session_info) -> bool:
    reasons = [reason.strip() for reason in existing_reasons.split(",") if reason.strip()]
    return "market_session_not_regular" in reasons and "regular" in session_info.sources


def _main_decision_grade_human_explanation(session_info, reason_codes: str) -> str:
    if "market_session_conflict" in reason_codes:
        return (
            "Main foi bloqueado porque a sessao veio conflitante: "
            f"sources={_format_market_session_sources(session_info.sources)}. "
            "Isso sugere bug de deteccao/parsing de market_session, nao necessariamente ausencia de mercado regular."
        )
    if "market_session_not_regular" in reason_codes:
        return f"Main foi bloqueado porque a sessao primaria veio `{session_info.primary}`, fora de regular."
    return "Main nao foi decision-grade pelos reason_codes acima."


def _field_or_not_present(block: str, *names: str) -> str:
    return _field_any(block, *names) or "not_present_in_input"


def _int_field_any(block: str, *names: str) -> int:
    value = _field_any(block, *names)
    if not value:
        return 0
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else 0


def _field_any(block: str, *names: str) -> str:
    for name in names:
        value = _field(block, name)
        if value:
            return value
    return ""


def generate_from_file(input_path: Path, output_path: Path, history_path: Path | None = None) -> None:
    nightly_markdown = input_path.read_text(encoding="utf-8")
    extra_markdowns = _load_raw_reports(nightly_markdown)
    review = generate_analyst_final_review(nightly_markdown, extra_markdowns=extra_markdowns)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(review, encoding="utf-8")
    if history_path:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(review, encoding="utf-8")


def _load_raw_reports(nightly_markdown: str) -> list[str]:
    paths = []
    for field in ("main_report", "close_report"):
        match = re.search(rf"(?m)^- {field}:\s*`([^`]+)`", nightly_markdown)
        if match:
            path = Path(match.group(1))
            if path.exists():
                paths.append(path.read_text(encoding="utf-8"))
    return paths


def _legacy_main_blocks_operation(markdown: str) -> bool:
    main_summary = _section_text(markdown, "Main summary")
    main_context = main_summary or markdown
    checks = [
        "diagnostic_not_decision_grade",
        "report_grade: `blocked",
        "Data mode: `blocked`",
        "data_mode: `blocked`",
        "data_mode: `demo`",
        "data_mode: `fixture`",
        "market_session: `unknown`",
        "market_session: `closed`",
        "regular,unknown",
        "blocked_or_diagnostic",
    ]
    return any(check in main_context for check in checks)


def _asset_blocks(markdown: str) -> list[str]:
    matches = re.finditer(r"(?ms)^## ([A-Z0-9.]{1,12})\s*\n(.*?)(?=^## |\Z)", markdown)
    return [f"## {match.group(1)}\n{match.group(2).strip()}" for match in matches if "- Ativo:" in match.group(2)]


def _dedupe_assets(assets: list[AssetReview]) -> list[AssetReview]:
    by_ticker: dict[str, AssetReview] = {}
    order: list[str] = []
    for asset in assets:
        if asset.ticker not in by_ticker:
            order.append(asset.ticker)
        by_ticker[asset.ticker] = asset
    return [by_ticker[ticker] for ticker in order]


def _classify_asset(block: str) -> AssetReview | None:
    ticker = _field(block, "Ativo")
    asset_type = _field(block, "Tipo")
    if not ticker or not asset_type:
        return None
    label = (_field(block, "decision_label") or _field(block, "Decisao")).lower()
    reasons = _field(block, "reason_codes").lower()
    severity = _field(block, "missing_data_severity").lower()
    data_quality = _field(block, "data_quality").lower()
    expected_value = _float_field(block, "expected_value_r")
    investment_score = _float_field(block, "Investment Quality Score")
    metrics = _field(block, "Metricas principais")
    lower_block = block.lower()

    crypto_basic_status = _crypto_basic_data_status(block)
    crypto_flow_status = _crypto_flow_data_status(lower_block)
    binance_status = _binance_status(lower_block)
    critical_missing_price = _critical_price_missing(lower_block)
    provider_blocked = data_quality == "blocked" or "provider blocked" in lower_block
    if asset_type != "crypto" and (severity == "critical" or critical_missing_price or provider_blocked):
        return _asset_review(
            ticker,
            asset_type,
            "blocked",
            "Dado critico ausente impede conclusao minima.",
            "Nenhum sinal pode ser usado operacionalmente sem dados minimos.",
            "provider/preco minimo indisponivel.",
            block,
        )

    if asset_type == "stock":
        if label in {"avoid", "rejected"} or (expected_value is not None and expected_value < 0) or "negative_ev" in reasons:
            return _asset_review(
                ticker,
                asset_type,
                "rejected",
                "Tese/EV insuficiente para observacao operacional.",
                "Setup nao compensa a qualidade/risco atual.",
                "EV negativo, score fraco ou tese invalida.",
                block,
            )
        if label == "technical_unvalidated" and _has_positive_equity_signals(block, metrics, investment_score):
            return _asset_review(
                ticker,
                asset_type,
                "watch_pending_checks",
                f"{ticker} tem sinais tecnicos/fundamentais parciais, mas ainda exige validacao de news, earnings/guidance, valuation, risco de gap e main decision-grade antes de qualquer entrada.",
                "Momentum/medias/score ou crescimento sugerem algo a observar.",
                "Dados/eventos/noticias ainda nao verificados; nao e compra.",
                block,
            )
        if "not_verified" in lower_block or "not_collected" in lower_block:
            return _asset_review(
                ticker,
                asset_type,
                "research_only",
                "Ha interesse potencial, mas faltam validacoes basicas.",
                "Existe algum sinal ou cobertura no pacote.",
                "Dados ainda not_verified/not_collected.",
                block,
            )
        return _asset_review(ticker, asset_type, "watch_only", "Observacao sem entrada.", "Dados minimos presentes.", "Sem aprovacao operacional.", block)

    if asset_type == "crypto":
        if crypto_basic_status == "not_verified":
            return _asset_review(
                ticker,
                asset_type,
                "blocked",
                "Dado basico de cripto ausente impede conclusao minima.",
                "Nenhum sinal pode ser usado operacionalmente sem preco/liquidez basicos.",
                "provider/preco minimo indisponivel.",
                block,
                basic_data_status=crypto_basic_status,
                flow_data_status=crypto_flow_status,
                binance_status=binance_status,
            )
        if _crypto_flow_missing(lower_block):
            decision = "crypto_watch_context" if ticker in {"BTC", "ETH", "SOL"} else "crypto_research_only"
            return _asset_review(
                ticker,
                asset_type,
                decision,
                "Setup/ativo pode ser acompanhado, mas flow/news not_verified impedem trade.",
                "Preco/liquidez/setup basico parecem existir no pacote.",
                "flow/derivatives nao verificados; CVD/OI/liquidations/premium ausentes.",
                block,
                basic_data_status=crypto_basic_status,
                flow_data_status=crypto_flow_status,
                binance_status=binance_status,
            )
        return _asset_review(
            ticker,
            asset_type,
            "watch_pending_flow_confirmation",
            "Observar cripto ate confirmacao de fluxo.",
            "Dados basicos presentes.",
            "Fluxo precisa confirmar.",
            block,
            basic_data_status=crypto_basic_status,
            flow_data_status=crypto_flow_status,
            binance_status=binance_status,
        )

    return None


def _asset_review(
    ticker: str,
    asset_type: str,
    decision: str,
    thesis: str,
    confirms: str,
    contradicts: str,
    block: str,
    *,
    basic_data_status: str = "",
    flow_data_status: str = "",
    binance_status: str = "",
) -> AssetReview:
    return AssetReview(
        ticker=ticker,
        asset_type=asset_type,
        decision=decision,
        thesis=thesis,
        confirms=confirms,
        contradicts=contradicts,
        valuation=_valuation_line(block),
        events_news=_events_line(block),
        risks=_risks_line(block),
        path_to_operation=_path_to_operation(decision, asset_type),
        basic_data_status=basic_data_status,
        flow_data_status=flow_data_status,
        binance_status=binance_status,
        metrics=_field(block, "Metricas principais"),
        data_quality=_field(block, "data_quality"),
    )


def _has_positive_equity_signals(block: str, metrics: str, investment_score: float | None) -> bool:
    score_ok = investment_score is not None and investment_score >= 60
    positive_metrics = any(token in metrics for token in ("Relative strength:", "Revenue growth:", "EPS growth:", "EMA 9:", "Average volume:"))
    return score_ok and positive_metrics


def _crypto_flow_missing(lower_block: str) -> bool:
    return any(token in lower_block for token in ("cvd_proxy_unavailable", "open_interest_change_unavailable", "liquidations_unavailable", "coinbase_premium_unavailable", "news_not_collected", "news_status: `not_verified`"))


def _critical_price_missing(lower_block: str) -> bool:
    return any(
        token in lower_block
        for token in (
            "price_history_unavailable",
            "price history: n/a",
            "insufficient_price_history",
            "data_mode: `blocked`",
        )
    )


def _crypto_basic_data_status(block: str) -> str:
    lower_block = block.lower()
    if "data_mode: `blocked`" in lower_block or "price history: n/a" in lower_block:
        return "not_verified"
    metrics = _field(block, "Metricas principais").lower()
    provider = (_field(block, "provider") or _field(block, "Data source")).lower()
    data_source = _field(block, "Data source").lower()
    has_basic_provider = any(source in provider for source in ("coingecko", "coinbase", "hyperliquid", "fallback"))
    has_price_or_liquidity = any(token in metrics for token in ("last price:", "market cap:", "average volume:", "daily change:", "rsi:"))
    has_missing_price = "price_history_unavailable" in lower_block or "insufficient_price_history" in lower_block
    if not (has_price_or_liquidity and (has_basic_provider or not has_missing_price)):
        return "not_verified"
    if "fallback" in lower_block or data_source in {"alphavantage", "yahoo", "stooq"}:
        return "fallback"
    if "is_stale: `yes`" in lower_block or ("stale_reason:" in lower_block and "not_stale" not in lower_block):
        return "cache"
    if "cache age:" in lower_block and "cache age: unknown" not in lower_block and "cache age: 0s" not in lower_block:
        return "cache"
    return "live"


def _crypto_flow_data_status(lower_block: str) -> str:
    if _crypto_flow_missing(lower_block):
        return "not_verified"
    flow_tokens = ("funding rate", "open interest", "open interest change", "liquidation", "cvd", "premium")
    if all(token in lower_block for token in ("open interest", "cvd", "premium")) and "n/a" not in lower_block:
        return "live"
    if any(token in lower_block for token in flow_tokens):
        return "partial"
    return "unavailable"


def _binance_status(lower_block: str) -> str:
    if "binance_restricted_location" in lower_block or "http_error:451" in lower_block:
        return "restricted"
    if "binance" in lower_block:
        return "ok"
    return "not_used"


def _valuation_line(block: str) -> str:
    metrics = _field(block, "Metricas principais")
    if not metrics:
        return "not_verified"
    pieces = [piece.strip() for piece in metrics.split(";") if any(key in piece for key in ("PE:", "PEG:", "Market cap:", "Revenue growth:", "EPS growth:"))]
    return "; ".join(pieces[:4]) if pieces else "not_verified"


def _events_line(block: str) -> str:
    statuses = []
    for field in ("event_check_status", "news_status", "earnings_guidance_status", "news_catalyst_status"):
        value = _field(block, field)
        if value:
            statuses.append(f"{field}: {value}")
    return "; ".join(statuses) if statuses else "not_verified"


def _risks_line(block: str) -> str:
    reasons = _field(block, "reason_codes")
    return reasons if reasons else "not_verified"


def _path_to_operation(decision: str, asset_type: str) -> str:
    if decision in {"blocked", "rejected"}:
        return "Precisa sair de blocked/rejected antes de qualquer watch."
    if asset_type == "crypto":
        return "Validar CVD, OI, liquidations, premium, news e regime antes de qualquer entrada manual."
    return "Validar news, earnings/guidance, valuation, risco de gap, setor e main decision-grade antes de qualquer entrada manual."


def _asset_lines(assets: list[AssetReview]) -> list[str]:
    lines: list[str] = []
    for asset in assets:
        lines.extend(
            [
                f"### {asset.ticker}",
                "",
                f"* ticker: {asset.ticker}",
                f"* legacy_label: {asset.decision}",
                f"* source_decision: {asset.source_decision or 'unknown'}",
                f"* tese: {asset.thesis}",
                f"* o que confirma: {asset.confirms}",
                f"* o que contradiz: {asset.contradicts}",
                f"* valuation: {asset.valuation}",
                f"* earnings/guidance/news: {asset.events_news}",
                f"* riscos: {asset.risks}",
                f"* o que precisaria mudar para virar operacao: {asset.path_to_operation}",
                "",
            ]
        )
        if asset.asset_type == "crypto":
            insert_at = len(lines) - 7
            lines[insert_at:insert_at] = [
                f"* basic_data_status: {asset.basic_data_status}",
                f"* flow_data_status: {asset.flow_data_status}",
                f"* binance_status: {asset.binance_status}",
            ]
    return lines


def _telegram_summary(
    final_decision: str,
    assets: list[AssetReview],
    *,
    report_data_grade: str,
    trade_readiness: str,
    market_brief_status: str,
    input_incomplete: bool,
    main_diagnostic_lines: list[str] | None = None,
) -> str:
    top_equities = _top_equities_to_watch(assets)
    best_equity = top_equities[0] if top_equities else None
    top_crypto = _top_crypto_to_watch(assets)
    trade_block = "nenhum" if final_decision == "tradeable" else _trade_block_summary(assets, report_data_grade)
    data_error = _data_error_summary(input_incomplete, main_diagnostic_lines or [])
    lines = [
        f"Decisao operacional: {final_decision}",
        f"Report data grade: {report_data_grade}",
        f"Trade readiness: {trade_readiness}",
        f"Market brief: {market_brief_status}",
        f"Top equities: {_format_top_ticker_list(top_equities) if top_equities else 'nenhum'}",
        f"Top crypto: {_format_top_ticker_list(top_crypto) if top_crypto else 'nenhum'}",
        f"Melhor equity: {_best_asset_summary(best_equity)}",
        f"Melhor crypto: {_best_crypto_summary(top_crypto)}",
        f"Bloqueio para trade: {trade_block}",
        f"Erro de dados, se houver: {data_error}",
        (
            "Proximo passo: revisar stop/invalidation e sizing do main; decisao e execucao manuais"
            if final_decision == "tradeable"
            else "Proximo passo: aguardar proximo main decision-grade"
        ),
    ]
    return "\n".join(lines)


def _telegram_reason(main_not_decision_grade: bool, main_diagnostic_lines: list[str]) -> str:
    diagnostic = "\n".join(main_diagnostic_lines)
    if "market_session_conflict: true" in diagnostic or "reason_codes: `market_session_conflict`" in diagnostic:
        sources = _field_any(diagnostic, "market_session_sources").strip("[]").replace(", ", "/")
        return f"possivel bug de session detection; main veio {sources}."
    return "main nao decision-grade" if main_not_decision_grade else "checks pendentes ainda nao liberam entrada"


def _trade_block_summary(assets: list[AssetReview], report_data_grade: str) -> str:
    blockers: list[str] = []
    if report_data_grade != "decision_grade":
        blockers.append("main_not_decision_grade")
    if any(asset.asset_type == "stock" and "not_verified" in asset.events_news for asset in assets):
        blockers.append("news/earnings")
    if any(asset.asset_type == "crypto" and asset.flow_data_status in {"not_verified", "unavailable"} for asset in assets):
        blockers.append("crypto_flow_pending")
    if not blockers:
        blockers.append("manual_trade_checks_pending")
    if "news/earnings" in blockers and "crypto_flow_pending" in blockers:
        return "news/earnings/flow/crypto_flow_pending (flow/derivatives nao verificados)"
    if "crypto_flow_pending" in blockers:
        return ",".join(dict.fromkeys(blockers)).replace("crypto_flow_pending", "crypto_flow_pending (flow/derivatives nao verificados)")
    return ",".join(dict.fromkeys(blockers))


def _data_error_summary(input_incomplete: bool, main_diagnostic_lines: list[str]) -> str:
    diagnostic = "\n".join(main_diagnostic_lines)
    errors: list[str] = []
    if input_incomplete:
        errors.append("nightly_input_incomplete")
    if "possible_session_detection_bug: true" in diagnostic:
        errors.append("possible_session_detection_bug")
    if "market_session_conflict: true" in diagnostic:
        errors.append("market_session_conflict")
    return ",".join(dict.fromkeys(errors)) if errors else "nenhum"


def _telegram_candidate_summary(assets: list[AssetReview]) -> str:
    if not assets:
        return "nenhum"
    return "; ".join(f"{asset.ticker} em {asset.decision}" for asset in assets[:5])


def _best_asset_summary(asset: AssetReview | None) -> str:
    if asset is None:
        return "nenhum"
    return f"{asset.ticker} - {asset.decision}"


def _best_crypto_summary(assets: list[AssetReview]) -> str:
    if not assets:
        return "nenhum"
    best_decision = assets[0].decision
    tickers = [asset.ticker for asset in assets if asset.decision == best_decision]
    return f"{'/'.join(tickers)} - {best_decision}"


def _telegram_rejected_summary(assets: list[AssetReview]) -> str:
    rejected = [asset for asset in assets if asset.decision in {"rejected", "blocked"}]
    if not rejected:
        return "nenhum"
    return "; ".join(f"{asset.ticker}={asset.decision}" for asset in rejected[:12])


def _telegram_crypto_decision_summary(assets: list[AssetReview]) -> str:
    if not assets:
        return "nenhum"
    context = [asset.ticker for asset in assets if asset.decision == "crypto_watch_context"]
    research = [asset.ticker for asset in assets if asset.decision == "crypto_research_only"]
    blocked = [asset.ticker for asset in assets if asset.decision == "blocked"]
    pieces = []
    if context:
        pieces.append(f"contexto/research {','.join(context)}")
    if research:
        pieces.append(f"research_only {','.join(research)}")
    if blocked:
        pieces.append(f"blocked {','.join(blocked)}")
    return "; ".join(pieces) if pieces else _telegram_asset_statuses(assets)


def _telegram_universe_context(assets: list[AssetReview]) -> str:
    stocks = [asset for asset in assets if asset.asset_type == "stock"]
    cryptos = [asset for asset in assets if asset.asset_type == "crypto"]
    parts = []
    if stocks:
        parts.append(f"Acoes: {_telegram_asset_statuses(stocks)}.")
    if cryptos:
        parts.append(f"Cripto: {_telegram_asset_statuses(cryptos)}.")
    return " ".join(parts)


def _telegram_asset_statuses(assets: list[AssetReview]) -> str:
    return "; ".join(f"{asset.ticker}={asset.decision}" for asset in assets)


def _telegram_crypto_context(watch_assets: list[AssetReview]) -> str:
    tickers = [
        asset.ticker
        for asset in watch_assets
        if asset.asset_type == "crypto" and asset.decision in {"crypto_watch_context", "crypto_research_only"} and asset.basic_data_status != "not_verified" and asset.flow_data_status in {"not_verified", "unavailable"}
    ]
    majors = [ticker for ticker in ("BTC", "ETH", "SOL") if ticker in tickers]
    if majors:
        return f"Cripto: {'/'.join(majors)} apenas contexto/research; flow/derivatives nao verificados."
    if tickers:
        return f"Cripto: {','.join(tickers)} apenas research; flow/derivatives nao verificados."
    return ""


def _watch_summary(assets: list[AssetReview]) -> str:
    return ", ".join(f"{asset.ticker}:{asset.decision}" for asset in assets) if assets else "nenhum"


def _field(block: str, name: str) -> str:
    escaped = re.escape(name)
    match = re.search(rf"(?m)^-\s*{escaped}:\s*(.+)$", block)
    if not match:
        return ""
    return match.group(1).strip().strip("` ")


def _float_field(block: str, name: str) -> float | None:
    value = _field(block, name)
    if not value or value.lower() in {"n/a", "unknown"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(match.group(0)) if match else None


def _section_text(markdown: str, heading: str) -> str:
    match = re.search(rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", markdown)
    return match.group(1).strip() if match else ""


def _main_raw_report_context(markdown: str) -> str:
    reports = re.finditer(r"(?ms)^# Investment and Swing Trade Advisor\s*\n(.*?)(?=^# Investment and Swing Trade Advisor|\Z)", markdown)
    for match in reports:
        report = match.group(0)
        if _field_any(report, "report_type") == "main":
            return report
    return ""


def _default_brt_history_path() -> Path:
    brt_date = datetime.now(timezone(timedelta(hours=-3))).date().isoformat()
    return Path("reports") / "history" / f"{brt_date}-analyst-final-review.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-path", default="reports/nightly-review-input.md")
    parser.add_argument("--output-path", default="reports/analyst-final-review.md")
    parser.add_argument("--history-path", default="")
    args = parser.parse_args(argv)

    input_path = Path(args.input_path)
    output_path = Path(args.output_path)
    history_path = Path(args.history_path) if args.history_path else _default_brt_history_path()
    if not input_path.exists():
        print(f"nightly_review_input_missing:{input_path}")
        return 1
    generate_from_file(input_path=input_path, output_path=output_path, history_path=history_path)
    print(f"analyst_final_review={output_path}")
    print(f"analyst_final_review_history={history_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
