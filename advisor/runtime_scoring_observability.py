from __future__ import annotations

import hashlib
import io
import json
import math
import sys
import tokenize
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import wraps
from typing import Any, Callable, Iterator, Mapping, Sequence

from advisor.models import AssetDecision, BacktestStats, RiskPlan


@dataclass(frozen=True)
class RuleMetadata:
    rule_id: str
    function: str
    source_code_locator: dict[str, object]
    branch_signature: str
    branch_kind: str
    axis: str
    effect_type: str
    evidence_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObservationError:
    error_type: str
    operation: str
    error_code: str
    exception_type: str
    sequence: int | None = None
    invocation_id: str | None = None


@dataclass
class RuntimeEvent:
    sequence: int
    invocation_id: str
    rule_id: str
    reached: bool
    evaluated: bool
    matched: bool | None
    terminated: bool
    termination_kind: str | None
    axis: str
    effect_type: str
    evidence_keys: list[str]
    condition_inputs: dict[str, object]
    state_changes: dict[str, object]
    reason_codes_added: list[str]
    alerts_added: list[str]
    limitations_added: list[str]
    branch_label: str | None = None


@dataclass
class InvocationTrace:
    invocation_id: str
    function: str
    parent_invocation_id: str | None
    call_ordinal: int
    started_sequence: int
    completed_sequence: int | None = None
    termination_kind: str | None = None
    termination_rule_id: str | None = None
    termination_sequence: int | None = None
    coverage_status: str = "active"
    interval_complete: bool = False
    coverage_complete: bool = False
    invocation_coverage_complete: bool = False
    last_reliable_sequence: int = 0
    observation_failure_sequence: int | None = None
    catalog_rule_ids: list[str] = field(default_factory=list)
    reached_rule_ids: list[str] = field(default_factory=list)
    known_unreached_rule_ids: list[str] = field(default_factory=list)
    unreached_rule_ids: list[str] = field(default_factory=list)
    unknown_rule_ids: list[str] = field(default_factory=list)


@dataclass
class RuntimeTrace:
    classification_inputs: dict[str, object]
    initial_state: dict[str, object]
    final_state: dict[str, object]
    events: list[RuntimeEvent]
    invocations: list[InvocationTrace]
    effective_now_utc: datetime | None
    trace_status: str
    observer_enabled: bool
    coverage_complete: bool
    last_reliable_sequence: int
    observation_failure_sequence: int | None
    observation_errors: list[ObservationError]
    active_invocation_id: str | None = None
    collector_state: str = "idle"


class _EvaluationRaised(Exception):
    def __init__(self, original: Exception) -> None:
        super().__init__()
        self.original = original


class _ObservationOperationFailure(Exception):
    def __init__(self, operation: str, original: Exception) -> None:
        super().__init__()
        self.operation = operation
        self.original = original


_EVALUATED_VALUE = object()


@dataclass
class _ObservationToken:
    context: ObservationContext | None
    invocation: InvocationTrace | None
    rule: RuleMetadata | None
    value: object = None
    matched: bool | None = None
    recorded: bool = False
    condition_inputs: dict[str, object] = field(default_factory=dict)
    branch_label: str | None = None
    terminate_if_matched: bool = False

    def finish(
        self,
        *,
        state_changes: dict[str, object] | None = None,
        reason_codes_added: Sequence[str] = (),
        alerts_added: Sequence[str] = (),
        limitations_added: Sequence[str] = (),
        terminated: bool | None = None,
        termination_kind: str | None = None,
    ) -> None:
        if not self.recorded or self.rule is None or self.invocation is None:
            return
        if terminated is None:
            terminated = bool(self.terminate_if_matched and self.matched is True)
        self.context._commit_event(
            invocation=self.invocation,
            rule=self.rule,
            evaluated=True,
            matched=self.matched,
            terminated=bool(terminated),
            termination_kind=termination_kind if terminated else None,
            condition_inputs=self.condition_inputs,
            state_changes=state_changes or {},
            reason_codes_added=reason_codes_added,
            alerts_added=alerts_added,
            limitations_added=limitations_added,
            branch_label=self.branch_label,
        )


class _InMemoryCollector:
    def record_event(self, _event: RuntimeEvent) -> None:
        return None


_RULE_FUNCTION_GROUPS: dict[str, tuple[str, ...]] = {
    "classify_asset": (
        "classify_asset.stale_annotation",
        "classify_asset.initial_blocking_base",
        "classify_asset.initial_confidence_cap",
        "classify_asset.initial_hard_gate_cap",
        "classify_asset.initial_low_sample_cap",
        "classify_asset.max_blocking_cap",
        "classify_asset.high_severity_cap",
        "classify_asset.stale_cap",
        "classify_asset.backtest_branch_entry",
        "classify_asset.win_rate_below_35",
        "classify_asset.win_rate_below_40",
        "classify_asset.win_rate_below_45_nonpositive_ev",
        "classify_asset.nonpositive_ev",
        "classify_asset.negative_ev_high_severity",
        "classify_asset.intc_like_cap",
        "classify_asset.confidence_below_65",
        "classify_asset.technical_unvalidated_cap",
        "classify_asset.minimum_market_cap_override",
        "classify_asset.earnings_imminent_wait",
        "classify_asset.fundamental_gap_thesis",
        "classify_asset.earnings_missing_thesis",
        "classify_asset.sample_quality_setup_quality",
        "classify_asset.sample_quality_derived",
        "classify_asset.hard_gate_earnings_choice",
        "classify_asset.low_win_rate_choice",
        "classify_asset.last_price_timestamp_field",
        "classify_asset.stale_reason_field",
        "classify_asset.news_status_field",
        "classify_asset.gap_risk_field",
        "classify_asset.short_status_field",
    ),
    "_base_decision": (
        "classify_asset.base_avoid",
        "classify_asset.base_wait",
        "classify_asset.base_tradeable",
        "classify_asset.base_technical_unvalidated",
    ),
    "_is_intc_like_case": (
        "classify_asset.intc_investment_threshold",
        "classify_asset.intc_gap_requirement",
        "classify_asset.intc_valuation_requirement",
    ),
    "_hold_suggestion": ("classify_asset.hold_median_days",),
    "_has_confidence_limiting_data_gap": (
        "classify_asset.confidence_nonblocking_skip",
        "classify_asset.confidence_explicit_limitation",
        "classify_asset.confidence_pattern_limitation",
    ),
    "_data_quality": (
        "classify_asset.data_quality_blocked",
        "classify_asset.data_quality_limited",
    ),
    "_missing_data_severity": (
        "classify_asset.severity_blocking",
        "classify_asset.severity_high",
        "classify_asset.severity_medium",
    ),
    "_apply_uncollected_context_limits": (
        "classify_asset.uncollected_news_limit",
        "classify_asset.uncollected_sector_limit",
        "classify_asset.missing_ev_components",
    ),
    "_freshness_context": (
        "classify_asset.cache_stale",
        "classify_asset.last_price_timestamp_fallback",
        "classify_asset.freshness_stale_reason_choice",
    ),
    "_market_session": (
        "classify_asset.market_session_weekend",
        "classify_asset.market_session_regular",
        "classify_asset.market_session_pre",
        "classify_asset.market_session_after",
    ),
    "_data_quality_score": (
        "classify_asset.data_score_blocked",
        "classify_asset.data_score_limited",
        "classify_asset.data_score_high_severity",
        "classify_asset.data_score_critical_severity",
        "classify_asset.data_score_earnings_missing",
        "classify_asset.data_score_stale",
    ),
    "_decision_confidence_score": (
        "classify_asset.confidence_sample_low",
        "classify_asset.confidence_sample_medium",
        "classify_asset.confidence_sample_size_presence",
        "classify_asset.confidence_nonpositive_ev",
        "classify_asset.confidence_earnings_missing",
        "classify_asset.confidence_mixed_provider",
        "classify_asset.confidence_neutral_market",
        "classify_asset.confidence_risk_off",
        "classify_asset.confidence_stale",
        "classify_asset.confidence_news_low",
        "classify_asset.confidence_news_not_collected",
        "classify_asset.confidence_macro_not_collected",
        "classify_asset.confidence_sector_not_collected",
        "classify_asset.confidence_ev_components",
    ),
    "_event_check_status": (
        "classify_asset.event_crypto",
        "classify_asset.event_source_unavailable",
        "classify_asset.event_not_collected",
        "classify_asset.event_verified",
    ),
    "_bucket_for_decision": (
        "classify_asset.bucket_known",
        "classify_asset.bucket_speculative",
    ),
    "_thesis_status": (
        "classify_asset.thesis_fundamental_gap",
        "classify_asset.thesis_strengthening",
        "classify_asset.thesis_weakening",
        "classify_asset.thesis_stable",
    ),
    "_sector_benchmark": (
        "classify_asset.sector_semiconductors",
        "classify_asset.sector_software",
        "classify_asset.sector_cloud",
        "classify_asset.sector_healthcare",
    ),
    "_short_setup_score": ("classify_asset.short_setup_threshold",),
    "_is_technical_unvalidated": ("classify_asset.technical_ev_presence",),
    "_news_summary": ("classify_asset.news_summary_empty",),
    "rate_sample_quality": (
        "classify_asset.sample_quality_low",
        "classify_asset.sample_quality_medium",
    ),
    "_apply_cap": ("classify_asset.apply_cap_rank_choice",),
    "_weaker_cap": ("classify_asset.weaker_cap_rank_choice",),
}

