from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from advisor.models import AssetDecision, AssetSnapshot


SCHEMA_VERSION = "1.0"
REPORT_TYPES = frozenset({"main", "close"})
RUN_ORIGINS = frozenset({"github", "local"})
ASSET_TYPES = frozenset({"stock", "crypto"})
ENTRY_SEMANTICS = frozenset({"reference_close_not_fill"})
ALTERNATIVE_ENTRY_SEMANTICS = frozenset({"conditional_untracked", "not_present"})
EVALUATION_ROLES = frozenset(
    {
        "trade_candidate",
        "conditional_candidate",
        "observational_candidate",
        "observational_wait",
        "observational_avoid",
        "observational_blocked",
        "observational_other",
    }
)
_SOURCE_SHA_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PROVENANCE_FORBIDDEN_PATTERN = re.compile(
    r"(?:api[_-]?key|authorization|bearer\s|https?://|[A-Za-z]:\\|exception|headers?)",
    re.IGNORECASE,
)
# Brazil has observed UTC-03:00 without DST since 2019.  Keep the persisted
# timezone label explicit while using the same dependency-free fixed offset as
# the rest of this project (the bundled Python runtime has no tzdata package).
_BRT = timezone(timedelta(hours=-3))


@dataclass(frozen=True)
class SignalRunMetadata:
    schema_version: str
    source_sha: str
    run_id: str
    run_origin: str
    report_date_brt: str
    report_type: str
    signal_timestamp_utc: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported_signal_observation_schema_version")
        if validate_source_sha(self.source_sha) is None:
            raise ValueError("invalid_source_sha")
        if self.run_origin not in RUN_ORIGINS:
            raise ValueError("invalid_run_origin")
        if not _RUN_ID_PATTERN.fullmatch(self.run_id):
            raise ValueError("invalid_run_id")
        if self.report_type not in REPORT_TYPES:
            raise ValueError("invalid_report_type")
        timestamp = canonical_utc_timestamp(self.signal_timestamp_utc)
        if timestamp != self.signal_timestamp_utc:
            object.__setattr__(self, "signal_timestamp_utc", timestamp)
        expected_date = _parse_utc(timestamp).astimezone(_BRT).date().isoformat()
        if self.report_date_brt != expected_date:
            raise ValueError("report_date_brt_mismatch")


@dataclass(frozen=True)
class SignalObservation:
    signal_id: str
    schema_version: str
    source_sha: str
    run_id: str
    run_origin: str
    report_date_brt: str
    report_type: str
    signal_timestamp_utc: str
    asset: str
    asset_type: str
    universe_origin: str
    market_session: str
    market_timezone: str
    decision_label: str
    bucket: str
    investment_quality_score: float
    swing_trade_score: float
    decision_confidence_score: int
    data_quality_score: int
    expected_value_r: float | None
    backtest_sample_size: int
    sample_quality: str | None
    data_quality: str
    missing_data_severity: str
    ideal_entry: float
    alternative_entry: float | None
    entry_semantics: str
    alternative_entry_semantics: str
    stop: float
    target_2r: float
    target_3r: float
    per_unit_risk: float
    risk_amount: float
    risk_fraction: float
    max_position_units: float
    max_position_value: float
    reason_codes: tuple[str, ...]
    data_source: str
    data_timestamp: str | None
    last_price_timestamp: str | None
    provider: str
    is_stale: bool
    stock_regime: str
    crypto_regime: str
    relative_strength_vs_spy: float | None
    relative_strength_vs_qqq: float | None
    relative_strength_vs_sector: float | None
    sector_benchmark: str | None
    evaluation_role: str
    provenance_json: str
    observation_hash: str = ""
    persisted_at_utc: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported_signal_observation_schema_version")
        if validate_source_sha(self.source_sha) is None:
            raise ValueError("invalid_source_sha")
        if self.run_origin not in RUN_ORIGINS or not _RUN_ID_PATTERN.fullmatch(self.run_id):
            raise ValueError("invalid_run_metadata")
        if self.report_type not in REPORT_TYPES:
            raise ValueError("invalid_report_type")
        if self.asset_type not in ASSET_TYPES:
            raise ValueError("invalid_asset_type")
        if self.market_timezone != (
            "America/New_York" if self.asset_type == "stock" else "UTC"
        ):
            raise ValueError("invalid_market_timezone")
        if self.entry_semantics not in ENTRY_SEMANTICS:
            raise ValueError("invalid_entry_semantics")
        if self.alternative_entry_semantics not in ALTERNATIVE_ENTRY_SEMANTICS:
            raise ValueError("invalid_alternative_entry_semantics")
        if self.evaluation_role not in EVALUATION_ROLES:
            raise ValueError("invalid_evaluation_role")
        if not isinstance(self.is_stale, bool):
            raise ValueError("invalid_is_stale")
        if not re.fullmatch(r"[0-9a-f]{64}", self.signal_id):
            raise ValueError("invalid_signal_id")
        expected_signal_id = compute_signal_id(
            {
                "schema_version": self.schema_version,
                "source_sha": self.source_sha,
                "run_id": self.run_id,
                "report_type": self.report_type,
                "symbol": self.asset,
            }
        )
        if self.signal_id != expected_signal_id:
            raise ValueError("signal_id_identity_mismatch")
        timestamp = canonical_utc_timestamp(self.signal_timestamp_utc)
        expected_date = _parse_utc(timestamp).astimezone(_BRT).date().isoformat()
        if self.report_date_brt != expected_date:
            raise ValueError("report_date_brt_mismatch")
        if self.persisted_at_utc is not None:
            canonical_utc_timestamp(self.persisted_at_utc)


