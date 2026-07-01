from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


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


def generate_analyst_final_review(
    nightly_markdown: str,
    *,
    extra_markdowns: list[str] | None = None,
    public_equity_executed: bool = False,
) -> str:
    combined = "\n\n".join([nightly_markdown, *(extra_markdowns or [])])
    main_not_decision_grade = _main_blocks_operation(combined)
    assets = [_classify_asset(block) for block in _asset_blocks(combined)]
    assets = _dedupe_assets([asset for asset in assets if asset is not None])

    equities = [asset for asset in assets if asset.asset_type == "stock"]
    cryptos = [asset for asset in assets if asset.asset_type == "crypto"]
    watch_assets = [
        asset
        for asset in assets
        if asset.decision
        in {"watch_only", "watch_pending_checks", "research_only", "crypto_research_only", "crypto_watch_context", "watch_pending_flow_confirmation"}
    ]
    rejected_or_blocked = [asset for asset in assets if asset.decision in {"rejected", "blocked"}]
    final_decision = "no_trade" if main_not_decision_grade else ("watch_only" if watch_assets else "wait")
    public_equity_note = (
        "executed for equity candidates."
        if public_equity_executed
        else "not executed automatically in this environment; review based on nightly-review-input and safety rules."
    )

    lines: list[str] = [
        "# Analyst Final Review",
        "",
        f"Public Equity Investing executed: {str(public_equity_executed).lower()}",
        f"Public Equity Investing note: {public_equity_note}",
        "",
        "## Decisao geral para o proximo pregao",
        "",
        f"* {final_decision}",
        "",
        "Decisao operacional conservadora. Nenhum ativo aprovado como tradeable. Sem broker; sem ordem automatica; sem compra automatica.",
        "",
        "## Resumo do dia",
        "",
        f"* Decisao operacional: {final_decision}.",
        f"* Main decision-grade: {'nao' if main_not_decision_grade else 'sim/limitado'}.",
        f"* Candidatos para observar/research: {_watch_summary(watch_assets)}.",
        "* Limitacoes de dados: not_verified/not_collected limitam operacao, mas podem permitir watch/research quando existe setup minimo.",
        "",
        "## Equity review",
        "",
    ]

    lines.extend(_asset_lines(equities) if equities else ["Nenhuma equity candidata para review."])
    lines.extend(["", "## Crypto review", "", "Separado de equities. Public Equity Investing nao e fonte principal para cripto.", ""])
    lines.extend(_asset_lines(cryptos) if cryptos else ["Nenhum cripto ativo para review."])
    lines.extend(
        [
            "",
            "## Watchlist para amanha",
            "",
        ]
    )
    if watch_assets:
        for asset in watch_assets:
            lines.append(f"* {asset.ticker}: {asset.decision}. Sem entrada automatica; revisar checks pendentes.")
    else:
        lines.append("Nenhum ativo para watch/research.")
    lines.extend(
        [
            "",
            "## Rejected/blocked",
            "",
        ]
    )
    if rejected_or_blocked:
        for asset in rejected_or_blocked:
            lines.append(f"* {asset.ticker}: {asset.decision}. {asset.contradicts}")
    else:
        lines.append("Nenhum ativo rejected/blocked.")
    lines.extend(
        [
            "",
            "## Checklist antes de operar",
            "",
            "* Confirmar que o proximo main esta `Data mode: live` e `report_grade` decision-grade.",
            "* Confirmar que o main rodou em sessao valida: market_session = regular, nao unknown/closed.",
            "* Confirmar news, earnings/guidance e valuation para equities.",
            "* Confirmar CVD, OI, liquidations, premium e news para cripto.",
            "* Nao transformar `technical_unvalidated`, `research_only` ou `watch_pending_checks` em compra.",
            "* Nao transformar watch/research em tradeable sem novo main decision-grade.",
            "* Sem alavancagem quando confidence for low ou missing_data_severity for high/blocking.",
            "",
            "## Telegram summary",
            "",
            _telegram_summary(final_decision, watch_assets, main_not_decision_grade),
        ]
    )
    return "\n".join(lines).strip() + "\n"


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


def _main_blocks_operation(markdown: str) -> bool:
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
                f"* decision: {asset.decision}",
                f"* decisao final: {asset.decision}",
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


def _telegram_summary(final_decision: str, watch_assets: list[AssetReview], main_not_decision_grade: bool) -> str:
    candidates = "; ".join(f"{asset.ticker} em {asset.decision}" for asset in watch_assets) if watch_assets else "nenhum"
    reason = "main nao decision-grade e dados de news/earnings/flow ainda nao verificados" if main_not_decision_grade else "checks pendentes ainda nao liberam entrada"
    crypto_context = _telegram_crypto_context(watch_assets)
    crypto_sentence = f" {crypto_context}" if crypto_context else ""
    return (
        f"Decisao operacional: {final_decision}. Nenhum ativo aprovado para entrada. "
        f"Para observar amanha: {candidates}.{crypto_sentence} Motivo: {reason}. "
        "Sem broker, sem ordem automatica, sem compra automatica."
    )


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
