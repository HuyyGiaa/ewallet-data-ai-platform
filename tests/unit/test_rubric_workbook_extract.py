"""The fail-closed ledger must use the points of the original EDAI K11 workbook."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from ops.rubric.evidence_matrix import EXPECTED_RUBRIC, load_matrix

ROOT = Path(__file__).resolve().parents[2]
EXTRACT = ROOT / "docs/rubric/edai_k11_workbook_extract.json"
MATRIX = ROOT / "docs/rubric/rubric_evidence_matrix.csv"
SHEETS = ("edai-1 (50%)", "edai-2 (50%)")


class RubricWorkbookExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.extract = json.loads(EXTRACT.read_text(encoding="utf-8"))

    def test_extract_contains_only_the_two_active_sheets_each_worth_100(self) -> None:
        self.assertEqual(tuple(self.extract["sheets"]), SHEETS)
        self.assertRegex(self.extract["sha256"], r"^[0-9a-f]{64}$")
        for sheet in SHEETS:
            rows = self.extract["sheets"][sheet]["rows"]
            self.assertEqual(sum(row["points"] for row in rows), 100)
            self.assertEqual(self.extract["sheets"][sheet]["total"], 100)

    def test_expected_rubric_points_match_the_workbook(self) -> None:
        workbook = {
            (sheet, row["row"]): row["points"]
            for sheet in SHEETS for row in self.extract["sheets"][sheet]["rows"]
        }
        expected = {(item.sheet, item.row): item.points for item in EXPECTED_RUBRIC}
        self.assertEqual(expected, workbook)

    def test_ledger_points_match_the_workbook(self) -> None:
        workbook = {
            (sheet, row["row"]): row["points"]
            for sheet in SHEETS for row in self.extract["sheets"][sheet]["rows"]
        }
        ledger = {(row["sheet"], int(row["row"])): int(row["points"]) for row in load_matrix(MATRIX)}
        self.assertEqual(ledger, workbook)


if __name__ == "__main__":
    unittest.main()
