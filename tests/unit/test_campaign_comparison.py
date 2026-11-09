"""The post-campaign comparison accepts real equivalence and reports differences."""

from __future__ import annotations

import unittest

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from data_platform.config.dataset_identity import CANONICAL_100GB, REHEARSAL_5GB
from ops.gcp.compare_campaign_outputs import compare_stage, compare_table, stage_keys
from ops.gcp.validate_spark_output_equivalence import expected_tables


class ComparisonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spark = (SparkSession.builder.master("local[1]").appName("campaign-compare")
                     .config("spark.ui.enabled", "false").config("spark.sql.session.timeZone", "UTC")
                     .config("spark.sql.shuffle.partitions", "2").getOrCreate())
        cls.spark.sparkContext.setLogLevel("ERROR")
        cls.frame = cls.spark.createDataFrame(
            [("a", 1, 10.0, None), ("b", 2, 1000000.5, "x"), ("c", 3, None, "y")],
            "id string, n long, value double, name string",
        ).withColumn("created", F.current_timestamp())

    def compare(self, change, keys=("id",)):
        return compare_table(self.frame, change(self.frame), "t", keys)

    def test_identical_tables_pass(self):
        self.assertTrue(self.compare(lambda df: df)["passed"])

    def test_audit_columns_are_excluded(self):
        self.assertTrue(self.compare(lambda df: df.withColumn("created", F.lit(None).cast("timestamp")))["passed"])

    def test_last_bit_float_differences_are_tolerated(self):
        self.assertTrue(self.compare(lambda df: df.withColumn("value", F.col("value") * (1 + 1e-13)))["passed"])

    def test_a_real_float_difference_is_reported(self):
        report = self.compare(lambda df: df.withColumn("value", F.col("value") * (1 + 1e-9)))
        self.assertFalse(report["passed"])
        self.assertEqual({"value": 2}, report["differences"]["columns"])

    def test_null_position_change_is_reported(self):
        report = self.compare(lambda df: df.withColumn("name", F.coalesce(F.col("name"), F.lit("z"))))
        self.assertEqual({"name": 1}, report["differences"]["columns"])

    def test_missing_or_extra_keys_and_duplicates_are_reported(self):
        report = self.compare(lambda df: df.filter("id <> 'a'"))
        self.assertIn("unmatched_keys", report["differences"])
        self.assertEqual(1, report["differences"]["row_count"])
        report = self.compare(lambda df: df.unionByName(df.filter("id = 'a'")))
        self.assertEqual({"baseline": 0, "optimized": 1}, report["differences"]["duplicate_keys"])

    def test_a_schema_difference_is_reported(self):
        report = self.compare(lambda df: df.withColumn("n", F.col("n").cast("int")))
        self.assertIn("schema", report["differences"])

    def test_tables_without_keys_compare_as_a_multiset(self):
        self.assertTrue(compare_table(self.frame, self.frame, "t", None)["passed"])
        changed = self.frame.withColumn("n", F.col("n") + 1)
        self.assertIn("multiset", compare_table(self.frame, changed, "t", None)["differences"])

    def test_every_table_with_a_key_has_a_known_key(self):
        for stage in ("dp2-silver", "dp2-gold", "dp3-features"):
            with self.subTest(stage):
                keys = stage_keys(stage)
                self.assertEqual(set(), expected_tables(stage, REHEARSAL_5GB) - set(keys))
                self.assertEqual(set(), expected_tables(stage, CANONICAL_100GB) - set(keys))
        self.assertEqual({}, dict(stage_keys("dp1-bronze")))

    def test_silver_keys_match_the_relationship_contract(self):
        from pathlib import Path

        import yaml

        from ops.gcp.compare_campaign_outputs import SILVER_KEYS

        root = Path(__file__).resolve().parents[2]
        document = yaml.safe_load((root / "data_platform/contracts/relationships/schema_relationships.yml").read_text())
        contract = {table: tuple(keys) for table, keys in document["keys"]["silver"].items()}
        self.assertEqual(contract, {k: v for k, v in SILVER_KEYS.items() if k != "merchant_history"})

    def test_compare_stage_reads_every_expected_table(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        read = {}
        def fake_load(uri):
            read[uri] = True
            return self.frame
        with tempfile.TemporaryDirectory() as directory, patch("ops.gcp.compare_campaign_outputs.compare_table",
                                                               return_value={"passed": True}):
            spark = type("S", (), {"read": type("R", (), {"format": staticmethod(lambda f: type("L", (), {"load": staticmethod(fake_load)})())})()})()
            result = compare_stage(spark, "dp3-features", REHEARSAL_5GB, f"{directory}/a", f"{directory}/b")
            self.assertEqual("PASS", result["status"])
        self.assertEqual(8, len(read))
        self.assertTrue(all(Path(uri).parts[-1].startswith("feat_") for uri in read))


if __name__ == "__main__":
    unittest.main()