_KNOWN_OBSERVED_FUNCTIONS = frozenset(
    {
        "classify_asset",
        "_base_decision",
        "_apply_cap",
        "_weaker_cap",
        "_decision_rank",
        "_is_intc_like_case",
        "_hold_suggestion",
        "_has_confidence_limiting_data_gap",
        "_has_blocking_data_gap",
        "_data_quality",
        "_missing_data_severity",
        "_apply_uncollected_context_limits",
        "_freshness_context",
        "_market_session",
        "_has_fundamental_validation_gap",
        "_is_technical_unvalidated",
        "_data_quality_score",
        "_decision_confidence_score",
        "_event_check_status",
        "_bucket_for_decision",
        "_thesis_status",
        "_sector_benchmark",
        "_short_setup_score",
        "_news_summary",
        "rate_sample_quality",
    }
)

# The approved inventory separates ordinary if/elif branches from the 15
# inline conditional expressions that also change an observed field.
_INLINE_RULE_IDS = frozenset(
    {
        "classify_asset.sample_quality_setup_quality",
        "classify_asset.sample_quality_derived",
        "classify_asset.hard_gate_earnings_choice",
        "classify_asset.low_win_rate_choice",
        "classify_asset.last_price_timestamp_fallback",
        "classify_asset.freshness_stale_reason_choice",
        "classify_asset.apply_cap_rank_choice",
        "classify_asset.weaker_cap_rank_choice",
        "classify_asset.technical_ev_presence",
        "classify_asset.confidence_sample_size_presence",
        "classify_asset.last_price_timestamp_field",
        "classify_asset.stale_reason_field",
        "classify_asset.news_status_field",
        "classify_asset.gap_risk_field",
        "classify_asset.short_status_field",
    }
)

# Fact observations receive a boolean local that may have been normalized with
# ``bool(...)``.  These approved expressions intentionally retain that wrapper
# because it is part of the frozen canonical signature.
_BOOLEAN_WRAPPED_RULE_IDS = frozenset(
    {
        "classify_asset.backtest_branch_entry",
        "classify_asset.last_price_timestamp_fallback",
        "classify_asset.last_price_timestamp_field",
        "classify_asset.news_status_field",
        "classify_asset.sample_quality_derived",
        "classify_asset.stale_reason_field",
        "classify_asset.uncollected_sector_limit",
    }
)

_RULE_METADATA_DEFAULTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "_base_decision": ("decision", "base", ("decision.base_score_inputs",)),
    "_is_intc_like_case": ("decision", "annotation", ("decision.base_score_inputs",)),
    "_hold_suggestion": ("other", "annotation", ("backtest.sample_size",)),
    "_has_confidence_limiting_data_gap": ("confidence", "annotation", ("data_quality.limitation",)),
    "_data_quality": ("quality", "annotation", ("data_quality.limitation",)),
    "_missing_data_severity": ("quality", "annotation", ("data_quality.limitation",)),
    "_apply_uncollected_context_limits": ("quality", "annotation", ("data_quality.limitation",)),
    "_freshness_context": ("quality", "annotation", ("technical.price_stale",)),
    "_market_session": ("other", "annotation", ("market.session",)),
    "_data_quality_score": ("quality", "cap", ("data_quality.score",)),
    "_decision_confidence_score": ("confidence", "cap", ("decision_confidence.score",)),
    "_event_check_status": ("quality", "annotation", ("event.earnings_status",)),
    "_bucket_for_decision": ("other", "annotation", ("decision.base_score_inputs",)),
    "_thesis_status": ("other", "annotation", ("decision.base_score_inputs",)),
    "_sector_benchmark": ("other", "annotation", ("asset.theme",)),
    "_short_setup_score": ("risk", "annotation", ("decision.base_score_inputs",)),
    "_news_summary": ("other", "annotation", ("news.confirmed_status",)),
    "rate_sample_quality": ("confidence", "annotation", ("backtest.sample_size",)),
    "_apply_cap": ("decision", "cap", ("decision.base_score_inputs",)),
    "_weaker_cap": ("decision", "cap", ("decision.base_score_inputs",)),
}


_RULE_METADATA_OVERRIDES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "classify_asset.stale_annotation": ("quality", "annotation", ("technical.price_stale",)),
    "classify_asset.initial_blocking_base": ("decision", "base", ("data_quality.limitation",)),
    "classify_asset.initial_confidence_cap": ("decision", "cap", ("data_quality.limitation",)),
    "classify_asset.initial_hard_gate_cap": (
        "decision",
        "cap",
        ("market.regime", "market.liquidity", "event.earnings_imminent", "technical.recent_gap"),
    ),
    "classify_asset.initial_low_sample_cap": ("decision", "cap", ("backtest.sample_size",)),
    "classify_asset.max_blocking_cap": ("decision", "cap", ("data_quality.limitation",)),
    "classify_asset.high_severity_cap": ("decision", "cap", ("data_quality.missing_severity",)),
    "classify_asset.stale_cap": ("decision", "cap", ("technical.price_stale",)),
    "classify_asset.backtest_branch_entry": (
        "decision",
        "control_flow",
        ("backtest.sample_size", "backtest.win_rate_2r"),
    ),
    "classify_asset.win_rate_below_35": (
        "decision",
        "cap",
        ("backtest.win_rate_2r", "decision.base_score_inputs"),
    ),
    "classify_asset.win_rate_below_40": ("decision", "cap", ("backtest.win_rate_2r",)),
    "classify_asset.win_rate_below_45_nonpositive_ev": (
        "decision",
        "cap",
        ("backtest.win_rate_2r", "backtest.expected_value_r"),
    ),
    "classify_asset.nonpositive_ev": ("decision", "cap", ("backtest.expected_value_r",)),
    "classify_asset.negative_ev_high_severity": (
        "decision",
        "cap",
        ("backtest.expected_value_r", "data_quality.missing_severity"),
    ),
    "classify_asset.intc_like_cap": (
        "decision",
        "cap",
        ("decision.base_score_inputs", "technical.recent_gap", "backtest.win_rate_2r"),
    ),
    "classify_asset.confidence_below_65": ("decision", "cap", ("decision_confidence.score",)),
    "classify_asset.technical_unvalidated_cap": (
        "decision",
        "cap",
        (
            "decision.base_score_inputs",
            "data_quality.limitation",
            "data_quality.missing_severity",
            "backtest.expected_value_r",
            "backtest.sample_quality",
        ),
    ),
    "classify_asset.minimum_market_cap_override": (
        "decision",
        "override",
        ("fundamentals.market_cap_status",),
    ),
    "classify_asset.earnings_imminent_wait": (
        "decision",
        "adjustment",
        ("event.earnings_imminent", "decision.base_score_inputs"),
    ),
    "classify_asset.fundamental_gap_thesis": ("other", "annotation", ("data_quality.limitation",)),
    "classify_asset.earnings_missing_thesis": ("other", "annotation", ("fundamentals.earnings_status",)),
    "classify_asset.sample_quality_setup_quality": ("confidence", "annotation", ("backtest.sample_quality",)),
    "classify_asset.sample_quality_derived": ("confidence", "annotation", ("backtest.sample_size",)),
    "classify_asset.hard_gate_earnings_choice": ("decision", "cap", ("event.earnings_imminent",)),
    "classify_asset.low_win_rate_choice": (
        "decision",
        "cap",
        ("backtest.win_rate_2r", "decision.base_score_inputs"),
    ),
    "classify_asset.last_price_timestamp_fallback": ("other", "annotation", ("asset.data_timestamp",)),
    "classify_asset.freshness_stale_reason_choice": ("quality", "annotation", ("technical.price_stale",)),
    "classify_asset.technical_ev_presence": ("decision", "annotation", ("backtest.expected_value_r",)),
    "classify_asset.confidence_sample_size_presence": ("confidence", "annotation", ("backtest.sample_size",)),
    "classify_asset.last_price_timestamp_field": ("other", "annotation", ("asset.data_timestamp",)),
    "classify_asset.stale_reason_field": ("quality", "annotation", ("technical.price_stale",)),
    "classify_asset.news_status_field": ("quality", "annotation", ("provider.news.status",)),
    "classify_asset.gap_risk_field": ("risk", "annotation", ("technical.recent_gap",)),
    "classify_asset.short_status_field": ("risk", "annotation", ("decision.base_score_inputs",)),
    "classify_asset.data_score_blocked": ("quality", "annotation", ("data_quality.score",)),
    "classify_asset.data_score_limited": ("quality", "cap", ("data_quality.score",)),
    "classify_asset.data_score_high_severity": ("quality", "cap", ("data_quality.missing_severity",)),
    "classify_asset.data_score_critical_severity": ("quality", "cap", ("data_quality.missing_severity",)),
    "classify_asset.data_score_earnings_missing": ("quality", "cap", ("data_quality.limitation",)),
    "classify_asset.data_score_stale": ("quality", "cap", ("technical.price_stale",)),
    "classify_asset.uncollected_news_limit": ("quality", "annotation", ("provider.news.status",)),
    "classify_asset.uncollected_sector_limit": ("quality", "annotation", ("asset.theme", "asset.type")),
    "classify_asset.missing_ev_components": (
        "quality",
        "annotation",
        ("backtest.expected_value_r", "backtest.avg_win_r", "backtest.avg_loss_r"),
    ),
    "classify_asset.event_crypto": ("other", "annotation", ("asset.type",)),
    "classify_asset.event_source_unavailable": ("quality", "annotation", ("fundamentals.earnings_status",)),
    "classify_asset.event_not_collected": ("quality", "annotation", ("fundamentals.earnings_status",)),
    "classify_asset.event_verified": ("quality", "annotation", ("event.earnings_status",)),
    "classify_asset.confidence_sample_low": ("confidence", "cap", ("backtest.sample_size",)),
    "classify_asset.confidence_sample_medium": ("confidence", "cap", ("backtest.sample_size",)),
    "classify_asset.confidence_nonpositive_ev": ("confidence", "cap", ("backtest.expected_value_r",)),
    "classify_asset.confidence_earnings_missing": ("confidence", "cap", ("data_quality.limitation",)),
    "classify_asset.confidence_mixed_provider": ("confidence", "cap", ("provider.mixed_data",)),
    "classify_asset.confidence_neutral_market": ("confidence", "cap", ("market.regime",)),
    "classify_asset.confidence_risk_off": ("confidence", "cap", ("market.regime",)),
    "classify_asset.confidence_stale": ("confidence", "cap", ("technical.price_stale",)),
    "classify_asset.confidence_news_low": ("confidence", "cap", ("news.confirmed_status", "news.confidence")),
    "classify_asset.confidence_news_not_collected": ("confidence", "cap", ("provider.news.status",)),
    "classify_asset.confidence_macro_not_collected": ("confidence", "cap", ("market.regime",)),
    "classify_asset.confidence_sector_not_collected": ("confidence", "cap", ("asset.theme",)),
    "classify_asset.confidence_ev_components": ("confidence", "cap", ("backtest.expected_value_r",)),
}


