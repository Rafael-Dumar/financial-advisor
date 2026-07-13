from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PostPhase2BaselineScriptTests(unittest.TestCase):
    def test_script_uses_explicit_run_ids_and_paths_without_hardcoded_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit = root / "audit"
            main = root / "main"
            close = root / "close"
            audit.mkdir()
            main.mkdir()
            close.mkdir()
            (audit / "gate-analysis.json").write_text(
                json.dumps({"assets": {"AMD": {"base_decision": "tradeable", "final_decision": "wait", "base_scores": {"investment_quality": 70, "swing_trade": 80}, "gates": [{"gate": "market_not_risk_on", "category": "hard_gate", "effect": "cap_to_wait_or_watch_buy", "source": "advisor/scoring.py:hard_gates"}]}}}),
                encoding="utf-8",
            )
            (audit / "data-lineage.json").write_text(
                json.dumps({"assets": {"AMD": {"asset_type": "stock", "fields": {"quote_status": {"status": "available"}, "latest_candle_date": {"value_preview": "2026-07-11", "market_data_kind": "eod_candle"}, "candles": {"source_data_timestamp": "2026-07-11", "cache_fetched_at": "2026-07-12T12:00:00+00:00", "cache_age_seconds": 60}}}}}),
                encoding="utf-8",
            )
            (audit / "audit-summary.json").write_text(json.dumps({"schema_drift": False}), encoding="utf-8")
            (audit / "provider-audit.json").write_text(json.dumps({"errors": [], "providers": {"coingecko": {"calls": [{"provider": "coingecko", "endpoint_name": "markets", "symbol": "AVAX", "schema_valid": False, "fields_present": [], "fields_missing": ["market_cap", "total_volume"], "payload_type": "list", "records_returned": 0, "status": "temporarily_unavailable", "failure_cause": "schema_error", "url_sanitized": "https://example.test/markets?apikey=REDACTED"}]}}}), encoding="utf-8")
            (main / "advisor-report.md").write_text("# main\n", encoding="utf-8")
            (close / "advisor-report.md").write_text("# close\n", encoding="utf-8")
            output_json = root / "baseline.json"
            output_doc = root / "baseline.md"
            schema_drift = root / "schema-drift.json"
            command = [
                sys.executable,
                "scripts/build_post_phase2_baseline.py",
                "--audit-dir", str(audit),
                "--main-artifact-dir", str(main),
                "--close-artifact-dir", str(close),
                "--main-run-id", "111",
                "--close-run-id", "222",
                "--commit-sha", "abc123",
                "--output-json", str(output_json),
                "--output-doc", str(output_doc),
                "--schema-drift-output", str(schema_drift),
            ]
            completed = subprocess.run(command, check=False, capture_output=True, text=True)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(output_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"]["main_run_id"], "111")
            self.assertEqual(payload["source"]["close_run_id"], "222")
            self.assertEqual(payload["commit_sha"], "abc123")
            self.assertNotIn("29199329100", output_doc.read_text(encoding="utf-8"))
            drift = json.loads(schema_drift.read_text(encoding="utf-8"))
            self.assertEqual(drift["occurrences"][0]["provider"], "coingecko")
            self.assertEqual(drift["occurrences"][0]["missing_expected_fields"], ["market_cap", "total_volume"])


if __name__ == "__main__":
    unittest.main()
