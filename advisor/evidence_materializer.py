from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from advisor import evidence_archive as _evidence_archive
from advisor.evidence_archive import _load_authority
from advisor.evidence_schema import (
    CorporateActionReasonCode,
    CryptoPolicy,
    HorizonStatus,
    ObservationEvidenceRecord,
    ObservationSourceBinding,
    SignalBasisStatus,
    SplitPolicy,
    resolve_signal_price_basis_status,
)
from advisor.models import PriceBasisClaim
from advisor.signal_observation import (
    SignalObservation,
    compute_observation_hash,
    compute_signal_id,
)
from advisor.signal_outcome import (
    HORIZONS,
    ForwardCandle,
    ForwardMarketSeries,
    signal_market_date,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OBSERVATION_FIELDS = frozenset(
    {
        "signal_id",
        "schema_version",
        "source_sha",
        "run_id",
        "run_origin",
        "report_date_brt",
        "report_type",
        "signal_timestamp_utc",
        "asset",
        "asset_type",
        "universe_origin",
        "market_session",
        "market_timezone",
        "decision_label",
        "bucket",
        "investment_quality_score",
        "swing_trade_score",
        "decision_confidence_score",
        "data_quality_score",
        "expected_value_r",
        "backtest_sample_size",
        "sample_quality",
        "data_quality",
        "missing_data_severity",
        "ideal_entry",
        "alternative_entry",
        "entry_semantics",
        "alternative_entry_semantics",
        "stop",
        "target_2r",
        "target_3r",
        "per_unit_risk",
        "risk_amount",
        "risk_fraction",
        "max_position_units",
        "max_position_value",
        "reason_codes",
        "data_source",
        "data_timestamp",
        "last_price_timestamp",
        "provider",
        "is_stale",
        "stock_regime",
        "crypto_regime",
        "relative_strength_vs_spy",
        "relative_strength_vs_qqq",
        "relative_strength_vs_sector",
        "sector_benchmark",
        "evaluation_role",
        "provenance_json",
        "observation_hash",
        "persisted_at_utc",
    }
)
_SOURCE_BINDING_FIELDS = frozenset(
    {"signal_id", "observation_hash", "snapshot_sha256", "price_basis_claim"}
)
_CLAIM_FIELDS = frozenset(
    {"price_basis", "price_basis_policy_version", "source_contract"}
)
_MARKET_IDENTITY_FIELDS = frozenset(
    {"asset_type", "interval", "market_date", "market_timezone", "schema_version", "symbol"}
)
_MARKET_PAYLOAD_FIELDS = frozenset(
    {
        "asset_type",
        "interval",
        "market_date",
        "market_timezone",
        "ohlcv",
        "price_basis",
        "session_close_type",
        "session_status",
    }
)
_OHLCV_FIELDS = frozenset({"close", "high", "low", "open", "volume"})
_CORPORATE_IDENTITY_FIELDS = frozenset(
    {
        "asset_type",
        "corporate_action_provider",
        "coverage_end_date",
        "coverage_start_date",
        "function",
        "schema_version",
        "symbol",
    }
)
_CORPORATE_PAYLOAD_FIELDS = frozenset({"data", "normalized_events", "symbol"})
_NORMALIZED_EVENT_FIELDS = frozenset(
    {"effective_date", "split_factor_raw", "split_ratio"}
)
_SPLIT_RATIO_FIELDS = frozenset({"new_shares", "old_shares"})
_ELIGIBLE_TERMINAL_STATUSES = frozenset({"verified_none", "not_applicable"})
_HORIZON_STATUSES = frozenset(
    {
        "market_data_unavailable",
        "pending",
        "conflict",
        "signal_basis_unavailable",
        "feed_unavailable",
        "split_in_horizon_unavailable",
        "verified_none",
        "not_applicable",
    }
)


class MaterializationError(ValueError):
    """A canonical evidence checkout cannot provide a valid materialization input."""


@dataclass(frozen=True)
class HorizonQualification:
    status: HorizonStatus
    policy: SplitPolicy | CryptoPolicy | None
    reason_code: CorporateActionReasonCode | None
    proof: Mapping[str, object] | None

    def __post_init__(self) -> None:
        if self.status not in _HORIZON_STATUSES:
            raise ValueError("invalid_horizon_status")
        if self.reason_code is not None and self.status != "conflict":
            raise ValueError("reason_code_requires_conflict")
        if self.status == "conflict" and self.reason_code != "corporate_action_revision_conflict":
            raise ValueError("conflict_reason_code_required")
        if self.status == "verified_none" and self.policy != "verified_no_split_in_signal_horizon_v1":
            raise ValueError("verified_none_requires_split_policy")
        if self.status == "not_applicable" and self.policy != "not_applicable_crypto_raw_ohlcv_v1":
            raise ValueError("not_applicable_requires_crypto_policy")
        if self.proof is not None and not isinstance(self.proof, Mapping):
            raise TypeError("proof must be a mapping")


@dataclass(frozen=True)
class MaterializationResult:
    completed_horizons: tuple[str, ...]
    pending_horizons: tuple[str, ...]
    proof_transport: Path | None
    outcome_transport: Path | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "completed_horizons", tuple(self.completed_horizons))
        object.__setattr__(self, "pending_horizons", tuple(self.pending_horizons))


