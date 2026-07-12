from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from advisor.backtest import backtest_similar_setups, summarize_backtest_setups
from advisor.audit import run_data_audit
from advisor.cache import SQLiteCache
from advisor.config import AdvisorConfig
from advisor.fixtures import benchmarks_from_fixture, load_scan_fixture, snapshots_from_fixture
from advisor.live_loader import LiveDataLoader
from advisor.models import AssetDecision, AssetSnapshot, BacktestStats, Candle, RiskPlan
from advisor.report import render_analyst_review_input, render_blocked_report, render_html_report, render_markdown_report
from advisor.risk import detect_return_correlation, detect_theme_concentration, rate_sample_quality
from advisor.scan_engine import derive_market_regimes, derive_relative_strength
from advisor.scoring import classify_asset, score_asset
from advisor.telegram_notify import notify_from_report


MIN_PRICE_HISTORY_FOR_SCORING = 80


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="advisor")
    parser.add_argument("--db", default="data/advisor.db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--fixture-dir", type=Path)
    scan_parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    scan_parser.add_argument("--db", default=None)
    scan_parser.add_argument("--include-discovery", action="store_true")
    scan_parser.add_argument("--require-live", action="store_true")

    backtest_parser = subparsers.add_parser("backtest")
    backtest_parser.add_argument("--fixture-dir", type=Path)

    flow_parser = subparsers.add_parser("collect-crypto-flow")
    flow_parser.add_argument("--fixture-dir", type=Path)
    flow_parser.add_argument("--db", default=None)

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("report_type", nargs="?", choices=["main", "close"])
    report_parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    report_parser.add_argument("--db", default=None)
    report_parser.add_argument("--include-discovery", action="store_true")
    report_parser.add_argument("--require-live", action="store_true")
    report_parser.add_argument("--from-main", action="store_true")

    notify_parser = subparsers.add_parser("notify-telegram")
    notify_parser.add_argument("--report", type=Path, default=Path("reports/latest.md"))
    notify_parser.add_argument("--artifact-path", default="reports/latest.md")
    notify_parser.add_argument("--workflow-url", default="")

    signals_parser = subparsers.add_parser("signals")
    signals_parser.add_argument("--db", default=None)
    signals_subparsers = signals_parser.add_subparsers(dest="signals_command", required=True)
    update_results_parser = signals_subparsers.add_parser("update-results")
    update_results_parser.add_argument("--fixture-dir", type=Path)

    config_parser = subparsers.add_parser("config")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    validate_parser = config_subparsers.add_parser("validate")
    validate_parser.add_argument("--allow-missing-keys", action="store_true")
    validate_parser.add_argument("--require-live", action="store_true")

    audit_parser = subparsers.add_parser("audit")
    audit_subparsers = audit_parser.add_subparsers(dest="audit_command", required=True)
    audit_data_parser = audit_subparsers.add_parser("data")
    audit_data_parser.add_argument("--require-live", action="store_true")
    audit_data_parser.add_argument("--include-discovery", action="store_true")
    audit_data_parser.add_argument("--output-dir", type=Path, default=Path("reports/audit"))
    audit_data_parser.add_argument("--source-db", type=Path, default=Path("data/advisor.db"))
    audit_data_parser.add_argument("--db", dest="source_db", type=Path, default=argparse.SUPPRESS)
    audit_data_parser.add_argument("--audit-db", type=Path, default=Path("reports/audit/audit.db"))
    audit_data_parser.add_argument("--symbols", default="")
    audit_data_parser.add_argument("--no-network", action="store_true")
    audit_data_parser.add_argument("--trace-gates", action="store_true")
    audit_data_parser.add_argument("--fail-on-schema-drift", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "scan":
        return _scan(args)
    if args.command == "backtest":
        return _backtest(args)
    if args.command == "collect-crypto-flow":
        return _collect_crypto_flow(args, default_db=args.db)
    if args.command == "report":
        return _report(args, default_db=args.db)
    if args.command == "notify-telegram":
        return _notify_telegram(args)
    if args.command == "signals" and args.signals_command == "update-results":
        return _signals_update_results(args, default_db=args.db)
    if args.command == "config" and args.config_command == "validate":
        return _validate_config(args)
    if args.command == "audit" and args.audit_command == "data":
        return _audit_data(args)
    return 1


def _scan(args: argparse.Namespace) -> int:
    db_path = Path(args.db or "data/advisor.db")
    config = getattr(args, "config", None) or AdvisorConfig.default()
    provider_budget = _provider_budget_summary(
        config,
        include_discovery=args.include_discovery,
        universe_scanned=0,
        close_universe_source=getattr(args, "close_universe_source", None),
        cache_reused_from_main=getattr(args, "cache_reused_from_main", False),
    )
    if args.require_live and not getattr(args, "skip_live_validation", False):
        if args.fixture_dir is not None:
            print("require_live_conflicts_with_fixture_dir")
            return 1
        errors = config.validate(allow_missing_keys=False)
        if errors:
            if hasattr(args, "provider_budget"):
                args.provider_budget = _provider_budget_summary(
                    config,
                    include_discovery=args.include_discovery,
                    universe_scanned=0,
                    errors=errors,
                    close_universe_source=getattr(args, "close_universe_source", None),
                    cache_reused_from_main=getattr(args, "cache_reused_from_main", False),
                )
            _record_scan_errors(args, errors)
            print("\n".join(_format_config_errors(errors)))
            return 1
    if args.fixture_dir is None:
        if config.has_live_keys():
            try:
                _enforce_live_budget(config, include_discovery=args.include_discovery)
                data_mode = "live"
                payload = {
                    "account_capital": config.account_capital,
                }
                loader = LiveDataLoader(config, db_path=db_path)
                snapshots = loader.load_snapshots(include_discovery=args.include_discovery)
                benchmarks = loader.load_benchmarks()
            except RuntimeError as error:
                provider_budget = _provider_budget_summary(
                    config,
                    include_discovery=args.include_discovery,
                    universe_scanned=0,
                    loader=locals().get("loader"),
                    errors=[str(error)],
                    close_universe_source=getattr(args, "close_universe_source", None),
                    cache_reused_from_main=getattr(args, "cache_reused_from_main", False),
                )
                if hasattr(args, "provider_budget"):
                    args.provider_budget = provider_budget
                _record_scan_errors(args, [str(error)])
                print(str(error))
                return 1
        else:
            data_mode = "demo"
            payload = _default_scan_payload(include_discovery=args.include_discovery)
            snapshots = snapshots_from_fixture(payload)
            benchmarks = benchmarks_from_fixture(payload)
    else:
        data_mode = "fixture"
        payload = load_scan_fixture(args.fixture_dir)
        snapshots = snapshots_from_fixture(payload)
        benchmarks = benchmarks_from_fixture(payload)
    account_capital = float(payload.get("account_capital", config.account_capital))
    coverage_universe = _coverage_universe(config)
    regimes = derive_market_regimes(snapshots=snapshots, benchmarks=benchmarks)
    stock_regime = payload.get("stock_regime", regimes.stock.label)
    crypto_regime = payload.get("crypto_regime", regimes.crypto.label)
    decisions = []
    for snapshot in snapshots:
        if len(snapshot.candles) < MIN_PRICE_HISTORY_FOR_SCORING:
            decisions.append(_unscorable_decision(snapshot, "insufficient_price_history"))
            continue
        scored = score_asset(
            snapshot,
            stock_regime_label=stock_regime,
            crypto_regime_label=crypto_regime,
            account_capital=account_capital,
            risk_fraction=config.risk_fraction,
            relative_strength_percent=derive_relative_strength(
                snapshot,
                snapshots=snapshots,
                benchmarks=benchmarks,
            ),
            minimum_market_cap=(
                config.minimum_crypto_market_cap
                if snapshot.asset_type == "crypto"
                else config.minimum_stock_market_cap
            ),
        )
        stats = _stats_for_snapshot(payload, snapshot)
        decisions.append(classify_asset(scored, stats))

    candidate_symbols = {
        decision.symbol
        for decision in decisions
        if decision.decision in {"tradeable", "watch_buy"}
    }
    candidate_snapshots = [
        snapshot
        for snapshot in snapshots
        if snapshot.symbol in candidate_symbols
    ]
    portfolio_alerts = detect_theme_concentration(
        {snapshot.symbol: snapshot.theme for snapshot in candidate_snapshots},
        max_same_theme=2,
    )
    portfolio_alerts.extend(
        detect_return_correlation(
            {snapshot.symbol: snapshot.candles for snapshot in candidate_snapshots},
        )
    )
    portfolio_alerts.extend(str(alert) for alert in payload.get("portfolio_alerts", []))
    provider_budget = _provider_budget_summary(
        config,
        include_discovery=args.include_discovery,
        universe_scanned=len(decisions),
        loader=locals().get("loader"),
        close_universe_source=getattr(args, "close_universe_source", None),
        cache_reused_from_main=getattr(args, "cache_reused_from_main", False),
    )
    deep_analysis_candidates = [decision.symbol for decision in _rank_report_decisions(decisions)[:5]]
    deep_skipped = _deep_analysis_skipped(coverage_universe, decisions)
    provider_budget["deep_analysis_limited_by_budget"] = bool(deep_skipped)
    provider_budget["deep_analysis_skipped"] = deep_skipped
    markdown = render_markdown_report(
        decisions,
        stock_regime=stock_regime,
        crypto_regime=crypto_regime,
        report_type=getattr(args, "report_type", None) or "main",
        data_mode=data_mode,
        portfolio_alerts=portfolio_alerts,
        provider_budget=provider_budget,
        coverage_universe=coverage_universe,
        deep_analysis_candidates=deep_analysis_candidates,
    )
    analyst_markdown = render_analyst_review_input(
        decisions,
        report_type=getattr(args, "report_type", None) or "main",
        data_mode=data_mode,
        stock_regime=stock_regime,
        crypto_regime=crypto_regime,
    )
    html = render_html_report(markdown)
    cache = SQLiteCache(db_path)
    cache.save_latest_report(markdown, html)
    report_file = str(args.output_dir / "advisor-report.md")
    cache.save_signal_journal(decisions, report_file=report_file)
    _write_reports(
        args.output_dir,
        markdown,
        html,
        report_type=getattr(args, "report_type", None),
        analyst_markdown=analyst_markdown,
    )
    print(f"Report written to {args.output_dir / 'advisor-report.md'}")
    return 0


def _backtest(args: argparse.Namespace) -> int:
    setups = _load_backtest_setups(args.fixture_dir)
    stats = summarize_backtest_setups(setups)
    has_fixture = bool(args.fixture_dir and (args.fixture_dir / "backtest.json").exists())
    data_mode = "fixture" if has_fixture else "demo"
    show_two_r = has_fixture and stats.sample_size >= 30
    show_three_r = has_fixture and stats.sample_size >= 60
    limitations = []
    if not has_fixture:
        limitations.append("demo_backtest_not_historical")
    if stats.sample_size < 30:
        limitations.append("backtest_sample_low")
    if stats.sample_size < 60:
        limitations.append("three_r_sample_low")
    print(
        json.dumps(
            {
                "data_mode": data_mode,
                "sample_size": stats.sample_size,
                "sample_quality": rate_sample_quality(stats.sample_size),
                "win_rate_2r": stats.win_rate_2r if show_two_r else None,
                "win_rate_3r": stats.win_rate_3r if show_three_r else None,
                "expected_value_r": stats.expected_value_r if show_two_r else None,
                "avg_win_r": stats.avg_win_r if show_two_r else None,
                "avg_loss_r": stats.avg_loss_r if show_two_r else None,
                "median_days_to_2r": stats.median_days_to_2r if show_two_r else None,
                "median_days_to_3r": stats.median_days_to_3r if show_three_r else None,
                "criterion": "+2R_before_-1R",
                "horizon_days": 30,
                "limitations": limitations,
            },
            sort_keys=True,
        )
    )
    return 0


def _stats_for_snapshot(payload: dict[str, object], snapshot) -> BacktestStats:
    if "sample_size" not in payload:
        return backtest_similar_setups(snapshot.candles)
    sample_size = int(payload.get("sample_size", 0))
    win_rate_2r = payload.get("win_rate_2r")
    win_rate_3r = payload.get("win_rate_3r")
    expected_value_r = payload.get("expected_value_r")
    avg_win_r = payload.get("avg_win_r")
    avg_loss_r = payload.get("avg_loss_r")
    median_days_to_2r = payload.get("median_days_to_2r")
    median_days_to_3r = payload.get("median_days_to_3r")
    return BacktestStats(
        sample_size=sample_size,
        win_rate_2r=float(win_rate_2r) if win_rate_2r is not None and sample_size >= 30 else None,
        win_rate_3r=float(win_rate_3r) if win_rate_3r is not None and sample_size >= 60 else None,
        median_days_to_2r=int(median_days_to_2r) if median_days_to_2r is not None and sample_size >= 30 else None,
        median_days_to_3r=int(median_days_to_3r) if median_days_to_3r is not None and sample_size >= 60 else None,
        expected_value_r=float(expected_value_r) if expected_value_r is not None and sample_size >= 30 else None,
        avg_win_r=float(avg_win_r) if avg_win_r is not None and sample_size >= 30 else None,
        avg_loss_r=float(avg_loss_r) if avg_loss_r is not None and sample_size >= 30 else None,
        setup_quality=rate_sample_quality(sample_size),
    )


def _collect_crypto_flow(args: argparse.Namespace, default_db: str | None = None) -> int:
    db_path = Path(args.db or default_db or "data/advisor.db")
    cache = SQLiteCache(db_path)
    if args.fixture_dir and (args.fixture_dir / "crypto_flow.json").exists():
        payload = json.loads((args.fixture_dir / "crypto_flow.json").read_text(encoding="utf-8"))
    else:
        try:
            flows = LiveDataLoader(AdvisorConfig.default(), db_path=db_path).collect_crypto_flow()
        except RuntimeError as error:
            print(str(error))
            return 1
        status = "degraded" if any(flow.get("limitations") for flow in flows.values()) else "ok"
        payload = {
            "status": status,
            "note": "CVD is a proxy when derived from taker buy/sell volume; liquidation history may be incomplete.",
            "assets": flows,
        }
    cache.set_json("crypto_flow", "latest", payload)
    assets = payload.get("assets", {})
    if isinstance(assets, dict):
        for symbol, flow in assets.items():
            cache.set_json("crypto_flow", str(symbol), flow)
    symbols = ",".join(sorted(assets)) if isinstance(assets, dict) else ""
    print(f"Crypto flow collected for {len(assets) if isinstance(assets, dict) else 0} assets: {symbols} ({payload.get('status', 'unknown')}).")
    return 0


def _report(args: argparse.Namespace, default_db: str | None = None) -> int:
    if args.report_type in {"main", "close"}:
        return _run_report_job(args, default_db=default_db)
    db_path = Path(args.db or default_db or "data/advisor.db")
    if args.require_live:
        errors = AdvisorConfig.default().validate(allow_missing_keys=False)
        if errors:
            print("\n".join(_format_config_errors(errors)))
            return 1
    latest = SQLiteCache(db_path).load_latest_report()
    if latest is None:
        print("No report found. Run advisor scan first.")
        return 1
    markdown, html = latest
    if args.require_live and "- Data mode: `live`" not in markdown:
        print("latest_report_not_live")
        return 1
    _write_reports(args.output_dir, markdown, html)
    print(f"Report written to {args.output_dir / 'advisor-report.md'}")
    return 0


def _run_report_job(args: argparse.Namespace, default_db: str | None = None) -> int:
    db_path = Path(args.db or default_db or "data/advisor.db")
    config = AdvisorConfig.default()
    if args.require_live:
        errors = config.validate(allow_missing_keys=False)
        if errors:
            provider_budget = _provider_budget_summary(
                config,
                include_discovery=args.include_discovery,
                universe_scanned=0,
                errors=errors,
            )
            return _write_blocked_report(
                output_dir=args.output_dir,
                report_type=args.report_type,
                reasons=errors,
                provider_budget=provider_budget,
            )
    close_universe_source = None
    cache_reused_from_main = False
    if args.report_type == "close" and getattr(args, "from_main", False):
        baseline = _load_main_baseline(args.output_dir, db_path=db_path)
        if baseline is None or _main_baseline_is_blocked(baseline):
            provider_budget = _provider_budget_summary(
                config,
                include_discovery=False,
                universe_scanned=0,
                close_universe_source="main_baseline",
                cache_reused_from_main=False,
            )
            return _write_blocked_report(
                output_dir=args.output_dir,
                report_type=args.report_type,
                reasons=["main_baseline_missing_or_blocked"],
                provider_budget=provider_budget,
            )
        symbols = _symbols_from_main_baseline(baseline)
        if symbols:
            _apply_close_universe_to_config(config, symbols)
            close_universe_source = "main_baseline"
            cache_reused_from_main = True
        else:
            close_universe_source = "fallback"
        args.include_discovery = False
    scan_args = argparse.Namespace(
        db=str(db_path),
        fixture_dir=None,
        output_dir=args.output_dir,
        include_discovery=args.include_discovery,
        require_live=args.require_live,
        report_type=args.report_type,
        scan_errors=[],
        provider_budget=None,
        config=config,
        skip_live_validation=True,
        close_universe_source=close_universe_source,
        cache_reused_from_main=cache_reused_from_main,
    )
    scan_code = _scan(scan_args)
    if scan_code != 0 and args.require_live:
        return _write_blocked_report(
            output_dir=args.output_dir,
            report_type=args.report_type,
            reasons=["live_report_failed", *scan_args.scan_errors],
            provider_budget=scan_args.provider_budget,
        )
    return scan_code


def _write_blocked_report(
    *,
    output_dir: Path,
    report_type: str,
    reasons: list[str],
    provider_budget: dict[str, object] | None = None,
) -> int:
    markdown = render_blocked_report(report_type=report_type, reasons=reasons, provider_budget=provider_budget)
    analyst_markdown = render_analyst_review_input(
        [],
        report_type=report_type,
        data_mode="blocked",
        stock_regime="not_verified",
        crypto_regime="not_verified",
    )
    html = render_html_report(markdown)
    _write_reports(output_dir, markdown, html, report_type=report_type, analyst_markdown=analyst_markdown)
    print(f"blocked_report_written={output_dir / 'advisor-report.md'}")
    return 0


def _load_main_baseline(output_dir: Path, *, db_path: Path) -> str | None:
    brt_date = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
    history_path = output_dir / "history" / f"{brt_date}-main.md"
    if history_path.exists():
        return history_path.read_text(encoding="utf-8")
    latest = SQLiteCache(db_path).load_latest_report()
    if latest is None:
        return None
    markdown, _html = latest
    if "- report_type: `main`" not in markdown:
        return None
    return markdown


def _main_baseline_is_blocked(markdown: str) -> bool:
    return (
        "- Data mode: `blocked`" in markdown
        or "- report_grade: `not_decision_grade`" in markdown
        or "blocked_report_written" in markdown
    )


def _symbols_from_main_baseline(markdown: str) -> list[str]:
    wanted_sections = {
        "Coverage universe",
        "Tradeable hoje",
        "Watchlist aprovada",
        "Watchlist apenas",
        "Research queue",
        "Equity research queue",
        "Setup tecnico detectado, mas nao validado",
    }
    symbols: list[str] = []
    active = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            active = line.removeprefix("## ").strip() in wanted_sections
            continue
        if not active:
            continue
        table_symbol = _symbol_from_coverage_row(line)
        if table_symbol and table_symbol not in symbols:
            symbols.append(table_symbol)
            continue
        match = re.match(r"- `([A-Z0-9.-]+)`", line)
        if match:
            symbol = match.group(1).upper()
            if symbol not in symbols:
                symbols.append(symbol)
    return symbols


def _symbol_from_coverage_row(line: str) -> str | None:
    if not line.startswith("|"):
        return None
    columns = [column.strip() for column in line.strip("|").split("|")]
    if len(columns) < 2:
        return None
    symbol = columns[0].upper()
    if symbol in {"", "TICKER", "---"} or set(symbol) == {"-"}:
        return None
    if not re.fullmatch(r"[A-Z0-9.-]{1,12}", symbol):
        return None
    return symbol


def _apply_close_universe_to_config(config: AdvisorConfig, symbols: list[str]) -> None:
    crypto_symbols = {
        "BTC",
        "ETH",
        "SOL",
        "HYPE",
        "ZEC",
        "BNB",
        "XRP",
        "LINK",
        "AVAX",
    }
    config.stock_watchlist = [symbol for symbol in symbols if symbol not in crypto_symbols]
    config.crypto_watchlist = [symbol for symbol in symbols if symbol in crypto_symbols]
    config.discovery_stock_candidates = []
    config.discovery_crypto_candidates = []


def _coverage_universe(config: AdvisorConfig) -> list[dict[str, str]]:
    return [
        *({"symbol": symbol, "asset_type": "stock"} for symbol in dict.fromkeys(config.stock_watchlist)),
        *({"symbol": symbol, "asset_type": "crypto"} for symbol in dict.fromkeys(config.crypto_watchlist)),
    ]


def _deep_analysis_skipped(
    coverage_universe: list[dict[str, str]],
    decisions: list[AssetDecision],
) -> list[str]:
    analyzed = {decision.symbol for decision in decisions}
    return [
        str(item["symbol"])
        for item in coverage_universe
        if str(item["symbol"]) not in analyzed
    ]


def _rank_report_decisions(decisions: list[AssetDecision]) -> list[AssetDecision]:
    return sorted(
        decisions,
        key=lambda decision: (
            decision.decision in {"tradeable", "watch_buy", "technical_unvalidated", "speculative_watch"},
            decision.swing_trade_score,
            decision.investment_quality_score,
            decision.decision_confidence_score,
        ),
        reverse=True,
    )


def _record_scan_errors(args: argparse.Namespace, errors: list[str]) -> None:
    if hasattr(args, "scan_errors") and isinstance(args.scan_errors, list):
        args.scan_errors.extend(errors)


def _notify_telegram(args: argparse.Namespace) -> int:
    status = notify_from_report(
        report_path=args.report,
        artifact_path=args.artifact_path,
        workflow_url=args.workflow_url,
    )
    print(status)
    return 0


def _signals_update_results(args: argparse.Namespace, default_db: str | None = None) -> int:
    db_path = Path(args.db or default_db or "data/advisor.db")
    candles_by_asset = _load_signal_result_candles(args.fixture_dir)
    updated = SQLiteCache(db_path).update_signal_results(candles_by_asset)
    print(json.dumps({"updated_signals": updated}, sort_keys=True))
    return 0


def _validate_config(args: argparse.Namespace) -> int:
    config = AdvisorConfig.default()
    allow_missing = args.allow_missing_keys and not args.require_live
    errors = config.validate(allow_missing_keys=allow_missing)
    if errors:
        print("\n".join(_format_config_errors(errors)))
        return 1
    lines = ["live config ok" if args.require_live else "config ok"]
    lines.extend(_config_summary(config))
    print("\n".join(lines))
    return 0


def _audit_data(args: argparse.Namespace) -> int:
    if args.require_live and args.no_network:
        print("require_live_conflicts_with_no_network")
        return 1
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()] or None
    try:
        result = run_data_audit(
            config=AdvisorConfig.default(),
            source_db=args.source_db,
            audit_db=args.audit_db,
            output_dir=args.output_dir,
            symbols=symbols,
            include_discovery=args.include_discovery,
            require_live=args.require_live,
            no_network=args.no_network or not args.require_live,
            trace_gates=args.trace_gates,
            fail_on_schema_drift=args.fail_on_schema_drift,
        )
    except ValueError as error:
        print(str(error))
        return 1
    print(f"data_audit_written={args.output_dir}")
    if result.get("schema_drift"):
        print("schema_drift=true")
    for error in result.get("errors", []):
        print(str(error))
    return int(result.get("exit_code", 0))


