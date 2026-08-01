from __future__ import annotations

import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from advisor.models import AssetDecision, RiskPlan
from advisor.runtime_scoring_observability import (
    RULE_CATALOG,
    InvocationTrace,
    ObservationError,
    RuntimeEvent,
    RuntimeTrace,
    asset_decision_sha256,
)
from advisor.runtime_scoring_artifact import (
    ArtifactAssetInput,
    ArtifactValidationError,
    canonical_json_bytes,
    SCHEMA_1_ENUM_DOMAINS,
    serialize_runtime_trace_deterministic,
    trace_sha256,
    validate_runtime_scoring_artifact,
    write_runtime_scoring_artifact,
)


FIXED_NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
RUN_METADATA = {
    "run_id": "phase3a3-2-test",
    "report_date": "2026-07-29",
    "schedule": "main",
    "source_sha": "25b11df3f2a4d3dd8cb85cd45682e43811e35884",
    "runtime_sha": None,
    "timezone": "America/Sao_Paulo",
}


# Independent schema authority used only by the semantic-enum tests.  These
# values intentionally do not import or derive from the production validator.
INDEPENDENT_ENUM_DOMAINS = {
    "artifact_status": {"complete", "partial", "failed"},
    "trace_status": {"complete", "partial", "failed"},
    "asset_type": {"stock", "crypto"},
    "decision": {"tradeable", "watch_buy", "technical_unvalidated", "wait", "avoid", "blocked"},
    "collector_state": {"idle", "disabled", "active", "failed"},
    "termination_kind": {"return", "raise"},
    "branch_kind": {"if", "elif", "ifexp"},
    "serialization_status": {"complete", "error"},
    "classification_status": {"completed", "failed", "unavailable"},
    "decision_status": {"available", "unavailable"},
    "schedule": {"main", "close", "nightly"},
    "coverage_status": {"active", "partial", "complete"},
    "axis": {"decision", "confidence", "quality", "risk", "other"},
    "effect_type": {"adjustment", "annotation", "base", "cap", "control_flow", "override"},
    "runtime_sha_status": {"available", "unavailable"},
    "schema_validation_status": {"valid"},
    "error_code": {
        "artifact_error",
        "asset_decision_serialization_error",
        "asset_decision_unavailable",
        "collector_error",
        "no_assets",
        "observation_operation_failed",
        "serialization_error",
        "single_asset_hard_budget_exceeded",
        "soft_budget_exceeded",
        "trace_serialization_error",
    },
}


# Independent path inventory for values that use the decision enum inside a
# deterministic trace.  The paths are intentionally written here instead of
# being derived from the production validator so that removing one validator
# branch makes this matrix fail.
INDEPENDENT_DECISION_PATHS = (
    ("trace.initial_state.decision", ("initial_state", "decision")),
    ("trace.initial_state.max_decision", ("initial_state", "max_decision")),
    ("trace.final_state.decision", ("final_state", "decision")),
    ("trace.final_state.max_decision", ("final_state", "max_decision")),
    ("trace.classification.final_decision", ("classification", "final_decision")),
    (
        "trace.events[0].state_changes.decision.before",
        ("events", 0, "state_changes", "decision", "before"),
    ),
    (
        "trace.events[0].state_changes.decision.candidate",
        ("events", 0, "state_changes", "decision", "candidate"),
    ),
    (
        "trace.events[0].state_changes.decision.after",
        ("events", 0, "state_changes", "decision", "after"),
    ),
)


def _decision(
    symbol: str,
    *,
    asset_type: str = "stock",
    universe_origin: str = "configured",
    reason_codes: list[str] | None = None,
    alternative_entry: float | None = 99.12345678901235,
) -> AssetDecision:
    return AssetDecision(
        symbol=symbol,
        asset_type=asset_type,
        decision="tradeable",
        investment_quality_score=71.125,
        swing_trade_score=73.375,
        risk_plan=RiskPlan(
            entry=100.125,
            stop=96.5,
            target_2r=107.375,
            target_3r=111.0,
            per_unit_risk=3.625,
            risk_amount=250.0,
            risk_fraction=0.005,
            max_position_units=68.0,
            max_position_value=6808.5,
            risk_reward_2r="2.00:1",
            alerts=["risk", "risk"],
            position_size_display="68",
        ),
        alerts=["market_not_risk_on", "market_not_risk_on"],
        limitations=["macro_limited"],
        thesis="allowlisted thesis",
        metrics_summary=["metric-b", "metric-a", "metric-a"],
        ideal_entry=100.125,
        alternative_entry=alternative_entry,
        hold_suggestion="1-8 semanas",
        backtest_stats=None,
        sample_quality="high",
        reason_codes=reason_codes or ["b", "a", "a"],
        data_quality="complete",
        missing_data_severity="low",
        data_source="fixture",
        data_timestamp="2026-07-29T15:00:00Z",
        bucket="tradeable",
        market_session="open",
        provider="fixture",
        event_check_status="complete",
        news_status="complete",
        macro_regime="risk_on",
        macro_status="complete",
        thesis_status="complete",
        data_quality_score=95,
        decision_confidence_score=80,
        universe_origin=universe_origin,
    )


def _trace(
    symbol: str,
    *,
    status: str = "complete",
    matched: bool = False,
    repeated_helper: bool = False,
    invocation_without_events: bool = False,
    operational_offset_seconds: int = 0,
    local_path: str = "reports/runtime/local.tmp",
) -> RuntimeTrace:
    rule = RULE_CATALOG[0]
    invocation = InvocationTrace(
        invocation_id=f"{rule.function}#1",
        function=rule.function,
        parent_invocation_id=None,
        call_ordinal=1,
        started_sequence=1,
        completed_sequence=3,
        termination_kind="return",
        coverage_status="complete" if status == "complete" else "partial",
        interval_complete=True,
        coverage_complete=status == "complete",
        invocation_coverage_complete=status == "complete",
        last_reliable_sequence=2,
        catalog_rule_ids=[rule.rule_id],
        reached_rule_ids=[rule.rule_id],
        known_unreached_rule_ids=[],
        unreached_rule_ids=[],
        unknown_rule_ids=[] if status == "complete" else [],
    )
    event = RuntimeEvent(
        sequence=2,
        invocation_id=invocation.invocation_id,
        rule_id=rule.rule_id,
        reached=True,
        evaluated=True,
        matched=matched,
        terminated=False,
        termination_kind=None,
        axis=rule.axis,
        effect_type=rule.effect_type,
        evidence_keys=list(rule.evidence_keys),
        condition_inputs={
            "precise": 1.2345678901234567,
            "ordered": ["b", "a", "a"],
            "symbol": symbol,
        },
        state_changes={},
        reason_codes_added=[],
        alerts_added=[],
        limitations_added=[],
        branch_label=f"{rule.function}.fixture",
    )
    invocations = [invocation]
    events = [] if invocation_without_events else [event]
    if invocation_without_events:
        invocation.reached_rule_ids = []
        invocation.known_unreached_rule_ids = [rule.rule_id] if status == "complete" else []
        invocation.unreached_rule_ids = [rule.rule_id] if status == "complete" else []
        invocation.unknown_rule_ids = [] if status == "complete" else [rule.rule_id]
    if repeated_helper:
        invocations.append(
            replace(
                invocation,
                invocation_id=f"{rule.function}#2",
                call_ordinal=2,
                started_sequence=4,
                completed_sequence=6,
                last_reliable_sequence=5,
            )
        )
        events.append(replace(event, sequence=5, invocation_id=f"{rule.function}#2"))
    errors = []
    if status != "complete":
        errors = [
            ObservationError(
                error_type="trace_collection_error",
                operation="record_event",
                error_code="collector_error",
                exception_type="RuntimeError",
                sequence=4,
                invocation_id=invocation.invocation_id,
            )
        ]
    trace = RuntimeTrace(
        classification_inputs={
            "effective_now_utc": FIXED_NOW,
            "symbol": symbol,
            "precise": 1.2345678901234567,
            "ordered": ["b", "a", "a"],
        },
        initial_state={"decision": "tradeable", "score": 71.125},
        final_state={"decision": "tradeable", "score": 71.125},
        events=events,
        invocations=invocations,
        effective_now_utc=FIXED_NOW,
        trace_status=status,
        observer_enabled=True,
        coverage_complete=status == "complete",
        last_reliable_sequence=max((item.sequence for item in events), default=0),
        observation_failure_sequence=4 if status != "complete" else None,
        observation_errors=errors,
        active_invocation_id=None,
        collector_state="active" if status == "complete" else "failed",
    )
    trace.runtime_metadata = {
        "trace_started_at": FIXED_NOW + timedelta(seconds=operational_offset_seconds),
        "trace_completed_at": FIXED_NOW + timedelta(seconds=operational_offset_seconds, milliseconds=5),
        "duration": 0.005 + operational_offset_seconds,
        "local_path": local_path,
    }
    return trace


