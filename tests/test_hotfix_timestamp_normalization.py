from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from advisor.artifact_selection import ArtifactSelectionError, _timestamp, select_artifact_pair


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FETCH_SCRIPT = PROJECT_ROOT / "scripts" / "fetch-latest-github-reports.ps1"
SHA = "6d0c1f705032606d6b449f79b8c151b941e1c037"


def _candidate(
    report_type: str,
    run_id: int,
    *,
    created_at: str,
    artifact_created_at: str | None = None,
) -> dict[str, object]:
    generated_at = "2026-07-15T15:52:00Z" if report_type == "main" else "2026-07-15T21:52:00Z"
    return {
        "run_id": run_id,
        "created_at": created_at,
        "event": "schedule",
        "conclusion": "success",
        "head_sha": SHA,
        "head_branch": "main",
        "url": f"https://example.invalid/runs/{run_id}",
        "artifact_name": f"financial-advisor-{report_type}-{run_id}",
        "artifact_expired": False,
        "artifact_created_at": artifact_created_at or created_at,
        "report_type": report_type,
        "report_brt_date": "2026-07-15",
        "report_generated_at": generated_at,
        "report_path": f"reports/history/2026-07-15-{report_type}.md",
    }


class HotfixTimestampNormalizationTests(unittest.TestCase):
    def test_iso_timestamp_variants_are_accepted(self) -> None:
        expected = datetime(2026, 7, 15, 15, 51, 43, tzinfo=timezone.utc)
        variants = (
            "2026-07-15T15:51:43Z",
            "2026-07-15T15:51:43+00:00",
            "2026-07-15T15:51:43.000000Z",
            "2026-07-15T12:51:43-03:00",
        )

        for value in variants:
            with self.subTest(value=value):
                self.assertEqual(_timestamp(value, field="created_at"), expected)

    def test_runner_legacy_created_at_is_accepted_as_utc(self) -> None:
        created_at = "07/15/2026 15:51:43"

        parsed = _timestamp(created_at, field="created_at")

        self.assertEqual(parsed, datetime(2026, 7, 15, 15, 51, 43, tzinfo=timezone.utc))

    def test_invalid_timestamp_raises_sanitized_artifact_selection_error(self) -> None:
        invalid_value = "not-iso:C:\\private\\payload.json"

        with self.assertRaises(ArtifactSelectionError) as context:
            _timestamp(invalid_value, field="created_at")

        self.assertEqual(
            str(context.exception),
            "invalid_artifact_timestamp:field=created_at:value_format=non_iso",
        )
        self.assertNotIn(invalid_value, str(context.exception))

    def test_timezone_free_iso_is_rejected_instead_of_assuming_local_time(self) -> None:
        with self.assertRaisesRegex(
            ArtifactSelectionError,
            r"^invalid_artifact_timestamp:field=report_generated_at:value_format=non_iso$",
        ):
            _timestamp("2026-07-15T15:51:43", field="report_generated_at")

    def test_legacy_timestamps_preserve_order_and_artifact_age(self) -> None:
        candidates = [
            _candidate("main", 10, created_at="07/15/2026 15:51:43"),
            _candidate("main", 9, created_at="07/15/2026 14:51:43"),
            _candidate("close", 20, created_at="07/15/2026 21:51:43"),
        ]

        selection = select_artifact_pair(
            candidates,
            brt_date="2026-07-15",
            expected_head_sha=SHA,
            selected_at="2026-07-15T22:00:00Z",
        )

        self.assertEqual(selection.main.run_id, 10)
        self.assertEqual(selection.close.run_id, 20)
        self.assertEqual(selection.main.event, "schedule")
        self.assertEqual(selection.main.head_sha, SHA)
        self.assertEqual(selection.close.head_sha, SHA)
        self.assertEqual(selection.artifact_age_seconds, 22097)

    def test_real_powershell_candidate_json_is_accepted_by_python_selector(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("PowerShell is required for the producer-to-parser integration test")

        probe = r'''
param(
    [string]$FetchScript,
    [string]$Python
)
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $FetchScript,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) { throw 'fetch_script_parse_failed' }
$functionAst = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'ConvertTo-Iso8601UtcString'
}, $true)
if ($null -eq $functionAst) { throw 'timestamp_normalizer_function_missing' }
. ([scriptblock]::Create($functionAst.Extent.Text))

$githubMain = '{"createdAt":"2026-07-15T15:51:43Z","artifactCreatedAt":"2026-07-15T15:52:03Z"}' | ConvertFrom-Json
$githubClose = '{"createdAt":"2026-07-15T21:51:43+00:00","artifactCreatedAt":"2026-07-15T21:52:03+00:00"}' | ConvertFrom-Json
$dateTimeValue = [datetime]'2026-07-15T15:51:43Z'
$dateTimeOffsetValue = [datetimeoffset]'2026-07-15T12:51:43-03:00'

$candidateRecords = @(
    [pscustomobject]@{
        run_id = 10
        created_at = ConvertTo-Iso8601UtcString $githubMain.createdAt
        event = 'schedule'
        conclusion = 'success'
        head_sha = '6d0c1f705032606d6b449f79b8c151b941e1c037'
        head_branch = 'main'
        url = 'https://example.invalid/runs/10'
        artifact_name = 'financial-advisor-main-10'
        artifact_expired = $false
        artifact_created_at = ConvertTo-Iso8601UtcString $githubMain.artifactCreatedAt
        report_type = 'main'
        report_brt_date = '2026-07-15'
        report_generated_at = ConvertTo-Iso8601UtcString '2026-07-15T12:52:00-03:00'
        report_path = 'reports/history/2026-07-15-main.md'
    },
    [pscustomobject]@{
        run_id = 20
        created_at = ConvertTo-Iso8601UtcString $githubClose.createdAt
        event = 'schedule'
        conclusion = 'success'
        head_sha = '6d0c1f705032606d6b449f79b8c151b941e1c037'
        head_branch = 'main'
        url = 'https://example.invalid/runs/20'
        artifact_name = 'financial-advisor-close-20'
        artifact_expired = $false
        artifact_created_at = ConvertTo-Iso8601UtcString $githubClose.artifactCreatedAt
        report_type = 'close'
        report_brt_date = '2026-07-15'
        report_generated_at = ConvertTo-Iso8601UtcString '2026-07-15T18:52:00-03:00'
        report_path = 'reports/history/2026-07-15-close.md'
    }
)
$candidateJson = ConvertTo-Json -InputObject $candidateRecords -Depth 8
$selectionJson = $candidateJson | & $Python -m advisor.artifact_selection --brt-date 2026-07-15 --expected-head-sha 6d0c1f705032606d6b449f79b8c151b941e1c037
if ($LASTEXITCODE -ne 0) { throw 'python_artifact_selection_failed' }
$selection = $selectionJson | Out-String | ConvertFrom-Json
[pscustomobject]@{
    datetime_value = ConvertTo-Iso8601UtcString $dateTimeValue
    datetimeoffset_value = ConvertTo-Iso8601UtcString $dateTimeOffsetValue
    main_run_id = $selection.main.run_id
    close_run_id = $selection.close.run_id
    main_created_at = $selection.main.created_at
    close_created_at = $selection.close.created_at
} | ConvertTo-Json -Compress
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            probe_path = Path(temp_dir) / "timestamp-probe.ps1"
            probe_path.write_text(probe, encoding="utf-8")
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(probe_path),
                    "-FetchScript",
                    str(FETCH_SCRIPT),
                    "-Python",
                    sys.executable,
                ],
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        payload = json.loads(completed.stdout)
        expected_utc = "2026-07-15T15:51:43.0000000Z"
        self.assertEqual(payload["datetime_value"], expected_utc)
        self.assertEqual(payload["datetimeoffset_value"], expected_utc)
        self.assertEqual(payload["main_run_id"], 10)
        self.assertEqual(payload["close_run_id"], 20)
        self.assertRegex(payload["main_created_at"], r"^2026-07-15T15:51:43(?:\.0+)?Z$")
        self.assertRegex(payload["close_created_at"], r"^2026-07-15T21:51:43(?:\.0+)?Z$")


if __name__ == "__main__":
    unittest.main()