def _config_summary(config: AdvisorConfig) -> list[str]:
    base_stocks, base_cryptos = config.symbols_for_scan(include_discovery=False)
    discovery_stocks, discovery_cryptos = config.symbols_for_scan(include_discovery=True)
    lines = [
        f"stock_watchlist={len(base_stocks)} symbols={','.join(base_stocks)}",
        f"crypto_watchlist={len(base_cryptos)} symbols={','.join(base_cryptos)}",
        f"discovery_stock_candidates={len(discovery_stocks) - len(base_stocks)}",
        f"discovery_crypto_candidates={len(discovery_cryptos) - len(base_cryptos)}",
        f"account_capital={config.account_capital:.2f}",
        f"risk_per_trade={config.risk_fraction * 100:.2f}%",
        f"max_risk_per_trade={config.max_risk_fraction * 100:.2f}%",
        f"max_daily_loss={config.max_daily_loss_fraction * 100:.2f}%",
        f"max_weekly_loss={config.max_weekly_loss_fraction * 100:.2f}%",
        f"minimum_stock_market_cap={config.minimum_stock_market_cap:.2f}",
        f"minimum_crypto_market_cap={config.minimum_crypto_market_cap:.2f}",
        f"required_key_fmp={'present' if config.fmp_api_key else 'missing'}",
        f"required_key_coingecko={'present' if config.coingecko_api_key else 'missing'}",
        f"optional_key_alphavantage={'present' if config.alphavantage_api_key else 'missing'}",
        f"optional_key_coinbase={'present' if config.coinbase_api_key else 'missing'}",
        f"estimated_live_calls_base={_format_counts(config.estimated_live_calls(include_discovery=False))}",
        f"estimated_live_calls_with_discovery={_format_counts(config.estimated_live_calls(include_discovery=True))}",
    ]
    if config.max_stocks_per_run is not None:
        lines.append(f"max_stocks_per_run={config.max_stocks_per_run}")
    for namespace, seconds in sorted(config.freshness_seconds.items()):
        lines.append(f"freshness_{namespace}_seconds={seconds}")
    for provider, limit in sorted(config.api_limits.items()):
        lines.append(f"api_limit_{provider}={limit}")
    for provider, limit in sorted(config.api_run_limits.items()):
        lines.append(f"api_run_limit_{provider}={limit}")
    return lines


