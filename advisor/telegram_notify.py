from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import request


SendJson = Callable[[str, dict[str, str]], Any]


def build_telegram_message(markdown: str, *, artifact_path: str, workflow_url: str) -> str:
    report_type = _field(markdown, "report_type")
    data_mode = _field(markdown, "Data mode")
    decision = _field(markdown, "Decisao geral")
    brt_date = datetime.now(timezone(timedelta(hours=-3))).date().isoformat()
    tradeable = _summary_count(markdown, "Ativos tradeable")
    watchlist = _section_symbols(markdown, "Watchlist aprovada")[:3]
    research = _section_symbols(markdown, "Research queue")[:3]
    risks = _section_text(markdown, "Riscos principais").splitlines()
    risk_line = next((line.strip("- ").strip() for line in risks if line.strip()), "not_verified")
    lines = [
        "Financial Advisor report",
        f"brt_date: {brt_date}",
        f"report_type: {report_type}",
        f"data_mode: {data_mode}",
        f"decision: {decision}",
        f"tradeable_count: {tradeable}",
        f"watchlist_top3: {_csv_or_none(watchlist)}",
        f"research_queue_top3: {_csv_or_none(research)}",
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
