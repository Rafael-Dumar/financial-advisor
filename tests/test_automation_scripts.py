from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AutomationScriptsTests(unittest.TestCase):
    def test_automation_scripts_enforce_live_reporting_contract(self) -> None:
        scripts = [
            ("run-main-report.ps1", "--include-discovery", "main"),
            ("run-close-report.ps1", "", "close"),
        ]

        for script_name, expected_scan_flag, history_label in scripts:
            script_path = PROJECT_ROOT / "scripts" / script_name
            self.assertTrue(script_path.exists(), f"missing {script_name}")
            content = script_path.read_text(encoding="utf-8")

            self.assertIn(".venv\\Scripts\\Activate.ps1", content)
            self.assertIn("advisor config validate --require-live", content)
            self.assertIn("advisor scan", content)
            self.assertIn("--require-live", content)
            self.assertIn(".tmp\\logs", content)
            self.assertIn("reports\\latest.md", content)
            self.assertIn(f"{history_label}.md", content)
            self.assertIn("Data mode: `live`", content)
            self.assertIn(".Contains('Data mode: `live`')", content)
            self.assertNotIn("-notlike '*Data mode: `live`*'", content)
            self.assertIn("live_validation_failed", content)
            if expected_scan_flag:
                self.assertIn(expected_scan_flag, content)
