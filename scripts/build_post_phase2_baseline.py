from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _field(fields: dict[str, Any], name: str, key: str, default: Any = None) -> Any:
    value = fields.get(name, {})
    return value.get(key, default) if isinstance(value, dict) else default


def _category(reason: str, fields: dict[str, Any]) -> str:
    if reason in {"guidance_recent_not_collected", "macro_not_collected_confidence_limited", "liquidations_unavailable"}:
        return "not_implemented"
    if reason == "news_not_collected_confidence_limited" and _field(fields, "news", "status") == "not_configured":
        return "not_configured"
    if "stale" in reason:
        return "stale_data"
    if reason in {"sector_relative_strength_not_collected", "coinbase_premium_unavailable", "cvd_proxy_unavailable", "open_interest_change_unavailable"}:
        return "optional_context_missing"
    if reason in {"market_not_risk_on", "high_volatility", "recent_gap_risk", "possible_priced_in", "relative_strength_weak"}:
        return "genuine_market_risk"
    if reason in {"negative_ev_with_high_data_severity", "weak_setup_win_rate", "ev_components_missing"}:
        return "backtest_limitation"
    if reason.endswith("_missing") or reason in {"fundamentals_unavailable", "insufficient_price_history", "price_history_unavailable", "mixed_provider_data", "yahoo_price_fallback", "binance_flow_unavailable", "binance_restricted_location"}:
        return "real_data_failure"
    if reason in {"technical_unvalidated", "data_incomplete_confidence_limited", "high_severity_data_not_watchlist", "confidence_limiting_data_gap"}:
        return "reporting_only"
    return "genuine_asset_risk"


def _source_parts(source: str) -> tuple[str, str]:
    return tuple(source.rsplit(":", 1)) if ":" in source else (source, "unknown")


def _counterfactual(base: str, final: str, sequence: list[dict[str, Any]], categories: set[str]) -> str:
    caps = [entry for entry in sequence if str(entry.get("cap_applied", "")).startswith("cap_")]
    return base if caps and all(entry["category"] in categories for entry in caps) else final


def _report_scores(markdown: str, symbol: str) -> tuple[int | None, int | None]:
    block = re.search(rf"^## {re.escape(symbol)}$([\s\S]*?)(?=^## |\Z)", markdown, flags=re.MULTILINE)
    if block is None:
        return None, None
    def number(label: str) -> int | None:
        match = re.search(rf"^- {re.escape(label)}: `?(\d+)`?", block.group(1), flags=re.MULTILINE)
        return int(match.group(1)) if match else None
    return number("decision_confidence_score"), number("data_quality_score")