def _format_config_errors(errors: list[str]) -> list[str]:
    lines = list(errors)
    needs_live_key_steps = any(
        error
        in {
            "missing_fmp_api_key",
            "missing_coingecko_api_key",
            "placeholder_fmp_api_key",
            "placeholder_coingecko_api_key",
        }
        for error in errors
    )
    if needs_live_key_steps:
        lines.extend(
            [
                "next_steps:",
                "- copy .env.example to .env",
                "- set FMP_API_KEY",
                "- set COINGECKO_API_KEY",
                "- run advisor config validate --require-live",
            ]
        )
    return lines


def _format_counts(counts: dict[str, int]) -> str:
    return ",".join(f"{name}={counts[name]}" for name in sorted(counts))


def _provider_budget_summary(
    config: AdvisorConfig,
    *,
    include_discovery: bool,
    universe_scanned: int,
    loader: object | None = None,
    errors: list[str] | None = None,
    close_universe_source: str | None = None,
    cache_reused_from_main: bool = False,
) -> dict[str, object]:
    errors = errors or []
    stocks, cryptos = config.symbols_for_scan(include_discovery=include_discovery)
    uncapped_stocks = list(dict.fromkeys([
        *config.stock_watchlist,
        *(config.discovery_stock_candidates if include_discovery else []),
    ]))
    uncapped_cryptos = list(dict.fromkeys([
        *config.crypto_watchlist,
        *(config.discovery_crypto_candidates if include_discovery else []),
    ]))
    universe_requested = len(uncapped_stocks) + len(uncapped_cryptos)
    cache_hits = int(getattr(loader, "cache_hits", 0) or 0)
    cache_misses = int(getattr(loader, "cache_misses", 0) or 0)
    used_calls = dict(getattr(loader, "provider_call_counts", {}) or {})
    provider_statuses = dict(getattr(loader, "provider_statuses", {}) or {})
    provider_retry_after = dict(getattr(loader, "provider_retry_after", {}) or {})
    skipped_due_to_rate_limit_by_provider = dict(getattr(loader, "skipped_provider_calls_due_to_rate_limit", {}) or {})
    skipped_due_to_api_budget = any(str(error).startswith("api_budget_exceeded:") for error in errors)
    provider_rate_limit_status = _provider_rate_limit_status(errors, provider_statuses=provider_statuses)
    estimated_calls = config.estimated_live_calls(include_discovery=include_discovery)
    skipped_provider_calls_due_to_cache = cache_hits
    skipped_provider_calls_due_to_rate_limit = sum(int(value or 0) for value in skipped_due_to_rate_limit_by_provider.values())
    if provider_rate_limit_status == "rate_limited" and not skipped_provider_calls_due_to_rate_limit:
        skipped_provider_calls_due_to_rate_limit = max(
            int(estimated_calls.get("fmp", 0) or 0) - int(used_calls.get("fmp", 0) or 0),
            0,
        )
    return {
        "estimated_calls": estimated_calls,
        "used_calls": used_calls,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "universe_requested": universe_requested,
        "universe_scanned": universe_scanned,
        "discovery_enabled": include_discovery,
        "skipped_due_to_api_budget": skipped_due_to_api_budget,
        "provider_rate_limit_status": provider_rate_limit_status,
        "few_assets_reason": _few_assets_reason(
            include_discovery=include_discovery,
            universe_requested=universe_requested,
            universe_scanned=universe_scanned,
            capped_stock_count=len(stocks),
            uncapped_stock_count=len(uncapped_stocks),
            errors=errors,
        ),
        "actions_cache_hit": os.getenv("ADVISOR_ACTIONS_CACHE_HIT", "unknown") or "unknown",
        "cache_reused_from_main": cache_reused_from_main,
        "close_universe_source": close_universe_source or ("discovery" if include_discovery else "manual"),
        "skipped_provider_calls_due_to_cache": skipped_provider_calls_due_to_cache,
        "skipped_provider_calls_due_to_rate_limit": skipped_provider_calls_due_to_rate_limit,
        "fmp_status": _provider_status("fmp", errors, provider_statuses),
        "coingecko_status": _provider_status("coingecko", errors, provider_statuses),
        "retry_after": provider_retry_after.get("fmp") or _retry_after_from_errors(errors),
    }


