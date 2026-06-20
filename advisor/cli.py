from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from advisor.backtest import backtest_similar_setups, summarize_backtest_setups
from advisor.cache import SQLiteCache
from advisor.config import AdvisorConfig
from advisor.fixtures import benchmarks_from_fixture, load_scan_fixture, snapshots_from_fixture
from advisor.live_loader import LiveDataLoader
from advisor.models import AssetDecision, AssetSnapshot, BacktestStats, Candle, RiskPlan
from advisor.report import render_blocked_report, render_html_report, render_markdown_report
from advisor.risk import detect_return_correlation, detect_theme_concentration, rate_sample_quality
from advisor.scan_engine import derive_market_regimes, derive_relative_strength
from advisor.scoring import classify_asset, score_asset


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

    args = parser.parse_args(argv)
    if args.command == "scan":
        return _scan(args)
    if args.command == "backtest":
        return _backtest(args)
    if args.command == "collect-crypto-flow":
        return _collect_crypto_flow(args, default_db=args.db)
    if args.command == "report":
        return _report(args, default_db=args.db)
    if args.command == "signals" and args.signals_command == "update-results":
        return _signals_update_results(args, default_db=args.db)
    if args.command == "config" and args.config_command == "validate":
        return _validate_config(args)
    return 1


def _scan(args: argparse.Namespace) -> int:
    db_path = Path(args.db or "data/advisor.db")
    config = AdvisorConfig.default()
    if args.require_live:
        if args.fixture_dir is not None:
            print("require_live_conflicts_with_fixture_dir")
            return 1
        errors = config.validate(allow_missing_keys=False)
        if errors:
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
    markdown = render_markdown_report(
        decisions,
        stock_regime=stock_regime,
        crypto_regime=crypto_regime,
        report_type=getattr(args, "report_type", None) or "main",
        data_mode=data_mode,
        portfolio_alerts=portfolio_alerts,
    )
    html = render_html_report(markdown)
    cache = SQLiteCache(db_path)
    cache.save_latest_report(markdown, html)
    report_file = str(args.output_dir / "advisor-report.md")
    cache.save_signal_journal(decisions, report_file=report_file)
    _write_reports(args.output_dir, markdown, html, report_type=getattr(args, "report_type", None))
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
            return _write_blocked_report(
                output_dir=args.output_dir,
                report_type=args.report_type,
                reasons=errors,
            )
    scan_args = argparse.Namespace(
        db=str(db_path),
        fixture_dir=None,
        output_dir=args.output_dir,
        include_discovery=args.include_discovery,
        require_live=args.require_live,
        report_type=args.report_type,
        scan_errors=[],
    )
    scan_code = _scan(scan_args)
    if scan_code != 0 and args.require_live:
        return _write_blocked_report(
            output_dir=args.output_dir,
            report_type=args.report_type,
            reasons=["live_report_failed", *scan_args.scan_errors],
        )
    return scan_code


def _write_blocked_report(*, output_dir: Path, report_type: str, reasons: list[str]) -> int:
    markdown = render_blocked_report(report_type=report_type, reasons=reasons)
    html = render_html_report(markdown)
    _write_reports(output_dir, markdown, html, report_type=report_type)
    print(f"blocked_report_written={output_dir / 'advisor-report.md'}")
    return 0


def _record_scan_errors(args: argparse.Namespace, errors: list[str]) -> None:
    if hasattr(args, "scan_errors") and isinstance(args.scan_errors, list):
        args.scan_errors.extend(errors)


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


def _write_reports(output_dir: Path, markdown: str, html: str, *, report_type: str | None = None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "advisor-report.md").write_text(markdown, encoding="utf-8")
    (output_dir / "advisor-report.html").write_text(html, encoding="utf-8")
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
