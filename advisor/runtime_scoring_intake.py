"""Validate and preserve runtime scoring artifacts selected by the nightly job.

This module consumes already-downloaded GitHub artifact directories.  It does
not access the network, run scoring, read providers, or interpret decisions.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping

from .runtime_scoring_artifact import (
    ArtifactValidationError,
    DEFAULT_MAX_DECOMPRESSED_MEMBER_BYTES,
    validate_runtime_scoring_artifact,
)


_SINGLE_NAME = "scoring-runtime-trace.json.gz"
_INDEX_NAME = "scoring-runtime-trace.index.json"
_PART_PATTERN = re.compile(r"^scoring-runtime-trace\.part-(\d{4})\.json\.gz$")
_MANIFEST_NAME = "nightly-runtime-manifest.json"
_SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_RUN_ID_PATTERN = re.compile(r"^\d{1,40}$")
_ARTIFACT_NAME_PATTERN = re.compile(r"^financial-advisor-(?:main|close)-\d{1,40}$")
_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_MAX_METADATA_BYTES = DEFAULT_MAX_DECOMPRESSED_MEMBER_BYTES
_DIRECTORY_RENAME_RETRY_DELAYS = (0.01, 0.05)
_WINDOWS_TRANSIENT_RENAME_ERRORS = {5, 32}


class RuntimeIntakeUnavailable(RuntimeError):
    """Raised when the complete intake transaction cannot be published."""


@dataclass(frozen=True)
class RuntimeArtifactInput:
    """Metadata for one already-selected main or close GitHub artifact."""

    schedule: str
    artifact_root: Path | str
    github_run_id: str
    github_artifact_name: str
    expected_source_sha: str
    expected_report_date: str
    expected_schedule: str

    def root_path(self) -> Path:
        return Path(self.artifact_root)


@dataclass(frozen=True)
class _LocatedBundle:
    mode: str
    primary_path: Path


def _safe_sha(value: object) -> str | None:
    if isinstance(value, str) and _SHA_PATTERN.fullmatch(value):
        return value
    return None


def _safe_date(value: object) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _safe_schedule(value: object) -> str | None:
    return value if value in {"main", "close"} else None


def _safe_run_id(value: object) -> str | None:
    candidate = str(value) if isinstance(value, int) else value
    if isinstance(candidate, str) and _RUN_ID_PATTERN.fullmatch(candidate):
        return candidate
    return None


def _safe_artifact_name(value: object) -> str | None:
    if isinstance(value, str) and _ARTIFACT_NAME_PATTERN.fullmatch(value):
        return value
    return None


def _safe_identifier(value: object) -> str | None:
    if isinstance(value, str) and _SAFE_IDENTIFIER_PATTERN.fullmatch(value):
        return value
    return None


def _safe_expected_source(value: object) -> str | None:
    return _safe_sha(value)


def _safe_expected_date(value: object) -> str | None:
    return _safe_date(value)


def _empty_entry(
    request: RuntimeArtifactInput,
    *,
    intake_status: str,
    validation_status: str,
    error_code: str | None,
) -> dict[str, object]:
    return {
        "artifact_status": None,
        "asset_count": None,
        "error_code": error_code,
        "expected_report_date": _safe_expected_date(request.expected_report_date),
        "expected_schedule": _safe_schedule(request.expected_schedule),
        "expected_source_sha": _safe_expected_source(request.expected_source_sha),
        "files": [],
        "github_artifact_name": _safe_artifact_name(request.github_artifact_name),
        "github_run_id": _safe_run_id(request.github_run_id),
        "intake_status": intake_status,
        "logical_run_id": None,
        "mode": None,
        "report_date": None,
        "rule_catalog_hash": None,
        "schedule": None,
        "source_sha": None,
        "validation_status": validation_status,
    }


def _is_symlink(path: Path) -> bool:
    return path.is_symlink() or os.path.islink(os.fspath(path))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def _locate_bundle(root: Path) -> tuple[str, _LocatedBundle | None]:
    """Find exactly one allowlisted bundle without following symlinks."""

    try:
        if _is_symlink(root):
            return "unsafe", None
        if not root.exists() or not root.is_dir():
            return "missing", None
        resolved_root = root.resolve(strict=True)
        singles: list[Path] = []
        indexes: list[Path] = []
        unsafe = False
        for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            for directory_name in list(directories):
                candidate = current_path / directory_name
                if _is_symlink(candidate):
                    unsafe = True
                    directories.remove(directory_name)
            for filename in filenames:
                candidate = current_path / filename
                if _is_symlink(candidate):
                    unsafe = True
                    continue
                if not _is_within(candidate, resolved_root):
                    unsafe = True
                    continue
                if candidate.parent.name != "runtime":
                    continue
                if filename == _SINGLE_NAME:
                    singles.append(candidate)
                elif filename == _INDEX_NAME:
                    indexes.append(candidate)
        if unsafe:
            return "unsafe", None
        candidates = singles + indexes
        if not candidates:
            return "missing", None
        if len(candidates) != 1:
            return "ambiguous", None
        primary = candidates[0]
        mode = "single" if primary.name == _SINGLE_NAME else "chunked"
        return "found", _LocatedBundle(mode=mode, primary_path=primary)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unavailable", None


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON value: {value}")


def _read_bounded_gzip(path: Path) -> bytes:
    output = bytearray()
    with gzip.open(path, "rb") as source:
        while True:
            remaining = _MAX_METADATA_BYTES - len(output)
            if remaining < 0:
                raise ValueError("decompressed metadata exceeds limit")
            chunk = source.read(min(1024 * 1024, remaining + 1))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > _MAX_METADATA_BYTES:
                raise ValueError("decompressed metadata exceeds limit")
    return bytes(output)


def _read_validated_payload(bundle: _LocatedBundle) -> dict[str, object]:
    if bundle.mode == "single":
        raw = _read_bounded_gzip(bundle.primary_path)
    else:
        if bundle.primary_path.stat().st_size > _MAX_METADATA_BYTES:
            raise ValueError("index metadata exceeds limit")
        raw = bundle.primary_path.read_bytes()
    payload = json.loads(
        raw,
        object_pairs_hook=_strict_pairs,
        parse_constant=_reject_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError("artifact payload is not an object")
    return payload


def _validated_part_paths(bundle: _LocatedBundle, payload: Mapping[str, object]) -> list[Path]:
    if bundle.mode != "chunked" or payload.get("artifact_status") == "failed":
        return []
    records = payload.get("parts")
    if not isinstance(records, list):
        raise ValueError("chunk parts are missing")
    result: list[Path] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("chunk descriptor is malformed")
        filename = record.get("filename")
        if not isinstance(filename, str) or _PART_PATTERN.fullmatch(filename) is None:
            raise ValueError("chunk filename is unsafe")
        if filename in seen:
            raise ValueError("duplicate chunk filename")
        seen.add(filename)
        path = bundle.primary_path.parent / filename
        if path.parent != bundle.primary_path.parent or _is_symlink(path) or not _is_within(path, bundle.primary_path.parent):
            raise ValueError("chunk path is unsafe")
        if not path.is_file():
            raise ValueError("chunk file is missing")
        result.append(path)
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_validated_files(
    bundle: _LocatedBundle,
    part_paths: list[Path],
    destination_dir: Path,
    *,
    output_dir: Path,
) -> list[dict[str, object]]:
    destination_dir.mkdir(parents=True, exist_ok=True)
    source_paths = [bundle.primary_path, *part_paths]
    records: list[dict[str, object]] = []
    for source in source_paths:
        if _is_symlink(source) or not source.is_file():
            raise OSError("validated source is unavailable")
        destination = destination_dir / source.name
        shutil.copyfile(source, destination)
        source_hash = _sha256_file(source)
        destination_hash = _sha256_file(destination)
        if source_hash != destination_hash:
            raise OSError("preserved file hash mismatch")
        records.append(
            {
                "path": f"{output_dir.name}/{destination_dir.name}/{destination.name}",
                "size_bytes": destination.stat().st_size,
                "sha256": destination_hash,
            }
        )
    return records


def _process_request(
    request: RuntimeArtifactInput,
    *,
    stage_dir: Path,
    output_dir: Path,
) -> dict[str, object]:
    location_status, bundle = _locate_bundle(request.root_path())
    if location_status == "missing":
        return _empty_entry(
            request,
            intake_status="missing",
            validation_status="not_found",
            error_code="runtime_missing",
        )
    if location_status == "ambiguous":
        return _empty_entry(
            request,
            intake_status="invalid",
            validation_status="rejected",
            error_code="runtime_ambiguous",
        )
    if location_status == "unsafe":
        return _empty_entry(
            request,
            intake_status="invalid",
            validation_status="rejected",
            error_code="runtime_unsafe_path",
        )
    if location_status == "unavailable" or bundle is None:
        return _empty_entry(
            request,
            intake_status="unavailable",
            validation_status="error",
            error_code="runtime_intake_unavailable",
        )

    try:
        validation = validate_runtime_scoring_artifact(bundle.primary_path)
    except ArtifactValidationError:
        return _empty_entry(
            request,
            intake_status="invalid",
            validation_status="rejected",
            error_code="runtime_validation_failed",
        )
    except (OSError, KeyError, TypeError, ValueError):
        return _empty_entry(
            request,
            intake_status="invalid",
            validation_status="error",
            error_code="runtime_validation_failed",
        )

    try:
        payload = _read_validated_payload(bundle)
        metadata = payload.get("run_metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("run metadata is missing")
        artifact_status = payload.get("artifact_status")
        if artifact_status != validation.artifact_status:
            raise ValueError("artifact status mismatch")
        expected_validator_mode = "single" if bundle.mode == "single" else "chunked"
        if bundle.mode == "single" and validation.mode != expected_validator_mode:
            raise ValueError("single mode mismatch")
        if bundle.mode == "chunked" and validation.mode not in {"chunked", "failed"}:
            raise ValueError("chunked mode mismatch")
        if bundle.mode == "single" and artifact_status == "failed":
            raise ValueError("failed single artifact is not supported")
        if metadata.get("source_sha") != request.expected_source_sha:
            raise ValueError("source binding mismatch")
        if metadata.get("report_date") != request.expected_report_date:
            raise ValueError("report date binding mismatch")
        if request.schedule not in {"main", "close"}:
            raise ValueError("request slot is invalid")
        if request.schedule != request.expected_schedule:
            raise ValueError("request schedule binding mismatch")
        if metadata.get("schedule") != request.schedule:
            raise ValueError("artifact schedule binding mismatch")
        catalog_hash = payload.get("rule_catalog_hash")
        logical_run_id = metadata.get("run_id")
        source_sha = metadata.get("source_sha")
        report_date = metadata.get("report_date")
        schedule = metadata.get("schedule")
        if _safe_sha(catalog_hash) is None or _safe_identifier(logical_run_id) is None:
            raise ValueError("validated metadata cannot be represented safely")
        part_paths = _validated_part_paths(bundle, payload)
    except (OSError, KeyError, TypeError, ValueError):
        return _empty_entry(
            request,
            intake_status="invalid",
            validation_status="rejected",
            error_code="runtime_binding_mismatch",
        )

    schedule_dir = stage_dir / request.schedule
    try:
        files = _copy_validated_files(
            bundle,
            part_paths,
            schedule_dir,
            output_dir=output_dir,
        )
    except (OSError, ValueError):
        if schedule_dir.exists() or schedule_dir.is_symlink():
            _remove_path(schedule_dir)
        return _empty_entry(
            request,
            intake_status="invalid",
            validation_status="rejected",
            error_code="runtime_copy_failed",
        )

    return {
        "artifact_status": artifact_status,
        "asset_count": len(validation.symbols),
        "error_code": None,
        "expected_report_date": _safe_expected_date(request.expected_report_date),
        "expected_schedule": _safe_schedule(request.expected_schedule),
        "expected_source_sha": _safe_expected_source(request.expected_source_sha),
        "files": files,
        "github_artifact_name": _safe_artifact_name(request.github_artifact_name),
        "github_run_id": _safe_run_id(request.github_run_id),
        "intake_status": "validated",
        "logical_run_id": _safe_identifier(logical_run_id),
        "mode": "single" if bundle.mode == "single" else "chunked",
        "report_date": _safe_date(report_date),
        "rule_catalog_hash": _safe_sha(catalog_hash),
        "schedule": _safe_schedule(schedule),
        "source_sha": _safe_sha(source_sha),
        "validation_status": "valid",
    }


def _manifest_bytes(manifest: Mapping[str, object]) -> bytes:
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ).replace("\r\n", "\n").replace("\r", "\n")
    return (encoded + "\n").encode("utf-8")


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _replace_directory_transactionally(source: Path, destination: Path) -> None:
    for attempt in range(len(_DIRECTORY_RENAME_RETRY_DELAYS) + 1):
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            winerror = getattr(error, "winerror", None)
            if (
                os.name != "nt"
                or winerror not in _WINDOWS_TRANSIENT_RENAME_ERRORS
                or attempt >= len(_DIRECTORY_RENAME_RETRY_DELAYS)
            ):
                raise
            time.sleep(_DIRECTORY_RENAME_RETRY_DELAYS[attempt])


def _publish_transaction(stage_dir: Path, output_dir: Path) -> None:
    parent = output_dir.parent
    backup: Path | None = None
    had_previous = output_dir.exists() or output_dir.is_symlink()
    try:
        if had_previous:
            backup = Path(tempfile.mkdtemp(prefix=".runtime-intake-backup-", dir=parent))
            backup.rmdir()
            _replace_directory_transactionally(output_dir, backup)
        _replace_directory_transactionally(stage_dir, output_dir)
        stage_dir = Path()
        if backup is not None:
            _remove_path(backup)
            backup = None
    except (OSError, ValueError) as error:
        if had_previous and backup is not None and backup.exists() and not output_dir.exists():
            try:
                _replace_directory_transactionally(backup, output_dir)
                backup = None
            except OSError:
                pass
        raise RuntimeIntakeUnavailable() from error
    finally:
        if stage_dir != Path() and (stage_dir.exists() or stage_dir.is_symlink()):
            _remove_path(stage_dir)
        if backup is not None and (backup.exists() or backup.is_symlink()):
            _remove_path(backup)


def intake_runtime_artifacts(
    *,
    output_dir: Path | str,
    main: RuntimeArtifactInput,
    close: RuntimeArtifactInput,
    source_report_sha: str | None = None,
) -> dict[str, object]:
    """Validate and publish main/close artifacts as one deterministic set."""

    final_dir = Path(output_dir)
    parent = final_dir.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix=".runtime-intake-staging-", dir=parent))
    except OSError as error:
        raise RuntimeIntakeUnavailable() from error

    try:
        entries = {
            "main": _process_request(main, stage_dir=stage_dir, output_dir=final_dir),
            "close": _process_request(close, stage_dir=stage_dir, output_dir=final_dir),
        }
        manifest: dict[str, object] = {
            "manifest_schema_version": "1.0",
            "source_report_sha": _safe_sha(source_report_sha) or _safe_sha(main.expected_source_sha),
            "entries": entries,
        }
        (stage_dir / _MANIFEST_NAME).write_bytes(_manifest_bytes(manifest))
        _publish_transaction(stage_dir, final_dir)
        return {"manifest": manifest, "entries": entries}
    except RuntimeIntakeUnavailable:
        raise
    except Exception as error:
        if stage_dir.exists() or stage_dir.is_symlink():
            _remove_path(stage_dir)
        raise RuntimeIntakeUnavailable() from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate selected runtime scoring artifacts locally.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-report-sha", required=False, default=None)
    for prefix in ("main", "close"):
        parser.add_argument(f"--{prefix}-root", required=True)
        parser.add_argument(f"--{prefix}-run-id", required=True)
        parser.add_argument(f"--{prefix}-artifact-name", required=True)
        parser.add_argument(f"--{prefix}-expected-source-sha", required=True)
        parser.add_argument(f"--{prefix}-expected-report-date", required=True)
        parser.add_argument(f"--{prefix}-expected-schedule", required=True)
    return parser


def _request_from_args(args: argparse.Namespace, prefix: str) -> RuntimeArtifactInput:
    return RuntimeArtifactInput(
        schedule=prefix,
        artifact_root=getattr(args, f"{prefix}_root"),
        github_run_id=getattr(args, f"{prefix}_run_id"),
        github_artifact_name=getattr(args, f"{prefix}_artifact_name"),
        expected_source_sha=getattr(args, f"{prefix}_expected_source_sha"),
        expected_report_date=getattr(args, f"{prefix}_expected_report_date"),
        expected_schedule=getattr(args, f"{prefix}_expected_schedule"),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = intake_runtime_artifacts(
            output_dir=args.output_dir,
            main=_request_from_args(args, "main"),
            close=_request_from_args(args, "close"),
            source_report_sha=args.source_report_sha,
        )
    except Exception:
        print("runtime_scoring_intake_unavailable")
        return 1
    entries = result["entries"]
    print(
        "runtime_scoring_intake=ok "
        f"main={entries['main']['intake_status']} "
        f"close={entries['close']['intake_status']}"
    )
    return 0


__all__ = [
    "RuntimeArtifactInput",
    "RuntimeIntakeUnavailable",
    "intake_runtime_artifacts",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
