from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from advisor.telegram_notify import build_telegram_message, notify_from_report


REPORT = """# Investment and Swing Trade Advisor

- report_type: `main`
- Data mode: `live`
- market_session: `regular`
- Decisao geral: `wait`

## Resumo executivo

- Ativos tradeable: 0
- Watchlist aprovada: 1
- Research queue: 1
- Coverage universe: 4

## Watchlist aprovada

- `MSFT`

## Research queue

- `NVDA`

## Coverage universe

| Ticker | Type | Last price | Daily change | Trend | Bucket | Data status | Reason |
| --- | --- | --- | --- | --- | --- | --- | --- |
| MSFT | stock | 100.00 | 1.20% | up | watchlist | live | setup_present |
| NVDA | stock | n/a | n/a | not_verified | research_queue | not_verified | not_selected_for_deep_analysis |
| HYPE | crypto | 35.00 | -2.00% | down | technical_unvalidated | live | high_volatility |
| BTC | crypto | n/a | n/a | not_verified | not_deep_analyzed | not_verified | not_selected_for_deep_analysis |

## Deep analysis candidates

- `MSFT`
- `HYPE`

## provider_budget_summary

- provider_rate_limit_status: `ok`
- deep_analysis_limited_by_budget: `true`
- deep_analysis_skipped: NVDA,BTC

## Riscos principais

earnings_data_missing
"""


class TelegramNotifyTests(unittest.TestCase):
    def test_missing_telegram_secrets_does_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "latest.md"
            report_path.write_text(REPORT, encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                status = notify_from_report(
                    report_path=report_path,
                    artifact_path="reports/latest.md",
                    workflow_url="https://github.com/example/actions/runs/1",
                    send_json=lambda url, payload: (_ for _ in ()).throw(AssertionError("should not send")),
                )

        self.assertEqual(status, "telegram_skipped_missing_secrets")

    def test_telegram_payload_is_short_and_does_not_include_secret(self) -> None:
        payloads = []

        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "latest.md"
            report_path.write_text(REPORT, encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "123456:secret-token",
                    "TELEGRAM_CHAT_ID": "999",
                },
                clear=True,
            ):
                status = notify_from_report(
                    report_path=report_path,
                    artifact_path="reports/latest.md",
                    workflow_url="https://github.com/example/actions/runs/1",
                    send_json=lambda url, payload: payloads.append((url, payload)) or {"ok": True},
                )

        self.assertEqual(status, "telegram_sent")
        self.assertEqual(len(payloads), 1)
        url, payload = payloads[0]
        self.assertIn("123456:secret-token", url)
        self.assertEqual(payload["chat_id"], "999")
        self.assertNotIn("secret-token", payload["text"])
        self.assertNotIn("Traceback", payload["text"])
        self.assertIn("brt_date:", payload["text"])
        self.assertIn("report_type: main", payload["text"])
        self.assertIn("decision: wait", payload["text"])
        self.assertIn("coverage_count: 4", payload["text"])
        self.assertIn("watchlist_count: 1", payload["text"])
        self.assertIn("deep_analysis_candidates: MSFT,HYPE", payload["text"])
        self.assertIn("provider_rate_limit_status: ok", payload["text"])
        self.assertIn("budget_limited: true", payload["text"])
        self.assertIn("workflow: https://github.com/example/actions/runs/1", payload["text"])

    def test_build_telegram_message_handles_missing_report_fields(self) -> None:
        message = build_telegram_message(
            "not a normal report",
            artifact_path="reports/latest.md",
            workflow_url="https://github.com/example/actions/runs/2",
        )

        self.assertIn("report_type: unknown", message)
        self.assertIn("decision: unknown", message)
        self.assertIn("artifact: reports/latest.md", message)


if __name__ == "__main__":
    unittest.main()