def _asset(symbol: str, **kwargs: object) -> ArtifactAssetInput:
    decision_kwargs = {
        key: kwargs.pop(key)
        for key in list(kwargs)
        if key in {"asset_type", "universe_origin", "reason_codes", "alternative_entry"}
    }
    trace = _trace(symbol, **kwargs)
    return ArtifactAssetInput(
        decision=_decision(symbol, **decision_kwargs),
        trace=trace,
        runtime_metadata=trace.runtime_metadata,
    )


def _read_json(path: Path) -> dict[str, object]:
    with gzip.open(path, "rb") as source:
        return json.loads(source.read())


def _write_canonical_gzip(path: Path, payload: dict[str, object]) -> None:
    path.write_bytes(gzip.compress(canonical_json_bytes(payload), compresslevel=9, mtime=0))


def _recompute_payload_hash(payload: dict[str, object]) -> None:
    integrity = payload["integrity"]
    basis = copy.deepcopy(payload)
    basis["integrity"].pop("artifact_payload_hash", None)
    integrity["artifact_payload_hash"] = hashlib.sha256(canonical_json_bytes(basis)).hexdigest()


def _recompute_trace_hashes(payload: dict[str, object]) -> None:
    trace_domains = []
    for asset in payload.get("assets", []):
        if asset.get("trace") is not None:
            trace_basis = copy.deepcopy(asset["trace"])
            trace_basis.pop("trace_id", None)
            asset["trace"]["trace_id"] = "sha256:" + hashlib.sha256(
                canonical_json_bytes(trace_basis)
            ).hexdigest()
            asset["trace_hash"] = hashlib.sha256(canonical_json_bytes(asset["trace"])).hexdigest()
        trace_domains.append(
            {
                "symbol": asset.get("symbol"),
                "asset_type": asset.get("asset_type"),
                "universe_origin": asset.get("universe_origin"),
                "trace_hash": asset.get("trace_hash"),
            }
        )
    trace_domains.sort(key=lambda item: (item["symbol"], item["asset_type"], item["universe_origin"]))
    payload["integrity"]["trace_hash"] = hashlib.sha256(canonical_json_bytes(trace_domains)).hexdigest()


def _recompute_decision_trace_and_payload_hashes(payload: dict[str, object]) -> None:
    for asset in payload.get("assets", []):
        decision = asset.get("serialized_asset_decision")
        if not isinstance(decision, dict):
            continue
        decision_bytes = canonical_json_bytes(decision).removesuffix(b"\n")
        decision_hash = hashlib.sha256(decision_bytes).hexdigest()
        asset["serialized_asset_decision_hash"] = decision_hash
        trace = asset.get("trace")
        if isinstance(trace, dict) and isinstance(trace.get("classification"), dict):
            trace["classification"]["serialized_asset_decision_hash"] = decision_hash
            trace["classification"]["final_decision"] = decision["decision"]
    _recompute_trace_hashes(payload)
    decision_counts: dict[str, int] = {}
    for asset in payload.get("assets", []):
        decision = asset.get("serialized_asset_decision")
        if isinstance(decision, dict):
            value = str(decision.get("decision"))
            decision_counts[value] = decision_counts.get(value, 0) + 1
    payload["integrity"]["decision_counts"] = dict(sorted(decision_counts.items()))
    _recompute_payload_hash(payload)


def _recompute_catalog_trace_and_payload_hashes(payload: dict[str, object]) -> None:
    catalog_hash = hashlib.sha256(canonical_json_bytes(payload["rule_catalog"])).hexdigest()
    payload["rule_catalog_hash"] = catalog_hash
    for asset in payload.get("assets", []):
        trace = asset.get("trace")
        if isinstance(trace, dict):
            trace["rule_catalog_hash"] = catalog_hash
    _recompute_decision_trace_and_payload_hashes(payload)


def _set_trace_path(payload: dict[str, object], path: tuple[object, ...], value: object) -> None:
    cursor: object = payload["assets"][0]["trace"]
    for component in path[:-1]:
        cursor = cursor[component]  # type: ignore[index]
    cursor[path[-1]] = value  # type: ignore[index]


def _duplicate_first_json_key(raw: bytes, key: str) -> bytes:
    text = raw.decode("utf-8")
    marker = f'"{key}":'
    start = text.index(marker)
    value_start = start + len(marker)
    decoder = json.JSONDecoder()
    while value_start < len(text) and text[value_start].isspace():
        value_start += 1
    _value, value_end = decoder.raw_decode(text, value_start)
    pair = text[start:value_end]
    return (text[:value_end] + "," + pair + text[value_end:]).encode("utf-8")


def _write_raw_gzip(path: Path, raw: bytes) -> None:
    path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))


