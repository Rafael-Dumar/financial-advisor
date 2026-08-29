from __future__ import annotations

import hashlib
import json
import math
import struct
import zlib
from collections.abc import Mapping
from typing import Literal


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
