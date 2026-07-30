from __future__ import annotations

import hashlib
import json
import ast
import copy
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from enum import Enum
from unittest.mock import patch
from pathlib import Path

from advisor.models import AssetDecision, AssetSnapshot, BacktestStats, Candle, EventInfo, Fundamentals, RiskPlan, ScoredAsset
from advisor.scoring import classify_asset


FIXED_NOW_UTC = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


def _canonical_value(value: object) -> object:
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, float):
        return {"__float__": value.hex()}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    raise TypeError(f"unsupported characterization value: {type(value).__name__}")


def _risk_plan_payload(plan: RiskPlan) -> dict[str, object]:
    return {
        "entry": plan.entry,
        "stop": plan.stop,
        "target_2r": plan.target_2r,
        "target_3r": plan.target_3r,
        "per_unit_risk": plan.per_unit_risk,
        "risk_amount": plan.risk_amount,
        "risk_fraction": plan.risk_fraction,
        "max_position_units": plan.max_position_units,
        "max_position_value": plan.max_position_value,
        "risk_reward_2r": plan.risk_reward_2r,
        "alerts": plan.alerts,
        "position_size_display": plan.position_size_display,
    }


def _backtest_payload(stats: BacktestStats | None) -> object:
    if stats is None:
        return None
    return {
        "sample_size": stats.sample_size,
        "win_rate_2r": stats.win_rate_2r,
        "win_rate_3r": stats.win_rate_3r,
        "median_days_to_2r": stats.median_days_to_2r,
        "median_days_to_3r": stats.median_days_to_3r,
        "expected_value_r": stats.expected_value_r,
        "avg_win_r": stats.avg_win_r,
        "avg_loss_r": stats.avg_loss_r,
        "setup_quality": stats.setup_quality,
        "max_drawdown_r": stats.max_drawdown_r,
        "period_start": stats.period_start,
        "period_end": stats.period_end,
        "benchmark_comparison": stats.benchmark_comparison,
        "warnings": stats.warnings,
    }


def _decision_payload(decision: AssetDecision) -> dict[str, object]:
    return {
        "symbol": decision.symbol,
        "asset_type": decision.asset_type,
        "decision": decision.decision,
        "investment_quality_score": decision.investment_quality_score,
        "swing_trade_score": decision.swing_trade_score,
        "risk_plan": _risk_plan_payload(decision.risk_plan),
        "alerts": decision.alerts,
        "limitations": decision.limitations,
        "thesis": decision.thesis,
        "metrics_summary": decision.metrics_summary,
        "ideal_entry": decision.ideal_entry,
        "alternative_entry": decision.alternative_entry,
        "hold_suggestion": decision.hold_suggestion,
        "backtest_stats": _backtest_payload(decision.backtest_stats),
        "sample_quality": decision.sample_quality,
        "reason_codes": decision.reason_codes,
        "data_quality": decision.data_quality,
        "missing_data_severity": decision.missing_data_severity,
        "news_summary": decision.news_summary,
        "data_source": decision.data_source,
        "data_timestamp": decision.data_timestamp,
        "cache_age_seconds": decision.cache_age_seconds,
        "bucket": decision.bucket,
        "market_session": decision.market_session,
        "last_price_timestamp": decision.last_price_timestamp,
        "provider": decision.provider,
        "is_stale": decision.is_stale,
        "stale_reason": decision.stale_reason,
        "event_check_status": decision.event_check_status,
        "news_status": decision.news_status,
        "macro_regime": decision.macro_regime,
        "macro_status": decision.macro_status,
        "thesis_status": decision.thesis_status,
        "data_quality_score": decision.data_quality_score,
        "decision_confidence_score": decision.decision_confidence_score,
        "relative_strength_vs_spy": decision.relative_strength_vs_spy,
        "relative_strength_vs_qqq": decision.relative_strength_vs_qqq,
        "relative_strength_vs_sector": decision.relative_strength_vs_sector,
        "sector_benchmark": decision.sector_benchmark,
        "short_setup_score": decision.short_setup_score,
        "squeeze_risk": decision.squeeze_risk,
        "gap_risk": decision.gap_risk,
        "borrow_data_available": decision.borrow_data_available,
        "short_status": decision.short_status,
        "universe_origin": decision.universe_origin,
    }


