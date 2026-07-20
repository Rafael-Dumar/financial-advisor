from __future__ import annotations

import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/phase3"
REPORT = ROOT / ".tmp/nightly-review/2026-07-15/run-29429941131-main/history/2026-07-15-main.md"


class Phase3A2ForensicsTests(unittest.TestCase):
    def test_shadowed_cap_is_linked_to_later_decision_override(self):
        from scripts.phase3a2_forensics import detect_shadowed
        events = [
            {"trace_event_id": "e1", "sequence": 1, "axis": "decision", "changed": True,
             "value_before": "watch_buy", "value_after": "technical_unvalidated", "rule_id": "cap.initial"},
            {"trace_event_id": "e2", "sequence": 2, "axis": "decision", "changed": True,
             "value_before": "technical_unvalidated", "value_after": "avoid", "rule_id": "override.minimum_market_cap"},
        ]
        result = detect_shadowed(events)
        self.assertEqual(result[0]["later_shadowed_by"], "e2")
        self.assertEqual(result[0]["shadow_status"], "fully_shadowed")

    def test_no_op_before_override_remains_no_op(self):
        from scripts.phase3a2_forensics import detect_shadowed
        events = [
            {"trace_event_id": "e1", "sequence": 1, "axis": "decision", "changed": False,
             "value_before": "watch_buy", "value_after": "watch_buy", "rule_id": "cap.initial"},
            {"trace_event_id": "e2", "sequence": 2, "axis": "decision", "changed": True,
             "value_before": "watch_buy", "value_after": "avoid", "rule_id": "override"},
        ]
        result = detect_shadowed(events)
        self.assertIsNone(result[0]["later_shadowed_by"])
        self.assertEqual(result[0]["shadow_status"], "not_shadowed")

    def test_different_axes_are_not_shadowed(self):
        from scripts.phase3a2_forensics import detect_shadowed
        events = [
            {"trace_event_id": "e1", "sequence": 1, "axis": "confidence", "changed": True,
             "value_before": 55, "value_after": 45, "rule_id": "cap.confidence"},
            {"trace_event_id": "e2", "sequence": 2, "axis": "decision", "changed": True,
             "value_before": "watch_buy", "value_after": "avoid", "rule_id": "override"},
        ]
        result = detect_shadowed(events)
        self.assertIsNone(result[0]["later_shadowed_by"])

    def test_shadow_detector_orders_events_by_sequence(self):
        from scripts.phase3a2_forensics import detect_shadowed
        events = [
            {"trace_event_id": "e2", "sequence": 2, "axis": "decision", "changed": True, "value_before": "technical_unvalidated", "value_after": "avoid"},
            {"trace_event_id": "e1", "sequence": 1, "axis": "decision", "changed": True, "value_before": "watch_buy", "value_after": "technical_unvalidated"},
        ]
        result = detect_shadowed(events)
        self.assertEqual([event["trace_event_id"] for event in result], ["e1", "e2"])
        self.assertEqual(result[0]["later_shadowed_by"], "e2")

    def test_news_statuses_are_kept_by_scope(self):
        from scripts.phase3a2_forensics import parse_news_status
        text = (FIXTURES / "semantic_news_real.md").read_text(encoding="utf-8")
        result = parse_news_status(text, "HIMS", "fixture.md")
        self.assertEqual(result["decision_status"]["value"], "collected")
        self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable")
        self.assertIn("sec_8k", result["collection_attempt_status"]["value"])
        self.assertTrue(result["collection_attempt_status"]["marker_evidence"])
        self.assertTrue(result["conflict"])
        self.assertEqual(result["conflict_type"], "decision_vs_capability")

    def test_news_scope_survives_section_and_line_reordering(self):
        from scripts.phase3a2_forensics import parse_news_status
        original = (FIXTURES / "semantic_news_real.md").read_text(encoding="utf-8")
        reordered_sections = "## HIMS\n- Ativo: `HIMS`\n- Data source: yahoo\n- provider: `yahoo`\n- guidance_status: `not_implemented`\n- quote_status: `unsupported_by_plan`\n- news_status: `temporarily_unavailable`\n- sec_filings_status: `available`\n\n- decision_label: `avoid`\n- Swing Trade Score: 69\n- News/catalyst summary: sec_8k status=confirmed\n- news_status: `collected`"
        reordered_lines = "## HIMS\n- Ativo: `HIMS`\n- decision_label: `avoid`\n- News/catalyst summary: sec_8k status=confirmed\n- news_status: `collected`\n\n- provider: `yahoo`\n- Data source: yahoo\n- quote_status: `unsupported_by_plan`\n- sec_filings_status: `available`\n- guidance_status: `not_implemented`\n- news_status: `temporarily_unavailable`"
        for text in (original, reordered_sections, reordered_lines):
            result = parse_news_status(text, "HIMS", "fixture.md")
            self.assertEqual(result["decision_status"]["value"], "collected")
            self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable")

    def test_unrelated_news_status_does_not_change_asset_scopes(self):
        from scripts.phase3a2_forensics import parse_news_status
        text = (FIXTURES / "semantic_news_real.md").read_text(encoding="utf-8")
        text += "\n## OTHER\n### capability\n- news_status: `broken`\n"
        result = parse_news_status(text, "HIMS", "fixture.md")
        self.assertEqual(result["decision_status"]["value"], "collected")
        self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable")

    def test_same_asset_unrelated_block_isolated_from_capability(self):
        from scripts.phase3a2_forensics import parse_news_status
        text = (FIXTURES / "semantic_news_unrelated.md").read_text(encoding="utf-8")
        result = parse_news_status(text, "HIMS", "fixture.md")
        self.assertEqual(result["decision_status"]["value"], "collected")
        self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable")
        self.assertEqual(result["conflict_type"], "decision_vs_capability")
        blocks = result.get("semantic_blocks", [])
        self.assertTrue(any(block["type"] == "unclassified" and "unrelated_status" in "\n".join(block["raw_lines"]) for block in blocks))

    def test_news_artifact_records_semantic_segmentation_strategy(self):
        import json
        data = json.loads((ROOT / "reports/audit/phase3-news-status-reconciliation.json").read_text(encoding="utf-8"))
        self.assertEqual(data["parser_strategy"], "semantic_block_segmentation_and_classification")
        self.assertEqual(data["capability_signature_strategy"], "required_semantic_marker_groups")
        self.assertTrue(data["partial_capability_blocks_ignored"])
        self.assertEqual(len(data["capability_signature_matrix"]), 11)
        self.assertFalse(data["positional_fallback_used"])
        self.assertTrue(data["unclassified_blocks_ignored"])
        hims = next(row for row in data["assets"] if row["symbol"] == "HIMS")
        self.assertTrue(any(block["type"] == "unclassified" and not block["used_for_scope"] for block in hims["semantic_blocks"]))
        self.assertTrue(any(block["type"] == "collection" and block["used_for_scope"] for block in hims["semantic_blocks"]))

    def test_unrelated_block_before_between_and_after_is_ignored(self):
        from scripts.phase3a2_forensics import parse_news_status
        decision = "- decision_label: `avoid`\n- Swing Trade Score: 69\n- News/catalyst summary: sec_8k status=confirmed\n- news_status: `collected`"
        capability = "- Data source: yahoo\n- provider: `yahoo`\n- quote_status: `unsupported_by_plan`\n- guidance_status: `not_implemented`\n- news_status: `temporarily_unavailable`"
        unrelated = "- Entrada ideal: 36.97\n- news_status: `ignored`"
        for body in (f"{unrelated}\n{decision}\n{capability}", f"{decision}\n{unrelated}\n{capability}", f"{decision}\n{capability}\n{unrelated}"):
            text = f"## HIMS\n- Ativo: `HIMS`\n{body}\n"
            result = parse_news_status(text, "HIMS", "fixture.md")
            self.assertEqual(result["decision_status"]["value"], "collected")
            self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable")

    def test_multiple_unclassified_blocks_do_not_change_scopes(self):
        from scripts.phase3a2_forensics import parse_news_status
        decision = "- decision_label: `avoid`\n- Swing Trade Score: 69\n- News/catalyst summary: sec_8k status=confirmed\n- news_status: `collected`"
        capability = "- Data source: yahoo\n- provider: `yahoo`\n- quote_status: `unsupported_by_plan`\n- guidance_status: `not_implemented`\n- news_status: `temporarily_unavailable`"
        unrelated = "\n".join(f"- Entrada ideal: {index}.00\n- news_status: `ignored_{index}`" for index in range(1, 6))
        result = parse_news_status(f"## HIMS\n- Ativo: `HIMS`\n{decision}\n{unrelated}\n{capability}\n", "HIMS", "fixture.md")
        self.assertEqual(result["decision_status"]["value"], "collected")
        self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable")

    def test_two_valid_capability_blocks_are_ambiguous_but_unclassified_is_not(self):
        from scripts.phase3a2_forensics import parse_news_status
        text = """## HIMS
- Ativo: `HIMS`
- decision_label: `avoid`
- News/catalyst summary: sec_8k status=confirmed
- news_status: `collected`
- Data source: yahoo
- provider: `yahoo`
- quote_status: `unsupported_by_plan`
- news_status: `temporarily_unavailable`
- Entrada ideal: 36.97
- news_status: `ignored`
- Data source: yahoo
- provider: `yahoo`
- quote_status: `available`
- news_status: `collected`
"""
        result = parse_news_status(text, "HIMS", "fixture.md")
        self.assertIsNone(result["capability_status"]["value"])
        self.assertEqual(result["capability_status"]["reason"], "semantic_scope_ambiguous")
        self.assertGreaterEqual(len(result["capability_status"]["candidates"]["capability"]), 2)

    def test_partial_unrelated_markers_are_not_promoted_to_capability(self):
        from scripts.phase3a2_forensics import parse_news_status
        text = """## HIMS
- Ativo: `HIMS`
- decision_label: `avoid`
- News/catalyst summary: sec_8k status=confirmed
- news_status: `collected`
- Entrada ideal: 36.97
- provider: `fake`
- news_status: `ignored`
- Data source: yahoo
- provider: `yahoo`
- quote_status: `unsupported_by_plan`
- news_status: `temporarily_unavailable`
"""
        result = parse_news_status(text, "HIMS", "fixture.md")
        self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable")
        self.assertFalse(any(block["type"] == "capability" and block["start_line"] == 6 for block in result.get("semantic_blocks", [])))

    def test_each_partial_capability_marker_is_unclassified(self):
        from scripts.phase3a2_forensics import parse_news_status
        markers = ["provider", "quote_status", "guidance_status", "macro_status", "sec_filings_status", "sector_benchmark_status"]
        for marker in markers:
            with self.subTest(marker=marker):
                partial = f"- Entrada ideal: 36.97\n- {marker}: `partial`\n- news_status: `partial_status`"
                valid = "- Data source: yahoo\n- provider: `yahoo`\n- guidance_status: `not_implemented`\n- news_status: `temporarily_unavailable`"
                text = f"## HIMS\n- Ativo: `HIMS`\n- decision_label: `avoid`\n- News/catalyst summary: sec_8k status=confirmed\n- news_status: `collected`\n{partial}\n{valid}\n"
                result = parse_news_status(text, "HIMS", "fixture.md")
                self.assertEqual(result["decision_status"]["value"], "collected")
                self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable")
                self.assertFalse(result["conflict"] is False and result["capability_status"].get("reason") == "semantic_scope_ambiguous")
                partial_blocks = [block for block in result["semantic_blocks"] if block["type"] == "unclassified" and "capability" in block.get("partial_candidate_types", [])]
                self.assertTrue(partial_blocks)
                self.assertTrue(all(not block.get("used_for_scope", False) for block in partial_blocks))

    def test_insufficient_capability_combinations_are_unclassified(self):
        from scripts.phase3a2_forensics import parse_news_status
        combinations = [("provider", "quote_status"), ("guidance_status", "macro_status"), ("sec_filings_status", "sector_benchmark_status"), ("quote_status", "guidance_status")]
        for first, second in combinations:
            with self.subTest(first=first, second=second):
                partial = f"- Entrada ideal: 36.97\n- {first}: `partial`\n- {second}: `partial`\n- news_status: `partial_status`"
                valid = "- Data source: yahoo\n- provider: `yahoo`\n- guidance_status: `not_implemented`\n- news_status: `temporarily_unavailable`"
                result = parse_news_status(f"## HIMS\n- Ativo: `HIMS`\n- decision_label: `avoid`\n- News/catalyst summary: sec_8k status=confirmed\n- news_status: `collected`\n{partial}\n{valid}\n", "HIMS", "fixture.md")
                self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable")
                self.assertFalse(result["capability_status"].get("reason") == "semantic_scope_ambiguous")

    def test_provider_plus_news_without_context_is_unclassified(self):
        from scripts.phase3a2_forensics import parse_news_status
        text = """## HIMS
- Ativo: `HIMS`
- decision_label: `avoid`
- News/catalyst summary: sec_8k status=confirmed
- news_status: `collected`
- Entrada ideal: 36.97
- provider: `partial`
- news_status: `partial_status`
- Data source: yahoo
- provider: `yahoo`
- guidance_status: `not_implemented`
- news_status: `temporarily_unavailable`
"""
        result = parse_news_status(text, "HIMS", "fixture.md")
        self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable")
        self.assertFalse(result["capability_status"].get("reason") == "semantic_scope_ambiguous")

    def test_capability_signature_requires_positive_groups(self):
        from scripts.phase3a2_forensics import is_complete_capability_block
        complete, missing = is_complete_capability_block(
            [{"name": name} for name in ("Data source", "provider", "guidance_status")],
            {"Data source", "provider", "guidance_status", "news_status"},
        )
        self.assertTrue(complete)
        self.assertEqual(missing, [])
        incomplete, missing = is_complete_capability_block(
            [{"name": name} for name in ("quote_status", "guidance_status")],
            {"quote_status", "guidance_status", "news_status"},
        )
        self.assertFalse(incomplete)
        self.assertIn("provider_identity_group", missing)

    def test_partial_blocks_do_not_outvote_one_complete_capability(self):
        from scripts.phase3a2_forensics import parse_news_status
        partials = "\n".join(f"- Entrada ideal: {i}.00\n- {marker}: `partial`\n- news_status: `partial_{i}`" for i, marker in enumerate(("provider", "quote_status", "guidance_status", "macro_status", "sec_filings_status"), 1))
        valid = "- Data source: yahoo\n- provider: `yahoo`\n- quote_status: `unsupported_by_plan`\n- guidance_status: `not_implemented`\n- news_status: `temporarily_unavailable`"
        result = parse_news_status(f"## HIMS\n- Ativo: `HIMS`\n- decision_label: `avoid`\n- News/catalyst summary: sec_8k status=confirmed\n- news_status: `collected`\n{partials}\n{valid}\n", "HIMS", "fixture.md")
        self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable")
        self.assertFalse(result["capability_status"].get("reason") == "semantic_scope_ambiguous")

    def test_news_without_semantic_marker_is_unavailable(self):
        from scripts.phase3a2_forensics import parse_news_status
        text = "## HIMS\n- Ativo: `HIMS`\n- news_status: `collected`\n- quote_status: `available`\n"
        result = parse_news_status(text, "HIMS", "fixture.md")
        self.assertIsNone(result["decision_status"]["value"])
        self.assertEqual(result["decision_status"]["reason"], "semantic_scope_not_identifiable")

    def test_duplicate_status_in_scope_is_ambiguous(self):
        from scripts.phase3a2_forensics import parse_news_status
        text = "## HIMS\n- Ativo: `HIMS`\n- decision_label: `avoid`\n- Swing Trade Score: 69\n- news_status: `collected`\n- news_status: `not_verified`\n- quote_status: `unsupported_by_plan`\n"
        result = parse_news_status(text, "HIMS", "fixture.md")
        self.assertIsNone(result["decision_status"]["value"])
        self.assertEqual(result["decision_status"]["reason"], "semantic_scope_ambiguous")

    def test_multiple_semantic_blocks_for_same_scope_are_ambiguous(self):
        from scripts.phase3a2_forensics import parse_news_status
        text = """## HIMS
- Ativo: `HIMS`
- decision_label: `avoid`
- Investment Quality Score: 38
- Swing Trade Score: 69
- news_status: `collected`
- quote_status: `unsupported_by_plan`
- guidance_status: `not_implemented`
- news_status: `temporarily_unavailable`
- decision_label: `avoid`
- Investment Quality Score: 38
- Swing Trade Score: 69
- news_status: `not_verified`
"""
        result = parse_news_status(text, "HIMS", "fixture.md")
        self.assertIsNone(result["decision_status"]["value"])
        self.assertEqual(result["decision_status"]["reason"], "semantic_scope_ambiguous")

    def test_semantic_markers_survive_neutral_line_insertion(self):
        from scripts.phase3a2_forensics import parse_news_status
        original = (FIXTURES / "semantic_news_real.md").read_text(encoding="utf-8")
        for count in (1, 4, 10, 20):
            neutral = "\n".join(f"- neutral_field_{index}: unchanged" for index in range(count))
            text = original.replace("- news_status: `collected`", neutral + "\n- news_status: `collected`", 1)
            text = text.replace("- news_status: `temporarily_unavailable`", neutral + "\n- news_status: `temporarily_unavailable`", 1)
            result = parse_news_status(text, "HIMS", "fixture.md")
            self.assertEqual(result["decision_status"]["value"], "collected")
            self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable")

    def test_crypto_flow_schema_is_explicit_for_all_four_crypto_assets(self):
        import json
        data = json.loads((ROOT / "reports/audit/phase3-baseline-v3.json").read_text(encoding="utf-8"))
        names = {"funding", "open_interest_current", "open_interest_change", "cvd", "premium", "liquidations"}
        for asset in data["assets"]:
            if asset["asset_type"] == "crypto":
                self.assertEqual(set(asset["crypto_flow"]), names)
                self.assertTrue(all(item["value"] is None and item["provenance"] == "unavailable" and item["source"] is None for item in asset["crypto_flow"].values()))

    def test_news_locators_are_one_based_and_point_to_raw_field(self):
        import json
        lines = REPORT.read_text(encoding="utf-8").splitlines()
        data = json.loads((ROOT / "reports/audit/phase3-news-status-reconciliation.json").read_text(encoding="utf-8"))
        for symbol in ("HIMS", "AMD", "MSFT"):
            row = next(item for item in data["assets"] if item["symbol"] == symbol)
            for key in ("decision_status", "capability_status"):
                status = row[key]
                line = status["source"]["line"]
                raw = status["raw_value"]
                self.assertIn("news_status:", lines[line - 1])
                self.assertIn(str(raw), lines[line - 1])
            collection = row["collection_attempt_status"]
            self.assertIn("News/catalyst summary:", lines[collection["source"]["line"] - 1])

    def test_sample_quality_raw_contract_is_independent_for_five_symbols(self):
        expected = {"AMD": "sample_quality_amd.md", "ETH": "sample_quality_eth.md", "HIMS": "sample_quality_hims.md", "MSFT": "sample_quality_msft.md", "USAR": "sample_quality_usar.md"}
        for symbol, filename in expected.items():
            lines = (FIXTURES / filename).read_text(encoding="utf-8").splitlines()
            line_number = next(index for index, line in enumerate(lines, 1) if "Qualidade da amostra:" in line)
            self.assertEqual(lines[line_number - 1], "- Qualidade da amostra: baixa")
            self.assertEqual("baixa", lines[line_number - 1].split(":", 1)[1].strip())
            self.assertEqual("low", {"baixa": "low"}["baixa"])

    def test_baseline_sample_quality_is_a_provenance_wrapper(self):
        import json
        data = json.loads((ROOT / "reports/audit/phase3-baseline-v3.json").read_text(encoding="utf-8"))
        for asset in data["assets"]:
            sample = asset["backtest"]["sample_quality"]
            self.assertIsInstance(sample, dict)
            self.assertEqual(sample["provenance"], "observed")
            self.assertEqual(sample["raw_value"], "baixa")
            self.assertEqual(sample["value"], "low")

    def test_original_occurrence_source_is_explicitly_unavailable_when_missing(self):
        from scripts.phase3a2_forensics import recover_original_occurrences
        result = recover_original_occurrences(ROOT)
        self.assertEqual(result["original_occurrence_set_status"], "unavailable")
        self.assertIsNone(result["recovered_occurrence_count"])
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["confirmed_duplicates"], 0)
        self.assertEqual(result["occurrences"], [])
        self.assertNotIn("previous_suspected_count", result)

    def test_unavailable_replay_is_null_and_does_not_claim_parity(self):
        from scripts.phase3a2_forensics import replay_capability
        result = replay_capability(ROOT, REPORT, "6d0c1f705032606d6b449f79b8c151b941e1c037")
        self.assertEqual(result["summary"]["parity_complete_count"], 0)
        self.assertTrue(result["assets"])
        for asset in result["assets"]:
            self.assertFalse(asset["original_replay"]["parity"])
            self.assertIsNone(asset["counterfactuals"][0]["counterfactual_decision"])
            self.assertEqual(asset["counterfactuals"][0]["reason"], "exact_classify_asset_inputs_not_recoverable")

    def test_rule_catalog_points_to_real_branches(self):
        from scripts.phase3a2_forensics import rule_catalog
        catalog = rule_catalog(ROOT / "advisor/scoring.py")
        self.assertTrue(catalog)
        source = (ROOT / "advisor/scoring.py").read_text(encoding="utf-8")
        for rule in catalog:
            self.assertIn(rule["source_code_locator"]["branch_signature"].split("(")[0], source)
        self.assertFalse(any("technical_unvalidated_predicate:backtest_component" in r["rule_id"] for r in catalog))

    def test_occurrence_mutations_change_source_derived_count(self):
        from scripts.phase3a2_forensics import occurrence_ids_from_source
        source = [
            {"symbol": "ETH", "original_rule_or_reason": "r1", "original_decision_before": "a", "original_decision_after": "b", "original_source_locator": "x", "original_payload": {"n": 1}},
            {"symbol": "SOL", "original_rule_or_reason": "r2", "original_decision_before": "a", "original_decision_after": "b", "original_source_locator": "y", "original_payload": {"n": 2}},
        ]
        self.assertEqual(len(occurrence_ids_from_source(source)), 2)
        self.assertEqual(len(occurrence_ids_from_source(source[:1])), 1)
        self.assertEqual(len(occurrence_ids_from_source(source + [source[0]])), 3)

    def test_different_evidence_keys_are_independent_not_duplicates(self):
        from scripts.phase3a2_forensics import classify_occurrence
        occurrence = {"linked_trace_event_ids": ["e1"]}
        events = [{"trace_event_id": "e1", "changed": True, "evidence_key": "market:regime", "axis": "decision"}]
        self.assertEqual(classify_occurrence(occurrence, events, "data:missing"), "independent_gate")

    def test_same_evidence_key_with_two_effects_is_duplicate_candidate(self):
        from scripts.phase3a2_forensics import classify_occurrence
        occurrence = {"linked_trace_event_ids": ["e1", "e2"]}
        events = [
            {"trace_event_id": "e1", "changed": True, "evidence_key": "data:missing", "axis": "decision"},
            {"trace_event_id": "e2", "changed": True, "evidence_key": "data:missing", "axis": "decision"},
        ]
        self.assertEqual(classify_occurrence(occurrence, events), "confirmed_duplicate_penalty")

    def test_unlinked_occurrence_is_unable_to_determine(self):
        from scripts.phase3a2_forensics import classify_occurrence
        self.assertEqual(classify_occurrence({"linked_trace_event_ids": []}, []), "unable_to_determine")

    def test_evaluated_unchanged_event_is_no_op(self):
        from scripts.phase3a2_forensics import classify_occurrence
        occurrence = {"linked_trace_event_ids": ["e1"]}
        events = [{"trace_event_id": "e1", "changed": False, "evidence_key": "data:missing", "axis": "decision"}]
        self.assertEqual(classify_occurrence(occurrence, events), "no_op")

    def test_hims_trace_is_derived_from_raw_source(self):
        from scripts.phase3a2_forensics import derive_hims_trace_from_raw
        actual = derive_hims_trace_from_raw((FIXTURES / "hims_source_raw.md").read_text(encoding="utf-8"))
        self.assertEqual(actual["base_decision"], "watch_buy")
        self.assertEqual(actual["severity_intermediate_decision"], "technical_unvalidated")
        self.assertEqual(actual["override_rule_id"], "classify_asset.below_minimum_market_cap_override")
        self.assertEqual(actual["effect_type"], "explicit_override")
        self.assertEqual(actual["override_decision_before"], "technical_unvalidated")
        self.assertEqual(actual["override_decision_after"], "avoid")
        self.assertEqual(actual["final_decision"], "avoid")
        self.assertEqual(actual["observed_final_decision"], "avoid")
        self.assertFalse(actual["replay_divergence"])
        self.assertEqual(actual["source_code_locator"]["branch_signature"], 'if "below_minimum_market_cap" in alerts')

    def test_hims_fixture_is_exact_raw_report_excerpt(self):
        fixture_lines = (FIXTURES / "hims_source_raw.md").read_text(encoding="utf-8").splitlines()
        report_lines = REPORT.read_text(encoding="utf-8").splitlines()
        self.assertEqual(fixture_lines, report_lines[838:847])
        raw = "\n".join(fixture_lines)
        for forbidden in ("base_decision", "decision_before", "decision_after", "explicit_override", "replay_divergence", "market_cap: below_minimum_market_cap"):
            self.assertNotIn(forbidden, raw)

if __name__ == "__main__":
    unittest.main()
