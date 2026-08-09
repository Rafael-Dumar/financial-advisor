from __future__ import annotations

import json
import os
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import advisor.cli as cli_module
from advisor.cache import SQLiteCache
from advisor.cli import main as advisor_main
from advisor.config import AdvisorConfig
from advisor.signal_observation import (
    SignalObservationWriteResult,
    SignalRunMetadata,
    build_signal_observation,
    create_run_metadata,
)
from tests.test_signal_observation import _decision, _snapshot


SOURCE_SHA = "b" * 40
FIXED_TIMESTAMP = "2026-08-09T02:30:00Z"


def _write_fixture(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "scan.json").write_text(
        json.dumps(
            {
                "account_capital": 50_000,
                "stock_regime": "neutral",
                "crypto_regime": "neutral",
                "sample_size": 120,
                "win_rate_2r": 0.58,
                "win_rate_3r": 0.34,
                "expected_value_r": 0.52,
                "assets": [
                    {
                        "symbol": "AMD",
                        "asset_type": "stock",
                        "theme": "semiconductors",
                        "market_cap": 100_000_000_000,
                        "average_volume": 5_000_000,
                        "revenue_growth": 0.18,
                        "eps_growth": 0.14,
                        "margin_trend": 0.04,
                        "free_cash_flow_positive": True,
                        "pe": 28,
                        "peg": 1.6,
                        "historical_pe": 30,
                        "days_to_earnings": 35,
                        "guidance_recent": False,
                        "post_earnings_gap_percent": 0.01,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _fixed_run_metadata(**kwargs: object) -> SignalRunMetadata:
    return SignalRunMetadata(
        schema_version="1.0",
        source_sha=str(kwargs["source_sha"]),
        run_id="github-run-1",
        run_origin="github",
        report_date_brt="2026-08-08",
        report_type=str(kwargs["report_type"]),
        signal_timestamp_utc=FIXED_TIMESTAMP,
    )


class SignalObservationIntegrationTests(unittest.TestCase):
    def test_github_retry_uses_stable_identity_and_financial_content_for_conflicts(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "ledger.db")
            with patch.dict(
                os.environ,
                {"GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "1"},
                clear=True,
            ):
                metadata_a = create_run_metadata(
                    source_sha=SOURCE_SHA,
                    report_type="main",
                    signal_timestamp_utc="2026-08-09T16:00:00Z",
                )
            with patch.dict(
                os.environ,
                {"GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "2"},
                clear=True,
            ):
                metadata_b = create_run_metadata(
                    source_sha=SOURCE_SHA,
                    report_type="main",
                    signal_timestamp_utc="2026-08-09T16:00:10Z",
                )

            observation_a = build_signal_observation(
                _decision(),
                _snapshot(),
                metadata_a,
                stock_regime="neutral",
                crypto_regime="neutral",
            )
            observation_b = build_signal_observation(
                _decision(),
                _snapshot(),
                metadata_b,
                stock_regime="neutral",
                crypto_regime="neutral",
            )

            self.assertEqual(observation_a.signal_id, observation_b.signal_id)
            self.assertEqual(observation_a.observation_hash, observation_b.observation_hash)
            self.assertEqual(cache.save_signal_observations([observation_a]).status, "written")
            self.assertEqual(cache.save_signal_observations([observation_b]).status, "duplicate_same")
            self.assertEqual(cache.count_signal_observations(), 1)

            divergent = replace(observation_b, decision_confidence_score=99)
            self.assertEqual(cache.save_signal_observations([divergent]).status, "conflict")
            self.assertEqual(cache.count_signal_observations(), 1)
            original = cache.load_signal_observations()[0]
            self.assertEqual(original["decision_confidence_score"], 82)
            self.assertEqual(original["signal_timestamp_utc"], "2026-08-09T16:00:00.000000Z")

    def test_new_github_run_and_local_invocations_create_new_observations(self):
        with TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "ledger.db")

            github_observations = []
            for run_id in ("123456", "123457"):
                with patch.dict(os.environ, {"GITHUB_RUN_ID": run_id}, clear=True):
                    metadata = create_run_metadata(
                        source_sha=SOURCE_SHA,
                        report_type="main",
                        signal_timestamp_utc="2026-08-09T16:00:00Z",
                    )
                github_observations.append(
                    build_signal_observation(
                        _decision(),
                        _snapshot(),
                        metadata,
                        stock_regime="neutral",
                        crypto_regime="neutral",
                    )
                )
            self.assertNotEqual(github_observations[0].signal_id, github_observations[1].signal_id)
            self.assertEqual(cache.save_signal_observations(github_observations).status, "written")

            local_observations = []
            for _ in range(2):
                with patch.dict(os.environ, {}, clear=True):
                    metadata = create_run_metadata(
                        source_sha=SOURCE_SHA,
                        report_type="main",
                        signal_timestamp_utc="2026-08-09T16:00:00Z",
                    )
                local_observations.append(
                    build_signal_observation(
                        _decision(),
                        _snapshot(),
                        metadata,
                        stock_regime="neutral",
                        crypto_regime="neutral",
                    )
                )
            self.assertNotEqual(local_observations[0].signal_id, local_observations[1].signal_id)
            self.assertEqual(cache.save_signal_observations(local_observations).status, "written")
            self.assertEqual(cache.count_signal_observations(), 4)

    def test_plain_scan_uses_legacy_journal_and_main_close_use_only_canonical_ledger(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "advisor.db"
            fixture = _write_fixture(root / "fixture")
            scan_output = root / "scan-report"
            scan_stdout = StringIO()
            with redirect_stdout(scan_stdout):
                self.assertEqual(
                    advisor_main(
                        [
                            "scan",
                            "--fixture-dir",
                            str(fixture),
                            "--db",
                            str(db_path),
                            "--output-dir",
                            str(scan_output),
                        ]
                    ),
                    0,
                )
            self.assertNotIn("signal_observation_status", scan_stdout.getvalue())
            cache = SQLiteCache(db_path)
            self.assertEqual(cache.count_signal_observations(), 0)
            self.assertEqual(len(cache.load_signal_journal()), 1)

            with patch("advisor.cli.AdvisorConfig.default", return_value=AdvisorConfig()), patch(
                "advisor.cli._resolve_source_sha", return_value=SOURCE_SHA
            ), patch("advisor.cli.create_run_metadata", side_effect=_fixed_run_metadata):
                main_output = root / "main-report"
                stdout = StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(
                        advisor_main(
                            [
                                "report",
                                "main",
                                "--db",
                                str(db_path),
                                "--output-dir",
                                str(main_output),
                            ]
                        ),
                        0,
                    )
                self.assertIn("signal_observation_status=written", stdout.getvalue())

            main_rows = cache.load_signal_observations()
            self.assertTrue(main_rows)
            self.assertTrue(all(row["report_type"] == "main" for row in main_rows))
            self.assertEqual(len(cache.load_signal_journal()), 1)

            with patch("advisor.cli.AdvisorConfig.default", return_value=AdvisorConfig()), patch(
                "advisor.cli._resolve_source_sha", return_value=SOURCE_SHA
            ), patch("advisor.cli.create_run_metadata", side_effect=_fixed_run_metadata):
                close_output = root / "close-report"
                self.assertEqual(
                    advisor_main(
                        [
                            "report",
                            "close",
                            "--db",
                            str(db_path),
                            "--output-dir",
                            str(close_output),
                        ]
                    ),
                    0,
                )

            all_rows = cache.load_signal_observations()
            self.assertGreater(len(all_rows), len(main_rows))
            self.assertEqual({row["report_type"] for row in all_rows}, {"main", "close"})
            self.assertEqual(len(cache.load_signal_journal()), 1)

    def test_ledger_status_variants_do_not_change_report_or_decision_authority(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "advisor.db"
            output_dir = root / "reports"
            real_markdown = cli_module.render_markdown_report
            real_analyst = cli_module.render_analyst_review_input
            real_assign = cli_module._assign_universe_origins

            def fixed_markdown(decisions, **kwargs):
                kwargs["generated_at"] = "2026-08-09T02:30:00+00:00"
                return real_markdown(decisions, **kwargs)

            def fixed_analyst(decisions, **kwargs):
                kwargs["generated_at"] = "2026-08-08T23:30:00-03:00"
                return real_analyst(decisions, **kwargs)

            def run_variant(save_side_effect=None, save_return=None):
                captured = []

                def assign(decisions, config):
                    assigned = real_assign(decisions, config)
                    captured.extend(assigned)
                    return assigned

                save_patch = patch.object(
                    SQLiteCache,
                    "save_signal_observations",
                    side_effect=save_side_effect,
                    return_value=save_return,
                ) if save_side_effect is not None or save_return is not None else patch.object(
                    SQLiteCache, "save_signal_observations"
                )
                with ExitStack() as stack:
                    stack.enter_context(patch("advisor.cli.AdvisorConfig.default", return_value=AdvisorConfig()))
                    stack.enter_context(patch("advisor.cli._resolve_source_sha", return_value=SOURCE_SHA))
                    stack.enter_context(patch("advisor.cli.create_run_metadata", side_effect=_fixed_run_metadata))
                    stack.enter_context(patch.object(cli_module, "render_markdown_report", side_effect=fixed_markdown))
                    stack.enter_context(patch.object(cli_module, "render_analyst_review_input", side_effect=fixed_analyst))
                    stack.enter_context(patch.object(cli_module, "_assign_universe_origins", side_effect=assign))
                    if save_side_effect is not None or save_return is not None:
                        stack.enter_context(save_patch)
                    stdout = StringIO()
                    with redirect_stdout(stdout):
                        code = advisor_main(
                            [
                                "report",
                                "main",
                                "--db",
                                str(db_path),
                                "--output-dir",
                                str(output_dir),
                            ]
                        )
                fingerprint = [
                    (
                        decision.symbol,
                        decision.decision,
                        decision.investment_quality_score,
                        decision.swing_trade_score,
                        decision.decision_confidence_score,
                        decision.data_quality_score,
                        tuple(decision.reason_codes),
                        repr(decision.risk_plan),
                    )
                    for decision in captured
                ]
                return code, stdout.getvalue(), fingerprint, (
                    (output_dir / "advisor-report.md").read_bytes(),
                    (output_dir / "analyst-review-input.md").read_bytes(),
                )

            first = run_variant()
            self.assertEqual(first[0], 0)
            self.assertIn("signal_observation_status=written", first[1])
            baseline_reports = first[3]

            duplicate = run_variant()
            self.assertEqual(duplicate[0], 0)
            self.assertIn("signal_observation_status=duplicate_same", duplicate[1])

            variants = [
                ("conflict", SignalObservationWriteResult(status="conflict"), None),
                ("unavailable", SignalObservationWriteResult(status="unavailable"), None),
                ("exception", None, RuntimeError("secret persistence failure")),
            ]
            for label, result, error in variants:
                with self.subTest(label=label):
                    current = run_variant(save_side_effect=error, save_return=result)
                    self.assertEqual(current[0], 0)
                    self.assertEqual(current[2], first[2])
                    self.assertEqual(current[3], baseline_reports)
                    self.assertNotIn("secret persistence failure", current[1])
                    if label == "exception":
                        self.assertIn("signal_observation_status=unavailable error_code=storage_error", current[1])
                    else:
                        self.assertIn(f"signal_observation_status={label}", current[1])

    def test_source_sha_unavailable_is_fail_open_and_does_not_write_rows(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "advisor.db"
            output_dir = root / "reports"
            stdout = StringIO()
            with (
                patch("advisor.cli.AdvisorConfig.default", return_value=AdvisorConfig()),
                patch("advisor.cli._resolve_source_sha", return_value=None),
                redirect_stdout(stdout),
            ):
                code = advisor_main(
                    [
                        "report",
                        "main",
                        "--db",
                        str(db_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn(
                "signal_observation_status=unavailable error_code=source_sha_unavailable",
                stdout.getvalue(),
            )
            self.assertTrue((output_dir / "advisor-report.md").exists())
            cache = SQLiteCache(db_path)
            self.assertEqual(cache.count_signal_observations(), 0)
            self.assertEqual(cache.load_signal_journal(), [])

if __name__ == "__main__":
    unittest.main()
