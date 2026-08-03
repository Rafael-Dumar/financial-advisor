from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import advisor.cli as cli_module
import advisor.scoring as scoring_module
from advisor.cache import SQLiteCache
from advisor.cli import main as advisor_main
from advisor.config import AdvisorConfig
from advisor.runtime_scoring_artifact import ArtifactAssetInput
from advisor.runtime_scoring_observability import ObservationContext, asset_decision_sha256


BASE_SHA = "e0119c49847eff89f9eb0a9231a33d5c6914b39c"
FALLBACK_SHA = "b" * 64
FIXED_NOW_UTC = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
FIXED_REPORT_UTC = "2026-08-01T15:00:00+00:00"
FIXED_REPORT_BRT = "2026-08-01T12:00:00-03:00"
BRT = timezone(timedelta(hours=-3))


def _candles(count: int = 220) -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    for index in range(count):
        close = 100 + index * 0.4
        rows.append(
            {
                "date": f"2026-01-{(index % 28) + 1:02d}",
                "open": close - 0.2,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_000_000,
            }
        )
    return rows


def _asset(
    symbol: str,
    *,
    candles: list[dict[str, float | str]] | None = None,
    market_cap: float = 100_000_000_000,
    asset_type: str = "stock",
) -> dict[str, object]:
    item: dict[str, object] = {
        "symbol": symbol,
        "asset_type": asset_type,
        "theme": "software" if symbol != "DUP" else "semiconductors",
        "market_cap": market_cap,
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
        "data_source": "fixture",
    }
    if candles is not None:
        item["candles"] = candles
    return item


