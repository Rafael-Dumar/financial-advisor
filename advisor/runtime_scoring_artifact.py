from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import math
import os
import re
import tempfile
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from advisor.models import AssetDecision
from advisor.runtime_scoring_observability import (
    InvocationTrace,
    ObservationError,
    RuleMetadata,
    RuntimeEvent,
    RuntimeTrace,
    asset_decision_sha256,
    canonical_asset_decision_bytes,
)


SCHEMA_VERSION = "1.0"
RULE_CATALOG_VERSION = "1.0"
DEFAULT_SOFT_BUDGET_BYTES = 25 * 1024 * 1024
DEFAULT_HARD_BUDGET_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_DECOMPRESSED_MEMBER_BYTES = 100 * 1024 * 1024
DEFAULT_COMPRESSLEVEL = 9
_ARTIFACT_NAME = "scoring-runtime-trace.json.gz"
_INDEX_NAME = "scoring-runtime-trace.index.json"
_PART_NAME = "scoring-runtime-trace.part-{number:04d}.json.gz"
SCHEMA_1_ENUM_DOMAINS = MappingProxyType(
    {
        "artifact_status": frozenset({"complete", "partial", "failed"}),
        "trace_status": frozenset({"complete", "partial", "failed"}),
        "asset_type": frozenset({"stock", "crypto"}),
        "decision": frozenset(
            {"tradeable", "watch_buy", "technical_unvalidated", "wait", "avoid", "blocked"}
        ),
        "collector_state": frozenset({"idle", "disabled", "active", "failed"}),
        "termination_kind": frozenset({"return", "raise"}),
        "branch_kind": frozenset({"if", "elif", "ifexp"}),
        "serialization_status": frozenset({"complete", "error"}),
        "classification_status": frozenset({"completed", "failed", "unavailable"}),
        "decision_status": frozenset({"available", "unavailable"}),
        "schedule": frozenset({"main", "close", "nightly"}),
        "coverage_status": frozenset({"active", "partial", "complete"}),
        "axis": frozenset({"decision", "confidence", "quality", "risk", "other"}),
        "effect_type": frozenset(
            {"adjustment", "annotation", "base", "cap", "control_flow", "override"}
        ),
        "runtime_sha_status": frozenset({"available", "unavailable"}),
        "schema_validation_status": frozenset({"valid"}),
        "error_code": frozenset(
            {
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
            }
        ),
    }
)
_RECOVERABLE_STATUS_ERROR_CODES = frozenset(
    {
        "asset_decision_serialization_error",
        "collector_error",
        "observation_operation_failed",
        "serialization_error",
        "trace_serialization_error",
    }
)
_FATAL_STATUS_ERROR_CODES = frozenset({"artifact_error", "single_asset_hard_budget_exceeded"})
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_FORBIDDEN_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "auth_headers",
    "cookie",
    "cookies",
    "environment",
    "environment_variables",
    "headers",
    "provider_payload",
    "raw_payload",
    "request_dump",
    "stack_trace",
    "token",
    "tokens",
}
_RUN_METADATA_KEYS = {
    "asset_count",
    "report_date",
    "run_id",
    "runtime_sha",
    "runtime_sha_status",
    "schedule",
    "source_sha",
    "timezone",
}
_ARTIFACT_ROOT_KEYS = {
    "schema_version",
    "trace_schema_version",
    "artifact_status",
    "run_metadata",
    "rule_catalog_version",
    "rule_catalog_hash",
    "rule_catalog_entry_count",
    "rule_catalog",
    "assets",
    "errors",
    "integrity",
}
_INDEX_ROOT_KEYS = (_ARTIFACT_ROOT_KEYS - {"assets"}) | {"parts", "failed_assets"}
_PART_ROOT_KEYS = {
    "schema_version",
    "trace_schema_version",
    "artifact_status",
    "run_metadata",
    "rule_catalog_hash",
    "rule_catalog_reference",
    "part_number",
    "assets",
    "errors",
    "integrity",
}
_SINGLE_INTEGRITY_KEYS = {
    "artifact_payload_hash",
    "artifact_status",
    "asset_count",
    "decision_counts",
    "trace_hash",
    "source_sha",
    "runtime_sha",
    "schema_validation_status",
    "gzip_deterministic",
    "soft_budget_bytes",
    "hard_budget_bytes",
    "chunk_count",
}
_INDEX_INTEGRITY_KEYS = {
    "artifact_payload_hash",
    "artifact_status",
    "asset_count",
    "chunk_count",
    "trace_hash",
    "soft_budget_bytes",
    "hard_budget_bytes",
}
_PART_INTEGRITY_KEYS = {
    "artifact_payload_hash",
    "artifact_status",
    "asset_count",
    "gzip_deterministic",
    "trace_hash",
}
_ASSET_KEYS = {
    "symbol",
    "asset_type",
    "universe_origin",
    "serialization_status",
    "serialized_asset_decision",
    "serialized_asset_decision_hash",
    "trace_hash",
    "trace_status",
    "trace",
    "runtime_metadata",
    "errors",
}
_FAILED_ASSET_KEYS = _ASSET_KEYS | {"decision_status"}
_TRACE_KEYS = {
    "trace_schema_version",
    "trace_id",
    "rule_catalog_hash",
    "source_sha",
    "runtime_sha",
    "report_date",
    "schedule",
    "symbol",
    "asset_type",
    "universe_origin",
    "effective_now_utc",
    "classification_inputs",
    "initial_state",
    "final_state",
    "events",
    "invocations",
    "trace_status",
    "observer_enabled",
    "coverage_complete",
    "last_reliable_sequence",
    "last_persisted_event_sequence",
    "observation_failure_sequence",
    "active_invocation_id",
    "collector_state",
    "observation_errors",
    "classification",
}
_RUNTIME_METADATA_KEYS = {
    "trace_started_at",
    "trace_completed_at",
    "duration",
    "local_path",
    "classification_status",
    "serialization_status",
    "exception_type",
}
_EVENT_KEYS = {
    "sequence",
    "invocation_id",
    "rule_id",
    "reached",
    "evaluated",
    "matched",
    "terminated",
    "termination_kind",
    "axis",
    "effect_type",
    "evidence_keys",
    "condition_inputs",
    "state_changes",
    "reason_codes_added",
    "alerts_added",
    "limitations_added",
    "branch_label",
}
_INVOCATION_KEYS = {
    "invocation_id",
    "function",
    "parent_invocation_id",
    "call_ordinal",
    "started_sequence",
    "completed_sequence",
    "termination_kind",
    "termination_rule_id",
    "termination_sequence",
    "coverage_status",
    "interval_complete",
    "coverage_complete",
    "invocation_coverage_complete",
    "last_reliable_sequence",
    "observation_failure_sequence",
    "catalog_rule_ids",
    "reached_rule_ids",
    "known_unreached_rule_ids",
    "unreached_rule_ids",
    "unknown_rule_ids",
}
_ERROR_KEYS = {
    "error_type",
    "operation",
    "error_code",
    "exception_type",
    "sequence",
    "invocation_id",
    "symbol",
    "warning",
}
_PART_DESCRIPTOR_KEYS = {
    "part_number",
    "filename",
    "sha256",
    "compressed_size_bytes",
    "uncompressed_size_bytes",
    "symbols",
}
_DECISION_KEYS = {
    "symbol",
    "asset_type",
    "decision",
    "investment_quality_score",
    "swing_trade_score",
    "risk_plan",
    "alerts",
    "limitations",
    "thesis",
    "metrics_summary",
    "ideal_entry",
    "alternative_entry",
    "hold_suggestion",
    "backtest_stats",
    "sample_quality",
    "reason_codes",
    "data_quality",
    "missing_data_severity",
    "news_summary",
    "data_source",
    "data_timestamp",
    "cache_age_seconds",
    "bucket",
    "market_session",
    "last_price_timestamp",
    "provider",
    "is_stale",
    "stale_reason",
    "event_check_status",
    "news_status",
    "macro_regime",
    "macro_status",
    "thesis_status",
    "data_quality_score",
    "decision_confidence_score",
    "relative_strength_vs_spy",
    "relative_strength_vs_qqq",
    "relative_strength_vs_sector",
    "sector_benchmark",
    "short_setup_score",
    "squeeze_risk",
    "gap_risk",
    "borrow_data_available",
    "short_status",
    "universe_origin",
}
_RISK_PLAN_KEYS = {
    "entry",
    "stop",
    "target_2r",
    "target_3r",
    "per_unit_risk",
    "risk_amount",
    "risk_fraction",
    "max_position_units",
    "max_position_value",
    "risk_reward_2r",
    "alerts",
    "position_size_display",
}
_BACKTEST_KEYS = {
    "sample_size",
    "win_rate_2r",
    "win_rate_3r",
    "median_days_to_2r",
    "median_days_to_3r",
    "expected_value_r",
    "avg_win_r",
    "avg_loss_r",
    "setup_quality",
    "max_drawdown_r",
    "period_start",
    "period_end",
    "benchmark_comparison",
    "warnings",
}
_FLOAT_HEX_TEXT = re.compile(r"[-+]?0x[0-9a-f]+(?:\.[0-9a-f]*)?p[-+]?\d+\Z")


class ArtifactValidationError(ValueError):
    """Raised when a local runtime scoring artifact is not auditable."""


class ArtifactSecurityError(ArtifactValidationError):
    """Raised when a payload attempts to cross the artifact allowlist."""


@dataclass(frozen=True)
class ArtifactAssetInput:
    decision: AssetDecision
    trace: RuntimeTrace
    runtime_metadata: Mapping[str, object] = field(default_factory=dict)
    errors: Sequence[ObservationError | Mapping[str, object]] = field(default_factory=tuple)


@dataclass(frozen=True)
class ArtifactWriteResult:
    artifact_status: str
    mode: str
    output_paths: tuple[Path, ...]
    canonical_json_bytes: bytes
    artifact_payload_hash: str
    artifact_file_hash: str
    part_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactValidationResult:
    artifact_status: str
    mode: str
    symbols: tuple[str, ...]
    artifact_payload_hash: str
    artifact_file_hash: str
    part_hashes: tuple[str, ...] = ()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    text = normalized.isoformat(timespec="microseconds")
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    return text


def _normalize_utc_timestamp(value: object, *, name: str) -> str:
    if isinstance(value, datetime):
        try:
            return _utc_text(value)
        except ValueError as error:
            raise ArtifactValidationError(f"{name} must be timezone-aware") from error
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{name} must be an ISO-8601 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ArtifactValidationError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArtifactValidationError(f"{name} must include an explicit timezone")
    return _utc_text(parsed)


def _validate_relative_path(value: str) -> str:
    if _WINDOWS_ABSOLUTE.match(value) or value.startswith(("\\\\", "//", "/", "\\")):
        raise ArtifactSecurityError("absolute paths are not allowed in deterministic payloads")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if ".." in path.parts:
        raise ArtifactSecurityError("paths above the repository root are not allowed")
    return str(path)


def _looks_like_path_key(key: str) -> bool:
    lowered = key.lower()
    return lowered == "path" or lowered.endswith("_path") or lowered.endswith("_locator")


def _validate_string(value: str, *, key: str | None) -> str:
    if key is not None and _looks_like_path_key(key):
        return _validate_relative_path(value)
    if _WINDOWS_ABSOLUTE.match(value) or value.startswith("\\\\"):
        raise ArtifactSecurityError("personal absolute path is not allowed")
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ArtifactSecurityError("URL credentials, query strings, and fragments are not allowed")
    return value


