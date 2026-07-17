from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FETCH_SCRIPT = PROJECT_ROOT / "scripts" / "fetch-latest-github-reports.ps1"
VALIDATE_SCRIPT = PROJECT_ROOT / "scripts" / "validate-github-api-access.ps1"
NIGHTLY_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "financial-advisor-nightly-review.yml"
SOURCE_SHA = "bf7792c8d15fb7d4e864106b9a396588943a6578"
TOKEN_MARKER = "token-must-never-appear-in-output"


class NightlyAuthDryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.powershell = shutil.which("pwsh") or shutil.which("powershell")
        if self.powershell is None:
            self.skipTest("PowerShell is required")

    def _fake_gh(self, root: Path) -> tuple[Path, Path]:
        fake_dir = root / "fake-bin"
        fake_dir.mkdir(parents=True)
        log_path = root / "gh-calls.jsonl"
        fake_python = fake_dir / "fake_gh.py"
        fake_python.write_text(
            textwrap.dedent(
                '''
                from __future__ import annotations

                import json
                import os
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                log_path = Path(os.environ["FAKE_GH_LOG"])
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(args) + "\\n")

                scenario = os.environ.get("FAKE_GH_SCENARIO", "success")
                brt_date = os.environ["FAKE_BRT_DATE"]
                source_sha = os.environ["FAKE_SOURCE_SHA"]
                main_created = f"{brt_date}T15:00:00Z"
                close_created = f"{brt_date}T21:00:00Z"

                if args[:2] == ["auth", "status"]:
                    print("auth status intentionally fails", file=sys.stderr)
                    raise SystemExit(1)

                if args[:2] == ["api", "repos/test/repo"]:
                    if scenario == "repo_denied":
                        print("HTTP 401", file=sys.stderr)
                        raise SystemExit(1)
                    print("test/repo")
                    raise SystemExit(0)

                if args[:2] == ["api", "repos/test/repo/actions/runs?per_page=1"]:
                    if scenario == "actions_denied":
                        print("HTTP 403", file=sys.stderr)
                        raise SystemExit(1)
                    if scenario == "api_unavailable":
                        print("connection timeout", file=sys.stderr)
                        raise SystemExit(1)
                    print("2")
                    raise SystemExit(0)

                if args[:2] == ["run", "list"]:
                    if scenario == "unauthorized":
                        print("HTTP 401", file=sys.stderr)
                        raise SystemExit(1)
                    print(json.dumps([
                        {
                            "databaseId": 101,
                            "createdAt": main_created,
                            "displayTitle": "main",
                            "status": "completed",
                            "conclusion": "success",
                            "url": "https://example.invalid/runs/101",
                            "workflowName": "Financial Advisor Reports",
                            "event": "schedule",
                            "headSha": source_sha,
                            "headBranch": "main",
                        },
                        {
                            "databaseId": 202,
                            "createdAt": close_created,
                            "displayTitle": "close",
                            "status": "completed",
                            "conclusion": "success",
                            "url": "https://example.invalid/runs/202",
                            "workflowName": "Financial Advisor Reports",
                            "event": "schedule",
                            "headSha": source_sha,
                            "headBranch": "main",
                        },
                    ]))
                    raise SystemExit(0)

                if args[:2] == ["run", "view"]:
                    run_id = int(args[2])
                    report_type = "main" if run_id == 101 else "close"
                    created_at = main_created if report_type == "main" else close_created
                    print(json.dumps({
                        "databaseId": run_id,
                        "createdAt": created_at,
                        "url": f"https://example.invalid/runs/{run_id}",
                        "status": "completed",
                        "conclusion": "success",
                        "name": "Financial Advisor Reports",
                        "workflowName": "Financial Advisor Reports",
                        "headBranch": "main",
                        "headSha": source_sha,
                        "event": "schedule",
                    }))
                    raise SystemExit(0)

                if args and args[0] == "api" and "/actions/runs/" in args[1] and args[1].endswith("/artifacts"):
                    run_id = int(args[1].split("/")[-2])
                    report_type = "main" if run_id == 101 else "close"
                    created_at = main_created if report_type == "main" else close_created
                    print(json.dumps({"artifacts": [{
                        "name": f"financial-advisor-{report_type}-{run_id}",
                        "expired": False,
                        "created_at": created_at,
                    }]}))
                    raise SystemExit(0)

                if args[:2] == ["run", "download"]:
                    run_id = int(args[2])
                    report_type = "main" if run_id == 101 else "close"
                    output_dir = Path(args[args.index("--dir") + 1])
                    output_dir.mkdir(parents=True, exist_ok=True)
                    generated_at = main_created if report_type == "main" else close_created
                    (output_dir / f"{brt_date}-{report_type}.md").write_text(
                        "# Fixture report\\n\\n"
                        f"- report_type: `{report_type}`\\n"
                        f"- Generated at: `{generated_at}`\\n"
                        "- Data mode: `live`\\n"
                        "- primary_report_grade: `decision_grade`\\n"
                        "- overall_report_grade: `decision_grade`\\n"
                        "- primary_market_session: `regular`\\n"
                        "- Decisao geral: `no_trade`\\n",
                        encoding="utf-8",
                    )
                    (output_dir / "analyst-review-input.md").write_text(
                        f"# {report_type} fixture\\n",
                        encoding="utf-8",
                    )
                    raise SystemExit(0)

                print("unexpected fake gh command", file=sys.stderr)
                raise SystemExit(2)
                '''
            ).lstrip(),
            encoding="utf-8",
        )
        wrapper = fake_dir / "gh.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{fake_python}" %*\r\n',
            encoding="utf-8",
        )
        python_wrapper = fake_dir / "python.cmd"
        python_wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" %*\r\n',
            encoding="utf-8",
        )
        return fake_dir, log_path

    def _metadata_script(self) -> str:
        workflow = NIGHTLY_WORKFLOW.read_text(encoding="utf-8")
        step = workflow.split("      - name: Record nightly execution metadata\n", 1)[1]
        step = step.split("\n      - name:", 1)[0]
        run_block = step.split("        run: |\n", 1)[1]
        script = textwrap.dedent(run_block)
        replacements = {
            "${{ github.event_name }}": "workflow_dispatch",
            "${{ github.sha }}": "0123456789abcdef0123456789abcdef01234567",
            "${{ steps.nightly-inputs.outputs.source_head_sha }}": SOURCE_SHA,
            "${{ steps.nightly-inputs.outputs.dry_run }}": "true",
            "${{ steps.nightly-inputs.outputs.replay_reason }}": "nightly_auth_hotfix_validation",
        }
        for expression, value in replacements.items():
            script = script.replace(expression, value)
        return script

    def _run_metadata_script(self, nightly_fields: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
        (PROJECT_ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".tmp") as temp_dir:
            root = Path(temp_dir)
            reports = root / "reports"
            reports.mkdir()
            (reports / "nightly-review-input.md").write_text(nightly_fields, encoding="utf-8")
            script_path = root / "record-nightly-metadata.ps1"
            script_path.write_text(self._metadata_script(), encoding="utf-8")
            env = os.environ.copy()
            env["TELEGRAM_STEP_OUTCOME"] = "skipped"
            completed = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                ],
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            metadata_path = reports / "nightly-review-metadata.json"
            metadata = None
            if metadata_path.exists():
                metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
            return completed, metadata

    def test_powershell_automatic_input_shadows_outer_markdown(self) -> None:
        script = textwrap.dedent(
            """
            $input = '- main_run_id: `29512805075`'
            function Read-BrokenField {
              $match = [regex]::Match($input, '(?m)^- main_run_id:')
              if (-not $match.Success) { throw 'automatic_input_shadowed_outer_content' }
            }
            Read-BrokenField
            """
        )
        completed = subprocess.run(
            [self.powershell, "-NoProfile", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("automatic_input_shadowed_outer_content", completed.stderr + completed.stdout)

    def test_real_metadata_step_reads_fields_via_explicit_content_parameter(self) -> None:
        completed, metadata = self._run_metadata_script(
            textwrap.dedent(
                """
                # Nightly review input
                - main_run_id: `29512805075`
                - close_run_id: `29535264127`
                - main_event: `schedule`
                - close_event: `schedule`
                - artifact_selection_status: `valid_current_day`
                """
            ).lstrip()
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertEqual(
            metadata,
            {
                "workflow_event": "workflow_dispatch",
                "runtime_sha": "0123456789abcdef0123456789abcdef01234567",
                "source_report_sha": SOURCE_SHA,
                "main_run_id": 29512805075,
                "close_run_id": 29535264127,
                "main_event": "schedule",
                "close_event": "schedule",
                "artifact_selection_status": "valid_current_day",
                "dry_run": True,
                "telegram_sent": False,
                "replay_reason": "nightly_auth_hotfix_validation",
            },
        )

    def test_real_metadata_step_reports_sanitized_missing_field(self) -> None:
        completed, metadata = self._run_metadata_script(
            textwrap.dedent(
                """
                - close_run_id: `29535264127`
                - main_event: `schedule`
                - close_event: `schedule`
                - artifact_selection_status: `valid_current_day`
                """
            ).lstrip()
        )
        output = completed.stderr + completed.stdout

        self.assertNotEqual(completed.returncode, 0)
        self.assertIsNone(metadata)
        self.assertIn("nightly_metadata_field_missing:main_run_id", output)
        self.assertNotIn(SOURCE_SHA, output)

    def test_dry_run_skips_telegram_and_upload_follows_metadata(self) -> None:
        workflow = NIGHTLY_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn(
            "if: ${{ github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && inputs.send_telegram) }}",
            workflow,
        )
        self.assertLess(
            workflow.index("- name: Record nightly execution metadata"),
            workflow.index("- name: Upload analyst final review artifact"),
        )

    def _environment(self, fake_dir: Path, log_path: Path, *, with_token: bool = True) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = str(fake_dir) + os.pathsep + env.get("PATH", "")
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        env["FAKE_GH_LOG"] = str(log_path)
        brt = timezone(timedelta(hours=-3))
        env["FAKE_BRT_DATE"] = datetime.now(brt).date().isoformat()
        env["FAKE_SOURCE_SHA"] = SOURCE_SHA
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        if with_token:
            env["GH_TOKEN"] = TOKEN_MARKER
        return env

    def _run_fetch(
        self,
        scenario: str,
        *,
        with_token: bool = True,
        extra_args: list[str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]], str]:
        (PROJECT_ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".tmp") as temp_dir:
            root = Path(temp_dir)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            copied_script = scripts_dir / FETCH_SCRIPT.name
            shutil.copy2(FETCH_SCRIPT, copied_script)
            fake_dir, log_path = self._fake_gh(root)
            env = self._environment(fake_dir, log_path, with_token=with_token)
            env["FAKE_GH_SCENARIO"] = scenario
            command = [
                    self.powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(copied_script),
                    "-Repo",
                    "test/repo",
                    "-ExpectedHeadSha",
                    SOURCE_SHA,
                ]
            command.extend(extra_args or [])
            completed = subprocess.run(
                command,
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            calls = []
            if log_path.exists():
                calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            if completed.returncode == 0:
                input_path = root / "reports" / "nightly-review-input.md"
                self.assertTrue(input_path.exists())
                input_content = input_path.read_text(encoding="utf-8-sig")
            else:
                input_content = ""
            return completed, calls, input_content

    def test_real_operations_pass_even_when_auth_status_would_fail(self) -> None:
        completed, calls, _ = self._run_fetch("success")

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertNotIn(["auth", "status"], calls)
        self.assertIn(["run", "list", "--repo", "test/repo", "--workflow", "Financial Advisor Reports", "--limit", "20", "--json", "databaseId,createdAt,displayTitle,status,conclusion,url,workflowName,event,headSha,headBranch"], calls)

    def test_missing_token_fails_before_any_external_call(self) -> None:
        completed, calls, _ = self._run_fetch("success", with_token=False)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("github_token_missing", completed.stderr + completed.stdout)
        self.assertEqual(calls, [])

    def test_unauthorized_run_list_is_sanitized_and_never_logs_token(self) -> None:
        completed, _, _ = self._run_fetch("unauthorized")
        output = completed.stderr + completed.stdout

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "github_api_call_failed:operation=list_workflow_runs:exit_code=1",
            output,
        )
        self.assertNotIn(TOKEN_MARKER, output)
        self.assertNotIn("HTTP 401", output)

    def test_manual_dry_run_metadata_preserves_scheduled_source_runs(self) -> None:
        runtime_sha = "0123456789abcdef0123456789abcdef01234567"
        completed, _, input_content = self._run_fetch(
            "success",
            extra_args=[
                "-WorkflowEvent",
                "workflow_dispatch",
                "-RuntimeSha",
                runtime_sha,
                "-DryRun",
                "-ReplayReason",
                "nightly_auth_hotfix_validation",
            ],
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        expected_fields = (
            "- workflow_event: `workflow_dispatch`",
            f"- runtime_sha: `{runtime_sha}`",
            f"- source_report_sha: `{SOURCE_SHA}`",
            "- main_run_id: `101`",
            "- close_run_id: `202`",
            "- main_event: `schedule`",
            "- close_event: `schedule`",
            "- artifact_selection_status: `valid_current_day`",
            "- dry_run: `true`",
            "- telegram_sent: `false`",
            "- replay_reason: `nightly_auth_hotfix_validation`",
        )
        for field in expected_fields:
            with self.subTest(field=field):
                self.assertIn(field, input_content)

    def _run_validate(self, scenario: str) -> subprocess.CompletedProcess[str]:
        (PROJECT_ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".tmp") as temp_dir:
            root = Path(temp_dir)
            fake_dir, log_path = self._fake_gh(root)
            env = self._environment(fake_dir, log_path)
            env["FAKE_GH_SCENARIO"] = scenario
            env["GH_REPO"] = "test/repo"
            return subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(VALIDATE_SCRIPT),
                ],
                cwd=PROJECT_ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

    def test_repo_and_actions_api_access_passes(self) -> None:
        completed = self._run_validate("success")
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("github_api_access_validated", completed.stdout)
        self.assertNotIn(TOKEN_MARKER, completed.stdout + completed.stderr)

    def test_actions_api_denied_is_classified(self) -> None:
        completed = self._run_validate("actions_denied")
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("github_actions_read_denied", output)
        self.assertNotIn(TOKEN_MARKER, output)

    def test_repository_api_denied_is_classified(self) -> None:
        completed = self._run_validate("repo_denied")
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("github_repository_read_denied", output)
        self.assertNotIn(TOKEN_MARKER, output)

    def test_github_api_unavailable_is_classified(self) -> None:
        completed = self._run_validate("api_unavailable")
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("github_api_unavailable:operation=read_actions:exit_code=1", output)
        self.assertNotIn(TOKEN_MARKER, output)

    def test_validation_script_uses_safe_read_queries_only(self) -> None:
        content = VALIDATE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(".full_name", content)
        self.assertIn(".total_count", content)
        self.assertNotIn("auth status", content)
        self.assertNotIn("auth token", content)
        self.assertNotIn("--show-token", content)
        self.assertNotRegex(content, r"(?m)^\s*(env|set)\s*$")