class RuntimeScoringArtifactDeterminismTests(unittest.TestCase):
    def test_same_logical_content_produces_identical_json_hash_and_gzip(self) -> None:
        assets = [_asset("BBB"), _asset("AAA")]
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = write_runtime_scoring_artifact(
                Path(first_dir), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG, assets=assets
            )
            second = write_runtime_scoring_artifact(
                Path(second_dir), run_metadata=dict(reversed(list(RUN_METADATA.items()))),
                rule_catalog=tuple(reversed(RULE_CATALOG)), assets=list(reversed(assets))
            )
            self.assertEqual(first.canonical_json_bytes, second.canonical_json_bytes)
            self.assertEqual(first.artifact_payload_hash, second.artifact_payload_hash)
            self.assertEqual(first.output_paths[0].read_bytes(), second.output_paths[0].read_bytes())
            self.assertEqual(first.artifact_file_hash, second.artifact_file_hash)

    def test_operational_metadata_does_not_change_trace_hash(self) -> None:
        first = _trace("AAA", operational_offset_seconds=0, local_path="reports/runtime/a.tmp")
        second = _trace("AAA", operational_offset_seconds=90, local_path="C:/Users/private/a.tmp")
        self.assertEqual(trace_sha256(first, decision=_decision("AAA"), run_metadata=RUN_METADATA),
                         trace_sha256(second, decision=_decision("AAA"), run_metadata=RUN_METADATA))
        self.assertEqual(
            serialize_runtime_trace_deterministic(first, decision=_decision("AAA"), run_metadata=RUN_METADATA),
            serialize_runtime_trace_deterministic(second, decision=_decision("AAA"), run_metadata=RUN_METADATA),
        )

    def test_decision_and_trace_hashes_are_separate_domains(self) -> None:
        decision = _decision("AAA")
        trace_hash = trace_sha256(_trace("AAA"), decision=decision, run_metadata=RUN_METADATA)
        self.assertNotEqual(asset_decision_sha256(decision), trace_hash)

    def test_asset_input_order_does_not_change_canonical_asset_order(self) -> None:
        permutations = [
            [_asset("ZZZ"), _asset("AAA", asset_type="crypto"), _asset("AAA")],
            [_asset("AAA"), _asset("ZZZ"), _asset("AAA", asset_type="crypto")],
            [_asset("AAA", asset_type="crypto"), _asset("AAA"), _asset("ZZZ")],
        ]
        canonical = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, assets in enumerate(permutations):
                result = write_runtime_scoring_artifact(
                    root / str(index), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG, assets=assets
                )
                canonical.append(result.canonical_json_bytes)
                payload = _read_json(result.output_paths[0])
                self.assertEqual(
                    [(item["symbol"], item["asset_type"]) for item in payload["assets"]],
                    [("AAA", "crypto"), ("AAA", "stock"), ("ZZZ", "stock")],
                )
        self.assertEqual(canonical[0], canonical[1])
        self.assertEqual(canonical[1], canonical[2])

    def test_serializer_preserves_float_bits_list_order_duplicates_and_false_event(self) -> None:
        trace = _trace("AAA", matched=False)
        payload = serialize_runtime_trace_deterministic(trace, decision=_decision("AAA"), run_metadata=RUN_METADATA)
        encoded = canonical_json_bytes(payload)
        self.assertIn((1.2345678901234567).hex().encode("ascii"), encoded)
        self.assertEqual(payload["classification_inputs"]["ordered"], ["b", "a", "a"])
        self.assertEqual(len(payload["events"]), 1)
        self.assertIs(payload["events"][0]["matched"], False)

        reordered = copy.deepcopy(trace)
        reordered.classification_inputs["ordered"] = ["a", "b", "a"]
        rounded = copy.deepcopy(trace)
        rounded.classification_inputs["precise"] = 1.23
        no_duplicate = copy.deepcopy(trace)
        no_duplicate.classification_inputs["ordered"] = ["b", "a"]
        for mutated in (reordered, rounded, no_duplicate):
            self.assertNotEqual(
                trace_sha256(trace, decision=_decision("AAA"), run_metadata=RUN_METADATA),
                trace_sha256(mutated, decision=_decision("AAA"), run_metadata=RUN_METADATA),
            )

    def test_canonical_type_tags_keep_float_int_and_string_domains_distinct(self) -> None:
        payloads = [
            {"value": 1.0},
            {"value": 1},
            {"value": "1"},
            {"value": "0x1.0000000000000p+0"},
            {"value": 0.0},
            {"value": -0.0},
        ]
        encoded = [canonical_json_bytes(payload) for payload in payloads]
        self.assertEqual(len(encoded), len(set(encoded)))

    def test_serializer_orders_events_and_invocations_but_rejects_information_omissions(self) -> None:
        trace = _trace("AAA", repeated_helper=True)
        trace.events.reverse()
        trace.invocations.reverse()
        payload = serialize_runtime_trace_deterministic(trace, decision=_decision("AAA"), run_metadata=RUN_METADATA)
        self.assertEqual([event["sequence"] for event in payload["events"]], [2, 5])
        self.assertEqual([invocation["started_sequence"] for invocation in payload["invocations"]], [1, 4])

        omitted_event = copy.deepcopy(trace)
        omitted_event.events = omitted_event.events[1:]
        omitted_invocation = copy.deepcopy(trace)
        omitted_invocation.invocations = omitted_invocation.invocations[1:]
        omitted_error = _trace("AAA", status="partial")
        omitted_error.observation_errors = []
        for mutated in (omitted_event, omitted_invocation, omitted_error):
            with self.subTest(mutated=mutated):
                with self.assertRaises((ValueError, ArtifactValidationError)):
                    serialize_runtime_trace_deterministic(
                        mutated, decision=_decision("AAA"), run_metadata=RUN_METADATA
                    )

    def test_non_finite_float_and_absolute_path_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            trace = _trace("AAA")
            trace.classification_inputs["bad"] = value
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    serialize_runtime_trace_deterministic(
                        trace, decision=_decision("AAA"), run_metadata=RUN_METADATA
                    )
        trace = _trace("AAA")
        trace.classification_inputs["path"] = "C:\\Users\\private\\secret.txt"
        with self.assertRaises(ValueError):
            serialize_runtime_trace_deterministic(trace, decision=_decision("AAA"), run_metadata=RUN_METADATA)

    def test_gzip_has_fixed_mtime_no_filename_and_reproducible_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = write_runtime_scoring_artifact(
                Path(directory) / "first", run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=[_asset("AAA")]
            )
            second = write_runtime_scoring_artifact(
                Path(directory) / "second", run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=[_asset("AAA")]
            )
            first_bytes = first.output_paths[0].read_bytes()
            self.assertEqual(first_bytes, second.output_paths[0].read_bytes())
            self.assertEqual(int.from_bytes(first_bytes[4:8], "little"), 0)
            self.assertEqual(first_bytes[3] & 0x08, 0)


