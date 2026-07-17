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
        self.assertIn("ADVISOR_MAX_STOCKS_PER_RUN: 11", content)
        self.assertIn("ADVISOR_FMP_CALL_BUDGET_PER_RUN: 90", content)
        self.assertNotIn("ADVISOR_MAX_STOCKS_PER_RUN: 2", content)

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

    def test_reports_workflow_does_not_send_preliminary_telegram(self) -> None:
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "financial-advisor-reports.yml"
        content = workflow_path.read_text(encoding="utf-8")

        self.assertNotIn("TELEGRAM_BOT_TOKEN", content)
        self.assertNotIn("TELEGRAM_CHAT_ID", content)
        self.assertNotIn("python -m advisor notify-telegram", content)

    def test_nightly_analyst_review_workflow_runs_without_codex(self) -> None:
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "financial-advisor-nightly-review.yml"

        self.assertTrue(workflow_path.exists(), "missing financial-advisor-nightly-review.yml")
        content = workflow_path.read_text(encoding="utf-8")

        self.assertIn("30 21 * * 1-5", content)
        self.assertIn("workflow_dispatch", content)
        self.assertIn("python-version: '3.12'", content)
        self.assertIn("contents: read", content)
        self.assertIn("actions: read", content)
        self.assertIn("GH_TOKEN: ${{ github.token }}", content)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", content)
        self.assertIn("GH_HOST: github.com", content)
        self.assertIn("GH_REPO: ${{ github.repository }}", content)
        self.assertIn("source_head_sha:", content)
        self.assertIn("send_telegram:", content)
        self.assertIn("allow_stale_diagnostic:", content)
        self.assertIn("default: false", content)
        self.assertIn("name: Validate GitHub API access", content)
        self.assertIn("scripts/validate-github-api-access.ps1", content)
        self.assertIn("ExpectedHeadSha = '${{ steps.nightly-inputs.outputs.source_head_sha }}'", content)
        self.assertIn("invalid_source_head_sha", content)
        self.assertIn("^[0-9a-fA-F]{40}$", content)
        self.assertIn("github.event_name == 'schedule'", content)
        self.assertIn("inputs.send_telegram", content)
        self.assertIn("nightly-review-metadata.json", content)
        self.assertIn("replay_reason", content)
        self.assertIn("nightly_auth_hotfix_validation", content)
        self.assertIn("telegram_sent", content)
        self.assertIn("id: telegram", content)
        self.assertIn(
            "if: ${{ github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && inputs.send_telegram) }}",
            content,
        )
        self.assertLess(content.index("Send analyst final Telegram"), content.index("Upload analyst final review artifact"))
        self.assertIn("scripts/fetch-latest-github-reports.ps1", content)
        self.assertIn("python -m advisor.analyst_review", content)
        self.assertIn("python -m advisor.telegram_notify analyst-final", content)
        self.assertIn("TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}", content)
        self.assertIn("TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}", content)
        self.assertIn("actions/upload-artifact", content)
        self.assertIn("reports/analyst-final-review.md", content)


if __name__ == "__main__":
    unittest.main()
