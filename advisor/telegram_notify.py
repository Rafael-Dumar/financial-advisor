from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import request


SendJson = Callable[[str, dict[str, str]], Any]
TELEGRAM_MAX_CHARS = 3500


def build_telegram_message(markdown: str, *, artifact_path: str, workflow_url: str) -> str:
    report_type = _field(markdown, "report_type")
    data_mode = _field(markdown, "Data mode")
    decision = _field(markdown, "Decisao geral")
    brt_date = datetime.now(timezone(timedelta(hours=-3))).date().isoformat()
    tradeable = _summary_count(markdown, "Ativos tradeable")
    watchlist_count = _summary_count(markdown, "Watchlist aprovada")
    coverage_count = _coverage_count(markdown)
    watchlist = _section_symbols(markdown, "Watchlist aprovada")[:3]
    research = _section_symbols(markdown, "Research queue")[:3]
    deep_analysis = _section_symbols(markdown, "Deep analysis candidates")[:5]
    provider_rate_limit_status = _field(markdown, "provider_rate_limit_status")
    budget_limited = _field(markdown, "deep_analysis_limited_by_budget")
    risks = _section_text(markdown, "Riscos principais").splitlines()
    risk_line = next((line.strip("- ").strip() for line in risks if line.strip()), "not_verified")
    lines = [
        "Financial Advisor report",
        f"brt_date: {brt_date}",
        f"report_type: {report_type}",
        f"data_mode: {data_mode}",
        f"decision: {decision}",
        f"coverage_count: {coverage_count}",
        f"tradeable_count: {tradeable}",
        f"watchlist_count: {watchlist_count}",
        f"watchlist_top3: {_csv_or_none(watchlist)}",
        f"research_queue_top3: {_csv_or_none(research)}",
        f"deep_analysis_candidates: {_csv_or_none(deep_analysis)}",
        f"provider_rate_limit_status: {provider_rate_limit_status}",
        f"budget_limited: {budget_limited}",
        f"main_risks: {risk_line}",
        f"artifact: {artifact_path}",
    ]
    if workflow_url:
        lines.append(f"workflow: {workflow_url}")
    return "\n".join(lines)


def notify_from_report(
    *,
    report_path: Path,
    artifact_path: str,
    workflow_url: str,
    send_json: SendJson | None = None,
) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return "telegram_skipped_missing_secrets"
    markdown = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    message = build_telegram_message(markdown, artifact_path=artifact_path, workflow_url=workflow_url)
    sender = send_json or _post_json
    sender(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {
            "chat_id": chat_id,
            "text": message[:3500],
            "disable_web_page_preview": "true",
        },
    )
    return "telegram_sent"


def extract_telegram_summary(markdown: str) -> str:
    summary = _section_text(markdown, "Telegram summary")
    if not summary:
        raise ValueError("telegram_summary_missing")
    return summary.strip()


def build_analyst_final_telegram_message(summary: str) -> str:
    safe_summary = _remove_forbidden_trade_language(summary).strip()
    safety_lines = [
        "Analyst Final Review",
        "decisao_final_conservadora: true",
        "seguranca: sem broker; sem ordem automatica; sem compra automatica.",
        "",
        safe_summary,
    ]
    message = "\n".join(line for line in safety_lines if line is not None).strip()
    return message[:TELEGRAM_MAX_CHARS]


def notify_from_analyst_final_review(
    *,
    report_path: Path,
    send_json: SendJson | None = None,
) -> str:
    if not report_path.exists():
        raise FileNotFoundError(f"analyst_final_review_missing:{report_path}")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return "telegram_skipped_missing_secrets"

    markdown = report_path.read_text(encoding="utf-8")
    summary = extract_telegram_summary(markdown)
    message = build_analyst_final_telegram_message(summary)
    sender = send_json or _post_json
    sender(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        },
    )
    return "telegram_sent"


def _post_json(url: str, payload: dict[str, str]) -> Any:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _remove_forbidden_trade_language(text: str) -> str:
    replacements = {
        "comprar agora": "nao comprar automaticamente",
        "vender agora": "nao vender automaticamente",
        "buy now": "do not buy automatically",
        "sell now": "do not sell automatically",
    }
    cleaned = text
    for forbidden, replacement in replacements.items():
        cleaned = _replace_case_insensitive(cleaned, forbidden, replacement)
    return cleaned


def _replace_case_insensitive(text: str, old: str, new: str) -> str:
    lower_text = text.lower()
    lower_old = old.lower()
    start = lower_text.find(lower_old)
    while start != -1:
        end = start + len(old)
        text = text[:start] + new + text[end:]
        lower_text = text.lower()
        start = lower_text.find(lower_old, start + len(new))
    return text


def _field(markdown: str, name: str) -> str:
    prefix = f"- {name}:"
    for line in markdown.splitlines():
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            return value.strip("` ")
    return "unknown"


def _summary_count(markdown: str, label: str) -> str:
    prefix = f"- {label}:"
    for line in markdown.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return "unknown"


def _coverage_count(markdown: str) -> str:
    field_value = _summary_count(markdown, "Coverage universe")
    if field_value != "unknown":
        return field_value
    table = _section_text(markdown, "Coverage universe")
    rows = [
        line
        for line in table.splitlines()
        if line.startswith("| ")
        and not line.startswith("| ---")
        and "Ticker" not in line
    ]
    return str(len(rows)) if rows else "unknown"


def _section_symbols(markdown: str, heading: str) -> list[str]:
    text = _section_text(markdown, heading)
    symbols = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- `") and "`" in stripped[3:]:
            symbols.append(stripped.split("`", 2)[1])
    return symbols


def _section_text(markdown: str, heading: str) -> str:
    lines = markdown.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"## {heading}":
            start = index + 1
            break
    if start is None:
        return ""
    section = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.strip():
            section.append(line)
    return "\n".join(section).strip()


def _csv_or_none(values: list[str]) -> str:
    return ",".join(values) if values else "nenhum"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "analyst-final":
        print("usage: python -m advisor.telegram_notify analyst-final --report-path <path>")
        return 2
    report_path = Path("reports/analyst-final-review.md")
    index = 1
    while index < len(args):
        if args[index] == "--report-path" and index + 1 < len(args):
            report_path = Path(args[index + 1])
            index += 2
            continue
        print(f"unknown_arg:{args[index]}")
        return 2
    try:
        status = notify_from_analyst_final_review(report_path=report_path)
    except FileNotFoundError as exc:
        print(str(exc))
        return 1
    except ValueError as exc:
        print(str(exc))
        return 1
    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