def build_baseline(*, audit_dir: Path, main_artifact_dir: Path, close_artifact_dir: Path, main_run_id: str, close_run_id: str, commit_sha: str) -> dict[str, Any]:
    gate_assets = _load(audit_dir / "gate-analysis.json").get("assets", {})
    lineage_assets = _load(audit_dir / "data-lineage.json").get("assets", {})
    summary = _load(audit_dir / "audit-summary.json")
    provider = _load(audit_dir / "provider-audit.json")
    main_report_path = main_artifact_dir / "advisor-report.md"
    main_report = main_report_path.read_text(encoding="utf-8") if main_report_path.exists() else ""
    recurring: dict[str, dict[str, Any]] = defaultdict(lambda: {"assets": [], "categories": Counter(), "caps": Counter()})
    assets = []
    for symbol, traced in sorted(gate_assets.items()):
        lineage = lineage_assets.get(symbol, {})
        fields = lineage.get("fields", {})
        sequence = []
        for gate in traced.get("gates", []):
            category = _category(str(gate["gate"]), fields)
            source_file, source_function = _source_parts(str(gate.get("source", "unknown")))
            item = {
                "reason_code": gate["gate"],
                "category": category,
                "severity": "cap" if str(gate.get("effect", "")).startswith("cap_") else "observed",
                "decision_before": traced.get("base_decision"),
                "cap_applied": gate.get("effect"),
                "decision_after": traced.get("final_decision"),
                "source_file": source_file,
                "source_function": source_function,
            }
            sequence.append(item)
            recurring[item["reason_code"]]["assets"].append(symbol)
            recurring[item["reason_code"]]["categories"][category] += 1
            recurring[item["reason_code"]]["caps"][item["cap_applied"] or "input_only"] += 1
        base = traced.get("base_decision", "unknown")
        final = traced.get("final_decision", "unknown")
        decision_confidence, data_quality = _report_scores(main_report, symbol)
        assets.append({
            "symbol": symbol,
            "asset_type": lineage.get("asset_type", "unknown"),
            "data_summary": {
                "quote_status": _field(fields, "quote_status", "status", "unavailable"),
                "quote_provider": _field(fields, "quote_provider", "value_preview"),
                "quote_timestamp": _field(fields, "quote_timestamp", "value_preview"),
                "quote_age_seconds": _field(fields, "quote_age_seconds", "value_preview"),
                "latest_candle_date": _field(fields, "latest_candle_date", "value_preview"),
                "candle_data_kind": _field(fields, "latest_candle_date", "market_data_kind"),
                "candle_source_timestamp": _field(fields, "candles", "source_data_timestamp"),
                "cache_fetched_at": _field(fields, "candles", "cache_fetched_at"),
                "cache_age_seconds": _field(fields, "candles", "cache_age_seconds"),
                "earnings_status": _field(fields, "earnings_date", "status", "unknown"),
                "guidance_status": _field(fields, "guidance", "status", "unknown"),
                "macro_status": _field(fields, "macro", "status", "unknown"),
                "news_status": _field(fields, "news", "status", "unknown"),
                "sec_filings_status": _field(fields, "sec_filings", "status", "unknown"),
                "sector_benchmark_status": _field(fields, "sector_benchmark", "status", "unknown"),
                "liquidations_status": _field(fields, "liquidations", "status", "unknown"),
            },
            "scores": {"investment_quality": traced.get("base_scores", {}).get("investment_quality"), "swing_trade": traced.get("base_scores", {}).get("swing_trade"), "decision_confidence": decision_confidence, "data_quality": data_quality},
            "base_decision": base,
            "gate_sequence": sequence,
            "final_decision": final,
            "diagnostic_counterfactuals": {
                "current": final,
                "without_not_implemented": _counterfactual(base, final, sequence, {"not_implemented"}),
                "without_optional_context_missing": _counterfactual(base, final, sequence, {"optional_context_missing"}),
                "without_backtest_hard_gate": _counterfactual(base, final, sequence, {"backtest_limitation"}),
                "preserving_only_real_risks": _counterfactual(base, final, sequence, {"not_implemented", "optional_context_missing", "not_configured", "reporting_only", "backtest_limitation"}),
            },
        })
    gates = [{"gate": reason, "assets_affected": sorted(item["assets"]), "frequency": len(item["assets"]), "category": item["categories"].most_common(1)[0][0], "cap_applied": item["caps"].most_common(1)[0][0]} for reason, item in sorted(recurring.items(), key=lambda item: (-len(item[1]["assets"]), item[0]))]
    return {"schema_version": "post-phase2-gate-baseline-v1", "diagnostic_only": True, "commit_sha": commit_sha, "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "source": {"main_run_id": main_run_id, "close_run_id": close_run_id, "main_artifact_dir": str(main_artifact_dir), "close_artifact_dir": str(close_artifact_dir), "audit_summary": summary, "provider_audit_errors": provider.get("errors", [])}, "assets": assets, "recurring_gates": gates}


def build_schema_drift(provider_audit: dict[str, Any]) -> dict[str, Any]:
    occurrences = []
    for provider_name, provider in sorted(provider_audit.get("providers", {}).items()):
        for call in provider.get("calls", []):
            if call.get("schema_valid") is not False:
                continue
            records = call.get("records_returned")
            impact = "fallback_triggered" if call.get("fallback_used") else "snapshot_degraded" if records == 0 else "field_missing"
            occurrences.append({
                "provider": call.get("provider", provider_name),
                "endpoint_name": call.get("endpoint_name"),
                "symbol": call.get("symbol"),
                "expected_schema": call.get("fields_missing", []),
                "actual_top_level_type": call.get("payload_type"),
                "actual_fields": call.get("fields_present", []),
                "missing_expected_fields": call.get("fields_missing", []),
                "unexpected_fields": [],
                "parser": "audit.validate_schema",
                "impact": impact,
                "payload_preview_sanitized": {"records_returned": records, "http_status": call.get("http_status"), "status": call.get("status"), "failure_cause": call.get("failure_cause")},
            })
    return {"schema_version": "post-phase2-schema-drift-v1", "sanitized": True, "occurrences": occurrences}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a diagnostic-only post-Phase-2 gate baseline from explicit artifact paths.")
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--main-artifact-dir", type=Path, required=True)
    parser.add_argument("--close-artifact-dir", type=Path, required=True)
    parser.add_argument("--main-run-id", required=True)
    parser.add_argument("--close-run-id", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-doc", type=Path, required=True)
    parser.add_argument("--schema-drift-output", type=Path, required=True)
    args = parser.parse_args()
    baseline = build_baseline(audit_dir=args.audit_dir, main_artifact_dir=args.main_artifact_dir, close_artifact_dir=args.close_artifact_dir, main_run_id=args.main_run_id, close_run_id=args.close_run_id, commit_sha=args.commit_sha)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    drift = build_schema_drift(_load(args.audit_dir / "provider-audit.json"))
    args.schema_drift_output.parent.mkdir(parents=True, exist_ok=True)
    args.schema_drift_output.write_text(json.dumps(drift, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Post-Phase 2 Baseline", "", "## Source", "", f"- Commit: `{args.commit_sha}`", f"- Main run: `{args.main_run_id}`", f"- Close run: `{args.close_run_id}`", "- Diagnostic only: no scoring, gate, threshold, or decision behavior was changed.", "", "## Recurring gates", "", "| Gate | Assets | Category | Frequency | Cap |", "|---|---|---|---:|---|"]
    for gate in baseline["recurring_gates"]:
        lines.append(f"| `{gate['gate']}` | {', '.join(gate['assets_affected'])} | `{gate['category']}` | {gate['frequency']} | `{gate['cap_applied']}` |")
    lines += ["", "## Counterfactuals", "", "The JSON counterfactuals are diagnostic rollback views only; they are not recommendations and do not change any gate or decision."]
    args.output_doc.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