def _provider_rate_limit_status(errors: list[str], *, provider_statuses: dict[str, str] | None = None) -> str:
    if provider_statuses and "rate_limited" in provider_statuses.values():
        return "rate_limited"
    if not errors:
        return "ok"
    joined = " ".join(errors).lower()
    if "429" in joined or "limit reach" in joined or "api_limit_exhausted" in joined:
        return "rate_limited"
    if "api_budget_exceeded" in joined:
        return "budget_blocked_before_fetch"
    if "provider_fetch_error" in joined or "provider_api_error" in joined:
        return "provider_error"
    if "missing_" in joined or "placeholder_" in joined:
        return "not_checked"
    return "error"


def _provider_status(provider: str, errors: list[str], provider_statuses: dict[str, str]) -> str:
    if provider in provider_statuses:
        return provider_statuses[provider]
    provider_errors = [error for error in errors if f":{provider}:" in str(error) or str(error).endswith(f":{provider}")]
    if _provider_rate_limit_status(provider_errors) == "rate_limited":
        return "rate_limited"
    if provider_errors:
        return "provider_error"
    return "ok"


def _retry_after_from_errors(errors: list[str]) -> str:
    joined = " ".join(str(error) for error in errors)
    match = re.search(r"retry[-_ ]after[:= ]+([0-9]+)", joined, flags=re.IGNORECASE)
    return match.group(1) if match else "unknown"


