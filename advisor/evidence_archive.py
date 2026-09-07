from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from .evidence_schema import (
    ArchiveStatus,
    canonical_content_sha256,
    canonical_json_bytes,
    classify_idempotency,
    decompress_single_member_gzip,
    deterministic_gzip,
    strict_json_loads_bytes,
    validate_canonical_envelope,
)


_BRANCH_SCHEMA = {
    "branch_name": "advisor-evidence",
    "financial_evidence_count": 0,
    "schema_version": "1.0",
}
_SCHEMA_VERSION = "1.0"
_MAX_COMPRESSED_BYTES = 25 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_MAX_BATCH_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

_TYPE_TO_DIRECTORY = {
    "observation": "observations",
    "market_bar": "market-bars",
    "corporate_action": "corporate-actions",
    "horizon_proof": "horizon-proofs",
    "outcome": "outcomes",
}
_DIRECTORY_TO_TYPE = {value: key for key, value in _TYPE_TO_DIRECTORY.items()}
_PARTITION_FIELDS = {
    "observation": ("payload", "report_date_brt"),
    "market_bar": ("logical_identity", "market_date"),
    "corporate_action": ("logical_identity", "coverage_end_date"),
    "horizon_proof": ("payload", "horizon_end_date"),
    "outcome": ("payload", "horizon_end_date"),
}
_CONFLICT_REASON_CODES = frozenset(
    {
        "divergent_payload",
        "hash_mismatch",
        "provider_mixing",
        "identity_collision",
        "corporate_action_revision_conflict",
    }
)
_RETRYABLE_PUSH_ERRORS = frozenset(
    {"push_failed", "push_unconfirmed", "push_already_up_to_date"}
)


class _ArchiveError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class ArchiveResult:
    status: ArchiveStatus
    batch_identity: str
    manifest_path: str | None
    committed_paths: tuple[str, ...]
    conflict_paths: tuple[str, ...]
    error_code: str | None
    durability_confirmed: bool


@dataclass(frozen=True)
class _Shard:
    envelope: dict[str, object]
    canonical_bytes: bytes
    compressed_bytes: bytes
    evidence_type: str
    partition_date: str
    logical_identity_sha256: str
    canonical_content_sha256: str
    payload_sha256: str
    path: str


@dataclass(frozen=True)
class _Conflict:
    identity: dict[str, object]
    conflict_identity_sha256: str
    content: dict[str, object]
    path: str
    partition_date: str


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _require_sha256(value: object, *, field_name: str) -> str:
    if not _is_sha256(value):
        raise _ArchiveError(f"invalid_{field_name}")
    return value  # type: ignore[return-value]


def _require_string(value: object, *, error_code: str) -> str:
    if not isinstance(value, str) or not value:
        raise _ArchiveError(error_code)
    return value


def _safe_branch_name(branch_name: str) -> str:
    if (
        not isinstance(branch_name, str)
        or not branch_name
        or branch_name.startswith("-")
        or ".." in branch_name
        or not _BRANCH_RE.fullmatch(branch_name)
    ):
        raise _ArchiveError("invalid_branch_name")
    return branch_name