@dataclass(frozen=True)
class _MarketEvidence:
    candle: ForwardCandle
    canonical_content_sha256: str
    provider: str


@dataclass(frozen=True)
class _CorporateEvidence:
    coverage_start: date
    coverage_end: date
    normalized_events: tuple[Mapping[str, object], ...]
    canonical_content_sha256: str


def _require_mapping(value: object, *, error_code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MaterializationError(error_code)
    return value


def _require_sha256(value: object, *, error_code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MaterializationError(error_code)
    return value


def _require_date(value: object, *, error_code: str) -> date:
    if not isinstance(value, str):
        raise MaterializationError(error_code)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise MaterializationError(error_code) from exc


def _validate_json_value(value: object) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MaterializationError("non_finite_json_value")
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise MaterializationError("non_string_json_object_key")
        for item in value.values():
            _validate_json_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_value(item)
        return
    raise MaterializationError("unsupported_json_value")


def _claim_from_mapping(value: object) -> PriceBasisClaim | None:
    if value is None:
        return None
    claim = _require_mapping(value, error_code="invalid_price_basis_claim")
    if set(claim) != _CLAIM_FIELDS or any(not isinstance(item, str) for item in claim.values()):
        raise MaterializationError("invalid_price_basis_claim")
    return PriceBasisClaim(
        price_basis=claim["price_basis"],  # type: ignore[arg-type]
        price_basis_policy_version=claim["price_basis_policy_version"],  # type: ignore[arg-type]
        source_contract=claim["source_contract"],  # type: ignore[arg-type]
    )


def _record_from_shard(shard: Any) -> ObservationEvidenceRecord:
    payload = _require_mapping(shard.envelope.get("payload"), error_code="invalid_observation_payload")
    if set(payload) != _OBSERVATION_FIELDS:
        raise MaterializationError("invalid_observation_payload")

    observation_values = {
        field_name: payload[field_name] for field_name in _OBSERVATION_FIELDS
    }
    reason_codes = observation_values["reason_codes"]
    if not isinstance(reason_codes, list) or any(not isinstance(code, str) for code in reason_codes):
        raise MaterializationError("invalid_observation_reason_codes")
    if observation_values["persisted_at_utc"] is not None:
        raise MaterializationError("canonical_observation_persisted_timestamp")
    observation_values["reason_codes"] = tuple(reason_codes)
    try:
        observation = SignalObservation(**observation_values)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise MaterializationError("invalid_observation") from exc
    if compute_observation_hash(observation) != observation.observation_hash:
        raise MaterializationError("observation_hash_mismatch")

    logical_identity = _require_mapping(
        shard.envelope.get("logical_identity"),
        error_code="invalid_observation_identity",
    )
    expected_identity = {
        "report_type": observation.report_type,
        "run_id": observation.run_id,
        "schema_version": observation.schema_version,
        "source_sha": observation.source_sha,
        "symbol": observation.asset,
    }
    if dict(logical_identity) != expected_identity:
        raise MaterializationError("observation_identity_mismatch")
    if compute_signal_id(expected_identity) != observation.signal_id:
        raise MaterializationError("signal_id_identity_mismatch")

    semantic_provenance = _require_mapping(
        shard.envelope.get("semantic_provenance"),
        error_code="invalid_observation_provenance",
    )
    binding_values = dict(
        _require_mapping(
            semantic_provenance.get("source_binding"),
            error_code="invalid_source_binding",
        )
    )
    if set(binding_values) != _SOURCE_BINDING_FIELDS:
        raise MaterializationError("invalid_source_binding_fields")
    binding = ObservationSourceBinding(
        signal_id=binding_values["signal_id"],  # type: ignore[arg-type]
        observation_hash=binding_values["observation_hash"],  # type: ignore[arg-type]
        snapshot_sha256=_require_sha256(
            binding_values["snapshot_sha256"], error_code="invalid_snapshot_sha256"
        ),
        price_basis_claim=_claim_from_mapping(binding_values["price_basis_claim"]),
    )
    if binding.signal_id != observation.signal_id:
        raise MaterializationError("source_binding_signal_id_mismatch")
    if binding.observation_hash != observation.observation_hash:
        raise MaterializationError("source_binding_observation_hash_mismatch")
    _require_sha256(binding.observation_hash, error_code="invalid_observation_hash")
    return ObservationEvidenceRecord(observation=observation, source_binding=binding)


def _parse_market_shard(shard: Any, *, symbol: str, asset_type: str) -> _MarketEvidence:
    logical_identity = _require_mapping(
        shard.envelope.get("logical_identity"), error_code="invalid_market_identity"
    )
    if set(logical_identity) != _MARKET_IDENTITY_FIELDS:
        raise MaterializationError("invalid_market_identity")
    market_date = _require_date(logical_identity["market_date"], error_code="invalid_market_date")
    market_timezone = "UTC" if asset_type == "crypto" else "America/New_York"
    expected_identity = {
        "asset_type": asset_type,
        "interval": "1d",
        "market_date": market_date.isoformat(),
        "market_timezone": market_timezone,
        "schema_version": "1.0",
        "symbol": symbol,
    }
    if dict(logical_identity) != expected_identity:
        raise MaterializationError("market_identity_mismatch")

    payload = _require_mapping(shard.envelope.get("payload"), error_code="invalid_market_payload")
    if set(payload) != _MARKET_PAYLOAD_FIELDS:
        raise MaterializationError("invalid_market_payload")
    if (
        payload["asset_type"] != asset_type
        or payload["interval"] != "1d"
        or payload["market_date"] != market_date.isoformat()
        or payload["market_timezone"] != market_timezone
        or payload["price_basis"] != "raw_ohlcv"
        or payload["session_status"] != "complete"
    ):
        raise MaterializationError("market_payload_identity_mismatch")
    ohlcv = _require_mapping(payload["ohlcv"], error_code="invalid_market_ohlcv")
    if set(ohlcv) != _OHLCV_FIELDS:
        raise MaterializationError("incomplete_market_ohlcv")
    try:
        candle = ForwardCandle.from_mapping(
            {
                "date": market_date.isoformat(),
                "open": ohlcv["open"],
                "high": ohlcv["high"],
                "low": ohlcv["low"],
                "close": ohlcv["close"],
                "volume": ohlcv["volume"],
            }
        )
    except (TypeError, ValueError) as exc:
        raise MaterializationError("invalid_market_ohlcv") from exc
    provenance = _require_mapping(
        shard.envelope.get("semantic_provenance"), error_code="invalid_market_provenance"
    )
    provider = provenance.get("price_provider")
    if not isinstance(provider, str) or not provider:
        raise MaterializationError("invalid_market_provider")
    return _MarketEvidence(
        candle=candle,
        canonical_content_sha256=_require_sha256(
            shard.envelope.get("canonical_content_sha256"),
            error_code="invalid_market_content_hash",
        ),
        provider=provider,
    )


def _parse_corporate_shard(shard: Any, *, symbol: str, asset_type: str) -> _CorporateEvidence:
    logical_identity = _require_mapping(
        shard.envelope.get("logical_identity"), error_code="invalid_corporate_identity"
    )
    if set(logical_identity) != _CORPORATE_IDENTITY_FIELDS:
        raise MaterializationError("invalid_corporate_identity")
    if (
        logical_identity["asset_type"] != asset_type
        or logical_identity["corporate_action_provider"] != "alpha_vantage"
        or logical_identity["function"] != "SPLITS"
        or logical_identity["schema_version"] != "1.0"
        or logical_identity["symbol"] != symbol
    ):
        raise MaterializationError("corporate_identity_mismatch")
    coverage_start = _require_date(
        logical_identity["coverage_start_date"], error_code="invalid_coverage_start_date"
    )
    coverage_end = _require_date(
        logical_identity["coverage_end_date"], error_code="invalid_coverage_end_date"
    )
    if coverage_start > coverage_end:
        raise MaterializationError("invalid_coverage_window")

    payload = _require_mapping(
        shard.envelope.get("payload"), error_code="invalid_corporate_payload"
    )
    if set(payload) != _CORPORATE_PAYLOAD_FIELDS or payload["symbol"] != symbol:
        raise MaterializationError("invalid_corporate_payload")
    _validate_json_value(payload["data"])
    normalized_events = payload["normalized_events"]
    if not isinstance(normalized_events, list):
        raise MaterializationError("invalid_normalized_events")
    parsed_events: list[Mapping[str, object]] = []
    for event in normalized_events:
        event_mapping = _require_mapping(event, error_code="invalid_normalized_event")
        if set(event_mapping) != _NORMALIZED_EVENT_FIELDS:
            raise MaterializationError("invalid_normalized_event")
        _require_date(event_mapping["effective_date"], error_code="invalid_split_date")
        if not isinstance(event_mapping["split_factor_raw"], str):
            raise MaterializationError("invalid_split_factor")
        ratio = _require_mapping(
            event_mapping["split_ratio"], error_code="invalid_split_ratio"
        )
        if set(ratio) != _SPLIT_RATIO_FIELDS or any(
            not isinstance(value, str) or not value for value in ratio.values()
        ):
            raise MaterializationError("invalid_split_ratio")
        parsed_events.append(dict(event_mapping))
    return _CorporateEvidence(
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        normalized_events=tuple(parsed_events),
        canonical_content_sha256=_require_sha256(
            shard.envelope.get("canonical_content_sha256"),
            error_code="invalid_corporate_content_hash",
        ),
    )


class EvidenceMaterializer:
    def __init__(self, *, evidence_checkout: Path, db_path: Path):
        if not isinstance(evidence_checkout, Path):
            raise TypeError("evidence_checkout must be a Path")
        if not isinstance(db_path, Path):
            raise TypeError("db_path must be a Path")
        self.evidence_checkout = evidence_checkout
        self.db_path = db_path

    def _load(self):
        try:
            return _load_authority(self.evidence_checkout, branch_name="advisor-evidence")
        except (_evidence_archive._ArchiveError, OSError, TypeError, ValueError) as exc:
            error_code = getattr(exc, "error_code", None) or str(exc) or "invalid_evidence_checkout"
            raise MaterializationError(error_code) from exc

    def _read_observation_record_with_hash(
        self, *, signal_id: str, observation_hash: str
    ) -> tuple[ObservationEvidenceRecord, str]:
        _require_sha256(signal_id, error_code="invalid_signal_id")
        _require_sha256(observation_hash, error_code="invalid_observation_hash")
        shards, _ = self._load()
        matches: list[tuple[ObservationEvidenceRecord, str]] = []
        for shard in shards.values():
            if shard.evidence_type != "observation":
                continue
            record = _record_from_shard(shard)
            if (
                record.observation.signal_id == signal_id
                and record.observation.observation_hash == observation_hash
            ):
                matches.append(
                    (
                        record,
                        _require_sha256(
                            shard.envelope.get("canonical_content_sha256"),
                            error_code="invalid_observation_content_hash",
                        ),
                    )
                )
        if not matches:
            raise MaterializationError("canonical_observation_missing")
        if len(matches) != 1:
            raise MaterializationError("ambiguous_canonical_observation")
        return matches[0]

    def read_observation_evidence_record(
        self, *, signal_id: str, observation_hash: str
    ) -> ObservationEvidenceRecord:
        record, _ = self._read_observation_record_with_hash(
            signal_id=signal_id,
            observation_hash=observation_hash,
        )
        return record

    def _market_evidence(
        self, *, symbol: str, asset_type: str
    ) -> list[_MarketEvidence]:
        shards, _ = self._load()
        evidence: list[_MarketEvidence] = []
        for shard in shards.values():
            if shard.evidence_type != "market_bar":
                continue
            identity = shard.envelope.get("logical_identity")
            if not isinstance(identity, Mapping):
                continue
            if identity.get("symbol") != symbol or identity.get("asset_type") != asset_type:
                continue
            evidence.append(_parse_market_shard(shard, symbol=symbol, asset_type=asset_type))
        if not evidence:
            raise MaterializationError("market_data_unavailable")
        evidence.sort(key=lambda item: item.candle.date)
        if len({item.candle.date for item in evidence}) != len(evidence):
            raise MaterializationError("duplicate_market_date")
        if len({item.provider for item in evidence}) != 1:
            raise MaterializationError("mixed_price_provider")
        return evidence

    def _corporate_evidence(self, *, symbol: str, asset_type: str) -> list[_CorporateEvidence]:
        shards, _ = self._load()
        evidence: list[_CorporateEvidence] = []
        for shard in shards.values():
            if shard.evidence_type != "corporate_action":
                continue
            identity = shard.envelope.get("logical_identity")
            if not isinstance(identity, Mapping):
                continue
            if identity.get("symbol") != symbol or identity.get("asset_type") != asset_type:
                continue
            evidence.append(_parse_corporate_shard(shard, symbol=symbol, asset_type=asset_type))
        return evidence

    def _has_relevant_corporate_conflict(
        self, *, symbol: str, horizon_start: date, horizon_end: date
    ) -> bool:
        conflict_root = self.evidence_checkout / "evidence" / "conflicts"
        if not conflict_root.exists():
            return False
        for path in sorted(conflict_root.rglob("*.json.gz")):
            relative = path.relative_to(self.evidence_checkout).as_posix()
            try:
                conflict = _evidence_archive._read_conflict(
                    self.evidence_checkout, relative
                )
            except (_evidence_archive._ArchiveError, OSError, TypeError, ValueError) as exc:
                raise MaterializationError("invalid_conflict") from exc
            identity = conflict.identity
            if (
                identity.get("evidence_type") != "corporate_action"
                or identity.get("reason_code") != "corporate_action_revision_conflict"
            ):
                continue
            logical_identity = identity.get("logical_identity")
            if not isinstance(logical_identity, Mapping) or logical_identity.get("symbol") != symbol:
                continue
            conflict_start = _require_date(
                logical_identity.get("coverage_start_date"),
                error_code="invalid_conflict_coverage",
            )
            conflict_end = _require_date(
                logical_identity.get("coverage_end_date"),
                error_code="invalid_conflict_coverage",
            )
            if conflict_start <= horizon_end and horizon_start <= conflict_end:
                return True
        return False

    def _forward_market_series(
        self, *, symbol: str, asset_type: str
    ) -> ForwardMarketSeries:
        if asset_type not in {"stock", "crypto"}:
            raise ValueError("unsupported_frozen_asset_type")
        evidence = self._market_evidence(symbol=symbol, asset_type=asset_type)
        return ForwardMarketSeries(
            asset=symbol,
            asset_type=asset_type,
            provider=evidence[0].provider,
            price_basis="split_adjusted_ohlc" if asset_type == "stock" else "raw_ohlcv",
            candles=tuple(item.candle for item in evidence),
        )

    def qualify_horizon(
        self, *, record: ObservationEvidenceRecord, horizon: int
    ) -> HorizonQualification:
        if isinstance(horizon, bool) or horizon not in HORIZONS:
            raise ValueError("invalid_horizon")
        if not isinstance(record, ObservationEvidenceRecord):
            return HorizonQualification(
                status="signal_basis_unavailable",
                policy=None,
                reason_code=None,
                proof=None,
            )
        try:
            canonical_record, observation_shard_hash = self._read_observation_record_with_hash(
                signal_id=record.observation.signal_id,
                observation_hash=record.observation.observation_hash,
            )
        except MaterializationError:
            return HorizonQualification(
                status="signal_basis_unavailable",
                policy=None,
                reason_code=None,
                proof=None,
            )

        observation = canonical_record.observation
        asset_type = observation.asset_type
        if asset_type not in {"stock", "crypto"}:
            return HorizonQualification(
                status="signal_basis_unavailable",
                policy=None,
                reason_code=None,
                proof=None,
            )
        try:
            market_evidence = self._market_evidence(
                symbol=observation.asset,
                asset_type=asset_type,
            )
            market_date = signal_market_date(
                observation.signal_timestamp_utc,
                observation.market_timezone,
            )
            eligible = [
                item
                for item in market_evidence
                if item.candle.date > market_date
            ]
        except (MaterializationError, TypeError, ValueError):
            return HorizonQualification(
                status="market_data_unavailable",
                policy=None,
                reason_code=None,
                proof=None,
            )
        if len(eligible) < horizon:
            return HorizonQualification(
                status="pending",
                policy=None,
                reason_code=None,
                proof=None,
            )
        selected = eligible[:horizon]
        horizon_end = date.fromisoformat(selected[-1].candle.date)
        horizon_start = date.fromisoformat(market_date)

        if asset_type == "crypto":
            return HorizonQualification(
                status="not_applicable",
                policy="not_applicable_crypto_raw_ohlcv_v1",
                reason_code=None,
                proof=None,
            )

        try:
            if self._has_relevant_corporate_conflict(
                symbol=observation.asset,
                horizon_start=horizon_start,
                horizon_end=horizon_end,
            ):
                return HorizonQualification(
                    status="conflict",
                    policy=None,
                    reason_code="corporate_action_revision_conflict",
                    proof=None,
                )
        except MaterializationError:
            return HorizonQualification(
                status="market_data_unavailable",
                policy=None,
                reason_code=None,
                proof=None,
            )

        basis_status: SignalBasisStatus = resolve_signal_price_basis_status(
            record=canonical_record
        )
        if basis_status != "verified_raw_ohlcv":
            return HorizonQualification(
                status="signal_basis_unavailable",
                policy=None,
                reason_code=None,
                proof=None,
            )

        try:
            corporate_evidence = self._corporate_evidence(
                symbol=observation.asset,
                asset_type=asset_type,
            )
        except MaterializationError:
            return HorizonQualification(
                status="feed_unavailable",
                policy=None,
                reason_code=None,
                proof=None,
            )
        covering = [
            item
            for item in corporate_evidence
            if item.coverage_start <= horizon_start
            and item.coverage_end >= horizon_end
        ]
        if not covering:
            return HorizonQualification(
                status="feed_unavailable",
                policy=None,
                reason_code=None,
                proof=None,
            )
        covering.sort(
            key=lambda item: (
                item.coverage_start,
                item.coverage_end,
                item.canonical_content_sha256,
            )
        )
        selected_corporate = covering[-1]
        for candidate in covering[:-1]:
            if (
                candidate.coverage_start <= selected_corporate.coverage_end
                and selected_corporate.coverage_start <= candidate.coverage_end
                and candidate.normalized_events != selected_corporate.normalized_events
            ):
                return HorizonQualification(
                    status="conflict",
                    policy=None,
                    reason_code="corporate_action_revision_conflict",
                    proof=None,
                )
        split_events = [
            event
            for event in selected_corporate.normalized_events
            if horizon_start
            <= date.fromisoformat(str(event["effective_date"]))
            <= horizon_end
        ]
        proof = {
            "signal_market_date": market_date,
            "horizon_start_date": selected[0].candle.date,
            "horizon_end_date": selected[-1].candle.date,
            "horizon_bars": horizon,
            "corporate_action_policy": "verified_no_split_in_signal_horizon_v1",
            "corporate_action_provider": "alpha_vantage",
            "split_check_status": "verified_none" if not split_events else "split_in_horizon_unavailable",
            "split_event_count": len(split_events),
            "price_basis": "split_adjusted_ohlc",
            "raw_price_basis": "raw_ohlcv",
            "signal_price_basis_status": "verified_raw_ohlcv",
            "evidence_hashes": {
                "observation_shard": observation_shard_hash,
                "market_bar_shards": [
                    item.canonical_content_sha256 for item in selected
                ],
                "corporate_action_shard": selected_corporate.canonical_content_sha256,
            },
            "proof_status": (
                "verified_none"
                if not split_events
                else "split_in_horizon_unavailable"
            ),
        }
        if split_events:
            return HorizonQualification(
                status="split_in_horizon_unavailable",
                policy="verified_no_split_in_signal_horizon_v1",
                reason_code=None,
                proof=proof,
            )
        return HorizonQualification(
            status="verified_none",
            policy="verified_no_split_in_signal_horizon_v1",
            reason_code=None,
            proof=proof,
        )

    def materialize(self) -> MaterializationResult:
        shards, _ = self._load()
        records: list[ObservationEvidenceRecord] = []
        for shard in shards.values():
            if shard.evidence_type == "observation":
                records.append(_record_from_shard(shard))
        completed: list[str] = []
        pending: list[str] = []
        for record in sorted(records, key=lambda item: item.observation.signal_id):
            for horizon in HORIZONS:
                qualification = self.qualify_horizon(record=record, horizon=horizon)
                key = f"{record.observation.signal_id}:{horizon}"
                if qualification.status in _ELIGIBLE_TERMINAL_STATUSES:
                    completed.append(key)
                else:
                    pending.append(key)
        return MaterializationResult(
            completed_horizons=tuple(completed),
            pending_horizons=tuple(pending),
            proof_transport=None,
            outcome_transport=None,
        )
