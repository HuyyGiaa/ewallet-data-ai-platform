"""Reproducibility and rubric evidence contracts."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


class EvidenceTests(unittest.TestCase):
    def test_validator_passes_and_reports_static_not_gke(self) -> None:
        proc = subprocess.run(
            [sys.executable, "scripts/observability/validate.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        report = json.loads(proc.stdout)
        self.assertEqual(report["status"], "STATIC_VALIDATION_PASS")
        self.assertFalse(report["gke_runtime_verified"])
        self.assertFalse(report["rubric_verified"])
        self.assertTrue(all(report["checks"].values()))

    def test_manifest_hashes_every_declared_artifact(self) -> None:
        manifest = json.loads((
            ROOT / "docs/evidence/observability/source_validation/manifest.json"
        ).read_text())
        self.assertEqual(manifest["status"], "STATIC_VALIDATION_PASS")
        self.assertFalse(manifest["gke_runtime_verified"])
        for item in manifest["artifacts"]:
            content = (ROOT / item["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])

    def test_rubric_mapping_covers_exact_observability_rows(self) -> None:
        with (ROOT / "docs/rubric/rubric_evidence_matrix.csv").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        selected = {
            int(row["row"]): row for row in rows
            if row["sheet"] == "edai-2 (50%)" and 46 <= int(row["row"]) <= 51
        }
        self.assertEqual(set(selected), set(range(46, 52)))
        for row in selected.values():
            self.assertEqual(row["status"], "PARTIAL")
            self.assertEqual(row["assessment_basis"], "VERIFIED")
            self.assertEqual(row["runtime_evidence"], "")
            self.assertEqual(row["awarded_points"], "0")
            self.assertIn("docs/observability.md", row["source_paths"])
            self.assertIn("static validation passed", row["notes"].lower())

        documentation = (ROOT / "docs/observability.md").read_text()
        for status in (
            "SOURCE_READY", "STATIC_VALIDATION_PASS", "LOCAL_RUNTIME_VERIFIED",
            "GKE_RUNTIME_VERIFIED", "RUBRIC_VERIFIED",
        ):
            self.assertIn(status, documentation)
        self.assertIn("rubic final-coursework (final -", documentation)
        for row in range(46, 52):
            self.assertIn(f"| {row} |", documentation)


if __name__ == "__main__":
    unittest.main()