def _few_assets_reason(
    *,
    include_discovery: bool,
    universe_requested: int,
    universe_scanned: int,
    capped_stock_count: int,
    uncapped_stock_count: int,
    errors: list[str],
) -> str:
    if any("api_budget_exceeded" in str(error) for error in errors):
        return "budget_limit"
    if any("provider_" in str(error) or "api_limit_exhausted" in str(error) for error in errors):
        return "provider_error"
    if capped_stock_count < uncapped_stock_count:
        return "budget_limit"
    if universe_requested <= 3:
        return "manual_small_universe"
    if not include_discovery:
        return "discovery_disabled"
    if universe_scanned == 0:
        return "other"
    return "other"


def _write_reports(
    output_dir: Path,
    markdown: str,
    html: str,
    *,
    report_type: str | None = None,
    analyst_markdown: str | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "advisor-report.md").write_text(markdown, encoding="utf-8")
    (output_dir / "advisor-report.html").write_text(html, encoding="utf-8")
    if analyst_markdown is not None:
        (output_dir / "analyst-review-input.md").write_text(analyst_markdown, encoding="utf-8")
    if report_type:
        (output_dir / "latest.md").write_text(markdown, encoding="utf-8")
        (output_dir / "latest.html").write_text(html, encoding="utf-8")
    history_dir = output_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    (history_dir / f"advisor-report-{stamp}.md").write_text(markdown, encoding="utf-8")
    (history_dir / f"advisor-report-{stamp}.html").write_text(html, encoding="utf-8")
    if report_type:
        brt_date = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
        (history_dir / f"{brt_date}-{report_type}.md").write_text(markdown, encoding="utf-8")
        (history_dir / f"{brt_date}-{report_type}.html").write_text(html, encoding="utf-8")