class RuntimeScoringArtifactStatusAndBudgetTests(unittest.TestCase):
    def test_zero_assets_are_failed_with_no_assets_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG, assets=[]
            )
            payload = _read_json(result.output_paths[0])
            self.assertEqual(result.artifact_status, "failed")
            self.assertEqual(payload["artifact_status"], "failed")
            self.assertEqual(payload["integrity"]["asset_count"], 0)
            self.assertTrue(any(error["error_code"] == "no_assets" for error in payload["errors"]))

    def test_zero_assets_remain_failed_and_auditable_when_hard_budget_is_tiny(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=[], soft_budget_bytes=1, hard_budget_bytes=1,
            )
            self.assertEqual(result.mode, "failed")
            payload = json.loads(result.output_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["artifact_status"], "failed")
            self.assertEqual(payload["parts"], [])
            self.assertEqual(payload["failed_assets"], [])
            self.assertTrue(any(error["error_code"] == "no_assets" for error in payload["errors"]))
            self.assertEqual(validate_runtime_scoring_artifact(result.output_paths[0]).symbols, ())

    def test_zero_assets_complete_or_partial_without_no_assets_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG, assets=[]
            ).output_paths[0]
            payload = _read_json(path)
            for status in ("complete", "partial"):
                mutated = copy.deepcopy(payload)
                mutated["artifact_status"] = status
                mutated["integrity"]["artifact_status"] = status
                mutated["errors"] = []
                _recompute_payload_hash(mutated)
                _write_canonical_gzip(path, mutated)
                with self.subTest(status=status), self.assertRaises(ArtifactValidationError):
                    validate_runtime_scoring_artifact(path)

            mutated = copy.deepcopy(payload)
            mutated["artifact_status"] = "failed"
            mutated["integrity"]["artifact_status"] = "failed"
            mutated["errors"] = []
            _recompute_payload_hash(mutated)
            _write_canonical_gzip(path, mutated)
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(path)

    def test_schema_catalog_and_integrity_domains_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=[_asset("AAA")]
            )
            payload = _read_json(result.output_paths[0])
            self.assertEqual(payload["schema_version"], "1.0")
            self.assertEqual(payload["artifact_status"], "complete")
            self.assertEqual(payload["run_metadata"]["asset_count"], 1)
            self.assertEqual(payload["rule_catalog_entry_count"], 97)
            self.assertEqual(json.dumps(payload).count('"rule_catalog":'), 1)
            self.assertIn("trace_hash", payload["integrity"])
            trace = payload["assets"][0]["trace"]
            self.assertTrue(trace["trace_id"].startswith("sha256:"))
            self.assertEqual(trace["last_persisted_event_sequence"], trace["last_reliable_sequence"])
            self.assertEqual(
                payload["assets"][0]["runtime_metadata"]["local_path"],
                "reports/runtime/local.tmp",
            )
            self.assertEqual(result.artifact_file_hash, hashlib.sha256(result.output_paths[0].read_bytes()).hexdigest())

    def test_writer_consumes_completed_inputs_without_calling_classifiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory, (
            patch("advisor.scoring.classify_asset", side_effect=AssertionError("classification rerun"))
        ), patch(
            "advisor.scoring.classify_asset_with_trace", side_effect=AssertionError("trace reconstructed")
        ):
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=[_asset("AAA")]
            )
        self.assertEqual(result.artifact_status, "complete")

    def test_partial_trace_marks_artifact_partial_and_sanitizes_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=[_asset("AAA"), _asset("BBB", status="partial")]
            )
            payload = _read_json(result.output_paths[0])
            self.assertEqual(result.artifact_status, "partial")
            self.assertEqual(payload["artifact_status"], "partial")
            self.assertNotIn("C:\\", json.dumps(payload))
            self.assertNotIn("collector failure detail", json.dumps(payload))
            self.assertTrue(any(error.get("error_code") == "collector_error" for error in payload["errors"]))

    def test_serialization_error_keeps_other_assets_and_marks_partial(self) -> None:
        broken = _asset("BROKEN")
        broken.trace.classification_inputs["bad"] = float("nan")
        with tempfile.TemporaryDirectory() as directory:
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=[_asset("GOOD"), broken]
            )
            payload = _read_json(result.output_paths[0])
            self.assertEqual(result.artifact_status, "partial")
            self.assertEqual([asset["symbol"] for asset in payload["assets"]], ["BROKEN", "GOOD"])
            broken_payload = payload["assets"][0]
            self.assertEqual(broken_payload["serialization_status"], "error")
            self.assertIn("serialized_asset_decision_hash", broken_payload)
            self.assertIsNone(broken_payload["trace"])
            self.assertEqual(payload["assets"][1]["serialization_status"], "complete")

    def test_soft_budget_adds_auditable_warning_without_evidence_loss(self) -> None:
        asset = _asset("AAA")
        asset.trace.classification_inputs["evidence"] = "x" * 4096
        with tempfile.TemporaryDirectory() as directory:
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=[asset], soft_budget_bytes=512, hard_budget_bytes=1024 * 1024
            )
            payload = _read_json(result.output_paths[0])
            self.assertEqual(result.mode, "single")
            self.assertEqual(payload["assets"][0]["trace"]["classification_inputs"]["evidence"], "x" * 4096)
            self.assertTrue(any(error["error_code"] == "soft_budget_exceeded" for error in payload["errors"]))

    def test_hard_budget_chunks_assets_and_index_validates_parts(self) -> None:
        assets = []
        for symbol in ("AAA", "BBB", "CCC"):
            asset = _asset(symbol)
            asset.trace.classification_inputs["evidence"] = symbol * 3000
            assets.append(asset)
        with tempfile.TemporaryDirectory() as directory:
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=assets, soft_budget_bytes=512, hard_budget_bytes=2100
            )
            self.assertEqual(result.mode, "chunked")
            self.assertTrue(result.output_paths[0].name.endswith(".index.json"))
            index = json.loads(result.output_paths[0].read_text(encoding="utf-8"))
            self.assertGreater(len(index["parts"]), 1)
            self.assertEqual(
                [symbol for part in index["parts"] for symbol in part["symbols"]],
                ["AAA", "BBB", "CCC"],
            )
            for part in index["parts"]:
                self.assertLessEqual(part["compressed_size_bytes"], 2100)
            validate_runtime_scoring_artifact(result.output_paths[0])

    def test_single_asset_over_hard_limit_is_failed_without_trace_truncation(self) -> None:
        asset = _asset("HUGE")
        asset.trace.classification_inputs["evidence"] = hashlib.sha256(b"x").hexdigest() * 300
        with tempfile.TemporaryDirectory() as directory:
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=[asset], soft_budget_bytes=64, hard_budget_bytes=256
            )
            self.assertEqual(result.artifact_status, "failed")
            index = json.loads(result.output_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(index["artifact_status"], "failed")
            self.assertEqual(index["failed_assets"][0]["symbol"], "HUGE")
            self.assertEqual(
                index["failed_assets"][0]["serialized_asset_decision_hash"],
                asset_decision_sha256(asset.decision),
            )
            self.assertEqual(
                index["failed_assets"][0]["trace"]["classification_inputs"]["evidence"],
                hashlib.sha256(b"x").hexdigest() * 300,
            )
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_oversized_asset_does_not_drop_other_assets_from_failed_index(self) -> None:
        oversized = _asset("AAA")
        oversized.trace.classification_inputs["evidence"] = "".join(
            hashlib.sha256(str(index).encode("ascii")).hexdigest() for index in range(600)
        )
        retained = _asset("BBB")
        with tempfile.TemporaryDirectory() as directory:
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=[oversized, retained], soft_budget_bytes=64, hard_budget_bytes=3000,
            )
            self.assertEqual(result.mode, "failed")
            index = json.loads(result.output_paths[0].read_text(encoding="utf-8"))
            self.assertEqual([asset["symbol"] for asset in index["failed_assets"]], ["AAA", "BBB"])
            self.assertTrue(any(
                error.get("error_code") == "single_asset_hard_budget_exceeded"
                and error.get("symbol") == "AAA"
                for error in index["errors"]
            ))
            self.assertEqual(validate_runtime_scoring_artifact(result.output_paths[0]).symbols, ("AAA", "BBB"))

    def test_secret_or_unknown_run_metadata_fails_closed_without_output(self) -> None:
        cases = (
            dict(RUN_METADATA, api_key="secret-value"),
            dict(RUN_METADATA, undocumented_field="value"),
        )
        for run_metadata in cases:
            with self.subTest(run_metadata=run_metadata), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    write_runtime_scoring_artifact(
                        Path(directory), run_metadata=run_metadata, rule_catalog=RULE_CATALOG,
                        assets=[_asset("AAA")]
                    )
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_missing_operational_timestamps_are_rejected(self) -> None:
        asset = _asset("AAA")
        object.__setattr__(asset, "runtime_metadata", {})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ArtifactValidationError):
                write_runtime_scoring_artifact(
                    Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                    assets=[asset]
                )

    def test_atomic_rename_failure_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("advisor.runtime_scoring_artifact.os.replace", side_effect=OSError("rename failed")):
                with self.assertRaises(OSError):
                    write_runtime_scoring_artifact(
                        root, run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                        assets=[_asset("AAA")]
                    )
            self.assertEqual(list(root.glob("*.tmp")), [])
            self.assertFalse((root / "scoring-runtime-trace.json.gz").exists())


