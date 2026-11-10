"""The post-campaign comparison accepts real equivalence and reports differences."""

from __future__ import annotations

import unittest
import unittest.mock

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from data_platform.config.dataset_identity import CANONICAL_100GB, REHEARSAL_5GB
from ops.gcp.compare_campaign_outputs import (
    EQUIVALENT,
    INCOMPLETE,
    MISMATCH,
    STAGES,
    audit_columns,
    compare_campaigns,
    compare_stage,
    compare_table,
    manifest_problems,
    stage_keys,
)
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
        return compare_table(self.frame, change(self.frame), "t", keys, ("created",))

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
        self.assertTrue(compare_table(self.frame, self.frame, "t", None, ("created",))["passed"])
        changed = self.frame.withColumn("n", F.col("n") + 1)
        self.assertIn("multiset", compare_table(self.frame, changed, "t", None, ("created",))["differences"])

    def test_every_table_with_a_key_has_a_known_key(self):
        for stage in ("dp2-silver", "dp2-gold", "dp3-features"):
            with self.subTest(stage):
                keys = stage_keys(stage)
                self.assertEqual(set(), expected_tables(stage, REHEARSAL_5GB) - set(keys))
                self.assertEqual(set(), expected_tables(stage, CANONICAL_100GB) - set(keys))
        self.assertEqual({}, dict(stage_keys("dp1-bronze")))

    def key_frame(self):
        return self.spark.createDataFrame(
            [("u1", "2026-01-01 10:00:00", 1, "food"), ("u2", "2026-01-02 10:00:00", 0, "retail")],
            "id string, ts string, label int, category string",
        ).withColumn("event_timestamp", F.col("ts").cast("timestamp")).drop("ts")

    def test_a_changed_key_or_business_timestamp_is_a_mismatch(self):
        frame = self.key_frame()
        moved = frame.withColumn("event_timestamp", F.col("event_timestamp") + F.expr("INTERVAL 1 SECOND"))
        self.assertIn("unmatched_keys", compare_table(frame, moved, "feat", ("id", "event_timestamp"))["differences"])
        self.assertEqual({"event_timestamp": 2}, compare_table(frame, moved, "t", ("id",))["differences"]["columns"])
        renamed = frame.withColumn("id", F.concat(F.col("id"), F.lit("x")))
        self.assertIn("unmatched_keys", compare_table(frame, renamed, "t", ("id",))["differences"])

    def test_a_changed_fraud_label_or_category_is_a_mismatch(self):
        frame = self.key_frame()
        flipped = frame.withColumn("label", 1 - F.col("label"))
        self.assertEqual({"label": 2}, compare_table(frame, flipped, "fraud_labels", ("id",))["differences"]["columns"])
        recategorized = frame.withColumn("category", F.lit("other"))
        self.assertEqual({"category": 2}, compare_table(frame, recategorized, "t", ("id",))["differences"]["columns"])

    def test_tolerance_does_not_apply_to_integers(self):
        frame = self.spark.createDataFrame([("a", 10**15)], "id string, n long")
        self.assertFalse(compare_table(frame, frame.withColumn("n", F.col("n") + 1), "t", ("id",))["passed"])

    def test_per_run_validation_still_catches_bad_audit_metadata(self):
        from data_platform.quality.feature_timestamp_quality import feature_timestamp_results

        clock = __import__("datetime").datetime(2026, 11, 10, tzinfo=__import__("datetime").timezone.utc)
        frame = self.key_frame().withColumn("created", F.lit(clock).cast("timestamp"))
        self.assertTrue(all(r.passed for r in feature_timestamp_results(frame, "t", materialization_clock=clock, require_clock=True)))
        wrong = frame.withColumn("created", F.col("event_timestamp"))
        failed = {r.rule_name for r in feature_timestamp_results(wrong, "t", materialization_clock=clock, require_clock=True) if not r.passed}
        self.assertIn("t.created_matches_materialization_clock", failed)
        missing = {r.rule_name for r in feature_timestamp_results(frame.drop("created"), "t", require_clock=True) if not r.passed}
        self.assertEqual({"t.timestamp_columns_typed"}, missing)

    def test_only_declared_columns_are_audit_columns(self):
        self.assertEqual(("created_timestamp", "created"), audit_columns("dp2-gold", "feat_user_90d"))
        for table in ("feat_user_behavior", "feat_account_behavior", "feat_device_behavior", "feat_merchant_behavior"):
            self.assertEqual(("created",), audit_columns("dp3-features", table))
        for stage, table in (("dp2-gold", "dim_merchant_scd2"), ("dp2-gold", "dim_user"), ("dp2-silver", "transactions")):
            self.assertEqual((), audit_columns(stage, table))

    def test_a_timestamp_named_like_audit_metadata_is_still_compared_elsewhere(self):
        frame = self.frame.withColumn("created_at", F.current_timestamp())
        changed = frame.withColumn("created_at", F.col("created_at") + F.expr("INTERVAL 1 SECOND"))
        report = compare_table(frame, changed, "dim_user", ("id",), ("created",))
        self.assertEqual({"created_at": 3}, report["differences"]["columns"])

    def test_different_created_timestamp_on_the_legacy_table_is_excluded(self):
        frame = self.frame.withColumn("created_timestamp", F.lit("2026-01-01 00:00:00").cast("timestamp"))
        changed = frame.withColumn("created_timestamp", F.lit("2026-06-01 00:00:00").cast("timestamp"))
        self.assertTrue(compare_table(frame, changed, "feat_user_90d", ("id",), ("created_timestamp", "created"))["passed"])

    def test_a_missing_declared_audit_column_is_incomplete_not_equivalent(self):
        report = compare_table(self.frame, self.frame.drop("created"), "t", ("id",), ("created",))
        self.assertFalse(report["passed"])
        self.assertIn("audit columns", report["incomplete"])

    def manifest(self, profile, **overrides):
        stages = {stage: {"run_id": f"{profile}-{stage}", "output_root": f"/{profile}/{stage}",
                          "validation": {"status": "PASS"}} for stage, _ in STAGES}
        return {"status": "COMPLETE", "spark_profile": profile, "dataset_version": REHEARSAL_5GB,
                "benchmark_id": "ab-1", "source": {"manifest_sha256": "s"}, "stages": stages, **overrides}

    def test_comparison_requires_two_complete_campaigns_on_one_source(self):
        base, opt = self.manifest("baseline"), self.manifest("optimized")
        self.assertEqual([], manifest_problems(base, opt))
        cases = {"status": dict(opt, status="FAILED"), "source": dict(opt, source={"manifest_sha256": "x"}),
                 "dataset": dict(opt, dataset_version="other"), "profile": dict(opt, spark_profile="baseline")}
        for name, changed in cases.items():
            with self.subTest(name):
                report = compare_campaigns(None, base, changed)
                self.assertEqual(INCOMPLETE, report["verdict"])
                self.assertTrue(report["problems"])
                self.assertEqual([], report["stages"])
        failed = self.manifest("optimized")
        failed["stages"]["gold"]["validation"]["status"] = "FAIL"
        self.assertEqual(INCOMPLETE, compare_campaigns(None, base, failed)["verdict"])

    def test_a_report_names_runs_and_verdicts_every_stage(self):
        base, opt = self.manifest("baseline"), self.manifest("optimized")
        with unittest.mock.patch("ops.gcp.compare_campaign_outputs.compare_stage",
                                 side_effect=lambda spark, stage, *a: {"stage": stage, "verdict": EQUIVALENT, "tables": []}):
            report = compare_campaigns(None, base, opt)
        self.assertEqual(EQUIVALENT, report["verdict"])
        self.assertEqual("baseline-gold", report["baseline_run_ids"]["gold"])
        self.assertEqual("s", report["source_manifest_sha256"])
        self.assertEqual({"rel_tol": 1e-12, "abs_tol": 1e-9}, report["float_policy"])
        with unittest.mock.patch("ops.gcp.compare_campaign_outputs.compare_stage",
                                 side_effect=lambda spark, stage, *a: {"stage": stage, "verdict": MISMATCH if stage == "dp2-gold" else EQUIVALENT, "tables": []}):
            self.assertEqual(MISMATCH, compare_campaigns(None, base, opt)["verdict"])

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
            self.assertEqual(EQUIVALENT, result["verdict"])
        self.assertEqual(8, len(read))
        self.assertTrue(all(Path(uri).parts[-1].startswith("feat_") for uri in read))


if __name__ == "__main__":
    unittest.main()