def _enforce_live_budget(config: AdvisorConfig, *, include_discovery: bool) -> None:
    estimated = config.estimated_live_calls(include_discovery=include_discovery)
    limits = {**config.api_limits, **config.api_run_limits}
    over_budget = [
        f"{provider}:{count}>{limits[provider]}"
        for provider, count in estimated.items()
        if provider in limits and count > limits[provider]
    ]
    if over_budget:
        raise RuntimeError(f"api_budget_exceeded:{','.join(over_budget)}")


def _unscorable_decision(snapshot: AssetSnapshot, reason: str) -> AssetDecision:
    limitations = sorted(set([reason, *snapshot.missing_data]))
    if "fmp_price_unavailable" in limitations:
        limitations.append("probable_cause:fmp_plan_or_price_endpoint_unavailable")
    limitations = sorted(set(limitations))
    return AssetDecision(
        symbol=snapshot.symbol,
        asset_type=snapshot.asset_type,
        decision="blocked",
        investment_quality_score=0,
        swing_trade_score=0,
        risk_plan=RiskPlan(
            entry=0,
            stop=0,
            target_2r=0,
            target_3r=0,
            per_unit_risk=0,
            risk_amount=0,
            risk_fraction=0,
            max_position_units=0,
            max_position_value=0,
            risk_reward_2r="n/a",
            alerts=[reason],
            position_size_display="0",
        ),
        alerts=[reason],
        limitations=limitations,
        thesis="Dados de preco insuficientes; o bot nao deve sugerir entrada sem historico verificavel.",
        metrics_summary=["price history: n/a"],
        ideal_entry=0,
        alternative_entry=None,
        hold_suggestion="n/a",
        backtest_stats=BacktestStats(sample_size=0, win_rate_2r=None, win_rate_3r=None),
        sample_quality="low",
        reason_codes=sorted(set([reason, *limitations])),
        data_quality="blocked",
        missing_data_severity="critical",
        data_source=snapshot.data_source,
        data_timestamp=snapshot.data_timestamp,
        cache_age_seconds=snapshot.cache_age_seconds,
    )