def _run_git(
    cwd: Path,
    *args: str,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise _ArchiveError("git_error")
    return completed


def _git_text(cwd: Path, *args: str, check: bool = True) -> str:
    completed = _run_git(cwd, *args, check=check)
    try:
        return completed.stdout.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise _ArchiveError("git_output_invalid") from exc


def _origin_url(repo_dir: Path) -> str:
    result = _run_git(repo_dir, "config", "--get", "remote.origin.url", check=False)
    if result.returncode != 0:
        raise _ArchiveError("origin_missing")
    try:
        origin = result.stdout.decode("utf-8", "strict").strip()
    except UnicodeDecodeError as exc:
        raise _ArchiveError("origin_invalid") from exc
    if not origin:
        raise _ArchiveError("origin_missing")
    return origin


def _remote_branch_exists(repo_dir: Path, origin: str, branch_name: str) -> bool:
    ref_name = f"refs/heads/{branch_name}"
    result = _run_git(
        repo_dir,
        "ls-remote",
        origin,
        ref_name,
        check=False,
    )
    if result.returncode != 0:
        raise _ArchiveError("remote_query_error")
    try:
        lines = result.stdout.decode("utf-8", "strict").splitlines()
    except UnicodeDecodeError as exc:
        raise _ArchiveError("remote_query_error") from exc
    if not lines:
        return False
    for line in lines:
        fields = line.split()
        if len(fields) != 2 or fields[1] != ref_name:
            raise _ArchiveError("remote_query_error")
    return True


def _assert_no_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise _ArchiveError("symlink_path")
    if not root.exists() or not root.is_dir():
        raise _ArchiveError("directory_missing")
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise _ArchiveError("storage_error") from exc
        for entry in entries:
            if entry.is_symlink():
                raise _ArchiveError("symlink_path")
            entry_path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                pending.append(entry_path)


def _safe_relative_path(path: Path) -> str:
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _ArchiveError("unsafe_path")
    if any("/" in part or "\\" in part for part in path.parts):
        raise _ArchiveError("unsafe_path")
    return path.as_posix()


def _validate_date(value: object, *, error_code: str = "invalid_partition_date") -> str:
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        raise _ArchiveError(error_code)
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise _ArchiveError(error_code) from exc
    return value


def _sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _identity_sha256(logical_identity: Mapping[str, object]) -> str:
    return _sha256_json(logical_identity)


def _partition_date(envelope: Mapping[str, object]) -> str:
    evidence_type = envelope.get("evidence_type")
    if evidence_type not in _PARTITION_FIELDS:
        raise _ArchiveError("unsupported_evidence_type")
    source_key, field_name = _PARTITION_FIELDS[evidence_type]
    source = envelope.get(source_key)
    if not isinstance(source, Mapping):
        raise _ArchiveError("invalid_partition_source")
    return _validate_date(source.get(field_name))


def _canonical_path(
    *,
    evidence_type: str,
    partition_date: str,
    logical_identity_sha256: str,
) -> str:
    directory = _TYPE_TO_DIRECTORY.get(evidence_type)
    if directory is None:
        raise _ArchiveError("unsupported_evidence_type")
    _validate_date(partition_date)
    _require_sha256(logical_identity_sha256, field_name="logical_identity_sha256")
    return (
        f"evidence/{directory}/{partition_date[:4]}/{partition_date[5:7]}/"
        f"{partition_date[8:10]}/{logical_identity_sha256}.json.gz"
    )


def _write_under(root: Path, relative_path: str, data: bytes) -> None:
    relative = Path(relative_path)
    _safe_relative_path(relative)
    target = root.joinpath(*relative.parts)
    root_resolved = root.resolve()
    target_parent = target.parent
    target_parent.mkdir(parents=True, exist_ok=True)
    for parent in [target_parent, *target_parent.parents]:
        if parent == root.parent:
            break
        if parent == root_resolved:
            break
        if parent.is_symlink():
            raise _ArchiveError("symlink_path")
    try:
        target.resolve().relative_to(root_resolved)
    except ValueError as exc:
        raise _ArchiveError("unsafe_path") from exc
    if target.exists() and target.is_symlink():
        raise _ArchiveError("symlink_path")
    target.write_bytes(data)


def _envelope_without_transport(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _ArchiveError("invalid_envelope")
    envelope = dict(value)
    envelope.pop("transport", None)
    try:
        validate_canonical_envelope(envelope)
    except (TypeError, ValueError) as exc:
        raise _ArchiveError("invalid_envelope") from exc
    return envelope


def _read_shard_bytes(
    *,
    root: Path,
    relative_path: str,
    require_canonical_path: bool = True,
) -> _Shard:
    relative = Path(relative_path)
    _safe_relative_path(relative)
    if relative.suffixes != [".json", ".gz"]:
        raise _ArchiveError("unexpected_extension")
    if not relative.parts or relative.parts[0] != "evidence":
        raise _ArchiveError("unsafe_path")
    if len(relative.parts) != 6:
        raise _ArchiveError("invalid_shard_path")
    directory = relative.parts[1]
    evidence_type = _DIRECTORY_TO_TYPE.get(directory)
    if evidence_type is None:
        raise _ArchiveError("invalid_shard_path")
    partition = _validate_date("-".join(relative.parts[2:5]))
    filename = relative.parts[5]
    if not _SHA256_RE.fullmatch(filename[:-8]) or not filename.endswith(".json.gz"):
        raise _ArchiveError("invalid_shard_path")
    expected_identity_sha = filename[:-8]
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise _ArchiveError("symlink_path" if path.is_symlink() else "storage_error")
    try:
        compressed = path.read_bytes()
    except OSError as exc:
        raise _ArchiveError("storage_error") from exc
    if len(compressed) > _MAX_COMPRESSED_BYTES:
        raise _ArchiveError("compressed_size_exceeded")
    try:
        raw = decompress_single_member_gzip(
            compressed,
            max_uncompressed_bytes=_MAX_UNCOMPRESSED_BYTES,
        )
        parsed = strict_json_loads_bytes(raw)
        validate_canonical_envelope(parsed)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise _ArchiveError("invalid_canonical_shard") from exc
    if not isinstance(parsed, Mapping):
        raise _ArchiveError("invalid_canonical_shard")
    envelope = dict(parsed)
    actual_type = envelope.get("evidence_type")
    if actual_type != evidence_type:
        raise _ArchiveError("shard_type_path_mismatch")
    if envelope.get("schema_version") != _SCHEMA_VERSION:
        raise _ArchiveError("unsupported_schema_version")
    logical_identity = envelope.get("logical_identity")
    if not isinstance(logical_identity, Mapping):
        raise _ArchiveError("invalid_logical_identity")
    actual_identity_sha = _identity_sha256(logical_identity)
    if actual_identity_sha != expected_identity_sha:
        raise _ArchiveError("shard_identity_path_mismatch")
    partition_date = _partition_date(envelope)
    if require_canonical_path:
        expected_path = _canonical_path(
            evidence_type=evidence_type,
            partition_date=partition_date,
            logical_identity_sha256=actual_identity_sha,
        )
        if expected_path != relative.as_posix():
            raise _ArchiveError("shard_partition_path_mismatch")
    try:
        canonical_bytes = canonical_json_bytes(envelope)
    except (TypeError, ValueError) as exc:
        raise _ArchiveError("invalid_canonical_shard") from exc
    if "transport" in envelope or canonical_bytes != raw:
        raise _ArchiveError("noncanonical_shard_bytes")
    return _Shard(
        envelope=envelope,
        canonical_bytes=canonical_bytes,
        compressed_bytes=compressed,
        evidence_type=evidence_type,
        partition_date=partition_date,
        logical_identity_sha256=actual_identity_sha,
        canonical_content_sha256=_require_sha256(
            envelope.get("canonical_content_sha256"),
            field_name="canonical_content_sha256",
        ),
        payload_sha256=_require_sha256(
            envelope.get("payload_sha256"),
            field_name="payload_sha256",
        ),
        path=relative.as_posix(),
    )


def _read_transport(transport_dir: Path) -> list[_Shard]:
    if not isinstance(transport_dir, Path):
        raise _ArchiveError("transport_dir_must_be_path")
    _assert_no_symlinks(transport_dir)
    files: list[Path] = []
    for current, directories, names in os.walk(transport_dir, followlinks=False):
        for directory in directories:
            if (Path(current) / directory).is_symlink():
                raise _ArchiveError("symlink_path")
        for name in names:
            path = Path(current) / name
            if path.is_symlink():
                raise _ArchiveError("symlink_path")
            relative = path.relative_to(transport_dir)
            _safe_relative_path(relative)
            if relative.suffixes != [".json", ".gz"]:
                raise _ArchiveError("unexpected_extension")
            files.append(path)
    if not files:
        raise _ArchiveError("empty_batch")

    shards: list[_Shard] = []
    total_uncompressed = 0
    for path in sorted(files, key=lambda item: item.relative_to(transport_dir).as_posix()):
        try:
            compressed = path.read_bytes()
        except OSError as exc:
            raise _ArchiveError("storage_error") from exc
        if len(compressed) > _MAX_COMPRESSED_BYTES:
            raise _ArchiveError("compressed_size_exceeded")
        try:
            raw = decompress_single_member_gzip(
                compressed,
                max_uncompressed_bytes=_MAX_UNCOMPRESSED_BYTES,
            )
            parsed = strict_json_loads_bytes(raw)
            validate_canonical_envelope(parsed)  # type: ignore[arg-type]
            envelope = _envelope_without_transport(parsed)
        except (TypeError, ValueError) as exc:
            raise _ArchiveError("invalid_transport_shard") from exc
        total_uncompressed += len(raw)
        if total_uncompressed > _MAX_BATCH_UNCOMPRESSED_BYTES:
            raise _ArchiveError("batch_size_exceeded")
        if not isinstance(envelope, Mapping):
            raise _ArchiveError("invalid_transport_shard")
        evidence_type = envelope.get("evidence_type")
        if evidence_type not in _TYPE_TO_DIRECTORY:
            raise _ArchiveError("unsupported_evidence_type")
        if envelope.get("schema_version") != _SCHEMA_VERSION:
            raise _ArchiveError("unsupported_schema_version")
        logical_identity = envelope.get("logical_identity")
        if not isinstance(logical_identity, Mapping):
            raise _ArchiveError("invalid_logical_identity")
        identity_sha = _identity_sha256(logical_identity)
        partition_date = _partition_date(envelope)
        canonical_path = _canonical_path(
            evidence_type=evidence_type,
            partition_date=partition_date,
            logical_identity_sha256=identity_sha,
        )
        canonical_bytes = canonical_json_bytes(envelope)
        canonical_compressed = deterministic_gzip(canonical_bytes)
        shards.append(
            _Shard(
                envelope=dict(envelope),
                canonical_bytes=canonical_bytes,
                compressed_bytes=canonical_compressed,
                evidence_type=evidence_type,
                partition_date=partition_date,
                logical_identity_sha256=identity_sha,
                canonical_content_sha256=_require_sha256(
                    envelope.get("canonical_content_sha256"),
                    field_name="canonical_content_sha256",
                ),
                payload_sha256=_require_sha256(
                    envelope.get("payload_sha256"),
                    field_name="payload_sha256",
                ),
                path=canonical_path,
            )
        )
    _validate_provider_consistency(shards)
    return shards


def _batch_identity(shards: Sequence[_Shard]) -> str:
    entries = [
        {
            "canonical_content_sha256": shard.canonical_content_sha256,
            "evidence_type": shard.evidence_type,
            "logical_identity_sha256": shard.logical_identity_sha256,
        }
        for shard in shards
    ]
    entries.sort(
        key=lambda entry: (
            entry["evidence_type"],
            entry["logical_identity_sha256"],
            entry["canonical_content_sha256"],
        )
    )
    identity = {
        "entries": entries,
        "operation": "archive",
        "schema_version": _SCHEMA_VERSION,
    }
    return _sha256_json(identity)


def _manifest_path(batch_identity: str, partition_date: str) -> str:
    return (
        f"evidence/manifests/{partition_date[:4]}/{partition_date[5:7]}/"
        f"{partition_date[8:10]}/{batch_identity}.json.gz"
    )


def _manifest_for_batch(
    *,
    shards: Sequence[_Shard],
    batch_identity: str,
    status: str = "committed",
    operation: str = "archive",
) -> tuple[str, dict[str, object], bytes]:
    if not shards:
        raise _ArchiveError("empty_batch")
    partition_date = min(shard.partition_date for shard in shards)
    entries: list[dict[str, object]] = []
    counts: dict[str, int] = defaultdict(int)
    for shard in sorted(
        shards,
        key=lambda item: (
            item.evidence_type,
            item.logical_identity_sha256,
            item.canonical_content_sha256,
        ),
    ):
        counts[shard.evidence_type] += 1
        entries.append(
            {
                "canonical_content_sha256": shard.canonical_content_sha256,
                "compressed_bytes_sha256": hashlib.sha256(
                    shard.compressed_bytes
                ).hexdigest(),
                "compressed_bytes": len(shard.compressed_bytes),
                "evidence_type": shard.evidence_type,
                "logical_identity_sha256": shard.logical_identity_sha256,
                "path": shard.path,
                "partition_date": shard.partition_date,
                "payload_sha256": shard.payload_sha256,
            }
        )
    manifest: dict[str, object] = {
        "batch_identity": batch_identity,
        "batch_sha256": batch_identity,
        "counts": {
            "by_evidence_type": dict(sorted(counts.items())),
            "by_status": {status: len(entries)},
        },
        "entries": entries,
        "logical_identities": [
            entry["logical_identity_sha256"] for entry in entries
        ],
        "operation": operation,
        "schema_version": _SCHEMA_VERSION,
        "shards": [entry["path"] for entry in entries],
        "status": status,
    }
    path = _manifest_path(batch_identity, partition_date)
    raw = canonical_json_bytes(manifest)
    return path, manifest, deterministic_gzip(raw)


def _validate_manifest_object(
    manifest: object,
    *,
    expected_path: str | None = None,
) -> dict[str, object]:
    if not isinstance(manifest, Mapping):
        raise _ArchiveError("invalid_manifest")
    required = {
        "batch_identity",
        "batch_sha256",
        "counts",
        "entries",
        "logical_identities",
        "operation",
        "schema_version",
        "shards",
        "status",
    }
    if set(manifest) != required:
        raise _ArchiveError("invalid_manifest")
    if manifest["schema_version"] != _SCHEMA_VERSION:
        raise _ArchiveError("invalid_manifest")
    operation = manifest["operation"]
    status = manifest["status"]
    if operation not in {"archive", "conflict_archive"}:
        raise _ArchiveError("invalid_manifest")
    if status not in {"committed", "conflict"}:
        raise _ArchiveError("invalid_manifest")
    if (operation == "archive" and status != "committed") or (
        operation == "conflict_archive" and status != "conflict"
    ):
        raise _ArchiveError("invalid_manifest")
    batch_identity = _require_sha256(
        manifest["batch_identity"], field_name="batch_identity"
    )
    if manifest["batch_sha256"] != batch_identity:
        raise _ArchiveError("invalid_manifest")
    entries = manifest["entries"]
    shards = manifest["shards"]
    logical_identities = manifest["logical_identities"]
    if not isinstance(entries, list) or not isinstance(shards, list):
        raise _ArchiveError("invalid_manifest")
    if (
        not isinstance(logical_identities, list)
        or not entries
        or len(entries) != len(shards)
    ):
        raise _ArchiveError("invalid_manifest")
    normalized_entries: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise _ArchiveError("invalid_manifest")
        expected_keys = {
            "canonical_content_sha256",
            "compressed_bytes_sha256",
            "compressed_bytes",
            "evidence_type",
            "logical_identity_sha256",
            "path",
            "partition_date",
            "payload_sha256",
        }
        if set(entry) != expected_keys:
            raise _ArchiveError("invalid_manifest")
        normalized = dict(entry)
        _require_sha256(
            normalized["canonical_content_sha256"],
            field_name="canonical_content_sha256",
        )
        _require_sha256(
            normalized["compressed_bytes_sha256"],
            field_name="compressed_bytes_sha256",
        )
        _require_sha256(
            normalized["logical_identity_sha256"],
            field_name="logical_identity_sha256",
        )
        _require_sha256(normalized["payload_sha256"], field_name="payload_sha256")
        is_conflict_entry = operation == "conflict_archive"
        if (
            not isinstance(normalized["evidence_type"], str)
            or (
                normalized["evidence_type"] != "conflict"
                if is_conflict_entry
                else normalized["evidence_type"] not in _TYPE_TO_DIRECTORY
            )
            or not isinstance(normalized["path"], str)
            or not isinstance(normalized["compressed_bytes"], int)
            or isinstance(normalized["compressed_bytes"], bool)
        ):
            raise _ArchiveError("invalid_manifest")
        _validate_date(normalized["partition_date"])
        if is_conflict_entry:
            conflict_path = Path(normalized["path"])
            _safe_relative_path(conflict_path)
            if (
                len(conflict_path.parts) != 6
                or conflict_path.parts[:2] != ("evidence", "conflicts")
                or conflict_path.parts[2:5]
                != tuple(normalized["partition_date"].split("-"))
                or not conflict_path.name.endswith(".json.gz")
                or conflict_path.name[:-8] != normalized["logical_identity_sha256"]
            ):
                raise _ArchiveError("invalid_manifest")
        else:
            expected_shard_path = _canonical_path(
                evidence_type=normalized["evidence_type"],
                partition_date=normalized["partition_date"],
                logical_identity_sha256=normalized["logical_identity_sha256"],
            )
            if normalized["path"] != expected_shard_path:
                raise _ArchiveError("invalid_manifest")
        normalized_entries.append(normalized)
    sorted_entries = sorted(
        normalized_entries,
        key=lambda entry: (
            entry["evidence_type"],
            entry["logical_identity_sha256"],
            entry["canonical_content_sha256"],
        ),
    )
    if normalized_entries != sorted_entries:
        raise _ArchiveError("invalid_manifest")
    if shards != [entry["path"] for entry in normalized_entries]:
        raise _ArchiveError("invalid_manifest")
    if logical_identities != [entry["logical_identity_sha256"] for entry in normalized_entries]:
        raise _ArchiveError("invalid_manifest")
    counts = manifest["counts"]
    if not isinstance(counts, Mapping) or set(counts) != {
        "by_evidence_type",
        "by_status",
    }:
        raise _ArchiveError("invalid_manifest")
    by_evidence_type = counts["by_evidence_type"]
    by_status = counts["by_status"]
    if not isinstance(by_evidence_type, Mapping) or not isinstance(by_status, Mapping):
        raise _ArchiveError("invalid_manifest")
    expected_counts: dict[str, int] = defaultdict(int)
    for entry in normalized_entries:
        count = by_evidence_type.get(entry["evidence_type"])
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise _ArchiveError("invalid_manifest")
        expected_counts[entry["evidence_type"]] += 1
    for count in by_status.values():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise _ArchiveError("invalid_manifest")
    if dict(by_evidence_type) != dict(sorted(expected_counts.items())):
        raise _ArchiveError("invalid_manifest")
    expected_status = {status: len(normalized_entries)}
    if dict(by_status) != expected_status:
        raise _ArchiveError("invalid_manifest")
    if operation == "archive":
        identity_entries = [
            {
                "canonical_content_sha256": entry["canonical_content_sha256"],
                "evidence_type": entry["evidence_type"],
                "logical_identity_sha256": entry["logical_identity_sha256"],
            }
            for entry in normalized_entries
        ]
        identity_object = {
            "entries": identity_entries,
            "operation": "archive",
            "schema_version": _SCHEMA_VERSION,
        }
        if _sha256_json(identity_object) != batch_identity:
            raise _ArchiveError("invalid_manifest")
    if expected_path is not None:
        if not expected_path.startswith("evidence/manifests/"):
            raise _ArchiveError("invalid_manifest")
        if Path(expected_path).name != f"{batch_identity}.json.gz":
            raise _ArchiveError("invalid_manifest")
        expected_partition = min(
            entry["partition_date"] for entry in normalized_entries
        )
        manifest_path = Path(expected_path)
        if (
            len(manifest_path.parts) != 6
            or manifest_path.parts[:2] != ("evidence", "manifests")
            or manifest_path.parts[2:5]
            != tuple(expected_partition.split("-"))
        ):
            raise _ArchiveError("invalid_manifest")
    return dict(manifest)


def _read_manifest(root: Path, relative_path: str) -> dict[str, object]:
    relative = Path(relative_path)
    _safe_relative_path(relative)
    if relative.parts[:2] != ("evidence", "manifests"):
        raise _ArchiveError("invalid_manifest_path")
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise _ArchiveError("invalid_manifest_path")
    try:
        compressed = path.read_bytes()
        if len(compressed) > _MAX_COMPRESSED_BYTES:
            raise _ArchiveError("compressed_size_exceeded")
        raw = decompress_single_member_gzip(
            compressed,
            max_uncompressed_bytes=_MAX_UNCOMPRESSED_BYTES,
        )
        parsed = strict_json_loads_bytes(raw)
        if canonical_json_bytes(parsed) != raw:
            raise _ArchiveError("invalid_manifest")
    except (OSError, TypeError, ValueError) as exc:
        raise _ArchiveError("invalid_manifest") from exc
    return _validate_manifest_object(parsed, expected_path=relative.as_posix())


def _load_authority(
    root: Path,
    *,
    branch_name: str = "advisor-evidence",
) -> tuple[dict[tuple[str, str], _Shard], dict[str, dict[str, object]]]:
    evidence_root = root / "evidence"
    _assert_no_symlinks(root)
    top_level_names = {entry.name for entry in root.iterdir() if entry.name != ".git"}
    if top_level_names != {"evidence"}:
        raise _ArchiveError("invalid_evidence_root")
    if not evidence_root.is_dir():
        raise _ArchiveError("invalid_evidence_root")
    schema_path = evidence_root / "branch-schema.json"
    if not schema_path.is_file() or schema_path.is_symlink():
        raise _ArchiveError("invalid_branch_schema")
    try:
        schema = strict_json_loads_bytes(schema_path.read_bytes())
    except (OSError, TypeError, ValueError) as exc:
        raise _ArchiveError("invalid_branch_schema") from exc
    expected_schema = dict(_BRANCH_SCHEMA)
    expected_schema["branch_name"] = branch_name
    if schema != expected_schema:
        raise _ArchiveError("invalid_branch_schema")

    shards: dict[tuple[str, str], _Shard] = {}
    manifests: dict[str, dict[str, object]] = {}
    allowed_roots = set(_DIRECTORY_TO_TYPE) | {"manifests", "conflicts"}
    for path in sorted(evidence_root.rglob("*")):
        if path.is_symlink():
            raise _ArchiveError("symlink_path")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.as_posix() == "evidence/branch-schema.json":
            continue
        if len(relative.parts) < 2 or relative.parts[0] != "evidence":
            raise _ArchiveError("unsafe_path")
        if relative.parts[1] not in allowed_roots:
            raise _ArchiveError("invalid_evidence_path")
        if relative.parts[1] in _DIRECTORY_TO_TYPE:
            shard = _read_shard_bytes(root=root, relative_path=relative.as_posix())
            key = (shard.evidence_type, shard.logical_identity_sha256)
            if key in shards:
                raise _ArchiveError("duplicate_canonical_shard")
            shards[key] = shard
        elif relative.parts[1] == "manifests":
            manifest = _read_manifest(root, relative.as_posix())
            manifest_id = manifest["batch_identity"]
            if manifest_id in manifests:
                raise _ArchiveError("duplicate_manifest")
            manifests[manifest_id] = manifest
        elif relative.parts[1] == "conflicts":
            _read_conflict(root, relative.as_posix())
    for manifest in manifests.values():
        for entry in manifest["entries"]:  # type: ignore[index]
            if manifest["operation"] == "archive":
                if entry["path"] not in {
                    shard.path for shard in shards.values()
                }:
                    raise _ArchiveError("manifest_shard_missing")
                shard = next(
                    shard for shard in shards.values() if shard.path == entry["path"]
                )
                if (
                    shard.canonical_content_sha256 != entry["canonical_content_sha256"]
                    or shard.payload_sha256 != entry["payload_sha256"]
                    or len(shard.compressed_bytes) != entry["compressed_bytes"]
                    or hashlib.sha256(shard.compressed_bytes).hexdigest()
                    != entry["compressed_bytes_sha256"]
                ):
                    raise _ArchiveError("manifest_shard_mismatch")
            else:
                conflict = _read_conflict(root, entry["path"])
                conflict_path = root.joinpath(*Path(entry["path"]).parts)
                compressed_bytes = conflict_path.read_bytes()
                if (
                    conflict.conflict_identity_sha256
                    != entry["logical_identity_sha256"]
                    or conflict.conflict_identity_sha256
                    != entry["canonical_content_sha256"]
                    or _sha256_json(conflict.content) != entry["payload_sha256"]
                    or len(compressed_bytes) != entry["compressed_bytes"]
                    or hashlib.sha256(compressed_bytes).hexdigest()
                    != entry["compressed_bytes_sha256"]
                ):
                    raise _ArchiveError("manifest_conflict_mismatch")
        if manifest["operation"] == "conflict_archive":
            conflict_entries = []
            for entry in manifest["entries"]:  # type: ignore[index]
                conflict = _read_conflict(root, entry["path"])
                conflict_entries.append(conflict.identity)
            expected_transaction_identity = _sha256_json(
                {
                    "entries": sorted(conflict_entries, key=canonical_json_bytes),
                    "operation": "conflict_archive",
                    "schema_version": _SCHEMA_VERSION,
                }
            )
            if expected_transaction_identity != manifest["batch_identity"]:
                raise _ArchiveError("manifest_conflict_identity_mismatch")
    _validate_provider_consistency(list(shards.values()))
    return shards, manifests


def _conflict_identity(
    *,
    evidence_type: str,
    schema_version: str,
    logical_identity: Mapping[str, object],
    existing_canonical_sha256: str,
    incoming_canonical_sha256: str,
    reason_code: str,
) -> tuple[dict[str, object], str]:
    if reason_code not in _CONFLICT_REASON_CODES:
        raise _ArchiveError("invalid_conflict_reason")
    identity: dict[str, object] = {
        "evidence_type": evidence_type,
        "existing_canonical_sha256": existing_canonical_sha256,
        "incoming_canonical_sha256": incoming_canonical_sha256,
        "logical_identity": dict(logical_identity),
        "reason_code": reason_code,
        "schema_version": schema_version,
    }
    return identity, _sha256_json(identity)


def _conflict_from_pair(
    *,
    existing: _Shard,
    incoming: _Shard,
    reason_code: str,
) -> _Conflict:
    identity, identity_sha = _conflict_identity(
        evidence_type=incoming.evidence_type,
        schema_version=_require_string(
            incoming.envelope.get("schema_version"), error_code="invalid_schema_version"
        ),
        logical_identity=incoming.envelope["logical_identity"],  # type: ignore[arg-type]
        existing_canonical_sha256=existing.canonical_content_sha256,
        incoming_canonical_sha256=incoming.canonical_content_sha256,
        reason_code=reason_code,
    )
    content = {
        **identity,
        "existing_bytes_sha256": hashlib.sha256(existing.compressed_bytes).hexdigest(),
        "incoming_bytes_sha256": hashlib.sha256(incoming.compressed_bytes).hexdigest(),
        "logical_identity_sha256": incoming.logical_identity_sha256,
    }
    partition_date = min(existing.partition_date, incoming.partition_date)
    path = (
        f"evidence/conflicts/{partition_date[:4]}/{partition_date[5:7]}/"
        f"{partition_date[8:10]}/{identity_sha}.json.gz"
    )
    return _Conflict(
        identity=identity,
        conflict_identity_sha256=identity_sha,
        content=content,
        path=path,
        partition_date=partition_date,
    )


def _read_conflict(root: Path, relative_path: str) -> _Conflict:
    relative = Path(relative_path)
    _safe_relative_path(relative)
    if len(relative.parts) != 6 or relative.parts[:2] != ("evidence", "conflicts"):
        raise _ArchiveError("invalid_conflict_path")
    partition_date = _validate_date("-".join(relative.parts[2:5]))
    filename = relative.parts[5]
    if not filename.endswith(".json.gz") or not _SHA256_RE.fullmatch(filename[:-8]):
        raise _ArchiveError("invalid_conflict_path")
    path = root.joinpath(*relative.parts)
    try:
        compressed = path.read_bytes()
        if len(compressed) > _MAX_COMPRESSED_BYTES:
            raise _ArchiveError("compressed_size_exceeded")
        raw = decompress_single_member_gzip(
            compressed,
            max_uncompressed_bytes=_MAX_UNCOMPRESSED_BYTES,
        )
        parsed = strict_json_loads_bytes(raw)
    except (OSError, TypeError, ValueError) as exc:
        raise _ArchiveError("invalid_conflict") from exc
    if not isinstance(parsed, Mapping):
        raise _ArchiveError("invalid_conflict")
    if canonical_json_bytes(parsed) != raw:
        raise _ArchiveError("invalid_conflict")
    if parsed.get("schema_version") != _SCHEMA_VERSION:
        raise _ArchiveError("invalid_conflict")
    required = {
        "evidence_type",
        "existing_canonical_sha256",
        "incoming_canonical_sha256",
        "logical_identity",
        "reason_code",
        "schema_version",
        "existing_bytes_sha256",
        "incoming_bytes_sha256",
        "logical_identity_sha256",
    }
    if set(parsed) != required:
        raise _ArchiveError("invalid_conflict")
    identity, identity_sha = _conflict_identity(
        evidence_type=parsed["evidence_type"],  # type: ignore[arg-type]
        schema_version=parsed["schema_version"],  # type: ignore[arg-type]
        logical_identity=parsed["logical_identity"],  # type: ignore[arg-type]
        existing_canonical_sha256=parsed["existing_canonical_sha256"],  # type: ignore[arg-type]
        incoming_canonical_sha256=parsed["incoming_canonical_sha256"],  # type: ignore[arg-type]
        reason_code=parsed["reason_code"],  # type: ignore[arg-type]
    )
    if (
        parsed["logical_identity_sha256"]
        != _identity_sha256(parsed["logical_identity"])  # type: ignore[arg-type]
        or not _is_sha256(parsed["existing_bytes_sha256"])
        or not _is_sha256(parsed["incoming_bytes_sha256"])
        or identity_sha != filename[:-8]
    ):
        raise _ArchiveError("invalid_conflict")
    return _Conflict(
        identity=identity,
        conflict_identity_sha256=identity_sha,
        content=dict(parsed),
        path=relative.as_posix(),
        partition_date=partition_date,
    )


def _conflict_transaction_identity(conflicts: Sequence[_Conflict]) -> str:
    entries = sorted(
        [conflict.identity for conflict in conflicts],
        key=canonical_json_bytes,
    )
    return _sha256_json(
        {
            "entries": entries,
            "operation": "conflict_archive",
            "schema_version": _SCHEMA_VERSION,
        }
    )


def _conflict_manifest(
    conflicts: Sequence[_Conflict], transaction_identity: str
) -> tuple[str, bytes]:
    if not conflicts:
        raise _ArchiveError("empty_conflict_transaction")
    partition_date = min(conflict.partition_date for conflict in conflicts)
    entries = [
        {
            "canonical_content_sha256": conflict.conflict_identity_sha256,
            "compressed_bytes_sha256": hashlib.sha256(
                deterministic_gzip(canonical_json_bytes(conflict.content))
            ).hexdigest(),
            "compressed_bytes": len(
                deterministic_gzip(canonical_json_bytes(conflict.content))
            ),
            "evidence_type": "conflict",
            "logical_identity_sha256": conflict.conflict_identity_sha256,
            "path": conflict.path,
            "partition_date": conflict.partition_date,
            "payload_sha256": _sha256_json(conflict.content),
        }
        for conflict in sorted(conflicts, key=lambda item: item.path)
    ]
    manifest: dict[str, object] = {
        "batch_identity": transaction_identity,
        "batch_sha256": transaction_identity,
        "counts": {
            "by_evidence_type": {"conflict": len(entries)},
            "by_status": {"conflict": len(entries)},
        },
        "entries": entries,
        "logical_identities": [entry["logical_identity_sha256"] for entry in entries],
        "operation": "conflict_archive",
        "schema_version": _SCHEMA_VERSION,
        "shards": [entry["path"] for entry in entries],
        "status": "conflict",
    }
    path = _manifest_path(transaction_identity, partition_date)
    return path, deterministic_gzip(canonical_json_bytes(manifest))


def _date_value(value: object) -> date:
    return date.fromisoformat(_validate_date(value))


def _corporate_overlap_conflict(
    *,
    existing: _Shard,
    incoming: _Shard,
) -> bool:
    if existing.evidence_type != "corporate_action" or incoming.evidence_type != "corporate_action":
        return False
    existing_identity = existing.envelope["logical_identity"]
    incoming_identity = incoming.envelope["logical_identity"]
    if not isinstance(existing_identity, Mapping) or not isinstance(incoming_identity, Mapping):
        raise _ArchiveError("invalid_corporate_identity")
    if (
        existing_identity.get("corporate_action_provider")
        != incoming_identity.get("corporate_action_provider")
        or existing_identity.get("symbol") != incoming_identity.get("symbol")
    ):
        return False
    existing_start = _date_value(existing_identity.get("coverage_start_date"))
    existing_end = _date_value(existing_identity.get("coverage_end_date"))
    incoming_start = _date_value(incoming_identity.get("coverage_start_date"))
    incoming_end = _date_value(incoming_identity.get("coverage_end_date"))
    overlap_start = max(existing_start, incoming_start)
    overlap_end = min(existing_end, incoming_end)
    if overlap_start > overlap_end:
        return False

    def event_map(shard: _Shard) -> dict[str, object]:
        payload = shard.envelope.get("payload")
        if not isinstance(payload, Mapping):
            raise _ArchiveError("invalid_corporate_payload")
        events = payload.get("normalized_events")
        if not isinstance(events, list):
            raise _ArchiveError("invalid_corporate_events")
        result: dict[str, object] = {}
        for event in events:
            if not isinstance(event, Mapping):
                raise _ArchiveError("invalid_corporate_event")
            event_date = _date_value(event.get("effective_date"))
            if overlap_start <= event_date <= overlap_end:
                ratio = event.get("split_ratio")
                if not isinstance(ratio, Mapping):
                    raise _ArchiveError("invalid_corporate_ratio")
                result[event_date.isoformat()] = {
                    "new_shares": ratio.get("new_shares"),
                    "old_shares": ratio.get("old_shares"),
                }
        return result

    return canonical_json_bytes(event_map(existing)) != canonical_json_bytes(
        event_map(incoming)
    )


def _market_series_key(shard: _Shard) -> tuple[str, str, str, str, str] | None:
    if shard.evidence_type != "market_bar":
        return None
    identity = shard.envelope.get("logical_identity")
    payload = shard.envelope.get("payload")
    provenance = shard.envelope.get("semantic_provenance")
    if (
        not isinstance(identity, Mapping)
        or not isinstance(payload, Mapping)
        or not isinstance(provenance, Mapping)
    ):
        raise _ArchiveError("invalid_market_bar")
    values = (
        identity.get("asset_type"),
        identity.get("symbol"),
        identity.get("interval"),
        identity.get("market_timezone"),
        payload.get("price_basis"),
    )
    if any(not isinstance(value, str) or not value for value in values):
        raise _ArchiveError("invalid_market_bar")
    provider = provenance.get("price_provider")
    if not isinstance(provider, str) or not provider:
        raise _ArchiveError("invalid_market_provider")
    return values  # type: ignore[return-value]


def _market_provider(shard: _Shard) -> str | None:
    if shard.evidence_type != "market_bar":
        return None
    provenance = shard.envelope.get("semantic_provenance")
    if not isinstance(provenance, Mapping):
        raise _ArchiveError("invalid_market_provider")
    provider = provenance.get("price_provider")
    if not isinstance(provider, str) or not provider:
        raise _ArchiveError("invalid_market_provider")
    return provider


def _validate_provider_consistency(shards: Sequence[_Shard]) -> None:
    providers_by_series: dict[tuple[str, str, str, str, str], str] = {}
    for shard in shards:
        series_key = _market_series_key(shard)
        if series_key is None:
            continue
        provider = _market_provider(shard)
        if provider is None:
            raise _ArchiveError("invalid_market_provider")
        previous = providers_by_series.get(series_key)
        if previous is not None and previous != provider:
            raise _ArchiveError("provider_mixing")
        providers_by_series[series_key] = provider


def _validate_existing_batch(
    *,
    root: Path,
    manifest: Mapping[str, object],
    incoming: Sequence[_Shard],
) -> bool:
    if manifest.get("operation") != "archive" or manifest.get("status") != "committed":
        return False
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        return False
    incoming_by_key = {
        (shard.evidence_type, shard.logical_identity_sha256): shard
        for shard in incoming
    }
    if len(entries) != len(incoming_by_key):
        return False
    for entry in entries:
        if not isinstance(entry, Mapping):
            return False
        key = (entry.get("evidence_type"), entry.get("logical_identity_sha256"))
        shard = incoming_by_key.get(key)  # type: ignore[arg-type]
        if shard is None:
            return False
        if (
            entry.get("canonical_content_sha256") != shard.canonical_content_sha256
            or entry.get("payload_sha256") != shard.payload_sha256
            or entry.get("path") != shard.path
        ):
            return False
        try:
            existing = _read_shard_bytes(root=root, relative_path=entry["path"])  # type: ignore[arg-type]
        except _ArchiveError:
            return False
        if (
            existing.canonical_content_sha256 != shard.canonical_content_sha256
            or existing.payload_sha256 != shard.payload_sha256
            or existing.canonical_bytes != shard.canonical_bytes
        ):
            return False
    return True


def _configure_clone(clone: Path, *, name: str = "Evidence Archive", email: str = "archive@example.invalid") -> None:
    _run_git(clone, "config", "user.name", name)
    _run_git(clone, "config", "user.email", email)


def _clone_branch(origin: str, branch_name: str, parent: Path) -> Path:
    clone = parent / "evidence-clone"
    _run_git(
        parent,
        "clone",
        "--branch",
        branch_name,
        "--single-branch",
        origin,
        str(clone),
    )
    _configure_clone(clone)
    return clone


def _push_commit(clone: Path, branch_name: str, paths: Sequence[str]) -> tuple[bool, str]:
    unique_paths = sorted(set(paths))
    if not unique_paths:
        raise _ArchiveError("empty_commit")
    try:
        _run_git(clone, "add", "--", *unique_paths)
    except _ArchiveError as exc:
        raise _ArchiveError("storage_error") from exc
    committed = _run_git(
        clone,
        "commit",
        "-m",
        "archive evidence batch",
        check=False,
    )
    if committed.returncode != 0:
        return False, "storage_error"
    commit_sha = _git_text(clone, "rev-parse", "HEAD").strip()
    pushed = _run_git(
        clone,
        "push",
        "--porcelain",
        "origin",
        f"HEAD:refs/heads/{branch_name}",
        check=False,
    )
    if pushed.returncode != 0:
        return False, "push_failed"
    remote_sha = _git_text(
        clone,
        "ls-remote",
        "origin",
        f"refs/heads/{branch_name}",
    ).split()[0]
    if remote_sha != commit_sha:
        return False, "push_unconfirmed"
    if any(line.startswith(b"=") for line in pushed.stdout.splitlines()):
        return False, "push_already_up_to_date"
    return True, commit_sha


def _existing_conflicts(root: Path) -> dict[str, _Conflict]:
    result: dict[str, _Conflict] = {}
    conflicts_root = root / "evidence" / "conflicts"
    if not conflicts_root.exists():
        return result
    for path in sorted(conflicts_root.rglob("*.json.gz")):
        relative = path.relative_to(root).as_posix()
        conflict = _read_conflict(root, relative)
        if conflict.conflict_identity_sha256 in result:
            raise _ArchiveError("duplicate_conflict")
        result[conflict.conflict_identity_sha256] = conflict
    return result


def _prepare_conflicts(
    *,
    root: Path,
    conflicts: Sequence[_Conflict],
) -> tuple[str, tuple[str, ...], dict[str, bytes]]:
    transaction_identity = _conflict_transaction_identity(conflicts)
    manifest_path, manifest_bytes = _conflict_manifest(conflicts, transaction_identity)
    files: dict[str, bytes] = {}
    for conflict in conflicts:
        files[conflict.path] = deterministic_gzip(canonical_json_bytes(conflict.content))
    files[manifest_path] = manifest_bytes
    existing = _existing_conflicts(root)
    missing = {
        path: data
        for path, data in files.items()
        if path not in {
            conflict.path for conflict in existing.values()
        }
    }
    existing_manifest = root.joinpath(*Path(manifest_path).parts)
    if existing_manifest.is_file():
        existing_object = _read_manifest(root, manifest_path)
        expected_object = _validate_manifest_object(
            strict_json_loads_bytes(
                decompress_single_member_gzip(
                    manifest_bytes,
                    max_uncompressed_bytes=_MAX_UNCOMPRESSED_BYTES,
                )
            ),
            expected_path=manifest_path,
        )
        if existing_object != expected_object:
            raise _ArchiveError("manifest_conflict_mismatch")
        missing.pop(manifest_path, None)
    return transaction_identity, tuple(sorted(path for path in files if path.startswith("evidence/conflicts/"))), missing


class EvidenceArchive:
    def __init__(
        self,
        *,
        repo_dir: Path,
        branch_name: str = "advisor-evidence",
        max_push_attempts: int = 3,
    ):
        if not isinstance(repo_dir, Path):
            raise TypeError("repo_dir must be a Path")
        if isinstance(max_push_attempts, bool) or not isinstance(max_push_attempts, int):
            raise TypeError("max_push_attempts must be an integer")
        if max_push_attempts < 1 or max_push_attempts > 3:
            raise ValueError("max_push_attempts must be between one and three")
        self.repo_dir = repo_dir
        self.branch_name = _safe_branch_name(branch_name)
        self.max_push_attempts = max_push_attempts

    def archive(self, transport_dir: Path) -> ArchiveResult:
        batch_identity = ""
        try:
            incoming = _read_transport(transport_dir)
            batch_identity = _batch_identity(incoming)
            seen: dict[tuple[str, str], _Shard] = {}
            for shard in incoming:
                key = (shard.evidence_type, shard.logical_identity_sha256)
                previous = seen.get(key)
                if previous is not None and previous.canonical_content_sha256 != shard.canonical_content_sha256:
                    raise _ArchiveError("batch_identity_collision")
                if previous is not None:
                    raise _ArchiveError("duplicate_transport_shard")
                seen[key] = shard
            origin = _origin_url(self.repo_dir)
        except _ArchiveError as exc:
            return ArchiveResult(
                status="rejected",
                batch_identity=batch_identity,
                manifest_path=None,
                committed_paths=(),
                conflict_paths=(),
                error_code=exc.error_code,
                durability_confirmed=False,
            )
        except (OSError, TypeError, ValueError, subprocess.SubprocessError):
            return ArchiveResult(
                status="rejected",
                batch_identity=batch_identity,
                manifest_path=None,
                committed_paths=(),
                conflict_paths=(),
                error_code="storage_error",
                durability_confirmed=False,
            )

        for _attempt in range(self.max_push_attempts):
            try:
                if not _remote_branch_exists(self.repo_dir, origin, self.branch_name):
                    return ArchiveResult(
                        status="evidence_branch_missing",
                        batch_identity=batch_identity,
                        manifest_path=None,
                        committed_paths=(),
                        conflict_paths=(),
                        error_code="evidence_branch_missing",
                        durability_confirmed=False,
                    )
                with tempfile.TemporaryDirectory(prefix="evidence-archive-") as temporary_directory:
                    clone = _clone_branch(
                        origin,
                        self.branch_name,
                        Path(temporary_directory),
                    )
                    existing, manifests = _load_authority(
                        clone,
                        branch_name=self.branch_name,
                    )
                    manifest_path, _, manifest_bytes = _manifest_for_batch(
                        shards=incoming,
                        batch_identity=batch_identity,
                    )
                    existing_manifest = manifests.get(batch_identity)
                    if existing_manifest is not None:
                        if _validate_existing_batch(
                            root=clone,
                            manifest=existing_manifest,
                            incoming=incoming,
                        ):
                            return ArchiveResult(
                                status="no_op",
                                batch_identity=batch_identity,
                                manifest_path=manifest_path,
                                committed_paths=(),
                                conflict_paths=(),
                                error_code=None,
                                durability_confirmed=True,
                            )
                        raise _ArchiveError("manifest_batch_mismatch")

                    conflicts: list[_Conflict] = []
                    new_shards: list[_Shard] = []
                    corporate_equivalent_only = bool(incoming)
                    for shard in incoming:
                        key = (shard.evidence_type, shard.logical_identity_sha256)
                        existing_shard = existing.get(key)
                        if existing_shard is None:
                            corporate_equivalent_only = False
                            incoming_series = _market_series_key(shard)
                            incoming_provider = _market_provider(shard)
                            provider_conflict = next(
                                (
                                    candidate
                                    for candidate in existing.values()
                                    if _market_series_key(candidate) == incoming_series
                                    and _market_provider(candidate) != incoming_provider
                                ),
                                None,
                            )
                            if provider_conflict is not None:
                                conflicts.append(
                                    _conflict_from_pair(
                                        existing=provider_conflict,
                                        incoming=shard,
                                        reason_code="provider_mixing",
                                    )
                                )
                                continue
                            for candidate in existing.values():
                                if (
                                    candidate.evidence_type == shard.evidence_type
                                    and _corporate_overlap_conflict(
                                        existing=candidate,
                                        incoming=shard,
                                    )
                                ):
                                    conflicts.append(
                                        _conflict_from_pair(
                                            existing=candidate,
                                            incoming=shard,
                                            reason_code="corporate_action_revision_conflict",
                                        )
                                    )
                            if not conflicts or conflicts[-1].path != shard.path:
                                new_shards.append(shard)
                            continue
                        if (
                            _market_series_key(existing_shard) is not None
                            and _market_provider(existing_shard)
                            != _market_provider(shard)
                        ):
                            conflicts.append(
                                _conflict_from_pair(
                                    existing=existing_shard,
                                    incoming=shard,
                                    reason_code="provider_mixing",
                                )
                            )
                            continue
                        if shard.evidence_type == "corporate_action":
                            if _corporate_overlap_conflict(
                                existing=existing_shard,
                                incoming=shard,
                            ):
                                conflicts.append(
                                    _conflict_from_pair(
                                        existing=existing_shard,
                                        incoming=shard,
                                        reason_code="corporate_action_revision_conflict",
                                    )
                                )
                            elif classify_idempotency(
                                existing_shard.envelope,
                                shard.envelope,
                            ) != "duplicate_same":
                                conflicts.append(
                                    _conflict_from_pair(
                                        existing=existing_shard,
                                        incoming=shard,
                                        reason_code="divergent_payload",
                                    )
                                )
                            continue
                        corporate_equivalent_only = False
                        if classify_idempotency(existing_shard.envelope, shard.envelope) == "duplicate_same":
                            continue
                        conflicts.append(
                            _conflict_from_pair(
                                existing=existing_shard,
                                incoming=shard,
                                reason_code="divergent_payload",
                            )
                        )

                    if conflicts:
                        conflict_by_identity = {
                            conflict.conflict_identity_sha256: conflict
                            for conflict in conflicts
                        }
                        conflicts = sorted(
                            conflict_by_identity.values(),
                            key=lambda conflict: conflict.path,
                        )
                        transaction_identity, conflict_paths, files = _prepare_conflicts(
                            root=clone,
                            conflicts=conflicts,
                        )
                        if not files:
                            return ArchiveResult(
                                status="conflict",
                                batch_identity=batch_identity,
                                manifest_path=_manifest_path(
                                    transaction_identity,
                                    min(conflict.partition_date for conflict in conflicts),
                                ),
                                committed_paths=(),
                                conflict_paths=conflict_paths,
                                error_code="conflict",
                                durability_confirmed=False,
                            )
                        for path, data in files.items():
                            _write_under(clone, path, data)
                        committed, error_code = _push_commit(
                            clone,
                            self.branch_name,
                            list(files),
                        )
                        if not committed:
                            if error_code not in _RETRYABLE_PUSH_ERRORS:
                                raise _ArchiveError(error_code)
                            continue
                        return ArchiveResult(
                            status="conflict",
                            batch_identity=batch_identity,
                            manifest_path=_manifest_path(
                                transaction_identity,
                                min(conflict.partition_date for conflict in conflicts),
                            ),
                            committed_paths=tuple(sorted(files)),
                            conflict_paths=conflict_paths,
                            error_code="conflict",
                            durability_confirmed=False,
                        )

                    if not new_shards:
                        if corporate_equivalent_only and not conflicts:
                            return ArchiveResult(
                                status="no_op",
                                batch_identity=batch_identity,
                                manifest_path=None,
                                committed_paths=(),
                                conflict_paths=(),
                                error_code=None,
                                durability_confirmed=True,
                            )
                        raise _ArchiveError("missing_historical_manifest")
                    for shard in new_shards:
                        _write_under(clone, shard.path, shard.compressed_bytes)
                    _write_under(clone, manifest_path, manifest_bytes)
                    committed, error_code = _push_commit(
                        clone,
                        self.branch_name,
                        [shard.path for shard in new_shards] + [manifest_path],
                    )
                    if not committed:
                        if error_code not in _RETRYABLE_PUSH_ERRORS:
                            raise _ArchiveError(error_code)
                        continue
                    return ArchiveResult(
                        status="committed",
                        batch_identity=batch_identity,
                        manifest_path=manifest_path,
                        committed_paths=tuple(
                            sorted([shard.path for shard in new_shards] + [manifest_path])
                        ),
                        conflict_paths=(),
                        error_code=None,
                        durability_confirmed=True,
                    )
            except _ArchiveError as exc:
                if exc.error_code in _RETRYABLE_PUSH_ERRORS:
                    continue
                return ArchiveResult(
                    status="rejected",
                    batch_identity=batch_identity,
                    manifest_path=None,
                    committed_paths=(),
                    conflict_paths=(),
                    error_code=exc.error_code,
                    durability_confirmed=False,
                )
            except (OSError, TypeError, ValueError, subprocess.SubprocessError):
                return ArchiveResult(
                    status="rejected",
                    batch_identity=batch_identity,
                    manifest_path=None,
                    committed_paths=(),
                    conflict_paths=(),
                    error_code="storage_error",
                    durability_confirmed=False,
                )
        return ArchiveResult(
            status="rejected",
            batch_identity=batch_identity,
            manifest_path=None,
            committed_paths=(),
            conflict_paths=(),
            error_code="push_race_exhausted",
            durability_confirmed=False,
        )


def bootstrap_evidence_branch(
    *,
    repo_dir: Path,
    branch_name: str = "advisor-evidence",
) -> str:
    if not isinstance(repo_dir, Path):
        raise TypeError("repo_dir must be a Path")
    branch_name = _safe_branch_name(branch_name)
    origin = _origin_url(repo_dir)
    if _remote_branch_exists(repo_dir, origin, branch_name):
        raise _ArchiveError("evidence_branch_already_exists")
    with tempfile.TemporaryDirectory(prefix="evidence-bootstrap-") as temporary_directory:
        parent = Path(temporary_directory)
        clone = parent / "bootstrap-clone"
        _run_git(parent, "clone", origin, str(clone))
        _configure_clone(clone, name="Evidence Bootstrap", email="bootstrap@example.invalid")
        orphan = _run_git(clone, "switch", "--orphan", branch_name, check=False)
        if orphan.returncode != 0:
            raise _ArchiveError("bootstrap_orphan_failed")
        _run_git(clone, "rm", "-rf", "--ignore-unmatch", ".", check=False)
        schema = dict(_BRANCH_SCHEMA)
        schema["branch_name"] = branch_name
        _write_under(clone, "evidence/branch-schema.json", canonical_json_bytes(schema))
        _run_git(clone, "add", "--", "evidence/branch-schema.json")
        committed = _run_git(
            clone,
            "commit",
            "-m",
            "initialize evidence branch",
            check=False,
        )
        if committed.returncode != 0:
            raise _ArchiveError("bootstrap_commit_failed")
        root_sha = _git_text(clone, "rev-parse", "HEAD").strip()
        parents = _git_text(clone, "rev-list", "--parents", "-n", "1", "HEAD").split()
        tree = _git_text(clone, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        if len(parents) != 1 or tree != ["evidence/branch-schema.json"]:
            raise _ArchiveError("bootstrap_shape_invalid")
        pushed = _run_git(
            clone,
            "push",
            "origin",
            f"HEAD:refs/heads/{branch_name}",
            check=False,
        )
        if pushed.returncode != 0:
            raise _ArchiveError("bootstrap_push_failed")
        remote_sha = _git_text(
            clone,
            "ls-remote",
            "origin",
            f"refs/heads/{branch_name}",
        ).split()[0]
        if remote_sha != root_sha:
            raise _ArchiveError("bootstrap_push_unconfirmed")
        return root_sha


def oldest_canonical_provider_by_symbol(
    *,
    evidence_checkout: Path,
    symbols: Sequence[str],
) -> Mapping[str, str]:
    if not isinstance(evidence_checkout, Path):
        raise TypeError("evidence_checkout must be a Path")
    if not isinstance(symbols, Sequence) or isinstance(symbols, (str, bytes)):
        raise TypeError("symbols must be a sequence")
    requested = set()
    for symbol in symbols:
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("invalid_symbol")
        requested.add(symbol)
    _assert_no_symlinks(evidence_checkout)
    market_root = evidence_checkout / "evidence" / "market-bars"
    if not market_root.exists():
        return {}
    records: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path in sorted(market_root.rglob("*.json.gz")):
        relative = path.relative_to(evidence_checkout).as_posix()
        shard = _read_shard_bytes(
            root=evidence_checkout,
            relative_path=relative,
        )
        logical_identity = shard.envelope["logical_identity"]
        provenance = shard.envelope["semantic_provenance"]
        if not isinstance(logical_identity, Mapping) or not isinstance(provenance, Mapping):
            raise _ArchiveError("invalid_market_bar")
        symbol = logical_identity.get("symbol")
        market_date = logical_identity.get("market_date")
        provider = provenance.get("price_provider")
        if not isinstance(symbol, str) or not isinstance(provider, str) or not provider:
            raise _ArchiveError("invalid_market_provider")
        market_date = _validate_date(market_date, error_code="invalid_market_date")
        if symbol in requested:
            records[symbol].append((market_date, provider))
    result: dict[str, str] = {}
    for symbol in sorted(records):
        ordered = sorted(records[symbol], key=lambda item: item[0])
        providers = {provider for _, provider in ordered}
        if len(providers) != 1:
            raise _ArchiveError("mixed_price_provider")
        result[symbol] = ordered[0][1]
    return result