def _write_fixture(root: Path, assets: list[dict[str, object]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "account_capital": 50_000,
        "stock_regime": "neutral",
        "crypto_regime": "neutral",
        "sample_size": 120,
        "win_rate_2r": 0.58,
        "win_rate_3r": 0.34,
        "expected_value_r": 0.52,
        "portfolio_alerts": ["fixture_alert"],
        "assets": assets,
    }
    (root / "scan.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def _config() -> AdvisorConfig:
    return AdvisorConfig(
        stock_watchlist=["PRIMARY", "DUP"],
        crypto_watchlist=["HYPE"],
        discovery_stock_candidates=["DISC"],
        discovery_crypto_candidates=[],
        account_capital=50_000,
        risk_fraction=0.005,
    )


def _read_artifact(path: Path) -> dict[str, object]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact_path(runtime_dir: Path) -> Path:
    paths = sorted(runtime_dir.glob("scoring-runtime-trace*"))
    published = [path for path in paths if not path.name.endswith(".tmp")]
    if len(published) != 1:
        raise AssertionError(f"expected one published artifact, found {published}")
    return published[0]


def _artifact_assets(payload: dict[str, object]) -> list[dict[str, object]]:
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return []
    return [asset for asset in assets if isinstance(asset, dict)]


def _runtime_manifest(runtime_dir: Path) -> dict[str, dict[str, object]]:
    manifest: dict[str, dict[str, object]] = {}
    for path in sorted(runtime_dir.rglob("*")):
        if not path.is_file():
            continue
        content = path.read_bytes()
        relative_path = path.relative_to(runtime_dir).as_posix()
        manifest[relative_path] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "content": content,
        }
    return manifest


class RuntimeScoringIntegrationTests(unittest.TestCase):
    def _fixed_renderers(self, stack: ExitStack) -> None:
        real_markdown = cli_module.render_markdown_report
        real_analyst = cli_module.render_analyst_review_input

        def fixed_markdown(decisions, **kwargs):
            kwargs["generated_at"] = FIXED_REPORT_UTC
            return real_markdown(decisions, **kwargs)

        def fixed_analyst(decisions, **kwargs):
            kwargs["generated_at"] = FIXED_REPORT_BRT
            return real_analyst(decisions, **kwargs)

        stack.enter_context(patch.object(cli_module, "render_markdown_report", side_effect=fixed_markdown))
        stack.enter_context(patch.object(cli_module, "render_analyst_review_input", side_effect=fixed_analyst))

    def _run_scan(
        self,
        *,
        fixture_dir: Path,
        output_dir: Path,
        db_path: Path,
        artifact_enabled: bool,
        config: AdvisorConfig | None = None,
        observed_impl=None,
        writer=None,
        validator=None,
        capture_decisions: list | None = None,
        environment: dict[str, str] | None = None,
        clear_environment: bool = False,
    ) -> dict[str, object]:
        config = config or _config()
        legacy_results: list = []
        observed_results: list = []
        score_calls: list = []
        loader = Mock(side_effect=AssertionError("fixture scan must not construct a live loader"))

        def legacy_spy(scored, stats, **kwargs):
            decision = scoring_module.classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)
            legacy_results.append(decision)
            return decision

        def observed_spy(scored, stats, **kwargs):
            if observed_impl is None:
                decision, trace = scoring_module.classify_asset_with_trace(
                    scored,
                    stats,
                    effective_now_utc=FIXED_NOW_UTC,
                )
            else:
                decision, trace = observed_impl(scored, stats)
            observed_results.append((decision, trace))
            return decision, trace

        def score_spy(*args, **kwargs):
            score_calls.append((args, kwargs))
            return scoring_module.score_asset(*args, **kwargs)

        real_assign = cli_module._assign_universe_origins

        def assign_spy(decisions, assigned_config):
            assigned = real_assign(decisions, assigned_config)
            if capture_decisions is not None:
                capture_decisions.extend(assigned)
            return assigned

        if writer is None:
            writer_impl = getattr(cli_module, "write_runtime_scoring_artifact", None)
            writer = Mock(wraps=writer_impl) if artifact_enabled and writer_impl is not None else Mock()
        if validator is None:
            validator_impl = getattr(cli_module, "validate_runtime_scoring_artifact", None)
            validator = Mock(wraps=validator_impl) if artifact_enabled and validator_impl is not None else Mock()
        stdout = StringIO()
        argv = [
            "scan",
            "--fixture-dir",
            str(fixture_dir),
            "--db",
            str(db_path),
            "--output-dir",
            str(output_dir),
        ]
        if artifact_enabled:
            argv.append("--runtime-scoring-artifact")

        with ExitStack() as stack:
            stack.enter_context(patch("advisor.cli.AdvisorConfig.default", return_value=config))
            stack.enter_context(patch.object(cli_module, "LiveDataLoader", loader))
            stack.enter_context(patch.object(cli_module, "score_asset", side_effect=score_spy))
            stack.enter_context(patch.object(cli_module, "classify_asset", side_effect=legacy_spy))
            stack.enter_context(
                patch.object(cli_module, "classify_asset_with_trace", side_effect=observed_spy, create=True)
            )
            stack.enter_context(patch.object(cli_module, "write_runtime_scoring_artifact", writer, create=True))
            stack.enter_context(patch.object(cli_module, "validate_runtime_scoring_artifact", validator, create=True))
            stack.enter_context(patch.object(cli_module, "_assign_universe_origins", side_effect=assign_spy))
            if environment is not None:
                stack.enter_context(patch.dict(os.environ, environment, clear=clear_environment))
            self._fixed_renderers(stack)
            with redirect_stdout(stdout):
                code = advisor_main(argv)

        return {
            "code": code,
            "stdout": stdout.getvalue(),
            "legacy": legacy_results,
            "observed": observed_results,
            "score_calls": score_calls,
            "loader": loader,
            "writer": writer,
            "validator": validator,
            "decisions": capture_decisions or [],
        }

    def _prepare_previous_artifact(self, root: Path) -> tuple[Path, Path, dict[str, dict[str, object]]]:
        fixture = _write_fixture(root / "fixture", [_asset("PRIMARY")])
        output_dir = root / "reports"
        result = self._run_scan(
            fixture_dir=fixture,
            output_dir=output_dir,
            db_path=root / "journal.db",
            artifact_enabled=True,
            environment={"GITHUB_SHA": BASE_SHA},
            clear_environment=True,
        )
        self.assertEqual(result["code"], 0)
        runtime_dir = output_dir / "runtime"
        manifest = _runtime_manifest(runtime_dir)
        self.assertTrue(manifest)
        return fixture, output_dir, manifest

    def _assert_no_runtime_transaction_residue(self, output_dir: Path) -> None:
        self.assertEqual(list(output_dir.glob(".runtime-staging-*")), [])
        self.assertEqual(list(output_dir.glob(".runtime-backup-*")), [])

    def test_default_off_uses_legacy_classifier_and_creates_no_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(root / "fixture", [_asset("PRIMARY")])
            output = root / "reports"
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=output,
                db_path=root / "legacy.db",
                artifact_enabled=False,
            )

            self.assertEqual(result["code"], 0)
            self.assertEqual(len(result["legacy"]), 1)
            self.assertEqual(len(result["observed"]), 0)
            self.assertEqual(result["writer"].call_count, 0)
            self.assertEqual(result["validator"].call_count, 0)
            self.assertFalse((output / "runtime").exists())

    def test_opt_in_uses_one_observed_call_per_scored_asset_and_validates_one_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(
                root / "fixture",
                [_asset("PRIMARY"), _asset("DISC"), _asset("SHORT", candles=[_candles(1)[0]])],
            )
            output = root / "reports"
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=output,
                db_path=root / "observed.db",
                artifact_enabled=True,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )

            self.assertEqual(result["code"], 0)
            self.assertEqual(len(result["observed"]), 2)
            self.assertEqual(len(result["legacy"]), 0)
            self.assertEqual(len(result["score_calls"]), 2)
            self.assertEqual(result["writer"].call_count, 1)
            self.assertEqual(result["validator"].call_count, 1)
            self.assertEqual(result["writer"].call_args.kwargs["assets"].__len__(), 2)
            self.assertIn("runtime_scoring_artifact_status=", result["stdout"])
            self.assertIn("runtime_scoring_artifact_assets=2", result["stdout"])
            self.assertIn("runtime_scoring_artifact_untraced_decisions=1", result["stdout"])

            artifact = _artifact_path(output / "runtime")
            payload = _read_artifact(artifact)
            self.assertEqual(payload["run_metadata"]["asset_count"], 2)
            self.assertEqual(payload["run_metadata"]["schedule"], "main")
            self.assertEqual(payload["run_metadata"]["timezone"], "America/Sao_Paulo")
            self.assertEqual(payload["run_metadata"]["source_sha"], BASE_SHA)
            self.assertIsNone(payload["run_metadata"]["runtime_sha"])
            self.assertEqual(payload["run_metadata"]["runtime_sha_status"], "unavailable")
            report_date = datetime.now(BRT).date().isoformat()
            self.assertEqual(payload["run_metadata"]["report_date"], report_date)
            self.assertRegex(
                payload["run_metadata"]["run_id"],
                rf"^{re.escape(report_date)}-main-{BASE_SHA[:12]}$",
            )

    def test_flag_preserves_reports_decisions_hashes_journal_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(
                root / "fixture",
                [_asset("PRIMARY"), _asset("DISC"), _asset("SHORT", candles=[_candles(1)[0]])],
            )
            off_decisions: list = []
            on_decisions: list = []
            off = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "reports-off",
                db_path=root / "off.db",
                artifact_enabled=False,
                capture_decisions=off_decisions,
            )
            on = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "reports-on",
                db_path=root / "on.db",
                artifact_enabled=True,
                capture_decisions=on_decisions,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )

            self.assertEqual(off["code"], 0)
            self.assertEqual(on["code"], 0)
            self.assertEqual(
                (root / "reports-off" / "advisor-report.md").read_text(encoding="utf-8"),
                (root / "reports-on" / "advisor-report.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (root / "reports-off" / "advisor-report.html").read_text(encoding="utf-8"),
                (root / "reports-on" / "advisor-report.html").read_text(encoding="utf-8"),
            )
            self.assertEqual([decision.symbol for decision in off_decisions], [decision.symbol for decision in on_decisions])
            self.assertEqual(
                [asset_decision_sha256(decision) for decision in off_decisions],
                [asset_decision_sha256(decision) for decision in on_decisions],
            )

            off_rows = SQLiteCache(root / "off.db").load_signal_journal()
            on_rows = SQLiteCache(root / "on.db").load_signal_journal()
            ignored = {"id", "created_at", "report_file"}
            normalize = lambda rows: [
                {key: value for key, value in row.items() if key not in ignored} for row in rows
            ]
            self.assertEqual(normalize(off_rows), normalize(on_rows))

            payload = _read_artifact(_artifact_path(root / "reports-on" / "runtime"))
            hashes_by_symbol = {
                asset["symbol"]: asset["serialized_asset_decision_hash"]
                for asset in _artifact_assets(payload)
            }
            for decision in on_decisions:
                if decision.symbol in hashes_by_symbol:
                    self.assertEqual(hashes_by_symbol[decision.symbol], asset_decision_sha256(decision))

    def test_final_universe_origins_are_attached_by_decision_position_and_duplicate_symbols_keep_traces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(
                root / "fixture",
                [
                    _asset("PRIMARY"),
                    _asset("DISC"),
                    _asset("UNKNOWN"),
                    _asset("DUP", market_cap=100_000_000_000),
                    _asset("DUP", market_cap=10_000_000_000),
                ],
            )
            markers: list[str] = []
            final_decisions: list = []

            def observed_with_marker(scored, stats):
                decision, trace = scoring_module.classify_asset_with_trace(
                    scored,
                    stats,
                    effective_now_utc=FIXED_NOW_UTC,
                )
                marker = f"call-{len(markers)}"
                markers.append(marker)
                trace.classification_inputs["integration_marker"] = marker
                return decision, trace

            writer = Mock(
                return_value=SimpleNamespace(output_paths=(root / "reports" / "runtime" / "spy",))
            )
            validator = Mock(return_value=SimpleNamespace(artifact_status="complete", mode="single"))
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "reports",
                db_path=root / "journal.db",
                artifact_enabled=True,
                observed_impl=observed_with_marker,
                capture_decisions=final_decisions,
                writer=writer,
                validator=validator,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )
            assets = writer.call_args.kwargs["assets"]
            expected = {
                "call-0": ("PRIMARY", "primary_watchlist"),
                "call-1": ("DISC", "discovery"),
                "call-2": ("UNKNOWN", "unknown"),
                "call-3": ("DUP", "primary_watchlist"),
                "call-4": ("DUP", "primary_watchlist"),
            }
            self.assertEqual(len(result["observed"]), 5)
            self.assertEqual(writer.call_count, 1)
            self.assertEqual(validator.call_count, 1)
            self.assertEqual(len(assets), 5)
            self.assertTrue(all(isinstance(asset, ArtifactAssetInput) for asset in assets))
            for position, (marker, (symbol, origin)) in enumerate(expected.items()):
                with self.subTest(marker=marker):
                    asset = assets[position]
                    self.assertIs(asset.decision, final_decisions[position])
                    self.assertEqual(asset.decision.symbol, symbol)
                    self.assertEqual(asset.decision.universe_origin, origin)
                    self.assertEqual(
                        asset.trace.classification_inputs["integration_marker"], marker
                    )
            self.assertEqual([decision.symbol for decision in final_decisions], [
                "PRIMARY", "DISC", "UNKNOWN", "DUP", "DUP"
            ])
            self.assertEqual([decision.universe_origin for decision in final_decisions], [
                "primary_watchlist", "discovery", "unknown", "primary_watchlist", "primary_watchlist"
            ])

    def test_real_writer_rejects_duplicate_identity_fail_open_and_preserves_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(
                root / "fixture",
                [
                    _asset("PRIMARY"),
                    _asset("DUP", market_cap=100_000_000_000),
                    _asset("DUP", market_cap=10_000_000_000),
                ],
            )
            final_decisions: list = []
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "reports",
                db_path=root / "journal.db",
                artifact_enabled=True,
                capture_decisions=final_decisions,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )

            self.assertEqual(result["code"], 0)
            self.assertEqual(result["writer"].call_count, 1)
            self.assertEqual(result["validator"].call_count, 0)
            self.assertTrue((root / "reports" / "advisor-report.md").exists())
            self.assertTrue((root / "reports" / "advisor-report.html").exists())
            warning = next(
                line for line in result["stdout"].splitlines()
                if line.startswith("runtime_scoring_artifact_status=")
            )
            self.assertEqual(warning, "runtime_scoring_artifact_status=failed error_code=serialization_error")
            self.assertNotIn("duplicate asset identity", warning)
            self.assertNotIn(str(root), warning)
            runtime = root / "reports" / "runtime"
            published = [
                path for path in runtime.glob("scoring-runtime-trace*")
                if not path.name.endswith(".tmp")
            ]
            self.assertEqual(published, [])

            rows = SQLiteCache(root / "journal.db").load_signal_journal()
            self.assertEqual(
                [(row["asset"], row["decision_label"]) for row in rows],
                [(decision.symbol, decision.decision) for decision in final_decisions],
            )

    def test_real_writer_and_validator_succeed_for_unique_identities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(
                root / "fixture",
                [_asset("PRIMARY"), _asset("DISC"), _asset("UNKNOWN")],
            )
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "reports",
                db_path=root / "journal.db",
                artifact_enabled=True,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )

            self.assertEqual(result["code"], 0)
            self.assertEqual(result["writer"].call_count, 1)
            self.assertEqual(result["validator"].call_count, 1)
            artifact = _artifact_path(root / "reports" / "runtime")
            payload = _read_artifact(artifact)
            self.assertEqual(
                [asset["symbol"] for asset in _artifact_assets(payload)],
                ["DISC", "PRIMARY", "UNKNOWN"],
            )

    def test_validator_failure_preserves_previous_runtime_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture, output_dir, before = self._prepare_previous_artifact(root)
            validator = Mock(side_effect=RuntimeError("secret validator detail"))
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=output_dir,
                db_path=root / "journal.db",
                artifact_enabled=True,
                validator=validator,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )

            self.assertEqual(result["code"], 0)
            self.assertEqual(result["writer"].call_count, 1)
            self.assertEqual(validator.call_count, 1)
            self.assertEqual(_runtime_manifest(output_dir / "runtime"), before)
            self.assertTrue((output_dir / "advisor-report.md").exists())
            self.assertTrue((output_dir / "advisor-report.html").exists())
            self.assertEqual(len(SQLiteCache(root / "journal.db").load_signal_journal()), 2)
            warning = next(
                line for line in result["stdout"].splitlines()
                if line.startswith("runtime_scoring_artifact_status=")
            )
            self.assertEqual(warning, "runtime_scoring_artifact_status=failed error_code=validator_error")
            self.assertNotIn("secret validator detail", warning)
            self._assert_no_runtime_transaction_residue(output_dir)

    def test_writer_failure_preserves_previous_runtime_and_skips_validator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture, output_dir, before = self._prepare_previous_artifact(root)
            writer = Mock(side_effect=RuntimeError("secret writer detail"))
            validator = Mock()
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=output_dir,
                db_path=root / "journal.db",
                artifact_enabled=True,
                writer=writer,
                validator=validator,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )

            self.assertEqual(result["code"], 0)
            self.assertEqual(writer.call_count, 1)
            self.assertEqual(validator.call_count, 0)
            self.assertEqual(_runtime_manifest(output_dir / "runtime"), before)
            self.assertTrue((output_dir / "advisor-report.md").exists())
            self.assertTrue((output_dir / "advisor-report.html").exists())
            self.assertEqual(len(SQLiteCache(root / "journal.db").load_signal_journal()), 2)
            warning = next(
                line for line in result["stdout"].splitlines()
                if line.startswith("runtime_scoring_artifact_status=")
            )
            self.assertEqual(warning, "runtime_scoring_artifact_status=failed error_code=writer_error")
            self.assertNotIn("secret writer detail", warning)
            self._assert_no_runtime_transaction_residue(output_dir)

    def test_publication_failure_rolls_back_previous_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture, output_dir, before = self._prepare_previous_artifact(root)
            real_rename = getattr(cli_module, "_rename_runtime_directory", None)
            rename_calls: list[tuple[Path, Path]] = []

            def fail_second_rename(source: Path, destination: Path) -> None:
                rename_calls.append((source, destination))
                if len(rename_calls) == 2:
                    raise OSError("secret publish failure")
                if real_rename is None:
                    raise AssertionError("publication rename boundary was not called")
                real_rename(source, destination)

            with patch.object(
                cli_module,
                "_rename_runtime_directory",
                side_effect=fail_second_rename,
                create=True,
            ):
                result = self._run_scan(
                    fixture_dir=fixture,
                    output_dir=output_dir,
                    db_path=root / "journal.db",
                    artifact_enabled=True,
                    environment={"GITHUB_SHA": BASE_SHA},
                    clear_environment=True,
                )

            self.assertEqual(result["code"], 0)
            self.assertEqual(result["writer"].call_count, 1)
            self.assertEqual(result["validator"].call_count, 1)
            self.assertEqual(len(rename_calls), 3)
            self.assertEqual(_runtime_manifest(output_dir / "runtime"), before)
            self.assertTrue((output_dir / "advisor-report.md").exists())
            self.assertTrue((output_dir / "advisor-report.html").exists())
            self.assertEqual(len(SQLiteCache(root / "journal.db").load_signal_journal()), 2)
            warning = next(
                line for line in result["stdout"].splitlines()
                if line.startswith("runtime_scoring_artifact_status=")
            )
            self.assertEqual(warning, "runtime_scoring_artifact_status=failed error_code=publish_error")
            self.assertNotIn("secret publish failure", warning)
            self._assert_no_runtime_transaction_residue(output_dir)

    def test_first_run_validator_failure_does_not_create_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(root / "fixture", [_asset("PRIMARY")])
            validator = Mock(side_effect=RuntimeError("secret validator detail"))
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "reports",
                db_path=root / "journal.db",
                artifact_enabled=True,
                validator=validator,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )

            self.assertEqual(result["code"], 0)
            self.assertEqual(result["writer"].call_count, 1)
            self.assertEqual(validator.call_count, 1)
            self.assertFalse((root / "reports" / "runtime").exists())
            self.assertTrue((root / "reports" / "advisor-report.md").exists())
            self.assertTrue((root / "reports" / "advisor-report.html").exists())
            self.assertEqual(len(SQLiteCache(root / "journal.db").load_signal_journal()), 1)
            self._assert_no_runtime_transaction_residue(root / "reports")

    def test_valid_run_replaces_previous_runtime_as_a_complete_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture, output_dir, _before = self._prepare_previous_artifact(root)
            stale_path = output_dir / "runtime" / "obsolete-previous-part.json.gz"
            stale_path.write_bytes(b"stale artifact bytes")

            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=output_dir,
                db_path=root / "journal.db",
                artifact_enabled=True,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )

            self.assertEqual(result["code"], 0)
            self.assertEqual(result["writer"].call_count, 1)
            self.assertEqual(result["validator"].call_count, 1)
            self.assertFalse(stale_path.exists())
            self.assertTrue(_artifact_path(output_dir / "runtime").exists())
            self.assertTrue((output_dir / "advisor-report.md").exists())
            self._assert_no_runtime_transaction_residue(output_dir)

    def test_unscorable_decision_is_reported_without_classifier_or_trace_and_zero_assets_is_failed_no_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(
                root / "fixture",
                [_asset("SHORT", candles=[_candles(1)[0]])],
            )
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "reports",
                db_path=root / "journal.db",
                artifact_enabled=True,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )
            self.assertEqual(result["code"], 0)
            self.assertEqual(len(result["observed"]), 0)
            self.assertEqual(len(result["legacy"]), 0)
            self.assertEqual(result["writer"].call_count, 1)
            self.assertEqual(result["validator"].call_count, 1)
            self.assertEqual(result["writer"].call_args.kwargs["assets"], [])
            self.assertIn("SHORT", (root / "reports" / "advisor-report.md").read_text(encoding="utf-8"))
            payload = _read_artifact(_artifact_path(root / "reports" / "runtime"))
            self.assertEqual(payload["artifact_status"], "failed")
            self.assertEqual(payload["integrity"]["asset_count"], 0)
            self.assertTrue(any(error["error_code"] == "no_assets" for error in payload["errors"]))
            self.assertIn("runtime_scoring_artifact_status=failed", result["stdout"])

    def test_partial_collector_preserves_decision_and_produces_partial_valid_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(root / "fixture", [_asset("PRIMARY")])

            class FailingCollector:
                def record_event(self, *_args: object, **_kwargs: object) -> None:
                    raise RuntimeError("collector secret must be sanitized")

            def partial_observed(scored, stats):
                context = ObservationContext(
                    enabled=True,
                    effective_now_utc=FIXED_NOW_UTC,
                    collector=FailingCollector(),
                )
                return scoring_module.classify_asset_with_trace(
                    scored,
                    stats,
                    effective_now_utc=FIXED_NOW_UTC,
                    observation_context=context,
                )

            legacy_decisions: list = []
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "reports",
                db_path=root / "journal.db",
                artifact_enabled=True,
                observed_impl=partial_observed,
                capture_decisions=legacy_decisions,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )
            self.assertEqual(result["code"], 0)
            self.assertEqual(len(result["observed"]), 1)
            self.assertEqual(result["observed"][0][1].trace_status, "partial")
            self.assertEqual(
                replace(result["observed"][0][0], universe_origin="primary_watchlist"),
                legacy_decisions[0],
            )
            self.assertIn("runtime_scoring_artifact_status=partial", result["stdout"])
            self.assertNotIn("collector secret", result["stdout"])
            payload = _read_artifact(_artifact_path(root / "reports" / "runtime"))
            self.assertEqual(payload["artifact_status"], "partial")

    def test_writer_failure_is_fail_open_and_does_not_change_successful_scan_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(root / "fixture", [_asset("PRIMARY")])
            writer = Mock(side_effect=RuntimeError("secret writer path"))
            validator = Mock()
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "reports",
                db_path=root / "journal.db",
                artifact_enabled=True,
                writer=writer,
                validator=validator,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )
            self.assertEqual(result["code"], 0)
            self.assertTrue((root / "reports" / "advisor-report.md").exists())
            self.assertEqual(writer.call_count, 1)
            self.assertEqual(validator.call_count, 0)
            warning = next(line for line in result["stdout"].splitlines() if line.startswith("runtime_scoring_artifact_status="))
            self.assertRegex(warning, r"^runtime_scoring_artifact_status=failed error_code=[a-z_]+$")
            self.assertNotIn("secret writer path", warning)
            self.assertNotIn(str(root), warning)

    def test_serialization_failure_is_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(root / "fixture", [_asset("PRIMARY")])
            writer = Mock(side_effect=TypeError("secret serialization detail"))
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "reports",
                db_path=root / "journal.db",
                artifact_enabled=True,
                writer=writer,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )
            self.assertEqual(result["code"], 0)
            warning = next(line for line in result["stdout"].splitlines() if line.startswith("runtime_scoring_artifact_status="))
            self.assertRegex(warning, r"error_code=(serialization_error|writer_error|artifact_error)$")
            self.assertNotIn("secret serialization detail", warning)

    def test_validator_failure_is_fail_open_and_removes_unvalidated_artifact_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(root / "fixture", [_asset("PRIMARY")])
            validator = Mock(side_effect=RuntimeError("secret validator detail"))
            result = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "reports",
                db_path=root / "journal.db",
                artifact_enabled=True,
                validator=validator,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )
            self.assertEqual(result["code"], 0)
            self.assertEqual(result["writer"].call_count, 1)
            self.assertEqual(validator.call_count, 1)
            warning = next(line for line in result["stdout"].splitlines() if line.startswith("runtime_scoring_artifact_status="))
            self.assertRegex(warning, r"^runtime_scoring_artifact_status=failed error_code=validator_error$")
            self.assertNotIn("secret validator detail", warning)
            runtime = root / "reports" / "runtime"
            self.assertFalse(any(path.name.startswith("scoring-runtime-trace") for path in runtime.glob("*")))
            self.assertTrue((root / "reports" / "advisor-report.md").exists())

    def test_invalid_github_sha_falls_back_to_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(root / "fixture", [_asset("PRIMARY")])
            with (
                patch.object(
                    cli_module.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        ["git", "rev-parse", "HEAD"], 0, stdout=FALLBACK_SHA + "\n", stderr=""
                    ),
                ) as git_run,
            ):
                result = self._run_scan(
                    fixture_dir=fixture,
                    output_dir=root / "reports",
                    db_path=root / "journal.db",
                    artifact_enabled=True,
                    environment={"GITHUB_SHA": "A" * 40},
                    clear_environment=True,
                )
            git_run.assert_called_once_with(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            payload = _read_artifact(_artifact_path(root / "reports" / "runtime"))
            self.assertEqual(result["code"], 0)
            self.assertEqual(payload["run_metadata"]["source_sha"], FALLBACK_SHA)
            self.assertEqual(result["writer"].call_count, 1)
            self.assertEqual(result["validator"].call_count, 1)

    def test_missing_github_sha_falls_back_to_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(root / "fixture", [_asset("PRIMARY")])
            with patch.object(
                cli_module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["git", "rev-parse", "HEAD"], 0, stdout=FALLBACK_SHA + "\n", stderr=""
                ),
            ) as git_run:
                result = self._run_scan(
                    fixture_dir=fixture,
                    output_dir=root / "reports",
                    db_path=root / "journal.db",
                    artifact_enabled=True,
                    environment={},
                    clear_environment=True,
                )
            git_run.assert_called_once_with(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            payload = _read_artifact(_artifact_path(root / "reports" / "runtime"))
            self.assertEqual(result["code"], 0)
            self.assertEqual(payload["run_metadata"]["source_sha"], FALLBACK_SHA)

    def test_invalid_github_sha_and_git_failure_are_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(root / "fixture", [_asset("PRIMARY")])
            writer = Mock()
            with patch.object(
                cli_module.subprocess, "run", side_effect=OSError("C:\\private\\git secret")
            ) as git_run:
                result = self._run_scan(
                    fixture_dir=fixture,
                    output_dir=root / "reports",
                    db_path=root / "journal.db",
                    artifact_enabled=True,
                    writer=writer,
                    environment={"GITHUB_SHA": "not-a-sha"},
                    clear_environment=True,
                )
            git_run.assert_called_once()
            self.assertEqual(result["code"], 0)
            self.assertEqual(writer.call_count, 0)
            self.assertEqual(result["validator"].call_count, 0)
            warning = next(
                line for line in result["stdout"].splitlines()
                if line.startswith("runtime_scoring_artifact_status=")
            )
            self.assertEqual(warning, "runtime_scoring_artifact_status=failed error_code=source_sha_unavailable")
            self.assertNotIn("private", warning)
            self.assertFalse((root / "reports" / "runtime").exists())
            self.assertTrue((root / "reports" / "advisor-report.md").exists())
            self.assertTrue((root / "reports" / "advisor-report.html").exists())

    def test_missing_github_sha_and_git_timeout_are_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(root / "fixture", [_asset("PRIMARY")])
            writer = Mock()
            timeout = subprocess.TimeoutExpired(["git", "rev-parse", "HEAD"], timeout=2)
            with patch.object(cli_module.subprocess, "run", side_effect=timeout) as git_run:
                result = self._run_scan(
                    fixture_dir=fixture,
                    output_dir=root / "reports",
                    db_path=root / "journal.db",
                    artifact_enabled=True,
                    writer=writer,
                    environment={},
                    clear_environment=True,
                )
            git_run.assert_called_once()
            self.assertEqual(result["code"], 0)
            self.assertEqual(writer.call_count, 0)
            self.assertEqual(result["validator"].call_count, 0)
            warning = next(
                line for line in result["stdout"].splitlines()
                if line.startswith("runtime_scoring_artifact_status=")
            )
            self.assertEqual(warning, "runtime_scoring_artifact_status=failed error_code=source_sha_unavailable")
            self.assertTrue((root / "reports" / "advisor-report.md").exists())
            self.assertTrue((root / "reports" / "advisor-report.html").exists())

    def test_report_main_and_close_propagate_opt_in_and_use_their_schedule(self) -> None:
        for report_type in ("main", "close"):
            with self.subTest(report_type=report_type), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = _config()
                writer = Mock(wraps=getattr(cli_module, "write_runtime_scoring_artifact", None))
                validator = Mock(wraps=getattr(cli_module, "validate_runtime_scoring_artifact", None))
                with ExitStack() as stack:
                    stack.enter_context(patch("advisor.cli.AdvisorConfig.default", return_value=config))
                    stack.enter_context(patch.object(cli_module, "classify_asset", side_effect=AssertionError("legacy classifier used")))
                    stack.enter_context(
                        patch.object(
                            cli_module,
                            "classify_asset_with_trace",
                            wraps=scoring_module.classify_asset_with_trace,
                            create=True,
                        )
                    )
                    stack.enter_context(patch.object(cli_module, "write_runtime_scoring_artifact", writer, create=True))
                    stack.enter_context(patch.object(cli_module, "validate_runtime_scoring_artifact", validator, create=True))
                    stack.enter_context(patch.dict(os.environ, {"GITHUB_SHA": BASE_SHA}, clear=True))
                    self._fixed_renderers(stack)
                    code = advisor_main(
                        [
                            "report",
                            report_type,
                            "--runtime-scoring-artifact",
                            "--db",
                            str(root / "journal.db"),
                            "--output-dir",
                            str(root / "reports"),
                        ]
                    )
                self.assertEqual(code, 0)
                self.assertEqual(writer.call_count, 1)
                self.assertEqual(validator.call_count, 1)
                payload = _read_artifact(_artifact_path(root / "reports" / "runtime"))
                self.assertEqual(payload["run_metadata"]["schedule"], report_type)

    def test_report_without_main_or_close_rejects_opt_in_without_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer = Mock()
            stdout = StringIO()
            with (
                patch.object(cli_module, "write_runtime_scoring_artifact", writer, create=True),
                redirect_stdout(stdout),
            ):
                code = advisor_main(
                    [
                        "report",
                        "--runtime-scoring-artifact",
                        "--db",
                        str(root / "journal.db"),
                        "--output-dir",
                        str(root / "reports"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue().strip(), "runtime_scoring_artifact_requires_main_or_close")
            self.assertEqual(writer.call_count, 0)
            self.assertFalse((root / "reports" / "runtime").exists())

    def test_opt_in_does_not_add_loader_calls_or_second_score_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(root / "fixture", [_asset("PRIMARY"), _asset("DISC")])
            off = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "off",
                db_path=root / "off.db",
                artifact_enabled=False,
            )
            on = self._run_scan(
                fixture_dir=fixture,
                output_dir=root / "on",
                db_path=root / "on.db",
                artifact_enabled=True,
                environment={"GITHUB_SHA": BASE_SHA},
                clear_environment=True,
            )
            self.assertEqual(len(off["score_calls"]), len(on["score_calls"]))
            self.assertEqual(len(on["score_calls"]), len(on["observed"]))
            off["loader"].assert_not_called()
            on["loader"].assert_not_called()

    def test_valid_github_sha_is_used_without_git_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _write_fixture(root / "fixture", [_asset("PRIMARY")])
            with patch.object(
                cli_module.subprocess, "run", side_effect=AssertionError("valid GITHUB_SHA must win")
            ):
                result = self._run_scan(
                    fixture_dir=fixture,
                    output_dir=root / "reports",
                    db_path=root / "journal.db",
                    artifact_enabled=True,
                    environment={"GITHUB_SHA": BASE_SHA},
                    clear_environment=True,
                )
            payload = _read_artifact(_artifact_path(root / "reports" / "runtime"))
            self.assertEqual(result["code"], 0)
            self.assertEqual(payload["run_metadata"]["source_sha"], BASE_SHA)

    def test_source_sha_environment_is_validated_without_normalization(self) -> None:
        sha40 = "a" * 40
        sha64 = "b" * 64
        for value in (sha40, sha64):
            with self.subTest(source=value), patch.dict(
                os.environ, {"GITHUB_SHA": value}, clear=True
            ), patch.object(
                cli_module.subprocess, "run", side_effect=AssertionError("environment SHA must win")
            ):
                self.assertEqual(cli_module._resolve_source_sha(), value)

        invalid_values = (
            f" {sha40}",
            f"{sha40} ",
            f"\t{sha40}",
            f"{sha40}\n",
            sha40.upper(),
            sha40[:-1],
            f"{sha40}0",
            sha64[:-1],
            f"{sha64}0",
            f"sha:{sha40}",
            f"{sha40} extra",
        )
        for value in invalid_values:
            with self.subTest(invalid_source=repr(value)), patch.dict(
                os.environ, {"GITHUB_SHA": value}, clear=True
            ), patch.object(
                cli_module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["git", "rev-parse", "HEAD"], 0, stdout=FALLBACK_SHA + "\n", stderr=""
                ),
            ) as git_run:
                self.assertEqual(cli_module._resolve_source_sha(), FALLBACK_SHA)
                git_run.assert_called_once()

    def test_git_stdout_removes_only_one_terminal_newline(self) -> None:
        sha40 = "a" * 40
        sha64 = "b" * 64
        valid_outputs = (
            (f"{sha40}\n", sha40),
            (f"{sha40}\r\n", sha40),
            (f"{sha64}\n", sha64),
            (sha40, sha40),
        )
        for stdout, expected in valid_outputs:
            with self.subTest(stdout=repr(stdout)), patch.dict(
                os.environ, {}, clear=True
            ), patch.object(
                cli_module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["git", "rev-parse", "HEAD"], 0, stdout=stdout, stderr=""
                ),
            ):
                self.assertEqual(cli_module._resolve_source_sha(), expected)

        invalid_outputs = (
            f" {sha40}\n",
            f"{sha40} \n",
            f"{sha40}\n\n",
            f"{sha40}\ntexto",
            f"{sha40.upper()}\n",
            "invalid\n",
        )
        for stdout in invalid_outputs:
            with self.subTest(invalid_stdout=repr(stdout)), patch.dict(
                os.environ, {}, clear=True
            ), patch.object(
                cli_module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["git", "rev-parse", "HEAD"], 0, stdout=stdout, stderr=""
                ),
            ):
                self.assertIsNone(cli_module._resolve_source_sha())


if __name__ == "__main__":
    unittest.main()
