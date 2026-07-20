from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest


ROOT = Path.cwd()
FIX = ROOT / "tests/fixtures/phase3"


class MutationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        report_value = os.environ.get("PHASE3_REPORT")
        if report_value:
            from scripts.phase3a2_forensics import build_all, write_artifacts
            report = ROOT / report_value
            artifacts = build_all(ROOT, report, run_mutations=False)
            write_artifacts(ROOT, artifacts)

    def test_M1_sample_quality_provenance(self):
        expected_symbols = ("AMD", "ETH", "HIMS", "MSFT", "USAR")
        data = json.loads((ROOT / "reports/audit/phase3-baseline-v3.json").read_text(encoding="utf-8"))
        assets = {asset["symbol"]: asset for asset in data["assets"]}
        for symbol in expected_symbols:
            wrapper = assets[symbol]["backtest"]["sample_quality"]
            self.assertEqual(wrapper["raw_value"], "baixa", "M1_sample_quality_raw_value")
            self.assertEqual(wrapper["value"], "low", "M1_sample_quality_normalization")
            self.assertEqual(wrapper["provenance"], "observed", "M1_sample_quality_provenance")
            expected_source = os.environ.get("PHASE3_REPORT", wrapper["source"]["relative_path"])
            self.assertEqual(wrapper["source"]["relative_path"], expected_source.replace("\\", "/"), "M1_sample_quality_source")

    def test_M2_hims_override(self):
        from scripts.phase3a2_forensics import derive_hims_trace_from_raw
        expected = {
            "base_decision": "watch_buy",
            "severity_intermediate_decision": "technical_unvalidated",
            "override_rule_id": "classify_asset.below_minimum_market_cap_override",
            "override_decision_before": "technical_unvalidated",
            "override_decision_after": "avoid",
            "effect_type": "explicit_override",
            "observed_final_decision": "avoid",
            "final_decision": "avoid",
            "replay_divergence": False,
        }
        actual = derive_hims_trace_from_raw((FIX / "hims_source_raw.md").read_text(encoding="utf-8"))
        self.assertEqual({key: actual[key] for key in expected}, expected, "M2_hims_source_chain")
        self.assertEqual(actual["source_code_locator"]["branch_signature"], 'if "below_minimum_market_cap" in alerts', "M2_real_branch")

    def test_M3_news_scopes(self):
        from scripts.phase3a2_forensics import parse_news_status
        result = parse_news_status((FIX / "semantic_news_real.md").read_text(encoding="utf-8"), "HIMS", "fixture.md")
        self.assertEqual(result["decision_status"]["value"], "collected", "M3_decision_scope")
        self.assertEqual(result["capability_status"]["value"], "temporarily_unavailable", "M3_capability_scope")
        self.assertEqual(result["decision_status"]["provenance"], "observed", "M3_decision_provenance")
        self.assertEqual(result["capability_status"]["provenance"], "observed", "M3_capability_provenance")
        isolated = parse_news_status((FIX / "semantic_news_unrelated.md").read_text(encoding="utf-8"), "HIMS", "fixture.md")
        self.assertEqual(isolated["decision_status"]["value"], "collected", "M3_unrelated_decision_isolation")
        self.assertEqual(isolated["capability_status"]["value"], "temporarily_unavailable", "M3_unrelated_capability_isolation")
        partial = """## HIMS
- Ativo: `HIMS`
- decision_label: `avoid`
- News/catalyst summary: sec_8k status=confirmed
- news_status: `collected`
- Entrada ideal: 36.97
- quote_status: `partial`
- news_status: `partial_status`
- Data source: yahoo
- provider: `yahoo`
- guidance_status: `not_implemented`
- news_status: `temporarily_unavailable`
"""
        partial_result = parse_news_status(partial, "HIMS", "fixture.md")
        self.assertEqual(partial_result["capability_status"]["value"], "temporarily_unavailable", "M3_partial_capability_signature")

    def test_M4_synthetic_occurrence_rejected(self):
        data = json.loads((FIX / "occurrence_source_fixture.json").read_text(encoding="utf-8"))
        self.assertNotIn("synthetic", {item["original_occurrence_id"] for item in data["immutable_records"]}, "M4_fixture_identity")
        self.assertEqual(data["original_occurrence_set_status"], "unavailable", "M4_original_unavailable")

    def test_M5_removed_occurrence_rejected(self):
        data = json.loads((FIX / "occurrence_source_fixture.json").read_text(encoding="utf-8"))
        ids = {item["original_occurrence_id"] for item in data["immutable_records"]}
        self.assertTrue({"fixture-eth-1", "fixture-sol-1"}.issubset(ids), "M5_hash_identity")

    def test_M6_unavailable_win_rate(self):
        data = json.loads((FIX / "unavailable_contracts.json").read_text(encoding="utf-8"))
        self.assertIsNone(data["win_rate_2r"]["value"], "M6_null_win_rate")
        self.assertEqual(data["win_rate_2r"]["provenance"], "unavailable", "M6_unavailable_provenance")

    def test_M7_unavailable_counterfactual(self):
        data = json.loads((FIX / "unavailable_contracts.json").read_text(encoding="utf-8"))
        self.assertIsNone(data["counterfactual"]["counterfactual_decision"], "M7_null_counterfactual")
        self.assertEqual(data["counterfactual"]["reason"], "exact_classify_asset_inputs_not_recoverable", "M7_unavailable_reason")

    def test_M8_shadow_detector_contract(self):
        from scripts.phase3a2_forensics import detect_shadowed
        events = [
            {"trace_event_id": "e2", "sequence": 2, "axis": "decision", "changed": True, "value_before": "technical_unvalidated", "value_after": "avoid"},
            {"trace_event_id": "e1", "sequence": 1, "axis": "decision", "changed": True, "value_before": "watch_buy", "value_after": "technical_unvalidated"},
        ]
        result = detect_shadowed(events)
        self.assertEqual(result[0]["trace_event_id"], "e1", "M8_sequence_order")
        self.assertEqual(result[0]["shadow_status"], "fully_shadowed", "M8_shadow_classification")
        self.assertEqual(result[0]["later_shadowed_by"], "e2", "M8_shadow_link")

    def test_M9_no_forced_total(self):
        from scripts.phase3a2_forensics import write_original_occurrence_artifact
        path = ROOT / "reports/audit/phase3-original-suspected-occurrences.json"
        data = write_original_occurrence_artifact(ROOT)
        self.assertEqual(data["original_occurrence_set_status"], "unavailable", "M9_original_unavailable")
        self.assertIsNone(data["recovered_occurrence_count"], "M9_null_recovered_count")
        self.assertFalse(data["reconciled"], "M9_not_reconciled")
        self.assertEqual(data["confirmed_duplicates"], 0, "M9_no_duplicates")
        self.assertEqual(data["occurrences"], [], "M9_no_synthetic_occurrences")
        self.assertTrue(path.exists(), "M9_contract_written")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(MutationContractTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