def _rule_metadata(rule_id: str, function: str) -> tuple[str, str, tuple[str, ...]]:
    if rule_id in _RULE_METADATA_OVERRIDES:
        return _RULE_METADATA_OVERRIDES[rule_id]
    try:
        return _RULE_METADATA_DEFAULTS[function]
    except KeyError as error:
        raise RuntimeError(f"missing metadata defaults for observed function: {function}") from error


def branch_kinds_from_source(source: str) -> dict[tuple[int, int], str]:
    """Resolve exact if/elif/ifexp kinds from AST coordinates and source tokens."""
    import ast

    tree = ast.parse(source)
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    token_by_start = {(token.start[0], token.start[1]): token.string for token in tokens}
    lines = source.splitlines()
    kinds: dict[tuple[int, int], str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.IfExp):
            kinds[(node.lineno, node.col_offset)] = "ifexp"
            continue
        if not isinstance(node, ast.If):
            continue
        line = lines[node.lineno - 1]
        prefix_bytes = line.encode("utf-8")[: node.col_offset]
        char_col = len(prefix_bytes.decode("utf-8", errors="ignore"))
        keyword = token_by_start.get((node.lineno, char_col))
        if keyword not in {"if", "elif"}:
            keyword = next(
                (
                    value
                    for (line_number, column), value in token_by_start.items()
                    if line_number == node.lineno and column >= char_col and value in {"if", "elif"}
                ),
                None,
            )
        if keyword not in {"if", "elif"}:
            raise AssertionError(f"could not resolve lexical branch keyword at {node.lineno}:{node.col_offset}")
        kinds[(node.lineno, node.col_offset)] = keyword
    return kinds


def _branch_kind_for_observation(
    node: object,
    *,
    called: str,
    rule_id: str,
    parents: dict[object, object],
    branch_kinds: dict[tuple[int, int], str],
    owner: object | None = None,
    fact_name: str | None = None,
) -> str:
    import ast

    if called == "_observed_if" or called == "_observe_value" or rule_id in _INLINE_RULE_IDS:
        return "ifexp"
    if owner is not None and fact_name is not None:
        candidates = []
        for candidate in ast.walk(owner):
            if not isinstance(candidate, ast.If):
                continue
            tests = list(ast.walk(candidate.test))
            if any(
                isinstance(test, ast.Name) and test.id == fact_name
                or isinstance(test, ast.NamedExpr)
                and isinstance(test.target, ast.Name)
                and test.target.id == fact_name
                for test in tests
            ):
                candidates.append(candidate)
        if candidates:
            def candidate_priority(candidate: object) -> tuple[int, int]:
                test = candidate.test
                has_named_expression = any(
                    isinstance(item, ast.NamedExpr)
                    and isinstance(item.target, ast.Name)
                    and item.target.id == fact_name
                    for item in ast.walk(test)
                )
                is_direct_name = isinstance(test, ast.Name) and test.id == fact_name
                shape_priority = 0 if has_named_expression else 1 if is_direct_name else 2
                return shape_priority, abs(candidate.lineno - getattr(node, "lineno", candidate.lineno))

            selected = min(
                candidates,
                key=candidate_priority,
            )
            return branch_kinds[(selected.lineno, selected.col_offset)]
    current = node
    while current in parents:
        parent = parents[current]
        if isinstance(parent, ast.If):
            return branch_kinds[(parent.lineno, parent.col_offset)]
        current = parent
    return "if"


def _source_observation_metadata(
    source: str,
    tree: object,
    node: object,
    *,
    called: str,
    functions: list[object],
    parents: dict[object, object],
    branch_kinds: dict[tuple[int, int], str],
) -> tuple[str, object, object, str]:
    """Resolve a literal rule call and its already-evaluated source fact."""
    import ast

    if called in {"_observe", "_observe_value", "_observed_if", "_observe_elif"}:
        id_index = 1
        evaluator_index = 2
    elif called in {"observe_condition", "observe_value"}:
        id_index = 2
        evaluator_index = 3
    else:
        raise AssertionError(f"unsupported observation call: {called}")
    args = getattr(node, "args", [])
    if len(args) <= id_index or not isinstance(args[id_index], ast.Constant):
        raise AssertionError("observation rule id must be a literal")
    rule_id = args[id_index].value
    if not isinstance(rule_id, str):
        raise AssertionError("observation rule id must be a string")
    owner = min(
        (
            function
            for function in functions
            if function.lineno <= node.lineno <= function.end_lineno
        ),
        key=lambda function: function.end_lineno - function.lineno,
        default=None,
    )
    if owner is None:
        raise AssertionError(f"rule has no owning function: {rule_id}")

    fact_name: str | None = None
    if called in {"_observe", "_observe_value"}:
        fact_keyword = next(
            (
                keyword
                for keyword in node.keywords
                if keyword.arg in {"matched", "value"}
            ),
            None,
        )
        if fact_keyword is not None:
            if not isinstance(fact_keyword.value, ast.Name):
                raise AssertionError(f"observation fact must be a local name: {rule_id}")
            fact_name = fact_keyword.value.id

    if fact_name is not None:
        candidates = []
        for candidate in ast.walk(owner):
            expression = None
            name = None
            if isinstance(candidate, ast.Assign) and len(candidate.targets) == 1 and isinstance(candidate.targets[0], ast.Name):
                name = candidate.targets[0].id
                expression = candidate.value
            elif isinstance(candidate, ast.NamedExpr) and isinstance(candidate.target, ast.Name):
                name = candidate.target.id
                expression = candidate.value
            if name == fact_name and (
                candidate.end_lineno < node.lineno
                or candidate.end_lineno == node.lineno and candidate.end_col_offset <= node.col_offset
            ):
                candidates.append((candidate.end_lineno, candidate.end_col_offset, expression))
        if not candidates:
            raise AssertionError(f"observation fact has no source assignment: {rule_id}")
        expression = max(candidates, key=lambda item: (item[0], item[1]))[2]
    else:
        if len(args) <= evaluator_index or not isinstance(args[evaluator_index], ast.Lambda):
            raise AssertionError(f"rule evaluator is not a lambda: {rule_id}")
        expression = args[evaluator_index].body

    if not isinstance(expression, ast.AST):
        raise AssertionError(f"rule has no canonical source expression: {rule_id}")
    if (
        rule_id not in _BOOLEAN_WRAPPED_RULE_IDS
        and isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id == "bool"
        and len(expression.args) == 1
        and not expression.keywords
    ):
        expression = expression.args[0]
    branch_kind = _branch_kind_for_observation(
        node,
        called=called,
        rule_id=rule_id,
        parents=parents,
        branch_kinds=branch_kinds,
        owner=owner,
        fact_name=fact_name,
    )
    return rule_id, owner, expression, branch_kind


