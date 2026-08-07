import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from advisor.runtime_scoring_artifact import (
    ArtifactValidationError,
    DEFAULT_HARD_BUDGET_BYTES,
    validate_runtime_scoring_artifact,
    write_runtime_scoring_artifact,
)
from advisor.runtime_scoring_intake import (
    RuntimeArtifactInput,
    RuntimeIntakeUnavailable,
    _replace_directory_transactionally,
    intake_runtime_artifacts,
    main as intake_main,
)
from advisor.runtime_scoring_observability import RULE_CATALOG
from tests.test_runtime_scoring_artifact import RUN_METADATA, _asset


SOURCE_SHA = "25b11df3f2a4d3dd8cb85cd45682e43811e35884"
OTHER_SOURCE_SHA = "b" * 64
REPORT_DATE = "2026-07-29"


def _permission_error(winerror: int) -> PermissionError:
    error = PermissionError(13, "simulated access denied")
    error.winerror = winerror
    return error


def _metadata(*, schedule: str = "main", source_sha: str = SOURCE_SHA, report_date: str = REPORT_DATE) -> dict[str, object]:
    result = copy.deepcopy(RUN_METADATA)
    result.update(
        {
            "run_id": f"logical-{schedule}",
            "schedule": schedule,
            "source_sha": source_sha,
            "report_date": report_date,
        }
    )
    return result


def _write_bundle(
    root: Path,
    *,
    schedule: str = "main",
    source_sha: str = SOURCE_SHA,
    report_date: str = REPORT_DATE,
    assets: list | None = None,
    hard_budget_bytes: int = DEFAULT_HARD_BUDGET_BYTES,
) -> tuple[Path, object]:
    bundle_dir = root / "uploaded-root" / "reports" / "runtime"
    result = write_runtime_scoring_artifact(
        bundle_dir,
        run_metadata=_metadata(schedule=schedule, source_sha=source_sha, report_date=report_date),
        rule_catalog=RULE_CATALOG,
        assets=[_asset("AAA")] if assets is None else assets,
        hard_budget_bytes=hard_budget_bytes,
    )
    return bundle_dir, result


def _request(
    root: Path,
    *,
    schedule: str = "main",
    source_sha: str = SOURCE_SHA,
    report_date: str = REPORT_DATE,
    github_run_id: str = "123456",
    expected_source_sha: str | None = None,
    expected_report_date: str | None = None,
    expected_schedule: str | None = None,
) -> RuntimeArtifactInput:
    return RuntimeArtifactInput(
        schedule=schedule,
        artifact_root=root,
        github_run_id=github_run_id,
        github_artifact_name=f"financial-advisor-{schedule}-{github_run_id}",
        expected_source_sha=source_sha if expected_source_sha is None else expected_source_sha,
        expected_report_date=report_date if expected_report_date is None else expected_report_date,
        expected_schedule=schedule if expected_schedule is None else expected_schedule,
    )


def _read_manifest(output_dir: Path) -> tuple[dict[str, object], bytes]:
    path = output_dir / "nightly-runtime-manifest.json"
    data = path.read_bytes()
    return json.loads(data), data


def _entry_files(output_dir: Path, schedule: str) -> list[dict[str, object]]:
    manifest, _ = _read_manifest(output_dir)
    return manifest["entries"][schedule]["files"]