@dataclass(frozen=True)
class SignalObservationWriteResult:
    status: str
    written: int = 0
    duplicate_same: int = 0
    conflicts: tuple[str, ...] = field(default_factory=tuple)


def validate_source_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if _SOURCE_SHA_PATTERN.fullmatch(value) else None


def canonical_utc_timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_utc_timestamp")
    try:
        parsed = _parse_utc(value)
    except (TypeError, ValueError):
        raise ValueError("invalid_utc_timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("utc_timestamp_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def create_run_metadata(
    *,
    source_sha: str,
    report_type: str,
    signal_timestamp_utc: str | None = None,
    run_id: str | None = None,
    run_origin: str | None = None,
) -> SignalRunMetadata:
    timestamp = canonical_utc_timestamp(signal_timestamp_utc or datetime.now(timezone.utc).isoformat())
    resolved_run_id = run_id
    resolved_origin = run_origin
    if resolved_run_id is None:
        github_run_id = os.getenv("GITHUB_RUN_ID", "")
        if github_run_id and github_run_id.isdigit():
            resolved_run_id = github_run_id
            resolved_origin = "github"
        else:
            resolved_run_id = f"local-{uuid.uuid4().hex}"
            resolved_origin = "local"
    if resolved_origin is None:
        resolved_origin = "github" if resolved_run_id.isdigit() else "local"
    report_date_brt = _parse_utc(timestamp).astimezone(_BRT).date().isoformat()
    return SignalRunMetadata(
        schema_version=SCHEMA_VERSION,
        source_sha=source_sha,
        run_id=resolved_run_id,
        run_origin=resolved_origin,
        report_date_brt=report_date_brt,
        report_type=report_type,
        signal_timestamp_utc=timestamp,
    )


def compute_signal_id(identity: Mapping[str, object]) -> str:
    required = {
        "schema_version",
        "source_sha",
        "run_id",
        "report_type",
        "symbol",
    }
    if set(identity) != required:
        raise ValueError("invalid_signal_identity")
    return _sha256(canonical_json_bytes(dict(identity)))


def compute_observation_hash(observation: SignalObservation) -> str:
    payload = observation_immutable_dict(observation)
    return _sha256(canonical_json_bytes(payload))


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("canonical_json_serialization_error") from error


def build_signal_observation(
    decision: AssetDecision,
    snapshot: AssetSnapshot,
    run_metadata: SignalRunMetadata,
    *,
    stock_regime: str,
    crypto_regime: str,
    persisted_at_utc: str | None = None,
) -> SignalObservation:
    if decision.symbol != snapshot.symbol or decision.asset_type != snapshot.asset_type:
        raise ValueError("decision_snapshot_identity_mismatch")
    if snapshot.asset_type not in ASSET_TYPES:
        raise ValueError("invalid_asset_type")
    stats = decision.backtest_stats
    identity = {
        "schema_version": run_metadata.schema_version,
        "source_sha": run_metadata.source_sha,
        "run_id": run_metadata.run_id,
        "report_type": run_metadata.report_type,
        "symbol": decision.symbol,
    }
    alternative_semantics = "conditional_untracked" if decision.alternative_entry is not None else "not_present"
    observation = SignalObservation(
        signal_id=compute_signal_id(identity),
        schema_version=run_metadata.schema_version,
        source_sha=run_metadata.source_sha,
        run_id=run_metadata.run_id,
        run_origin=run_metadata.run_origin,
        report_date_brt=run_metadata.report_date_brt,
        report_type=run_metadata.report_type,
        signal_timestamp_utc=run_metadata.signal_timestamp_utc,
        asset=decision.symbol,
        asset_type=decision.asset_type,
        universe_origin=decision.universe_origin,
        market_session=decision.market_session,
        market_timezone="America/New_York" if decision.asset_type == "stock" else "UTC",
        decision_label=decision.decision,
        bucket=decision.bucket,
        investment_quality_score=decision.investment_quality_score,
        swing_trade_score=decision.swing_trade_score,
        decision_confidence_score=decision.decision_confidence_score,
        data_quality_score=decision.data_quality_score,
        expected_value_r=stats.expected_value_r if stats else None,
        backtest_sample_size=stats.sample_size if stats else 0,
        sample_quality=decision.sample_quality,
        data_quality=decision.data_quality,
        missing_data_severity=decision.missing_data_severity,
        ideal_entry=decision.ideal_entry,
        alternative_entry=decision.alternative_entry,
        entry_semantics="reference_close_not_fill",
        alternative_entry_semantics=alternative_semantics,
        stop=decision.risk_plan.stop,
        target_2r=decision.risk_plan.target_2r,
        target_3r=decision.risk_plan.target_3r,
        per_unit_risk=decision.risk_plan.per_unit_risk,
        risk_amount=decision.risk_plan.risk_amount,
        risk_fraction=decision.risk_plan.risk_fraction,
        max_position_units=decision.risk_plan.max_position_units,
        max_position_value=decision.risk_plan.max_position_value,
        reason_codes=tuple(sorted(str(code) for code in decision.reason_codes)),
        data_source=_safe_observation_text(decision.data_source),
        data_timestamp=decision.data_timestamp,
        last_price_timestamp=decision.last_price_timestamp,
        provider=_safe_observation_text(decision.provider),
        is_stale=decision.is_stale,
        stock_regime=stock_regime,
        crypto_regime=crypto_regime,
        relative_strength_vs_spy=decision.relative_strength_vs_spy,
        relative_strength_vs_qqq=decision.relative_strength_vs_qqq,
        relative_strength_vs_sector=decision.relative_strength_vs_sector,
        sector_benchmark=decision.sector_benchmark,
        evaluation_role=evaluation_role_for_decision(decision.decision),
        provenance_json=build_provenance_json(decision, snapshot),
        persisted_at_utc=persisted_at_utc,
    )
    return replace(observation, observation_hash=compute_observation_hash(observation))


def evaluation_role_for_decision(decision: str) -> str:
    return {
        "tradeable": "trade_candidate",
        "watch_buy": "conditional_candidate",
        "technical_unvalidated": "observational_candidate",
        "wait": "observational_wait",
        "avoid": "observational_avoid",
        "blocked": "observational_blocked",
    }.get(decision, "observational_other")


def build_provenance_json(decision: AssetDecision, snapshot: AssetSnapshot) -> str:
    metadata = snapshot.data_fetch_metadata
    values: dict[str, object] = {
        "data_source": decision.data_source,
        "data_timestamp": decision.data_timestamp,
        "last_price_timestamp": decision.last_price_timestamp,
        "provider": decision.provider,
        "cache_age_seconds": decision.cache_age_seconds,
        "quote_status": snapshot.quote_status,
        "quote_timestamp": snapshot.quote_timestamp,
        "quote_source": snapshot.quote_source,
        "quote_age_seconds": snapshot.quote_age_seconds,
        "quote_is_intraday": snapshot.quote_is_intraday,
        "fetched_at": metadata.fetched_at if metadata else None,
        "cache_fetched_at": metadata.cache_fetched_at if metadata else None,
        "source_timestamp": metadata.source_timestamp if metadata else None,
        "source_age_seconds": metadata.source_age_seconds if metadata else None,
        "cache_hit": metadata.cache_hit if metadata else None,
        "fallback_used": metadata.fallback_used if metadata else None,
        "fallback_from": metadata.fallback_from if metadata else None,
        "fallback_to": metadata.fallback_to if metadata else None,
        "granularity": metadata.granularity if metadata else None,
        "market_data_kind": metadata.market_data_kind if metadata else None,
    }
    sanitized = {
        key: safe_value
        for key in sorted(values)
        if values[key] is not None
        for safe_value in [_safe_provenance_value(values[key])]
        if safe_value is not None
    }
    return canonical_json_bytes(sanitized).decode("utf-8")


def _safe_observation_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "unknown"
    if _PROVENANCE_FORBIDDEN_PATTERN.search(value):
        return "unknown"
    return value


def _safe_provenance_value(value: object) -> object | None:
    if isinstance(value, str) and _PROVENANCE_FORBIDDEN_PATTERN.search(value):
        return None
    return value


def observation_immutable_dict(observation: SignalObservation) -> dict[str, object]:
    values = asdict(observation)
    values.pop("observation_hash", None)
    values.pop("persisted_at_utc", None)
    values.pop("signal_timestamp_utc", None)
    return values


def observation_record(observation: SignalObservation, *, persisted_at_utc: str) -> dict[str, object]:
    record = asdict(observation)
    record["reason_codes"] = json.dumps(list(observation.reason_codes), ensure_ascii=False, separators=(",", ":"))
    record["persisted_at_utc"] = persisted_at_utc
    record["is_stale"] = int(observation.is_stale)
    return record


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