def _discover_source_locators() -> dict[str, tuple[str, str, int, int, str, str]]:
    """Resolve static rule signatures to their real instrumented call sites.

    Rule IDs remain the approved static catalog keys; source inspection only
    supplies the current line span and verifies that each signature still has
    a corresponding AST call.  IDs are never generated from line numbers.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    expected_ids = {
        rule_id
        for rule_ids in _RULE_FUNCTION_GROUPS.values()
        for rule_id in rule_ids
    }
    resolved: dict[str, tuple[str, str, int, int, str, str]] = {}
    observed_ids: set[str] = set()
    for relative_path in ("advisor/scoring.py", "advisor/risk.py"):
        path = root / relative_path
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        branch_kinds = branch_kinds_from_source(source)
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func.id if isinstance(node.func, ast.Name) else None
            if called not in {"_observe", "_observe_value", "_observed_if", "_observe_elif", "observe_condition", "observe_value"}:
                continue
            if called in {"_observe", "_observe_value", "_observed_if", "_observe_elif"}:
                if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
                    continue
            elif len(node.args) < 3 or not isinstance(node.args[2], ast.Constant):
                continue
            rule_id, owner, expression, branch_kind = _source_observation_metadata(
                source,
                tree,
                node,
                called=called,
                functions=functions,
                parents=parents,
                branch_kinds=branch_kinds,
            )
            if not isinstance(rule_id, str) or not rule_id.startswith("classify_asset."):
                continue
            observed_ids.add(rule_id)
            if rule_id not in expected_ids:
                continue
            if rule_id in resolved:
                raise RuntimeError(f"catalog rule is instrumented more than once: {rule_id}")
            signature = ast.dump(
                expression,
                annotate_fields=True,
                include_attributes=False,
            )
            resolved[rule_id] = (
                relative_path,
                owner.name if owner is not None else "",
                node.lineno,
                node.end_lineno,
                signature,
                branch_kind,
            )
    if observed_ids != expected_ids:
        missing = sorted(expected_ids - observed_ids)
        extra = sorted(observed_ids - expected_ids)
        raise RuntimeError(f"catalog source locators do not match approved branches: missing={missing}, extra={extra}")
    if set(resolved) != expected_ids or any(not record[1] for record in resolved.values()):
        missing = sorted(expected_ids - set(resolved))
        raise RuntimeError(f"catalog source locators missing AST branches: {missing}")
    return resolved


_RUNTIME_LOCATOR_DATA = _discover_source_locators()


RULE_CATALOG: tuple[RuleMetadata, ...] = tuple(
    sorted(
        (
            RuleMetadata(
                rule_id=rule_id,
                function=function,
                source_code_locator={
                    "path": _RUNTIME_LOCATOR_DATA[rule_id][0],
                    "function": _RUNTIME_LOCATOR_DATA[rule_id][1],
                    "line_start": _RUNTIME_LOCATOR_DATA[rule_id][2],
                    "line_end": _RUNTIME_LOCATOR_DATA[rule_id][3],
                },
                branch_signature=_RUNTIME_LOCATOR_DATA[rule_id][4],
                branch_kind=_RUNTIME_LOCATOR_DATA[rule_id][5],
                axis=_rule_metadata(rule_id, function)[0],
                effect_type=_rule_metadata(rule_id, function)[1],
                evidence_keys=_rule_metadata(rule_id, function)[2],
            )
            for function, rule_ids in _RULE_FUNCTION_GROUPS.items()
            for rule_id in rule_ids
        ),
        key=lambda rule: rule.rule_id,
    )
)

if len(RULE_CATALOG) != 97 or len({rule.rule_id for rule in RULE_CATALOG}) != 97:
    raise RuntimeError("approved scoring observability catalog must contain 97 unique rule IDs")

_RULE_BY_ID = {rule.rule_id: rule for rule in RULE_CATALOG}
_RULE_IDS_BY_FUNCTION = {
    function: list(rule_ids)
    for function, rule_ids in _RULE_FUNCTION_GROUPS.items()
}


def _source_rule_records(root: object) -> dict[str, dict[str, object]]:
    """Extract rule call sites and canonical evaluator ASTs independently."""
    import ast
    from pathlib import Path

    root_path = Path(root)
    records: dict[str, dict[str, object]] = {}
    for relative_path in ("advisor/scoring.py", "advisor/risk.py"):
        source = (root_path / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        branch_kinds = branch_kinds_from_source(source)
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func.id if isinstance(node.func, ast.Name) else None
            if called not in {"_observe", "_observe_value", "_observed_if", "_observe_elif", "observe_condition", "observe_value"}:
                continue
            if called in {"_observe", "_observe_value", "_observed_if", "_observe_elif"}:
                if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
                    continue
            elif len(node.args) < 3 or not isinstance(node.args[2], ast.Constant):
                continue
            rule_id, owner, expression, branch_kind = _source_observation_metadata(
                source,
                tree,
                node,
                called=called,
                functions=functions,
                parents=parents,
                branch_kinds=branch_kinds,
            )
            if not rule_id.startswith("classify_asset."):
                continue
            if owner is None:
                raise AssertionError(f"rule has no owning function: {rule_id}")
            if rule_id in records:
                raise AssertionError(f"rule is instrumented more than once: {rule_id}")
            records[rule_id] = {
                "path": relative_path,
                "function": owner.name,
                "line_start": node.lineno,
                "line_end": node.end_lineno,
                "branch_kind": branch_kind,
                "canonical_branch_signature": ast.dump(
                    expression,
                    annotate_fields=True,
                    include_attributes=False,
                ),
            }
    return records


def validate_rule_catalog(root: object, *, expected_snapshot: object | None = None) -> dict[str, list[object]]:
    """Compare runtime metadata with an independent snapshot and current AST."""
    import json
    from pathlib import Path

    root_path = Path(root)
    snapshot_path = (
        Path(expected_snapshot)
        if expected_snapshot is not None
        else root_path / "tests/fixtures/runtime_scoring_rule_catalog_expected.json"
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot_entries = list(snapshot.get("rules", []))
    expected_rules = {entry["rule_id"]: entry for entry in snapshot_entries}
    runtime_rules = {rule.rule_id: rule for rule in RULE_CATALOG}
    expected_ids = set(expected_rules)
    runtime_ids = set(runtime_rules)
    report: dict[str, list[object]] = {
        "missing_ids": sorted(expected_ids - runtime_ids),
        "extra_ids": sorted(runtime_ids - expected_ids),
        "metadata_mismatches": [],
        "invalid_locators": [],
        "signature_mismatches": [],
        "ownership_mismatches": [],
    }
    if len(snapshot_entries) != 97 or len(expected_rules) != 97:
        report["metadata_mismatches"].append("snapshot must contain 97 unique IDs")
    if len(RULE_CATALOG) != 97 or len(runtime_ids) != 97:
        report["metadata_mismatches"].append("catalog must contain 97 unique IDs")
    expected_counts = {
        "if": int(snapshot["expected_if_count"]),
        "elif": int(snapshot["expected_elif_count"]),
        "ifexp": int(snapshot["expected_ifexp_count"]),
    }
    runtime_counts = {
        kind: sum(rule.branch_kind == kind for rule in RULE_CATALOG)
        for kind in expected_counts
    }
    if runtime_counts != expected_counts:
        report["metadata_mismatches"].append(
            {"expected_branch_counts": expected_counts, "actual_branch_counts": runtime_counts}
        )
    source_records = _source_rule_records(root_path)
    source_ids = set(source_records)
    report["missing_ids"].extend(sorted(expected_ids - source_ids))
    report["extra_ids"].extend(sorted(source_ids - expected_ids))
    report["missing_ids"] = sorted(set(report["missing_ids"]))
    report["extra_ids"] = sorted(set(report["extra_ids"]))
    for rule_id in sorted(expected_ids | runtime_ids):
        expected = expected_rules.get(rule_id)
        actual = runtime_rules.get(rule_id)
        if expected is None or actual is None:
            continue
        actual_metadata = {
            "function": actual.function,
            "source_code_locator": actual.source_code_locator,
            "branch_signature": actual.branch_signature,
            "branch_kind": actual.branch_kind,
            "axis": actual.axis,
            "effect_type": actual.effect_type,
            "evidence_keys": list(actual.evidence_keys),
        }
        expected_metadata = {
            "function": expected["function"],
            "source_code_locator": expected["source_code_locator"],
            "branch_signature": expected["canonical_branch_signature"],
            "branch_kind": expected["branch_kind"],
            "axis": expected["axis"],
            "effect_type": expected["effect_type"],
            "evidence_keys": expected["evidence_keys"],
        }
        if actual_metadata != expected_metadata:
            report["metadata_mismatches"].append({"rule_id": rule_id, "expected": expected_metadata, "actual": actual_metadata})
        source_record = source_records.get(rule_id)
        if source_record is None:
            report["invalid_locators"].append(rule_id)
            continue
        actual_locator = actual.source_code_locator
        if actual_locator != {
            "path": source_record["path"],
            "function": source_record["function"],
            "line_start": source_record["line_start"],
            "line_end": source_record["line_end"],
        }:
            report["invalid_locators"].append(rule_id)
        if actual.branch_signature != source_record["canonical_branch_signature"]:
            report["signature_mismatches"].append(rule_id)
        if actual.branch_kind != source_record["branch_kind"]:
            report["signature_mismatches"].append(rule_id)
        if expected["branch_kind"] != source_record["branch_kind"]:
            report["signature_mismatches"].append(rule_id)
        expected_function = _RULE_IDS_BY_FUNCTION.get(actual.function, [])
        if rule_id not in expected_function:
            report["ownership_mismatches"].append(rule_id)
    if any(report.values()):
        raise AssertionError(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report


def validate_runtime_trace(trace: RuntimeTrace) -> None:
    """Assert ownership, coverage, sequence, and invocation-graph invariants."""
    if trace.trace_status in {"partial", "failed"} and trace.coverage_complete:
        raise AssertionError("partial or failed trace cannot claim complete coverage")
    if len({invocation.invocation_id for invocation in trace.invocations}) != len(trace.invocations):
        raise AssertionError("duplicate invocation ID")
    invocations = {invocation.invocation_id: invocation for invocation in trace.invocations}
    roots = [invocation for invocation in trace.invocations if invocation.parent_invocation_id is None]
    if trace.invocations and len(roots) != 1:
        raise AssertionError("trace must have exactly one root invocation")
    if trace.coverage_complete and len(roots) != 1:
        raise AssertionError("complete trace must have exactly one root invocation")
    if trace.active_invocation_id is not None and trace.active_invocation_id not in invocations:
        raise AssertionError("trace references an unknown active invocation")
    if trace.coverage_complete and trace.active_invocation_id is not None:
        raise AssertionError("complete trace cannot have an active invocation")
    for invocation in trace.invocations:
        parent_id = invocation.parent_invocation_id
        if parent_id is not None and parent_id not in invocations:
            raise AssertionError(f"invocation references unknown parent: {invocation.invocation_id}")
        if invocation.function not in _KNOWN_OBSERVED_FUNCTIONS:
            raise AssertionError(f"invocation references unknown function: {invocation.function}")
        if invocation.completed_sequence is not None and invocation.completed_sequence <= invocation.started_sequence:
            raise AssertionError(f"invocation interval is not positive: {invocation.invocation_id}")
        if invocation.interval_complete != (invocation.completed_sequence is not None):
            raise AssertionError(f"invocation interval flag mismatch: {invocation.invocation_id}")
        if invocation.completed_sequence is None and trace.coverage_complete:
            raise AssertionError(f"complete trace contains an open interval: {invocation.invocation_id}")
    for invocation in trace.invocations:
        seen_ancestors: set[str] = set()
        current = invocation
        while current.parent_invocation_id is not None:
            if current.invocation_id in seen_ancestors:
                raise AssertionError(f"invocation parent cycle: {invocation.invocation_id}")
            seen_ancestors.add(current.invocation_id)
            current = invocations[current.parent_invocation_id]
    for invocation in trace.invocations:
        if invocation.parent_invocation_id is None:
            continue
        parent = invocations[invocation.parent_invocation_id]
        if invocation.started_sequence <= parent.started_sequence:
            raise AssertionError(f"child starts before parent: {invocation.invocation_id}")
        if parent.completed_sequence is not None and invocation.started_sequence >= parent.completed_sequence:
            raise AssertionError(f"child starts after parent completed: {invocation.invocation_id}")
        if invocation.completed_sequence is not None and invocation.completed_sequence <= parent.started_sequence:
            raise AssertionError(f"child ends before parent started: {invocation.invocation_id}")
        if invocation.completed_sequence is not None and parent.completed_sequence is not None:
            if invocation.completed_sequence >= parent.completed_sequence:
                raise AssertionError(f"child is outside parent interval: {invocation.invocation_id}")
    seen_sequences: set[int] = set()
    if trace.observation_failure_sequence is not None:
        if trace.observation_failure_sequence <= trace.last_reliable_sequence:
            raise AssertionError("observation failure must follow the last reliable sequence")
        seen_sequences.add(trace.observation_failure_sequence)
    previous_sequence = 0
    for invocation in trace.invocations:
        for sequence in (invocation.started_sequence, invocation.completed_sequence):
            if sequence is None:
                continue
            if sequence <= 0:
                raise AssertionError(f"invocation sequence must be positive: {sequence}")
            if sequence in seen_sequences:
                raise AssertionError(f"duplicate invocation sequence: {sequence}")
            seen_sequences.add(sequence)
    for event in trace.events:
        if event.sequence <= 0:
            raise AssertionError(f"event sequence must be positive: {event.sequence}")
        if event.sequence in seen_sequences:
            raise AssertionError(f"duplicate event sequence: {event.sequence}")
        if event.sequence <= previous_sequence:
            raise AssertionError(f"non-monotonic event sequence: {event.sequence}")
        seen_sequences.add(event.sequence)
        previous_sequence = event.sequence
        invocation = invocations.get(event.invocation_id)
        if invocation is None:
            raise AssertionError(f"event references unknown invocation: {event.invocation_id}")
        rule = _RULE_BY_ID.get(event.rule_id)
        if rule is None:
            raise AssertionError(f"event references unknown rule: {event.rule_id}")
        if rule.function != invocation.function:
            raise AssertionError(f"event owner mismatch: {event.rule_id}")
        if event.rule_id not in invocation.catalog_rule_ids:
            raise AssertionError(f"event rule is not in invocation catalog: {event.rule_id}")
        if event.sequence <= invocation.started_sequence:
            raise AssertionError(f"event precedes invocation: {event.rule_id}")
        if invocation.completed_sequence is not None and event.sequence >= invocation.completed_sequence:
            raise AssertionError(f"event follows invocation: {event.rule_id}")
    if trace.last_reliable_sequence:
        if trace.last_reliable_sequence not in {event.sequence for event in trace.events}:
            raise AssertionError("last reliable sequence does not identify a confirmed event")
    for left_index, left in enumerate(trace.invocations):
        if left.completed_sequence is None:
            continue
        for right in trace.invocations[left_index + 1 :]:
            if right.completed_sequence is None:
                continue
            left_is_ancestor = False
            current = right
            while current.parent_invocation_id is not None:
                if current.parent_invocation_id == left.invocation_id:
                    left_is_ancestor = True
                    break
                current = invocations[current.parent_invocation_id]
            right_is_ancestor = False
            current = left
            while current.parent_invocation_id is not None:
                if current.parent_invocation_id == right.invocation_id:
                    right_is_ancestor = True
                    break
                current = invocations[current.parent_invocation_id]
            if left_is_ancestor or right_is_ancestor:
                continue
            if not (
                left.completed_sequence < right.started_sequence
                or right.completed_sequence < left.started_sequence
            ):
                raise AssertionError("non-nested invocation intervals overlap")
    for invocation in trace.invocations:
        expected_catalog = list(_RULE_IDS_BY_FUNCTION.get(invocation.function, []))
        if invocation.catalog_rule_ids != expected_catalog:
            raise AssertionError(f"invocation catalog mismatch: {invocation.invocation_id}")
        if invocation.coverage_complete != invocation.invocation_coverage_complete:
            raise AssertionError(f"coverage flag mismatch: {invocation.invocation_id}")
        if trace.coverage_complete and not invocation.interval_complete:
            raise AssertionError(f"complete trace contains incomplete invocation: {invocation.invocation_id}")
        catalog = set(invocation.catalog_rule_ids)
        reached = set(invocation.reached_rule_ids)
        known_unreached = set(invocation.known_unreached_rule_ids)
        if len(invocation.catalog_rule_ids) != len(catalog):
            raise AssertionError(f"duplicate catalog rule: {invocation.invocation_id}")
        if len(invocation.reached_rule_ids) != len(reached):
            raise AssertionError(f"duplicate reached rule: {invocation.invocation_id}")
        if len(invocation.known_unreached_rule_ids) != len(known_unreached):
            raise AssertionError(f"duplicate unreached rule: {invocation.invocation_id}")
        if not reached <= catalog or not known_unreached <= catalog:
            raise AssertionError(f"coverage contains an unknown rule: {invocation.invocation_id}")
        if reached & known_unreached:
            raise AssertionError(f"coverage overlap: {invocation.invocation_id}")
        if invocation.coverage_complete and reached | known_unreached != catalog:
            raise AssertionError(f"incomplete complete-coverage partition: {invocation.invocation_id}")
        if not invocation.coverage_complete:
            if invocation.known_unreached_rule_ids or invocation.unreached_rule_ids:
                raise AssertionError(f"partial coverage cannot claim known unreached: {invocation.invocation_id}")
            unknown = set(invocation.unknown_rule_ids)
            if reached & unknown or reached | unknown != catalog:
                raise AssertionError(f"partial coverage must partition reached and unknown rules: {invocation.invocation_id}")
        event_rules = [event.rule_id for event in trace.events if event.invocation_id == invocation.invocation_id]
        if list(dict.fromkeys(event_rules)) != invocation.reached_rule_ids:
            raise AssertionError(f"reached events disagree with invocation: {invocation.invocation_id}")


class ObservationContext:
    """Fail-open in-memory observation state for one classification."""

    _OBSERVATION_METHODS = frozenset(
        {
            "start_trace",
            "capture_initial_state",
            "begin_invocation",
            "observe_condition",
            "observe_value",
            "update_classification_inputs",
            "end_invocation",
            "record_invocation_exception",
            "capture_final_state",
            "build_trace",
        }
    )

    def __getattribute__(self, name: str) -> object:
        """Turn collector-operation failures into sanitized no-op fallbacks.

        This proxy also covers test doubles and runtime subclasses overriding an
        operation, while `_EvaluationRaised` is deliberately allowed to reach
        the observation wrapper so genuine classifier exceptions propagate.
        """
        value = object.__getattribute__(self, name)
        if name.startswith("_") or name not in ObservationContext._OBSERVATION_METHODS:
            return value
        try:
            depth = object.__getattribute__(self, "_observation_proxy_depth")
        except AttributeError:
            return value
        if depth:
            return value
        if not callable(value):
            return value

        def guarded(*args: object, **kwargs: object) -> object:
            object.__setattr__(self, "_observation_proxy_depth", depth + 1)
            try:
                result = value(*args, **kwargs)
                if name == "build_trace" and self.enabled and not isinstance(result, RuntimeTrace):
                    self._mark_collector_failure("build_trace", RuntimeError("invalid trace result"))
                    return self._minimal_trace(trace_status="failed")
                return result
            except _EvaluationRaised:
                raise
            except _ObservationOperationFailure as failure:
                self._mark_collector_failure(failure.operation, failure.original)
                return self._observation_fallback(name, args, kwargs)
            except Exception as error:
                self._mark_collector_failure(name, error)
                return self._observation_fallback(name, args, kwargs)
            finally:
                object.__setattr__(self, "_observation_proxy_depth", depth)

        return guarded

    def _observation_fallback(
        self,
        operation: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object:
        if operation == "observe_condition":
            invocation = args[0] if len(args) > 0 else None
            rule_id = args[1] if len(args) > 1 else ""
            evaluated_value = kwargs.get("evaluated_value", _EVALUATED_VALUE)
            if evaluated_value is _EVALUATED_VALUE:
                evaluator = args[2] if len(args) > 2 else (lambda: False)
                value = evaluator()  # type: ignore[operator]
            else:
                value = evaluated_value
            return _ObservationToken(
                self,
                invocation if isinstance(invocation, InvocationTrace) else None,
                _RULE_BY_ID.get(str(rule_id)),
                value=value,
                matched=bool(value),
                recorded=False,
            )
        if operation == "observe_value":
            invocation = args[0] if len(args) > 0 else None
            rule_id = args[1] if len(args) > 1 else ""
            evaluated_value = kwargs.get("evaluated_value", _EVALUATED_VALUE)
            if evaluated_value is _EVALUATED_VALUE:
                evaluator = args[2] if len(args) > 2 else (lambda: None)
                value = evaluator()  # type: ignore[operator]
            else:
                value = evaluated_value
            matcher = kwargs.get("matcher", bool)
            return _ObservationToken(
                self,
                invocation if isinstance(invocation, InvocationTrace) else None,
                _RULE_BY_ID.get(str(rule_id)),
                value=value,
                matched=bool(matcher(value)),  # type: ignore[operator]
                recorded=False,
            )
        if operation == "begin_invocation":
            return None
        if operation == "end_invocation":
            return None
        if operation == "build_trace":
            return self._minimal_trace(trace_status="failed")
        return None

    def __init__(
        self,
        *,
        enabled: bool,
        effective_now_utc: datetime | None,
        collector: object | None = None,
    ) -> None:
        self.enabled = enabled
        self.effective_now_utc = effective_now_utc
        self.collector = collector if collector is not None else _InMemoryCollector()
        self.collector_state = "disabled" if not enabled else "active"
        self.sequence_counter = 0
        self.invocation_stack: list[InvocationTrace] = []
        self.trace_status = "disabled" if not enabled else "active"
        self.last_reliable_sequence = 0
        self.observation_errors: list[ObservationError] = []
        self.observation_failure_sequence: int | None = None
        self._recording_enabled = enabled
        self._events: list[RuntimeEvent] = []
        self._event_sequences: set[int] = set()
        self._invocations: list[InvocationTrace] = []
        self._call_ordinals: dict[tuple[str | None, str], int] = {}
        self._classification_inputs: dict[str, object] = {}
        self._initial_state: dict[str, object] = {}
        self._final_state: dict[str, object] = {}
        self._final_state_ready = False
        self._observation_proxy_depth = 0
        self._pending_branch_token: _ObservationToken | None = None

    def _reserve_sequence(self) -> int:
        self.sequence_counter += 1
        return self.sequence_counter

    @classmethod
    def create_enabled(cls, effective_now_utc: datetime, collector: object | None = None) -> ObservationContext:
        return cls(enabled=True, effective_now_utc=effective_now_utc, collector=collector)

    def capture_initial_state(self, state: dict[str, object]) -> None:
        if self.enabled:
            self._initial_state = dict(state)

    def capture_final_state(self, state: dict[str, object]) -> None:
        if self.enabled:
            self._final_state = dict(state)
            self._final_state_ready = True

    def _safe_call(self, operation: str, callback: Callable[[], object], default: object = None) -> object:
        try:
            return callback()
        except _EvaluationRaised:
            raise
        except _ObservationOperationFailure as failure:
            self._mark_collector_failure(failure.operation, failure.original)
            return default
        except Exception as error:
            self._mark_collector_failure(operation, error)
            return default

    def safe_start_trace(self, *, classification_inputs: dict[str, object], initial_state: dict[str, object]) -> None:
        if not self.enabled:
            return
        self._safe_call(
            "start_trace",
            lambda: self.start_trace(classification_inputs=classification_inputs, initial_state=initial_state),
        )

    def safe_capture_initial_state(self, state: dict[str, object]) -> None:
        if not self.enabled:
            return
        self._safe_call("capture_initial_state", lambda: self.capture_initial_state(state))

    def safe_capture_final_state(self, state: dict[str, object]) -> None:
        if not self.enabled:
            return
        self._safe_call("capture_final_state", lambda: self.capture_final_state(state))

    def safe_update_classification_inputs(self, **values: object) -> None:
        if not self.enabled:
            return
        self._safe_call("update_classification_inputs", lambda: self.update_classification_inputs(**values))

    def safe_observe_condition(
        self,
        invocation: InvocationTrace | None,
        rule_id: str,
        evaluator: Callable[[], object] | None,
        *,
        evaluated_value: object = _EVALUATED_VALUE,
        condition_inputs: Callable[[], Mapping[str, object]] | Mapping[str, object] | None = None,
        branch_label: str | None = None,
        terminate_if_matched: bool = False,
    ) -> _ObservationToken | None:
        if not self.enabled:
            return None
        evaluated = False
        value: object = None

        def evaluate_once() -> object:
            nonlocal evaluated, value
            if not evaluated:
                evaluated = True
                try:
                    value = (
                        evaluated_value
                        if evaluated_value is not _EVALUATED_VALUE
                        else evaluator() if evaluator is not None else False
                    )
                except Exception as error:
                    raise _EvaluationRaised(error) from error
            return value

        try:
            return self.observe_condition(
                invocation,
                rule_id,
                evaluate_once if evaluated_value is _EVALUATED_VALUE else None,
                evaluated_value=evaluated_value,
                condition_inputs=condition_inputs,
                branch_label=branch_label,
                terminate_if_matched=terminate_if_matched,
            )
        except _EvaluationRaised as error:
            raise error.original
        except Exception as error:
            self._mark_collector_failure("observe_condition", error)
            try:
                fallback_value = evaluate_once()
            except _EvaluationRaised as evaluation_error:
                raise evaluation_error.original
            return _ObservationToken(
                self,
                invocation,
                _RULE_BY_ID.get(rule_id),
                value=fallback_value,
                matched=bool(fallback_value),
                recorded=False,
                branch_label=branch_label,
                terminate_if_matched=terminate_if_matched,
            )

    def safe_observe_value(
        self,
        invocation: InvocationTrace | None,
        rule_id: str,
        evaluator: Callable[[], object] | None,
        *,
        evaluated_value: object = _EVALUATED_VALUE,
        matcher: Callable[[object], object] = bool,
        condition_inputs: Callable[[], Mapping[str, object]] | Mapping[str, object] | None = None,
        branch_label: str | None = None,
    ) -> _ObservationToken | None:
        if not self.enabled:
            return None
        evaluated = False
        value: object = None
        matched_evaluated = False
        matched_value: bool | None = None

        def evaluate_once() -> object:
            nonlocal evaluated, value
            if not evaluated:
                evaluated = True
                try:
                    value = (
                        evaluated_value
                        if evaluated_value is not _EVALUATED_VALUE
                        else evaluator() if evaluator is not None else None
                    )
                except Exception as error:
                    raise _EvaluationRaised(error) from error
            return value

        def match_once(candidate: object) -> bool:
            nonlocal matched_evaluated, matched_value
            if not matched_evaluated:
                matched_evaluated = True
                matched_value = bool(matcher(candidate))
            return bool(matched_value)

        try:
            return self.observe_value(
                invocation,
                rule_id,
                evaluate_once if evaluated_value is _EVALUATED_VALUE else None,
                evaluated_value=evaluated_value,
                matcher=match_once,
                condition_inputs=condition_inputs,
                branch_label=branch_label,
            )
        except _EvaluationRaised as error:
            raise error.original
        except Exception as error:
            self._mark_collector_failure("observe_value", error)
            try:
                fallback_value = evaluate_once()
            except _EvaluationRaised as evaluation_error:
                raise evaluation_error.original
            try:
                fallback_matched = match_once(fallback_value)
            except Exception as matcher_error:
                self._mark_collector_failure("observe_value", matcher_error)
                fallback_matched = bool(fallback_value)
            return _ObservationToken(
                self,
                invocation,
                _RULE_BY_ID.get(rule_id),
                value=fallback_value,
                matched=fallback_matched,
                recorded=False,
                branch_label=branch_label,
            )

    def safe_begin_invocation(self, function: str) -> InvocationTrace | None:
        if not self.enabled:
            return None
        return self._safe_call("begin_invocation", lambda: self.begin_invocation(function))  # type: ignore[return-value]

    def safe_end_invocation(
        self,
        invocation: InvocationTrace | None,
        *,
        termination_kind: str | None = None,
        termination_rule_id: str | None = None,
        termination_sequence: int | None = None,
    ) -> None:
        if not self.enabled or invocation is None:
            return
        self._safe_call(
            "end_invocation",
            lambda: self.end_invocation(
                invocation,
                termination_kind=termination_kind,
                termination_rule_id=termination_rule_id,
                termination_sequence=termination_sequence,
            ),
        )

    @property
    def events(self) -> list[RuntimeEvent]:
        return self._events if self.enabled else []

    @property
    def invocations(self) -> list[InvocationTrace]:
        return self._invocations if self.enabled else []

    def start_trace(self, *, classification_inputs: dict[str, object], initial_state: dict[str, object]) -> None:
        if not self.enabled:
            return
        self._classification_inputs = dict(classification_inputs)
        try:
            self.capture_initial_state(initial_state)
        except Exception as error:
            raise _ObservationOperationFailure("capture_initial_state", error) from error

    def update_classification_inputs(self, **values: object) -> None:
        if self.enabled:
            self._classification_inputs.update(values)

    def record_invocation_exception(self, invocation: InvocationTrace | None) -> None:
        """Record a sanitized raise event after the classifier has raised.

        The classifier owns evaluation and control flow.  This method only
        maps the already-raised traceback to the nearest catalogued rule in
        the active function so the observed trace preserves the exception
        termination without re-running the condition.
        """
        if not self.enabled or invocation is None:
            return
        traceback = sys.exc_info()[2]
        candidates: list[tuple[int, RuleMetadata]] = []
        while traceback is not None:
            frame = traceback.tb_frame
            frame_function = frame.f_code.co_name
            frame_line = traceback.tb_lineno
            for rule_id in _RULE_IDS_BY_FUNCTION.get(invocation.function, ()):
                rule = _RULE_BY_ID[rule_id]
                locator = rule.source_code_locator
                if locator.get("function") != frame_function:
                    continue
                line_start = int(locator.get("line_start", 0))
                candidates.append((abs(line_start - frame_line), rule))
            traceback = traceback.tb_next
        if not candidates:
            return
        _, rule = min(candidates, key=lambda item: (item[0], item[1].rule_id))
        self._record_exception_event(
            invocation=invocation,
            rule=rule,
            condition_inputs={},
            branch_label=f"{invocation.function}.{rule.rule_id.rsplit('.', 1)[-1]}",
        )

    def begin_invocation(self, function: str) -> InvocationTrace | None:
        if not self.enabled:
            return None
        parent = self.invocation_stack[-1] if self.invocation_stack else None
        parent_id = parent.invocation_id if parent else None
        key = (parent_id, function)
        call_ordinal = self._call_ordinals.get(key, 0) + 1
        self._call_ordinals[key] = call_ordinal
        invocation_id = f"{function}#{call_ordinal}" if parent is None else f"{parent_id}/{function}#{call_ordinal}"
        started_sequence = self._reserve_sequence()
        invocation = InvocationTrace(
            invocation_id=invocation_id,
            function=function,
            parent_invocation_id=parent_id,
            call_ordinal=call_ordinal,
            started_sequence=started_sequence,
            catalog_rule_ids=list(_RULE_IDS_BY_FUNCTION.get(function, [])),
        )
        self.invocation_stack.append(invocation)
        self._invocations.append(invocation)
        return invocation

    def _refresh_invocation_coverage(self, invocation: InvocationTrace) -> None:
        """Recompute observed/unknown rule partitions without closing an interval."""
        reached = [
            event.rule_id
            for event in self._events
            if event.invocation_id == invocation.invocation_id
        ]
        invocation.reached_rule_ids = list(dict.fromkeys(reached))
        missing = [
            rule_id
            for rule_id in invocation.catalog_rule_ids
            if rule_id not in invocation.reached_rule_ids
        ]
        complete = (
            invocation.interval_complete
            and self.trace_status in {"active", "complete"}
            and invocation.termination_kind != "raise"
        )
        invocation.coverage_complete = complete
        invocation.invocation_coverage_complete = complete
        invocation.coverage_status = "complete" if complete else "partial"
        if complete:
            invocation.known_unreached_rule_ids = missing
            invocation.unreached_rule_ids = list(missing)
            invocation.unknown_rule_ids = []
        else:
            invocation.known_unreached_rule_ids = []
            invocation.unreached_rule_ids = []
            invocation.unknown_rule_ids = missing

    def _finalize_invocation(
        self,
        invocation: InvocationTrace,
        *,
        termination_kind: str | None = None,
        termination_rule_id: str | None = None,
        termination_sequence: int | None = None,
    ) -> None:
        if invocation.completed_sequence is not None:
            return
        if not self.invocation_stack or self.invocation_stack[-1] is not invocation:
            self._mark_collector_failure(
                "end_invocation",
                RuntimeError("invocation stack order is inconsistent"),
                invocation_id=invocation.invocation_id,
            )
            return
        self.invocation_stack.pop()
        if invocation.termination_kind is None:
            invocation.termination_kind = termination_kind or "return"
        if termination_rule_id is not None:
            invocation.termination_rule_id = termination_rule_id
        if termination_sequence is not None:
            invocation.termination_sequence = termination_sequence
        invocation.completed_sequence = self._reserve_sequence()
        invocation.interval_complete = True
        invocation.last_reliable_sequence = self.last_reliable_sequence
        invocation.observation_failure_sequence = self.observation_failure_sequence
        self._refresh_invocation_coverage(invocation)

    def end_invocation(
        self,
        invocation: InvocationTrace | None,
        *,
        termination_kind: str | None = None,
        termination_rule_id: str | None = None,
        termination_sequence: int | None = None,
    ) -> None:
        if not self.enabled or invocation is None:
            return
        if self.invocation_stack and self.invocation_stack[-1] is not invocation:
            raise RuntimeError("cannot end an invocation while a child is active")
        self._finalize_invocation(
            invocation,
            termination_kind=termination_kind,
            termination_rule_id=termination_rule_id,
            termination_sequence=termination_sequence,
        )

    def abort_active_invocations(self) -> None:
        if not self.enabled:
            return
        if self.trace_status == "active":
            self.trace_status = "partial"
        while self.invocation_stack:
            invocation = self.invocation_stack[-1]
            if invocation.termination_kind is None:
                invocation.termination_kind = "raise"
            self.safe_end_invocation(invocation)

    def _minimal_trace(self, *, trace_status: str = "failed") -> RuntimeTrace:
        return RuntimeTrace(
            classification_inputs=dict(self._classification_inputs),
            initial_state=dict(self._initial_state),
            final_state=dict(self._final_state) if self._final_state_ready else {"availability": "unavailable"},
            events=list(self._events),
            invocations=list(self._invocations),
            effective_now_utc=self.effective_now_utc,
            trace_status=trace_status,
            observer_enabled=True,
            coverage_complete=False,
            last_reliable_sequence=self.last_reliable_sequence,
            observation_failure_sequence=self.observation_failure_sequence,
            observation_errors=list(self.observation_errors),
            active_invocation_id=self.invocation_stack[-1].invocation_id if self.invocation_stack else None,
            collector_state=self.collector_state,
        )


    def safe_build_trace(self, *, final_state: dict[str, object]) -> RuntimeTrace:
        if not self.enabled:
            return self._minimal_trace(trace_status="failed")
        self.safe_capture_final_state(final_state)
        if not self._final_state_ready:
            return self._minimal_trace(trace_status="failed")
        before_errors = len(self.observation_errors)
        try:
            trace = self.build_trace(final_state=final_state)
        except _ObservationOperationFailure as failure:
            self._mark_collector_failure(failure.operation, failure.original)
            return self._minimal_trace(trace_status="failed")
        except Exception as error:
            self._mark_collector_failure("build_trace", error)
            return self._minimal_trace(trace_status="failed")
        if not isinstance(trace, RuntimeTrace):
            if len(self.observation_errors) == before_errors:
                self._mark_collector_failure("build_trace", RuntimeError("invalid trace result"))
            return self._minimal_trace(trace_status="failed")
        return trace

    def _mark_collector_failure(
        self,
        operation: str,
        error: Exception,
        *,
        sequence: int | None = None,
        invocation_id: str | None = None,
    ) -> None:
        if sequence is None:
            sequence = self._reserve_sequence()
        if self.observation_failure_sequence is None:
            self.observation_failure_sequence = sequence
        self.observation_errors.append(
            ObservationError(
                error_type="trace_collection_error",
                operation=operation,
                error_code="observation_operation_failed",
                exception_type=type(error).__name__,
                sequence=sequence,
                invocation_id=invocation_id,
            )
        )
        self.collector_state = "failed"
        self.trace_status = "partial"
        self._recording_enabled = False

    def _safe_inputs(self, condition_inputs: Callable[[], Mapping[str, object]] | Mapping[str, object] | None) -> dict[str, object]:
        if condition_inputs is None:
            return {}
        try:
            values = condition_inputs() if callable(condition_inputs) else condition_inputs
            return {str(key): values[key] for key in sorted(values)}
        except Exception as error:
            self._mark_collector_failure("condition_inputs", error)
            return {}

    def observe_condition(
        self,
        invocation: InvocationTrace | None,
        rule_id: str,
        evaluator: Callable[[], object] | None,
        *,
        evaluated_value: object = _EVALUATED_VALUE,
        condition_inputs: Callable[[], Mapping[str, object]] | Mapping[str, object] | None = None,
        branch_label: str | None = None,
        terminate_if_matched: bool = False,
    ) -> _ObservationToken | None:
        if not self.enabled:
            return None
        rule = _RULE_BY_ID[rule_id]
        resolved_branch_label = branch_label or (
            f"{invocation.function if invocation is not None else rule.function}."
            f"{rule_id.rsplit('.', 1)[-1]}"
        )
        try:
            value = evaluator() if evaluated_value is _EVALUATED_VALUE and evaluator is not None else evaluated_value
            matched = bool(value)
        except Exception:
            if invocation is not None:
                self._record_exception_event(
                    invocation=invocation,
                    rule=rule,
                    condition_inputs=self._safe_inputs(condition_inputs),
                    branch_label=resolved_branch_label,
                )
            raise
        return _ObservationToken(
            self,
            invocation,
            rule,
            value=value,
            matched=matched,
            recorded=self._recording_enabled and invocation is not None,
            condition_inputs=self._safe_inputs(condition_inputs) if self._recording_enabled else {},
            branch_label=resolved_branch_label,
            terminate_if_matched=terminate_if_matched,
        )

    def observe_value(
        self,
        invocation: InvocationTrace | None,
        rule_id: str,
        evaluator: Callable[[], object] | None,
        *,
        evaluated_value: object = _EVALUATED_VALUE,
        matcher: Callable[[object], object] = bool,
        condition_inputs: Callable[[], Mapping[str, object]] | Mapping[str, object] | None = None,
        branch_label: str | None = None,
    ) -> _ObservationToken | None:
        if not self.enabled:
            return None
        rule = _RULE_BY_ID[rule_id]
        resolved_branch_label = branch_label or (
            f"{invocation.function if invocation is not None else rule.function}."
            f"{rule_id.rsplit('.', 1)[-1]}"
        )
        try:
            value = evaluator() if evaluated_value is _EVALUATED_VALUE and evaluator is not None else evaluated_value
            matched = bool(matcher(value))
        except Exception:
            if invocation is not None:
                self._record_exception_event(
                    invocation=invocation,
                    rule=rule,
                    condition_inputs=self._safe_inputs(condition_inputs),
                    branch_label=resolved_branch_label,
                )
            raise
        return _ObservationToken(
            self,
            invocation,
            rule,
            value=value,
            matched=matched,
            recorded=self._recording_enabled and invocation is not None,
            condition_inputs=self._safe_inputs(condition_inputs) if self._recording_enabled else {},
            branch_label=resolved_branch_label,
        )

    def _commit_event(
        self,
        *,
        invocation: InvocationTrace,
        rule: RuleMetadata,
        evaluated: bool,
        matched: bool | None,
        terminated: bool,
        termination_kind: str | None,
        condition_inputs: dict[str, object],
        state_changes: dict[str, object],
        reason_codes_added: Sequence[str],
        alerts_added: Sequence[str],
        limitations_added: Sequence[str],
        branch_label: str | None,
    ) -> None:
        if not self.enabled or not self._recording_enabled:
            return
        sequence = self._reserve_sequence()
        try:
            if rule.rule_id not in invocation.catalog_rule_ids:
                raise RuntimeError("event rule is outside invocation catalog")
            if rule.function != invocation.function:
                raise RuntimeError("event rule owner does not match invocation")
            if sequence in self._event_sequences:
                raise RuntimeError("event sequence was already recorded")
        except Exception as error:
            self._mark_collector_failure(
                "record_event",
                error,
                sequence=sequence,
                invocation_id=invocation.invocation_id,
            )
            return
        event = RuntimeEvent(
            sequence=sequence,
            invocation_id=invocation.invocation_id,
            rule_id=rule.rule_id,
            reached=True,
            evaluated=evaluated,
            matched=matched,
            terminated=terminated,
            termination_kind=termination_kind,
            axis=rule.axis,
            effect_type=rule.effect_type,
            evidence_keys=list(rule.evidence_keys),
            condition_inputs=condition_inputs,
            state_changes=state_changes,
            reason_codes_added=list(reason_codes_added),
            alerts_added=list(alerts_added),
            limitations_added=list(limitations_added),
            branch_label=branch_label,
        )
        try:
            recorder = getattr(self.collector, "record_event")
            recorder(event)
        except Exception as error:
            self._mark_collector_failure(
                "record_event",
                error,
                sequence=sequence,
                invocation_id=invocation.invocation_id,
            )
            return
        self._events.append(event)
        self._event_sequences.add(sequence)
        self.last_reliable_sequence = sequence
        if terminated:
            invocation.termination_kind = termination_kind or "return"
            invocation.termination_rule_id = rule.rule_id
            invocation.termination_sequence = sequence

    def _record_exception_event(
        self,
        *,
        invocation: InvocationTrace,
        rule: RuleMetadata,
        condition_inputs: dict[str, object],
        branch_label: str | None,
    ) -> None:
        self._commit_event(
            invocation=invocation,
            rule=rule,
            evaluated=False,
            matched=None,
            terminated=True,
            termination_kind="raise",
            condition_inputs=condition_inputs,
            state_changes={},
            reason_codes_added=(),
            alerts_added=(),
            limitations_added=(),
            branch_label=branch_label,
        )

    def build_trace(self, *, final_state: dict[str, object]) -> RuntimeTrace | None:
        if not self.enabled:
            return None
        if not self._final_state_ready:
            try:
                self.capture_final_state(final_state)
            except Exception as error:
                raise _ObservationOperationFailure("capture_final_state", error) from error
        if not self._final_state_ready:
            return self._minimal_trace(trace_status="failed")
        complete = (
            self.trace_status == "active"
            and not self.invocation_stack
            and all(invocation.interval_complete for invocation in self._invocations)
        )
        for invocation in self._invocations:
            self._refresh_invocation_coverage(invocation)
        trace_status = "complete" if complete else ("partial" if self.trace_status == "active" else self.trace_status)
        return RuntimeTrace(
            classification_inputs=dict(self._classification_inputs),
            initial_state=dict(self._initial_state),
            final_state=dict(self._final_state),
            events=list(self._events),
            invocations=list(self._invocations),
            effective_now_utc=self.effective_now_utc,
            trace_status=trace_status,
            observer_enabled=True,
            coverage_complete=complete,
            last_reliable_sequence=self.last_reliable_sequence,
            observation_failure_sequence=self.observation_failure_sequence,
            observation_errors=list(self.observation_errors),
            active_invocation_id=self.invocation_stack[-1].invocation_id if self.invocation_stack else None,
            collector_state=self.collector_state,
        )


@contextmanager
def observed_invocation(context: ObservationContext, function: str) -> Iterator[InvocationTrace | None]:
    invocation = context.safe_begin_invocation(function)
    try:
        yield invocation
    except Exception:
        if invocation is not None and invocation.termination_kind is None:
            context.record_invocation_exception(invocation)
            invocation.termination_kind = "raise"
        context.safe_end_invocation(invocation)
        raise
    else:
        context.safe_end_invocation(invocation)


def observed_helper(function: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: object, observation_context: ObservationContext | None = None, **kwargs: object) -> object:
            if observation_context is None or not observation_context.enabled:
                return func(*args, observation_context=None, **kwargs)
            with observed_invocation(observation_context, function):
                return func(*args, observation_context=observation_context, **kwargs)

        return wrapper

    return decorator


def observe_condition(
    context: ObservationContext | None,
    invocation: InvocationTrace | None,
    rule_id: str,
    evaluator: Callable[[], object] | None,
    *,
    evaluated_value: object = _EVALUATED_VALUE,
    condition_inputs: Callable[[], Mapping[str, object]] | Mapping[str, object] | None = None,
    branch_label: str | None = None,
    terminate_if_matched: bool = False,
) -> _ObservationToken | None:
    if context is None or not context.enabled:
        return None
    invocation = invocation if invocation is not None else (
        context.invocation_stack[-1] if context.invocation_stack else None
    )
    return context.safe_observe_condition(
        invocation,
        rule_id,
        evaluator,
        evaluated_value=evaluated_value,
        condition_inputs=condition_inputs,
        branch_label=branch_label,
        terminate_if_matched=terminate_if_matched,
    )


def observe_value(
    context: ObservationContext | None,
    invocation: InvocationTrace | None,
    rule_id: str,
    evaluator: Callable[[], object] | None,
    *,
    evaluated_value: object = _EVALUATED_VALUE,
    matcher: Callable[[object], object] = bool,
    condition_inputs: Callable[[], Mapping[str, object]] | Mapping[str, object] | None = None,
    branch_label: str | None = None,
) -> _ObservationToken | None:
    if context is None or not context.enabled:
        return None
    invocation = invocation if invocation is not None else (
        context.invocation_stack[-1] if context.invocation_stack else None
    )
    return context.safe_observe_value(
        invocation,
        rule_id,
        evaluator,
        evaluated_value=evaluated_value,
        matcher=matcher,
        condition_inputs=condition_inputs,
        branch_label=branch_label,
    )


def _canonical_value(value: object) -> object:
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not valid in decision equivalence")
        return {"__float__": value.hex()}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    raise TypeError(f"unsupported AssetDecision value: {type(value).__name__}")


def _serialize_risk_plan(plan: RiskPlan) -> dict[str, object]:
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


def _serialize_backtest_stats(stats: BacktestStats | None) -> dict[str, object] | None:
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


def _serialize_asset_decision_fields(decision: AssetDecision) -> dict[str, object]:
    return {
        "symbol": decision.symbol,
        "asset_type": decision.asset_type,
        "decision": decision.decision,
        "investment_quality_score": decision.investment_quality_score,
        "swing_trade_score": decision.swing_trade_score,
        "risk_plan": _serialize_risk_plan(decision.risk_plan),
        "alerts": decision.alerts,
        "limitations": decision.limitations,
        "thesis": decision.thesis,
        "metrics_summary": decision.metrics_summary,
        "ideal_entry": decision.ideal_entry,
        "alternative_entry": decision.alternative_entry,
        "hold_suggestion": decision.hold_suggestion,
        "backtest_stats": _serialize_backtest_stats(decision.backtest_stats),
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


def canonical_asset_decision_bytes(decision: AssetDecision) -> bytes:
    """Return the explicit, lossless equivalence serialization of a decision."""

    payload = _canonical_value(_serialize_asset_decision_fields(decision))
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def asset_decision_sha256(decision: AssetDecision) -> str:
    return hashlib.sha256(canonical_asset_decision_bytes(decision)).hexdigest()


__all__ = [
    "InvocationTrace",
    "ObservationContext",
    "ObservationError",
    "RULE_CATALOG",
    "RuleMetadata",
    "RuntimeEvent",
    "RuntimeTrace",
    "asset_decision_sha256",
    "canonical_asset_decision_bytes",
    "observe_condition",
    "observe_value",
    "observed_helper",
    "observed_invocation",
    "validate_rule_catalog",
    "validate_runtime_trace",
]
