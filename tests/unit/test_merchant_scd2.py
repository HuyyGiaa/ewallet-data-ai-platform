"""SCD Type 2 merchant dimension: builder semantics, as-of lookup and validator negatives."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import StringType, StructField, StructType, TimestampType

from data_platform.processing.spark.gold.dimension_builder import (
    build_dim_merchant,
    build_dim_merchant_scd2,
)
from data_platform.processing.spark.gold.scd2 import (
    Scd2ContractError,
    build_scd2_from_snapshots,
    lookup_as_of,
)
from data_platform.processing.spark.silver.cleaners import clean_merchant_history
from data_platform.quality.scd2_quality import (
    as_of_resolution_result,
    merchant_history_results,
    merchant_scd2_results,
)


def ts(month: int, day: int = 1, hour: int = 0) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=timezone.utc)


def utc(value: datetime | None) -> datetime | None:
    """PySpark returns naive datetimes in the driver's local zone; make them aware UTC."""
    return None if value is None else value.astimezone(timezone.utc)


HISTORY_SCHEMA = StructType([
    StructField("snapshot_id", StringType()),
    StructField("merchant_id", StringType()),
    StructField("merchant_name", StringType()),
    StructField("category", StringType()),
    StructField("snapshot_ts", TimestampType()),
    StructField("history_source", StringType()),
])


class Scd2Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spark = (
            SparkSession.builder.master("local[1]").appName("scd2-tests")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.session.timeZone", "UTC")
            .config("spark.sql.shuffle.partitions", "2")
            .getOrCreate()
        )
        cls.spark.sparkContext.setLogLevel("ERROR")

    def history(self, rows):
        """rows: (merchant_id, name, category, snapshot_ts)."""
        data = [(f"{m}-{t}", m, n, c, t, "synthetic") for m, n, c, t in rows]
        return self.spark.createDataFrame(data, HISTORY_SCHEMA)

    def versions(self, rows):
        frame = build_dim_merchant_scd2(self.history(rows))
        return [
            (r.merchant_id, r.merchant_name, r.category, utc(r.valid_from_ts), utc(r.valid_to_ts), r.is_current)
            for r in frame.orderBy("merchant_id", "valid_from_ts").collect()
        ]


class BuilderTest(Scd2Base):
    def test_01_single_state_merchant(self):
        self.assertEqual([("m1", "A", "food", ts(1), None, True)],
                         self.versions([("m1", "A", "food", ts(1))]))

    def test_02_multiple_historical_versions(self):
        rows = [("m1", "A", "food", ts(1)), ("m1", "A", "retail", ts(2)), ("m1", "B", "retail", ts(3))]
        self.assertEqual([
            ("m1", "A", "food", ts(1), ts(2), False),
            ("m1", "A", "retail", ts(2), ts(3), False),
            ("m1", "B", "retail", ts(3), None, True),
        ], self.versions(rows))

    def test_03_unchanged_consecutive_snapshots_collapse(self):
        rows = [("m1", "A", "food", ts(1)), ("m1", "A", "food", ts(2)), ("m1", "A", "food", ts(3)),
                ("m1", "B", "food", ts(4)), ("m1", "B", "food", ts(5))]
        self.assertEqual([("m1", "A", "food", ts(1), ts(4), False), ("m1", "B", "food", ts(4), None, True)],
                         self.versions(rows))

    def test_03b_a_reverted_state_opens_a_new_version(self):
        rows = [("m1", "A", "food", ts(1)), ("m1", "B", "food", ts(2)), ("m1", "A", "food", ts(3))]
        self.assertEqual(3, len(self.versions(rows)))

    def test_04_duplicate_identical_snapshots(self):
        rows = [("m1", "A", "food", ts(1))] * 3 + [("m1", "B", "food", ts(2))] * 2
        self.assertEqual([("m1", "A", "food", ts(1), ts(2), False), ("m1", "B", "food", ts(2), None, True)],
                         self.versions(rows))

    def test_05_conflicting_same_timestamp_snapshots_are_rejected(self):
        with self.assertRaisesRegex(Scd2ContractError, "conflicting snapshots"):
            self.versions([("m1", "A", "food", ts(1)), ("m1", "B", "food", ts(1))])

    def test_09_null_effective_timestamp_or_key_is_rejected(self):
        with self.assertRaisesRegex(Scd2ContractError, "null"):
            self.versions([("m1", "A", "food", ts(1)), ("m1", "B", "food", None)])
        with self.assertRaisesRegex(Scd2ContractError, "null"):
            self.versions([(None, "A", "food", ts(1))])

    def test_09b_malformed_effective_timestamp_type_is_rejected(self):
        frame = self.history([("m1", "A", "food", ts(1))]).withColumn("snapshot_ts", F.lit("2026-01-01"))
        with self.assertRaisesRegex(Scd2ContractError, "must be TIMESTAMP"):
            build_dim_merchant_scd2(frame)
        with self.assertRaisesRegex(Scd2ContractError, "missing columns"):
            build_dim_merchant_scd2(self.history([("m1", "A", "food", ts(1))]).drop("category"))

    def test_10_out_of_order_snapshots(self):
        rows = [("m1", "B", "food", ts(3)), ("m1", "A", "food", ts(1)), ("m1", "A", "food", ts(2))]
        self.assertEqual([("m1", "A", "food", ts(1), ts(3), False), ("m1", "B", "food", ts(3), None, True)],
                         self.versions(rows))

    def test_11_multiple_merchants_have_independent_histories(self):
        rows = [("m1", "A", "food", ts(1)), ("m1", "B", "food", ts(3)),
                ("m2", "X", "retail", ts(2)), ("m2", "X", "retail", ts(3))]
        self.assertEqual([
            ("m1", "A", "food", ts(1), ts(3), False),
            ("m1", "B", "food", ts(3), None, True),
            ("m2", "X", "retail", ts(2), None, True),
        ], self.versions(rows))

    def test_14_rebuilding_or_replaying_snapshots_is_idempotent(self):
        rows = [("m1", "A", "food", ts(1)), ("m1", "B", "food", ts(2)), ("m2", "X", "retail", ts(1))]
        once = build_dim_merchant_scd2(self.history(rows))
        twice = build_dim_merchant_scd2(self.history(rows).unionByName(self.history(rows)))
        self.assertEqual(sorted(once.collect()), sorted(twice.collect()))
        self.assertEqual(sorted(once.collect()), sorted(build_dim_merchant_scd2(self.history(rows)).collect()))

    def test_surrogate_key_is_deterministic_and_preserves_the_business_key(self):
        frame = build_dim_merchant_scd2(self.history([("m1", "A", "food", ts(1)), ("m1", "B", "food", ts(2))]))
        self.assertEqual(["merchant_sk", "merchant_id", "merchant_name", "category",
                          "valid_from_ts", "valid_to_ts", "is_current"], frame.columns)
        keys = [row.merchant_sk for row in frame.orderBy("valid_from_ts").collect()]
        self.assertEqual(2, len(set(keys)))
        self.assertTrue(all(len(key) == 64 for key in keys))

    def test_null_attribute_changes_are_detected(self):
        rows = [("m1", "A", None, ts(1)), ("m1", "A", None, ts(2)), ("m1", "A", "food", ts(3))]
        self.assertEqual(2, len(self.versions(rows)))

    def test_generic_builder_refuses_missing_attributes(self):
        with self.assertRaises(Scd2ContractError):
            build_scd2_from_snapshots(self.history([("m1", "A", "food", ts(1))]), business_key="merchant_id",
                                      attribute_columns=(), effective_ts_column="snapshot_ts")


