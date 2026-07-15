from __future__ import annotations

import unittest

from advisor.artifact_selection import ArtifactSelectionError, select_artifact_pair


SHA = "8497c24daca83175ee9911bd0a5a524f1b7b73ec"


def _candidate(
    report_type: str,
    run_id: int,
    *,
    brt_date: str = "2026-07-14",
    head_sha: str = SHA,
    event: str = "schedule",
    conclusion: str = "success",
    head_branch: str = "main",
    expired: bool = False,
    created_at: str | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    hour = "15:46:28" if report_type == "main" else "21:13:34"
    generated_hour = "15:46:49" if report_type == "main" else "21:13:56"
    return {
        "run_id": run_id,
        "created_at": created_at or f"{brt_date}T{hour}Z",
        "event": event,
        "conclusion": conclusion,
        "head_sha": head_sha,
        "head_branch": head_branch,
        "url": f"https://example.invalid/runs/{run_id}",
        "artifact_name": f"financial-advisor-{report_type}-{run_id}",
        "artifact_expired": expired,
        "artifact_created_at": created_at or f"{brt_date}T{hour}Z",
        "report_type": report_type,
        "report_brt_date": brt_date,
        "report_generated_at": generated_at or f"{brt_date}T{generated_hour}Z",
        "report_path": f"reports/history/{brt_date}-{report_type}.md",
    }


class Phase25ArtifactSelectionTests(unittest.TestCase):
    def test_selects_exact_scheduled_same_sha_pair_deterministically(self) -> None:
        candidates = [
            _candidate("close", 20),
            _candidate("main", 10),
            _candidate("main", 9, created_at="2026-07-14T15:00:00Z"),
        ]

        selection = select_artifact_pair(
            candidates,
            brt_date="2026-07-14",
            expected_head_sha=SHA,
            selected_at="2026-07-14T22:00:00Z",
        )

        self.assertEqual(selection.status, "valid_current_day")
        self.assertTrue(selection.operational_allowed)
        self.assertEqual(selection.main.run_id, 10)
        self.assertEqual(selection.close.run_id, 20)
        self.assertEqual(selection.main.head_sha, selection.close.head_sha)
        self.assertEqual(selection.artifact_age_seconds, 22412)

    def test_rejects_old_artifact_without_explicit_stale_flag(self) -> None:
        candidates = [_candidate("main", 10, brt_date="2026-07-13"), _candidate("close", 20, brt_date="2026-07-13")]

        with self.assertRaisesRegex(ArtifactSelectionError, "no_valid_current_day_artifact_pair"):
            select_artifact_pair(candidates, brt_date="2026-07-14", expected_head_sha=SHA)

    def test_stale_diagnostic_is_explicit_and_never_operational(self) -> None:
        candidates = [_candidate("main", 10, brt_date="2026-07-13"), _candidate("close", 20, brt_date="2026-07-13")]

        selection = select_artifact_pair(
            candidates,
            brt_date="2026-07-14",
            expected_head_sha=SHA,
            allow_stale_diagnostic=True,
            selected_at="2026-07-14T22:00:00Z",
        )

        self.assertEqual(selection.status, "stale_diagnostic")
        self.assertFalse(selection.operational_allowed)
        self.assertEqual(selection.source_date, "2026-07-13")
        self.assertGreater(selection.artifact_age_seconds, 0)

    def test_rejects_sha_mismatch(self) -> None:
        candidates = [
            _candidate("main", 10),
            _candidate("close", 20, head_sha="different"),
        ]

        with self.assertRaisesRegex(ArtifactSelectionError, "no_valid_current_day_artifact_pair"):
            select_artifact_pair(candidates, brt_date="2026-07-14", expected_head_sha=SHA)

    def test_manual_run_cannot_replace_scheduled_without_flag(self) -> None:
        candidates = [_candidate("main", 10, event="workflow_dispatch"), _candidate("close", 20)]

        with self.assertRaisesRegex(ArtifactSelectionError, "no_valid_current_day_artifact_pair"):
            select_artifact_pair(candidates, brt_date="2026-07-14", expected_head_sha=SHA)

        selection = select_artifact_pair(
            candidates,
            brt_date="2026-07-14",
            expected_head_sha=SHA,
            allow_manual=True,
        )
        self.assertEqual(selection.main.event, "workflow_dispatch")

    def test_rejects_main_and_close_from_different_market_dates(self) -> None:
        candidates = [_candidate("main", 10), _candidate("close", 20, brt_date="2026-07-13")]

        with self.assertRaisesRegex(ArtifactSelectionError, "no_valid_current_day_artifact_pair"):
            select_artifact_pair(candidates, brt_date="2026-07-14", expected_head_sha=SHA)

    def test_rejects_expired_or_content_mismatched_artifact(self) -> None:
        expired = [_candidate("main", 10, expired=True), _candidate("close", 20)]
        wrong_content = [_candidate("main", 10), {**_candidate("close", 20), "report_type": "main"}]

        for candidates in (expired, wrong_content):
            with self.assertRaisesRegex(ArtifactSelectionError, "no_valid_current_day_artifact_pair"):
                select_artifact_pair(candidates, brt_date="2026-07-14", expected_head_sha=SHA)

    def test_rejects_wrong_branch_failed_run_or_incoherent_generated_order(self) -> None:
        invalid_sets = [
            [_candidate("main", 10, head_branch="feature"), _candidate("close", 20)],
            [_candidate("main", 10, conclusion="failure"), _candidate("close", 20)],
            [
                _candidate("main", 10, generated_at="2026-07-14T21:30:00Z"),
                _candidate("close", 20, generated_at="2026-07-14T21:00:00Z"),
            ],
        ]

        for candidates in invalid_sets:
            with self.assertRaisesRegex(ArtifactSelectionError, "no_valid_current_day_artifact_pair"):
                select_artifact_pair(candidates, brt_date="2026-07-14", expected_head_sha=SHA)


if __name__ == "__main__":
    unittest.main()