class RuntimeScoringArtifactValidationTests(unittest.TestCase):
    def _single_artifact(self, directory: str) -> Path:
        return write_runtime_scoring_artifact(
            Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
            assets=[_asset("AAA"), _asset("BBB")]
        ).output_paths[0]

    def _failed_artifact(self, directory: str) -> Path:
        asset = _asset("HUGE")
        asset.trace.classification_inputs["evidence"] = hashlib.sha256(b"x").hexdigest() * 300
        return write_runtime_scoring_artifact(
            Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
            assets=[asset], soft_budget_bytes=64, hard_budget_bytes=256
        ).output_paths[0]

    def test_semantic_domains_are_central_and_immutable(self) -> None:
        self.assertEqual(SCHEMA_1_ENUM_DOMAINS["artifact_status"], frozenset({"complete", "partial", "failed"}))
        with self.assertRaises(TypeError):
            SCHEMA_1_ENUM_DOMAINS["new_domain"] = frozenset()  # type: ignore[index]
        with self.assertRaises(AttributeError):
            SCHEMA_1_ENUM_DOMAINS["decision"].add("__unknown_enum_value__")  # type: ignore[union-attr]

    def test_independent_allowlisted_enum_values_are_not_over_restricted(self) -> None:
        import advisor.runtime_scoring_artifact as artifact_module

        for domain, values in INDEPENDENT_ENUM_DOMAINS.items():
            for value in values:
                with self.subTest(domain=domain, value=value):
                    artifact_module._schema_enum(value, f"test.{domain}", domain)

    def test_valid_single_artifact_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            validated = validate_runtime_scoring_artifact(path)
            self.assertEqual(validated.artifact_status, "complete")
            self.assertEqual(validated.symbols, ("AAA", "BBB"))

    def test_corrupt_gzip_byte_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            data = bytearray(path.read_bytes())
            data[len(data) // 2] ^= 0x01
            path.write_bytes(bytes(data))
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(path)

    def test_reader_rejects_concatenated_trailing_and_truncated_gzip_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            member = path.read_bytes()
            cases = {
                "concatenated": member + member,
                "trailing": member + b"trailing-bytes",
                "truncated": member[:-8],
            }
            for name, data in cases.items():
                with self.subTest(name=name):
                    path.write_bytes(data)
                    expected = {
                        "concatenated": "multiple gzip members",
                        "trailing": "trailing bytes",
                        "truncated": "truncated gzip",
                    }[name]
                    with self.assertRaisesRegex(ArtifactValidationError, expected):
                        validate_runtime_scoring_artifact(path)

    def test_reader_accepts_exact_decompressed_limit_and_rejects_limit_plus_one(self) -> None:
        from advisor.runtime_scoring_artifact import DEFAULT_MAX_DECOMPRESSED_MEMBER_BYTES

        self.assertEqual(DEFAULT_MAX_DECOMPRESSED_MEMBER_BYTES, 100 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            raw = gzip.decompress(path.read_bytes())
            validate_runtime_scoring_artifact(path, max_decompressed_member_bytes=len(raw))
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(path, max_decompressed_member_bytes=len(raw) - 1)

    def test_duplicate_json_key_is_rejected_before_canonical_json_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            duplicate = _duplicate_first_json_key(gzip.decompress(path.read_bytes()), "schema_version")
            _write_raw_gzip(path, duplicate)
            with self.assertRaisesRegex(ArtifactValidationError, "duplicate JSON key"):
                validate_runtime_scoring_artifact(path)

    def test_duplicate_keys_are_rejected_at_every_structural_depth(self) -> None:
        from advisor.runtime_scoring_artifact import _load_canonical_json

        cases = {
            "top-level": b'{"schema_version":"1.0","schema_version":"1.0"}',
            "integrity": b'{"integrity":{"artifact_status":"complete","artifact_status":"complete"}}',
            "asset": b'{"assets":[{"symbol":"AAA","symbol":"AAA"}]}',
            "trace": b'{"trace":{"trace_status":"complete","trace_status":"complete"}}',
            "invocation": b'{"invocations":[{"invocation_id":"a","invocation_id":"a"}]}',
            "event": b'{"events":[{"sequence":1,"sequence":1}]}',
            "index": b'{"parts":[],"parts":[]}',
            "part": b'{"assets":[],"assets":[]}',
        }
        for name, raw in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(ArtifactValidationError, "duplicate JSON key"):
                _load_canonical_json(raw, label=name)

    def test_json_nan_and_infinity_are_rejected_by_parser(self) -> None:
        from advisor.runtime_scoring_artifact import _load_canonical_json

        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ArtifactValidationError, "non-finite JSON constant"
            ):
                _load_canonical_json(f'{{"value":{value}}}'.encode("ascii"), label=value)

    def test_critical_unknown_key_and_missing_trace_schema_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            payload = _read_json(path)
            payload["critical_unknown"] = True
            _recompute_payload_hash(payload)
            _write_canonical_gzip(path, payload)
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(path)

    def test_cross_field_trace_status_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            payload = _read_json(path)
            asset = payload["assets"][0]
            asset["trace_status"] = "complete"
            asset["trace"]["trace_status"] = "partial"
            asset["trace"]["coverage_complete"] = False
            asset["trace"]["collector_state"] = "failed"
            asset["trace"]["observation_errors"] = [
                {
                    "error_type": "trace_collection_error",
                    "operation": "test",
                    "error_code": "collector_error",
                    "exception_type": "RuntimeError",
                }
            ]
            _recompute_trace_hashes(payload)
            _recompute_payload_hash(payload)
            _write_canonical_gzip(path, payload)
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(path)

    def test_duration_is_lossless_finite_nonnegative_float_or_explicit_runtime_null(self) -> None:
        valid_values = (
            {"__float__": float(0.0).hex()},
            {"__float__": float(1.25).hex()},
            {"__float__": float(-0.0).hex()},
        )
        invalid_values = (
            "not-a-duration",
            1,
            True,
            {"__float__": float(-1.0).hex()},
            {"__float__": "0x1p999999999999999999999"},
            None,
        )
        for value in valid_values:
            with self.subTest(valid=value), tempfile.TemporaryDirectory() as directory:
                path = self._single_artifact(directory)
                payload = _read_json(path)
                payload["assets"][0]["runtime_metadata"]["duration"] = value
                _recompute_payload_hash(payload)
                _write_canonical_gzip(path, payload)
                validate_runtime_scoring_artifact(path)
        for value in invalid_values:
            with self.subTest(invalid=value), tempfile.TemporaryDirectory() as directory:
                path = self._single_artifact(directory)
                payload = _read_json(path)
                payload["assets"][0]["runtime_metadata"]["duration"] = value
                _recompute_payload_hash(payload)
                _write_canonical_gzip(path, payload)
                with self.assertRaises(ArtifactValidationError):
                    validate_runtime_scoring_artifact(path)

    def test_artifact_status_is_derived_and_outer_inner_statuses_are_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            payload = _read_json(path)
            asset = payload["assets"][0]

            # A stored partial/failed status cannot downgrade a validated
            # complete asset and become authoritative.
            for status in ("partial", "failed"):
                mutated = copy.deepcopy(payload)
                mutated["artifact_status"] = status
                mutated["integrity"]["artifact_status"] = status
                _recompute_payload_hash(mutated)
                _write_canonical_gzip(path, mutated)
                with self.subTest(outer=status), self.assertRaises(ArtifactValidationError):
                    validate_runtime_scoring_artifact(path)

            # An outer partial status cannot hide a complete inner trace when
            # the asset status itself disagrees with the trace.
            mutated = copy.deepcopy(payload)
            mutated["artifact_status"] = "partial"
            mutated["integrity"]["artifact_status"] = "partial"
            mutated["assets"][0]["trace_status"] = "partial"
            _recompute_trace_hashes(mutated)
            _recompute_payload_hash(mutated)
            _write_canonical_gzip(path, mutated)
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(path)

            # A valid mix derives partial from the content, not from input
            # order or a caller-selected outer status.
            partial = _read_json(self._single_artifact(directory))
            partial["assets"][1]["trace_status"] = "partial"
            partial["assets"][1]["trace"]["trace_status"] = "partial"
            partial["assets"][1]["trace"]["coverage_complete"] = False
            partial["assets"][1]["trace"]["collector_state"] = "failed"
            partial["assets"][1]["trace"]["observation_errors"] = [
                {
                    "error_type": "trace_collection_error",
                    "operation": "test",
                    "error_code": "collector_error",
                    "exception_type": "RuntimeError",
                }
            ]
            partial["artifact_status"] = "partial"
            partial["integrity"]["artifact_status"] = "partial"
            _recompute_trace_hashes(partial)
            _recompute_payload_hash(partial)
            _write_canonical_gzip(path, partial)
            self.assertEqual(validate_runtime_scoring_artifact(path).artifact_status, "partial")

        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            payload = _read_json(path)
            metadata = payload["assets"][0]["runtime_metadata"]
            metadata["duration"] = None
            metadata["serialization_status"] = "error"
            payload["assets"][0]["serialization_status"] = "error"
            payload["artifact_status"] = "partial"
            payload["integrity"]["artifact_status"] = "partial"
            payload["assets"][0]["errors"].append(
                {
                    "error_type": "serialization_error",
                    "operation": "test",
                    "error_code": "serialization_error",
                }
            )
            _recompute_payload_hash(payload)
            _write_canonical_gzip(path, payload)
            validate_runtime_scoring_artifact(path)

    def test_unknown_decision_is_rejected_after_all_hashes_are_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            payload = _read_json(path)
            decision = payload["assets"][0]["serialized_asset_decision"]
            decision["decision"] = "__unknown_enum_value__"
            _recompute_decision_trace_and_payload_hashes(payload)
            _write_canonical_gzip(path, payload)
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(path)

    def test_nested_initial_state_decision_is_rejected_after_hashes_are_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            payload = _read_json(path)
            _set_trace_path(payload, ("initial_state", "decision"), "__unknown_enum_value__")
            _recompute_trace_hashes(payload)
            _recompute_payload_hash(payload)
            _write_canonical_gzip(path, payload)
            with self.assertRaisesRegex(ArtifactValidationError, "unknown enum value"):
                validate_runtime_scoring_artifact(path)

    def test_independent_nested_decision_path_matrix_rejects_unknown_values(self) -> None:
        for name, path_parts in INDEPENDENT_DECISION_PATHS:
            with self.subTest(path=name), tempfile.TemporaryDirectory() as directory:
                path = self._single_artifact(directory)
                payload = _read_json(path)
                trace = payload["assets"][0]["trace"]
                trace["initial_state"]["max_decision"] = "tradeable"
                trace["final_state"]["max_decision"] = "tradeable"
                trace["events"][0]["state_changes"]["decision"] = {
                    "before": "tradeable",
                    "candidate": "tradeable",
                    "after": "tradeable",
                    "changed": False,
                }
                _recompute_trace_hashes(payload)
                _recompute_payload_hash(payload)
                _set_trace_path(payload, path_parts, "__unknown_enum_value__")
                _recompute_trace_hashes(payload)
                _recompute_payload_hash(payload)
                _write_canonical_gzip(path, payload)
                with self.assertRaisesRegex(ArtifactValidationError, "unknown enum value"):
                    validate_runtime_scoring_artifact(path)

    def test_spec_classification_base_decision_is_not_a_materialized_v1_path(self) -> None:
        """Keep the documented-but-unemitted path fail-closed without widening schema v1."""

        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            payload = _read_json(path)
            _set_trace_path(
                payload,
                ("classification", "base_decision"),
                "__unknown_enum_value__",
            )
            _recompute_trace_hashes(payload)
            _recompute_payload_hash(payload)
            _write_canonical_gzip(path, payload)
            with self.assertRaisesRegex(ArtifactValidationError, "trace.classification has unknown keys"):
                validate_runtime_scoring_artifact(path)

    def test_unknown_semantic_enums_are_rejected_by_independent_table(self) -> None:
        cases = (
            ("artifact_status", "single", lambda payload: (
                payload.__setitem__("artifact_status", "__unknown_enum_value__"),
                payload["integrity"].__setitem__("artifact_status", "__unknown_enum_value__"),
                _recompute_payload_hash(payload),
            )),
            ("trace_status", "single", lambda payload: (
                payload["assets"][0].__setitem__("trace_status", "__unknown_enum_value__"),
                payload["assets"][0]["trace"].__setitem__("trace_status", "__unknown_enum_value__"),
                _recompute_trace_hashes(payload),
                _recompute_payload_hash(payload),
            )),
            ("asset_type", "single", lambda payload: (
                payload["assets"][0].__setitem__("asset_type", "__unknown_enum_value__"),
                payload["assets"][0]["serialized_asset_decision"].__setitem__("asset_type", "__unknown_enum_value__"),
                payload["assets"][0]["trace"].__setitem__("asset_type", "__unknown_enum_value__"),
                _recompute_decision_trace_and_payload_hashes(payload),
            )),
            ("decision", "single", lambda payload: (
                payload["assets"][0]["serialized_asset_decision"].__setitem__("decision", "__unknown_enum_value__"),
                _recompute_decision_trace_and_payload_hashes(payload),
            )),
            ("collector_state", "single", lambda payload: (
                payload["assets"][0]["trace"].__setitem__("collector_state", "__unknown_enum_value__"),
                _recompute_trace_hashes(payload),
                _recompute_payload_hash(payload),
            )),
            ("event.termination_kind", "single", lambda payload: (
                payload["assets"][0]["trace"]["events"][0].__setitem__("termination_kind", "__unknown_enum_value__"),
                _recompute_trace_hashes(payload),
                _recompute_payload_hash(payload),
            )),
            ("invocation.termination_kind", "single", lambda payload: (
                payload["assets"][0]["trace"]["invocations"][0].__setitem__("termination_kind", "__unknown_enum_value__"),
                _recompute_trace_hashes(payload),
                _recompute_payload_hash(payload),
            )),
            ("rule_catalog.branch_kind", "single", lambda payload: (
                payload["rule_catalog"][0].__setitem__("branch_kind", "__unknown_enum_value__"),
                _recompute_catalog_trace_and_payload_hashes(payload),
            )),
            ("runtime_metadata.classification_status", "single", lambda payload: (
                payload["assets"][0]["runtime_metadata"].__setitem__("classification_status", "__unknown_enum_value__"),
                _recompute_payload_hash(payload),
            )),
            ("runtime_metadata.serialization_status", "single", lambda payload: (
                payload["assets"][0]["runtime_metadata"].__setitem__("serialization_status", "__unknown_enum_value__"),
                _recompute_payload_hash(payload),
            )),
            ("run_metadata.runtime_sha_status", "single", lambda payload: (
                payload["run_metadata"].__setitem__("runtime_sha_status", "__unknown_enum_value__"),
                _recompute_payload_hash(payload),
            )),
            ("run_metadata.schedule", "single", lambda payload: (
                payload["run_metadata"].__setitem__("schedule", "__unknown_enum_value__"),
                payload["assets"][0]["trace"].__setitem__("schedule", "__unknown_enum_value__"),
                _recompute_trace_hashes(payload),
                _recompute_payload_hash(payload),
            )),
            ("invocation.coverage_status", "single", lambda payload: (
                payload["assets"][0]["trace"]["invocations"][0].__setitem__("coverage_status", "__unknown_enum_value__"),
                _recompute_trace_hashes(payload),
                _recompute_payload_hash(payload),
            )),
            ("event.axis", "single", lambda payload: (
                payload["assets"][0]["trace"]["events"][0].__setitem__("axis", "__unknown_enum_value__"),
                _recompute_trace_hashes(payload),
                _recompute_payload_hash(payload),
            )),
            ("event.effect_type", "single", lambda payload: (
                payload["assets"][0]["trace"]["events"][0].__setitem__("effect_type", "__unknown_enum_value__"),
                _recompute_trace_hashes(payload),
                _recompute_payload_hash(payload),
            )),
            ("error.error_code", "single", lambda payload: (
                payload.__setitem__("errors", [{"error_code": "__unknown_enum_value__"}]),
                _recompute_payload_hash(payload),
            )),
            ("integrity.schema_validation_status", "single", lambda payload: (
                payload["integrity"].__setitem__("schema_validation_status", "__unknown_enum_value__"),
                _recompute_payload_hash(payload),
            )),
            ("failed_asset.decision_status", "failed", lambda payload: (
                payload["failed_assets"][0].__setitem__("decision_status", "__unknown_enum_value__"),
                _recompute_payload_hash(payload),
            )),
        )
        for name, mode, mutate in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = self._single_artifact(directory) if mode == "single" else self._failed_artifact(directory)
                payload = _read_json(path) if mode == "single" else json.loads(path.read_text(encoding="utf-8"))
                mutate(payload)
                if mode == "failed":
                    path.write_bytes(canonical_json_bytes(payload))
                else:
                    _write_canonical_gzip(path, payload)
                with self.assertRaises(ArtifactValidationError):
                    validate_runtime_scoring_artifact(path)

    def test_integrity_gzip_deterministic_must_be_true_literal(self) -> None:
        for value in (False, 0, 1, None, "true"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = self._single_artifact(directory)
                payload = _read_json(path)
                payload["integrity"]["gzip_deterministic"] = value
                _recompute_payload_hash(payload)
                _write_canonical_gzip(path, payload)
                with self.assertRaises(ArtifactValidationError):
                    validate_runtime_scoring_artifact(path)

            payload = _read_json(self._single_artifact(directory))
            payload["assets"][0]["trace"].pop("trace_schema_version")
            _recompute_trace_hashes(payload)
            _recompute_payload_hash(payload)
            _write_canonical_gzip(path, payload)
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(path)

    def test_part_uncompressed_size_and_failed_decision_mutations_are_rejected(self) -> None:
        assets = []
        for symbol in ("AAA", "BBB", "CCC"):
            asset = _asset(symbol)
            asset.trace.classification_inputs["evidence"] = symbol * 3000
            assets.append(asset)
        with tempfile.TemporaryDirectory() as directory:
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=assets, soft_budget_bytes=512, hard_budget_bytes=2100
            )
            index_path = result.output_paths[0]
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["parts"][0]["uncompressed_size_bytes"] += 1
            _recompute_payload_hash(index)
            index_path.write_bytes(canonical_json_bytes(index))
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(index_path)

    def test_failed_asset_with_unavailable_decision_is_explicit_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset = _asset("HUGE")
            asset.trace.classification_inputs["evidence"] = hashlib.sha256(b"x").hexdigest() * 300
            index_path = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=[asset], soft_budget_bytes=64, hard_budget_bytes=256
            ).output_paths[0]
            index = json.loads(index_path.read_text(encoding="utf-8"))
            failed = index["failed_assets"][0]
            failed["decision_status"] = "unavailable"
            failed["serialized_asset_decision"] = None
            failed["serialized_asset_decision_hash"] = None
            failed["trace"] = None
            failed["trace_hash"] = None
            failed["errors"].append(
                {
                    "error_code": "asset_decision_unavailable",
                    "error_type": "serialization_error",
                    "operation": "serialize_asset_decision",
                    "symbol": "HUGE",
                }
            )
            domains = [
                {
                    "symbol": failed["symbol"],
                    "asset_type": failed["asset_type"],
                    "universe_origin": failed["universe_origin"],
                    "trace_hash": failed["trace_hash"],
                }
            ]
            index["integrity"]["trace_hash"] = hashlib.sha256(canonical_json_bytes(domains)).hexdigest()
            _recompute_payload_hash(index)
            index_path.write_bytes(canonical_json_bytes(index))
            validated = validate_runtime_scoring_artifact(index_path)
            self.assertEqual(validated.artifact_status, "failed")

        with tempfile.TemporaryDirectory() as directory:
            asset = _asset("HUGE")
            asset.trace.classification_inputs["evidence"] = hashlib.sha256(b"x").hexdigest() * 300
            index_path = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=[asset], soft_budget_bytes=64, hard_budget_bytes=256
            ).output_paths[0]
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["failed_assets"][0]["serialized_asset_decision"]["decision"] = "avoid"
            _recompute_payload_hash(index)
            index_path.write_bytes(canonical_json_bytes(index))
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(index_path)

    def test_float_type_tag_removal_is_rejected_after_hashes_are_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._single_artifact(directory)
            payload = _read_json(path)
            payload["assets"][0]["trace"]["initial_state"]["score"] = "0x1.1c80000000000p+6"
            _recompute_trace_hashes(payload)
            _recompute_payload_hash(payload)
            _write_canonical_gzip(path, payload)
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(path)

    def test_schema_catalog_duplicate_and_asset_hash_corruptions_are_detected(self) -> None:
        mutations = {
            "schema": lambda payload: payload.__setitem__("schema_version", "999"),
            "catalog": lambda payload: payload.pop("rule_catalog"),
            "duplicate": lambda payload: payload["assets"].append(copy.deepcopy(payload["assets"][0])),
            "decision_hash": lambda payload: payload["assets"][0].__setitem__(
                "serialized_asset_decision_hash", "0" * 64
            ),
            "trace_hash": lambda payload: payload["assets"][0].__setitem__("trace_hash", "0" * 64),
            "artifact_hash": lambda payload: payload["integrity"].__setitem__(
                "artifact_payload_hash", "0" * 64
            ),
            "aggregate_trace_hash": lambda payload: payload["integrity"].__setitem__(
                "trace_hash", "0" * 64
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = self._single_artifact(directory)
                payload = _read_json(path)
                mutate(payload)
                _write_canonical_gzip(path, payload)
                with self.assertRaises(ArtifactValidationError):
                    validate_runtime_scoring_artifact(path)

    def test_missing_part_changed_part_hash_and_duplicate_chunk_symbol_are_detected(self) -> None:
        assets = []
        for symbol in ("AAA", "BBB", "CCC"):
            asset = _asset(symbol)
            asset.trace.classification_inputs["evidence"] = symbol * 3000
            assets.append(asset)
        for mutation in ("missing_part", "part_hash", "duplicate_symbol"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                result = write_runtime_scoring_artifact(
                    Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                    assets=assets, soft_budget_bytes=512, hard_budget_bytes=2100
                )
                index_path = result.output_paths[0]
                index = json.loads(index_path.read_text(encoding="utf-8"))
                if mutation == "missing_part":
                    (index_path.parent / index["parts"][0]["filename"]).unlink()
                elif mutation == "part_hash":
                    index["parts"][0]["sha256"] = "0" * 64
                    index_path.write_bytes(canonical_json_bytes(index))
                else:
                    index["parts"][1]["symbols"][0] = index["parts"][0]["symbols"][0]
                    index_path.write_bytes(canonical_json_bytes(index))
                with self.assertRaises(ArtifactValidationError):
                    validate_runtime_scoring_artifact(index_path)

    def test_part_asset_hash_mutation_is_rejected_by_full_part_validation(self) -> None:
        assets = []
        for symbol in ("AAA", "BBB", "CCC"):
            asset = _asset(symbol)
            asset.trace.classification_inputs["evidence"] = symbol * 3000
            assets.append(asset)
        with tempfile.TemporaryDirectory() as directory:
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                assets=assets, soft_budget_bytes=512, hard_budget_bytes=2100
            )
            index_path = result.output_paths[0]
            index = json.loads(index_path.read_text(encoding="utf-8"))
            part_path = index_path.parent / index["parts"][0]["filename"]
            part = _read_json(part_path)
            part["assets"][0]["serialized_asset_decision_hash"] = "0" * 64
            _recompute_payload_hash(part)
            part_bytes = gzip.compress(canonical_json_bytes(part), compresslevel=9, mtime=0)
            part_path.write_bytes(part_bytes)
            index["parts"][0]["sha256"] = hashlib.sha256(part_bytes).hexdigest()
            index["parts"][0]["compressed_size_bytes"] = len(part_bytes)
            index["parts"][0]["uncompressed_size_bytes"] = len(canonical_json_bytes(part))
            _recompute_payload_hash(index)
            index_path.write_bytes(canonical_json_bytes(index))
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(index_path)


class RuntimeScoringArtifactPropertyAndMutationTests(unittest.TestCase):
    def test_small_deterministic_combinations_preserve_all_assets_and_trace_shapes(self) -> None:
        for count in (1, 2, 4):
            for reverse_order in (False, True):
                assets = [
                    _asset(
                        f"S{index}",
                        status="partial" if index == count - 1 and count > 1 else "complete",
                        matched=index % 2 == 0,
                        repeated_helper=index % 3 == 0,
                        invocation_without_events=index == 0 and count > 1,
                        reason_codes=["dup", "dup", str(index)],
                    )
                    for index in range(count)
                ]
                if reverse_order:
                    assets.reverse()
                with self.subTest(count=count, reverse=reverse_order), tempfile.TemporaryDirectory() as directory:
                    result = write_runtime_scoring_artifact(
                        Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG, assets=assets
                    )
                    payload = _read_json(result.output_paths[0])
                    self.assertEqual(len(payload["assets"]), count)
                    self.assertEqual(
                        [item["symbol"] for item in payload["assets"]],
                        sorted(item.decision.symbol for item in assets),
                    )
                    validate_runtime_scoring_artifact(result.output_paths[0])

    def test_small_combinations_keep_serialization_errors_local_to_the_asset(self) -> None:
        for count in (1, 2, 4):
            assets = [_asset(f"S{index}") for index in range(count)]
            assets[-1].trace.classification_inputs["bad"] = float("nan")
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                result = write_runtime_scoring_artifact(
                    Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG, assets=assets
                )
                payload = _read_json(result.output_paths[0])
                self.assertEqual(len(payload["assets"]), count)
                self.assertEqual(payload["assets"][-1]["serialization_status"], "error")
                if count > 1:
                    self.assertTrue(all(
                        asset["serialization_status"] == "complete"
                        for asset in payload["assets"][:-1]
                    ))

    def test_mutation_guards_reject_clock_mtime_unsorted_assets_removed_false_event_and_hash_confusion(self) -> None:
        assets = [_asset("BBB"), _asset("AAA", matched=False)]
        with tempfile.TemporaryDirectory() as directory:
            result = write_runtime_scoring_artifact(
                Path(directory), run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG, assets=assets
            )
            path = result.output_paths[0]
            original = _read_json(path)
            mutations = []

            nonzero_mtime = bytearray(path.read_bytes())
            nonzero_mtime[4:8] = (123).to_bytes(4, "little")
            mutations.append(("mtime", bytes(nonzero_mtime)))

            unsorted = copy.deepcopy(original)
            unsorted["assets"].reverse()
            mutations.append(("asset_order", gzip.compress(canonical_json_bytes(unsorted), mtime=0)))

            removed_false = copy.deepcopy(original)
            removed_false["assets"][0]["trace"]["events"] = []
            mutations.append(("matched_false", gzip.compress(canonical_json_bytes(removed_false), mtime=0)))

            rounded = copy.deepcopy(original)
            rounded["assets"][0]["trace"]["classification_inputs"]["precise"] = {
                "__float__": (1.23).hex()
            }
            mutations.append(("rounded_float", gzip.compress(canonical_json_bytes(rounded), mtime=0)))

            confused = copy.deepcopy(original)
            confused["assets"][0]["trace_hash"] = confused["assets"][0]["serialized_asset_decision_hash"]
            mutations.append(("hash_domain", gzip.compress(canonical_json_bytes(confused), mtime=0)))

            for name, data in mutations:
                with self.subTest(name=name):
                    path.write_bytes(data)
                    with self.assertRaises(ArtifactValidationError):
                        validate_runtime_scoring_artifact(path)

    def test_cleanup_failure_preserves_destination_and_original_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "scoring-runtime-trace.json.gz"
            destination.write_bytes(b"previous artifact")
            with patch("advisor.runtime_scoring_artifact.os.replace", side_effect=OSError("replace failure")), patch.object(
                Path, "unlink", side_effect=OSError("cleanup failure")
            ):
                with self.assertRaisesRegex(OSError, "replace failure") as context:
                    write_runtime_scoring_artifact(
                        root, run_metadata=RUN_METADATA, rule_catalog=RULE_CATALOG,
                        assets=[_asset("AAA")]
                    )
            self.assertEqual(destination.read_bytes(), b"previous artifact")
            self.assertEqual(getattr(context.exception, "cleanup_status", None), "failed")
            temporary_files = list(root.glob("*.tmp"))
            self.assertTrue(temporary_files)
            with self.assertRaises(ArtifactValidationError):
                validate_runtime_scoring_artifact(temporary_files[0])
            for temporary in temporary_files:
                temporary.unlink()


if __name__ == "__main__":
    unittest.main()
