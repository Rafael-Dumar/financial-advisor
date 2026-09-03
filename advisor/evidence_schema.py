from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Sequence

from advisor.models import AssetSnapshot, PriceBasisClaim
from advisor.signal_observation import SignalObservation


ArchiveStatus = Literal[
    "committed",
    "no_op",
    "conflict",
    "rejected",
    "evidence_branch_missing",
]
HorizonStatus = Literal[
    "market_data_unavailable",
    "pending",
    "conflict",
    "signal_basis_unavailable",
    "feed_unavailable",
    "split_in_horizon_unavailable",
    "verified_none",
    "not_applicable",
]
CorporateActionReasonCode = Literal["corporate_action_revision_conflict"]
SignalBasisStatus = Literal[
    "verified_raw_ohlcv",
    "signal_basis_unavailable",
]
SplitPolicy = Literal["verified_no_split_in_signal_horizon_v1"]
CryptoPolicy = Literal["not_applicable_crypto_raw_ohlcv_v1"]


@dataclass(frozen=True)
class ObservationSourceBinding:
    signal_id: str
    observation_hash: str
    snapshot_sha256: str
    price_basis_claim: PriceBasisClaim | None


@dataclass(frozen=True)
class ObservationEvidenceRecord:
    observation: SignalObservation
    source_binding: ObservationSourceBinding


_GZIP_HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff"
_HASH_HEX_LENGTH = hashlib.sha256().digest_size * 2
_ENVELOPE_REQUIRED_KEYS = frozenset(
    {
        "canonical_content_sha256",
        "evidence_type",
        "logical_identity",
        "payload",
        "payload_sha256",
        "schema_version",
        "semantic_provenance",
    }
)
_ENVELOPE_OPTIONAL_KEYS = frozenset({"transport"})


def _normalize_json_value(value: object, *, active_ids: set[int]) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return value

    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in active_ids:
            raise ValueError("circular JSON value")
        active_ids.add(value_id)
        try:
            normalized: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("JSON object keys must be strings")
                if key in normalized:
                    raise ValueError(f"duplicate JSON object key: {key!r}")
                normalized[key] = _normalize_json_value(
                    item, active_ids=active_ids
                )
            return normalized
        finally:
            active_ids.remove(value_id)

    if isinstance(value, (list, tuple)):
        value_id = id(value)
        if value_id in active_ids:
            raise ValueError("circular JSON value")
        active_ids.add(value_id)
        try:
            return [
                _normalize_json_value(item, active_ids=active_ids)
                for item in value
            ]
        finally:
            active_ids.remove(value_id)

    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    normalized = _normalize_json_value(value, active_ids=set())
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return serialized.encode("utf-8")


def _reject_duplicate_object_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_constant(constant: str) -> object:
    raise ValueError(f"non-finite JSON constant: {constant}")


def strict_json_loads_bytes(data: bytes) -> object:
    if not isinstance(data, bytes):
        raise TypeError("strict_json_loads_bytes requires bytes")

    text = data.decode("utf-8", "strict")
    parsed = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_object_pairs,
        parse_constant=_reject_nonfinite_constant,
    )
    return _normalize_json_value(parsed, active_ids=set())


def deterministic_gzip(json_bytes: bytes) -> bytes:
    if not isinstance(json_bytes, bytes):
        raise TypeError("deterministic_gzip requires bytes")

    compressor = zlib.compressobj(
        level=9,
        method=zlib.DEFLATED,
        wbits=-15,
    )
    deflated = compressor.compress(json_bytes) + compressor.flush()
    crc32 = zlib.crc32(json_bytes) & 0xFFFFFFFF
    size = len(json_bytes) & 0xFFFFFFFF
    return _GZIP_HEADER + deflated + struct.pack("<II", crc32, size)


def _gzip_header_end(data: bytes) -> int:
    if len(data) < 10:
        raise ValueError("truncated gzip header")
    if data[:2] != b"\x1f\x8b" or data[2] != 8:
        raise ValueError("invalid gzip signature or compression method")

    flags = data[3]
    if flags & 0xE0:
        raise ValueError("reserved gzip flags are set")

    offset = 10
    if flags & 0x04:
        if offset + 2 > len(data):
            raise ValueError("truncated gzip extra length")
        extra_length = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        if offset + extra_length > len(data):
            raise ValueError("truncated gzip extra field")
        offset += extra_length

    for flag in (0x08, 0x10):
        if flags & flag:
            terminator = data.find(b"\x00", offset)
            if terminator < 0:
                raise ValueError("unterminated gzip header string")
            offset = terminator + 1

    header_without_crc16_end = offset
    if flags & 0x02:
        if offset + 2 > len(data):
            raise ValueError("truncated gzip header CRC")
        expected_crc16 = struct.unpack_from("<H", data, offset)[0]
        actual_crc16 = zlib.crc32(data[:header_without_crc16_end]) & 0xFFFF
        if expected_crc16 != actual_crc16:
            raise ValueError("gzip header CRC mismatch")
        offset += 2

    return offset


