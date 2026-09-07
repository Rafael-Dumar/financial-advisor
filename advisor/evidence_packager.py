from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Mapping
from datetime import date
from pathlib import Path
import re
import shutil
import tempfile

from advisor.evidence_schema import (
    canonical_content_sha256,
    canonical_json_bytes,
    deterministic_gzip,
    payload_sha256,
    strict_json_loads_bytes,
    validate_canonical_envelope,
)


__all__ = ("package_task4_transport",)

_SCHEMA_VERSION = "1.0"
_MARKET_TRANSPORT = "market-transport.json"
_CORPORATE_TRANSPORT = "corporate-actions-transport.json"
_TRANSPORT_NAMES = frozenset({_MARKET_TRANSPORT, _CORPORATE_TRANSPORT})
_ASSET_TYPES = frozenset({"stock", "etf", "crypto"})
_MARKET_PROVIDERS = frozenset({"fmp", "binance", "hyperliquid"})
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9.-]{1,12}$")
_MARKET_BASE_KEYS = frozenset(
    {
        "asset_type",
        "assigned_provider",
        "assignment_policy_version",
        "bars",
        "coverage_window",
        "interval",
        "market_timezone",
        "symbol",
    }
)
_MARKET_AVAILABLE_KEYS = _MARKET_BASE_KEYS | frozenset(
    {"semantic_provenance", "source_request", "status"}
)
_CORPORATE_BASE_KEYS = frozenset(
    {
        "asset_type",
        "coverage_window",
        "corporate_action_provider",
        "function",
        "normalized_events",
        "source_request",
        "status",
        "symbol",
    }
)
_CORPORATE_AVAILABLE_KEYS = _CORPORATE_BASE_KEYS | frozenset(
    {"payload", "semantic_provenance"}
)


class _PackagingError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


def _fail(error_code: str) -> None:
    raise _PackagingError(error_code)