class AsOfLookupTest(Scd2Base):
    def setUp(self):
        self.scd2 = build_dim_merchant_scd2(self.history([
            ("m1", "A", "food", ts(1)), ("m1", "B", "food", ts(3)), ("m1", "C", "travel", ts(5)),
        ]))

    def lookup(self, event_time):
        events = self.spark.createDataFrame([("e", "m1", event_time)], "event_id string, merchant_id string, t timestamp")
        rows = lookup_as_of(events, self.scd2, business_key="merchant_id", event_ts_column="t",
                            version_columns=("merchant_name",)).collect()
        self.assertEqual(1, len(rows), "a lookup never duplicates an event")
        return rows[0].asof_merchant_name

    def test_06_exact_boundary_selects_the_new_version(self):
        self.assertEqual("B", self.lookup(ts(3)))
        self.assertEqual("C", self.lookup(ts(5)))
        self.assertEqual("A", self.lookup(datetime(2026, 2, 28, 23, 59, 59, 999999, tzinfo=timezone.utc)))

    def test_07_before_the_first_version_has_no_match(self):
        self.assertIsNone(self.lookup(datetime(2025, 12, 31, tzinfo=timezone.utc)))
        self.assertEqual("A", self.lookup(ts(1)))

    def test_08_after_the_last_version_uses_the_open_current_version(self):
        self.assertEqual("C", self.lookup(datetime(2030, 1, 1, tzinfo=timezone.utc)))

    def test_as_of_rule_counts_unresolved_transactions(self):
        tx = self.spark.createDataFrame(
            [("t1", "m1", ts(2)), ("t2", "m1", ts(3)), ("t3", None, ts(4))],
            "transaction_id string, merchant_id string, timestamp timestamp")
        self.assertTrue(as_of_resolution_result(self.scd2, tx).passed)
        early = tx.unionByName(self.spark.createDataFrame(
            [("t4", "m1", datetime(2025, 1, 1, tzinfo=timezone.utc))],
            "transaction_id string, merchant_id string, timestamp timestamp"))
        result = as_of_resolution_result(self.scd2, early)
        self.assertFalse(result.passed)
        self.assertEqual(1, result.actual_value)


