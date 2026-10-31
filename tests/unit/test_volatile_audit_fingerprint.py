"""Write-time audit columns must not break Method 2 output equivalence."""
from __future__ import annotations

import datetime as dt
import importlib.util
import unittest
from unittest.mock import patch

from ops.gcp.summarize_spark_output import VOLATILE_AUDIT_COLUMNS, _table_summary

HAS_SPARK = importlib.util.find_spec("pyspark") is not None


@unittest.skipUnless(HAS_SPARK, "pyspark is required")
class VolatileAuditFingerprintTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from pyspark.sql import SparkSession

        cls.spark = (
            SparkSession.builder.master("local[1]")
            .config("spark.sql.session.timeZone", "UTC")
            .config("spark.ui.enabled", "false")
            .getOrCreate()
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.spark.stop()

    def _frame(self, created: dt.datetime, count: int = 3):
        event = dt.datetime(2026, 10, 1)
        return self.spark.createDataFrame(
            [("u1", count, event, created), ("u2", 5, event, created)],
            "user_id string, f_user_total_transactions_90d long, "
            "event_timestamp timestamp, created_timestamp timestamp",
        )

    def _summary(self, frame, *, stage: str = "dp2-gold", table: str = "feat_user_90d"):
        with patch("ops.gcp.summarize_spark_output._partition_columns", return_value=[]), \
                patch("ops.gcp.summarize_spark_output._file_layout", return_value={}):
            return _table_summary(frame, stage=stage, table=table, table_path="unused")

    def test_gold_feat_user_90d_created_timestamp_is_declared_volatile(self) -> None:
        self.assertEqual(("created_timestamp",),
                         VOLATILE_AUDIT_COLUMNS[("dp2-gold", "feat_user_90d")])

    def test_runs_differing_only_in_write_time_are_equivalent(self) -> None:
        baseline = self._summary(self._frame(dt.datetime(2026, 10, 31, 4)))
        optimized = self._summary(self._frame(dt.datetime(2026, 10, 31, 9)))
        for key in ("schema_sha256", "row_count", "semantic_content_fingerprint",
                    "required_null_counts"):
            self.assertEqual(baseline[key], optimized[key], key)
        self.assertEqual(["created_timestamp"], baseline["excluded_volatile_columns"])
        self.assertEqual(0, baseline["required_null_counts"]["created_timestamp"])

    def test_feature_value_change_is_still_detected(self) -> None:
        created = dt.datetime(2026, 10, 31, 4)
        left = self._summary(self._frame(created, count=3))
        right = self._summary(self._frame(created, count=4))
        self.assertNotEqual(left["semantic_content_fingerprint"],
                            right["semantic_content_fingerprint"])

    def test_null_audit_timestamp_is_reported(self) -> None:
        summary = self._summary(self._frame(None))
        self.assertEqual(2, summary["required_null_counts"]["created_timestamp"])

    def test_undeclared_tables_keep_every_column_in_fingerprint(self) -> None:
        left = self._summary(self._frame(dt.datetime(2026, 10, 31, 4)),
                             table="dim_user")
        right = self._summary(self._frame(dt.datetime(2026, 10, 31, 9)),
                              table="dim_user")
        self.assertNotEqual(left["semantic_content_fingerprint"],
                            right["semantic_content_fingerprint"])
        self.assertEqual([], left["excluded_volatile_columns"])


if __name__ == "__main__":
    unittest.main()
