from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from advisor.analyst_review import (
    MainReviewContext,
    RUNTIME_MANIFEST_MAX_BYTES,
    RuntimeAuditEntry,
    RuntimeAuditSummary,
    generate_analyst_final_review,
    load_runtime_audit,
    main as analyst_review_main,
    main_blocks_operation,
    parse_review_package,
)


def _nightly(*, primary_grade: str = "decision_grade", artifact_valid: bool = True) -> str:
    return f"""# Nightly qualitative review input

- brt_date: `2026-07-14`
- main_run_id: `10`
- close_run_id: `20`
- main_head_sha: `8497c24`
- close_head_sha: `8497c24`
- main_event: `schedule`
- close_event: `schedule`
- main_generated_at: `2026-07-14T15:46:49Z`
- close_generated_at: `2026-07-14T21:13:56Z`
- artifact_selection_status: `valid_current_day`
- artifact_valid: `{str(artifact_valid).lower()}`

## Main summary

- report_type: `main`
- Data mode: `live`
- primary_report_grade: `{primary_grade}`
- overall_report_grade: `diagnostic_not_decision_grade`
- primary_market_session: `regular`
- discovery_coverage_grade: `degraded`
- stale_asset_count_primary: 0
- provider_rate_limit_status: `ok`
- blocking_reasons: `nenhum`

## Close summary

- report_type: `close`
- primary_report_grade: `close_decision_grade`
"""


def _asset(
    ticker: str,
    decision: str,
    *,
    origin: str = "primary_watchlist",
    report_type: str = "main",
    extra: str = "",
) -> str:
    session = "regular" if report_type == "main" else "after_hours"
    return f"""# Investment and Swing Trade Advisor

- report_type: `{report_type}`

## {ticker}

- Ativo: `{ticker}`
- Tipo: `stock`
- universe_origin: `{origin}`
- decision_label: `{decision}`
- Decisao: `{decision}`
- data_quality: `ok`
- missing_data_severity: `low`
- Investment Quality Score: 90
- Swing Trade Score: 90
- market_session: `{session}`
- is_stale: `no`
- provider: `fmp`
- event_check_status: `not_implemented`
- news_status: `collected`
- guidance_status: `not_implemented`
- Entrada ideal: 100.00
- Stop/invalidation: 95.00
- Tamanho maximo da posicao: 10 unidades / 1000.00
- Dados ausentes ou limitacoes: {extra or 'nenhum'}
"""


def _runtime_entry(
    schedule: str,
    *,
    intake_status: str = "validated",
    validation_status: str = "valid",
    artifact_status: str | None = "complete",
) -> dict[str, object]:
    return {
        "intake_status": intake_status,
        "validation_status": validation_status,
        "artifact_status": artifact_status,
        "expected_schedule": schedule,
        "files": [
            {
                "path": f"runtime/{schedule}/scoring-runtime-trace.json.gz",
                "size_bytes": 1,
                "sha256": "a" * 64,
            }
        ],
    }


def _runtime_manifest(
    *,
    source_report_sha: str = "8497c24",
    main: dict[str, object] | None = None,
    close: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "manifest_schema_version": "1.0",
        "source_report_sha": source_report_sha,
        "entries": {
            "main": main or _runtime_entry("main"),
            "close": close or _runtime_entry("close"),
        },
    }