def _default_scan_payload(*, include_discovery: bool = False) -> dict[str, object]:
    assets = [
        {
            "symbol": "MSFT",
            "asset_type": "stock",
            "theme": "software",
            "market_cap": 3_000_000_000_000,
            "average_volume": 20_000_000,
            "revenue_growth": 0.16,
            "eps_growth": 0.12,
            "margin_trend": 0.04,
            "free_cash_flow_positive": True,
            "pe": 32,
            "peg": 2.1,
            "historical_pe": 30,
            "days_to_earnings": 30,
            "missing_data": ["demo_data_not_live"],
        }
    ]
    portfolio_alerts = ["demo_data_not_live"]
    if include_discovery:
        assets.append(
            {
                "symbol": "AVGO",
                "asset_type": "stock",
                "theme": "semiconductors",
                "market_cap": 650_000_000_000,
                "average_volume": 5_000_000,
                "revenue_growth": 0.20,
                "eps_growth": 0.16,
                "margin_trend": 0.04,
                "free_cash_flow_positive": True,
                "pe": 35,
                "peg": 1.9,
                "historical_pe": 34,
                "days_to_earnings": 45,
                "missing_data": ["demo_data_not_live", "discovery_universe_demo"],
            }
        )
        portfolio_alerts.append("discovery_universe_demo")
    return {
        "account_capital": 50_000,
        "portfolio_alerts": portfolio_alerts,
        "stock_regime": "neutral",
        "crypto_regime": "neutral",
        "assets": assets,
    }