class ValidatorTest(Scd2Base):
    ROWS = [("m1", "A", "food", ts(1)), ("m1", "B", "food", ts(3)), ("m2", "X", "retail", ts(1))]

    def setUp(self):
        self.snapshots = self.history(self.ROWS)
        self.merchants = self.spark.createDataFrame(
            [("m1", "B", "food"), ("m2", "X", "retail")], "merchant_id string, merchant_name string, category string")
        self.dim_merchant = build_dim_merchant(self.merchants)
        self.scd2 = build_dim_merchant_scd2(self.snapshots)

    def failed(self, results):
        return {result.rule_name for result in results if not result.passed}

    def test_12_13_16_valid_dimension_passes_every_rule(self):
        self.assertEqual(set(), self.failed(merchant_scd2_results(self.scd2, self.dim_merchant)))
        self.assertEqual(set(), self.failed(merchant_history_results(self.snapshots, self.merchants)))

    def corrupt(self, change):
        return self.failed(merchant_scd2_results(change(self.scd2), self.dim_merchant))

    def test_overlapping_intervals_are_detected(self):
        late_end = lambda df: df.withColumn(
            "valid_to_ts",
            F.when(F.col("merchant_name") == "A", F.lit(ts(4))).otherwise(F.col("valid_to_ts")))
        self.assertIn("dim_merchant_scd2.contiguous_non_overlapping_intervals", self.corrupt(late_end))

    def test_multiple_current_versions_are_detected(self):
        both_current = lambda df: df.withColumn("is_current", F.lit(True)).withColumn(
            "valid_to_ts", F.lit(None).cast("timestamp"))
        failed = self.corrupt(both_current)
        self.assertIn("dim_merchant_scd2.one_current_version_per_merchant", failed)
        self.assertIn("dim_merchant_scd2.contiguous_non_overlapping_intervals", failed)

    def test_invalid_current_flag_is_detected(self):
        flipped = lambda df: df.withColumn("is_current", ~F.col("is_current"))
        self.assertIn("dim_merchant_scd2.current_flag_matches_open_end", self.corrupt(flipped))

    def test_inverted_interval_and_duplicate_start_are_detected(self):
        inverted = lambda df: df.withColumn(
            "valid_to_ts",
            F.when(F.col("valid_to_ts").isNotNull(), F.col("valid_from_ts")).otherwise(None))
        self.assertIn("dim_merchant_scd2.valid_to_after_valid_from", self.corrupt(inverted))
        duplicated = lambda df: df.unionByName(df.filter(F.col("merchant_id") == "m2"))
        failed = self.corrupt(duplicated)
        self.assertIn("dim_merchant_scd2.unique_key(merchant_id,valid_from_ts)", failed)
        self.assertIn("dim_merchant_scd2.unique_key(merchant_sk)", failed)

    def test_redundant_versions_and_wrong_schema_are_detected(self):
        renamed = lambda df: df.withColumn("merchant_name", F.when(F.col("merchant_id") == "m1", "B")
                                           .otherwise(F.col("merchant_name")))
        self.assertIn("dim_merchant_scd2.no_redundant_versions", self.corrupt(renamed))
        self.assertEqual({"dim_merchant_scd2.schema"},
                         self.corrupt(lambda df: df.withColumn("valid_from_ts", F.col("valid_from_ts").cast("string"))))

    def test_16_current_version_must_match_the_current_state_dimension(self):
        stale = self.spark.createDataFrame([("m1", "Z", "food"), ("m2", "X", "retail"), ("m3", "Q", "food")],
                                           "merchant_id string, merchant_name string, category string")
        failed = self.failed(merchant_scd2_results(self.scd2, build_dim_merchant(stale)))
        self.assertIn("dim_merchant_scd2.current_version_equals_dim_merchant", failed)

    def test_history_rules_detect_bad_sources(self):
        conflict = self.snapshots.unionByName(self.history([("m1", "Q", "food", ts(1))]))
        observed = self.snapshots.withColumn("history_source", F.lit("observed"))
        drifted = self.snapshots.filter(~((F.col("merchant_id") == "m1") & (F.col("snapshot_ts") == ts(3))))
        self.assertIn("merchant_history.unique_key(merchant_id,snapshot_ts)",
                      self.failed(merchant_history_results(conflict, self.merchants)))
        self.assertIn("merchant_history.synthetic_provenance",
                      self.failed(merchant_history_results(observed, self.merchants)))
        self.assertIn("merchant_history.latest_snapshot_equals_merchants",
                      self.failed(merchant_history_results(drifted, self.merchants)))

    def test_silver_cleaner_drops_exact_duplicates_and_keeps_conflicts(self):
        raw = self.snapshots.unionByName(self.snapshots).unionByName(
            self.history([("m1", "Q", "food", ts(1))]).withColumn("snapshot_id", F.lit("m1-x")))
        cleaned = clean_merchant_history(raw)
        self.assertEqual(4, cleaned.count())
        with self.assertRaises(Scd2ContractError):
            build_dim_merchant_scd2(cleaned)


if __name__ == "__main__":
    unittest.main()
