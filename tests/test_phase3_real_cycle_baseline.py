from __future__ import annotations

import hashlib
import ast
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / ".tmp/nightly-review/2026-07-15/run-29429941131-main/history/2026-07-15-main.md"
EXPECTED_SCORING_SHA256 = "16B7A0A4C93ECD0E633B7DF560C585F10398426EDA8861D7D06C38E3449BAFAD"
EXPECTED_REPORT_SHA256 = "410DCADEA7EB22DCB71C53A45B05E1C70484B038D87565847C966351805D4E8A"


class Phase3BaselineArtifactTests(unittest.TestCase):
    def test_source_grounded_builder_has_no_parallel_simulator_or_forced_49(self):
        source = (ROOT / "scripts/phase3a2_forensics.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name == "_simulate_without_backtest" for node in ast.walk(tree)))
        self.assertFalse(any(isinstance(node, ast.Name) and node.id in {"FORCED_TOTAL", "RECOVERED_COUNT"} for node in ast.walk(tree)))

    def test_scoring_bytes_are_unchanged_by_audit_builder(self):
        from scripts.phase3a2_forensics import build_all
        build_all(ROOT, REPORT, run_mutations=False)
        self.assertEqual(hashlib.sha256((ROOT / "advisor/scoring.py").read_bytes()).hexdigest().upper(), EXPECTED_SCORING_SHA256)
        self.assertEqual(hashlib.sha256(REPORT.read_bytes()).hexdigest().upper(), EXPECTED_REPORT_SHA256)
        diff = subprocess.run(["git", "diff", "--", "advisor/scoring.py"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(diff.stdout, "")

    def test_fixed_scoring_hash_detects_mutated_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "scoring.py"
            copy.write_bytes((ROOT / "advisor/scoring.py").read_bytes())
            original = hashlib.sha256(copy.read_bytes()).hexdigest()
            copy.write_bytes(copy.read_bytes() + b"\n# controlled mutation\n")
            mutated = hashlib.sha256(copy.read_bytes()).hexdigest()
            self.assertEqual(original.upper(), EXPECTED_SCORING_SHA256)
            self.assertNotEqual(mutated.upper(), EXPECTED_SCORING_SHA256)

    def test_unavailable_occurrence_contract_is_explicit(self):
        import json
        data = json.loads((ROOT / "reports/audit/phase3-original-suspected-occurrences.json").read_text(encoding="utf-8"))
        self.assertEqual(data["original_occurrence_set_status"], "unavailable")
        self.assertEqual(data["source_search_status"], "completed_no_original_runtime_artifact")
        self.assertIsNone(data["recovered_occurrence_count"])
        self.assertFalse(data["reconciled"])
        self.assertEqual(data["confirmed_duplicates"], 0)
        self.assertEqual(data["occurrences"], [])
        self.assertEqual(data["reason"], "original_occurrence_artifact_not_found")

    def test_mutation_artifact_has_identical_workspace_manifests_and_determinism(self):
        import json
        data = json.loads((ROOT / "reports/audit/phase3-mutation-test-results.json").read_text(encoding="utf-8"))
        self.assertTrue(data["determinism"]["verified"])
        self.assertEqual(data["determinism"]["generation_hashes"][0], data["determinism"]["generation_hashes"][1])
        base = data["control_run"]["workspace_manifest_sha256"]
        for mutation in data["mutations"]:
            self.assertEqual(mutation["base_workspace_manifest_sha256"], base)
            self.assertEqual(mutation["clone_workspace_manifest_sha256_before"], base)
            self.assertTrue(mutation["mutation_changed_target"])
            self.assertNotRegex(json.dumps(mutation), r"<duration>|0x[0-9a-fA-F]+|C:\\\\Users\\\\")

    def test_mutation_execution_contract_uses_one_pre_execution_base(self):
        import json
        data = json.loads((ROOT / "reports/audit/phase3-mutation-test-results.json").read_text(encoding="utf-8"))
        base = data["workspace_base"]["initial_manifest"]
        self.assertEqual(data["workspace_base"]["after_control_manifest"], base)
        self.assertEqual(data["workspace_base"]["final_manifest"], base)
        self.assertEqual(set(data["workspace_base"]["pre_execution_clone_manifests"]), {f"M{i}" for i in range(1, 10)})
        self.assertTrue(all(manifest == base for manifest in data["workspace_base"]["pre_execution_clone_manifests"].values()))
        control = data["control_run"]
        self.assertEqual(control["clone_manifest_before_test"], base)
        self.assertEqual(control["executed_test_count"], 9)
        for mutation in data["mutations"]:
            self.assertEqual(mutation["clone_manifest_before_test"], base)
            self.assertEqual(mutation["executed_test_count"], 9)
            self.assertEqual(mutation["command"], control["command"])
            self.assertEqual(mutation["selected_test_suite"], "tests.phase3_mutation_assertions")


if __name__ == "__main__":
    unittest.main()