def _mapping(value: object, error_code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(error_code)
    return value


def _string(value: object, error_code: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(error_code)
    return value


def _date_string(value: object, error_code: str) -> str:
    value = _string(value, error_code)
    if _DATE_RE.fullmatch(value) is None:
        _fail(error_code)
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise _PackagingError(error_code) from error
    return value


def _sha256(value: object, error_code: str) -> str:
    value = _string(value, error_code)
    if _SHA256_RE.fullmatch(value) is None:
        _fail(error_code)
    return value


def _symbol(value: object, error_code: str) -> str:
    value = _string(value, error_code)
    if _SYMBOL_RE.fullmatch(value) is None:
        _fail(error_code)
    return value


def _finite_number(value: object, error_code: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(error_code)
    if isinstance(value, float) and not math.isfinite(value):
        _fail(error_code)
    return value


def _transport_files(transport_dir: Path) -> dict[str, Path]:
    if not isinstance(transport_dir, Path):
        raise TypeError("transport_dir must be a Path")
    if transport_dir.is_symlink() or not transport_dir.is_dir():
        _fail("transport_directory_missing")
    try:
        entries = sorted(transport_dir.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise _PackagingError("transport_storage_error") from error
    if not entries:
        _fail("empty_transport")

    files: dict[str, Path] = {}
    for path in entries:
        if path.is_symlink():
            _fail("symlink_path")
        if not path.is_file() or path.name not in _TRANSPORT_NAMES:
            _fail("unsupported_transport")
        files[path.name] = path
    return files


def _transport_records(path: Path) -> list[Mapping[str, object]]:
    try:
        parsed = strict_json_loads_bytes(path.read_bytes())
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        raise _PackagingError("transport_validation_failed") from error
    payload = _mapping(parsed, "transport_validation_failed")
    if set(payload) != {"records"} or not isinstance(payload.get("records"), list):
        _fail("transport_validation_failed")
    records = payload["records"]
    if any(not isinstance(record, Mapping) for record in records):
        _fail("transport_validation_failed")
    return [record for record in records if isinstance(record, Mapping)]


def _coverage_window(value: object, error_code: str) -> tuple[str, str]:
    window = _mapping(value, error_code)
    if set(window) != {"start_date", "end_date"}:
        _fail(error_code)
    start = _date_string(window.get("start_date"), error_code)
    end = _date_string(window.get("end_date"), error_code)
    if start > end:
        _fail(error_code)
    return start, end


def _validate_market_provenance(value: object, provider: str) -> dict[str, object]:
    provenance = dict(_mapping(value, "invalid_market_provenance"))
    required = {
        "collection_policy_version",
        "price_basis_claim",
        "price_provider",
        "source_response_sha256",
    }
    if not required.issubset(provenance) or set(provenance) != required:
        _fail("invalid_market_provenance")
    if provenance.get("price_provider") != provider:
        _fail("invalid_market_provider")
    _string(provenance.get("collection_policy_version"), "invalid_market_provenance")
    _sha256(provenance.get("source_response_sha256"), "invalid_market_provenance")
    claim = _mapping(provenance.get("price_basis_claim"), "invalid_market_provenance")
    claim_keys = {"price_basis", "price_basis_policy_version", "source_contract"}
    if set(claim) != claim_keys:
        _fail("invalid_market_provenance")
    for key in claim_keys:
        _string(claim.get(key), "invalid_market_provenance")
    return provenance


def _market_envelopes(record: Mapping[str, object]) -> list[dict[str, object]]:
    if set(record) - (_MARKET_AVAILABLE_KEYS | {"status"}):
        _fail("unsupported_market_transport")
    if not _MARKET_BASE_KEYS.issubset(record):
        _fail("invalid_market_transport")
    asset_type = record.get("asset_type")
    if asset_type not in _ASSET_TYPES:
        _fail("unsupported_market_transport")
    symbol = _symbol(record.get("symbol"), "invalid_market_transport")
    interval = _string(record.get("interval"), "invalid_market_transport")
    if interval != "1d":
        _fail("unsupported_market_transport")
    expected_timezone = "UTC" if asset_type == "crypto" else "America/New_York"
    market_timezone = _string(record.get("market_timezone"), "invalid_market_transport")
    if market_timezone != expected_timezone:
        _fail("invalid_market_transport")
    assignment_policy = _string(
        record.get("assignment_policy_version"), "invalid_market_transport"
    )
    if assignment_policy != "price_provider_assignment_v1":
        _fail("unsupported_market_transport")
    status = _string(record.get("status"), "invalid_market_transport")
    bars = record.get("bars")
    if not isinstance(bars, list):
        _fail("invalid_market_transport")
    provider = record.get("assigned_provider")
    if status == "market_data_unavailable":
        if set(record) != _MARKET_BASE_KEYS | {"status"}:
            _fail("invalid_market_transport")
        if record.get("coverage_window") is not None:
            _fail("invalid_market_transport")
        return []
    if status != "available" or set(record) != _MARKET_AVAILABLE_KEYS:
        _fail("unsupported_market_transport")
    if provider not in _MARKET_PROVIDERS:
        _fail("invalid_market_provider")
    start_date, end_date = _coverage_window(
        record.get("coverage_window"), "invalid_market_transport"
    )
    provenance = _validate_market_provenance(record.get("semantic_provenance"), provider)
    if not isinstance(record.get("source_request"), Mapping):
        _fail("invalid_market_transport")
    if not bars:
        _fail("invalid_market_transport")

    envelopes: list[dict[str, object]] = []
    seen_dates: set[str] = set()
    for raw_bar in bars:
        bar = _mapping(raw_bar, "invalid_market_bar")
        required = {
            "market_date",
            "ohlcv",
            "price_basis",
            "session_close_type",
            "session_status",
        }
        if set(bar) != required:
            _fail("invalid_market_bar")
        market_date = _date_string(bar.get("market_date"), "invalid_market_bar")
        if market_date in seen_dates:
            _fail("duplicate_market_bar")
        seen_dates.add(market_date)
        if not start_date <= market_date <= end_date:
            _fail("invalid_market_transport")
        ohlcv = dict(_mapping(bar.get("ohlcv"), "invalid_market_bar"))
        if set(ohlcv) != {"close", "high", "low", "open", "volume"}:
            _fail("invalid_market_bar")
        values = {
            key: _finite_number(ohlcv.get(key), "invalid_market_bar")
            for key in ("open", "high", "low", "close", "volume")
        }
        if any(values[key] <= 0 for key in ("open", "high", "low", "close")):
            _fail("invalid_market_bar")
        if values["volume"] < 0:
            _fail("invalid_market_bar")
        if (
            values["low"] > values["open"]
            or values["open"] > values["high"]
            or values["low"] > values["close"]
            or values["close"] > values["high"]
        ):
            _fail("invalid_market_bar")
        if bar.get("price_basis") != "raw_ohlcv":
            _fail("unsupported_market_transport")
        if bar.get("session_status") != "complete":
            _fail("invalid_market_bar")
        if bar.get("session_close_type") not in {"regular", "early"}:
            _fail("invalid_market_bar")

        logical_identity = {
            "asset_type": asset_type,
            "interval": interval,
            "market_date": market_date,
            "market_timezone": market_timezone,
            "schema_version": _SCHEMA_VERSION,
            "symbol": symbol,
        }
        payload = {
            "asset_type": asset_type,
            "interval": interval,
            "market_date": market_date,
            "market_timezone": market_timezone,
            "ohlcv": values,
            "price_basis": bar["price_basis"],
            "session_close_type": bar["session_close_type"],
            "session_status": bar["session_status"],
        }
        envelopes.append(_canonical_envelope("market_bar", logical_identity, payload, provenance))
    return envelopes


def _validate_events(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        _fail("invalid_corporate_action")
    events: list[dict[str, object]] = []
    previous_date: str | None = None
    for raw_event in value:
        event = _mapping(raw_event, "invalid_corporate_action")
        if set(event) != {"effective_date", "split_factor_raw", "split_ratio"}:
            _fail("invalid_corporate_action")
        effective_date = _date_string(event.get("effective_date"), "invalid_corporate_action")
        if previous_date is not None and effective_date <= previous_date:
            _fail("invalid_corporate_action")
        previous_date = effective_date
        _string(event.get("split_factor_raw"), "invalid_corporate_action")
        ratio = _mapping(event.get("split_ratio"), "invalid_corporate_action")
        if set(ratio) != {"new_shares", "old_shares"}:
            _fail("invalid_corporate_action")
        new_shares = _string(ratio.get("new_shares"), "invalid_corporate_action")
        old_shares = _string(ratio.get("old_shares"), "invalid_corporate_action")
        if not new_shares.isdigit() or not old_shares.isdigit() or int(new_shares) <= 0 or int(old_shares) <= 0:
            _fail("invalid_corporate_action")
        events.append(
            {
                "effective_date": effective_date,
                "split_factor_raw": event["split_factor_raw"],
                "split_ratio": {
                    "new_shares": new_shares,
                    "old_shares": old_shares,
                },
            }
        )
    return events


def _validate_corporate_provenance(value: object, provider: str) -> dict[str, object]:
    provenance = dict(_mapping(value, "invalid_corporate_action"))
    required = {"corporate_action_provider", "source_response_sha256"}
    if set(provenance) != required or provenance.get("corporate_action_provider") != provider:
        _fail("invalid_corporate_action")
    _sha256(provenance.get("source_response_sha256"), "invalid_corporate_action")
    return provenance


def _corporate_envelopes(record: Mapping[str, object]) -> list[dict[str, object]]:
    if set(record) - (_CORPORATE_AVAILABLE_KEYS | {"status"}):
        _fail("unsupported_corporate_transport")
    if not _CORPORATE_BASE_KEYS.issubset(record):
        _fail("invalid_corporate_action")
    asset_type = record.get("asset_type")
    if asset_type not in {"stock", "etf"}:
        _fail("unsupported_corporate_transport")
    symbol = _symbol(record.get("symbol"), "invalid_corporate_action")
    provider = record.get("corporate_action_provider")
    if provider != "alpha_vantage":
        _fail("unsupported_corporate_transport")
    if record.get("function") != "SPLITS":
        _fail("unsupported_corporate_transport")
    start_date, end_date = _coverage_window(
        record.get("coverage_window"), "invalid_corporate_action"
    )
    events = _validate_events(record.get("normalized_events"))
    if not isinstance(record.get("source_request"), Mapping):
        _fail("invalid_corporate_action")
    status = _string(record.get("status"), "invalid_corporate_action")
    if status == "feed_unavailable":
        if set(record) != _CORPORATE_BASE_KEYS:
            _fail("invalid_corporate_action")
        return []
    if status != "available" or set(record) != _CORPORATE_AVAILABLE_KEYS:
        _fail("unsupported_corporate_transport")
    payload = dict(_mapping(record.get("payload"), "invalid_corporate_action"))
    if set(payload) != {"data", "normalized_events", "symbol"}:
        _fail("invalid_corporate_action")
    if not isinstance(payload.get("data"), list) or payload.get("symbol") != symbol:
        _fail("invalid_corporate_action")
    payload_events = _validate_events(payload.get("normalized_events"))
    if canonical_json_bytes(payload_events) != canonical_json_bytes(events):
        _fail("invalid_corporate_action")
    provenance = _validate_corporate_provenance(
        record.get("semantic_provenance"), provider
    )
    logical_identity = {
        "asset_type": asset_type,
        "corporate_action_provider": provider,
        "coverage_end_date": end_date,
        "coverage_start_date": start_date,
        "function": "SPLITS",
        "schema_version": _SCHEMA_VERSION,
        "symbol": symbol,
    }
    canonical_payload = {
        "data": payload["data"],
        "normalized_events": payload_events,
        "symbol": symbol,
    }
    return [
        _canonical_envelope(
            "corporate_action",
            logical_identity,
            canonical_payload,
            provenance,
        )
    ]


def _canonical_envelope(
    evidence_type: str,
    logical_identity: Mapping[str, object],
    payload: Mapping[str, object],
    semantic_provenance: Mapping[str, object],
) -> dict[str, object]:
    envelope = {
        "canonical_content_sha256": canonical_content_sha256(
            evidence_type=evidence_type,
            schema_version=_SCHEMA_VERSION,
            logical_identity=logical_identity,
            payload=payload,
            semantic_provenance=semantic_provenance,
        ),
        "evidence_type": evidence_type,
        "logical_identity": dict(logical_identity),
        "payload": dict(payload),
        "payload_sha256": payload_sha256(payload),
        "schema_version": _SCHEMA_VERSION,
        "semantic_provenance": dict(semantic_provenance),
    }
    try:
        validate_canonical_envelope(envelope)
        canonical_json_bytes(envelope)
    except (TypeError, ValueError) as error:
        raise _PackagingError("invalid_canonical_shard") from error
    return envelope


def _candidate_path(envelope: Mapping[str, object]) -> Path:
    evidence_type = envelope.get("evidence_type")
    identity = _mapping(envelope.get("logical_identity"), "invalid_canonical_shard")
    if evidence_type == "market_bar":
        directory = "market-bars"
        partition = _date_string(identity.get("market_date"), "invalid_canonical_shard")
    elif evidence_type == "corporate_action":
        directory = "corporate-actions"
        partition = _date_string(identity.get("coverage_end_date"), "invalid_canonical_shard")
    else:
        _fail("unsupported_evidence_type")
    identity_sha = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    return Path(
        "evidence",
        directory,
        partition[:4],
        partition[5:7],
        partition[8:10],
        f"{identity_sha}.json.gz",
    )


def _publish_candidates(output_dir: Path, candidates: Mapping[Path, bytes]) -> tuple[Path, ...]:
    if not isinstance(output_dir, Path):
        raise TypeError("output_dir must be a Path")
    if output_dir.is_symlink():
        _fail("symlink_path")
    parent = output_dir.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _PackagingError("packaging_storage_error") from error

    if output_dir.exists():
        if not output_dir.is_dir():
            _fail("output_dir_not_directory")
        try:
            if any(output_dir.iterdir()):
                _fail("output_dir_not_empty")
        except OSError as error:
            raise _PackagingError("packaging_storage_error") from error

    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=".evidence-packager-", dir=str(parent)))
        for relative_path, compressed in sorted(
            candidates.items(), key=lambda item: item[0].as_posix()
        ):
            target = staging.joinpath(*relative_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(compressed)
        if output_dir.exists():
            output_dir.rmdir()
        os.replace(staging, output_dir)
        staging = None
    except _PackagingError:
        raise
    except OSError as error:
        raise _PackagingError("packaging_storage_error") from error
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    return tuple(sorted(candidates, key=lambda path: path.as_posix()))


def package_task4_transport(
    *,
    transport_dir: Path,
    output_dir: Path,
) -> tuple[Path, ...]:
    """Package Task 4 transport into deterministic candidate canonical shards."""
    files = _transport_files(transport_dir)
    candidates: dict[Path, bytes] = {}
    for name in sorted(files):
        records = _transport_records(files[name])
        envelope_factory = (
            _market_envelopes if name == _MARKET_TRANSPORT else _corporate_envelopes
        )
        for record in records:
            for envelope in envelope_factory(record):
                relative_path = _candidate_path(envelope)
                compressed = deterministic_gzip(canonical_json_bytes(envelope))
                if relative_path in candidates:
                    _fail("duplicate_candidate_shard")
                candidates[relative_path] = compressed
    return _publish_candidates(output_dir, candidates)
