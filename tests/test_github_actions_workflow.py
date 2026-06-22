from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class GitHubActionsWorkflowTests(unittest.TestCase):
    def test_financial_advisor_reports_workflow_contract(self) -> None:
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "financial-advisor-reports.yml"

        self.assertTrue(workflow_path.exists(), "missing financial-advisor-reports.yml")
        content = workflow_path.read_text(encoding="utf-8")

        self.assertIn("15 14 * * 1-5", content)
        self.assertIn("15 20 * * 1-5", content)
        self.assertIn("workflow_dispatch", content)
        self.assertIn("report_type", content)
        self.assertIn("main", content)
        self.assertIn("close", content)
        self.assertIn("python-version: '3.12'", content)
        self.assertIn("python -m pip install -e .", content)
        self.assertIn("python -m advisor config validate --require-live", content)
        self.assertIn("python -m advisor report", content)
        self.assertIn("--require-live", content)
        self.assertIn("FMP_API_KEY: ${{ secrets.FMP_API_KEY }}", content)
        self.assertIn("COINGECKO_API_KEY: ${{ secrets.COINGECKO_API_KEY }}", content)
        self.assertIn("actions/upload-artifact", content)
        self.assertIn("reports/", content)
        self.assertNotIn("broker", content.lower())
        self.assertNotIn("place order", content.lower())
        self.assertIn("python -m advisor report main --include-discovery --require-live --output-dir reports", content)
        self.assertIn("python -m advisor report close --from-main --require-live --output-dir reports", content)
        self.assertIn("ADVISOR_STOCK_WATCHLIST: INTC,AMD,NVDA,HIMS,MU,MSFT,USAR,CRDO,DELL,MRVL,HOOD", content)
        self.assertIn("ADVISOR_CRYPTO_WATCHLIST: SOL,HYPE,BTC,ETH", content)

    def test_scheduled_report_type_selection_is_explicit(self) -> None:
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "financial-advisor-reports.yml"
        content = workflow_path.read_text(encoding="utf-8")

        self.assertIn("REPORT_TYPE=main", content)
        self.assertIn("REPORT_TYPE=close", content)
        self.assertIn("github.event.schedule", content)
        self.assertIn("15 20 * * 1-5", content)

    def test_workflow_persists_only_safe_market_cache(self) -> None:
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "financial-advisor-reports.yml"
        content = workflow_path.read_text(encoding="utf-8")

        self.assertIn("actions/cache/restore@v4", content)
        self.assertIn("actions/cache/save@v4", content)
        self.assertIn("data/advisor.db", content)
        self.assertIn("ADVISOR_ACTIONS_CACHE_HIT", content)
        self.assertNotIn(".env", content)

    def test_workflow_has_non_blocking_telegram_notification(self) -> None:
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "financial-advisor-reports.yml"
        content = workflow_path.read_text(encoding="utf-8")

        self.assertIn("TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}", content)
        self.assertIn("TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}", content)
        self.assertIn("python -m advisor notify-telegram", content)
        self.assertIn("continue-on-error: true", content)


if __name__ == "__main__":
    unittest.main()