def _write_runtime_manifest(root: Path, manifest: dict[str, object]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "nightly-runtime-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _runtime_audit_section(review: str) -> str:
    return review.split("\n## Runtime scoring audit\n", 1)[1]


def _authority_fingerprint(review: str) -> str:
    return review.split("\n## Runtime scoring audit\n", 1)[0]


def _telegram_summary(review: str) -> str:
    summary = review.split("\n## Telegram summary\n\n", 1)[1]
    return summary.split("\n## ", 1)[0]


class Phase25FinalReviewTests(unittest.TestCase):
    def test_runtime_audit_model_is_sanitized_and_structured(self) -> None:
        self.assertEqual(
            set(RuntimeAuditEntry.__dataclass_fields__),
            {"trace_status", "intake_status", "artifact_status"},
        )
        self.assertEqual(
            set(RuntimeAuditSummary.__dataclass_fields__),
            {"runtime_manifest_status", "runtime_audit_status", "main", "close"},
        )
        with tempfile.TemporaryDirectory() as directory:
            summary = load_runtime_audit(
                _write_runtime_manifest(Path(directory), _runtime_manifest()),
                expected_source_report_sha="8497c24",
            )

        self.assertIsInstance(summary, RuntimeAuditSummary)
        self.assertEqual(summary.main_trace_status, "complete")
        self.assertEqual(summary.close_trace_status, "complete")
        self.assertNotIn("events", vars(summary.main))
        self.assertNotIn("invocations", vars(summary.main))
        self.assertNotIn("rule_ids", vars(summary.main))

    def test_valid_runtime_manifest_is_rendered_as_observability_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = _write_runtime_manifest(Path(directory), _runtime_manifest())

            review = generate_analyst_final_review(
                _nightly(),
                extra_markdowns=[_asset("AMD", "wait")],
                runtime_manifest_path=manifest_path,
            )

        self.assertIn("## Runtime scoring audit", review)
        self.assertIn("runtime_audit_role: `observability_only`", review)
        self.assertIn("runtime_manifest_status: `loaded`", review)
        self.assertIn("runtime_audit_status: `complete`", review)
        self.assertIn("main_trace_status: `complete`", review)
        self.assertIn("close_trace_status: `complete`", review)
        self.assertIn("main_intake_status: `validated`", review)
        self.assertIn("close_intake_status: `validated`", review)
        self.assertIn("main_artifact_status: `complete`", review)
        self.assertIn("close_artifact_status: `complete`", review)
        self.assertIn(
            "Runtime audit is observability evidence only; it does not authorize, reject, rank or resize trades.",
            review,
        )

    def test_runtime_status_mapping_is_loaded_but_never_decision_authority(self) -> None:
        cases = (
            ("partial", "validated", "valid", "partial", "partial", "partial", "degraded"),
            ("failed", "validated", "valid", "failed", "failed", "failed", "degraded"),
            ("missing", "missing", "not_found", None, "missing", "unavailable", "degraded"),
            ("invalid", "invalid", "rejected", None, "invalid", "unavailable", "degraded"),
            ("unavailable", "unavailable", "error", None, "unavailable", "unavailable", "degraded"),
        )

        for (
            label,
            intake_status,
            validation_status,
            artifact_status,
            expected_trace,
            expected_artifact,
            expected_audit,
        ) in cases:
            with self.subTest(intake_status=label), tempfile.TemporaryDirectory() as directory:
                manifest_path = _write_runtime_manifest(
                    Path(directory),
                    _runtime_manifest(
                        main=_runtime_entry(
                            "main",
                            intake_status=intake_status,
                            validation_status=validation_status,
                            artifact_status=artifact_status,
                        )
                    ),
                )

                review = generate_analyst_final_review(
                    _nightly(),
                    extra_markdowns=[_asset("AMD", "wait")],
                    runtime_manifest_path=manifest_path,
                )

            audit = _runtime_audit_section(review)
            self.assertIn("runtime_manifest_status: `loaded`", audit)
            self.assertIn(f"runtime_audit_status: `{expected_audit}`", audit)
            self.assertIn(f"main_trace_status: `{expected_trace}`", audit)
            self.assertIn(f"main_artifact_status: `{expected_artifact}`", audit)
            self.assertIn("close_trace_status: `complete`", audit)
            self.assertIn("* no_trade", _authority_fingerprint(review))

    def test_missing_invalid_and_unavailable_manifest_are_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing-manifest.json"
            missing_review = generate_analyst_final_review(
                _nightly(),
                extra_markdowns=[_asset("AMD", "tradeable")],
                runtime_manifest_path=missing,
            )

            invalid = root / "invalid-manifest.json"
            invalid.write_text('{"manifest_schema_version":"1.0",', encoding="utf-8")
            invalid_review = generate_analyst_final_review(
                _nightly(),
                extra_markdowns=[_asset("AMD", "tradeable")],
                runtime_manifest_path=invalid,
            )

            schema_mismatch = _write_runtime_manifest(root, _runtime_manifest())
            schema_mismatch.write_text(
                json.dumps({**_runtime_manifest(), "manifest_schema_version": "9.9"}),
                encoding="utf-8",
            )
            schema_review = generate_analyst_final_review(
                _nightly(),
                extra_markdowns=[_asset("AMD", "tradeable")],
                runtime_manifest_path=schema_mismatch,
            )

            unavailable = root / "permission-manifest.json"
            unavailable.write_text(json.dumps(_runtime_manifest()), encoding="utf-8")
            original_open = Path.open

            def deny_manifest_open(path: Path, *args: object, **kwargs: object):
                if path == unavailable:
                    raise PermissionError("SECRET_PERMISSION_MESSAGE")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", new=deny_manifest_open):
                unavailable_review = generate_analyst_final_review(
                    _nightly(),
                    extra_markdowns=[_asset("AMD", "tradeable")],
                    runtime_manifest_path=unavailable,
                )

        for review, status in (
            (missing_review, "missing"),
            (invalid_review, "invalid"),
            (schema_review, "invalid"),
            (unavailable_review, "unavailable"),
        ):
            with self.subTest(status=status):
                audit = _runtime_audit_section(review)
                self.assertIn(f"runtime_manifest_status: `{status}`", audit)
                self.assertIn("runtime_audit_status: `unavailable`", audit)
                self.assertIn("main_trace_status: `unavailable`", audit)
                self.assertIn("close_trace_status: `unavailable`", audit)
                self.assertIn("* tradeable", _authority_fingerprint(review))
                self.assertIn("- tradeable_count: 1", _authority_fingerprint(review))
                self.assertNotIn("SECRET_PERMISSION_MESSAGE", review)

    def test_loader_rejects_duplicate_keys_nonfinite_values_and_invalid_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = _runtime_manifest()

            duplicate = root / "duplicate.json"
            duplicate.write_text(
                json.dumps(valid)[:-1] + ',"source_report_sha":"8497c24"}',
                encoding="utf-8",
            )
            duplicate_review = generate_analyst_final_review(
                _nightly(), runtime_manifest_path=duplicate
            )

            nonfinite = root / "nonfinite.json"
            nonfinite_payload = {**valid, "nonfinite": float("nan")}
            nonfinite.write_text(json.dumps(nonfinite_payload), encoding="utf-8")
            nonfinite_review = generate_analyst_final_review(
                _nightly(), runtime_manifest_path=nonfinite
            )

            impossible = root / "impossible.json"
            impossible.write_text(
                json.dumps(
                    _runtime_manifest(
                        main=_runtime_entry(
                            "main",
                            intake_status="validated",
                            validation_status="error",
                            artifact_status="complete",
                        )
                    )
                ),
                encoding="utf-8",
            )
            impossible_review = generate_analyst_final_review(
                _nightly(), runtime_manifest_path=impossible
            )

            wrong_sha = root / "wrong-sha.json"
            _write_runtime_manifest(root, _runtime_manifest(source_report_sha="different"))
            wrong_sha.write_text(
                json.dumps(_runtime_manifest(source_report_sha="different")), encoding="utf-8"
            )
            wrong_sha_review = generate_analyst_final_review(
                _nightly(), runtime_manifest_path=wrong_sha
            )

            wrong_schedule = root / "wrong-schedule.json"
            wrong_schedule_manifest = _runtime_manifest()
            wrong_schedule_manifest["entries"]["main"]["expected_schedule"] = "close"  # type: ignore[index]
            wrong_schedule.write_text(json.dumps(wrong_schedule_manifest), encoding="utf-8")
            wrong_schedule_review = generate_analyst_final_review(
                _nightly(), runtime_manifest_path=wrong_schedule
            )

        for review in (duplicate_review, nonfinite_review, impossible_review, wrong_sha_review, wrong_schedule_review):
            self.assertIn("runtime_manifest_status: `invalid`", _runtime_audit_section(review))
            self.assertNotIn("different", review)

    def test_loader_rejects_oversize_non_utf8_and_non_object_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversize = root / "oversize.json"
            oversize.write_bytes(b"{" + b"a" * RUNTIME_MANIFEST_MAX_BYTES + b"}")
            oversize_review = generate_analyst_final_review(
                _nightly(), runtime_manifest_path=oversize
            )

            non_utf8 = root / "non-utf8.json"
            non_utf8.write_bytes(b"\xff\xfe")
            non_utf8_review = generate_analyst_final_review(
                _nightly(), runtime_manifest_path=non_utf8
            )

            non_object = root / "non-object.json"
            non_object.write_text("[]", encoding="utf-8")
            non_object_review = generate_analyst_final_review(
                _nightly(), runtime_manifest_path=non_object
            )

        for review in (oversize_review, non_utf8_review, non_object_review):
            self.assertIn("runtime_manifest_status: `invalid`", _runtime_audit_section(review))

    def test_loader_does_not_coerce_enum_or_schedule_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_value = _runtime_manifest(
                main=_runtime_entry(
                    "main",
                    intake_status="VALIDATED",
                    validation_status="valid",
                    artifact_status="complete",
                )
            )
            bad_value["entries"]["close"]["expected_schedule"] = " close "  # type: ignore[index]
            path = _write_runtime_manifest(root, bad_value)
            review = generate_analyst_final_review(_nightly(), runtime_manifest_path=path)

        self.assertIn("runtime_manifest_status: `invalid`", _runtime_audit_section(review))

    def test_runtime_audit_never_reads_raw_trace_or_exposes_manifest_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace_path = root / "scoring-runtime-trace.json.gz"
            index_path = root / "scoring-runtime-trace.index.json"
            part_path = root / "scoring-runtime-trace.part-0001.json.gz"
            for path in (trace_path, index_path, part_path):
                path.write_text("SECRET_TRACE_PAYLOAD_MUST_NOT_BE_READ", encoding="utf-8")

            manifest = _runtime_manifest()
            manifest["secret_payload"] = "SECRET_TRACE_PAYLOAD_MUST_NOT_BE_READ"
            manifest["entries"]["main"]["files"] = [  # type: ignore[index]
                {"path": str(trace_path), "events": ["SECRET_TRACE_PAYLOAD_MUST_NOT_BE_READ"]}
            ]
            manifest_path = _write_runtime_manifest(root, manifest)
            original_open = Path.open

            def reject_trace_open(path: Path, *args: object, **kwargs: object):
                if "scoring-runtime-trace" in path.name:
                    raise AssertionError("raw runtime trace was opened")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", new=reject_trace_open):
                review = generate_analyst_final_review(
                    _nightly(),
                    extra_markdowns=[_asset("AMD", "wait")],
                    runtime_manifest_path=manifest_path,
                )

        audit = _runtime_audit_section(review)
        self.assertIn("runtime_manifest_status: `loaded`", audit)
        self.assertNotIn("SECRET_TRACE_PAYLOAD_MUST_NOT_BE_READ", review)
        for forbidden in (
            "scoring-runtime-trace",
            "logical_run_id",
            "github_run_id",
            "events",
            "invocations",
            "rule IDs",
            "serialized AssetDecision",
        ):
            self.assertNotIn(forbidden, audit)

    def test_runtime_variants_have_identical_authority_and_telegram_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete = _write_runtime_manifest(root / "complete", _runtime_manifest())
            partial = _write_runtime_manifest(
                root / "partial",
                _runtime_manifest(
                    main=_runtime_entry("main", validation_status="valid", artifact_status="partial")
                ),
            )
            failed = _write_runtime_manifest(
                root / "failed",
                _runtime_manifest(
                    main=_runtime_entry("main", validation_status="valid", artifact_status="failed")
                ),
            )
            close_failed = _write_runtime_manifest(
                root / "close-failed",
                _runtime_manifest(
                    close=_runtime_entry("close", validation_status="valid", artifact_status="failed")
                ),
            )
            missing = root / "missing.json"
            invalid = root / "invalid.json"
            invalid.write_text("not-json SECRET_INVALID_RUNTIME", encoding="utf-8")
            unavailable = root / "unavailable.json"
            unavailable.write_text(json.dumps(_runtime_manifest()), encoding="utf-8")

            paths = (complete, partial, failed, close_failed, missing, invalid, unavailable)
            reviews: list[str] = []
            for path in paths:
                if path == unavailable:
                    original_open = Path.open

                    def deny_unavailable(path_to_open: Path, *args: object, **kwargs: object):
                        if path_to_open == unavailable:
                            raise PermissionError("SECRET_UNAVAILABLE_RUNTIME")
                        return original_open(path_to_open, *args, **kwargs)

                    with patch.object(Path, "open", new=deny_unavailable):
                        reviews.append(
                            generate_analyst_final_review(
                                _nightly(),
                                extra_markdowns=[_asset("AMD", "tradeable"), _asset("AMD", "wait", report_type="close")],
                                runtime_manifest_path=path,
                            )
                        )
                else:
                    reviews.append(
                        generate_analyst_final_review(
                            _nightly(),
                            extra_markdowns=[_asset("AMD", "tradeable"), _asset("AMD", "wait", report_type="close")],
                            runtime_manifest_path=path,
                        )
                    )

        fingerprints = [_authority_fingerprint(review) for review in reviews]
        self.assertTrue(all(fingerprint == fingerprints[0] for fingerprint in fingerprints))
        telegrams = [_telegram_summary(review) for review in reviews]
        self.assertTrue(all(summary == telegrams[0] for summary in telegrams))
        self.assertNotIn("runtime", telegrams[0].lower())
        self.assertEqual(_telegram_summary(reviews[0]), _telegram_summary(reviews[2]))
        self.assertEqual(_telegram_summary(reviews[0]), _telegram_summary(reviews[4]))
        self.assertEqual(_telegram_summary(reviews[0]), _telegram_summary(reviews[5]))

    def test_runtime_complete_never_authorizes_or_repairs_report_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete = _write_runtime_manifest(root / "complete", _runtime_manifest())
            failed = _write_runtime_manifest(
                root / "failed",
                _runtime_manifest(
                    main=_runtime_entry("main", validation_status="valid", artifact_status="failed")
                ),
            )

            wait_review = generate_analyst_final_review(
                _nightly(), extra_markdowns=[_asset("AMD", "wait")], runtime_manifest_path=complete
            )
            failed_review = generate_analyst_final_review(
                _nightly(), extra_markdowns=[_asset("AMD", "tradeable")], runtime_manifest_path=failed
            )
            blocked_nightly = _nightly(primary_grade="diagnostic_not_decision_grade")
            blocked_review = generate_analyst_final_review(
                blocked_nightly, extra_markdowns=[_asset("AMD", "tradeable")], runtime_manifest_path=complete
            )

        self.assertIn("* no_trade", wait_review)
        self.assertIn("- tradeable_count: 0", wait_review)
        self.assertIn("* tradeable", failed_review)
        self.assertIn("- tradeable_count: 1", failed_review)
        self.assertIn("* no_trade", blocked_review)
        self.assertIn("main_primary_not_decision_grade", blocked_review)

    def test_cli_accepts_explicit_runtime_manifest_path_and_remains_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "nightly-review-input.md"
            output_path = root / "analyst-final-review.md"
            history_path = root / "history.md"
            manifest_path = _write_runtime_manifest(root, _runtime_manifest())
            input_path.write_text(_nightly(), encoding="utf-8")

            exit_code = analyst_review_main(
                [
                    "--input-path",
                    str(input_path),
                    "--output-path",
                    str(output_path),
                    "--history-path",
                    str(history_path),
                    "--runtime-manifest-path",
                    str(manifest_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertIn("runtime_manifest_status: `loaded`", output_path.read_text(encoding="utf-8"))

    def test_main_context_blocks_from_structured_fields_only(self) -> None:
        context = MainReviewContext(
            run_id="10",
            head_sha="8497c24",
            brt_date="2026-07-14",
            generated_at="2026-07-14T15:46:49Z",
            data_mode="live",
            primary_report_grade="decision_grade",
            overall_report_grade="diagnostic_not_decision_grade",
            primary_market_session="regular",
            discovery_coverage_grade="degraded",
            stale_asset_count_primary=0,
            provider_status="ok",
            artifact_valid=True,
            blocking_reasons=(),
        )

        self.assertFalse(main_blocks_operation(context))
        self.assertFalse(main_blocks_operation(context, close_markdown="blocked_or_diagnostic market_session: `closed`"))

    def test_substring_in_thesis_or_close_does_not_block_main(self) -> None:
        main = _asset("AMD", "wait", extra="thesis mentions blocked_or_diagnostic and not_collected")
        close = _asset("AMD", "avoid", report_type="close", extra="market_session: `closed`")

        review = generate_analyst_final_review(_nightly(), extra_markdowns=[main, close])

        self.assertIn("- main_primary_blocked: false", review)
        self.assertNotIn("main_primary_not_decision_grade", review)

    def test_main_and_close_are_not_last_wins(self) -> None:
        package = parse_review_package(
            _nightly(),
            extra_markdowns=[_asset("AMD", "technical_unvalidated"), _asset("AMD", "avoid", report_type="close")],
        )

        self.assertEqual(package.main_assets[0].source_decision, "technical_unvalidated")
        self.assertEqual(package.close_assets[0].source_decision, "avoid")
        self.assertIsNotNone(package.close_context)
        self.assertEqual(package.close_context.run_id, "20")
        review = generate_analyst_final_review(
            _nightly(),
            extra_markdowns=[_asset("AMD", "technical_unvalidated"), _asset("AMD", "avoid", report_type="close")],
        )
        self.assertIn("main_decision: `technical_unvalidated`", review)
        self.assertIn("close_decision: `avoid`", review)
        self.assertIn("decision_change: `changed_at_close`", review)
        self.assertIn("change_reason: `source_decision_changed_in_close`", review)

    def test_source_decision_is_preserved_and_not_implemented_is_field_specific(self) -> None:
        review = generate_analyst_final_review(_nightly(), extra_markdowns=[_asset("AMD", "wait")])

        self.assertIn("source_decision: `wait`", review)
        self.assertIn("review_status: `wait_from_main`", review)
        self.assertNotIn("source_decision: `research_only`", review)
        self.assertIn("legacy_label:", review)

    def test_tradeable_from_valid_main_reaches_final_review(self) -> None:
        review = generate_analyst_final_review(_nightly(), extra_markdowns=[_asset("AMD", "tradeable")])

        self.assertIn("* tradeable", review)
        self.assertIn("- tradeable_count: 1", review)
        self.assertIn("- tradeable_assets: `AMD`", review)
        self.assertIn("review_status: `tradeable_confirmed_from_main`", review)
        self.assertIn("decisao originada no scoring do main", review)
        self.assertIn("entry_from_main: `100.00`", review)
        self.assertIn("stop_invalidation_from_main: `95.00`", review)
        self.assertIn("sizing_from_main: `10 unidades / 1000.00`", review)
        self.assertNotIn("Nenhum ativo aprovado como tradeable", review)
        self.assertNotIn("Nao ha entrada aprovada", review)
        self.assertNotIn("Proximo passo: aguardar proximo main decision-grade", review)
        self.assertIn("Bloqueio para trade: nenhum", review)

    def test_overall_diagnostic_from_discovery_does_not_override_primary_tradeable(self) -> None:
        primary = _asset("AMD", "tradeable")
        discovery = _asset("AVAX", "blocked", origin="discovery", extra="empty_provider_response")

        review = generate_analyst_final_review(_nightly(), extra_markdowns=[primary + "\n" + discovery])

        self.assertIn("- primary_report_grade: `decision_grade`", review)
        self.assertIn("- overall_report_grade: `diagnostic_not_decision_grade`", review)
        self.assertIn("- discovery_coverage_grade: `degraded`", review)
        self.assertIn("* tradeable", review)
        self.assertIn("review_status: `tradeable_confirmed_from_main`", review)
        self.assertNotIn("Observacao sem entrada", review)

    def test_zero_tradeable_message_is_calculated(self) -> None:
        review = generate_analyst_final_review(_nightly(), extra_markdowns=[_asset("AMD", "wait")])

        self.assertIn("- tradeable_count: 0", review)
        self.assertIn("Nenhum tradeable no main selecionado", review)

    def test_missing_legacy_provenance_fails_closed_even_with_tradeable_text(self) -> None:
        legacy = """# Nightly qualitative review input

## Main summary
- Data mode: `live`
- report_grade: `decision_grade`
- market_session: `regular`
- provider_rate_limit_status: `ok`
"""
        review = generate_analyst_final_review(legacy, extra_markdowns=[_asset("AMD", "tradeable")])

        self.assertIn("- artifact_valid: false", review)
        self.assertIn("artifact_mismatch", review)
        self.assertIn("* no_trade", review)
        self.assertNotIn("review_status: `tradeable_confirmed_from_main`", review)

    def test_current_summary_without_raw_main_fails_closed(self) -> None:
        review = generate_analyst_final_review(_nightly())

        self.assertIn("- artifact_valid: false", review)
        self.assertIn("artifact_mismatch", review)
        self.assertIn("* no_trade", review)

    def test_discovery_is_separate_and_does_not_change_primary_decision(self) -> None:
        primary = _asset("AMD", "technical_unvalidated")
        discovery = _asset("AVAX", "blocked", origin="discovery", extra="empty_provider_response")

        review = generate_analyst_final_review(_nightly(), extra_markdowns=[primary + "\n" + discovery])

        self.assertIn("## Discovery coverage", review)
        self.assertIn("AVAX", review)
        self.assertIn("impact_on_primary_report: false", review)
        self.assertIn("collection_status: `empty_provider_response`", review)
        self.assertIn("provider: `fmp`", review)
        self.assertIn("- main_primary_blocked: false", review)

    def test_human_summary_answers_required_counts_and_close_change(self) -> None:
        review = generate_analyst_final_review(
            _nightly(),
            extra_markdowns=[
                _asset("AMD", "technical_unvalidated") + "\n" + _asset("AVAX", "blocked", origin="discovery"),
                _asset("AMD", "wait", report_type="close"),
            ],
        )

        self.assertIn("1. O main principal foi decision-grade? sim", review)
        self.assertIn("5. Quantos ativos eram technical_unvalidated? 1.", review)
        self.assertIn("9. Quais problemas estavam apenas no discovery? AVAX:blocked", review)
        self.assertIn("10. O que mudou no close? AMD.", review)

    def test_public_equity_false_is_honest_rule_based_review(self) -> None:
        review = generate_analyst_final_review(_nightly(), extra_markdowns=[_asset("AMD", "wait")])

        self.assertIn("# Rule-Based Final Review", review)
        self.assertIn("public_equity_executed: false", review)
        self.assertIn("nenhuma validacao externa/plugin foi executada", review)

    @patch("advisor.telegram_notify.notify_from_report")
    @patch("advisor.scoring.classify_asset")
    @patch("advisor.scoring.score_asset")
    def test_final_review_does_not_rescore_send_telegram_or_call_broker(
        self,
        score_asset_mock,
        classify_asset_mock,
        telegram_mock,
    ) -> None:
        review = generate_analyst_final_review(_nightly(), extra_markdowns=[_asset("AMD", "wait")])

        self.assertIn("source_decision: `wait`", review)
        score_asset_mock.assert_not_called()
        classify_asset_mock.assert_not_called()
        telegram_mock.assert_not_called()
        self.assertNotIn("broker_call", review)


if __name__ == "__main__":
    unittest.main()