def _characterization_bytes(decision: AssetDecision) -> bytes:
    return json.dumps(
        _canonical_value(_decision_payload(decision)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _candles(count: int = 220) -> list[Candle]:
    return [
        Candle(
            date=f"2026-01-{(index % 28) + 1:02d}",
            open=100.0 + index - 0.2,
            high=101.0 + index,
            low=99.0 + index,
            close=100.0 + index,
            volume=1_000_000,
        )
        for index in range(count)
    ]


def _base_snapshot(
    symbol: str = "BASE",
    *,
    asset_type: str = "stock",
    theme: str = "software",
    event: EventInfo | None = None,
    cache_age_seconds: int | None = 0,
) -> AssetSnapshot:
    fundamentals = Fundamentals(
        pe=28,
        peg=1.4,
        historical_pe=32,
        revenue_growth=0.22,
        eps_growth=0.18,
        margin_trend=0.06,
        free_cash_flow_positive=True,
        market_cap=500_000_000_000,
        average_volume=10_000_000,
    )
    if asset_type == "crypto":
        fundamentals = Fundamentals(
            pe=None,
            peg=None,
            historical_pe=None,
            revenue_growth=None,
            eps_growth=None,
            margin_trend=None,
            free_cash_flow_positive=None,
            market_cap=1_500_000_000_000,
            average_volume=5_000_000_000,
        )
    return AssetSnapshot(
        symbol=symbol,
        asset_type=asset_type,
        theme=theme,
        candles=_candles(),
        fundamentals=fundamentals,
        event=event if event is not None else (None if asset_type == "crypto" else EventInfo(45, False, 0.0)),
        missing_data=[],
        news_events=[],
        data_source="fmp",
        data_timestamp="2026-07-19T21:00:00+00:00",
        cache_age_seconds=cache_age_seconds,
    )


def _base_scored(
    symbol: str = "BASE",
    *,
    investment: float = 90.0,
    swing: float = 88.0,
    alerts: list[str] | None = None,
    limitations: list[str] | None = None,
    snapshot: AssetSnapshot | None = None,
) -> ScoredAsset:
    return ScoredAsset(
        snapshot=snapshot or _base_snapshot(symbol),
        investment_quality_score=investment,
        swing_trade_score=swing,
        risk_plan=RiskPlan(
            entry=100.0,
            stop=95.0,
            target_2r=110.0,
            target_3r=115.0,
            per_unit_risk=5.0,
            risk_amount=250.0,
            risk_fraction=0.005,
            max_position_units=50,
            max_position_value=5_000.0,
            risk_reward_2r="2.00:1",
            alerts=[],
            position_size_display="50",
        ),
        alerts=list(alerts or []),
        limitations=list(limitations or []),
        thesis="Teste.",
        metrics_summary=["RSI: 55.00"],
        ideal_entry=100.0,
        alternative_entry=97.0,
        hold_suggestion="1-8 semanas",
    )


def _stats(
    *,
    sample_size: int = 120,
    win_rate_2r: float | None = 0.58,
    expected_value_r: float | None = 0.55,
    avg_win_r: float | None = 2.0,
    avg_loss_r: float | None = -1.0,
    median_days_to_2r: int | None = 8,
) -> BacktestStats:
    return BacktestStats(
        sample_size=sample_size,
        win_rate_2r=win_rate_2r,
        win_rate_3r=0.33,
        median_days_to_2r=median_days_to_2r,
        expected_value_r=expected_value_r,
        avg_win_r=avg_win_r,
        avg_loss_r=avg_loss_r,
    )


def _characterization_cases() -> dict[str, tuple[ScoredAsset, BacktestStats | None]]:
    crypto = _base_scored(
        "BTC",
        snapshot=_base_snapshot("BTC", asset_type="crypto", theme="crypto"),
    )
    stale = _base_scored("STALE", snapshot=_base_snapshot("STALE", cache_age_seconds=90_000))
    earnings = _base_scored(
        "EARN",
        alerts=["earnings_imminent"],
        snapshot=_base_snapshot("EARN", event=EventInfo(3, False, 0.0)),
    )
    high_severity = _base_scored("HIGH", limitations=["earnings_data_missing"])
    no_op = _base_scored("NOOP", limitations=["earnings_data_missing"])
    return {
        "equity_tradeable_base": (_base_scored("TRADE"), _stats()),
        "equity_watch_buy": (_base_scored("WATCH", investment=75, swing=72), _stats()),
        "equity_wait": (_base_scored("WAIT", alerts=["market_risk_off"]), _stats()),
        "equity_avoid": (_base_scored("AVOID", investment=20, swing=40), _stats()),
        "equity_technical_unvalidated": (
            _base_scored("TECH", investment=38, swing=78, limitations=["fundamentals_unavailable"]),
            _stats(sample_size=90),
        ),
        "crypto_tradeable": (crypto, _stats()),
        "low_sample": (_base_scored("LOW"), _stats(sample_size=10, median_days_to_2r=None)),
        "stale_data": (stale, _stats()),
        "missing_data_severity_high": (high_severity, _stats()),
        "confidence_below_65": (_base_scored("CONF"), _stats(expected_value_r=-0.10)),
        "liquidity_gate": (
            _base_scored("LIQ", alerts=["low_liquidity", "position_too_small_for_risk"]),
            _stats(),
        ),
        "earnings_event": (earnings, _stats()),
        "market_regime": (_base_scored("REGIME", alerts=["market_risk_off"]), _stats()),
        "below_minimum_market_cap": (
            _base_scored("CAP", alerts=["below_minimum_market_cap"]),
            _stats(),
        ),
        "no_op_weaker_cap": (no_op, _stats(sample_size=10, median_days_to_2r=None)),
        "explicit_market_cap_override": (
            _base_scored("OVERRIDE", investment=90, swing=88, alerts=["below_minimum_market_cap"]),
            _stats(),
        ),
        "early_return_helper": (_base_scored("EARLY"), _stats()),
    }


# Frozen from the pre-instrumentation behavior at HEAD 64abb26.
CHARACTERIZATION_SHA256 = {
    "equity_tradeable_base": "67fa54a4e740bf0cb52a33a3b7630e99baf520fe48f5132605d9bec9bb7936ca",
    "equity_watch_buy": "f005190242fecaae2000752d5505fcb200ef9beb0e2abb90e7c9b90584951153",
    "equity_wait": "07a59ac3031314975cd3943cfab8fc6eadf815675f42a26f9de822ce63f82ed9",
    "equity_avoid": "e3f2c4bbd9263cc9bf4231bfc15dc29afad70de3bf7996d7f4cc02b00cc56174",
    "equity_technical_unvalidated": "1aec90b0656cfbbce416d2f4682b929cda566c138b10332b7171839e2782ac75",
    "crypto_tradeable": "0158677beea8ba12c8bf5c70d1f94ac35a2d8ee27233451d85d20c2733cde1a4",
    "low_sample": "5a3f6942571754563bec8c6ca822814c2714aa7439da6a9a6ec0099ca9a870c3",
    "stale_data": "1b996142b788fca052c1d70bd5f41e6729d6908ef1b88029311e23eef9773f85",
    "missing_data_severity_high": "89c280193c943732f03e447609cbfc1135fffd70df3cbf8d06cd3c0f7abf2de5",
    "confidence_below_65": "4f8ffcf091b69bef4dbacf3cd9646f4b49004730d9815c6a3421ea2ef12a0f6f",
    "liquidity_gate": "da780a7b73f4c36ebc129af36e4ba69d173e9b2b7ab693490f1c4f856365003c",
    "earnings_event": "05ef2580638eed97ff76cd13dea06641fd7734bf10373e9467c3c29242f732f6",
    "market_regime": "7d581c0cdf38adb8481a1080afbf630a4b1c90d16165cd27f1e914fe1be3a810",
    "below_minimum_market_cap": "75dc7e04ff6bd8e2adf7d4174c57e0596a2770d05fb5e3808f933b8d9fdfed0c",
    "no_op_weaker_cap": "03f9d4ff2df2b370223048c9f7cc7bf4feace60b89f48187404b449652cc5205",
    "explicit_market_cap_override": "160b7d3b68400e77f904e15eeae5ab546c824f97aa4161be4516135858c95dd8",
    "early_return_helper": "11b969efc725933a48b8df823101f7403cfaf35f4769a89e580d56532952c5bd",
}


def _approved_observability_wrapper_code():
    import advisor.runtime_scoring_observability as runtime
    import advisor.scoring as scoring

    decorated_helper = scoring._freshness_context
    original_helper = getattr(decorated_helper, "__wrapped__", None)
    if original_helper is None:
        raise AssertionError("decorated financial helper must expose __wrapped__")
    if decorated_helper.__code__ is original_helper.__code__:
        raise AssertionError("decorated helper and original helper must have different code objects")
    if decorated_helper.__code__.co_filename != runtime.__file__:
        raise AssertionError("approved wrapper code must originate in runtime_scoring_observability")
    return decorated_helper.__code__


def _profile_legacy_classifications(cases):
    call_records = []
    decisions = {}

    def profile_call(frame, event, _arg):
        if event == "call":
            call_records.append(
                {
                    "module": frame.f_globals["__name__"],
                    "code": frame.f_code,
                    "name": frame.f_code.co_name,
                    "filename": frame.f_code.co_filename,
                    "firstlineno": frame.f_code.co_firstlineno,
                }
            )

    for fixture_name, (scored, stats) in cases.items():
        previous_profile = sys.getprofile()
        sys.setprofile(profile_call)
        try:
            decisions[fixture_name] = classify_asset(
                scored,
                stats,
                effective_now_utc=FIXED_NOW_UTC,
            )
        finally:
            sys.setprofile(previous_profile)
    return decisions, call_records


def _runtime_call_report(call_records, approved_wrapper_code):
    import advisor.runtime_scoring_observability as runtime

    runtime_records = [
        record
        for record in call_records
        if record["module"] == runtime.__name__
    ]
    runtime_code_objects = {record["code"] for record in runtime_records}
    unapproved_runtime_code_objects = runtime_code_objects - {approved_wrapper_code}
    unapproved_runtime_functions = {
        (
            record["name"],
            record["filename"],
            record["firstlineno"],
            record["code"],
        )
        for record in runtime_records
        if record["code"] in unapproved_runtime_code_objects
    }
    return {
        "approved_wrapper_code": approved_wrapper_code,
        "approved_wrapper_call_count": sum(
            record["code"] is approved_wrapper_code
            for record in runtime_records
        ),
        "runtime_code_objects": runtime_code_objects,
        "unapproved_runtime_code_objects": unapproved_runtime_code_objects,
        "unapproved_runtime_functions": unapproved_runtime_functions,
    }


def _assert_only_approved_runtime_code(report):
    unapproved = report["unapproved_runtime_functions"]
    if unapproved:
        details = sorted(
            (name, filename, firstlineno)
            for name, filename, firstlineno, _code in unapproved
        )
        raise AssertionError(f"unapproved runtime observability calls: {details}")


class ScoringCharacterizationTests(unittest.TestCase):
    def test_disabled_path_has_no_observation_infrastructure_or_payloads(self) -> None:
        import advisor.runtime_scoring_observability as runtime

        from advisor.runtime_scoring_observability import asset_decision_sha256, canonical_asset_decision_bytes

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        expected = classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)
        counts = {name: 0 for name in (
            "start_trace",
            "capture_initial_state",
            "begin_invocation",
            "update_classification_inputs",
            "observe_condition",
            "observe_value",
            "record_event",
            "end_invocation",
            "capture_final_state",
            "build_trace",
            "_state_change",
            "_decision_change",
            "_classifier_inputs",
            "RuntimeTrace",
            "InvocationTrace",
            "RuntimeEvent",
            "collector_record_event",
        )}

        def spy(name, original):
            def wrapped(*args, **kwargs):
                counts[name] += 1
                return original(*args, **kwargs)
            return wrapped

        context_methods = (
            "start_trace",
            "capture_initial_state",
            "begin_invocation",
            "update_classification_inputs",
            "observe_condition",
            "observe_value",
            "end_invocation",
            "capture_final_state",
            "build_trace",
        )
        patches = [
            patch.object(runtime.ObservationContext, name, spy(name, getattr(runtime.ObservationContext, name)))
            for name in context_methods
        ]
        patches.extend(
            [
                patch("advisor.scoring._state_change", spy("_state_change", __import__("advisor.scoring", fromlist=["_state_change"])._state_change)),
                patch("advisor.scoring._decision_change", spy("_decision_change", __import__("advisor.scoring", fromlist=["_decision_change"])._decision_change)),
                patch("advisor.scoring._classifier_inputs", spy("_classifier_inputs", __import__("advisor.scoring", fromlist=["_classifier_inputs"])._classifier_inputs)),
                patch("advisor.scoring.observe_condition", spy("observe_condition", __import__("advisor.scoring", fromlist=["observe_condition"]).observe_condition)),
                patch("advisor.scoring.observe_value", spy("observe_value", __import__("advisor.scoring", fromlist=["observe_value"]).observe_value)),
                patch.object(runtime.RuntimeTrace, "__init__", side_effect=AssertionError("disabled trace constructed")),
                patch.object(runtime.InvocationTrace, "__init__", side_effect=AssertionError("disabled invocation constructed")),
                patch.object(runtime.RuntimeEvent, "__init__", side_effect=AssertionError("disabled event constructed")),
                patch.object(
                    runtime._InMemoryCollector,
                    "record_event",
                    spy("collector_record_event", runtime._InMemoryCollector.record_event),
                ),
            ]
        )
        with ExitStack() as stack:
            for observer_patch in patches:
                stack.enter_context(observer_patch)
            actual = classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)

        self.assertEqual(expected, actual)
        self.assertEqual(canonical_asset_decision_bytes(expected), canonical_asset_decision_bytes(actual))
        self.assertEqual(asset_decision_sha256(expected), asset_decision_sha256(actual))
        self.assertTrue(all(value == 0 for value in counts.values()), counts)
        self.assertFalse(hasattr(runtime, "_NullObservationContext"))
        self.assertNotIn("_NullObservationContext", Path("advisor/runtime_scoring_observability.py").read_text(encoding="utf-8"))

    def test_catalog_matches_independent_expected_snapshot_and_ast(self) -> None:
        import advisor.runtime_scoring_observability as runtime

        snapshot_path = Path("tests/fixtures/runtime_scoring_rule_catalog_expected.json")
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["specification_commit"], "64abb26ccce5e92d0ca3c1b8e340313fbf398837")
        self.assertEqual(snapshot["expected_if_count"], 74)
        self.assertEqual(snapshot["expected_elif_count"], 8)
        self.assertEqual(snapshot["expected_ifexp_count"], 15)
        self.assertEqual(len(snapshot["rules"]), 97)
        report = runtime.validate_rule_catalog(Path("."), expected_snapshot=snapshot_path)
        self.assertEqual(report, {
            "missing_ids": [],
            "extra_ids": [],
            "metadata_mismatches": [],
            "invalid_locators": [],
            "signature_mismatches": [],
            "ownership_mismatches": [],
        })

    def test_catalog_mutations_are_rejected_by_independent_authority(self) -> None:
        import dataclasses
        import advisor.runtime_scoring_observability as runtime

        snapshot_path = Path("tests/fixtures/runtime_scoring_rule_catalog_expected.json")
        mutations = {
            "axis": lambda rule: dataclasses.replace(rule, axis="confidence"),
            "effect_type": lambda rule: dataclasses.replace(rule, effect_type="adjustment"),
            "evidence_keys": lambda rule: dataclasses.replace(rule, evidence_keys=("wrong.key",)),
            "function": lambda rule: dataclasses.replace(rule, function="classify_asset"),
            "branch_kind": lambda rule: dataclasses.replace(rule, branch_kind="ifexp"),
            "locator": lambda rule: dataclasses.replace(rule, source_code_locator={**rule.source_code_locator, "line_start": 1}),
            "canonical_branch_signature": lambda rule: dataclasses.replace(rule, branch_signature=rule.rule_id),
        }
        for field_name, mutate in mutations.items():
            with self.subTest(field=field_name):
                changed = [mutate(rule) if rule.rule_id == "classify_asset.base_avoid" else rule for rule in runtime.RULE_CATALOG]
                with patch.object(runtime, "RULE_CATALOG", tuple(changed)):
                    with self.assertRaises(AssertionError):
                        runtime.validate_rule_catalog(Path("."), expected_snapshot=snapshot_path)
        with patch.object(runtime, "RULE_CATALOG", ()):
            with self.assertRaises(AssertionError):
                runtime.validate_rule_catalog(Path("."), expected_snapshot=snapshot_path)

    def test_invocation_intervals_and_parent_graph_are_strictly_validated(self) -> None:
        import advisor.runtime_scoring_observability as runtime
        from advisor.scoring import classify_asset_with_trace

        for name, (scored, stats) in _characterization_cases().items():
            with self.subTest(fixture=name):
                _decision, trace = classify_asset_with_trace(scored, stats, effective_now_utc=FIXED_NOW_UTC)
                runtime.validate_runtime_trace(trace)
                invocations = {invocation.invocation_id: invocation for invocation in trace.invocations}
                roots = [invocation for invocation in trace.invocations if invocation.parent_invocation_id is None]
                self.assertEqual(len(roots), 1)
                for invocation in trace.invocations:
                    self.assertIsNotNone(invocation.completed_sequence)
                    self.assertLess(invocation.started_sequence, invocation.completed_sequence)
                    if invocation.parent_invocation_id is not None:
                        parent = invocations[invocation.parent_invocation_id]
                        self.assertLess(parent.started_sequence, invocation.started_sequence)
                        self.assertLess(invocation.completed_sequence, parent.completed_sequence)

        _decision, trace = classify_asset_with_trace(
            *_characterization_cases()["equity_tradeable_base"],
            effective_now_utc=FIXED_NOW_UTC,
        )
        orphan = copy.deepcopy(trace)
        orphan_child = next(invocation for invocation in orphan.invocations if invocation.parent_invocation_id is not None)
        orphan_child.parent_invocation_id = "does-not-exist#1"
        with self.assertRaises(AssertionError):
            runtime.validate_runtime_trace(orphan)

        cycle = copy.deepcopy(trace)
        root = next(invocation for invocation in cycle.invocations if invocation.parent_invocation_id is None)
        child = next(invocation for invocation in cycle.invocations if invocation.parent_invocation_id is not None)
        root.parent_invocation_id = child.invocation_id
        with self.assertRaises(AssertionError):
            runtime.validate_runtime_trace(cycle)

        invalid_interval = copy.deepcopy(trace)
        invalid_parent = next(invocation for invocation in invalid_interval.invocations if invocation.parent_invocation_id is None)
        invalid_child = next(invocation for invocation in invalid_interval.invocations if invocation.parent_invocation_id is not None)
        invalid_parent.completed_sequence = invalid_child.started_sequence
        with self.assertRaises(AssertionError):
            runtime.validate_runtime_trace(invalid_interval)

        duplicate_sequence = copy.deepcopy(trace)
        duplicate_sequence.events[1].sequence = duplicate_sequence.events[0].sequence
        with self.assertRaises(AssertionError):
            runtime.validate_runtime_trace(duplicate_sequence)

        event_owner = copy.deepcopy(trace)
        event = event_owner.events[0]
        other_rule = next(rule.rule_id for rule in runtime.RULE_CATALOG if rule.function != event_owner.invocations[0].function)
        event.rule_id = other_rule
        with self.assertRaises(AssertionError):
            runtime.validate_runtime_trace(event_owner)

    def test_partial_end_failure_keeps_open_intervals_and_unknown_coverage(self) -> None:
        from advisor.runtime_scoring_observability import ObservationContext, validate_runtime_trace
        from advisor.scoring import classify_asset_with_trace

        class FailingEndContext(ObservationContext):
            def end_invocation(self, *_args: object, **_kwargs: object) -> None:
                raise RuntimeError("end failure detail")

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        observed, trace = classify_asset_with_trace(
            scored,
            stats,
            effective_now_utc=FIXED_NOW_UTC,
            observation_context=FailingEndContext(enabled=True, effective_now_utc=FIXED_NOW_UTC),
        )
        self.assertEqual(observed, classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC))
        self.assertEqual(trace.trace_status, "partial")
        self.assertFalse(trace.coverage_complete)
        self.assertTrue(any(invocation.completed_sequence is None for invocation in trace.invocations))
        self.assertTrue(any(invocation.unknown_rule_ids for invocation in trace.invocations if invocation.catalog_rule_ids))
        validate_runtime_trace(trace)

    def test_current_classification_matches_frozen_lossless_serialization(self) -> None:
        for name, (scored, stats) in _characterization_cases().items():
            with self.subTest(fixture=name):
                class FrozenDateTime(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return cls(
                            FIXED_NOW_UTC.year,
                            FIXED_NOW_UTC.month,
                            FIXED_NOW_UTC.day,
                            FIXED_NOW_UTC.hour,
                            FIXED_NOW_UTC.minute,
                            FIXED_NOW_UTC.second,
                            FIXED_NOW_UTC.microsecond,
                            tzinfo=FIXED_NOW_UTC.tzinfo,
                        )

                with patch("advisor.scoring.datetime", FrozenDateTime):
                    decision = classify_asset(scored, stats)
                serialized = _characterization_bytes(decision)
                self.assertEqual(hashlib.sha256(serialized).hexdigest(), CHARACTERIZATION_SHA256[name])

    def test_public_adapters_preserve_all_decision_fields_and_frozen_hashes(self) -> None:
        from advisor.runtime_scoring_observability import asset_decision_sha256, canonical_asset_decision_bytes
        from advisor.scoring import classify_asset_with_trace

        for name, (scored, stats) in _characterization_cases().items():
            with self.subTest(fixture=name):
                legacy = classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)
                observed, trace = classify_asset_with_trace(scored, stats, effective_now_utc=FIXED_NOW_UTC)
                self.assertEqual(legacy, observed)
                self.assertEqual(_characterization_bytes(legacy), _characterization_bytes(observed))
                self.assertEqual(canonical_asset_decision_bytes(legacy), canonical_asset_decision_bytes(observed))
                self.assertEqual(asset_decision_sha256(legacy), asset_decision_sha256(observed))
                self.assertEqual(asset_decision_sha256(legacy), CHARACTERIZATION_SHA256[name])
                self.assertEqual(trace.classification_inputs["effective_now_utc"], FIXED_NOW_UTC)
                self.assertEqual(trace.classification_inputs["scored_asset"]["alerts"], scored.alerts)
                self.assertEqual(trace.classification_inputs["scored_asset"]["limitations"], scored.limitations)
                self.assertEqual(
                    trace.classification_inputs["scored_asset"]["snapshot"]["symbol"],
                    scored.snapshot.symbol,
                )

    def test_disabled_context_has_no_events_invocations_or_serialization(self) -> None:
        from advisor.scoring import _classify_asset_observed

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        decision, trace = _classify_asset_observed(
            scored,
            stats,
            effective_now_utc=FIXED_NOW_UTC,
            observation_context=None,
        )
        expected = classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)
        self.assertEqual(expected, decision)
        self.assertIsNone(trace)
        with patch("advisor.scoring._classifier_inputs", side_effect=AssertionError("disabled path serialized inputs")):
            decision = classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)
        self.assertEqual(expected, decision)

    def test_effective_now_is_fixed_normalized_and_read_once(self) -> None:
        from advisor.runtime_scoring_observability import ObservationContext
        from advisor.scoring import classify_asset_with_trace

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        fixed_with_offset = datetime(2026, 7, 20, 12, 0, tzinfo=timezone(timedelta(hours=-3)))
        normalized_now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
        legacy = classify_asset(scored, stats, effective_now_utc=fixed_with_offset)
        observed, trace = classify_asset_with_trace(scored, stats, effective_now_utc=fixed_with_offset)
        self.assertEqual(legacy, observed)
        self.assertEqual(trace.effective_now_utc, normalized_now)
        context = ObservationContext.create_enabled(fixed_with_offset)
        _observed_with_context, context_trace = classify_asset_with_trace(
            scored,
            stats,
            effective_now_utc=fixed_with_offset,
            observation_context=context,
        )
        self.assertEqual(context_trace.effective_now_utc, normalized_now)

        class CountingDateTime(datetime):
            now_calls = 0

            @classmethod
            def now(cls, tz=None):
                cls.now_calls += 1
                return cls(
                    normalized_now.year,
                    normalized_now.month,
                    normalized_now.day,
                    normalized_now.hour,
                    normalized_now.minute,
                    normalized_now.second,
                    normalized_now.microsecond,
                    tzinfo=normalized_now.tzinfo,
                )

        with patch("advisor.scoring.datetime", CountingDateTime):
            classify_asset(scored, stats)
        self.assertEqual(CountingDateTime.now_calls, 1)
        with self.assertRaises(ValueError):
            classify_asset(scored, stats, effective_now_utc=datetime(2026, 7, 20, 15, 0))

    def test_sector_helper_records_early_return_without_materializing_later_rules(self) -> None:
        from advisor.scoring import classify_asset_with_trace

        scored, stats = _characterization_cases()["early_return_helper"]
        _decision, trace = classify_asset_with_trace(scored, stats, effective_now_utc=FIXED_NOW_UTC)
        sector_invocations = [invocation for invocation in trace.invocations if invocation.function == "_sector_benchmark"]
        self.assertTrue(sector_invocations)
        last_sector_invocation_id = sector_invocations[-1].invocation_id
        sector_events = [
            event
            for event in trace.events
            if event.invocation_id == last_sector_invocation_id and event.rule_id.startswith("classify_asset.sector_")
        ]
        self.assertEqual(
            [(event.rule_id, event.matched, event.terminated, event.termination_kind) for event in sector_events],
            [
                ("classify_asset.sector_semiconductors", False, False, None),
                ("classify_asset.sector_software", True, True, "return"),
            ],
        )
        self.assertEqual(
            [event.branch_label for event in sector_events],
            ["_sector_benchmark.sector_semiconductors", "_sector_benchmark.sector_software"],
        )
        self.assertNotIn("classify_asset.sector_cloud", [event.rule_id for event in sector_events])
        self.assertNotIn("classify_asset.sector_healthcare", [event.rule_id for event in sector_events])
        self.assertEqual(sector_invocations[-1].termination_kind, "return")
        self.assertEqual(
            sector_invocations[-1].catalog_rule_ids,
            [
                "classify_asset.sector_semiconductors",
                "classify_asset.sector_software",
                "classify_asset.sector_cloud",
                "classify_asset.sector_healthcare",
            ],
        )
        self.assertIn("classify_asset.sector_cloud", sector_invocations[-1].unreached_rule_ids)

    def test_repeated_helpers_have_distinct_deterministic_invocations(self) -> None:
        from advisor.scoring import classify_asset_with_trace

        scored, stats = _characterization_cases()["early_return_helper"]
        _decision, trace = classify_asset_with_trace(scored, stats, effective_now_utc=FIXED_NOW_UTC)
        sector_invocations = [invocation for invocation in trace.invocations if invocation.function == "_sector_benchmark"]
        self.assertGreaterEqual(len(sector_invocations), 2)
        self.assertEqual(len({invocation.invocation_id for invocation in sector_invocations}), len(sector_invocations))
        self.assertEqual(
            len({(invocation.parent_invocation_id, invocation.call_ordinal) for invocation in sector_invocations}),
            len(sector_invocations),
        )
        self.assertTrue(all(invocation.parent_invocation_id for invocation in sector_invocations))
        blocking_invocations = [invocation for invocation in trace.invocations if invocation.function == "_has_blocking_data_gap"]
        self.assertGreaterEqual(len(blocking_invocations), 2)
        self.assertEqual(
            [invocation.call_ordinal for invocation in blocking_invocations[:2]],
            [1, 2],
        )
        rank_invocations = [invocation for invocation in trace.invocations if invocation.function == "_decision_rank"]
        self.assertTrue(rank_invocations)
        self.assertEqual(
            len({invocation.invocation_id for invocation in rank_invocations}),
            len(rank_invocations),
        )

    def test_fixed_inputs_produce_deterministic_traces_and_short_circuit_events(self) -> None:
        from advisor.scoring import classify_asset_with_trace

        scored, stats = _characterization_cases()["low_sample"]
        first_decision, first_trace = classify_asset_with_trace(
            scored,
            stats,
            effective_now_utc=FIXED_NOW_UTC,
        )
        second_decision, second_trace = classify_asset_with_trace(
            scored,
            stats,
            effective_now_utc=FIXED_NOW_UTC,
        )
        self.assertEqual(first_decision, second_decision)
        self.assertEqual(first_trace, second_trace)
        sample_invocations = [
            invocation for invocation in first_trace.invocations if invocation.function == "rate_sample_quality"
        ]
        self.assertTrue(sample_invocations)
        sample_events = [
            event for event in first_trace.events if event.invocation_id == sample_invocations[-1].invocation_id
        ]
        self.assertEqual([event.rule_id for event in sample_events], ["classify_asset.sample_quality_low"])
        self.assertTrue(sample_events[0].matched)
        self.assertTrue(sample_events[0].terminated)

    def test_decision_state_deltas_are_lossless_and_noop_is_explicit(self) -> None:
        from advisor.scoring import classify_asset_with_trace

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        _decision, trace = classify_asset_with_trace(scored, stats, effective_now_utc=FIXED_NOW_UTC)
        apply_cap_events = [
            event for event in trace.events if event.rule_id == "classify_asset.apply_cap_rank_choice"
        ]
        self.assertTrue(apply_cap_events)
        state_change = apply_cap_events[-1].state_changes["decision"]
        self.assertEqual(state_change["before"], "tradeable")
        self.assertEqual(state_change["candidate"], "tradeable")
        self.assertEqual(state_change["after"], "tradeable")
        self.assertFalse(state_change["changed"])
        self.assertNotIn("alerts", apply_cap_events[-1].state_changes)

        stale_scored, stale_stats = _characterization_cases()["stale_data"]
        _stale_decision, stale_trace = classify_asset_with_trace(
            stale_scored,
            stale_stats,
            effective_now_utc=FIXED_NOW_UTC,
        )
        stale_events = [event for event in stale_trace.events if event.rule_id == "classify_asset.stale_annotation"]
        self.assertTrue(stale_events)
        self.assertEqual(stale_events[-1].alerts_added, ["stale_price_data"])
        self.assertEqual(stale_events[-1].limitations_added, ["stale_price_data"])

        uncollected_events = [
            event
            for event in trace.events
            if event.rule_id in {
                "classify_asset.uncollected_news_limit",
                "classify_asset.uncollected_sector_limit",
                "classify_asset.missing_ev_components",
            }
        ]
        self.assertTrue(any("news_not_collected_confidence_limited" in event.limitations_added for event in uncollected_events))
        high_scored, high_stats = _characterization_cases()["missing_data_severity_high"]
        _high_decision, high_trace = classify_asset_with_trace(
            high_scored,
            high_stats,
            effective_now_utc=FIXED_NOW_UTC,
        )
        score_events = [event for event in high_trace.events if event.rule_id == "classify_asset.data_score_limited"]
        self.assertTrue(score_events)
        score_change = score_events[-1].state_changes["data_quality_score"]
        self.assertEqual(score_change["before"], 95)
        self.assertEqual(score_change["candidate"], 65)
        self.assertEqual(score_change["after"], 65)
        self.assertTrue(score_change["changed"])

    def test_collector_failure_preserves_confirmed_events_and_marks_later_unknown(self) -> None:
        from advisor.runtime_scoring_observability import ObservationContext
        from advisor.scoring import classify_asset_with_trace

        class FailOnThirdEventCollector:
            def __init__(self) -> None:
                self.calls = 0

            def record_event(self, _event: object) -> None:
                self.calls += 1
                if self.calls == 3:
                    raise RuntimeError("collector failure detail")

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        expected = classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)
        collector = FailOnThirdEventCollector()
        context = ObservationContext(
            enabled=True,
            effective_now_utc=FIXED_NOW_UTC,
            collector=collector,
        )
        observed, trace = classify_asset_with_trace(
            scored,
            stats,
            effective_now_utc=FIXED_NOW_UTC,
            observation_context=context,
        )
        self.assertEqual(expected, observed)
        self.assertEqual(collector.calls, 3)
        self.assertEqual([event.sequence for event in trace.events], [2, 3])
        self.assertEqual(trace.last_reliable_sequence, 3)
        self.assertEqual(trace.observation_failure_sequence, 5)
        self.assertEqual(trace.trace_status, "partial")
        self.assertFalse(trace.coverage_complete)
        self.assertTrue(trace.invocations[0].unknown_rule_ids)
        self.assertNotIn("collector failure detail", str(trace.observation_errors))

    def test_serializer_preserves_float_bits_list_order_and_duplicates(self) -> None:
        from advisor.runtime_scoring_observability import asset_decision_sha256, canonical_asset_decision_bytes

        decision = classify_asset(
            *_characterization_cases()["equity_tradeable_base"],
            effective_now_utc=FIXED_NOW_UTC,
        )
        precise = replace(
            decision,
            alternative_entry=1.2345678901234567,
            reason_codes=["b", "a", "a"],
        )
        reordered = replace(precise, reason_codes=["a", "b", "a"])
        precise_bytes = canonical_asset_decision_bytes(precise)
        self.assertIn(precise.alternative_entry.hex().encode("ascii"), precise_bytes)
        self.assertNotEqual(precise_bytes, canonical_asset_decision_bytes(reordered))
        self.assertEqual(
            asset_decision_sha256(precise),
            hashlib.sha256(precise_bytes).hexdigest(),
        )

    def test_observed_adapter_calls_the_single_private_implementation_once(self) -> None:
        from advisor.scoring import classify_asset_with_trace

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        with patch("advisor.scoring._classify_asset_observed", wraps=__import__("advisor.scoring", fromlist=["_classify_asset_observed"])._classify_asset_observed) as observed_impl:
            classify_asset_with_trace(scored, stats, effective_now_utc=FIXED_NOW_UTC)
        self.assertEqual(observed_impl.call_count, 1)

    def test_production_callers_do_not_call_private_observed_implementation(self) -> None:
        import ast
        from pathlib import Path

        private_callers: list[str] = []
        for path in Path("advisor").glob("*.py"):
            if path.name == "scoring.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_classify_asset_observed"
                for node in ast.walk(tree)
            ):
                private_callers.append(str(path))
        self.assertEqual(private_callers, [])

    def test_real_exception_keeps_original_type_and_marks_raise_termination(self) -> None:
        from advisor.runtime_scoring_observability import ObservationContext
        from advisor.scoring import classify_asset_with_trace

        class ExplodingTheme:
            def __eq__(self, _other: object) -> bool:
                raise RuntimeError("real classifier failure")

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        broken = replace(scored, snapshot=replace(scored.snapshot, theme=ExplodingTheme()))
        context = ObservationContext.create_enabled(FIXED_NOW_UTC)
        with self.assertRaisesRegex(RuntimeError, "real classifier failure"):
            classify_asset_with_trace(
                broken,
                stats,
                effective_now_utc=FIXED_NOW_UTC,
                observation_context=context,
            )
        exception_events = [event for event in context.events if event.termination_kind == "raise"]
        self.assertTrue(exception_events)
        self.assertTrue(any(event.matched is None and not event.evaluated for event in exception_events))
        self.assertEqual(context.observation_errors, [])

    def test_collector_failure_is_fail_open_and_marks_partial_coverage(self) -> None:
        from advisor.runtime_scoring_observability import ObservationContext
        from advisor.scoring import classify_asset_with_trace

        class FailingCollector:
            def record_event(self, *_args: object, **_kwargs: object) -> None:
                raise RuntimeError("collector failure must be sanitized")

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        expected = classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)
        context = ObservationContext(enabled=True, effective_now_utc=FIXED_NOW_UTC, collector=FailingCollector())
        observed, trace = classify_asset_with_trace(
            scored,
            stats,
            effective_now_utc=FIXED_NOW_UTC,
            observation_context=context,
        )
        self.assertEqual(expected, observed)
        self.assertEqual(trace.trace_status, "partial")
        self.assertFalse(trace.coverage_complete)
        self.assertTrue(trace.observation_errors)
        self.assertIsNotNone(trace.last_reliable_sequence)
        self.assertNotIn("collector failure must be sanitized", str(trace.observation_errors))

    def test_real_classification_exception_is_not_converted_to_observation_error(self) -> None:
        from advisor.scoring import classify_asset_with_trace

        class ExplodingTheme:
            def __eq__(self, _other: object) -> bool:
                raise RuntimeError("real classifier failure")

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        broken = replace(scored, snapshot=replace(scored.snapshot, theme=ExplodingTheme()))
        with self.assertRaisesRegex(RuntimeError, "real classifier failure"):
            classify_asset(broken, stats, effective_now_utc=FIXED_NOW_UTC)
        with self.assertRaisesRegex(RuntimeError, "real classifier failure"):
            classify_asset_with_trace(broken, stats, effective_now_utc=FIXED_NOW_UTC)

    def test_rule_catalog_is_exactly_the_approved_97_and_static_ids_match_source(self) -> None:
        from pathlib import Path

        from advisor.runtime_scoring_observability import RULE_CATALOG, _source_rule_records, validate_rule_catalog

        snapshot_path = Path("tests/fixtures/runtime_scoring_rule_catalog_expected.json")
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        ids = [rule.rule_id for rule in RULE_CATALOG]
        self.assertEqual(len(ids), 97)
        self.assertEqual(len(ids), len(set(ids)))
        report = validate_rule_catalog(Path("."), expected_snapshot=snapshot_path)
        self.assertEqual(report, {
            "missing_ids": [],
            "extra_ids": [],
            "metadata_mismatches": [],
            "invalid_locators": [],
            "signature_mismatches": [],
            "ownership_mismatches": [],
        })
        source_records = _source_rule_records(Path("."))
        expected_records = {entry["rule_id"]: entry for entry in snapshot["rules"]}
        catalog = {rule.rule_id: rule for rule in RULE_CATALOG}
        self.assertEqual(set(ids), set(expected_records))
        for rule_id, rule in catalog.items():
            source = source_records[rule_id]
            expected = expected_records[rule_id]
            self.assertEqual(rule.function, expected["function"])
            self.assertEqual(rule.source_code_locator, expected["source_code_locator"])
            self.assertEqual(rule.source_code_locator, {
                "path": source["path"],
                "function": source["function"],
                "line_start": source["line_start"],
                "line_end": source["line_end"],
            })
            self.assertEqual(rule.branch_signature, expected["canonical_branch_signature"])
            self.assertEqual(rule.branch_signature, source["canonical_branch_signature"])
            self.assertEqual(rule.branch_kind, expected["branch_kind"])
            self.assertEqual(rule.axis, expected["axis"])
            self.assertEqual(rule.effect_type, expected["effect_type"])
            self.assertEqual(list(rule.evidence_keys), expected["evidence_keys"])
        self.assertEqual(catalog["classify_asset.confidence_below_65"].axis, "decision")
        self.assertEqual(catalog["classify_asset.confidence_below_65"].effect_type, "cap")
        self.assertEqual(catalog["classify_asset.confidence_below_65"].evidence_keys, ("decision_confidence.score",))
        self.assertEqual(catalog["classify_asset.backtest_branch_entry"].effect_type, "control_flow")
        self.assertEqual(catalog["classify_asset.earnings_imminent_wait"].effect_type, "adjustment")
        self.assertEqual(catalog["classify_asset.stale_annotation"].evidence_keys, ("technical.price_stale",))
        banned = ("penalty.", "gate.", "duplicate.", "shadowed.", "bad.", "technical.technical_unvalidated")
        for rule in RULE_CATALOG:
            for key in rule.evidence_keys:
                self.assertFalse(key.startswith(banned), (rule.rule_id, key))

    def test_catalog_metadata_and_invocation_ownership_are_consistent(self) -> None:
        from pathlib import Path

        from advisor.runtime_scoring_observability import RULE_CATALOG, validate_runtime_trace
        from advisor.scoring import classify_asset_with_trace

        expected_owners = {
            "classify_asset.last_price_timestamp_fallback": "_freshness_context",
            "classify_asset.freshness_stale_reason_choice": "_freshness_context",
            "classify_asset.technical_ev_presence": "_is_technical_unvalidated",
            "classify_asset.confidence_sample_size_presence": "_decision_confidence_score",
        }
        catalog = {rule.rule_id: rule for rule in RULE_CATALOG}
        self.assertEqual(len(catalog), 97)
        for rule in RULE_CATALOG:
            locator = rule.source_code_locator
            self.assertEqual(set(locator), {"path", "function", "line_start", "line_end"})
            self.assertTrue(rule.branch_signature)
        for rule_id, function in expected_owners.items():
            self.assertEqual(catalog[rule_id].function, function)

        for name, (scored, stats) in _characterization_cases().items():
            with self.subTest(fixture=name):
                _decision, trace = classify_asset_with_trace(
                    scored,
                    stats,
                    effective_now_utc=FIXED_NOW_UTC,
                )
                validate_runtime_trace(trace)
                invocations = {invocation.invocation_id: invocation for invocation in trace.invocations}
                for event in trace.events:
                    invocation = invocations[event.invocation_id]
                    self.assertEqual(catalog[event.rule_id].function, invocation.function)
                    self.assertIn(event.rule_id, invocation.catalog_rule_ids)
                for invocation in trace.invocations:
                    self.assertEqual(
                        set(invocation.reached_rule_ids) | set(invocation.known_unreached_rule_ids),
                        set(invocation.catalog_rule_ids),
                    )
                    self.assertEqual(
                        set(invocation.reached_rule_ids) & set(invocation.known_unreached_rule_ids),
                        set(),
                    )

    def test_seventeen_fixture_traces_have_zero_cross_invocation_contradictions(self) -> None:
        from advisor.runtime_scoring_observability import RULE_CATALOG, validate_runtime_trace
        from advisor.scoring import classify_asset_with_trace

        catalog = {rule.rule_id: rule for rule in RULE_CATALOG}
        contradictions: list[tuple[str, str, str]] = []
        for fixture_name, (scored, stats) in _characterization_cases().items():
            _decision, trace = classify_asset_with_trace(
                scored,
                stats,
                effective_now_utc=FIXED_NOW_UTC,
            )
            validate_runtime_trace(trace)
            reached = {
                rule_id
                for invocation in trace.invocations
                for rule_id in invocation.reached_rule_ids
            }
            for invocation in trace.invocations:
                for rule_id in invocation.known_unreached_rule_ids:
                    if rule_id in reached and rule_id not in invocation.catalog_rule_ids:
                        contradictions.append((fixture_name, invocation.invocation_id, rule_id))
                    self.assertEqual(catalog[rule_id].function, invocation.function)
        self.assertEqual(contradictions, [])

    def test_observation_operations_fail_open_without_changing_decision(self) -> None:
        from advisor.runtime_scoring_observability import ObservationContext, asset_decision_sha256, canonical_asset_decision_bytes
        from advisor.scoring import classify_asset_with_trace

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        legacy = classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)

        class FailingCollector:
            def record_event(self, *_args: object, **_kwargs: object) -> None:
                raise RuntimeError("collector detail must stay sanitized")

        class FailingContext(ObservationContext):
            def __init__(self, operation: str) -> None:
                collector = FailingCollector() if operation == "record_event" else None
                super().__init__(enabled=True, effective_now_utc=FIXED_NOW_UTC, collector=collector)
                self.operation = operation

            def _fail(self, operation: str) -> None:
                if self.operation == operation:
                    raise RuntimeError(f"{operation} detail must stay sanitized")

            def start_trace(self, **kwargs: object) -> None:
                self._fail("start_trace")
                return super().start_trace(**kwargs)

            def capture_initial_state(self, state: dict[str, object]) -> None:
                self._fail("capture_initial_state")
                return super().capture_initial_state(state)

            def begin_invocation(self, function: str):
                self._fail("begin_invocation")
                return super().begin_invocation(function)

            def observe_condition(self, *args: object, **kwargs: object):
                self._fail("observe_condition")
                return super().observe_condition(*args, **kwargs)

            def observe_value(self, *args: object, **kwargs: object):
                self._fail("observe_value")
                return super().observe_value(*args, **kwargs)

            def end_invocation(self, *args: object, **kwargs: object) -> None:
                self._fail("end_invocation")
                return super().end_invocation(*args, **kwargs)

            def capture_final_state(self, state: dict[str, object]) -> None:
                self._fail("capture_final_state")
                return super().capture_final_state(state)

            def build_trace(self, **kwargs: object):
                self._fail("build_trace")
                return super().build_trace(**kwargs)

        operations = (
            "start_trace",
            "capture_initial_state",
            "begin_invocation",
            "observe_condition",
            "observe_value",
            "record_event",
            "end_invocation",
            "capture_final_state",
            "build_trace",
        )
        for operation in operations:
            with self.subTest(operation=operation):
                observed, trace = classify_asset_with_trace(
                    scored,
                    stats,
                    effective_now_utc=FIXED_NOW_UTC,
                    observation_context=FailingContext(operation),
                )
                self.assertEqual(legacy, observed)
                self.assertEqual(canonical_asset_decision_bytes(legacy), canonical_asset_decision_bytes(observed))
                self.assertEqual(asset_decision_sha256(legacy), asset_decision_sha256(observed))
                self.assertIn(trace.trace_status, {"partial", "failed"})
                self.assertFalse(trace.coverage_complete)
                self.assertTrue(any(error.operation == operation for error in trace.observation_errors))
                self.assertNotIn("detail must stay sanitized", str(trace.observation_errors))

    def test_disabled_classification_does_not_call_observation_operations(self) -> None:
        from advisor.runtime_scoring_observability import ObservationContext

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        operations = (
            "start_trace",
            "capture_initial_state",
            "begin_invocation",
            "observe_condition",
            "observe_value",
            "update_classification_inputs",
            "end_invocation",
            "capture_final_state",
            "build_trace",
        )
        patches = [
            patch.object(
                ObservationContext,
                operation,
                side_effect=AssertionError(f"disabled operation called: {operation}"),
                create=True,
            )
            for operation in operations
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            decision = classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)
        self.assertEqual(decision, classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC))

    def test_disabled_matrix_allocates_no_observability_objects_or_payloads(self) -> None:
        import advisor.runtime_scoring_observability as runtime
        import advisor.scoring as scoring

        counts = {
            "_observe": 0,
            "_observe_value": 0,
            "_observe_elif": 0,
            "_observed_if": 0,
            "_finish_token": 0,
            "_finish_pending_observation": 0,
            "_finish_state_change": 0,
            "_finish_decision_change": 0,
            "_ObservationToken.__init__": 0,
            "_state_change": 0,
            "_decision_change": 0,
            "_classifier_inputs": 0,
            "ObservationContext.__init__": 0,
            "RuntimeTrace.__init__": 0,
            "InvocationTrace.__init__": 0,
            "RuntimeEvent.__init__": 0,
            "canonical_asset_decision_bytes": 0,
            "asset_decision_sha256": 0,
        }

        def spy(name, original):
            def wrapped(*args, **kwargs):
                counts[name] += 1
                return original(*args, **kwargs)

            return wrapped

        with ExitStack() as stack:
            stack.enter_context(patch.object(scoring, "_observe", spy("_observe", scoring._observe)))
            stack.enter_context(patch.object(scoring, "_observe_value", spy("_observe_value", scoring._observe_value)))
            for name in (
                "_observe_elif",
                "_observed_if",
                "_finish_token",
                "_finish_pending_observation",
                "_finish_state_change",
                "_finish_decision_change",
            ):
                stack.enter_context(patch.object(scoring, name, spy(name, getattr(scoring, name))))
            stack.enter_context(
                patch.object(
                    runtime._ObservationToken,
                    "__init__",
                    spy("_ObservationToken.__init__", runtime._ObservationToken.__init__),
                )
            )
            for name in ("_state_change", "_decision_change", "_classifier_inputs"):
                stack.enter_context(patch.object(scoring, name, spy(name, getattr(scoring, name))))
            stack.enter_context(
                patch.object(
                    runtime.ObservationContext,
                    "__init__",
                    spy("ObservationContext.__init__", runtime.ObservationContext.__init__),
                )
            )
            for class_name in ("RuntimeTrace", "InvocationTrace", "RuntimeEvent"):
                cls = getattr(runtime, class_name)
                stack.enter_context(patch.object(cls, "__init__", spy(f"{class_name}.__init__", cls.__init__)))
            for name in ("canonical_asset_decision_bytes", "asset_decision_sha256"):
                stack.enter_context(
                    patch.object(
                        runtime,
                        name,
                        spy(name, getattr(runtime, name)),
                    )
                )
            decisions = {
                name: classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)
                for name, (scored, stats) in _characterization_cases().items()
            }

        self.assertEqual(len(decisions), 17)
        self.assertTrue(all(value == 0 for value in counts.values()), counts)

    def test_disabled_call_tree_allows_only_the_actual_noop_decorator_wrapper(self) -> None:
        approved_wrapper_code = _approved_observability_wrapper_code()
        decisions, call_records = _profile_legacy_classifications(_characterization_cases())
        report = _runtime_call_report(call_records, approved_wrapper_code)

        _assert_only_approved_runtime_code(report)
        self.assertEqual(len(decisions), 17)
        self.assertEqual(report["unapproved_runtime_functions"], set())

    def test_disabled_call_tree_detects_injected_runtime_function_by_module_and_code_object(self) -> None:
        import advisor.runtime_scoring_observability as runtime
        import advisor.scoring as scoring

        approved_wrapper_code = _approved_observability_wrapper_code()
        cases = {"equity_tradeable_base": _characterization_cases()["equity_tradeable_base"]}
        expected = classify_asset(*cases["equity_tradeable_base"], effective_now_utc=FIXED_NOW_UTC)

        for probe_name in ("injected_observability_probe", "renamed_observability_boundary_probe"):
            with self.subTest(probe_name=probe_name):
                exec(f"def {probe_name}():\n    return None\n", runtime.__dict__)
                probe = runtime.__dict__[probe_name]
                original_helper = scoring._freshness_context

                def mutated_helper(*args: object, **kwargs: object):
                    probe()
                    return original_helper(*args, **kwargs)

                try:
                    with patch.object(scoring, "_freshness_context", mutated_helper):
                        decisions, call_records = _profile_legacy_classifications(cases)
                    report = _runtime_call_report(call_records, approved_wrapper_code)

                    self.assertEqual(decisions["equity_tradeable_base"], expected)
                    self.assertIn(probe.__code__, report["runtime_code_objects"])
                    with self.assertRaises(AssertionError):
                        _assert_only_approved_runtime_code(report)
                finally:
                    del runtime.__dict__[probe_name]

        restored_decisions, restored_records = _profile_legacy_classifications(_characterization_cases())
        restored_report = _runtime_call_report(restored_records, approved_wrapper_code)
        _assert_only_approved_runtime_code(restored_report)
        self.assertEqual(len(restored_decisions), 17)

    def test_disabled_path_never_constructs_token_even_if_constructor_raises(self) -> None:
        import advisor.runtime_scoring_observability as runtime

        scored, stats = _characterization_cases()["equity_tradeable_base"]

        def fail_constructor(*_args, **_kwargs):
            raise AssertionError("disabled path constructed observation token")

        with patch.object(runtime._ObservationToken, "__init__", side_effect=fail_constructor):
            classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)
            with self.assertRaisesRegex(AssertionError, "disabled path constructed observation token"):
                from advisor.scoring import classify_asset_with_trace

                classify_asset_with_trace(scored, stats, effective_now_utc=FIXED_NOW_UTC)

    def test_disabled_path_never_reaches_observational_wrappers(self) -> None:
        import advisor.scoring as scoring
        from advisor.scoring import classify_asset_with_trace

        cases = tuple(_characterization_cases().values())
        for helper_name in ("_observe_elif", "_observed_if", "_finish_token", "_finish_pending_observation"):
            with self.subTest(helper=helper_name):
                with patch.object(
                    scoring,
                    helper_name,
                    side_effect=AssertionError("observability helper reached in disabled path"),
                ):
                    for scored, stats in cases:
                        classify_asset(scored, stats, effective_now_utc=FIXED_NOW_UTC)

        scored, stats = _characterization_cases()["equity_tradeable_base"]
        decision, trace = classify_asset_with_trace(
            scored,
            stats,
            effective_now_utc=FIXED_NOW_UTC,
        )
        self.assertIsNotNone(decision)
        self.assertGreater(len(trace.events), 0)

    def test_disabled_observation_adapters_do_not_allocate_or_evaluate(self) -> None:
        import advisor.runtime_scoring_observability as runtime

        context = runtime.ObservationContext(enabled=False, effective_now_utc=FIXED_NOW_UTC)

        def fail_evaluator():
            raise AssertionError("disabled observation evaluator executed")

        with patch.object(
            runtime._ObservationToken,
            "__init__",
            side_effect=AssertionError("disabled observation token constructed"),
        ):
            self.assertIsNone(runtime.observe_condition(context, None, "disabled", fail_evaluator))
            self.assertIsNone(runtime.observe_value(context, None, "disabled", fail_evaluator))
            self.assertIsNone(runtime.observe_condition(None, None, "disabled", fail_evaluator))
            self.assertIsNone(runtime.observe_value(None, None, "disabled", fail_evaluator))

    def test_observation_call_sites_are_guarded_by_context_presence(self) -> None:
        source = Path("advisor/scoring.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

        def is_context_guard(test):
            if isinstance(test, ast.BoolOp):
                return any(is_context_guard(value) for value in test.values)
            return (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id in {"context", "observation_context"}
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.IsNot)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value is None
            )

        unguarded = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {
                "_observe",
                "_observe_value",
                "_observe_elif",
                "_observed_if",
                "_finish_token",
                "_finish_pending_observation",
                "_finish_state_change",
                "_finish_decision_change",
            }:
                continue
            if node.func.id in {"_observe_elif", "_observed_if"}:
                unguarded.append((node.func.id, node.lineno, "call site must be removed"))
                continue
            current = node
            guarded = False
            while current in parents:
                parent = parents[current]
                if isinstance(parent, ast.If) and is_context_guard(parent.test) and current in parent.body:
                    guarded = True
                    break
                current = parent
            if not guarded:
                unguarded.append((node.func.id, node.lineno))
        self.assertEqual(unguarded, [])

    def test_observation_call_sites_receive_facts_not_decision_callbacks(self) -> None:
        source = Path("advisor/scoring.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        callback_keywords = {"evaluator", "true_callback", "false_callback", "when_true", "when_false"}
        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {"_observe", "_observe_value"}:
                continue
            if len(node.args) >= 3 and isinstance(node.args[2], ast.Lambda):
                violations.append((node.func.id, node.lineno, "positional callback"))
            if any(keyword.arg in callback_keywords for keyword in node.keywords):
                violations.append((node.func.id, node.lineno, "decision callback keyword"))
            keyword_names = {keyword.arg for keyword in node.keywords}
            expected = "matched" if node.func.id == "_observe" else "value"
            if expected not in keyword_names:
                violations.append((node.func.id, node.lineno, f"missing {expected} fact"))
        self.assertEqual(violations, [])

    def test_lexical_branch_kind_distinguishes_if_elif_else_nested_if_and_ifexp(self) -> None:
        import advisor.runtime_scoring_observability as runtime

        source = """if first:\n    value = 1\nelif (\n    condition_a\n    and condition_b\n):\n    value = 2\nelse:\n    if nested:\n        value = 3\nvalue = left if condition else right\n"""
        tree = ast.parse(source)
        kinds = runtime.branch_kinds_from_source(source)
        if_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.If)]
        ifexp_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.IfExp)]
        self.assertEqual([kinds[(node.lineno, node.col_offset)] for node in sorted(if_nodes, key=lambda item: item.lineno)], ["if", "elif", "if"])
        self.assertEqual(kinds[(ifexp_nodes[0].lineno, ifexp_nodes[0].col_offset)], "ifexp")

    def test_source_branch_kind_counts_and_exact_elif_ids_match_snapshot(self) -> None:
        import advisor.runtime_scoring_observability as runtime

        snapshot_path = Path("tests/fixtures/runtime_scoring_rule_catalog_expected.json")
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        records = runtime._source_rule_records(Path("."))
        from collections import Counter

        self.assertEqual(Counter(record["branch_kind"] for record in records.values()), Counter({"if": 74, "elif": 8, "ifexp": 15}))
        self.assertEqual(
            sorted(rule_id for rule_id, record in records.items() if record["branch_kind"] == "elif"),
            sorted(entry["rule_id"] for entry in snapshot["rules"] if entry["branch_kind"] == "elif"),
        )

    def test_branch_kind_mutations_are_rejected_for_all_kinds(self) -> None:
        import dataclasses
        import advisor.runtime_scoring_observability as runtime

        snapshot_path = Path("tests/fixtures/runtime_scoring_rule_catalog_expected.json")
        for rule in runtime.RULE_CATALOG:
            mutations = {
                "if": "elif",
                "elif": "if",
                "ifexp": "if",
            }
            with self.subTest(rule=rule.rule_id):
                with patch.object(
                    runtime,
                    "RULE_CATALOG",
                    tuple(
                        dataclasses.replace(item, branch_kind=mutations[item.branch_kind])
                        if item.rule_id == rule.rule_id
                        else item
                        for item in runtime.RULE_CATALOG
                    ),
                ):
                    with self.assertRaises(AssertionError):
                        runtime.validate_rule_catalog(Path("."), expected_snapshot=snapshot_path)


if __name__ == "__main__":
    unittest.main()