def _load_backtest_setups(fixture_dir: Path | None) -> list[dict[str, object]]:
    if fixture_dir and (fixture_dir / "backtest.json").exists():
        payload = json.loads((fixture_dir / "backtest.json").read_text(encoding="utf-8"))
        return [_setup_from_payload(setup) for setup in payload.get("setups", [])]
    winner = [
        Candle("2026-01-01", 100, 101, 99, 100, 1000),
        Candle("2026-01-02", 101, 106, 100, 105, 1000),
        Candle("2026-01-03", 105, 111, 104, 110, 1000),
    ]
    loser = [
        Candle("2026-01-01", 100, 101, 99, 100, 1000),
        Candle("2026-01-02", 99, 100, 94, 95, 1000),
    ]
    return [
        {"candles": winner, "entry": 100, "stop": 95, "max_days": 30},
        {"candles": loser, "entry": 100, "stop": 95, "max_days": 30},
    ]


def _setup_from_payload(setup: dict[str, object]) -> dict[str, object]:
    candles = [
        Candle(
            row["date"],
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            float(row["volume"]),
        )
        for row in setup.get("candles", [])
    ]
    return {
        "candles": candles,
        "entry": setup["entry"],
        "stop": setup["stop"],
        "max_days": setup.get("max_days", 30),
        "benchmark": setup.get("benchmark"),
        "benchmark_return": setup.get("benchmark_return"),
    }


def _load_signal_result_candles(fixture_dir: Path | None) -> dict[str, list[Candle]]:
    if not fixture_dir or not (fixture_dir / "signal_results.json").exists():
        return {}
    payload = json.loads((fixture_dir / "signal_results.json").read_text(encoding="utf-8"))
    return {
        symbol: [
            Candle(
                row["date"],
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
            )
            for row in rows
        ]
        for symbol, rows in payload.get("candles", {}).items()
    }