def decompress_single_member_gzip(
    data: bytes, *, max_uncompressed_bytes: int
) -> bytes:
    if not isinstance(data, bytes):
        raise TypeError("decompress_single_member_gzip requires bytes")
    if isinstance(max_uncompressed_bytes, bool) or not isinstance(
        max_uncompressed_bytes, int
    ):
        raise TypeError("max_uncompressed_bytes must be an integer")
    if max_uncompressed_bytes < 0:
        raise ValueError("max_uncompressed_bytes must be non-negative")

    compressed_offset = _gzip_header_end(data)
    decompressor = zlib.decompressobj(wbits=-15)
    try:
        output = decompressor.decompress(
            data[compressed_offset:], max_uncompressed_bytes + 1
        )
        if len(output) > max_uncompressed_bytes:
            raise ValueError("gzip decompressed size exceeds limit")
        if decompressor.unconsumed_tail:
            raise ValueError("gzip decompression left unconsumed data")
        if not decompressor.eof:
            raise ValueError("truncated or malformed gzip deflate stream")
        output += decompressor.flush()
    except zlib.error as exc:
        raise ValueError("malformed gzip deflate stream") from exc

    if len(output) > max_uncompressed_bytes:
        raise ValueError("gzip decompressed size exceeds limit")

    trailer_and_trailing = decompressor.unused_data
    if len(trailer_and_trailing) != 8:
        raise ValueError("gzip must contain exactly one member and no trailing data")

    expected_crc32, expected_size = struct.unpack("<II", trailer_and_trailing)
    actual_crc32 = zlib.crc32(output) & 0xFFFFFFFF
    actual_size = len(output) & 0xFFFFFFFF
    if expected_crc32 != actual_crc32:
        raise ValueError("gzip CRC mismatch")
    if expected_size != actual_size:
        raise ValueError("gzip uncompressed size mismatch")
    return output


def _require_nonempty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    return value