class RuntimeScoringIntakeTests(unittest.TestCase):
    def test_valid_single_main_and_close_are_bound_preserved_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main_root = root / "main-download"
            close_root = root / "close-download"
            main_bundle, main_result = _write_bundle(main_root, schedule="main")
            close_bundle, close_result = _write_bundle(close_root, schedule="close")

            output_one = root / "reports-one" / "runtime"
            output_two = root / "reports-two" / "runtime"
            first = intake_runtime_artifacts(
                output_dir=output_one,
                main=_request(main_root, schedule="main"),
                close=_request(close_root, schedule="close", github_run_id="123457"),
                source_report_sha=SOURCE_SHA,
            )
            second = intake_runtime_artifacts(
                output_dir=output_two,
                main=_request(main_root, schedule="main"),
                close=_request(close_root, schedule="close", github_run_id="123457"),
                source_report_sha=SOURCE_SHA,
            )

            self.assertEqual(first["entries"]["main"]["intake_status"], "validated")
            self.assertEqual(first["entries"]["close"]["intake_status"], "validated")
            self.assertEqual(first["entries"]["main"]["validation_status"], "valid")
            self.assertEqual(first["entries"]["close"]["validation_status"], "valid")
            self.assertEqual(first["entries"]["main"]["logical_run_id"], "logical-main")
            self.assertEqual(first["entries"]["close"]["logical_run_id"], "logical-close")
            self.assertEqual(first["entries"]["main"]["github_run_id"], "123456")
            self.assertEqual(first["entries"]["main"]["mode"], "single")
            self.assertEqual(first["entries"]["close"]["mode"], "single")
            self.assertEqual(first["entries"]["main"]["asset_count"], 1)
            self.assertEqual(first["entries"]["main"]["artifact_status"], "complete")
            self.assertEqual(first["entries"]["main"]["error_code"], None)
            self.assertEqual(first["manifest"], second["manifest"])

            for schedule, source_bundle, writer_result in (
                ("main", main_bundle, main_result),
                ("close", close_bundle, close_result),
            ):
                source_path = writer_result.output_paths[0]
                destination = output_one / schedule / source_path.name
                self.assertEqual(destination.read_bytes(), source_path.read_bytes())
                file_record = _entry_files(output_one, schedule)[0]
                self.assertEqual(file_record["path"], f"runtime/{schedule}/{source_path.name}")
                self.assertEqual(file_record["size_bytes"], source_path.stat().st_size)
                self.assertEqual(file_record["sha256"], hashlib.sha256(source_path.read_bytes()).hexdigest())
                self.assertTrue(destination.is_file())
                self.assertTrue(source_bundle.is_dir())

            manifest_path = output_one / "nightly-runtime-manifest.json"
            manifest_bytes = manifest_path.read_bytes()
            self.assertTrue(manifest_bytes.endswith(b"\n"))
            self.assertNotIn(b"\r", manifest_bytes)
            self.assertFalse(manifest_bytes.endswith(b"\n\n"))
            self.assertNotIn(str(root).encode(), manifest_bytes)

    def test_chunked_bundle_preserves_only_index_and_referenced_parts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = [_asset("AAA"), _asset("BBB"), _asset("CCC")]
            bundle_dir, writer_result = _write_bundle(
                root / "chunked-download",
                assets=assets,
                hard_budget_bytes=2000,
            )
            self.assertEqual(writer_result.mode, "chunked")
            extra = bundle_dir / "scoring-runtime-trace.part-9999.json.gz"
            extra.write_bytes(b"not referenced")

            output_dir = root / "reports" / "runtime"
            result = intake_runtime_artifacts(
                output_dir=output_dir,
                main=_request(root / "chunked-download"),
                close=_request(root / "missing-close", schedule="close", github_run_id="2"),
                source_report_sha=SOURCE_SHA,
            )

            entry = result["entries"]["main"]
            self.assertEqual(entry["intake_status"], "validated")
            self.assertEqual(entry["mode"], "chunked")
            self.assertEqual(len(entry["files"]), len(writer_result.output_paths))
            self.assertFalse((output_dir / "main" / extra.name).exists())
            for source_path in writer_result.output_paths:
                destination = output_dir / "main" / source_path.name
                self.assertEqual(destination.read_bytes(), source_path.read_bytes())
                self.assertIn(
                    {
                        "path": f"runtime/main/{source_path.name}",
                        "size_bytes": source_path.stat().st_size,
                        "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                    },
                    entry["files"],
                )

    def test_chunked_publish_retries_first_windows_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(
                root / "chunked-download",
                assets=[_asset("AAA"), _asset("BBB"), _asset("CCC")],
                hard_budget_bytes=2000,
            )
            output_dir = root / "reports" / "runtime"
            real_replace = os.replace
            rename_calls: list[tuple[Path, Path, bool]] = []

            def fail_first_replace(source: str | os.PathLike, destination: str | os.PathLike) -> None:
                source_path = Path(source)
                destination_path = Path(destination)
                if ".runtime-intake-staging-" in source_path.name:
                    rename_calls.append((source_path, destination_path, destination_path.exists()))
                    if len(rename_calls) == 1:
                        raise _permission_error(5)
                real_replace(source, destination)

            with patch("advisor.runtime_scoring_intake.os.replace", side_effect=fail_first_replace):
                with patch("time.sleep") as sleep:
                    try:
                        result = intake_runtime_artifacts(
                            output_dir=output_dir,
                            main=_request(root / "chunked-download"),
                            close=_request(root / "missing-close", schedule="close", github_run_id="2"),
                            source_report_sha=SOURCE_SHA,
                        )
                    except RuntimeIntakeUnavailable:
                        result = None

            self.assertIsNotNone(result)
            self.assertEqual(result["entries"]["main"]["intake_status"], "validated")  # type: ignore[index]
            self.assertGreaterEqual(len(rename_calls), 2)
            self.assertLessEqual(len(rename_calls), 3)
            self.assertFalse(rename_calls[0][2])
            self.assertTrue(all(not destination_exists for _, _, destination_exists in rename_calls))
            self.assertEqual(sleep.call_count, len(rename_calls) - 1)
            self.assertTrue((output_dir / "main" / "scoring-runtime-trace.index.json").is_file())
            self.assertEqual(list(output_dir.parent.glob(".runtime-intake-*")), [])

    def test_directory_replace_succeeds_on_first_attempt_without_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "payload.bin").write_bytes(b"payload")
            real_replace = os.replace

            with patch("advisor.runtime_scoring_intake.os.name", "nt"):
                with patch("advisor.runtime_scoring_intake.os.replace", side_effect=real_replace) as replace:
                    with patch("advisor.runtime_scoring_intake.time.sleep") as sleep:
                        _replace_directory_transactionally(source, destination)

            self.assertEqual(replace.call_count, 1)
            self.assertEqual(sleep.call_count, 0)
            self.assertFalse(source.exists())
            self.assertTrue((destination / "payload.bin").is_file())

    def test_directory_replace_retries_first_permission_error_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            real_replace = os.replace
            attempts = 0

            def replace_once(source_path: str | os.PathLike, destination_path: str | os.PathLike) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise _permission_error(5)
                real_replace(source_path, destination_path)

            with patch("advisor.runtime_scoring_intake.os.name", "nt"):
                with patch(
                    "advisor.runtime_scoring_intake.os.replace",
                    side_effect=replace_once,
                ) as replace:
                    with patch("advisor.runtime_scoring_intake.time.sleep") as sleep:
                        _replace_directory_transactionally(source, destination)

            self.assertEqual(replace.call_count, 2)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.01])
            self.assertFalse(source.exists())
            self.assertTrue(destination.exists())

    def test_directory_replace_retries_twice_then_succeeds_on_third_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            real_replace = os.replace
            attempts = 0

            def replace_twice(source_path: str | os.PathLike, destination_path: str | os.PathLike) -> None:
                nonlocal attempts
                attempts += 1
                if attempts <= 2:
                    raise _permission_error(5 if attempts == 1 else 32)
                real_replace(source_path, destination_path)

            with patch("advisor.runtime_scoring_intake.os.name", "nt"):
                with patch(
                    "advisor.runtime_scoring_intake.os.replace",
                    side_effect=replace_twice,
                ) as replace:
                    with patch("advisor.runtime_scoring_intake.time.sleep") as sleep:
                        _replace_directory_transactionally(source, destination)

            self.assertEqual(replace.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.01, 0.05])
            self.assertFalse(source.exists())
            self.assertTrue(destination.exists())

    def test_directory_replace_persistent_permission_errors_stop_after_three_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()

            with patch("advisor.runtime_scoring_intake.os.name", "nt"):
                with patch(
                    "advisor.runtime_scoring_intake.os.replace",
                    side_effect=[_permission_error(5), _permission_error(5), _permission_error(5)],
                ) as replace:
                    with patch("advisor.runtime_scoring_intake.time.sleep") as sleep:
                        with self.assertRaises(PermissionError):
                            _replace_directory_transactionally(source, destination)

            self.assertEqual(replace.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_directory_replace_does_not_retry_unknown_windows_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()

            with patch("advisor.runtime_scoring_intake.os.name", "nt"):
                with patch(
                    "advisor.runtime_scoring_intake.os.replace",
                    side_effect=_permission_error(87),
                ) as replace:
                    with patch("advisor.runtime_scoring_intake.time.sleep") as sleep:
                        with self.assertRaises(PermissionError):
                            _replace_directory_transactionally(source, destination)

            self.assertEqual(replace.call_count, 1)
            self.assertEqual(sleep.call_count, 0)
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_directory_replace_does_not_retry_file_not_found_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()

            with patch("advisor.runtime_scoring_intake.os.name", "nt"):
                with patch(
                    "advisor.runtime_scoring_intake.os.replace",
                    side_effect=FileNotFoundError(2, "missing source"),
                ) as replace:
                    with patch("advisor.runtime_scoring_intake.time.sleep") as sleep:
                        with self.assertRaises(FileNotFoundError):
                            _replace_directory_transactionally(source, destination)

            self.assertEqual(replace.call_count, 1)
            self.assertEqual(sleep.call_count, 0)
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_directory_replace_does_not_retry_permission_error_on_non_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()

            with patch("advisor.runtime_scoring_intake.os.name", "posix"):
                with patch(
                    "advisor.runtime_scoring_intake.os.replace",
                    side_effect=_permission_error(5),
                ) as replace:
                    with patch("advisor.runtime_scoring_intake.time.sleep") as sleep:
                        with self.assertRaises(PermissionError):
                            _replace_directory_transactionally(source, destination)

            self.assertEqual(replace.call_count, 1)
            self.assertEqual(sleep.call_count, 0)
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_directory_replace_waits_only_between_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            real_replace = os.replace
            attempts = 0

            def replace_twice(source_path: str | os.PathLike, destination_path: str | os.PathLike) -> None:
                nonlocal attempts
                attempts += 1
                if attempts <= 2:
                    raise _permission_error(5 if attempts == 1 else 32)
                real_replace(source_path, destination_path)

            with patch("advisor.runtime_scoring_intake.os.name", "nt"):
                with patch(
                    "advisor.runtime_scoring_intake.os.replace",
                    side_effect=replace_twice,
                ):
                    with patch("advisor.runtime_scoring_intake.time.sleep") as sleep:
                        _replace_directory_transactionally(source, destination)

            self.assertEqual(sleep.call_count, 2)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.01, 0.05])

    def test_persistent_windows_publish_failure_rolls_back_previous_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "reports" / "runtime"
            previous_files = {
                "main/previous.bin": b"previous main",
                "close/previous.bin": b"previous close",
                "old-manifest.json": b"previous manifest",
            }
            for relative_path, data in previous_files.items():
                path = output_dir / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            _write_bundle(
                root / "chunked-download",
                assets=[_asset("AAA"), _asset("BBB"), _asset("CCC")],
                hard_budget_bytes=2000,
            )
            real_replace = os.replace
            publish_attempts: list[tuple[Path, Path]] = []

            def fail_publish(source: str | os.PathLike, destination: str | os.PathLike) -> None:
                source_path = Path(source)
                if ".runtime-intake-staging-" in source_path.name:
                    publish_attempts.append((source_path, Path(destination)))
                    raise _permission_error(5)
                real_replace(source, destination)

            with patch("advisor.runtime_scoring_intake.os.replace", side_effect=fail_publish):
                with patch("time.sleep") as sleep:
                    with self.assertRaises(RuntimeIntakeUnavailable):
                        intake_runtime_artifacts(
                            output_dir=output_dir,
                            main=_request(root / "chunked-download"),
                            close=_request(root / "missing-close", schedule="close", github_run_id="2"),
                            source_report_sha=SOURCE_SHA,
                        )

            self.assertEqual(len(publish_attempts), 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(
                {
                    path.relative_to(output_dir).as_posix(): path.read_bytes()
                    for path in output_dir.rglob("*")
                    if path.is_file()
                },
                previous_files,
            )
            self.assertEqual(list(output_dir.parent.glob(".runtime-intake-*")), [])

    def test_persistent_windows_publish_failure_without_previous_runtime_leaves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "reports" / "runtime"
            _write_bundle(
                root / "chunked-download",
                assets=[_asset("AAA"), _asset("BBB"), _asset("CCC")],
                hard_budget_bytes=2000,
            )
            real_replace = os.replace
            publish_attempts: list[tuple[Path, Path]] = []

            def fail_publish(source: str | os.PathLike, destination: str | os.PathLike) -> None:
                source_path = Path(source)
                if ".runtime-intake-staging-" in source_path.name:
                    publish_attempts.append((source_path, Path(destination)))
                    raise _permission_error(32)
                real_replace(source, destination)

            with patch("advisor.runtime_scoring_intake.os.replace", side_effect=fail_publish):
                with patch("time.sleep") as sleep:
                    with self.assertRaises(RuntimeIntakeUnavailable):
                        intake_runtime_artifacts(
                            output_dir=output_dir,
                            main=_request(root / "chunked-download"),
                            close=_request(root / "missing-close", schedule="close", github_run_id="2"),
                            source_report_sha=SOURCE_SHA,
                        )

            self.assertEqual(len(publish_attempts), 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertFalse(output_dir.exists())
            self.assertEqual(list(output_dir.parent.glob(".runtime-intake-*")), [])

    def test_all_path_open_handles_are_closed_before_publish_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(
                root / "chunked-download",
                assets=[_asset("AAA"), _asset("BBB"), _asset("CCC")],
                hard_budget_bytes=2000,
            )
            output_dir = root / "reports" / "runtime"
            real_open = Path.open
            real_replace = os.replace
            handles: list[object] = []
            rename_observations: list[tuple[bool, bool]] = []

            def tracked_open(path: Path, *args: object, **kwargs: object) -> object:
                handle = real_open(path, *args, **kwargs)
                handles.append(handle)
                return handle

            def observe_replace(source: str | os.PathLike, destination: str | os.PathLike) -> None:
                source_path = Path(source)
                destination_path = Path(destination)
                if ".runtime-intake-staging-" in source_path.name:
                    rename_observations.append(
                        (all(getattr(handle, "closed", False) for handle in handles), destination_path.exists())
                    )
                real_replace(source, destination)

            with patch.object(Path, "open", autospec=True, side_effect=tracked_open):
                with patch("advisor.runtime_scoring_intake.os.replace", side_effect=observe_replace):
                    result = intake_runtime_artifacts(
                        output_dir=output_dir,
                        main=_request(root / "chunked-download"),
                        close=_request(root / "missing-close", schedule="close", github_run_id="2"),
                        source_report_sha=SOURCE_SHA,
                    )

            self.assertEqual(result["entries"]["main"]["intake_status"], "validated")
            self.assertGreaterEqual(len(rename_observations), 1)
            self.assertTrue(all(observation == (True, False) for observation in rename_observations))

    def test_missing_referenced_chunk_part_is_invalid_and_copies_no_part(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _bundle_dir, writer_result = _write_bundle(
                root / "chunked-download",
                assets=[_asset("AAA"), _asset("BBB"), _asset("CCC")],
                hard_budget_bytes=2000,
            )
            part_to_remove = next(
                path for path in writer_result.output_paths if ".part-" in path.name
            )
            part_to_remove.unlink()

            output_dir = root / "reports" / "runtime"
            result = intake_runtime_artifacts(
                output_dir=output_dir,
                main=_request(root / "chunked-download"),
                close=_request(root / "missing-close", schedule="close", github_run_id="2"),
                source_report_sha=SOURCE_SHA,
            )

            self.assertEqual(result["entries"]["main"]["intake_status"], "invalid")
            self.assertEqual(result["entries"]["main"]["error_code"], "runtime_validation_failed")
            self.assertFalse((output_dir / "main").exists())

    def test_valid_failed_artifact_is_preserved_as_auditable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            huge = _asset("HUGE")
            huge.trace.classification_inputs["evidence"] = hashlib.sha256(b"x").hexdigest() * 300
            _bundle_dir, writer_result = _write_bundle(
                root / "failed-download",
                assets=[huge],
                hard_budget_bytes=256,
            )
            self.assertEqual(writer_result.artifact_status, "failed")
            self.assertEqual(writer_result.mode, "failed")

            output_dir = root / "reports" / "runtime"
            result = intake_runtime_artifacts(
                output_dir=output_dir,
                main=_request(root / "failed-download"),
                close=_request(root / "missing-close", schedule="close", github_run_id="2"),
                source_report_sha=SOURCE_SHA,
            )

            entry = result["entries"]["main"]
            self.assertEqual(entry["intake_status"], "validated")
            self.assertEqual(entry["validation_status"], "valid")
            self.assertEqual(entry["artifact_status"], "failed")
            self.assertEqual(entry["mode"], "chunked")
            self.assertEqual(len(entry["files"]), 1)
            self.assertTrue((output_dir / "main" / "scoring-runtime-trace.index.json").is_file())
            serialized = json.dumps(entry, sort_keys=True).lower()
            self.assertNotIn("authorize", serialized)
            self.assertNotIn("trade", serialized)

    def test_missing_schedule_is_fail_open_without_reusing_old_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "reports" / "runtime"
            (output_dir / "main").mkdir(parents=True)
            (output_dir / "close").mkdir(parents=True)
            (output_dir / "main" / "old-main.bin").write_bytes(b"old main")
            (output_dir / "close" / "old-close.bin").write_bytes(b"old close")
            (output_dir / "stale-extra.bin").write_bytes(b"stale")

            _write_bundle(root / "close-download", schedule="close")
            result = intake_runtime_artifacts(
                output_dir=output_dir,
                main=_request(root / "missing-main"),
                close=_request(root / "close-download", schedule="close", github_run_id="2"),
                source_report_sha=SOURCE_SHA,
            )

            self.assertEqual(result["entries"]["main"]["intake_status"], "missing")
            self.assertEqual(result["entries"]["main"]["error_code"], "runtime_missing")
            self.assertEqual(result["entries"]["close"]["intake_status"], "validated")
            self.assertFalse((output_dir / "main").exists())
            self.assertFalse((output_dir / "stale-extra.bin").exists())
            self.assertFalse((output_dir / "close" / "old-close.bin").exists())
            self.assertTrue((output_dir / "close" / "scoring-runtime-trace.json.gz").is_file())

    def test_validation_failure_never_copies_adulterated_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main_dir, writer_result = _write_bundle(root / "main-download")
            source_path = writer_result.output_paths[0]
            original = source_path.read_bytes()
            source_path.write_bytes(original[:-1] + bytes([original[-1] ^ 0x01]))
            _write_bundle(root / "close-download", schedule="close")

            output_dir = root / "reports" / "runtime"
            result = intake_runtime_artifacts(
                output_dir=output_dir,
                main=_request(root / "main-download"),
                close=_request(root / "close-download", schedule="close", github_run_id="2"),
                source_report_sha=SOURCE_SHA,
            )

            self.assertEqual(result["entries"]["main"]["intake_status"], "invalid")
            self.assertEqual(result["entries"]["main"]["error_code"], "runtime_validation_failed")
            self.assertFalse((output_dir / "main").exists())
            self.assertTrue((output_dir / "close" / "scoring-runtime-trace.json.gz").is_file())
            self.assertTrue(main_dir.is_dir())

    def test_binding_mismatch_for_source_date_and_schedule_is_rejected_independently(self) -> None:
        cases = (
            ("source", {"source_sha": OTHER_SOURCE_SHA}, {"expected_source_sha": SOURCE_SHA}),
            ("date", {"report_date": "2026-07-30"}, {"expected_report_date": REPORT_DATE}),
            ("schedule", {"schedule": "close"}, {"expected_schedule": "main"}),
        )
        for name, bundle_kwargs, request_kwargs in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _write_bundle(root / "main-download", **bundle_kwargs)
                request = _request(root / "main-download", **request_kwargs)
                output_dir = root / "reports" / "runtime"
                result = intake_runtime_artifacts(
                    output_dir=output_dir,
                    main=request,
                    close=_request(root / "missing-close", schedule="close", github_run_id="2"),
                    source_report_sha=SOURCE_SHA,
                )
                self.assertEqual(result["entries"]["main"]["intake_status"], "invalid")
                self.assertEqual(result["entries"]["main"]["error_code"], "runtime_binding_mismatch")
                self.assertFalse((output_dir / "main").exists())

    def test_main_slot_cannot_accept_close_bundle_when_expected_schedule_is_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root / "main-download", schedule="close")
            _write_bundle(root / "close-download", schedule="close")
            output_dir = root / "reports" / "runtime"

            result = intake_runtime_artifacts(
                output_dir=output_dir,
                main=_request(
                    root / "main-download",
                    schedule="main",
                    expected_schedule="close",
                ),
                close=_request(root / "close-download", schedule="close", github_run_id="2"),
                source_report_sha=SOURCE_SHA,
            )

            main_entry = result["entries"]["main"]
            self.assertEqual(main_entry["intake_status"], "invalid")
            self.assertEqual(main_entry["validation_status"], "rejected")
            self.assertEqual(main_entry["error_code"], "runtime_binding_mismatch")
            self.assertEqual(main_entry["files"], [])
            self.assertFalse((output_dir / "main").exists())
            self.assertEqual(result["entries"]["close"]["intake_status"], "validated")
            self.assertTrue((output_dir / "close").is_dir())

    def test_close_slot_cannot_accept_main_bundle_when_expected_schedule_is_main(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root / "main-download", schedule="main")
            _write_bundle(root / "close-download", schedule="main")
            output_dir = root / "reports" / "runtime"

            result = intake_runtime_artifacts(
                output_dir=output_dir,
                main=_request(root / "main-download", schedule="main"),
                close=_request(
                    root / "close-download",
                    schedule="close",
                    expected_schedule="main",
                    github_run_id="2",
                ),
                source_report_sha=SOURCE_SHA,
            )

            close_entry = result["entries"]["close"]
            self.assertEqual(close_entry["intake_status"], "invalid")
            self.assertEqual(close_entry["validation_status"], "rejected")
            self.assertEqual(close_entry["error_code"], "runtime_binding_mismatch")
            self.assertEqual(close_entry["files"], [])
            self.assertFalse((output_dir / "close").exists())
            self.assertEqual(result["entries"]["main"]["intake_status"], "validated")
            self.assertTrue((output_dir / "main").is_dir())

    def test_triple_schedule_binding_accepts_only_matching_main_and_close_values(self) -> None:
        cases = (
            ("main", "main", "main", True),
            ("close", "close", "close", True),
            ("main", "close", "close", False),
            ("close", "main", "main", False),
            ("main", "main", "close", False),
            ("close", "close", "main", False),
            ("nightly", "nightly", "main", False),
            ("main", "nightly", "main", False),
        )
        for request_schedule, expected_schedule, artifact_schedule, valid in cases:
            with self.subTest(
                request_schedule=request_schedule,
                expected_schedule=expected_schedule,
                artifact_schedule=artifact_schedule,
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                candidate_root = root / "candidate-download"
                _write_bundle(candidate_root, schedule=artifact_schedule)
                output_dir = root / "reports" / "runtime"
                if request_schedule == "close":
                    _write_bundle(root / "main-download", schedule="main")
                    main_request = _request(root / "main-download", schedule="main", github_run_id="1")
                    close_request = _request(
                        candidate_root,
                        schedule=request_schedule,
                        expected_schedule=expected_schedule,
                        github_run_id="2",
                    )
                    entry_key = "close"
                else:
                    _write_bundle(root / "close-download", schedule="close")
                    main_request = _request(
                        candidate_root,
                        schedule=request_schedule,
                        expected_schedule=expected_schedule,
                    )
                    close_request = _request(root / "close-download", schedule="close", github_run_id="2")
                    entry_key = "main"

                result = intake_runtime_artifacts(
                    output_dir=output_dir,
                    main=main_request,
                    close=close_request,
                    source_report_sha=SOURCE_SHA,
                )

                cli_args = ["--output-dir", str(output_dir), "--source-report-sha", SOURCE_SHA]
                for prefix, request in (("main", main_request), ("close", close_request)):
                    cli_args.extend(
                        [
                            f"--{prefix}-root",
                            str(request.artifact_root),
                            f"--{prefix}-run-id",
                            request.github_run_id,
                            f"--{prefix}-artifact-name",
                            request.github_artifact_name,
                            f"--{prefix}-expected-source-sha",
                            request.expected_source_sha,
                            f"--{prefix}-expected-report-date",
                            request.expected_report_date,
                            f"--{prefix}-expected-schedule",
                            request.expected_schedule,
                        ]
                    )
                with redirect_stdout(StringIO()) as cli_stdout:
                    cli_exit_code = intake_main(cli_args)
                self.assertEqual(cli_exit_code, 0)
                self.assertIn("runtime_scoring_intake=ok", cli_stdout.getvalue())

                self.assertTrue((output_dir / "nightly-runtime-manifest.json").is_file())
                entry = result["entries"][entry_key]
                if valid:
                    self.assertEqual(entry["intake_status"], "validated")
                    self.assertEqual(entry["validation_status"], "valid")
                    self.assertEqual(entry["error_code"], None)
                    self.assertTrue((output_dir / entry_key).is_dir())
                else:
                    self.assertEqual(entry["intake_status"], "invalid")
                    self.assertEqual(entry["validation_status"], "rejected")
                    self.assertEqual(entry["error_code"], "runtime_binding_mismatch")
                    self.assertEqual(entry["files"], [])
                    self.assertFalse((output_dir / entry_key).exists())
                    if request_schedule not in {"main", "close"}:
                        self.assertFalse((output_dir / request_schedule).exists())
                    other_key = "close" if entry_key == "main" else "main"
                    self.assertEqual(result["entries"][other_key]["intake_status"], "validated")
                    self.assertTrue((output_dir / other_key).is_dir())

    def test_ambiguous_single_or_multiple_bundles_never_selects_first(self) -> None:
        cases = ("single-and-index", "two-singles")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _write_bundle(root / "main-download", assets=[_asset("AAA"), _asset("BBB"), _asset("CCC")], hard_budget_bytes=2000 if case == "single-and-index" else DEFAULT_HARD_BUDGET_BYTES)
                if case == "single-and-index":
                    _write_bundle(root / "main-download", assets=[_asset("AAA"), _asset("BBB"), _asset("CCC")], hard_budget_bytes=DEFAULT_HARD_BUDGET_BYTES)
                else:
                    _write_bundle(root / "main-download-copy")
                    source = root / "main-download-copy" / "uploaded-root" / "reports" / "runtime" / "scoring-runtime-trace.json.gz"
                    target_dir = root / "main-download" / "second" / "runtime"
                    target_dir.mkdir(parents=True)
                    (target_dir / source.name).write_bytes(source.read_bytes())
                output_dir = root / "reports" / "runtime"
                result = intake_runtime_artifacts(
                    output_dir=output_dir,
                    main=_request(root / "main-download"),
                    close=_request(root / "missing-close", schedule="close", github_run_id="2"),
                    source_report_sha=SOURCE_SHA,
                )
                self.assertEqual(result["entries"]["main"]["intake_status"], "invalid")
                self.assertEqual(result["entries"]["main"]["error_code"], "runtime_ambiguous")
                self.assertFalse((output_dir / "main").exists())

    def test_candidate_symlink_rejection_is_deterministic_without_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _bundle_dir, writer_result = _write_bundle(root / "download")
            candidate = writer_result.output_paths[0]
            real_is_symlink = Path.is_symlink

            def candidate_only_is_symlink(path: Path) -> bool:
                if Path(path) == candidate:
                    return True
                return real_is_symlink(path)

            with patch.object(Path, "is_symlink", autospec=True, side_effect=candidate_only_is_symlink):
                with patch(
                    "advisor.runtime_scoring_intake.validate_runtime_scoring_artifact",
                    wraps=validate_runtime_scoring_artifact,
                ) as validator:
                    result = intake_runtime_artifacts(
                        output_dir=root / "reports" / "runtime",
                        main=_request(root / "download"),
                        close=_request(root / "missing-close", schedule="close", github_run_id="2"),
                        source_report_sha=SOURCE_SHA,
                    )

            self.assertEqual(result["entries"]["main"]["error_code"], "runtime_unsafe_path")
            self.assertEqual(result["entries"]["main"]["files"], [])
            self.assertEqual(validator.call_count, 0)
            self.assertTrue((root / "reports" / "runtime" / "nightly-runtime-manifest.json").is_file())
            self.assertFalse((root / "reports" / "runtime" / "main").exists())

    def test_validator_is_called_once_per_found_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root / "main-download")
            _write_bundle(root / "close-download", schedule="close")
            with patch(
                "advisor.runtime_scoring_intake.validate_runtime_scoring_artifact",
                wraps=validate_runtime_scoring_artifact,
            ) as validator:
                result = intake_runtime_artifacts(
                    output_dir=root / "reports" / "runtime",
                    main=_request(root / "main-download"),
                    close=_request(root / "close-download", schedule="close", github_run_id="2"),
                    source_report_sha=SOURCE_SHA,
                )
            self.assertEqual(validator.call_count, 2)

    def test_copy_failure_is_invalid_and_does_not_publish_partial_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root / "main-download")
            _write_bundle(root / "close-download", schedule="close")
            output_dir = root / "reports" / "runtime"
            real_copyfile = __import__("shutil").copyfile

            def fail_main_copy(source: str | os.PathLike, destination: str | os.PathLike) -> str:
                if str(destination).replace("\\", "/").endswith("/main/scoring-runtime-trace.json.gz"):
                    raise OSError("C:\\secret\\copy failure")
                return real_copyfile(source, destination)

            with patch("advisor.runtime_scoring_intake.shutil.copyfile", side_effect=fail_main_copy):
                result = intake_runtime_artifacts(
                    output_dir=output_dir,
                    main=_request(root / "main-download"),
                    close=_request(root / "close-download", schedule="close", github_run_id="2"),
                    source_report_sha=SOURCE_SHA,
                )

            self.assertEqual(result["entries"]["main"]["error_code"], "runtime_copy_failed")
            self.assertFalse((output_dir / "main").exists())
            self.assertTrue((output_dir / "close").is_dir())

    def test_publication_failure_rolls_back_complete_previous_runtime_and_leaves_no_residue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "reports" / "runtime"
            previous = output_dir / "main" / "previous.bin"
            previous.parent.mkdir(parents=True)
            previous.write_bytes(b"previous runtime")
            previous_manifest = {
                path.relative_to(output_dir).as_posix(): path.read_bytes()
                for path in output_dir.rglob("*")
                if path.is_file()
            }
            _write_bundle(root / "main-download")
            _write_bundle(root / "close-download", schedule="close")

            real_replace = os.replace

            def fail_stage_publish(source: str | os.PathLike, destination: str | os.PathLike) -> None:
                if Path(destination).name == "runtime" and ".runtime-intake-staging-" in Path(source).name:
                    raise OSError("C:\\secret\\publish failure")
                real_replace(source, destination)

            with patch("advisor.runtime_scoring_intake.os.replace", side_effect=fail_stage_publish):
                with self.assertRaises(RuntimeIntakeUnavailable):
                    intake_runtime_artifacts(
                        output_dir=output_dir,
                        main=_request(root / "main-download"),
                        close=_request(root / "close-download", schedule="close", github_run_id="2"),
                        source_report_sha=SOURCE_SHA,
                    )

            self.assertEqual(
                {
                    path.relative_to(output_dir).as_posix(): path.read_bytes()
                    for path in output_dir.rglob("*")
                    if path.is_file()
                },
                previous_manifest,
            )
            self.assertEqual(list(output_dir.parent.glob(".runtime-intake-staging-*")), [])
            self.assertEqual(list(output_dir.parent.glob(".runtime-intake-backup-*")), [])

    def test_cli_sanitizes_validator_errors_and_unexpected_process_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root / "main-download")
            _write_bundle(root / "close-download", schedule="close")
            output_dir = root / "reports" / "runtime"
            stdout = StringIO()
            with patch(
                "advisor.runtime_scoring_intake.validate_runtime_scoring_artifact",
                side_effect=ArtifactValidationError("C:\\private\\TOKEN=secret"),
            ), redirect_stdout(stdout):
                code = intake_main(
                    [
                        "--output-dir",
                        str(output_dir),
                        "--source-report-sha",
                        SOURCE_SHA,
                        "--main-root",
                        str(root / "main-download"),
                        "--main-run-id",
                        "1",
                        "--main-artifact-name",
                        "financial-advisor-main-1",
                        "--main-expected-source-sha",
                        SOURCE_SHA,
                        "--main-expected-report-date",
                        REPORT_DATE,
                        "--main-expected-schedule",
                        "main",
                        "--close-root",
                        str(root / "close-download"),
                        "--close-run-id",
                        "2",
                        "--close-artifact-name",
                        "financial-advisor-close-2",
                        "--close-expected-source-sha",
                        SOURCE_SHA,
                        "--close-expected-report-date",
                        REPORT_DATE,
                        "--close-expected-schedule",
                        "close",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertNotIn("private", stdout.getvalue())
            self.assertNotIn("secret", stdout.getvalue())
            self.assertNotIn(str(root), stdout.getvalue())

            stdout = StringIO()
            with patch(
                "advisor.runtime_scoring_intake.intake_runtime_artifacts",
                side_effect=RuntimeError("C:\\private\\TOKEN=secret"),
            ), redirect_stdout(stdout):
                code = intake_main(
                    [
                        "--output-dir",
                        str(output_dir),
                        "--source-report-sha",
                        SOURCE_SHA,
                        "--main-root",
                        str(root / "main-download"),
                        "--main-run-id",
                        "1",
                        "--main-artifact-name",
                        "financial-advisor-main-1",
                        "--main-expected-source-sha",
                        SOURCE_SHA,
                        "--main-expected-report-date",
                        REPORT_DATE,
                        "--main-expected-schedule",
                        "main",
                        "--close-root",
                        str(root / "close-download"),
                        "--close-run-id",
                        "2",
                        "--close-artifact-name",
                        "financial-advisor-close-2",
                        "--close-expected-source-sha",
                        SOURCE_SHA,
                        "--close-expected-report-date",
                        REPORT_DATE,
                        "--close-expected-schedule",
                        "close",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue().strip(), "runtime_scoring_intake_unavailable")

    def test_python_module_entrypoint_is_local_and_fail_open_for_missing_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "reports" / "runtime"
            command = [
                sys.executable,
                "-m",
                "advisor.runtime_scoring_intake",
                "--output-dir",
                str(output_dir),
                "--source-report-sha",
                SOURCE_SHA,
                "--main-root",
                str(root / "missing-main"),
                "--main-run-id",
                "1",
                "--main-artifact-name",
                "financial-advisor-main-1",
                "--main-expected-source-sha",
                SOURCE_SHA,
                "--main-expected-report-date",
                REPORT_DATE,
                "--main-expected-schedule",
                "main",
                "--close-root",
                str(root / "missing-close"),
                "--close-run-id",
                "2",
                "--close-artifact-name",
                "financial-advisor-close-2",
                "--close-expected-source-sha",
                SOURCE_SHA,
                "--close-expected-report-date",
                REPORT_DATE,
                "--close-expected-schedule",
                "close",
            ]
            completed = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stderr, "")
            self.assertIn("runtime_scoring_intake=ok main=missing close=missing", completed.stdout)
            manifest, _ = _read_manifest(output_dir)
            self.assertEqual(manifest["entries"]["main"]["intake_status"], "missing")
            self.assertEqual(manifest["entries"]["close"]["intake_status"], "missing")


if __name__ == "__main__":
    unittest.main()
