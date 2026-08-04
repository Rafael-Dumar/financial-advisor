from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _extract_report_invocations(content: str) -> list[list[str]]:
    """Return shell tokens only from executable report command lines."""

    invocations: list[list[str]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = shlex.split(stripped, comments=True, posix=True)
        if tokens[:4] != ["python", "-m", "advisor", "report"]:
            continue
        if len(tokens) < 5 or tokens[4] not in {"main", "close"}:
            continue
        invocations.append(tokens)
    return invocations


def _extract_named_step(content: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = content.index(marker)
    end = content.find("\n      - name:", start + len(marker))
    if end == -1:
        end = len(content)
    return content[start:end]


def _extract_github_actions_section(content: str) -> str:
    start = content.index("## GitHub Actions")
    end = content.find("\n## ", start + len("## GitHub Actions"))
    if end == -1:
        end = len(content)
    return content[start:end]


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
        self.assertIn("ADVISOR_STOCK_WATCHLIST: INTC,AMD,NVDA,HIMS,MU,MSFT,USAR,CRDO,DELL,MRVL,HOOD", content)
        self.assertIn("ADVISOR_CRYPTO_WATCHLIST: SOL,HYPE,BTC,ETH", content)
        self.assertIn("ADVISOR_MAX_STOCKS_PER_RUN: 11", content)
        self.assertIn("ADVISOR_FMP_CALL_BUDGET_PER_RUN: 90", content)
        self.assertNotIn("ADVISOR_MAX_STOCKS_PER_RUN: 2", content)

    def test_report_invocations_are_exactly_main_and_close_with_runtime_flag(self) -> None:
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "financial-advisor-reports.yml"
        content = workflow_path.read_text(encoding="utf-8")

        invocations = _extract_report_invocations(content)

        self.assertEqual(len(invocations), 2)
        self.assertEqual([tokens[4] for tokens in invocations], ["main", "close"])
        for tokens in invocations:
            self.assertEqual(tokens.count("--runtime-scoring-artifact"), 1)
            self.assertIn("--require-live", tokens)
            output_index = tokens.index("--output-dir")
            self.assertEqual(tokens[output_index + 1], "reports")
        self.assertIn("--include-discovery", invocations[0])
        self.assertIn("--from-main", invocations[1])

    def test_report_generation_precedes_single_parent_upload(self) -> None:
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "financial-advisor-reports.yml"
        content = workflow_path.read_text(encoding="utf-8")

        self.assertLess(
            content.index("      - name: Generate report"),
            content.index("      - name: Upload report artifact"),
        )

    def test_report_upload_is_one_unfiltered_reports_artifact(self) -> None:
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "financial-advisor-reports.yml"
        content = workflow_path.read_text(encoding="utf-8")
        upload_step = _extract_named_step(content, "Upload report artifact")

        self.assertEqual(content.count("uses: actions/upload-artifact@v4"), 1)
        self.assertIn("name: financial-advisor-${{ env.REPORT_TYPE }}-${{ github.run_id }}", upload_step)
        self.assertEqual(re.findall(r"(?m)^\s*path:\s*(\S+)\s*$", upload_step), ["reports/"])
        self.assertIn("if-no-files-found: error", upload_step)
        for forbidden in ("reports/runtime", "*.md", "*.html", "*.json", "exclude"):
            self.assertNotIn(forbidden, upload_step.lower())

    def test_runtime_absence_is_not_a_workflow_gate(self) -> None:
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "financial-advisor-reports.yml"
        content = workflow_path.read_text(encoding="utf-8").lower()

        self.assertNotIn("reports/runtime", content)
        self.assertNotIn("artifact_status", content)
        self.assertNotIn("exit 1", content)
        self.assertNotRegex(content, r"\b(?:partial|failed)\b")

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

    def test_nightly_workflow_does_not_consume_runtime_artifact(self) -> None:
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "financial-advisor-nightly-review.yml"
        content = workflow_path.read_text(encoding="utf-8")

        self.assertNotIn("--runtime-scoring-artifact", content)
        self.assertNotIn("scoring-runtime-trace", content)
        self.assertNotIn("artifact_status", content)

    def test_automation_setup_documents_runtime_artifact_contract(self) -> None:
        docs_path = PROJECT_ROOT / "docs" / "AUTOMATION_SETUP.md"
        section = _extract_github_actions_section(docs_path.read_text(encoding="utf-8"))
        normalized = section.casefold()
        lines = [line.casefold() for line in section.splitlines()]

        self.assertTrue(any("main" in line and "--runtime-scoring-artifact" in line for line in lines))
        self.assertTrue(any("close" in line and "--runtime-scoring-artifact" in line for line in lines))
        self.assertRegex(normalized, r"(?:mesmo|same).{0,80}artifact")
        for format_name in ("single", "chunked", "failed"):
            self.assertIn(format_name, normalized)
        for concept in ("ausente", "fail-open", "scan", "upload"):
            self.assertIn(concept, normalized)
        for concept in ("nightly", "final review", "telegram"):
            self.assertIn(concept, normalized)
        self.assertIn("auditoria", normalized)
        self.assertIn("autoriza", normalized)
        self.assertIn("trade", normalized)
        self.assertIn("broker", normalized)
        self.assertIn("ordem", normalized)

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