def canonical_content_sha256(
    *,
    evidence_type: str,
    schema_version: str,
    logical_identity: Mapping[str, object],
    payload: Mapping[str, object],
    semantic_provenance: Mapping[str, object],
) -> str:
    content = {
        "evidence_type": _require_nonempty_string(
            evidence_type, field_name="evidence_type"
        ),
        "schema_version": _require_nonempty_string(
            schema_version, field_name="schema_version"
        ),
        "logical_identity": _require_mapping(
            logical_identity, field_name="logical_identity"
        ),
        "payload": _require_mapping(payload, field_name="payload"),
        "semantic_provenance": _require_mapping(
            semantic_provenance, field_name="semantic_provenance"
        ),
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def payload_sha256(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _require_sha256_hex(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if len(value) != _HASH_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def validate_canonical_envelope(envelope: Mapping[str, object]) -> None:
    envelope_mapping = _require_mapping(envelope, field_name="envelope")
    keys = set(envelope_mapping.keys())
    allowed_keys = _ENVELOPE_REQUIRED_KEYS | _ENVELOPE_OPTIONAL_KEYS
    if not _ENVELOPE_REQUIRED_KEYS.issubset(keys):
        missing = sorted(_ENVELOPE_REQUIRED_KEYS - keys)
        raise ValueError(f"canonical envelope is missing keys: {missing}")
    unknown = keys - allowed_keys
    if unknown:
        raise ValueError(f"canonical envelope has unknown keys: {sorted(unknown)}")

    schema_version = _require_nonempty_string(
        envelope_mapping["schema_version"], field_name="schema_version"
    )
    evidence_type = _require_nonempty_string(
        envelope_mapping["evidence_type"], field_name="evidence_type"
    )
    logical_identity = _require_mapping(
        envelope_mapping["logical_identity"], field_name="logical_identity"
    )
    payload = _require_mapping(envelope_mapping["payload"], field_name="payload")
    semantic_provenance = _require_mapping(
        envelope_mapping["semantic_provenance"],
        field_name="semantic_provenance",
    )
    if "transport" in envelope_mapping:
        _require_mapping(envelope_mapping["transport"], field_name="transport")

    canonical_json_bytes(envelope_mapping)
    expected_payload_hash = payload_sha256(payload)
    actual_payload_hash = _require_sha256_hex(
        envelope_mapping["payload_sha256"], field_name="payload_sha256"
    )
    if actual_payload_hash != expected_payload_hash:
        raise ValueError("payload_sha256 mismatch")

    expected_content_hash = canonical_content_sha256(
        evidence_type=evidence_type,
        schema_version=schema_version,
        logical_identity=logical_identity,
        payload=payload,
        semantic_provenance=semantic_provenance,
    )
    actual_content_hash = _require_sha256_hex(
        envelope_mapping["canonical_content_sha256"],
        field_name="canonical_content_sha256",
    )
    if actual_content_hash != expected_content_hash:
        raise ValueError("canonical_content_sha256 mismatch")


def _idempotency_field(record: object, field_name: str) -> object:
    mapping = _require_mapping(record, field_name="evidence record")
    if field_name not in mapping:
        raise ValueError(f"evidence record is missing {field_name}")
    return mapping[field_name]


def classify_idempotency(
    existing: Mapping[str, object], incoming: Mapping[str, object]
) -> Literal["duplicate_same", "conflict"]:
    existing_identity = _idempotency_field(existing, "logical_identity")
    incoming_identity = _idempotency_field(incoming, "logical_identity")
    if canonical_json_bytes(existing_identity) != canonical_json_bytes(
        incoming_identity
    ):
        return "conflict"

    existing_hash = _idempotency_field(existing, "canonical_content_sha256")
    incoming_hash = _idempotency_field(incoming, "canonical_content_sha256")
    if not isinstance(existing_hash, str) or not isinstance(incoming_hash, str):
        raise TypeError("canonical_content_sha256 must be a string")
    if not existing_hash or not incoming_hash:
        raise ValueError("canonical_content_sha256 must be non-empty")
    if existing_hash == incoming_hash:
        return "duplicate_same"
    return "conflict"


_SIDECAR_SNAPSHOT_BINDING_VERSION = "observation_snapshot_binding_v1"
_SIDECAR_ENTRY_INTEGRITY_VERSION = "observation_sidecar_entry_integrity_v1"


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _snapshot_json_value_v1(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non_finite_json_number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("non_string_json_object_key")
        return {key: _snapshot_json_value_v1(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot_json_value_v1(item) for item in value]
    raise ValueError("unsupported_snapshot_projection_value")


def _price_basis_claim_projection_v1(
    value: PriceBasisClaim | None,
) -> Mapping[str, object] | None:
    if value is None:
        return None
    return {
        "price_basis": value.price_basis,
        "price_basis_policy_version": value.price_basis_policy_version,
        "source_contract": value.source_contract,
    }


def snapshot_projection_v1(snapshot: AssetSnapshot) -> Mapping[str, object]:
    return {
        "projection_version": "snapshot_projection_v1",
        "symbol": snapshot.symbol,
        "asset_type": snapshot.asset_type,
        "theme": snapshot.theme,
        "candles": [
            {
                "date": value.date,
                "open": value.open,
                "high": value.high,
                "low": value.low,
                "close": value.close,
                "volume": value.volume,
            }
            for value in snapshot.candles
        ],
        "fundamentals": {
            "pe": snapshot.fundamentals.pe,
            "peg": snapshot.fundamentals.peg,
            "historical_pe": snapshot.fundamentals.historical_pe,
            "revenue_growth": snapshot.fundamentals.revenue_growth,
            "eps_growth": snapshot.fundamentals.eps_growth,
            "margin_trend": snapshot.fundamentals.margin_trend,
            "free_cash_flow_positive": snapshot.fundamentals.free_cash_flow_positive,
            "market_cap": snapshot.fundamentals.market_cap,
            "average_volume": snapshot.fundamentals.average_volume,
            "market_cap_rank": snapshot.fundamentals.market_cap_rank,
        },
        "event": None if snapshot.event is None else {
            "days_to_earnings": snapshot.event.days_to_earnings,
            "guidance_recent": snapshot.event.guidance_recent,
            "post_earnings_gap_percent": snapshot.event.post_earnings_gap_percent,
            "last_earnings_date": snapshot.event.last_earnings_date,
            "next_earnings_date": snapshot.event.next_earnings_date,
        },
        "funding_rate": snapshot.funding_rate,
        "open_interest_change": snapshot.open_interest_change,
        "cvd_proxy": snapshot.cvd_proxy,
        "coinbase_premium": snapshot.coinbase_premium,
        "liquidation_imbalance": snapshot.liquidation_imbalance,
        "missing_data": [value for value in snapshot.missing_data],
        "news_events": [_snapshot_json_value_v1(value) for value in snapshot.news_events],
        "provider_capabilities": [
            {
                "provider": value.provider,
                "capability": value.capability,
                "configured": value.configured,
                "supported_by_plan": value.supported_by_plan,
                "implemented": value.implemented,
                "last_status": value.last_status,
                "fallback_available": value.fallback_available,
            }
            for value in snapshot.provider_capabilities
        ],
        "earnings_status": snapshot.earnings_status,
        "guidance_status": snapshot.guidance_status,
        "macro_status": snapshot.macro_status,
        "news_status": snapshot.news_status,
        "sec_filings_status": snapshot.sec_filings_status,
        "data_source": snapshot.data_source,
        "data_timestamp": snapshot.data_timestamp,
        "cache_age_seconds": snapshot.cache_age_seconds,
        "data_fetch_metadata": (
            None if snapshot.data_fetch_metadata is None else {
                "provider": snapshot.data_fetch_metadata.provider,
                "endpoint": snapshot.data_fetch_metadata.endpoint,
                "fetched_at": snapshot.data_fetch_metadata.fetched_at,
                "cache_fetched_at": snapshot.data_fetch_metadata.cache_fetched_at,
                "source_timestamp": snapshot.data_fetch_metadata.source_timestamp,
                "cache_age_seconds": snapshot.data_fetch_metadata.cache_age_seconds,
                "source_age_seconds": snapshot.data_fetch_metadata.source_age_seconds,
                "is_fresh": snapshot.data_fetch_metadata.is_fresh,
                "cache_hit": snapshot.data_fetch_metadata.cache_hit,
                "fallback_used": snapshot.data_fetch_metadata.fallback_used,
                "fallback_from": snapshot.data_fetch_metadata.fallback_from,
                "fallback_to": snapshot.data_fetch_metadata.fallback_to,
                "granularity": snapshot.data_fetch_metadata.granularity,
                "market_data_kind": snapshot.data_fetch_metadata.market_data_kind,
                "price_basis_claim": _price_basis_claim_projection_v1(
                    snapshot.data_fetch_metadata.price_basis_claim
                ),
            }
        ),
        "quote_status": snapshot.quote_status,
        "quote_price": snapshot.quote_price,
        "quote_timestamp": snapshot.quote_timestamp,
        "quote_source": snapshot.quote_source,
        "quote_age_seconds": snapshot.quote_age_seconds,
        "quote_is_intraday": snapshot.quote_is_intraday,
        "previous_close": snapshot.previous_close,
        "daily_change": snapshot.daily_change,
        "daily_change_pct": snapshot.daily_change_pct,
        "benchmark_provenance": _snapshot_json_value_v1(snapshot.benchmark_provenance),
        "crypto_metric_provenance": _snapshot_json_value_v1(
            snapshot.crypto_metric_provenance
        ),
    }


def snapshot_sha256_v1(snapshot: AssetSnapshot) -> str:
    return hashlib.sha256(canonical_json_bytes(snapshot_projection_v1(snapshot))).hexdigest()


def _observation_provenance(observation: SignalObservation) -> Mapping[str, object]:
    raw = observation.provenance_json.encode("utf-8")
    parsed = strict_json_loads_bytes(raw)
    if not isinstance(parsed, Mapping):
        raise TypeError("observation provenance must be a mapping")
    if canonical_json_bytes(parsed) != raw:
        raise ValueError("observation provenance is not canonical")
    return parsed


def _snapshot_provenance(
    observation: SignalObservation,
    snapshot: AssetSnapshot,
) -> Mapping[str, object]:
    metadata = snapshot.data_fetch_metadata
    if not snapshot.candles:
        raise ValueError("observation_snapshot_candles_missing")

    latest_candle = max(snapshot.candles, key=lambda candle: str(candle.date))

    values: dict[str, object] = {
        "data_source": snapshot.data_source,
        "data_timestamp": snapshot.data_timestamp,
        "last_price_timestamp": latest_candle.date,
        "provider": metadata.provider if metadata is not None else observation.provider,
        "cache_age_seconds": snapshot.cache_age_seconds,
        "quote_status": snapshot.quote_status,
        "quote_timestamp": snapshot.quote_timestamp,
        "quote_source": snapshot.quote_source,
        "quote_age_seconds": snapshot.quote_age_seconds,
        "quote_is_intraday": snapshot.quote_is_intraday,
    }
    if metadata is not None:
        values.update(
            {
                "fetched_at": metadata.fetched_at,
                "cache_fetched_at": metadata.cache_fetched_at,
                "source_timestamp": metadata.source_timestamp,
                "source_age_seconds": metadata.source_age_seconds,
                "cache_hit": metadata.cache_hit,
                "fallback_used": metadata.fallback_used,
                "fallback_from": metadata.fallback_from,
                "fallback_to": metadata.fallback_to,
                "granularity": metadata.granularity,
                "market_data_kind": metadata.market_data_kind,
            }
        )
    return {key: value for key, value in values.items() if value is not None}


def _claim_values(claim: object) -> Mapping[str, object] | None:
    if claim is None:
        return None
    return {
        "price_basis": getattr(claim, "price_basis", None),
        "price_basis_policy_version": getattr(
            claim, "price_basis_policy_version", None
        ),
        "source_contract": getattr(claim, "source_contract", None),
    }


def _snapshot_binding(
    observation: SignalObservation,
    snapshot: AssetSnapshot,
    claim_values: Mapping[str, object] | None,
) -> Mapping[str, object]:
    snapshot_provenance = _snapshot_provenance(observation, snapshot)
    latest_candle = max(snapshot.candles, key=lambda candle: str(candle.date))
    return {
        "binding_version": _SIDECAR_SNAPSHOT_BINDING_VERSION,
        "symbol": snapshot.symbol,
        "asset_type": snapshot.asset_type,
        "data_source": snapshot.data_source,
        "data_timestamp": snapshot.data_timestamp,
        "last_candle": {
            "date": latest_candle.date,
            "close": latest_candle.close,
        },
        "observation_provenance": dict(snapshot_provenance),
        "price_basis_claim": claim_values,
    }


def _snapshot_binding_matches_observation(
    observation: SignalObservation,
    snapshot_binding: Mapping[str, object],
    claim_values: Mapping[str, object] | None,
) -> bool:
    expected_snapshot_binding = _expected_snapshot_binding(
        observation,
        claim_values,
    )
    return canonical_json_bytes(snapshot_binding) == canonical_json_bytes(
        expected_snapshot_binding
    )


def _expected_snapshot_binding(
    observation: SignalObservation,
    claim_values: Mapping[str, object] | None,
) -> Mapping[str, object]:
    observation_provenance = _observation_provenance(observation)
    return {
        "binding_version": _SIDECAR_SNAPSHOT_BINDING_VERSION,
        "symbol": observation.asset,
        "asset_type": observation.asset_type,
        "data_source": observation.data_source,
        "data_timestamp": observation.data_timestamp,
        "last_candle": {
            "date": observation.last_price_timestamp,
            "close": observation.ideal_entry,
        },
        "observation_provenance": dict(observation_provenance),
        "price_basis_claim": claim_values,
    }


def _signal_input_hash(
    observation: SignalObservation,
    snapshot_binding: Mapping[str, object],
) -> str:
    return _canonical_sha256(
        {
            "binding_version": _SIDECAR_SNAPSHOT_BINDING_VERSION,
            "signal_id": observation.signal_id,
            "observation_hash": observation.observation_hash,
            "snapshot_binding": snapshot_binding,
        }
    )


def _entry_integrity_sha256(entry: Mapping[str, object]) -> str:
    protected_entry = {
        key: value
        for key, value in entry.items()
        if key != "entry_integrity_sha256"
    }
    return _canonical_sha256(
        {
            "binding_version": _SIDECAR_ENTRY_INTEGRITY_VERSION,
            "entry": protected_entry,
        }
    )


def _observation_sidecar_row(observation: SignalObservation) -> dict[str, object]:
    from dataclasses import asdict

    row = asdict(observation)
    row["reason_codes"] = list(observation.reason_codes)
    row["persisted_at_utc"] = None
    return row


def _source_binding_row(binding: ObservationSourceBinding) -> dict[str, object]:
    return {
        "signal_id": binding.signal_id,
        "observation_hash": binding.observation_hash,
        "snapshot_sha256": binding.snapshot_sha256,
        "price_basis_claim": _price_basis_claim_projection_v1(
            binding.price_basis_claim
        ),
    }


def build_observation_sidecar(
    records: Sequence[ObservationEvidenceRecord],
    *,
    output_path: Path,
) -> Path:
    """Write prebuilt observation/source-binding records without snapshot lookup."""
    from advisor.signal_observation import compute_observation_hash

    if not records:
        raise ValueError("observation_sidecar_requires_records")
    ordered_records = sorted(
        records,
        key=lambda record: record.observation.signal_id,
    )
    first = ordered_records[0].observation
    serialized_records: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    for record in ordered_records:
        observation = record.observation
        binding = record.source_binding
        identity = (observation.signal_id, observation.observation_hash)
        if identity in identities:
            raise ValueError("duplicate_observation_signal_id")
        identities.add(identity)
        if compute_observation_hash(observation) != observation.observation_hash:
            raise ValueError("observation_hash_mismatch")
        if (
            observation.schema_version != first.schema_version
            or observation.source_sha != first.source_sha
            or observation.run_id != first.run_id
            or observation.report_type != first.report_type
        ):
            raise ValueError("observation_sidecar_batch_identity_mismatch")
        if binding.signal_id != observation.signal_id:
            raise ValueError("source_binding_signal_id_mismatch")
        if binding.observation_hash != observation.observation_hash:
            raise ValueError("source_binding_observation_hash_mismatch")
        _require_sha256_hex(binding.snapshot_sha256, field_name="snapshot_sha256")
        serialized_records.append(
            {
                "observation": _observation_sidecar_row(observation),
                "source_binding": _source_binding_row(binding),
            }
        )
    sidecar = {
        "schema_version": first.schema_version,
        "source_sha": first.source_sha,
        "run_id": first.run_id,
        "report_type": first.report_type,
        "records": serialized_records,
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(deterministic_gzip(canonical_json_bytes(sidecar)))
    return path


def resolve_signal_price_basis_status(
    *,
    observation: SignalObservation,
    sidecar: Mapping[str, object],
) -> SignalBasisStatus:
    """Resolve a basis claim from one validated serialized atomic record."""
    from advisor.data_sources import is_qualified_raw_ohlcv_claim
    from advisor.signal_observation import compute_observation_hash

    unavailable: SignalBasisStatus = "signal_basis_unavailable"
    try:
        if not isinstance(sidecar, Mapping) or set(sidecar) != {
            "schema_version", "source_sha", "run_id", "report_type", "records"
        }:
            return unavailable
        if (
            sidecar["schema_version"] != observation.schema_version
            or sidecar["source_sha"] != observation.source_sha
            or sidecar["run_id"] != observation.run_id
            or sidecar["report_type"] != observation.report_type
            or compute_observation_hash(observation) != observation.observation_hash
        ):
            return unavailable
        records = sidecar["records"]
        if not isinstance(records, list):
            return unavailable
        matching_records = [
            record for record in records
            if isinstance(record, Mapping)
            and isinstance(record.get("source_binding"), Mapping)
            and record["source_binding"].get("signal_id") == observation.signal_id
            and record["source_binding"].get("observation_hash") == observation.observation_hash
        ]
        if len(matching_records) != 1:
            return unavailable
        record = matching_records[0]
        if set(record) != {"observation", "source_binding"}:
            return unavailable
        if canonical_json_bytes(record["observation"]) != canonical_json_bytes(
            _observation_sidecar_row(observation)
        ):
            return unavailable
        binding = record["source_binding"]
        if set(binding) != {
            "signal_id", "observation_hash", "snapshot_sha256", "price_basis_claim"
        }:
            return unavailable
        _require_sha256_hex(binding["snapshot_sha256"], field_name="snapshot_sha256")
        claim_values = binding["price_basis_claim"]
        if not isinstance(claim_values, Mapping) or set(claim_values) != {
            "price_basis", "price_basis_policy_version", "source_contract"
        }:
            return unavailable
        claim = PriceBasisClaim(
            price_basis=claim_values["price_basis"],
            price_basis_policy_version=claim_values["price_basis_policy_version"],
            source_contract=claim_values["source_contract"],
        )
        return "verified_raw_ohlcv" if is_qualified_raw_ohlcv_claim(claim) else unavailable
    except (AttributeError, KeyError, TypeError, ValueError):
        return unavailable


def _build_observation_sidecar_legacy(
    observations: Sequence[SignalObservation],
    *,
    snapshots_by_symbol: Mapping[str, AssetSnapshot],
    output_path: Path,
) -> Path:
    """Write the canonical observation list and its bound provenance sidecar."""
    from dataclasses import asdict

    from advisor.data_sources import is_qualified_raw_ohlcv_claim
    from advisor.signal_observation import compute_observation_hash

    if not observations:
        raise ValueError("observation_sidecar_requires_observations")

    ordered = sorted(observations, key=lambda observation: str(observation.signal_id))
    signal_ids = [str(observation.signal_id) for observation in ordered]
    if len(signal_ids) != len(set(signal_ids)):
        raise ValueError("duplicate_observation_signal_id")

    rows: list[dict[str, object]] = []
    provenance_rows: list[dict[str, object]] = []
    first = ordered[0]
    for observation in ordered:
        if compute_observation_hash(observation) != observation.observation_hash:
            raise ValueError("observation_hash_mismatch")
        if (
            observation.schema_version != first.schema_version
            or observation.source_sha != first.source_sha
            or observation.run_id != first.run_id
            or observation.report_type != first.report_type
        ):
            raise ValueError("observation_sidecar_batch_identity_mismatch")

        snapshot = snapshots_by_symbol.get(observation.asset)
        if snapshot is None:
            raise ValueError("observation_snapshot_missing")
        if snapshot.symbol != observation.asset or snapshot.asset_type != observation.asset_type:
            raise ValueError("observation_snapshot_identity_mismatch")

        row = asdict(observation)
        row["reason_codes"] = list(observation.reason_codes)
        row["persisted_at_utc"] = None
        rows.append(row)

        metadata = snapshot.data_fetch_metadata
        claim = getattr(metadata, "price_basis_claim", None) if metadata else None
        qualified = False
        if claim is not None:
            try:
                qualified = is_qualified_raw_ohlcv_claim(claim)
            except (TypeError, ValueError):
                qualified = False
        status: SignalBasisStatus = (
            "verified_raw_ohlcv" if qualified else "signal_basis_unavailable"
        )
        claim_values = None
        if claim is not None:
            claim_values = _claim_values(claim)
        snapshot_binding = _snapshot_binding(
            observation,
            snapshot,
            claim_values,
        )
        if not _snapshot_binding_matches_observation(
            observation,
            snapshot_binding,
            claim_values,
        ):
            status = "signal_basis_unavailable"
        signal_input_hash = _signal_input_hash(observation, snapshot_binding)
        collection_provenance = {
            "provider": metadata.provider if metadata else None,
            "endpoint": metadata.endpoint if metadata else None,
            "fetched_at": metadata.fetched_at if metadata else None,
            "cache_fetched_at": metadata.cache_fetched_at if metadata else None,
            "source_timestamp": metadata.source_timestamp if metadata else None,
            "cache_age_seconds": metadata.cache_age_seconds if metadata else None,
            "source_age_seconds": metadata.source_age_seconds if metadata else None,
            "is_fresh": metadata.is_fresh if metadata else None,
            "cache_hit": metadata.cache_hit if metadata else None,
            "fallback_used": metadata.fallback_used if metadata else None,
            "fallback_from": metadata.fallback_from if metadata else None,
            "fallback_to": metadata.fallback_to if metadata else None,
            "granularity": metadata.granularity if metadata else None,
            "market_data_kind": metadata.market_data_kind if metadata else None,
            "price_basis_claim": claim_values,
        }
        provenance_rows.append(
            {
                "signal_id": observation.signal_id,
                "observation_hash": observation.observation_hash,
                "signal_price_provider": metadata.provider
                if metadata is not None
                else observation.provider,
                "signal_input_hash": signal_input_hash,
                "signal_price_basis_status": status,
                "signal_price_basis_observed": (
                    claim_values["price_basis"] if claim_values else None
                ),
                "collection_provenance": collection_provenance,
                "snapshot_binding": snapshot_binding,
                "entry_integrity_version": _SIDECAR_ENTRY_INTEGRITY_VERSION,
            }
        )

        provenance_rows[-1]["entry_integrity_sha256"] = _entry_integrity_sha256(
            provenance_rows[-1]
        )

    sidecar = {
        "schema_version": first.schema_version,
        "source_sha": first.source_sha,
        "run_id": first.run_id,
        "report_type": first.report_type,
        "observations": rows,
        "provenance": provenance_rows,
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(deterministic_gzip(canonical_json_bytes(sidecar)))
    return path


def _resolve_signal_price_basis_status_legacy(
    *,
    observation: SignalObservation,
    sidecar: Mapping[str, object],
) -> SignalBasisStatus:
    """Resolve basis only from a validated ``(signal_id, observation_hash)`` binding."""
    from dataclasses import asdict

    from advisor.data_sources import is_qualified_raw_ohlcv_claim
    from advisor.models import PriceBasisClaim
    from advisor.signal_observation import compute_observation_hash

    unavailable: SignalBasisStatus = "signal_basis_unavailable"
    try:
        if not isinstance(sidecar, Mapping):
            return unavailable
        if (
            sidecar.get("schema_version") != observation.schema_version
            or sidecar.get("source_sha") != observation.source_sha
            or sidecar.get("run_id") != observation.run_id
            or sidecar.get("report_type") != observation.report_type
        ):
            return unavailable
        if compute_observation_hash(observation) != observation.observation_hash:
            return unavailable

        observation_rows = sidecar.get("observations")
        if not isinstance(observation_rows, list):
            return unavailable
        matching_rows = [
            row
            for row in observation_rows
            if isinstance(row, Mapping)
            and row.get("signal_id") == observation.signal_id
        ]
        if len(matching_rows) != 1:
            return unavailable
        row = matching_rows[0]
        if row.get("observation_hash") != observation.observation_hash:
            return unavailable
        expected_row = asdict(observation)
        expected_row["reason_codes"] = list(observation.reason_codes)
        expected_row["persisted_at_utc"] = None
        if canonical_json_bytes(row) != canonical_json_bytes(expected_row):
            return unavailable

        provenance_rows = sidecar.get("provenance")
        if not isinstance(provenance_rows, list):
            return unavailable
        matching_provenance = [
            candidate
            for candidate in provenance_rows
            if isinstance(candidate, Mapping)
            and candidate.get("signal_id") == observation.signal_id
            and candidate.get("observation_hash") == observation.observation_hash
        ]
        if len(matching_provenance) != 1:
            return unavailable
        provenance = matching_provenance[0]
        collection_provenance = provenance.get("collection_provenance")
        if not isinstance(collection_provenance, Mapping):
            return unavailable
        claim_values = collection_provenance.get("price_basis_claim")
        if claim_values is not None and not isinstance(claim_values, Mapping):
            return unavailable
        normalized_claim_values = (
            dict(claim_values) if isinstance(claim_values, Mapping) else None
        )
        if normalized_claim_values is not None and set(normalized_claim_values) != {
            "price_basis",
            "price_basis_policy_version",
            "source_contract",
        }:
            return unavailable

        claim = PriceBasisClaim(
            price_basis=(
                normalized_claim_values.get("price_basis")
                if normalized_claim_values is not None
                else None
            ),
            price_basis_policy_version=(
                normalized_claim_values.get("price_basis_policy_version")
                if normalized_claim_values is not None
                else None
            ),
            source_contract=(
                normalized_claim_values.get("source_contract")
                if normalized_claim_values is not None
                else None
            ),
        )

        snapshot_binding = provenance.get("snapshot_binding")
        if not isinstance(snapshot_binding, Mapping):
            return unavailable
        expected_snapshot_binding = _expected_snapshot_binding(
            observation,
            normalized_claim_values,
        )
        if canonical_json_bytes(snapshot_binding) != canonical_json_bytes(
            expected_snapshot_binding
        ):
            return unavailable

        signal_input_hash = provenance.get("signal_input_hash")
        _require_sha256_hex(signal_input_hash, field_name="signal_input_hash")
        if signal_input_hash != _signal_input_hash(
            observation,
            expected_snapshot_binding,
        ):
            return unavailable

        if provenance.get("entry_integrity_version") != _SIDECAR_ENTRY_INTEGRITY_VERSION:
            return unavailable
        entry_integrity_sha256 = provenance.get("entry_integrity_sha256")
        _require_sha256_hex(
            entry_integrity_sha256,
            field_name="entry_integrity_sha256",
        )
        if entry_integrity_sha256 != _entry_integrity_sha256(provenance):
            return unavailable

        qualified = is_qualified_raw_ohlcv_claim(claim)
        expected_status: SignalBasisStatus = (
            "verified_raw_ohlcv" if qualified else unavailable
        )
        if provenance.get("signal_price_basis_status") != expected_status:
            return unavailable
        if provenance.get("signal_price_basis_observed") != (
            claim.price_basis if qualified else None
        ):
            return unavailable
        return expected_status
    except (AttributeError, KeyError, TypeError, ValueError):
        return unavailable
