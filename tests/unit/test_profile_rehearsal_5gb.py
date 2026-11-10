"""The Source profiler's calculations, on small local Parquet in the GCS layout."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.fs as fs
import pyarrow.parquet as pq

from ops.gcp import profile_rehearsal_5gb as profiler


ROOT = Path(__file__).resolve().parents[2]
T0 = dt.datetime(2026, 1, 1, 8)


def write(root: Path, table: str, columns: dict) -> None:
    directory = root / profiler.ROOT / table
    directory.mkdir(parents=True)
    pq.write_table(pa.table(columns), directory / "shard-00000.parquet")


def transactions(ids, merchants, types):
    return {"transaction_id": ids, "merchant_id": merchants, "type": types, "timestamp": [T0] * len(ids)}


class ProfilerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.fs = fs.SubTreeFileSystem(str(self.root), fs.LocalFileSystem())
        # m1 has 3 payments and m2 has 1; t1 is duplicated across versions; t5 is a transfer.
        write(self.root, "transactions_v1", transactions(["t1", "t2"], ["m1", "m1"], ["payment", "payment"]))
        write(self.root, "transactions_v2", {
            **transactions(["t1", "t3", "t4", "t5"], ["m1", "m1", "m2", None], ["payment", "payment", "payment", "transfer"]),
            "channel": ["app", "app", "web", None],
        })
        write(self.root, "users", {"user_id": ["u1", "u2"]})
        write(self.root, "login_events", {"login_id": ["l1", "l2", "l2"]})

    def test_transaction_profile_deduplicates_and_counts_only_payments(self):
        result = profiler.scan_transactions(self.fs)
        self.assertEqual(6, result["physical_transaction_rows"])
        self.assertEqual(5, result["unique_transaction_ids"])
        self.assertEqual(1, result["duplicate_rows"])
        self.assertEqual(4, result["payment_transactions_logical"])
        skew = result["merchant_skew"]
        self.assertEqual(2, skew["distinct_merchants_with_payments"])
        self.assertEqual(1, skew["top_5_percent_merchants_count"])
        self.assertEqual(75.0, skew["top_5_percent_payment_share_percent"])
        self.assertEqual(1.5, skew["max_to_median_ratio"])
        self.assertEqual({8: 5}, result["hour_distribution_logical_transactions"])
        self.assertEqual({"NULL": 1, "app": 1, "web": 1}, result["v2_channel_distribution_logical_transactions"])
        self.assertFalse(result["schema"]["v1_has_channel"])
        self.assertTrue(result["schema"]["v2_has_channel"])

    def test_distinct_counts_expose_duplicate_ids(self):
        result = profiler.distinct_id_table(self.fs, "login_events", "login_id")
        self.assertEqual((3, 2), (result["physical_rows"], result["unique_login_id"]))
        self.assertEqual(66.666667, result["unique_ratio_percent"])

    def test_a_missing_required_column_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "missing columns"):
            list(profiler.batches(self.fs, "users", ["user_id", "absent"]))

    def run_main(self, **expected):
        output = self.root / "out/report.json"
        with patch.object(fs, "GcsFileSystem", return_value=self.fs), \
                patch.object(sys, "argv", ["profile", "--output", str(output)]), \
                patch.multiple(profiler, **expected):
            code = profiler.main()
        return code, json.loads(output.read_text(encoding="utf-8"))

    def test_mismatches_are_review_required(self):
        code, report = self.run_main(EXPECTED_PHYSICAL=6, EXPECTED_LOGICAL=5, EXPECTED_DUPLICATES=1)
        self.assertEqual((2, "REVIEW_REQUIRED"), (code, report["status"]))
        self.assertTrue(report["checks"]["expected_physical_transaction_rows"])
        self.assertFalse(report["checks"]["login_ids_unique"])

    def test_a_consistent_dataset_passes(self):
        write(self.root / "b", "login_events", {"login_id": ["l1", "l2"]})
        (self.root / profiler.ROOT / "login_events" / "shard-00000.parquet").replace(self.root / "dup.parquet")
        (self.root / "b" / profiler.ROOT / "login_events" / "shard-00000.parquet").replace(
            self.root / profiler.ROOT / "login_events" / "shard-00000.parquet")
        code, report = self.run_main(EXPECTED_PHYSICAL=6, EXPECTED_LOGICAL=5, EXPECTED_DUPLICATES=1)
        self.assertEqual((0, "PASS"), (code, report["status"]))

    def test_the_scan_targets_only_the_registered_rehearsal_dataset(self):
        self.assertEqual("ewallet-data-ai-platform-hg26-rehearsal-5gb-data/source/rehearsal-5gb-v1", profiler.ROOT)

    def test_help_runs_as_a_module(self):
        completed = subprocess.run([sys.executable, "-m", "ops.gcp.profile_rehearsal_5gb", "--help"],
                                   cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertIn("--output", completed.stdout)


if __name__ == "__main__":
    unittest.main()
