from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AutomationScriptsTests(unittest.TestCase):
    def test_automation_scripts_enforce_live_reporting_contract(self) -> None:
        scripts = [
            ("run-main-report.ps1", "--include-discovery", "main"),
            ("run-close-report.ps1", "", "close"),
        ]

        for script_name, expected_scan_flag, history_label in scripts:
            script_path = PROJECT_ROOT / "scripts" / script_name
            self.assertTrue(script_path.exists(), f"missing {script_name}")
            content = script_path.read_text(encoding="utf-8")

            self.assertIn(".venv\\Scripts\\Activate.ps1", content)
            self.assertIn("advisor config validate --require-live", content)
            self.assertIn("advisor scan", content)
            self.assertIn("--require-live", content)
            self.assertIn(".tmp\\logs", content)
            self.assertIn("reports\\latest.md", content)
            self.assertIn(f"{history_label}.md", content)
            self.assertIn("Data mode: `live`", content)
            self.assertIn(".Contains('Data mode: `live`')", content)
            self.assertNotIn("-notlike '*Data mode: `live`*'", content)
            self.assertIn("live_validation_failed", content)
            if expected_scan_flag:
                self.assertIn(expected_scan_flag, content)

    def test_fetch_latest_github_reports_script_contract(self) -> None:
        script_path = PROJECT_ROOT / "scripts" / "fetch-latest-github-reports.ps1"
        self.assertTrue(script_path.exists(), "missing fetch-latest-github-reports.ps1")
        content = script_path.read_text(encoding="utf-8")

        self.assertIn("Get-Command gh", content)
        self.assertIn("gh auth status", content)
        self.assertIn("gh run list", content)
        self.assertIn("Financial Advisor Reports", content)
        self.assertIn("gh run view", content)
        self.assertIn("gh run download", content)
        self.assertIn("Remove-Item -LiteralPath $downloadDir -Recurse -Force", content)
        self.assertNotIn("databaseId,createdAt,url,artifacts", content)
        self.assertNotRegex(content, r"--json', '[^']*artifacts")
        self.assertNotIn("view.artifacts", content)
        self.assertIn(".tmp\\nightly-review", content)
        self.assertIn("reports\\nightly-review-input.md", content)
        self.assertIn("analyst-review-input.md", content)
        self.assertIn("main_baseline_missing", content)
        self.assertIn("blocked_or_diagnostic", content)
        self.assertIn("Public Equity Investing", content)
        self.assertIn("no broker", content.lower())
        self.assertIn("no order execution", content.lower())
        self.assertNotIn("FMP_API_KEY", content)
        self.assertNotIn("COINGECKO_API_KEY", content)

    def test_docs_explain_nightly_review_artifact_fetch(self) -> None:
        doc_path = PROJECT_ROOT / "docs" / "AUTOMATION_SETUP.md"
        content = doc_path.read_text(encoding="utf-8")

        self.assertIn("## Nightly qualitative review prep", content)
        self.assertIn("GitHub CLI", content)
        self.assertIn("gh auth login", content)
        self.assertIn("scripts\\fetch-latest-github-reports.ps1", content)
        self.assertIn(".tmp\\nightly-review\\YYYY-MM-DD", content)
        self.assertIn("reports\\nightly-review-input.md", content)
        self.assertIn("gh run download", content)
        self.assertIn("does not rely on the `artifacts` JSON field", content)
        self.assertIn("Public Equity Investing", content)

    def test_send_analyst_final_telegram_script_contract(self) -> None:
        script_path = PROJECT_ROOT / "scripts" / "send-analyst-final-telegram.ps1"
        self.assertTrue(script_path.exists(), "missing send-analyst-final-telegram.ps1")
        content = script_path.read_text(encoding="utf-8")

        self.assertIn("reports\\analyst-final-review.md", content)
        self.assertIn("advisor.telegram_notify", content)
        self.assertIn("analyst-final", content)
        self.assertIn("analyst_final_review_missing", content)
        self.assertIn("TELEGRAM_BOT_TOKEN", content)
        self.assertIn("TELEGRAM_CHAT_ID", content)
        self.assertNotIn("Write-Host $env:TELEGRAM_BOT_TOKEN", content)
        self.assertNotIn("reports\\latest.md", content)
        self.assertNotIn("latest.md", content)
        self.assertNotIn("comprar agora", content.lower())
        self.assertNotIn("vender agora", content.lower())

    def test_run_nightly_analyst_review_send_telegram_option(self) -> None:
        script_path = PROJECT_ROOT / "scripts" / "run-nightly-analyst-review.ps1"
        self.assertTrue(script_path.exists(), "missing run-nightly-analyst-review.ps1")
        content = script_path.read_text(encoding="utf-8")

        self.assertIn("[switch]$SendTelegram", content)
        self.assertIn("fetch-latest-github-reports.ps1", content)
        self.assertIn("reports\\analyst-final-review.md", content)
        self.assertIn("reports\\history", content)
        self.assertIn("send-analyst-final-telegram.ps1", content)
        self.assertIn("advisor.analyst_review", content)
        self.assertIn("--input-path", content)
        self.assertIn("--output-path", content)
        self.assertIn("--history-path", content)
        self.assertNotIn("nao `regular,unknown`", content)

    def test_docs_explain_optional_telegram_for_nightly_review(self) -> None:
        doc_path = PROJECT_ROOT / "docs" / "AUTOMATION_SETUP.md"
        content = doc_path.read_text(encoding="utf-8")

        self.assertIn("## Optional Telegram for nightly analyst review", content)
        self.assertIn("BotFather", content)
        self.assertIn("TELEGRAM_BOT_TOKEN", content)
        self.assertIn("TELEGRAM_CHAT_ID", content)
        self.assertIn("send-analyst-final-telegram.ps1", content)
        self.assertIn("run-nightly-analyst-review.ps1\" -SendTelegram", content)
        self.assertIn("does not send `latest.md`", content)
        self.assertIn("does not print the bot token", content)