def _canonical_value(value: object, *, key: str | None = None) -> object:
    if isinstance(value, Enum):
        return _canonical_value(value.value, key=key)
    if isinstance(value, datetime):
        return _utc_text(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not valid in deterministic serialization")
        return {"__float__": value.hex()}
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _validate_string(value, key=key)
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key in sorted(value, key=lambda item: str(item)):
            string_key = str(raw_key)
            if string_key in result:
                raise ValueError(f"duplicate canonical mapping key: {string_key}")
            if string_key.lower() in _FORBIDDEN_KEYS:
                raise ArtifactSecurityError(f"forbidden field in artifact payload: {string_key}")
            result[string_key] = _canonical_value(value[raw_key], key=string_key)
        return result
    raise TypeError(f"unsupported deterministic value: {type(value).__name__}")


def canonical_json_bytes(payload: object) -> bytes:
    canonical = _canonical_value(payload)
    return (
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _embedded_canonical_json_bytes(payload: object) -> bytes:
    """Match canonical serializers that intentionally omit a trailing LF."""

    return canonical_json_bytes(payload).removesuffix(b"\n")


def _gzip_bytes(data: bytes, *, compresslevel: int = DEFAULT_COMPRESSLEVEL) -> bytes:
    target = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=target,
        compresslevel=compresslevel,
        mtime=0,
    ) as compressed:
        compressed.write(data)
    return target.getvalue()


def _decompress_single_gzip_member(
    data: bytes,
    *,
    max_decompressed_member_bytes: int,
    label: str,
) -> bytes:
    if max_decompressed_member_bytes <= 0:
        raise ValueError("maximum decompressed member size must be positive")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = bytearray()
    pending = data
    try:
        while pending:
            remaining = max_decompressed_member_bytes - len(output)
            if remaining < 0:
                raise ArtifactValidationError("decompressed payload exceeds safety limit")
            chunk = decoder.decompress(pending, remaining + 1)
            output.extend(chunk)
            if len(output) > max_decompressed_member_bytes:
                raise ArtifactValidationError("decompressed payload exceeds safety limit")
            if decoder.unused_data:
                if decoder.unused_data.startswith(b"\x1f\x8b"):
                    raise ArtifactValidationError("multiple gzip members are not allowed")
                raise ArtifactValidationError("trailing bytes after gzip member are not allowed")
            pending = decoder.unconsumed_tail
            if not pending:
                break
        if not decoder.eof:
            raise ArtifactValidationError("truncated gzip member")
        tail = decoder.flush()
        output.extend(tail)
        if len(output) > max_decompressed_member_bytes:
            raise ArtifactValidationError("decompressed payload exceeds safety limit")
        if decoder.unused_data:
            if decoder.unused_data.startswith(b"\x1f\x8b"):
                raise ArtifactValidationError("multiple gzip members are not allowed")
            raise ArtifactValidationError("trailing bytes after gzip member are not allowed")
        if decoder.unconsumed_tail:
            raise ArtifactValidationError("gzip stream has unconsumed input")
        return bytes(output)
    except ArtifactValidationError:
        raise
    except zlib.error as error:
        raise ArtifactValidationError(f"{label} is corrupt gzip") from error


def _serialize_error(error: ObservationError | Mapping[str, object], *, symbol: str | None = None) -> dict[str, object]:
    if isinstance(error, ObservationError):
        source: Mapping[str, object] = {
            "error_type": error.error_type,
            "operation": error.operation,
            "error_code": error.error_code,
            "exception_type": error.exception_type,
            "sequence": error.sequence,
            "invocation_id": error.invocation_id,
        }
    elif isinstance(error, Mapping):
        source = error
    else:
        raise TypeError("artifact errors must be ObservationError or mappings")
    allowed = {
        "error_type",
        "operation",
        "error_code",
        "exception_type",
        "sequence",
        "invocation_id",
        "symbol",
        "warning",
    }
    sanitized = {key: source.get(key) for key in sorted(allowed) if key in source}
    if symbol is not None:
        sanitized["symbol"] = symbol
    if "error_code" not in sanitized:
        sanitized["error_code"] = "artifact_error"
    return _canonical_value(sanitized)  # type: ignore[return-value]


def _serialize_event(event: RuntimeEvent) -> dict[str, object]:
    return _canonical_value(
        {
            "sequence": event.sequence,
            "invocation_id": event.invocation_id,
            "rule_id": event.rule_id,
            "reached": event.reached,
            "evaluated": event.evaluated,
            "matched": event.matched,
            "terminated": event.terminated,
            "termination_kind": event.termination_kind,
            "axis": event.axis,
            "effect_type": event.effect_type,
            "evidence_keys": event.evidence_keys,
            "condition_inputs": event.condition_inputs,
            "state_changes": event.state_changes,
            "reason_codes_added": event.reason_codes_added,
            "alerts_added": event.alerts_added,
            "limitations_added": event.limitations_added,
            "branch_label": event.branch_label,
        }
    )  # type: ignore[return-value]


def _serialize_invocation(invocation: InvocationTrace) -> dict[str, object]:
    return _canonical_value(
        {
            "invocation_id": invocation.invocation_id,
            "function": invocation.function,
            "parent_invocation_id": invocation.parent_invocation_id,
            "call_ordinal": invocation.call_ordinal,
            "started_sequence": invocation.started_sequence,
            "completed_sequence": invocation.completed_sequence,
            "termination_kind": invocation.termination_kind,
            "termination_rule_id": invocation.termination_rule_id,
            "termination_sequence": invocation.termination_sequence,
            "coverage_status": invocation.coverage_status,
            "interval_complete": invocation.interval_complete,
            "coverage_complete": invocation.coverage_complete,
            "invocation_coverage_complete": invocation.invocation_coverage_complete,
            "last_reliable_sequence": invocation.last_reliable_sequence,
            "observation_failure_sequence": invocation.observation_failure_sequence,
            "catalog_rule_ids": invocation.catalog_rule_ids,
            "reached_rule_ids": invocation.reached_rule_ids,
            "known_unreached_rule_ids": invocation.known_unreached_rule_ids,
            "unreached_rule_ids": invocation.unreached_rule_ids,
            "unknown_rule_ids": invocation.unknown_rule_ids,
        }
    )  # type: ignore[return-value]


def _validate_trace_information(trace: RuntimeTrace) -> None:
    _schema_enum(trace.trace_status, "trace.trace_status", "trace_status")
    if trace.trace_status == "complete" and not trace.coverage_complete:
        raise ArtifactValidationError("complete trace must claim complete coverage")
    if trace.trace_status != "complete" and not trace.observation_errors:
        raise ArtifactValidationError("partial or failed trace must retain an observation error")
    if trace.trace_status != "complete" and trace.coverage_complete:
        raise ArtifactValidationError("partial or failed trace cannot claim complete coverage")
    invocation_ids = [item.invocation_id for item in trace.invocations]
    if len(invocation_ids) != len(set(invocation_ids)):
        raise ArtifactValidationError("duplicate invocation ID")
    event_sequences = [event.sequence for event in trace.events]
    if len(event_sequences) != len(set(event_sequences)):
        raise ArtifactValidationError("duplicate event sequence")
    invocations = {item.invocation_id: item for item in trace.invocations}
    events_by_invocation: dict[str, list[RuntimeEvent]] = {item: [] for item in invocation_ids}
    for event in trace.events:
        if event.invocation_id not in invocations:
            raise ArtifactValidationError("event references omitted invocation")
        events_by_invocation[event.invocation_id].append(event)
    for invocation in trace.invocations:
        events = events_by_invocation[invocation.invocation_id]
        event_rule_ids = list(dict.fromkeys(event.rule_id for event in sorted(events, key=lambda item: item.sequence)))
        if event_rule_ids != invocation.reached_rule_ids:
            raise ArtifactValidationError("invocation reached rules disagree with retained events")
        if any(event.rule_id not in invocation.catalog_rule_ids for event in events):
            raise ArtifactValidationError("event references a rule outside its invocation catalog")


def serialize_runtime_trace_deterministic(
    trace: RuntimeTrace,
    *,
    decision: AssetDecision,
    run_metadata: Mapping[str, object],
    rule_catalog_hash: str | None = None,
) -> dict[str, object]:
    """Serialize only deterministic trace evidence; runtime metadata is excluded."""

    _validate_trace_information(trace)
    if trace.effective_now_utc is not None and not isinstance(trace.effective_now_utc, datetime):
        raise ArtifactValidationError("trace effective_now_utc must be a datetime or null")
    decision_hash = asset_decision_sha256(decision)
    payload = {
        "trace_schema_version": SCHEMA_VERSION,
        "rule_catalog_hash": rule_catalog_hash,
        "source_sha": run_metadata.get("source_sha"),
        "runtime_sha": run_metadata.get("runtime_sha"),
        "report_date": run_metadata.get("report_date"),
        "schedule": run_metadata.get("schedule"),
        "symbol": decision.symbol,
        "asset_type": decision.asset_type,
        "universe_origin": decision.universe_origin,
        "effective_now_utc": trace.effective_now_utc,
        "classification_inputs": trace.classification_inputs,
        "initial_state": trace.initial_state,
        "final_state": trace.final_state,
        "events": [_serialize_event(event) for event in sorted(trace.events, key=lambda item: item.sequence)],
        "invocations": [
            _serialize_invocation(invocation)
            for invocation in sorted(trace.invocations, key=lambda item: item.started_sequence)
        ],
        "trace_status": trace.trace_status,
        "observer_enabled": trace.observer_enabled,
        "coverage_complete": trace.coverage_complete,
        "last_reliable_sequence": trace.last_reliable_sequence,
        "last_persisted_event_sequence": trace.last_reliable_sequence,
        "observation_failure_sequence": trace.observation_failure_sequence,
        "active_invocation_id": trace.active_invocation_id,
        "collector_state": trace.collector_state,
        "observation_errors": [_serialize_error(error, symbol=decision.symbol) for error in trace.observation_errors],
        "classification": {
            "final_decision": decision.decision,
            "serialized_asset_decision_hash": decision_hash,
        },
    }
    canonical_payload = _canonical_value(payload)
    if not isinstance(canonical_payload, dict):
        raise ArtifactValidationError("deterministic trace payload must be an object")
    canonical_payload["trace_id"] = f"sha256:{_sha256(canonical_json_bytes(canonical_payload))}"
    return canonical_payload


def trace_sha256(
    trace: RuntimeTrace,
    *,
    decision: AssetDecision,
    run_metadata: Mapping[str, object],
    rule_catalog_hash: str | None = None,
) -> str:
    return _sha256(
        canonical_json_bytes(
            serialize_runtime_trace_deterministic(
                trace,
                decision=decision,
                run_metadata=run_metadata,
                rule_catalog_hash=rule_catalog_hash,
            )
        )
    )


def _serialize_rule(rule: RuleMetadata) -> dict[str, object]:
    return _canonical_value(
        {
            "rule_id": rule.rule_id,
            "function": rule.function,
            "source_code_locator": rule.source_code_locator,
            "branch_signature": rule.branch_signature,
            "branch_kind": rule.branch_kind,
            "axis": rule.axis,
            "effect_type": rule.effect_type,
            "evidence_keys": list(rule.evidence_keys),
        }
    )  # type: ignore[return-value]


def _rule_catalog_payload(rule_catalog: Sequence[RuleMetadata]) -> tuple[list[dict[str, object]], str]:
    serialized = [_serialize_rule(rule) for rule in sorted(rule_catalog, key=lambda item: item.rule_id)]
    rule_ids = [item["rule_id"] for item in serialized]
    if len(serialized) != 97 or len(set(rule_ids)) != 97:
        raise ArtifactValidationError("rule catalog must contain 97 unique rule IDs")
    return serialized, _sha256(canonical_json_bytes(serialized))


def _runtime_metadata(source: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(source, Mapping):
        raise ArtifactValidationError("runtime metadata must be a mapping")
    allowed = {
        "trace_started_at",
        "trace_completed_at",
        "duration",
        "local_path",
        "classification_status",
        "serialization_status",
        "exception_type",
    }
    unknown = set(source) - allowed
    if unknown:
        raise ArtifactValidationError(f"unknown runtime metadata fields: {sorted(unknown)}")
    required = {"trace_started_at", "trace_completed_at"}
    if not required <= set(source):
        raise ArtifactValidationError("runtime metadata must include trace start and completion timestamps")
    normalized = {
        key: source[key]
        for key in sorted(allowed)
        if key in source
    }
    normalized["trace_started_at"] = _normalize_utc_timestamp(
        normalized["trace_started_at"], name="runtime_metadata.trace_started_at"
    )
    normalized["trace_completed_at"] = _normalize_utc_timestamp(
        normalized["trace_completed_at"], name="runtime_metadata.trace_completed_at"
    )
    return _canonical_value(normalized)  # type: ignore[return-value]


def _asset_sort_key(asset: ArtifactAssetInput | Mapping[str, object]) -> tuple[str, str, str]:
    if isinstance(asset, ArtifactAssetInput):
        return (asset.decision.symbol, asset.decision.asset_type, asset.decision.universe_origin)
    return (
        str(asset.get("symbol", "")),
        str(asset.get("asset_type", "")),
        str(asset.get("universe_origin", "")),
    )


def _serialize_asset(
    asset: ArtifactAssetInput,
    *,
    run_metadata: Mapping[str, object],
    rule_catalog_hash: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    decision = asset.decision
    errors = [_serialize_error(error, symbol=decision.symbol) for error in asset.errors]
    try:
        decision_bytes = canonical_asset_decision_bytes(decision)
        decision_payload = json.loads(decision_bytes)
        decision_hash = _sha256(decision_bytes)
    except (TypeError, ValueError) as error:
        errors.append(
            _serialize_error(
                {
                    "error_type": "serialization_error",
                    "operation": "serialize_asset_decision",
                    "error_code": "asset_decision_serialization_error",
                    "exception_type": type(error).__name__,
                },
                symbol=decision.symbol,
            )
        )
        return (
            {
                "symbol": decision.symbol,
                "asset_type": decision.asset_type,
                "universe_origin": decision.universe_origin,
                "serialization_status": "error",
                "serialized_asset_decision": None,
                "serialized_asset_decision_hash": None,
                "trace_hash": None,
                "trace_status": "failed",
                "trace": None,
                "runtime_metadata": _runtime_metadata(asset.runtime_metadata),
                "errors": errors,
            },
            errors,
        )
    try:
        deterministic_trace = serialize_runtime_trace_deterministic(
            asset.trace,
            decision=decision,
            run_metadata=run_metadata,
            rule_catalog_hash=rule_catalog_hash,
        )
        serialized_trace_hash = _sha256(canonical_json_bytes(deterministic_trace))
        all_errors = errors + [
            _serialize_error(error, symbol=decision.symbol) for error in asset.trace.observation_errors
        ]
        serialized = {
            "symbol": decision.symbol,
            "asset_type": decision.asset_type,
            "universe_origin": decision.universe_origin,
            "serialization_status": "complete",
            "serialized_asset_decision": decision_payload,
            "serialized_asset_decision_hash": decision_hash,
            "trace_hash": serialized_trace_hash,
            "trace_status": asset.trace.trace_status,
            "trace": deterministic_trace,
            "runtime_metadata": _runtime_metadata(asset.runtime_metadata),
            "errors": all_errors,
        }
        return _canonical_value(serialized), all_errors  # type: ignore[return-value]
    except ArtifactSecurityError:
        raise
    except (ArtifactValidationError, TypeError, ValueError) as error:
        serialization_error = _serialize_error(
            {
                "error_type": "serialization_error",
                "operation": "serialize_runtime_trace",
                "error_code": "trace_serialization_error",
                "exception_type": type(error).__name__,
            },
            symbol=decision.symbol,
        )
        errors.append(serialization_error)
        return (
            _canonical_value(
                {
                    "symbol": decision.symbol,
                    "asset_type": decision.asset_type,
                    "universe_origin": decision.universe_origin,
                    "serialization_status": "error",
                    "serialized_asset_decision": decision_payload,
                    "serialized_asset_decision_hash": decision_hash,
                    "trace_hash": None,
                    "trace_status": "failed",
                    "trace": None,
                    "runtime_metadata": _runtime_metadata(asset.runtime_metadata),
                    "errors": errors,
                }
            ),  # type: ignore[return-value]
            errors,
        )


def _artifact_status(assets: Sequence[Mapping[str, object]]) -> str:
    if not assets:
        return "failed"
    if assets and all(asset.get("serialized_asset_decision_hash") is None for asset in assets):
        return "failed"
    if all(
        asset.get("serialization_status") == "complete"
        and asset.get("trace_status") == "complete"
        and isinstance(asset.get("trace"), Mapping)
        and asset["trace"].get("trace_status") == "complete"  # type: ignore[index]
        for asset in assets
    ):
        return "complete"
    return "partial"


def _derive_artifact_status(
    assets: Sequence[Mapping[str, object]],
    errors: Sequence[Mapping[str, object]] = (),
) -> str:
    """Derive status from validated asset evidence and sanitized errors."""

    status = _artifact_status(assets)
    codes = {error.get("error_code") for error in errors}
    if status != "failed" and codes & _FATAL_STATUS_ERROR_CODES:
        return "failed"
    if status != "failed" and codes & _RECOVERABLE_STATUS_ERROR_CODES:
        return "partial"
    return status


def _derive_index_status(
    *,
    parts: Sequence[Mapping[str, object]],
    failed_assets: Sequence[Mapping[str, object]],
    all_assets: Sequence[Mapping[str, object]],
    errors: Sequence[Mapping[str, object]],
) -> str:
    """Derive the aggregate status without trusting the stored status field."""

    if parts:
        return _derive_artifact_status(all_assets, errors)
    if not failed_assets:
        return "failed"
    if any(
        error.get("error_code") == "single_asset_hard_budget_exceeded"
        for error in errors
    ):
        return "failed"
    if all(asset.get("serialized_asset_decision_hash") is None for asset in failed_assets):
        return "failed"
    return "partial"


def _failed_asset_payload(asset: Mapping[str, object]) -> dict[str, object]:
    preserved = copy.deepcopy(dict(asset))
    available = (
        isinstance(preserved.get("serialized_asset_decision"), Mapping)
        and isinstance(preserved.get("serialized_asset_decision_hash"), str)
    )
    preserved["decision_status"] = "available" if available else "unavailable"
    if not available:
        preserved["serialized_asset_decision"] = None
        preserved["serialized_asset_decision_hash"] = None
    return preserved


def _decision_counts(assets: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for asset in assets:
        serialized = asset.get("serialized_asset_decision")
        if isinstance(serialized, Mapping):
            decision = serialized.get("decision")
            _schema_enum(decision, "integrity.decision_counts.key", "decision")
            if not isinstance(decision, str):
                raise ArtifactValidationError("integrity decision count key must be a decision enum")
            counts[decision] = counts.get(decision, 0) + 1
    return dict(sorted(counts.items()))


def _aggregate_trace_hash(assets: Sequence[Mapping[str, object]]) -> str:
    domains = [
        {
            "symbol": asset.get("symbol"),
            "asset_type": asset.get("asset_type"),
            "universe_origin": asset.get("universe_origin"),
            "trace_hash": asset.get("trace_hash"),
        }
        for asset in sorted(assets, key=_asset_sort_key)
    ]
    return _sha256(canonical_json_bytes(domains))


def _payload_hash(payload: Mapping[str, object], *, field_name: str = "artifact_payload_hash") -> str:
    basis = copy.deepcopy(dict(payload))
    integrity = basis.get("integrity")
    if isinstance(integrity, dict):
        integrity.pop(field_name, None)
    return _sha256(canonical_json_bytes(basis))


def serialize_artifact_presentation(
    *,
    run_metadata: Mapping[str, object],
    catalog: list[dict[str, object]],
    catalog_hash: str,
    assets: list[dict[str, object]],
    errors: list[dict[str, object]],
    soft_budget_bytes: int,
    hard_budget_bytes: int,
    chunk_count: int,
) -> dict[str, object]:
    status = _derive_artifact_status(assets, errors)
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "trace_schema_version": SCHEMA_VERSION,
        "artifact_status": status,
        "run_metadata": _canonical_value(run_metadata),
        "rule_catalog_version": RULE_CATALOG_VERSION,
        "rule_catalog_hash": catalog_hash,
        "rule_catalog_entry_count": len(catalog),
        "rule_catalog": catalog,
        "assets": assets,
        "errors": errors,
        "integrity": {
            "asset_count": len(assets),
            "decision_counts": _decision_counts(assets),
            "trace_hash": _aggregate_trace_hash(assets),
            "source_sha": run_metadata.get("source_sha"),
            "runtime_sha": run_metadata.get("runtime_sha"),
            "schema_validation_status": "valid",
            "artifact_status": status,
            "gzip_deterministic": True,
            "soft_budget_bytes": soft_budget_bytes,
            "hard_budget_bytes": hard_budget_bytes,
            "chunk_count": chunk_count,
        },
    }
    payload["integrity"]["artifact_payload_hash"] = _payload_hash(payload)  # type: ignore[index]
    return payload


def _part_payload(
    *,
    number: int,
    status: str,
    run_metadata: Mapping[str, object],
    catalog_hash: str,
    assets: list[dict[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "trace_schema_version": SCHEMA_VERSION,
        "artifact_status": status,
        "run_metadata": _canonical_value(run_metadata),
        "rule_catalog_hash": catalog_hash,
        "rule_catalog_reference": _INDEX_NAME,
        "part_number": number,
        "assets": assets,
        "errors": [error for asset in assets for error in asset.get("errors", [])],
        "integrity": {
            "asset_count": len(assets),
            "artifact_status": status,
            "gzip_deterministic": True,
            "trace_hash": _aggregate_trace_hash(assets),
        },
    }
    payload["integrity"]["artifact_payload_hash"] = _payload_hash(payload)  # type: ignore[index]
    return payload


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    primary_error: BaseException | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as target:
            temporary = Path(target.name)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        temporary = None
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except BaseException:
                if primary_error is None:
                    raise
                try:
                    setattr(primary_error, "cleanup_status", "failed")
                    setattr(primary_error, "cleanup_error_code", "temporary_unlink_failed")
                except Exception:
                    pass


def _validate_generated_payload(payload: Mapping[str, object], *, include_catalog: bool) -> None:
    """Validate schema and hash domains before any atomic rename publishes bytes."""

    if include_catalog:
        _validate_root_structure(payload)
    else:
        _validate_part_structure(payload)
    if include_catalog:
        catalog_hash = _validate_catalog(payload)
    else:
        catalog_hash = str(payload.get("rule_catalog_hash", ""))
        if not catalog_hash:
            raise ArtifactValidationError("generated part omitted catalog hash")
    assets = payload.get("assets")
    if assets is not None:
        _validate_assets(assets, catalog_hash=catalog_hash, run_metadata=payload["run_metadata"])
        if not isinstance(assets, list):
            raise ArtifactValidationError("generated assets are malformed")
        integrity = payload.get("integrity")
        if not isinstance(integrity, Mapping) or integrity.get("trace_hash") != _aggregate_trace_hash(assets):
            raise ArtifactValidationError("generated aggregate trace hash mismatch")
    _validate_common(payload)


def _chunk_assets(
    assets: list[dict[str, object]],
    *,
    status: str,
    run_metadata: Mapping[str, object],
    catalog_hash: str,
    hard_budget_bytes: int,
    compresslevel: int,
) -> tuple[list[tuple[dict[str, object], bytes]], list[dict[str, object]]]:
    chunks: list[list[dict[str, object]]] = []
    failed: list[dict[str, object]] = []
    current: list[dict[str, object]] = []
    for asset in assets:
        single_payload = _part_payload(
            number=1,
            status=status,
            run_metadata=run_metadata,
            catalog_hash=catalog_hash,
            assets=[asset],
        )
        if len(_gzip_bytes(canonical_json_bytes(single_payload), compresslevel=compresslevel)) > hard_budget_bytes:
            failed.append(asset)
            continue
        candidate = current + [asset]
        candidate_payload = _part_payload(
            number=len(chunks) + 1,
            status=status,
            run_metadata=run_metadata,
            catalog_hash=catalog_hash,
            assets=candidate,
        )
        if current and len(
            _gzip_bytes(canonical_json_bytes(candidate_payload), compresslevel=compresslevel)
        ) > hard_budget_bytes:
            chunks.append(current)
            current = [asset]
        else:
            current = candidate
    if current:
        chunks.append(current)
    serialized_chunks: list[tuple[dict[str, object], bytes]] = []
    for index, chunk in enumerate(chunks, start=1):
        payload = _part_payload(
            number=index,
            status=status,
            run_metadata=run_metadata,
            catalog_hash=catalog_hash,
            assets=chunk,
        )
        compressed = _gzip_bytes(canonical_json_bytes(payload), compresslevel=compresslevel)
        if len(compressed) > hard_budget_bytes:
            raise ArtifactValidationError("deterministic chunk exceeds hard compressed budget")
        serialized_chunks.append((payload, compressed))
    return serialized_chunks, failed


def write_runtime_scoring_artifact(
    output_dir: Path | str,
    *,
    run_metadata: Mapping[str, object],
    rule_catalog: Sequence[RuleMetadata],
    assets: Sequence[ArtifactAssetInput],
    errors: Sequence[ObservationError | Mapping[str, object]] = (),
    soft_budget_bytes: int = DEFAULT_SOFT_BUDGET_BYTES,
    hard_budget_bytes: int = DEFAULT_HARD_BUDGET_BYTES,
    compresslevel: int = DEFAULT_COMPRESSLEVEL,
) -> ArtifactWriteResult:
    """Write a local artifact from already-completed decisions and traces."""

    if soft_budget_bytes <= 0 or hard_budget_bytes <= 0:
        raise ValueError("artifact budgets must be positive")
    if not 0 <= compresslevel <= 9:
        raise ValueError("gzip compresslevel must be between 0 and 9")
    unknown_run_fields = set(run_metadata) - _RUN_METADATA_KEYS
    if unknown_run_fields:
        raise ValueError(f"unknown run metadata fields: {sorted(unknown_run_fields)}")
    if "asset_count" in run_metadata and run_metadata["asset_count"] != len(assets):
        raise ValueError("run metadata asset_count does not match completed inputs")
    run_metadata = dict(run_metadata)
    run_metadata["asset_count"] = len(assets)
    root = Path(output_dir)
    catalog, catalog_hash = _rule_catalog_payload(rule_catalog)
    serialized_assets: list[dict[str, object]] = []
    run_errors = [_serialize_error(error) for error in errors]
    if not assets:
        run_errors.append(
            _serialize_error(
                {
                    "error_type": "artifact_error",
                    "operation": "serialize_artifact",
                    "error_code": "no_assets",
                }
            )
        )
    for asset in sorted(assets, key=_asset_sort_key):
        serialized, asset_errors = _serialize_asset(
            asset,
            run_metadata=run_metadata,
            rule_catalog_hash=catalog_hash,
        )
        serialized_assets.append(serialized)
        run_errors.extend(asset_errors)
    payload = serialize_artifact_presentation(
        run_metadata=run_metadata,
        catalog=catalog,
        catalog_hash=catalog_hash,
        assets=serialized_assets,
        errors=run_errors,
        soft_budget_bytes=soft_budget_bytes,
        hard_budget_bytes=hard_budget_bytes,
        chunk_count=1,
    )
    initial_bytes = canonical_json_bytes(payload)
    if len(initial_bytes) > soft_budget_bytes:
        run_errors.append(
            _serialize_error(
                {
                    "error_type": "budget_warning",
                    "operation": "serialize_artifact",
                    "error_code": "soft_budget_exceeded",
                    "warning": True,
                }
            )
        )
        payload = serialize_artifact_presentation(
            run_metadata=run_metadata,
            catalog=catalog,
            catalog_hash=catalog_hash,
            assets=serialized_assets,
            errors=run_errors,
            soft_budget_bytes=soft_budget_bytes,
            hard_budget_bytes=hard_budget_bytes,
            chunk_count=1,
        )
    payload_bytes = canonical_json_bytes(payload)
    _validate_generated_payload(payload, include_catalog=True)
    compressed = _gzip_bytes(payload_bytes, compresslevel=compresslevel)
    if len(compressed) <= hard_budget_bytes:
        path = root / _ARTIFACT_NAME
        _atomic_write(path, compressed)
        return ArtifactWriteResult(
            artifact_status=str(payload["artifact_status"]),
            mode="single",
            output_paths=(path,),
            canonical_json_bytes=payload_bytes,
            artifact_payload_hash=str(payload["integrity"]["artifact_payload_hash"]),  # type: ignore[index]
            artifact_file_hash=_sha256(compressed),
        )

    status = str(payload["artifact_status"])
    chunks, failed_assets = _chunk_assets(
        serialized_assets,
        status=status,
        run_metadata=run_metadata,
        catalog_hash=catalog_hash,
        hard_budget_bytes=hard_budget_bytes,
        compresslevel=compresslevel,
    )
    if failed_assets or not serialized_assets:
        oversized_keys = {_asset_sort_key(asset) for asset in failed_assets}
        # A fatal oversized asset must not make otherwise serializable assets
        # disappear from the audit surface.  Preserve every serialized asset
        # in the failed index and attach the fatal error only to the offenders.
        failed_assets = [_failed_asset_payload(asset) for asset in serialized_assets]
        failed_errors = run_errors + [
            _serialize_error(
                {
                    "error_type": "serialization_error",
                    "operation": "chunk_asset",
                    "error_code": "single_asset_hard_budget_exceeded",
                    "symbol": asset["symbol"],
                }
            )
            for asset in serialized_assets
            if _asset_sort_key(asset) in oversized_keys
        ]
        failed_index: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "trace_schema_version": SCHEMA_VERSION,
            "artifact_status": "failed",
            "run_metadata": _canonical_value(run_metadata),
            "rule_catalog_version": RULE_CATALOG_VERSION,
            "rule_catalog_hash": catalog_hash,
            "rule_catalog_entry_count": len(catalog),
            "rule_catalog": catalog,
            "parts": [],
            "failed_assets": failed_assets,
            "errors": failed_errors,
            "integrity": {
                "artifact_status": "failed",
                "asset_count": len(serialized_assets),
                "chunk_count": 0,
                "trace_hash": _aggregate_trace_hash(serialized_assets),
                "soft_budget_bytes": soft_budget_bytes,
                "hard_budget_bytes": hard_budget_bytes,
            },
        }
        failed_index["integrity"]["artifact_payload_hash"] = _payload_hash(failed_index)  # type: ignore[index]
        _validate_index_structure(failed_index)
        _validate_catalog(failed_index)
        failed_symbols = _validate_assets(
            failed_assets,
            catalog_hash=catalog_hash,
            failed=True,
            run_metadata=failed_index["run_metadata"],
        )
        if failed_index["integrity"]["trace_hash"] != _aggregate_trace_hash(failed_assets):  # type: ignore[index]
            raise ArtifactValidationError("generated failed index trace hash mismatch")
        expected_status = _derive_index_status(
            parts=(),
            failed_assets=failed_assets,
            all_assets=failed_assets,
            errors=failed_errors,
        )
        if failed_index["artifact_status"] != expected_status:
            raise ArtifactValidationError("generated failed index status mismatch")
        if failed_index["integrity"]["asset_count"] != len(failed_symbols):  # type: ignore[index]
            raise ArtifactValidationError("generated failed index asset count mismatch")
        _validate_common(failed_index)
        index_bytes = canonical_json_bytes(failed_index)
        index_path = root / _INDEX_NAME
        _atomic_write(index_path, index_bytes)
        return ArtifactWriteResult(
            artifact_status="failed",
            mode="failed",
            output_paths=(index_path,),
            canonical_json_bytes=index_bytes,
            artifact_payload_hash=str(failed_index["integrity"]["artifact_payload_hash"]),  # type: ignore[index]
            artifact_file_hash=_sha256(index_bytes),
        )

    part_records: list[dict[str, object]] = []
    part_paths: list[Path] = []
    part_hashes: list[str] = []
    for number, (part_payload, part_bytes) in enumerate(chunks, start=1):
        filename = _PART_NAME.format(number=number)
        sha = _sha256(part_bytes)
        symbols = [str(asset["symbol"]) for asset in part_payload["assets"]]
        part_records.append(
            {
                "part_number": number,
                "filename": filename,
                "sha256": sha,
                "compressed_size_bytes": len(part_bytes),
                "uncompressed_size_bytes": len(canonical_json_bytes(part_payload)),
                "symbols": symbols,
            }
        )
        part_paths.append(root / filename)
        part_hashes.append(sha)
    index: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "trace_schema_version": SCHEMA_VERSION,
        "artifact_status": status,
        "run_metadata": _canonical_value(run_metadata),
        "rule_catalog_version": RULE_CATALOG_VERSION,
        "rule_catalog_hash": catalog_hash,
        "rule_catalog_entry_count": len(catalog),
        "rule_catalog": catalog,
        "parts": part_records,
        "errors": run_errors,
        "integrity": {
            "artifact_status": status,
            "asset_count": len(serialized_assets),
            "chunk_count": len(chunks),
            "trace_hash": _aggregate_trace_hash(serialized_assets),
            "soft_budget_bytes": soft_budget_bytes,
            "hard_budget_bytes": hard_budget_bytes,
        },
    }
    index["integrity"]["artifact_payload_hash"] = _payload_hash(index)  # type: ignore[index]
    _validate_common(index)
    _validate_catalog(index)
    if index["integrity"]["trace_hash"] != _aggregate_trace_hash(serialized_assets):  # type: ignore[index]
        raise ArtifactValidationError("generated chunk index trace hash mismatch")
    for part_payload, _part_bytes in chunks:
        _validate_generated_payload(part_payload, include_catalog=False)
    index_bytes = canonical_json_bytes(index)
    index_path = root / _INDEX_NAME
    written: list[Path] = []
    try:
        for path, (_part_payload_value, part_bytes) in zip(part_paths, chunks):
            _atomic_write(path, part_bytes)
            written.append(path)
        _atomic_write(index_path, index_bytes)
    except BaseException as error:
        for path in written:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except BaseException:
                try:
                    setattr(error, "cleanup_status", "failed")
                    setattr(error, "cleanup_error_code", "part_unlink_failed")
                except Exception:
                    pass
        raise
    return ArtifactWriteResult(
        artifact_status=status,
        mode="chunked",
        output_paths=(index_path, *part_paths),
        canonical_json_bytes=index_bytes,
        artifact_payload_hash=str(index["integrity"]["artifact_payload_hash"]),  # type: ignore[index]
        artifact_file_hash=_sha256(index_bytes),
        part_hashes=tuple(part_hashes),
    )


def _strict_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ArtifactValidationError(f"non-finite JSON constant is not allowed: {value}")


def _load_canonical_json(data: bytes, *, label: str) -> dict[str, object]:
    try:
        payload = json.loads(
            data,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactValidationError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ArtifactValidationError(f"{label} root must be an object")
    try:
        expected = canonical_json_bytes(payload)
    except (TypeError, ValueError) as error:
        raise ArtifactValidationError(f"{label} contains invalid canonical values") from error
    if data != expected:
        raise ArtifactValidationError(f"{label} is not canonical JSON")
    return payload


def _read_gzip(
    path: Path,
    *,
    max_decompressed_member_bytes: int = DEFAULT_MAX_DECOMPRESSED_MEMBER_BYTES,
) -> tuple[dict[str, object], bytes, bytes]:
    data = path.read_bytes()
    if len(data) < 10 or data[:2] != b"\x1f\x8b":
        raise ArtifactValidationError("artifact is not gzip")
    if int.from_bytes(data[4:8], "little") != 0:
        raise ArtifactValidationError("gzip mtime is not fixed")
    if data[3] & 0x08:
        raise ArtifactValidationError("gzip embeds a filename")
    decompressed = _decompress_single_gzip_member(
        data,
        max_decompressed_member_bytes=max_decompressed_member_bytes,
        label=path.name,
    )
    return _load_canonical_json(decompressed, label=path.name), data, decompressed


def _schema_object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ArtifactValidationError(f"{name} must be an object")
    return value


def _schema_keys(
    value: object,
    *,
    name: str,
    required: set[str],
    allowed: set[str] | None = None,
) -> dict[str, object]:
    obj = _schema_object(value, name)
    allowed_keys = required if allowed is None else allowed
    unknown = set(obj) - allowed_keys
    if unknown:
        raise ArtifactValidationError(f"{name} has unknown keys: {sorted(unknown)}")
    missing = required - set(obj)
    if missing:
        raise ArtifactValidationError(f"{name} is missing keys: {sorted(missing)}")
    return obj


def _schema_string(value: object, name: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{name} must be a string")


def _schema_text(value: object, name: str, *, nullable: bool = False) -> None:
    """Validate sanitized free text while retaining the input verbatim."""

    if nullable and value is None:
        return
    _schema_string(value, name)
    _validate_string(value, key=None)  # type: ignore[arg-type]


def _schema_identifier(value: object, name: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    _schema_string(value, name)
    if value == "":
        raise ArtifactValidationError(f"{name} must be a non-empty identifier")
    _validate_string(value, key=None)  # type: ignore[arg-type]


def _schema_symbol(value: object, name: str, *, nullable: bool = False) -> None:
    _schema_identifier(value, name, nullable=nullable)


def _schema_function_name(value: object, name: str, *, nullable: bool = False) -> None:
    _schema_identifier(value, name, nullable=nullable)


def _schema_path(value: object, name: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    _schema_string(value, name)
    if value == "":
        raise ArtifactValidationError(f"{name} must be a non-empty relative path")
    _validate_relative_path(value)  # type: ignore[arg-type]


def _schema_date(value: object, name: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    _schema_string(value, name)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:  # type: ignore[arg-type]
        raise ArtifactValidationError(f"{name} must be an ISO-8601 date")
    try:
        date.fromisoformat(value)  # type: ignore[arg-type]
    except ValueError as error:
        raise ArtifactValidationError(f"{name} must be an ISO-8601 date") from error


def _schema_version(value: object, name: str, expected: str) -> None:
    _schema_string(value, name)
    if value != expected:
        raise ArtifactValidationError(f"{name} has an unsupported version")


def _schema_enum(value: object, name: str, domain: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    _schema_string(value, name)
    allowed = SCHEMA_1_ENUM_DOMAINS[domain]
    if value not in allowed:
        raise ArtifactValidationError(f"{name} has an unknown enum value")


def _schema_string_list(value: object, name: str) -> None:
    values = _schema_list(value, name)
    for index, item in enumerate(values):
        _schema_string(item, f"{name}[{index}]")


def _schema_identifier_list(value: object, name: str) -> None:
    values = _schema_list(value, name)
    for index, item in enumerate(values):
        _schema_identifier(item, f"{name}[{index}]")


def _schema_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise ArtifactValidationError(f"{name} must be a boolean")


def _schema_int(value: object, name: str, *, nullable: bool = False, minimum: int | None = None) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise ArtifactValidationError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ArtifactValidationError(f"{name} must be >= {minimum}")


def _schema_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ArtifactValidationError(f"{name} must be an array")
    return value


def _schema_float_tag(
    value: object,
    name: str,
    *,
    nullable: bool = False,
    allow_integer: bool = False,
) -> None:
    if nullable and value is None:
        return
    if allow_integer and isinstance(value, int) and not isinstance(value, bool):
        return
    if not isinstance(value, dict) or set(value) != {"__float__"}:
        raise ArtifactValidationError(f"{name} must be a lossless float type tag")
    encoded = value.get("__float__")
    if not isinstance(encoded, str):
        raise ArtifactValidationError(f"{name} must contain a float hexadecimal string")
    try:
        parsed = float.fromhex(encoded)
    except (ValueError, OverflowError) as error:
        raise ArtifactValidationError(f"{name} contains an invalid float hexadecimal string") from error
    if not math.isfinite(parsed):
        raise ArtifactValidationError(f"{name} contains a non-finite float")


def _schema_nonnegative_integral_or_float_tag(value: object, name: str) -> None:
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            raise ArtifactValidationError(f"{name} must be >= 0")
        return

    _schema_float_tag(value, name)
    encoded = value["__float__"]  # type: ignore[index]
    if float.fromhex(encoded) < 0:  # type: ignore[arg-type]
        raise ArtifactValidationError(f"{name} must be >= 0")


def _schema_float_map(value: object, name: str) -> None:
    mapping = _schema_object(value, name)
    for key, item in mapping.items():
        _schema_identifier(key, f"{name}.key")
        _schema_float_tag(item, f"{name}.{key}")


def _validate_canonical_utc_timestamp(value: object, name: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    _schema_string(value, name)
    try:
        normalized = _normalize_utc_timestamp(value, name=name)
    except ArtifactValidationError:
        raise
    if normalized != value:
        raise ArtifactValidationError(f"{name} is not normalized UTC")


def _schema_hash(value: object, name: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ArtifactValidationError(f"{name} must be a lowercase SHA-256")


def _schema_revision_hash(value: object, name: str, *, nullable: bool = False) -> None:
    """Validate a source/runtime revision hash (Git SHA-1 or SHA-256)."""

    if nullable and value is None:
        return
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value) is None:
        raise ArtifactValidationError(f"{name} must be a lowercase source revision hash")


def _schema_trace_id(value: object, name: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ArtifactValidationError(f"{name} must be a content-derived trace ID")


def _float_expected_key(key: str) -> bool:
    return key in {"score", "precise", "entry", "stop", "target_2r", "target_3r"} or key.endswith("_score")


def _nested_semantic_domain(key: str) -> str | None:
    return {
        "artifact_status": "artifact_status",
        "asset_type": "asset_type",
        "axis": "axis",
        "branch_kind": "branch_kind",
        "collector_state": "collector_state",
        "coverage_status": "coverage_status",
        "classification_status": "classification_status",
        "decision": "decision",
        "decision_status": "decision_status",
        "effect_type": "effect_type",
        "final_decision": "decision",
        "max_decision": "decision",
        "runtime_sha_status": "runtime_sha_status",
        "schedule": "schedule",
        "serialization_status": "serialization_status",
        "termination_kind": "termination_kind",
        "trace_status": "trace_status",
    }.get(key)


def _validate_typed_value(
    value: object,
    *,
    path: str,
    expected_float: bool = False,
    semantic_parent: str | None = None,
) -> None:
    if isinstance(value, float):
        raise ArtifactValidationError(f"{path} contains an untagged float")
    if expected_float and isinstance(value, str) and _FLOAT_HEX_TEXT.fullmatch(value):
        raise ArtifactValidationError(f"{path} contains an untagged float")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_typed_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        if "__float__" in value:
            if set(value) != {"__float__"} or not isinstance(value["__float__"], str):
                raise ArtifactValidationError(f"{path} has an invalid float type tag")
            try:
                parsed = float.fromhex(value["__float__"])
            except (ValueError, OverflowError) as error:
                raise ArtifactValidationError(f"{path} has an invalid float hex value") from error
            if not math.isfinite(parsed):
                raise ArtifactValidationError(f"{path} has a non-finite float type tag")
        for key, item in value.items():
            key_text = str(key)
            domain = _nested_semantic_domain(key_text)
            if domain is not None and not isinstance(item, (Mapping, list)):
                _schema_enum(item, f"{path}.{key_text}", domain, nullable=True)
            elif semantic_parent == "decision" and key_text in {"before", "candidate", "after"}:
                _schema_enum(item, f"{path}.{key_text}", "decision", nullable=True)
            _validate_typed_value(
                item,
                path=f"{path}.{key_text}",
                expected_float=_float_expected_key(key_text),
                semantic_parent=domain or semantic_parent,
            )


def _validate_duration(value: object, name: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    _schema_float_tag(value, name)
    encoded = value["__float__"]  # type: ignore[index]
    parsed = float.fromhex(encoded)
    if parsed < 0:
        raise ArtifactValidationError(f"{name} must be nonnegative")


def _validate_hash_fields(integrity: dict[str, object], *, kind: str) -> None:
    _schema_hash(integrity["artifact_payload_hash"], f"{kind}.integrity.artifact_payload_hash")
    _schema_hash(integrity["trace_hash"], f"{kind}.integrity.trace_hash")
    _schema_enum(integrity["artifact_status"], f"{kind}.integrity.artifact_status", "artifact_status")
    _schema_int(integrity["asset_count"], f"{kind}.integrity.asset_count", minimum=0)
    if kind != "part":
        _schema_int(integrity["chunk_count"], f"{kind}.integrity.chunk_count", minimum=0)


def _validate_run_metadata(value: object) -> None:
    metadata = _schema_keys(
        value,
        name="run_metadata",
        required={"run_id", "report_date", "schedule", "source_sha", "runtime_sha", "timezone", "asset_count"},
        allowed=_RUN_METADATA_KEYS,
    )
    _schema_identifier(metadata["run_id"], "run_metadata.run_id")
    _schema_date(metadata["report_date"], "run_metadata.report_date")
    _schema_enum(metadata["schedule"], "run_metadata.schedule", "schedule")
    _schema_revision_hash(metadata["source_sha"], "run_metadata.source_sha", nullable=True)
    _schema_revision_hash(metadata["runtime_sha"], "run_metadata.runtime_sha", nullable=True)
    _schema_identifier(metadata["timezone"], "run_metadata.timezone")
    _schema_int(metadata["asset_count"], "run_metadata.asset_count", minimum=0)
    if "runtime_sha_status" in metadata:
        _schema_enum(metadata["runtime_sha_status"], "run_metadata.runtime_sha_status", "runtime_sha_status")
        if metadata["runtime_sha_status"] == "available" and metadata["runtime_sha"] is None:
            raise ArtifactValidationError("available runtime SHA status requires runtime_sha")
        if metadata["runtime_sha_status"] == "unavailable" and metadata["runtime_sha"] is not None:
            raise ArtifactValidationError("unavailable runtime SHA status requires a null runtime_sha")


def _validate_error(value: object, *, name: str) -> None:
    error = _schema_keys(value, name=name, required={"error_code"}, allowed=_ERROR_KEYS)
    _schema_enum(error["error_code"], f"{name}.error_code", "error_code")
    for key in ("error_type", "operation", "exception_type", "invocation_id", "symbol"):
        if key in error:
            validator = _schema_identifier if key in {"invocation_id", "symbol"} else _schema_text
            validator(error[key], f"{name}.{key}", nullable=True)
    if "sequence" in error:
        _schema_int(error["sequence"], f"{name}.sequence", nullable=True, minimum=0)
    if "warning" in error:
        _schema_bool(error["warning"], f"{name}.warning")


def _validate_error_list(value: object, *, name: str) -> None:
    errors = _schema_list(value, name)
    for index, error in enumerate(errors):
        _validate_error(error, name=f"{name}[{index}]")


def _validate_runtime_metadata(value: object) -> None:
    metadata = _schema_keys(
        value,
        name="runtime_metadata",
        required={"trace_started_at", "trace_completed_at"},
        allowed=_RUNTIME_METADATA_KEYS,
    )
    _validate_canonical_utc_timestamp(metadata["trace_started_at"], "runtime_metadata.trace_started_at")
    _validate_canonical_utc_timestamp(metadata["trace_completed_at"], "runtime_metadata.trace_completed_at")
    if "duration" in metadata:
        if metadata["duration"] is None and metadata.get("serialization_status") != "error" and metadata.get(
            "classification_status"
        ) not in {"failed", "unavailable"}:
            raise ArtifactValidationError(
                "runtime_metadata.duration null requires an explicit unavailable runtime status"
            )
        _validate_duration(metadata["duration"], "runtime_metadata.duration", nullable=True)
    for key in ("local_path", "exception_type"):
        if key in metadata:
            if key == "local_path":
                _schema_path(metadata[key], f"runtime_metadata.{key}", nullable=True)
            else:
                _schema_text(metadata[key], f"runtime_metadata.{key}", nullable=True)
    if "local_path" in metadata and metadata["local_path"] is not None:
        _validate_relative_path(metadata["local_path"])  # type: ignore[arg-type]
    if "classification_status" in metadata:
        _schema_enum(
            metadata["classification_status"],
            "runtime_metadata.classification_status",
            "classification_status",
            nullable=True,
        )
    if "serialization_status" in metadata:
        _schema_enum(
            metadata["serialization_status"],
            "runtime_metadata.serialization_status",
            "serialization_status",
            nullable=True,
        )


def _validate_integrity_structure(value: object, *, kind: str) -> dict[str, object]:
    expected = {
        "single": _SINGLE_INTEGRITY_KEYS,
        "index": _INDEX_INTEGRITY_KEYS,
        "part": _PART_INTEGRITY_KEYS,
    }[kind]
    integrity = _schema_keys(value, name=f"{kind}.integrity", required=expected, allowed=expected)
    _validate_hash_fields(integrity, kind=kind)
    if kind == "single":
        _schema_revision_hash(integrity["source_sha"], f"{kind}.integrity.source_sha", nullable=True)
        _schema_revision_hash(integrity["runtime_sha"], f"{kind}.integrity.runtime_sha", nullable=True)
        _schema_enum(
            integrity["schema_validation_status"],
            f"{kind}.integrity.schema_validation_status",
            "schema_validation_status",
        )
        if integrity["gzip_deterministic"] is not True:
            raise ArtifactValidationError(f"{kind}.integrity.gzip_deterministic must be true")
        _schema_int(integrity["soft_budget_bytes"], f"{kind}.integrity.soft_budget_bytes", minimum=1)
        _schema_int(integrity["hard_budget_bytes"], f"{kind}.integrity.hard_budget_bytes", minimum=1)
        decision_counts = _schema_object(integrity["decision_counts"], "single.integrity.decision_counts")
        for decision, count in decision_counts.items():
            _schema_enum(decision, "single.integrity.decision_counts.key", "decision")
            _schema_int(count, f"single.integrity.decision_counts.{decision}", minimum=0)
    elif kind == "index":
        _schema_int(integrity["soft_budget_bytes"], f"{kind}.integrity.soft_budget_bytes", minimum=1)
        _schema_int(integrity["hard_budget_bytes"], f"{kind}.integrity.hard_budget_bytes", minimum=1)
    else:
        if integrity["gzip_deterministic"] is not True:
            raise ArtifactValidationError(f"{kind}.integrity.gzip_deterministic must be true")


def _validate_root_structure(payload: Mapping[str, object]) -> None:
    root = _schema_keys(
        payload,
        name="artifact",
        required=_ARTIFACT_ROOT_KEYS,
        allowed=_ARTIFACT_ROOT_KEYS,
    )
    _schema_version(root["schema_version"], "artifact.schema_version", SCHEMA_VERSION)
    _schema_version(root["trace_schema_version"], "artifact.trace_schema_version", SCHEMA_VERSION)
    _schema_enum(root["artifact_status"], "artifact.artifact_status", "artifact_status")
    _validate_run_metadata(root["run_metadata"])
    _schema_version(root["rule_catalog_version"], "artifact.rule_catalog_version", RULE_CATALOG_VERSION)
    _schema_hash(root["rule_catalog_hash"], "artifact.rule_catalog_hash")
    _schema_int(root["rule_catalog_entry_count"], "artifact.rule_catalog_entry_count", minimum=0)
    _schema_list(root["rule_catalog"], "artifact.rule_catalog")
    _schema_list(root["assets"], "artifact.assets")
    _validate_error_list(root["errors"], name="artifact.errors")
    _validate_integrity_structure(root["integrity"], kind="single")


def _validate_index_structure(payload: Mapping[str, object]) -> None:
    root = _schema_keys(
        payload,
        name="index",
        required=(_ARTIFACT_ROOT_KEYS - {"assets"}) | {"parts"},
        allowed=_INDEX_ROOT_KEYS,
    )
    _schema_version(root["schema_version"], "index.schema_version", SCHEMA_VERSION)
    _schema_version(root["trace_schema_version"], "index.trace_schema_version", SCHEMA_VERSION)
    _schema_enum(root["artifact_status"], "index.artifact_status", "artifact_status")
    _validate_run_metadata(root["run_metadata"])
    _schema_version(root["rule_catalog_version"], "index.rule_catalog_version", RULE_CATALOG_VERSION)
    _schema_hash(root["rule_catalog_hash"], "index.rule_catalog_hash")
    _schema_int(root["rule_catalog_entry_count"], "index.rule_catalog_entry_count", minimum=0)
    _schema_list(root["rule_catalog"], "index.rule_catalog")
    _schema_list(root["parts"], "index.parts")
    _validate_error_list(root["errors"], name="index.errors")
    _validate_integrity_structure(root["integrity"], kind="index")
    if root["artifact_status"] == "failed":
        if "failed_assets" not in root:
            raise ArtifactValidationError("failed index must include failed_assets")
        _schema_list(root["failed_assets"], "index.failed_assets")
    elif "failed_assets" in root:
        raise ArtifactValidationError("non-failed index cannot include failed_assets")


def _validate_part_structure(payload: Mapping[str, object]) -> None:
    root = _schema_keys(payload, name="part", required=_PART_ROOT_KEYS, allowed=_PART_ROOT_KEYS)
    _schema_version(root["schema_version"], "part.schema_version", SCHEMA_VERSION)
    _schema_version(root["trace_schema_version"], "part.trace_schema_version", SCHEMA_VERSION)
    _schema_enum(root["artifact_status"], "part.artifact_status", "artifact_status")
    _validate_run_metadata(root["run_metadata"])
    _schema_hash(root["rule_catalog_hash"], "part.rule_catalog_hash")
    _schema_path(root["rule_catalog_reference"], "part.rule_catalog_reference")
    if root["rule_catalog_reference"] != _INDEX_NAME:
        raise ArtifactValidationError("part catalog reference is invalid")
    _schema_int(root["part_number"], "part.part_number", minimum=1)
    _schema_list(root["assets"], "part.assets")
    _validate_error_list(root["errors"], name="part.errors")
    _validate_integrity_structure(root["integrity"], kind="part")


def _validate_part_descriptor(value: object, *, index: int, expected_number: int) -> dict[str, object]:
    record = _schema_keys(
        value,
        name=f"index.parts[{index}]",
        required=_PART_DESCRIPTOR_KEYS,
    )
    _schema_int(record["part_number"], f"index.parts[{index}].part_number", minimum=1)
    if record["part_number"] != expected_number:
        raise ArtifactValidationError("chunk part numbering is invalid")
    _schema_path(record["filename"], f"index.parts[{index}].filename")
    if record["filename"] != _PART_NAME.format(number=expected_number):
        raise ArtifactValidationError("chunk part filename is invalid")
    _schema_hash(record["sha256"], f"index.parts[{index}].sha256")
    _schema_int(record["compressed_size_bytes"], f"index.parts[{index}].compressed_size_bytes", minimum=1)
    _schema_int(record["uncompressed_size_bytes"], f"index.parts[{index}].uncompressed_size_bytes", minimum=1)
    _validate_string_list(record["symbols"], name=f"index.parts[{index}].symbols")
    return record


def _validate_catalog(payload: Mapping[str, object]) -> str:
    catalog = payload.get("rule_catalog")
    if not isinstance(catalog, list):
        raise ArtifactValidationError("rule catalog is missing")
    rule_ids: list[str] = []
    for index, item in enumerate(catalog):
        rule = _schema_keys(
            item,
            name=f"rule_catalog[{index}]",
            required={
                "rule_id",
                "function",
                "source_code_locator",
                "branch_signature",
                "branch_kind",
                "axis",
                "effect_type",
                "evidence_keys",
            },
        )
        _schema_identifier(rule["rule_id"], f"rule_catalog[{index}].rule_id")
        _schema_function_name(rule["function"], f"rule_catalog[{index}].function")
        _schema_text(rule["branch_signature"], f"rule_catalog[{index}].branch_signature")
        _schema_enum(rule["branch_kind"], f"rule_catalog[{index}].branch_kind", "branch_kind")
        _schema_enum(rule["axis"], f"rule_catalog[{index}].axis", "axis")
        _schema_enum(rule["effect_type"], f"rule_catalog[{index}].effect_type", "effect_type")
        locator = _schema_keys(
            rule["source_code_locator"],
            name=f"rule_catalog[{index}].source_code_locator",
            required={"path", "function", "line_start", "line_end"},
        )
        _schema_path(locator["path"], f"rule_catalog[{index}].source_code_locator.path")
        _schema_function_name(locator["function"], f"rule_catalog[{index}].source_code_locator.function")
        _schema_int(locator["line_start"], f"rule_catalog[{index}].source_code_locator.line_start", minimum=1)
        _schema_int(locator["line_end"], f"rule_catalog[{index}].source_code_locator.line_end", minimum=1)
        evidence_keys = _schema_list(rule["evidence_keys"], f"rule_catalog[{index}].evidence_keys")
        for key in evidence_keys:
            _schema_identifier(key, f"rule_catalog[{index}].evidence_keys[]")
        rule_ids.append(str(rule["rule_id"]))
    if len(catalog) != 97 or len(rule_ids) != 97 or len(set(rule_ids)) != 97:
        raise ArtifactValidationError("rule catalog must contain 97 unique rule IDs")
    catalog_hash = _sha256(canonical_json_bytes(catalog))
    if payload.get("rule_catalog_hash") != catalog_hash:
        raise ArtifactValidationError("rule catalog hash mismatch")
    if payload.get("rule_catalog_entry_count") != 97:
        raise ArtifactValidationError("rule catalog entry count mismatch")
    return catalog_hash


def _validate_common(payload: Mapping[str, object]) -> None:
    if not isinstance(payload, Mapping):
        raise ArtifactValidationError("artifact root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ArtifactValidationError("unsupported schema version")
    if payload.get("trace_schema_version") != SCHEMA_VERSION:
        raise ArtifactValidationError("unsupported trace schema version")
    status = payload.get("artifact_status")
    _schema_enum(status, "artifact.artifact_status", "artifact_status")
    integrity = payload.get("integrity")
    if not isinstance(integrity, Mapping):
        raise ArtifactValidationError("artifact integrity is missing")
    if integrity.get("artifact_status") != status:
        raise ArtifactValidationError("artifact status mismatch")
    expected_hash = _payload_hash(payload)
    if integrity.get("artifact_payload_hash") != expected_hash:
        raise ArtifactValidationError("artifact payload hash mismatch")


def _validate_string_list(value: object, *, name: str) -> None:
    values = _schema_list(value, name)
    for index, item in enumerate(values):
        _schema_text(item, f"{name}[{index}]")


def _validate_event(value: object, *, index: int) -> None:
    event = _schema_keys(value, name=f"trace.events[{index}]", required=_EVENT_KEYS)
    _schema_int(event["sequence"], f"trace.events[{index}].sequence", minimum=1)
    _schema_identifier(event["invocation_id"], f"trace.events[{index}].invocation_id")
    _schema_identifier(event["rule_id"], f"trace.events[{index}].rule_id")
    for key in ("reached", "evaluated", "terminated"):
        _schema_bool(event[key], f"trace.events[{index}].{key}")
    _schema_bool(event["matched"], f"trace.events[{index}].matched") if event["matched"] is not None else None
    if not event["reached"]:
        raise ArtifactValidationError(f"trace.events[{index}].reached must be true")
    if event["matched"] is None and (
        event["evaluated"]
        or not event["terminated"]
        or event["termination_kind"] != "raise"
    ):
        raise ArtifactValidationError(f"trace.events[{index}] has an invalid unevaluated event")
    _schema_enum(
        event["termination_kind"],
        f"trace.events[{index}].termination_kind",
        "termination_kind",
        nullable=True,
    )
    _schema_text(event["branch_label"], f"trace.events[{index}].branch_label", nullable=True)
    _schema_enum(event["axis"], f"trace.events[{index}].axis", "axis")
    _schema_enum(event["effect_type"], f"trace.events[{index}].effect_type", "effect_type")
    _schema_identifier_list(event["evidence_keys"], f"trace.events[{index}].evidence_keys")
    _schema_object(event["condition_inputs"], f"trace.events[{index}].condition_inputs")
    _schema_object(event["state_changes"], f"trace.events[{index}].state_changes")
    _validate_typed_value(event["condition_inputs"], path=f"trace.events[{index}].condition_inputs")
    _validate_typed_value(event["state_changes"], path=f"trace.events[{index}].state_changes")
    for key in ("reason_codes_added", "alerts_added", "limitations_added"):
        _schema_identifier_list(event[key], f"trace.events[{index}].{key}")


def _validate_invocation(value: object, *, index: int) -> None:
    invocation = _schema_keys(value, name=f"trace.invocations[{index}]", required=_INVOCATION_KEYS)
    _schema_identifier(invocation["invocation_id"], f"trace.invocations[{index}].invocation_id")
    _schema_function_name(invocation["function"], f"trace.invocations[{index}].function")
    _schema_identifier(
        invocation["parent_invocation_id"],
        f"trace.invocations[{index}].parent_invocation_id",
        nullable=True,
    )
    _schema_enum(
        invocation["termination_kind"],
        f"trace.invocations[{index}].termination_kind",
        "termination_kind",
        nullable=True,
    )
    _schema_identifier(
        invocation["termination_rule_id"],
        f"trace.invocations[{index}].termination_rule_id",
        nullable=True,
    )
    for key in ("call_ordinal", "started_sequence", "last_reliable_sequence"):
        _schema_int(invocation[key], f"trace.invocations[{index}].{key}", minimum=0)
    for key in ("completed_sequence", "termination_sequence", "observation_failure_sequence"):
        _schema_int(invocation[key], f"trace.invocations[{index}].{key}", nullable=True, minimum=0)
    _schema_enum(
        invocation["coverage_status"],
        f"trace.invocations[{index}].coverage_status",
        "coverage_status",
    )
    for key in ("interval_complete", "coverage_complete", "invocation_coverage_complete"):
        _schema_bool(invocation[key], f"trace.invocations[{index}].{key}")
    for key in (
        "catalog_rule_ids",
        "reached_rule_ids",
        "known_unreached_rule_ids",
        "unreached_rule_ids",
        "unknown_rule_ids",
    ):
        _schema_identifier_list(invocation[key], f"trace.invocations[{index}].{key}")


def _validate_decision_payload(value: object, *, name: str) -> None:
    decision = _schema_keys(value, name=name, required=_DECISION_KEYS)
    _validate_typed_value(decision, path=name)
    _schema_symbol(decision["symbol"], f"{name}.symbol")
    for key in (
        "thesis",
        "hold_suggestion",
        "data_quality",
        "missing_data_severity",
        "data_source",
        "bucket",
        "market_session",
        "provider",
        "event_check_status",
        "news_status",
        "macro_regime",
        "macro_status",
        "thesis_status",
        "squeeze_risk",
        "gap_risk",
        "short_status",
    ):
        _schema_text(decision[key], f"{name}.{key}")
    _schema_enum(decision["asset_type"], f"{name}.asset_type", "asset_type")
    _schema_enum(decision["decision"], f"{name}.decision", "decision")
    _schema_identifier(decision["universe_origin"], f"{name}.universe_origin")
    for key in (
        "sample_quality",
        "news_summary",
        "data_timestamp",
        "last_price_timestamp",
        "stale_reason",
        "sector_benchmark",
    ):
        _schema_text(decision[key], f"{name}.{key}", nullable=True)
    for key in ("alerts", "limitations", "metrics_summary", "reason_codes"):
        _schema_string_list(decision[key], f"{name}.{key}")
    for key in (
        "investment_quality_score",
        "swing_trade_score",
        "ideal_entry",
        "relative_strength_vs_spy",
        "relative_strength_vs_qqq",
        "relative_strength_vs_sector",
    ):
        _schema_float_tag(decision[key], f"{name}.{key}", nullable=key.startswith("relative_strength"))
    _schema_float_tag(decision["short_setup_score"], f"{name}.short_setup_score", allow_integer=True)
    _schema_float_tag(decision["alternative_entry"], f"{name}.alternative_entry", nullable=True)
    for key in ("data_quality_score", "decision_confidence_score"):
        _schema_int(decision[key], f"{name}.{key}")
    _schema_int(decision["cache_age_seconds"], f"{name}.cache_age_seconds", nullable=True, minimum=0)
    _schema_bool(decision["is_stale"], f"{name}.is_stale")
    _schema_bool(decision["borrow_data_available"], f"{name}.borrow_data_available")
    risk_plan = _schema_object(decision["risk_plan"], f"{name}.risk_plan")
    _schema_keys(risk_plan, name=f"{name}.risk_plan", required=_RISK_PLAN_KEYS)
    for key in (
        "entry",
        "stop",
        "target_2r",
        "target_3r",
        "per_unit_risk",
        "risk_amount",
        "risk_fraction",
        "max_position_value",
    ):
        _schema_float_tag(risk_plan[key], f"{name}.risk_plan.{key}")
    _schema_nonnegative_integral_or_float_tag(
        risk_plan["max_position_units"], f"{name}.risk_plan.max_position_units"
    )
    _schema_text(risk_plan["risk_reward_2r"], f"{name}.risk_plan.risk_reward_2r")
    _schema_string_list(risk_plan["alerts"], f"{name}.risk_plan.alerts")
    _schema_text(risk_plan["position_size_display"], f"{name}.risk_plan.position_size_display")
    backtest = decision["backtest_stats"]
    if backtest is not None:
        backtest = _schema_keys(backtest, name=f"{name}.backtest_stats", required=_BACKTEST_KEYS)
        _schema_int(backtest["sample_size"], f"{name}.backtest_stats.sample_size", minimum=0)
        for key in (
            "win_rate_2r",
            "win_rate_3r",
            "expected_value_r",
            "avg_win_r",
            "avg_loss_r",
            "max_drawdown_r",
        ):
            _schema_float_tag(backtest[key], f"{name}.backtest_stats.{key}", nullable=True)
        for key in ("median_days_to_2r", "median_days_to_3r"):
            _schema_int(backtest[key], f"{name}.backtest_stats.{key}", nullable=True, minimum=0)
        _schema_text(backtest["setup_quality"], f"{name}.backtest_stats.setup_quality", nullable=True)
        _schema_text(backtest["period_start"], f"{name}.backtest_stats.period_start", nullable=True)
        _schema_text(backtest["period_end"], f"{name}.backtest_stats.period_end", nullable=True)
        _schema_float_map(backtest["benchmark_comparison"], f"{name}.backtest_stats.benchmark_comparison")
        _schema_string_list(backtest["warnings"], f"{name}.backtest_stats.warnings")


def _validate_trace_payload(
    trace: Mapping[str, object],
    decision_hash: str,
    *,
    decision_payload: Mapping[str, object] | None = None,
) -> None:
    trace = _schema_keys(trace, name="trace", required=_TRACE_KEYS)
    _schema_version(trace["trace_schema_version"], "trace.trace_schema_version", SCHEMA_VERSION)
    trace_id = trace.get("trace_id")
    trace_id_basis = copy.deepcopy(dict(trace))
    trace_id_basis.pop("trace_id", None)
    expected_trace_id = f"sha256:{_sha256(canonical_json_bytes(trace_id_basis))}"
    if trace_id != expected_trace_id:
        raise ArtifactValidationError("trace ID mismatch")
    _schema_hash(trace["rule_catalog_hash"], "trace.rule_catalog_hash", nullable=True)
    _schema_revision_hash(trace["source_sha"], "trace.source_sha", nullable=True)
    _schema_revision_hash(trace["runtime_sha"], "trace.runtime_sha", nullable=True)
    _schema_date(trace["report_date"], "trace.report_date", nullable=True)
    _schema_enum(trace["schedule"], "trace.schedule", "schedule", nullable=True)
    _schema_symbol(trace["symbol"], "trace.symbol")
    _schema_enum(trace["asset_type"], "trace.asset_type", "asset_type")
    _schema_identifier(trace["universe_origin"], "trace.universe_origin")
    _validate_canonical_utc_timestamp(trace["effective_now_utc"], "trace.effective_now_utc", nullable=True)
    for key in ("classification_inputs", "initial_state", "final_state"):
        _schema_object(trace[key], f"trace.{key}")
        _validate_typed_value(trace[key], path=f"trace.{key}")
    precise = trace["classification_inputs"].get("precise")
    if isinstance(precise, str) and re.fullmatch(r"[-+]?0x[0-9a-f]+(?:\.[0-9a-f]*)?p[-+]?\d+", precise):
        raise ArtifactValidationError("trace.classification_inputs.precise has an untagged float")
    _schema_enum(trace["trace_status"], "trace.trace_status", "trace_status")
    for key in ("observer_enabled", "coverage_complete"):
        _schema_bool(trace[key], f"trace.{key}")
    for key in ("last_reliable_sequence", "last_persisted_event_sequence"):
        _schema_int(trace[key], f"trace.{key}", minimum=0)
    _schema_int(trace["observation_failure_sequence"], "trace.observation_failure_sequence", nullable=True, minimum=0)
    _schema_identifier(trace["active_invocation_id"], "trace.active_invocation_id", nullable=True)
    _schema_enum(trace["collector_state"], "trace.collector_state", "collector_state")
    classification = trace.get("classification")
    classification = _schema_keys(
        classification,
        name="trace.classification",
        required={"final_decision", "serialized_asset_decision_hash"},
    )
    _schema_enum(classification["final_decision"], "trace.classification.final_decision", "decision")
    _schema_hash(classification["serialized_asset_decision_hash"], "trace.classification.serialized_asset_decision_hash")
    if classification.get("serialized_asset_decision_hash") != decision_hash:
        raise ArtifactValidationError("trace decision hash mismatch")
    if decision_payload is not None and classification["final_decision"] != decision_payload.get("decision"):
        raise ArtifactValidationError("trace final decision mismatch")
    events = trace.get("events")
    invocations = trace.get("invocations")
    if not isinstance(events, list) or not isinstance(invocations, list):
        raise ArtifactValidationError("trace events or invocations are missing")
    for index, event in enumerate(events):
        _validate_event(event, index=index)
    for index, invocation in enumerate(invocations):
        _validate_invocation(invocation, index=index)
    _validate_error_list(trace["observation_errors"], name="trace.observation_errors")
    sequences = [event.get("sequence") for event in events if isinstance(event, Mapping)]
    if len(sequences) != len(events) or sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
        raise ArtifactValidationError("trace events are not in unique sequence order")
    starts = [invocation.get("started_sequence") for invocation in invocations if isinstance(invocation, Mapping)]
    if len(starts) != len(invocations) or starts != sorted(starts) or len(set(starts)) != len(starts):
        raise ArtifactValidationError("trace invocations are not in unique start order")
    invocation_map = {
        str(invocation.get("invocation_id")): invocation
        for invocation in invocations
        if isinstance(invocation, Mapping)
    }
    events_by_invocation: dict[str, list[Mapping[str, object]]] = {
        invocation_id: [] for invocation_id in invocation_map
    }
    for event in events:
        if not isinstance(event, Mapping):
            raise ArtifactValidationError("trace event is malformed")
        invocation_id = str(event.get("invocation_id"))
        if invocation_id not in invocation_map:
            raise ArtifactValidationError("event references omitted invocation")
        events_by_invocation[invocation_id].append(event)
    for invocation_id, invocation in invocation_map.items():
        reached = invocation.get("reached_rule_ids")
        if not isinstance(reached, list):
            raise ArtifactValidationError("invocation reached rules are missing")
        retained = list(
            dict.fromkeys(str(event.get("rule_id")) for event in events_by_invocation[invocation_id])
        )
        if reached != retained:
            raise ArtifactValidationError("invocation reached rules disagree with retained events")
    status = trace.get("trace_status")
    errors = trace.get("observation_errors")
    if trace.get("last_persisted_event_sequence") != trace.get("last_reliable_sequence"):
        raise ArtifactValidationError("last persisted event sequence mismatch")
    _schema_enum(status, "trace.trace_status", "trace_status")
    if status == "complete" and trace.get("coverage_complete") is not True:
        raise ArtifactValidationError("complete trace must claim complete coverage")
    if status != "complete" and (not isinstance(errors, list) or not errors):
        raise ArtifactValidationError("partial trace omitted observation errors")


def _validate_asset_structure(value: object, *, name: str, failed: bool) -> dict[str, object]:
    required = _ASSET_KEYS | ({"decision_status"} if failed else set())
    asset = _schema_keys(
        value,
        name=name,
        required=required,
        allowed=_FAILED_ASSET_KEYS if failed else _ASSET_KEYS,
    )
    _schema_symbol(asset["symbol"], f"{name}.symbol")
    _schema_enum(asset["asset_type"], f"{name}.asset_type", "asset_type")
    _schema_identifier(asset["universe_origin"], f"{name}.universe_origin")
    _schema_enum(asset["serialization_status"], f"{name}.serialization_status", "serialization_status")
    _schema_hash(asset["serialized_asset_decision_hash"], f"{name}.serialized_asset_decision_hash", nullable=True)
    _schema_hash(asset["trace_hash"], f"{name}.trace_hash", nullable=True)
    _schema_enum(asset["trace_status"], f"{name}.trace_status", "trace_status")
    _validate_runtime_metadata(asset["runtime_metadata"])
    runtime_metadata = asset["runtime_metadata"]
    if (
        isinstance(runtime_metadata, Mapping)
        and "serialization_status" in runtime_metadata
        and runtime_metadata["serialization_status"] != asset["serialization_status"]
    ):
        raise ArtifactValidationError(f"{name} serialization status disagrees with runtime metadata")
    _validate_error_list(asset["errors"], name=f"{name}.errors")
    if asset["serialization_status"] == "error" and not any(
        isinstance(error, Mapping) and error.get("error_code") in _RECOVERABLE_STATUS_ERROR_CODES
        for error in asset["errors"]
    ):
        raise ArtifactValidationError(f"{name} serialization error lacks a sanitized error")
    decision_payload = asset["serialized_asset_decision"]
    if decision_payload is not None:
        _validate_decision_payload(decision_payload, name=f"{name}.serialized_asset_decision")
    if failed:
        _schema_enum(asset["decision_status"], f"{name}.decision_status", "decision_status")
        if asset["decision_status"] == "unavailable":
            if decision_payload is not None or asset["serialized_asset_decision_hash"] is not None:
                raise ArtifactValidationError(f"{name} unavailable decision must be null")
            if not asset["errors"]:
                raise ArtifactValidationError(f"{name} unavailable decision needs a sanitized error")
        elif decision_payload is None or asset["serialized_asset_decision_hash"] is None:
            raise ArtifactValidationError(f"{name} available decision is incomplete")
    return asset


def _validate_assets(
    assets: object,
    *,
    catalog_hash: str,
    failed: bool = False,
    run_metadata: Mapping[str, object] | None = None,
) -> tuple[str, ...]:
    if not isinstance(assets, list):
        raise ArtifactValidationError("assets are missing")
    keys: list[tuple[str, str, str]] = []
    symbols: list[str] = []
    for index, raw_asset in enumerate(assets):
        asset = _validate_asset_structure(raw_asset, name=f"{'failed_' if failed else ''}asset[{index}]", failed=failed)
        key = _asset_sort_key(asset)
        keys.append(key)
        symbols.append(key[0])
        decision_payload = asset.get("serialized_asset_decision")
        decision_hash = asset.get("serialized_asset_decision_hash")
        decision_available = isinstance(decision_payload, Mapping) and isinstance(decision_hash, str)
        if not decision_available:
            if asset.get("serialization_status") == "error" or (
                failed and asset.get("decision_status") == "unavailable"
            ):
                decision_payload = None
                decision_hash = None
            else:
                raise ArtifactValidationError("asset decision serialization is missing")
        if decision_available:
            if decision_payload.get("symbol") != asset["symbol"]:
                raise ArtifactValidationError("asset decision symbol mismatch")
            if decision_payload.get("asset_type") != asset["asset_type"]:
                raise ArtifactValidationError("asset decision asset_type mismatch")
            if decision_payload.get("universe_origin") != asset["universe_origin"]:
                raise ArtifactValidationError("asset decision universe_origin mismatch")
            _validate_decision_payload(decision_payload, name=f"asset[{index}].serialized_asset_decision")
            if _sha256(_embedded_canonical_json_bytes(decision_payload)) != decision_hash:
                raise ArtifactValidationError("asset decision hash mismatch")
        trace = asset.get("trace")
        trace_hash = asset.get("trace_hash")
        if not isinstance(trace, Mapping) or not isinstance(trace_hash, str):
            if asset.get("serialization_status") == "error" or (
                failed and asset.get("decision_status") == "unavailable"
            ):
                continue
            raise ArtifactValidationError("asset trace serialization is missing")
        if trace.get("trace_status") != asset["trace_status"]:
            raise ArtifactValidationError("asset and trace status mismatch")
        if trace.get("rule_catalog_hash") != catalog_hash:
            raise ArtifactValidationError("asset trace catalog hash mismatch")
        if not decision_available:
            raise ArtifactValidationError("trace cannot be validated without a decision hash")
        _validate_trace_payload(
            trace,
            decision_hash,
            decision_payload=decision_payload if isinstance(decision_payload, Mapping) else None,
        )
        if _sha256(canonical_json_bytes(trace)) != trace_hash:
            raise ArtifactValidationError("asset trace hash mismatch")
        if (
            trace.get("symbol") != asset["symbol"]
            or trace.get("asset_type") != asset["asset_type"]
            or trace.get("universe_origin") != asset["universe_origin"]
        ):
            raise ArtifactValidationError("trace identity mismatch")
        if run_metadata is not None:
            for key in ("source_sha", "runtime_sha", "report_date", "schedule"):
                if trace.get(key) != run_metadata.get(key):
                    raise ArtifactValidationError(f"trace {key} does not match run metadata")
        if "rule_catalog" in asset or "rule_catalog" in trace:
            raise ArtifactValidationError("rule catalog is repeated inside an asset")
    if keys != sorted(keys):
        raise ArtifactValidationError("assets are not in canonical order")
    if len(keys) != len(set(keys)):
        raise ArtifactValidationError("duplicate asset identity")
    return tuple(symbols)


def _validate_single(path: Path, *, max_decompressed_member_bytes: int) -> ArtifactValidationResult:
    payload, file_bytes, _decompressed_bytes = _read_gzip(
        path,
        max_decompressed_member_bytes=max_decompressed_member_bytes,
    )
    _validate_root_structure(payload)
    catalog_hash = _validate_catalog(payload)
    symbols = _validate_assets(
        payload.get("assets"),
        catalog_hash=catalog_hash,
        run_metadata=payload["run_metadata"],
    )
    integrity = payload["integrity"]
    expected_status = _derive_artifact_status(payload["assets"], payload["errors"])
    if payload["artifact_status"] != expected_status:
        raise ArtifactValidationError("artifact status does not match asset statuses")
    if integrity.get("asset_count") != len(symbols):
        raise ArtifactValidationError("asset count mismatch")
    if integrity.get("chunk_count") != 1:
        raise ArtifactValidationError("single artifact chunk count mismatch")
    if payload["run_metadata"]["asset_count"] != len(symbols):
        raise ArtifactValidationError("run metadata asset count mismatch")
    for key in ("source_sha", "runtime_sha"):
        if integrity.get(key) != payload["run_metadata"].get(key):
            raise ArtifactValidationError(f"integrity {key} does not match run metadata")
    if integrity.get("decision_counts") != _decision_counts(payload["assets"]):
        raise ArtifactValidationError("decision counts mismatch")
    if not symbols:
        if payload["artifact_status"] != "failed":
            raise ArtifactValidationError("zero assets cannot be complete or partial")
        if not any(error.get("error_code") == "no_assets" for error in payload["errors"]):
            raise ArtifactValidationError("zero assets requires no_assets error")
    if integrity.get("trace_hash") != _aggregate_trace_hash(payload["assets"]):
        raise ArtifactValidationError("aggregate trace hash mismatch")
    _validate_common(payload)
    return ArtifactValidationResult(
        artifact_status=str(payload["artifact_status"]),
        mode="single",
        symbols=symbols,
        artifact_payload_hash=str(integrity["artifact_payload_hash"]),
        artifact_file_hash=_sha256(file_bytes),
    )


def _validate_index(path: Path, *, max_decompressed_member_bytes: int) -> ArtifactValidationResult:
    data = path.read_bytes()
    payload = _load_canonical_json(data, label=path.name)
    _validate_index_structure(payload)
    catalog_hash = _validate_catalog(payload)
    status = str(payload["artifact_status"])
    parts = payload.get("parts")
    if not isinstance(parts, list):
        raise ArtifactValidationError("chunk index parts are missing")
    if status == "failed":
        if parts:
            raise ArtifactValidationError("failed artifact cannot publish complete parts")
        failed_assets = payload.get("failed_assets")
        if not isinstance(failed_assets, list):
            raise ArtifactValidationError("failed artifact omitted preserved assets")
        failed_symbols = _validate_assets(
            failed_assets,
            catalog_hash=catalog_hash,
            failed=True,
            run_metadata=payload["run_metadata"],
        )
        if payload["integrity"].get("asset_count") != len(failed_symbols):
            raise ArtifactValidationError("failed asset count mismatch")
        if payload["integrity"].get("chunk_count") != 0:
            raise ArtifactValidationError("failed artifact chunk count mismatch")
        if payload["run_metadata"]["asset_count"] != len(failed_symbols):
            raise ArtifactValidationError("failed run metadata asset count mismatch")
        if not failed_symbols and not any(error.get("error_code") == "no_assets" for error in payload["errors"]):
            raise ArtifactValidationError("zero assets requires no_assets error")
        if payload["integrity"].get("trace_hash") != _aggregate_trace_hash(failed_assets):
            raise ArtifactValidationError("failed artifact trace hash mismatch")
        expected_status = _derive_index_status(
            parts=(),
            failed_assets=failed_assets,
            all_assets=failed_assets,
            errors=payload["errors"],
        )
        if status != expected_status:
            raise ArtifactValidationError("failed index status does not match its contents")
        _validate_common(payload)
        return ArtifactValidationResult(
            artifact_status=status,
            mode="failed",
            symbols=tuple(
                str(asset.get("symbol"))
                for asset in failed_assets
            ),
            artifact_payload_hash=str(payload["integrity"]["artifact_payload_hash"]),
            artifact_file_hash=_sha256(data),
        )
    symbols: list[str] = []
    keys: list[tuple[str, str, str]] = []
    all_assets: list[Mapping[str, object]] = []
    part_hashes: list[str] = []
    if not parts:
        raise ArtifactValidationError("non-failed index must contain parts")
    for expected_number, raw_record in enumerate(parts, start=1):
        record = _validate_part_descriptor(raw_record, index=expected_number - 1, expected_number=expected_number)
        filename = record["filename"]
        part_path = path.parent / filename
        if not part_path.is_file():
            raise ArtifactValidationError("chunk part is missing")
        part_payload, part_bytes, _part_decompressed_bytes = _read_gzip(
            part_path,
            max_decompressed_member_bytes=max_decompressed_member_bytes,
        )
        part_hash = _sha256(part_bytes)
        if record.get("sha256") != part_hash:
            raise ArtifactValidationError("chunk part hash mismatch")
        if record.get("compressed_size_bytes") != len(part_bytes):
            raise ArtifactValidationError("chunk part size mismatch")
        if record.get("uncompressed_size_bytes") != len(_part_decompressed_bytes):
            raise ArtifactValidationError("chunk part uncompressed size mismatch")
        _validate_part_structure(part_payload)
        if part_payload["part_number"] != expected_number:
            raise ArtifactValidationError("part payload number mismatch")
        if part_payload["artifact_status"] != status:
            raise ArtifactValidationError("part status mismatch")
        if part_payload["run_metadata"] != payload["run_metadata"]:
            raise ArtifactValidationError("part run metadata mismatch")
        if "rule_catalog" in part_payload:
            raise ArtifactValidationError("rule catalog must appear only in the index")
        if part_payload.get("rule_catalog_hash") != catalog_hash:
            raise ArtifactValidationError("chunk catalog reference mismatch")
        part_symbols = _validate_assets(
            part_payload.get("assets"),
            catalog_hash=catalog_hash,
            run_metadata=part_payload["run_metadata"],
        )
        if record.get("symbols") != list(part_symbols):
            raise ArtifactValidationError("chunk symbol list mismatch")
        if part_payload["integrity"].get("asset_count") != len(part_symbols):
            raise ArtifactValidationError("part asset count mismatch")
        if part_payload["integrity"].get("trace_hash") != _aggregate_trace_hash(part_payload["assets"]):
            raise ArtifactValidationError("part aggregate trace hash mismatch")
        _validate_common(part_payload)
        symbols.extend(part_symbols)
        keys.extend(
            _asset_sort_key(asset)
            for asset in part_payload["assets"]
            if isinstance(asset, Mapping)
        )
        all_assets.extend(
            asset for asset in part_payload["assets"] if isinstance(asset, Mapping)
        )
        part_hashes.append(part_hash)
    if keys != sorted(keys):
        raise ArtifactValidationError("chunked assets are not in canonical order")
    if len(keys) != len(set(keys)) or len(symbols) != len(set(symbols)):
        raise ArtifactValidationError("duplicate asset across chunks")
    integrity = payload["integrity"]
    if integrity.get("chunk_count") != len(parts) or integrity.get("asset_count") != len(symbols):
        raise ArtifactValidationError("chunk index counts mismatch")
    if payload["run_metadata"]["asset_count"] != len(symbols):
        raise ArtifactValidationError("chunk run metadata asset count mismatch")
    if integrity.get("trace_hash") != _aggregate_trace_hash(all_assets):
        raise ArtifactValidationError("aggregate trace hash mismatch")
    expected_status = _derive_index_status(
        parts=parts,
        failed_assets=(),
        all_assets=all_assets,
        errors=payload["errors"],
    )
    if status != expected_status:
        raise ArtifactValidationError("chunk index status does not match part asset statuses")
    _validate_common(payload)
    return ArtifactValidationResult(
        artifact_status=status,
        mode="chunked",
        symbols=tuple(symbols),
        artifact_payload_hash=str(integrity["artifact_payload_hash"]),
        artifact_file_hash=_sha256(data),
        part_hashes=tuple(part_hashes),
    )


def validate_runtime_scoring_artifact(
    path: Path | str,
    *,
    max_decompressed_member_bytes: int = DEFAULT_MAX_DECOMPRESSED_MEMBER_BYTES,
) -> ArtifactValidationResult:
    artifact_path = Path(path)
    try:
        if artifact_path.name == _INDEX_NAME:
            return _validate_index(
                artifact_path,
                max_decompressed_member_bytes=max_decompressed_member_bytes,
            )
        if artifact_path.name == _ARTIFACT_NAME:
            return _validate_single(
                artifact_path,
                max_decompressed_member_bytes=max_decompressed_member_bytes,
            )
        raise ArtifactValidationError("unsupported artifact filename")
    except ArtifactValidationError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ArtifactValidationError("artifact validation failed") from error


__all__ = [
    "ArtifactAssetInput",
    "ArtifactSecurityError",
    "ArtifactValidationError",
    "ArtifactValidationResult",
    "ArtifactWriteResult",
    "DEFAULT_HARD_BUDGET_BYTES",
    "DEFAULT_MAX_DECOMPRESSED_MEMBER_BYTES",
    "DEFAULT_SOFT_BUDGET_BYTES",
    "SCHEMA_VERSION",
    "SCHEMA_1_ENUM_DOMAINS",
    "canonical_json_bytes",
    "serialize_artifact_presentation",
    "serialize_runtime_trace_deterministic",
    "trace_sha256",
    "validate_runtime_scoring_artifact",
    "write_runtime_scoring_artifact",
]
