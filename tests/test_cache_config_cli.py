import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from advisor.cache import ApiLimiter, SQLiteCache
from advisor.cli import main as advisor_main
from advisor.config import AdvisorConfig
from advisor.fixtures import load_scan_fixture


def _without_live_config_env(tmp: str | Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("FMP_API_KEY", None)
    env.pop("COINGECKO_API_KEY", None)
    env["ADVISOR_ENV_FILE"] = str(Path(tmp) / "missing-test.env")
    return env


class CacheConfigCliTests(unittest.TestCase):
    def test_sqlite_cache_respects_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SQLiteCache(Path(tmp) / "advisor.db")
            cache.set_json("prices", "MSFT", {"close": 100}, fetched_at="2026-01-01T00:00:00")

            fresh = cache.get_json("prices", "MSFT", max_age_seconds=3600, now="2026-01-01T00:30:00")
            stale = cache.get_json("prices", "MSFT", max_age_seconds=60, now="2026-01-01T00:30:00")

            self.assertEqual(fresh["close"], 100)
            self.assertIsNone(stale)

    def test_scan_fixture_loader_accepts_utf8_bom_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp)
            (fixture_dir / "scan.json").write_text(
                '\ufeff{"account_capital": 50000, "assets": []}',
                encoding="utf-8",
            )

            payload = load_scan_fixture(fixture_dir)

            self.assertEqual(payload["account_capital"], 50000)

    def test_api_limiter_blocks_after_daily_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            limiter = ApiLimiter(Path(tmp) / "advisor.db")
            self.assertTrue(limiter.allow("fmp", limit=2, day="2026-01-01"))
            self.assertTrue(limiter.allow("fmp", limit=2, day="2026-01-01"))
            self.assertFalse(limiter.allow("fmp", limit=2, day="2026-01-01"))

    def test_config_validate_rejects_missing_required_free_keys(self):
        config = AdvisorConfig.default()
        config.fmp_api_key = ""
        errors = config.validate()

        self.assertIn("missing_fmp_api_key", errors)
        self.assertNotIn("PETR4", config.stock_watchlist)

    def test_config_validate_rejects_example_placeholder_keys(self):
        config = AdvisorConfig.default()
        config.fmp_api_key = "your_fmp_free_key"
        config.coingecko_api_key = "your_coingecko_demo_key"

        errors = config.validate()

        self.assertIn("placeholder_fmp_api_key", errors)
        self.assertIn("placeholder_coingecko_api_key", errors)

    def test_default_watchlists_match_coverage_universe(self):
        config = AdvisorConfig.default()

        self.assertEqual(
            config.stock_watchlist,
            ["INTC", "AMD", "NVDA", "HIMS", "MU", "MSFT", "USAR", "CRDO", "DELL", "MRVL", "HOOD"],
        )
        self.assertEqual(config.crypto_watchlist, ["SOL", "HYPE", "BTC", "ETH"])

    def test_config_validate_checks_watchlists_and_freshness(self):
        config = AdvisorConfig.default()
        config.stock_watchlist = []
        config.crypto_watchlist = ["BTC"]
        config.freshness_seconds["prices"] = 0
        config.minimum_crypto_market_cap = 0

        errors = config.validate(allow_missing_keys=True)

        self.assertIn("empty_stock_watchlist", errors)
        self.assertIn("missing_hype_hyperliquid_watchlist_entry", errors)
        self.assertIn("invalid_freshness_prices", errors)
        self.assertIn("invalid_minimum_crypto_market_cap", errors)

    def test_config_has_curated_discovery_universe_without_unknown_microcaps(self):
        config = AdvisorConfig.default()

        self.assertIn("AVGO", config.discovery_stock_candidates)
        self.assertIn("AAPL", config.discovery_stock_candidates)
        self.assertIn("LINK", config.discovery_crypto_candidates)
        self.assertNotIn("PETR4", config.discovery_stock_candidates)
        self.assertGreater(config.minimum_stock_market_cap, 1_000_000_000)

    def test_estimated_live_call_budget_includes_discovery(self):
        config = AdvisorConfig.default()
        base_calls = config.estimated_live_calls(include_discovery=False)
        discovery_calls = config.estimated_live_calls(include_discovery=True)

        self.assertEqual(base_calls["fmp"], (len(config.stock_watchlist) * 7) + 2)
        self.assertEqual(base_calls["hyperliquid"], 2)
        self.assertEqual(base_calls["binance"], 16)
        self.assertEqual(discovery_calls["binance"], 36)
        self.assertGreater(discovery_calls["fmp"], base_calls["fmp"])
        self.assertLessEqual(discovery_calls["fmp"], config.api_limits["fmp"])

    def test_config_loads_dotenv_without_overriding_real_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "FMP_API_KEY=from_file",
                        "COINGECKO_API_KEY=coingecko_file",
                        "ADVISOR_ACCOUNT_CAPITAL=75000",
                        "ADVISOR_RISK_FRACTION=0.0075",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"FMP_API_KEY": "from_env"}, clear=False):
                config = AdvisorConfig.default(env_file=env_path)

        self.assertEqual(config.fmp_api_key, "from_env")
        self.assertEqual(config.coingecko_api_key, "coingecko_file")
        self.assertEqual(config.account_capital, 75_000)
        self.assertEqual(config.risk_fraction, 0.0075)

    def test_config_loads_daily_and_weekly_loss_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "ADVISOR_MAX_DAILY_LOSS_FRACTION=0.015",
                        "ADVISOR_MAX_WEEKLY_LOSS_FRACTION=0.04",
                    ]
                ),
                encoding="utf-8",
            )

            config = AdvisorConfig.default(env_file=env_path)

        self.assertEqual(config.max_daily_loss_fraction, 0.015)
        self.assertEqual(config.max_weekly_loss_fraction, 0.04)

    def test_config_limits_symbols_and_exposes_per_run_fmp_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "ADVISOR_STOCK_WATCHLIST=MSFT,NVDA,AMD",
                        "ADVISOR_CRYPTO_WATCHLIST=HYPE",
                        "ADVISOR_MAX_STOCKS_PER_RUN=2",
                        "ADVISOR_FMP_CALL_BUDGET_PER_RUN=20",
                    ]
                ),
                encoding="utf-8",
            )

            config = AdvisorConfig.default(env_file=env_path)

        stocks, cryptos = config.symbols_for_scan(include_discovery=False)
        self.assertEqual(stocks, ["MSFT", "NVDA"])
        self.assertEqual(cryptos, ["HYPE"])
        self.assertEqual(config.api_run_limits["fmp"], 20)
        self.assertLessEqual(config.estimated_live_calls(include_discovery=False)["fmp"], 20)

    def test_config_default_can_use_env_file_override_for_hermetic_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_env_path = Path(tmp) / ".env"
            real_env_path.write_text(
                "\n".join(["FMP_API_KEY=from_file", "COINGECKO_API_KEY=coingecko_file"]),
                encoding="utf-8",
            )
            missing_env_path = Path(tmp) / "missing.env"

            with patch.dict(
                "os.environ",
                {
                    "ADVISOR_ENV_FILE": str(missing_env_path),
                    "FMP_API_KEY": "",
                    "COINGECKO_API_KEY": "",
                },
                clear=False,
            ):
                config = AdvisorConfig.default()

        self.assertEqual(config.fmp_api_key, "")
        self.assertEqual(config.coingecko_api_key, "")

    def test_cli_commands_work_with_fixture_data_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp) / "fixtures"
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"
            fixture_dir.mkdir()
            (fixture_dir / "scan.json").write_text(
                json.dumps(
                    {
                        "account_capital": 50000,
                        "stock_regime": "risk_on",
                        "crypto_regime": "neutral",
                        "assets": [
                            {
                                "symbol": "NVDA",
                                "asset_type": "stock",
                                "theme": "semiconductors",
                                "market_cap": 3000000000000,
                                "average_volume": 40000000,
                                "revenue_growth": 0.20,
                                "eps_growth": 0.18,
                                "margin_trend": 0.05,
                                "free_cash_flow_positive": True,
                                "pe": 40,
                                "peg": 1.8,
                                "historical_pe": 45,
                                "days_to_earnings": 45,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            scan = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "scan",
                    "--fixture-dir",
                    str(fixture_dir),
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )
            report = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "report",
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )
            validate = subprocess.run(
                [sys.executable, "-m", "advisor", "config", "validate", "--allow-missing-keys"],
                check=False,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )

            self.assertEqual(scan.returncode, 0, scan.stderr)
            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertEqual(validate.returncode, 0, validate.stderr)
            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertIn("setups parecidos", report_text)
            self.assertIn("Criterio: atingiu +2R antes de -1R dentro do horizonte", report_text)
            self.assertIn("Data mode: `fixture`", report_text)
            self.assertIn("Guidance recent: n/a", report_text)
            self.assertIn("Post earnings gap: n/a", report_text)
            self.assertNotIn("Setup win rate estimado: 62%", report_text)

    def test_report_require_live_refuses_fixture_latest_report_for_automation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp) / "fixtures"
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"
            fixture_dir.mkdir()
            (fixture_dir / "scan.json").write_text(
                json.dumps(
                    {
                        "account_capital": 50000,
                        "assets": [
                            {
                                "symbol": "AMD",
                                "asset_type": "stock",
                                "theme": "semiconductors",
                                "market_cap": 260000000000,
                                "average_volume": 50000000,
                                "revenue_growth": 0.16,
                                "eps_growth": 0.12,
                                "margin_trend": 0.03,
                                "free_cash_flow_positive": True,
                                "pe": 34,
                                "peg": 1.9,
                                "historical_pe": 36,
                                "days_to_earnings": 32,
                                "guidance_recent": False,
                                "post_earnings_gap_percent": 0.01,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "scan",
                    "--fixture-dir",
                    str(fixture_dir),
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )

            live_env = _without_live_config_env(tmp)
            live_env["FMP_API_KEY"] = "test_fmp"
            live_env["COINGECKO_API_KEY"] = "test_coingecko"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "report",
                    "--require-live",
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=live_env,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("latest_report_not_live", result.stdout)

    def test_report_main_require_live_writes_blocked_report_when_live_validation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "report",
                    "main",
                    "--include-discovery",
                    "--require-live",
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )

            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("blocked_report_written", result.stdout)
            self.assertIn("report_type: `main`", report_text)
            self.assertIn("Data mode: `blocked`", report_text)
            self.assertIn("Decisao geral: `no_trade_day`", report_text)
            self.assertIn("live_validation_failed", report_text)
            self.assertIn("missing_fmp_api_key", report_text)
            self.assertNotIn("Decisao geral: `operate`", report_text)

    def test_report_main_require_live_includes_provider_error_in_blocked_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"
            config = AdvisorConfig.default()
            config.stock_watchlist = ["MSFT"]
            config.crypto_watchlist = ["HYPE"]
            config.fmp_api_key = "test_fmp"
            config.coingecko_api_key = "test_coingecko"
            buffer = StringIO()

            with (
                patch("advisor.cli.AdvisorConfig.default", return_value=config),
                patch("advisor.cli.LiveDataLoader") as loader_class,
                redirect_stdout(buffer),
            ):
                loader_class.return_value.load_snapshots.side_effect = RuntimeError(
                    "provider_fetch_error:fmp:prices:http_error:429:Limit Reach"
                )
                exit_code = advisor_main(
                    [
                        "report",
                        "main",
                        "--require-live",
                        "--db",
                        str(db_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertEqual(exit_code, 0)
            self.assertIn("blocked_report_written", buffer.getvalue())
            self.assertIn("live_report_failed", report_text)
            self.assertIn("provider_fetch_error:fmp:prices:http_error:429:Limit Reach", report_text)
            self.assertIn("Decisao geral: `no_trade_day`", report_text)

    def test_report_main_blocks_before_provider_calls_when_fmp_run_budget_is_exceeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"
            config = AdvisorConfig.default()
            config.stock_watchlist = ["MSFT", "NVDA"]
            config.crypto_watchlist = ["HYPE"]
            config.fmp_api_key = "test_fmp"
            config.coingecko_api_key = "test_coingecko"
            config.api_run_limits["fmp"] = 10

            with (
                patch("advisor.cli.AdvisorConfig.default", return_value=config),
                patch("advisor.cli.LiveDataLoader") as loader_class,
            ):
                exit_code = advisor_main(
                    [
                        "report",
                        "main",
                        "--require-live",
                        "--db",
                        str(db_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertEqual(exit_code, 0)
            loader_class.assert_not_called()
            self.assertIn("api_budget_exceeded:fmp:16>10", report_text)
            self.assertIn("Decisao geral: `no_trade_day`", report_text)

    def test_report_close_can_generate_close_sections_without_breaking_latest_report_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "report",
                    "close",
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )
            legacy_report = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "report",
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )

            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(legacy_report.returncode, 0, legacy_report.stderr)
            self.assertIn("report_type: `close`", report_text)
            self.assertIn("## Resumo de fechamento", report_text)
            self.assertIn("## Decisao geral para o proximo dia", report_text)
            self.assertIn("## Preparacao para o proximo pregao", report_text)

    def test_report_close_from_main_uses_main_baseline_universe_without_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            history_dir = output_dir / "history"
            history_dir.mkdir(parents=True)
            db_path = Path(tmp) / "advisor.db"
            brt_date = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
            (history_dir / f"{brt_date}-main.md").write_text(
                "\n".join(
                    [
                        "# Investment and Swing Trade Advisor",
                        "- report_type: `main`",
                        "- Data mode: `live`",
                        "## Tradeable hoje",
                        "- `MSFT`",
                        "## Watchlist aprovada",
                        "- `NVDA`",
                        "## Research queue",
                        "- `HYPE`",
                        "## Equity research queue",
                        "- `MRVL`",
                        "## Setup tecnico detectado, mas nao validado",
                        "- `AMD`: review",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            config = AdvisorConfig.default()
            config.stock_watchlist = ["SHOULD_NOT_USE"]
            config.crypto_watchlist = ["HYPE"]
            config.discovery_stock_candidates = ["AAPL"]
            config.discovery_crypto_candidates = ["LINK"]
            config.fmp_api_key = "test_fmp"
            config.coingecko_api_key = "test_coingecko"
            config.api_run_limits = {}
            buffer = StringIO()

            with (
                patch("advisor.cli.AdvisorConfig.default", return_value=config),
                patch("advisor.cli.LiveDataLoader") as loader_class,
                redirect_stdout(buffer),
            ):
                loader_class.return_value.load_snapshots.return_value = []
                loader_class.return_value.load_benchmarks.return_value = {}
                exit_code = advisor_main(
                    [
                        "report",
                        "close",
                        "--from-main",
                        "--require-live",
                        "--db",
                        str(db_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            used_config = loader_class.call_args.args[0]
            self.assertEqual(exit_code, 0)
            self.assertEqual(used_config.stock_watchlist, ["MSFT", "NVDA", "MRVL", "AMD"])
            self.assertEqual(used_config.crypto_watchlist, ["HYPE"])
            loader_class.return_value.load_snapshots.assert_called_once_with(include_discovery=False)
            self.assertIn("- discovery_enabled: `false`", report_text)
            self.assertIn("- close_universe_source: `main_baseline`", report_text)
            self.assertIn("- cache_reused_from_main: `true`", report_text)

    def test_report_close_from_main_blocks_when_main_baseline_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            history_dir = output_dir / "history"
            history_dir.mkdir(parents=True)
            db_path = Path(tmp) / "advisor.db"
            brt_date = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
            (history_dir / f"{brt_date}-main.md").write_text(
                "\n".join(
                    [
                        "# Investment and Swing Trade Advisor",
                        "- report_type: `main`",
                        "- Data mode: `blocked`",
                        "- Decisao geral: `no_trade_day`",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            config = AdvisorConfig.default()
            config.fmp_api_key = "test_fmp"
            config.coingecko_api_key = "test_coingecko"

            with (
                patch("advisor.cli.AdvisorConfig.default", return_value=config),
                patch("advisor.cli.LiveDataLoader") as loader_class,
            ):
                exit_code = advisor_main(
                    [
                        "report",
                        "close",
                        "--from-main",
                        "--require-live",
                        "--db",
                        str(db_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertEqual(exit_code, 0)
            loader_class.assert_not_called()
            self.assertIn("main_baseline_missing_or_blocked", report_text)
            self.assertIn("Data mode: `blocked`", report_text)
            self.assertIn("Decisao geral: `no_trade_day`", report_text)

    def test_report_close_from_main_can_use_latest_main_from_sqlite_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"
            SQLiteCache(db_path).save_latest_report(
                "\n".join(
                    [
                        "# Investment and Swing Trade Advisor",
                        "- report_type: `main`",
                        "- Data mode: `live`",
                        "## Research queue",
                        "- `MSFT`",
                        "",
                    ]
                ),
                "<html></html>",
            )
            config = AdvisorConfig.default()
            config.fmp_api_key = "test_fmp"
            config.coingecko_api_key = "test_coingecko"

            with (
                patch("advisor.cli.AdvisorConfig.default", return_value=config),
                patch("advisor.cli.LiveDataLoader") as loader_class,
            ):
                loader_class.return_value.load_snapshots.return_value = []
                loader_class.return_value.load_benchmarks.return_value = {}
                exit_code = advisor_main(
                    [
                        "report",
                        "close",
                        "--from-main",
                        "--require-live",
                        "--db",
                        str(db_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            used_config = loader_class.call_args.args[0]
            self.assertEqual(exit_code, 0)
            self.assertEqual(used_config.stock_watchlist, ["MSFT"])
            self.assertIn("- close_universe_source: `main_baseline`", report_text)

    def test_report_close_fmp_429_marks_rate_limited_and_no_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            history_dir = output_dir / "history"
            history_dir.mkdir(parents=True)
            db_path = Path(tmp) / "advisor.db"
            brt_date = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
            (history_dir / f"{brt_date}-main.md").write_text(
                "\n".join(
                    [
                        "# Investment and Swing Trade Advisor",
                        "- report_type: `main`",
                        "- Data mode: `live`",
                        "## Research queue",
                        "- `MSFT`",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            config = AdvisorConfig.default()
            config.fmp_api_key = "test_fmp"
            config.coingecko_api_key = "test_coingecko"

            with (
                patch("advisor.cli.AdvisorConfig.default", return_value=config),
                patch("advisor.cli.LiveDataLoader") as loader_class,
            ):
                loader_class.return_value.load_snapshots.side_effect = RuntimeError(
                    "provider_fetch_error:fmp:prices:http_error:429:Limit Reach"
                )
                exit_code = advisor_main(
                    [
                        "report",
                        "close",
                        "--from-main",
                        "--require-live",
                        "--db",
                        str(db_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertEqual(exit_code, 0)
            self.assertIn("- provider_rate_limit_status: `rate_limited`", report_text)
            self.assertIn("- fmp_status: `rate_limited`", report_text)
            self.assertIn("FMP rate limit atingido; relatorio bloqueado ou degradado conforme cache/fallback disponivel.", report_text)
            self.assertIn("Decisao geral: `no_trade_day`", report_text)
            self.assertNotIn("Decisao geral: `operate`", report_text)

    def test_scan_derives_regimes_from_fixture_benchmarks(self):
        def candle_rows(step):
            return [
                {
                    "date": f"2026-01-{(index % 28) + 1:02d}",
                    "open": 100 + index * step - 0.2,
                    "high": 100 + index * step + 1,
                    "low": 100 + index * step - 1,
                    "close": 100 + index * step,
                    "volume": 1_000_000,
                }
                for index in range(220)
            ]

        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp) / "fixtures"
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"
            fixture_dir.mkdir()
            (fixture_dir / "scan.json").write_text(
                json.dumps(
                    {
                        "account_capital": 50000,
                        "benchmarks": {"SPY": candle_rows(1), "QQQ": candle_rows(1)},
                        "assets": [
                            {
                                "symbol": "MSFT",
                                "asset_type": "stock",
                                "theme": "software",
                                "candles": candle_rows(1),
                                "market_cap": 3_000_000_000_000,
                                "average_volume": 20_000_000,
                                "revenue_growth": 0.20,
                                "eps_growth": 0.18,
                                "margin_trend": 0.05,
                                "free_cash_flow_positive": True,
                                "pe": 32,
                                "peg": 1.8,
                                "historical_pe": 35,
                                "days_to_earnings": 45,
                            },
                            {
                                "symbol": "BTC",
                                "asset_type": "crypto",
                                "theme": "crypto",
                                "candles": candle_rows(1),
                                "market_cap": 1_500_000_000_000,
                                "average_volume": 45_000_000_000,
                                "funding_rate": 0.01,
                                "open_interest_change": 0.05,
                            },
                            {
                                "symbol": "ETH",
                                "asset_type": "crypto",
                                "theme": "crypto",
                                "candles": candle_rows(2),
                                "market_cap": 500_000_000_000,
                                "average_volume": 20_000_000_000,
                                "funding_rate": 0.01,
                                "open_interest_change": 0.05,
                            },
                            {
                                "symbol": "SOL",
                                "asset_type": "crypto",
                                "theme": "crypto",
                                "candles": candle_rows(2),
                                "market_cap": 90_000_000_000,
                                "average_volume": 5_000_000_000,
                                "funding_rate": 0.01,
                                "open_interest_change": 0.05,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            scan = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "scan",
                    "--fixture-dir",
                    str(fixture_dir),
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )

            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertEqual(scan.returncode, 0, scan.stderr)
            self.assertIn("Stock regime: `risk_on`", report_text)
            self.assertIn("Crypto regime: `risk_on`", report_text)
            self.assertIn("Relative strength:", report_text)

    def test_backtest_command_outputs_historical_win_rate_summary(self):
        command = subprocess.run(
            [sys.executable, "-m", "advisor", "backtest"],
            check=False,
            text=True,
            capture_output=True,
            env=_without_live_config_env(tempfile.gettempdir()),
        )

        payload = json.loads(command.stdout)
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertIn("sample_size", payload)
        self.assertIn("win_rate_2r", payload)
        self.assertEqual(payload["criterion"], "+2R_before_-1R")
        self.assertEqual(payload["data_mode"], "demo")
        self.assertIsNone(payload["win_rate_2r"])
        self.assertIsNone(payload["win_rate_3r"])
        self.assertIn("demo_backtest_not_historical", payload["limitations"])

    def test_backtest_command_releases_only_two_r_with_adequate_fixture_sample(self):
        winner = {
            "candles": [
                {"date": "2026-01-01", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
                {"date": "2026-01-02", "open": 101, "high": 106, "low": 100, "close": 105, "volume": 1000},
                {"date": "2026-01-03", "open": 105, "high": 111, "low": 104, "close": 110, "volume": 1000},
            ],
            "entry": 100,
            "stop": 95,
            "max_days": 30,
        }
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp)
            (fixture_dir / "backtest.json").write_text(
                json.dumps({"setups": [winner for _ in range(30)]}),
                encoding="utf-8",
            )

            command = subprocess.run(
                [sys.executable, "-m", "advisor", "backtest", "--fixture-dir", str(fixture_dir)],
                check=False,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )

        payload = json.loads(command.stdout)
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(payload["data_mode"], "fixture")
        self.assertEqual(payload["sample_size"], 30)
        self.assertEqual(payload["sample_quality"], "medium")
        self.assertEqual(payload["win_rate_2r"], 1.0)
        self.assertIsNone(payload["win_rate_3r"])
        self.assertIn("three_r_sample_low", payload["limitations"])

    def test_collect_crypto_flow_persists_normalized_live_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "advisor.db"
            flows = {
                "BTC": {
                    "source": "binance",
                    "funding_rate": 0.0025,
                    "open_interest_change": 0.25,
                    "cvd_proxy": 0.2,
                    "cvd_is_proxy": True,
                    "limitations": ["liquidations_unavailable"],
                },
                "HYPE": {
                    "source": "hyperliquid",
                    "funding_rate": 0.0002,
                    "open_interest": 250,
                    "limitations": ["cvd_proxy_unavailable"],
                },
            }
            buffer = StringIO()

            with (
                patch("advisor.cli.LiveDataLoader") as loader_class,
                redirect_stdout(buffer),
            ):
                loader_class.return_value.collect_crypto_flow.return_value = flows
                exit_code = advisor_main(["collect-crypto-flow", "--db", str(db_path)])

            cached = SQLiteCache(db_path).get_json(
                "crypto_flow",
                "latest",
                max_age_seconds=3600,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(cached["assets"], flows)
            self.assertEqual(cached["status"], "degraded")
            self.assertIn("BTC,HYPE", buffer.getvalue())

    def test_default_scan_marks_demo_data_as_not_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"

            scan = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "scan",
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )

            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertEqual(scan.returncode, 0, scan.stderr)
            self.assertIn("Data mode: `demo`", report_text)
            self.assertIn("demo_data_not_live", report_text)

    def test_scan_require_live_fails_instead_of_falling_back_to_demo_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"

            scan = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "scan",
                    "--require-live",
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )

            self.assertNotEqual(scan.returncode, 0)
            self.assertIn("missing_fmp_api_key", scan.stdout)
            self.assertIn("missing_coingecko_api_key", scan.stdout)
            self.assertFalse((output_dir / "advisor-report.md").exists())

    def test_scan_require_live_rejects_fixture_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp) / "fixtures"
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"
            fixture_dir.mkdir()
            (fixture_dir / "scan.json").write_text(
                json.dumps({"account_capital": 50000, "assets": []}),
                encoding="utf-8",
            )

            scan = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "scan",
                    "--require-live",
                    "--fixture-dir",
                    str(fixture_dir),
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )

            self.assertNotEqual(scan.returncode, 0)
            self.assertIn("require_live_conflicts_with_fixture_dir", scan.stdout)
            self.assertFalse((output_dir / "advisor-report.md").exists())

    def test_scan_live_provider_error_returns_clean_failure_without_partial_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"
            config = AdvisorConfig.default()
            config.stock_watchlist = ["MSFT"]
            config.crypto_watchlist = ["HYPE"]
            config.fmp_api_key = "demo"
            config.coingecko_api_key = "demo"
            buffer = StringIO()

            with (
                patch("advisor.cli.AdvisorConfig.default", return_value=config),
                patch("advisor.cli.LiveDataLoader") as loader_class,
                redirect_stdout(buffer),
            ):
                loader_class.return_value.load_snapshots.side_effect = RuntimeError(
                    "provider_api_error:fmp:Invalid API call."
                )

                exit_code = advisor_main(
                    [
                        "scan",
                        "--require-live",
                        "--db",
                        str(db_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("provider_api_error:fmp:Invalid API call.", buffer.getvalue())
            self.assertNotIn("Traceback", buffer.getvalue())
            self.assertFalse((output_dir / "advisor-report.md").exists())

    def test_config_validate_require_live_overrides_allow_missing_keys(self):
        command = subprocess.run(
            [
                sys.executable,
                "-m",
                "advisor",
                "config",
                "validate",
                "--allow-missing-keys",
                "--require-live",
            ],
            check=False,
            text=True,
            capture_output=True,
            env=_without_live_config_env(tempfile.gettempdir()),
        )

        self.assertNotEqual(command.returncode, 0)
        self.assertIn("missing_fmp_api_key", command.stdout)
        self.assertIn("missing_coingecko_api_key", command.stdout)
        self.assertIn("next_steps:", command.stdout)
        self.assertIn("copy .env.example to .env", command.stdout)
        self.assertIn("set FMP_API_KEY", command.stdout)
        self.assertIn("set COINGECKO_API_KEY", command.stdout)
        self.assertIn("run advisor config validate --require-live", command.stdout)

    def test_config_validate_prints_auditable_operational_summary(self):
        command = subprocess.run(
            [sys.executable, "-m", "advisor", "config", "validate", "--allow-missing-keys"],
            check=False,
            text=True,
            capture_output=True,
            env=_without_live_config_env(tempfile.gettempdir()),
        )

        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertIn("config ok", command.stdout)
        self.assertIn("stock_watchlist=11", command.stdout)
        self.assertIn("crypto_watchlist=4", command.stdout)
        self.assertIn("risk_per_trade=0.50%", command.stdout)
        self.assertIn("max_risk_per_trade=1.00%", command.stdout)
        self.assertIn("minimum_stock_market_cap=10000000000.00", command.stdout)
        self.assertIn("minimum_crypto_market_cap=5000000000.00", command.stdout)
        self.assertIn("freshness_prices_seconds=21600", command.stdout)
        self.assertIn("estimated_live_calls_base=", command.stdout)
        self.assertIn("estimated_live_calls_with_discovery=", command.stdout)
        self.assertIn("api_limit_fmp=250", command.stdout)

    def test_scan_include_discovery_uses_curated_candidates_in_demo_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"

            scan = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "scan",
                    "--include-discovery",
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
                env=_without_live_config_env(tmp),
            )

            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertEqual(scan.returncode, 0, scan.stderr)
            self.assertIn("discovery_universe_demo", report_text)
            self.assertIn("`AVGO`", report_text)
            self.assertIn("win rate oculto", report_text)
            self.assertNotIn("win rate +2R", report_text)

    def test_scan_marks_assets_with_missing_price_history_as_avoid(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp) / "fixtures"
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"
            fixture_dir.mkdir()
            (fixture_dir / "scan.json").write_text(
                json.dumps(
                    {
                        "account_capital": 50000,
                        "assets": [
                            {
                                "symbol": "NODATA",
                                "asset_type": "stock",
                                "theme": "software",
                                "candles": [],
                                "market_cap": 10_000_000_000,
                                "average_volume": 1_000_000,
                                "revenue_growth": 0.2,
                                "eps_growth": 0.1,
                                "margin_trend": 0.02,
                                "free_cash_flow_positive": True,
                                "pe": 25,
                                "peg": 1.5,
                                "historical_pe": 24,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            scan = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "scan",
                    "--fixture-dir",
                    str(fixture_dir),
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
            )

            self.assertEqual(scan.returncode, 0, scan.stderr)
            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertIn("`blocked`", report_text)
            self.assertIn("insufficient_price_history", report_text)

    def test_scan_marks_assets_with_too_little_price_history_as_avoid(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp) / "fixtures"
            output_dir = Path(tmp) / "reports"
            db_path = Path(tmp) / "advisor.db"
            fixture_dir.mkdir()
            (fixture_dir / "scan.json").write_text(
                json.dumps(
                    {
                        "account_capital": 50000,
                        "assets": [
                            {
                                "symbol": "SHORT",
                                "asset_type": "stock",
                                "theme": "software",
                                "candles": [
                                    {
                                        "date": f"2026-01-{index + 1:02d}",
                                        "open": 100 + index,
                                        "high": 101 + index,
                                        "low": 99 + index,
                                        "close": 100 + index,
                                        "volume": 1_000_000,
                                    }
                                    for index in range(20)
                                ],
                                "market_cap": 50_000_000_000,
                                "average_volume": 5_000_000,
                                "revenue_growth": 0.2,
                                "eps_growth": 0.1,
                                "margin_trend": 0.02,
                                "free_cash_flow_positive": True,
                                "pe": 25,
                                "peg": 1.5,
                                "historical_pe": 24,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            scan = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "advisor",
                    "scan",
                    "--fixture-dir",
                    str(fixture_dir),
                    "--db",
                    str(db_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                text=True,
                capture_output=True,
            )

            self.assertEqual(scan.returncode, 0, scan.stderr)
            report_text = (output_dir / "advisor-report.md").read_text(encoding="utf-8")
            self.assertIn("`blocked`", report_text)
            self.assertIn("insufficient_price_history", report_text)
            self.assertIn("price history: n/a", report_text)


if __name__ == "__main__":
    unittest.main()
