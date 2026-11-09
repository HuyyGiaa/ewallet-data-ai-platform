"""Feature tables carry event_timestamp (business time) and created (materialization time)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from pyspark.sql import SparkSession, Window
from pyspark.sql import functions as F

from ai_platform.features.feast.schema import FEATURE_SPECS, feast_export_columns
from data_platform.processing.spark.gold.dimension_builder import build_dim_user
from data_platform.processing.spark.gold.feature_builder import build_feat_user_90d
from data_platform.processing.spark.gold.features.temporal import created_column
from data_platform.processing.spark.gold.historical_feature_pipeline import (
    feature_definitions,
    project_feature_transactions,
)
from data_platform.quality.feature_timestamp_quality import (
    PIT_PROBES,
    feature_timestamp_results,
    historical_feature_timestamp_results,
    pit_sample_result,
    shared_created_result,
)


CLOCK = datetime(2026, 11, 9, 8, 30, tzinfo=timezone.utc)
LATER = CLOCK + timedelta(hours=5)
TABLE_ENTITIES = {
    "feat_user_behavior": "user_id",
    "feat_account_behavior": "account_id",
    "feat_device_behavior": "device_id",
    "feat_merchant_behavior": "merchant_id",
}
FEAST_COLUMNS = {
    spec["table"]: [spec["entity_key"], "event_timestamp", *spec["features"]]
    for spec in FEATURE_SPECS.values()
}


def utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


class FeatureTimestampBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spark = (
            SparkSession.builder.master("local[1]").appName("feature-created-tests")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.session.timeZone", "UTC")
            .config("spark.sql.shuffle.partitions", "2")
            .getOrCreate()
        )
        cls.spark.sparkContext.setLogLevel("ERROR")
        t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        rows = [
            # id, user, account, device, merchant, type, status, amount, time
            ("t0", "u1", "a1", "d1", "m1", "payment", "success", 10.0, t0),
            ("t1", "u1", "a1", "d1", "m1", "payment", "failed", 20.0, t0 + timedelta(minutes=30)),
            ("t2", "u1", "a1", "d1", "m1", "payment", "success", 30.0, t0 + timedelta(minutes=60)),
            ("t3", "u1", "a2", "d1", "m1", "payment", "success", 40.0, t0 + timedelta(minutes=60)),
            ("t4", "u2", "a3", "d2", "m1", "payment", "success", 50.0, t0 + timedelta(minutes=61)),
            ("t5", "u2", "a3", "d2", None, "transfer", "success", 60.0, t0 + timedelta(minutes=90)),
            ("t6", "u1", "a1", "d1", "m2", "payment", "success", 70.0, t0 + timedelta(hours=3)),
        ]
        cls.transactions = cls.spark.createDataFrame(
            rows,
            "transaction_id string, user_id string, account_id string, device_id string, "
            "merchant_id string, type string, status string, amount double, timestamp timestamp",
        )
        cls.devices = cls.spark.createDataFrame(
            [("d1", "u1", t0 - timedelta(days=1)), ("d2", "u2", t0 - timedelta(days=2))],
            "device_id string, user_id string, first_seen_at timestamp",
        )
        cls.users = cls.spark.createDataFrame(
            [("u1", "A", "a@x", "1", True, t0), ("u2", "B", "b@x", "2", True, t0), ("u3", "C", "c@x", "3", False, t0)],
            "user_id string, full_name string, email string, phone string, kyc_verified boolean, created_at timestamp",
        )

    def tables(self, clock=CLOCK):
        projected = project_feature_transactions(self.transactions)
        return {name: frame for name, frame, _ in feature_definitions(
            projected, self.devices, materialized_at=clock)}

    def user_90d(self, clock=CLOCK):
        return build_feat_user_90d(self.transactions, build_dim_user(self.users), materialized_at=clock)

    def all_five(self, clock=CLOCK):
        return {**self.tables(clock), "feat_user_90d": self.user_90d(clock)}


class CreatedColumnTest(FeatureTimestampBase):
    def test_01_02_03_all_five_tables_have_typed_non_null_timestamps(self):
        for table, frame in self.all_five().items():
            with self.subTest(table):
                types = dict(frame.dtypes)
                self.assertEqual("timestamp", types["event_timestamp"])
                self.assertEqual("timestamp", types["created"])
                nulls = frame.filter(F.col("created").isNull() | F.col("event_timestamp").isNull()).count()
                self.assertEqual(0, nulls)

    def test_04_fixed_clock_is_reproducible_and_written_as_utc(self):
        for table, frame in self.all_five().items():
            with self.subTest(table):
                self.assertEqual(sorted(frame.collect()), sorted(self.all_five()[table].collect()))
                values = {utc(row.created) for row in frame.select("created").distinct().collect()}
                self.assertEqual({CLOCK}, values)
                epoch = frame.select(F.unix_micros("created").alias("us")).first().us
                self.assertEqual(int(CLOCK.timestamp() * 1_000_000), epoch)

    def test_05_created_comes_from_the_injected_clock_not_event_time(self):
        """Provenance, not inequality: with the clock before, equal to, or after
        the event times, every created equals the clock and every
        event_timestamp keeps its business time."""
        before = datetime(2025, 6, 1, tzinfo=timezone.utc)
        equal = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)  # t2/t3 event time
        after = datetime(2027, 1, 1, tzinfo=timezone.utc)
        baseline = {table: sorted(utc(row.event_timestamp) for row in frame.select("event_timestamp").collect())
                    for table, frame in self.all_five(CLOCK).items()}
        for clock in (before, equal, after):
            for table, frame in self.all_five(clock).items():
                with self.subTest(clock=clock, table=table):
                    self.assertEqual({clock}, {utc(row.created) for row in frame.select("created").collect()})
                    events = sorted(utc(row.event_timestamp) for row in frame.select("event_timestamp").collect())
                    self.assertEqual(baseline[table], events)
        equal_rows = self.tables(equal)["feat_user_behavior"].filter(F.col("created") == F.col("event_timestamp"))
        self.assertEqual(1, equal_rows.count(), "a row may legitimately have created = event_timestamp")

    def test_06_feature_values_do_not_depend_on_the_clock(self):
        for table, frame in self.all_five().items():
            with self.subTest(table):
                other = self.all_five(LATER)[table]
                feature_columns = [c for c in frame.columns if c not in ("created", "created_timestamp")]
                self.assertEqual(sorted(frame.select(feature_columns).collect()),
                                 sorted(other.select(feature_columns).collect()))
                self.assertNotEqual(frame.select("created").first(), other.select("created").first())

    def test_06b_persisted_schema_is_the_legacy_schema_plus_created(self):
        for table, frame in self.tables().items():
            with self.subTest(table):
                self.assertEqual(FEAST_COLUMNS[table] + ["created"], frame.columns)

    def test_07_entity_time_grain_stays_unique(self):
        for table, frame in self.tables().items():
            with self.subTest(table):
                key = [TABLE_ENTITIES[table], "event_timestamp"]
                self.assertEqual(frame.count(), frame.select(*key).distinct().count())

    def test_08_09_same_timestamp_peers_current_and_future_events_stay_excluded(self):
        rows = {(r.user_id, utc(r.event_timestamp)): r for r in self.tables()["feat_user_behavior"].collect()}
        t60 = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
        # t2 and t3 share 11:00: neither sees the other or itself; t0 is exactly
        # one hour earlier and falls inside [T-1h, T); t6 (future) never counts.
        self.assertEqual(2, rows[("u1", t60)].user_tx_count_1h)
        self.assertEqual(0, rows[("u1", datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))].user_tx_count_1h)
        merchant = {utc(r.event_timestamp): r for r in self.tables()["feat_merchant_behavior"].collect()
                    if r.merchant_id == "m1"}
        self.assertEqual(3, merchant[datetime(2026, 1, 1, 11, 1, tzinfo=timezone.utc)].merchant_tx_count_1h)

    def test_10_feat_user_90d_keeps_its_legacy_created_timestamp(self):
        frame = self.user_90d()
        self.assertEqual(0, frame.filter(~F.col("created").eqNullSafe(F.col("created_timestamp"))).count())
        self.assertEqual(3, frame.count())

    def test_naive_clock_is_refused(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware UTC"):
            created_column(datetime(2026, 1, 1))
        with self.assertRaisesRegex(ValueError, "timezone-aware UTC"):
            created_column(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=7))))

    def test_without_a_clock_spark_materialization_time_is_used(self):
        frame = feature_definitions(project_feature_transactions(self.transactions), self.devices)[0][1]
        self.assertEqual(1, frame.select("created").distinct().count())


class FeastCompatibilityTest(unittest.TestCase):
    def test_created_is_dropped_and_never_a_feast_feature(self):
        for table, columns in FEAST_COLUMNS.items():
            with self.subTest(table):
                self.assertEqual((), feast_export_columns(table, columns, columns))
                self.assertEqual(("created",), feast_export_columns(table, [*columns, "created"], columns))
                with self.assertRaises(ValueError):
                    feast_export_columns(table, ["created", *columns], columns)
                with self.assertRaises(ValueError):
                    feast_export_columns(table, columns[:-1], columns)
        features = [feature for spec in FEATURE_SPECS.values() for feature in spec["features"]]
        self.assertEqual(31, len(features))
        self.assertNotIn("created", features)
        self.assertNotIn("event_timestamp", features)


class FeatureValidatorTest(FeatureTimestampBase):
    def failed(self, results):
        return {result.rule_name for result in results if not result.passed}

    def clocks(self, clock=CLOCK):
        return {table: clock for table in TABLE_ENTITIES}

    def test_valid_outputs_pass_every_rule_including_pit(self):
        results = historical_feature_timestamp_results(self.tables(), self.transactions, sample_modulus=1,
                                                       materialization_clocks=self.clocks())
        self.assertEqual(set(), self.failed(results))
        self.assertEqual(4, sum(".pit_" in result.rule_name for result in results))
        self.assertEqual(4, sum("materialization_clock" in result.rule_name for result in results))
        self.assertEqual(set(), self.failed(feature_timestamp_results(
            self.user_90d(), "feat_user_90d", legacy_created_column="created_timestamp",
            materialization_clock=CLOCK, require_clock=True)))

    def test_created_before_equal_to_or_after_event_time_is_accepted(self):
        for clock in (datetime(2025, 6, 1, tzinfo=timezone.utc),
                      datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc),
                      datetime(2027, 1, 1, tzinfo=timezone.utc)):
            with self.subTest(clock=clock):
                results = historical_feature_timestamp_results(
                    self.tables(clock), self.transactions, sample_modulus=1,
                    materialization_clocks=self.clocks(clock))
                self.assertEqual(set(), self.failed(results))
                self.assertEqual(set(), self.failed(feature_timestamp_results(
                    self.user_90d(clock), "feat_user_90d", legacy_created_column="created_timestamp",
                    materialization_clock=clock, require_clock=True)))

    def test_a_builder_that_copies_event_time_is_detected_by_provenance(self):
        """feat_user_90d has one event_timestamp, so a copy has a single created
        value; only the recorded clock exposes it."""
        copied = self.user_90d().withColumn("created", F.col("event_timestamp"))
        failed = self.failed(feature_timestamp_results(copied, "feat_user_90d",
                                                       materialization_clock=CLOCK, require_clock=True))
        self.assertEqual({"feat_user_90d.created_matches_materialization_clock"}, failed)

    def test_a_missing_recorded_clock_fails_when_required(self):
        result = feature_timestamp_results(self.tables()["feat_user_behavior"], "feat_user_behavior",
                                           require_clock=True)
        failed = [item for item in result if not item.passed]
        self.assertEqual(["feat_user_behavior.created_matches_materialization_clock"],
                         [item.rule_name for item in failed])
        self.assertEqual("no recorded clock", failed[0].actual_value)

    def test_missing_created_is_detected(self):
        frame = self.tables()["feat_user_behavior"].drop("created")
        self.assertEqual({"feat_user_behavior.timestamp_columns_typed"},
                         self.failed(feature_timestamp_results(frame, "feat_user_behavior")))

    def test_null_created_is_detected(self):
        frame = self.tables()["feat_user_behavior"]
        frame = frame.withColumn("created", F.when(F.col("user_id") == "u2", None).otherwise(F.col("created")))
        self.assertIn("feat_user_behavior.timestamp_columns_not_null",
                      self.failed(feature_timestamp_results(frame, "feat_user_behavior")))

    def test_created_copied_from_event_time_is_detected(self):
        frame = self.tables()["feat_account_behavior"].withColumn("created", F.col("event_timestamp"))
        failed = self.failed(feature_timestamp_results(frame, "feat_account_behavior",
                                                       materialization_clock=CLOCK, require_clock=True))
        self.assertEqual({"feat_account_behavior.created_matches_materialization_clock",
                          "feat_account_behavior.created_single_materialization"}, failed)

    def test_string_typed_created_and_legacy_drift_are_detected(self):
        frame = self.tables()["feat_device_behavior"].withColumn("created", F.col("created").cast("string"))
        self.assertIn("feat_device_behavior.timestamp_columns_typed",
                      self.failed(feature_timestamp_results(frame, "feat_device_behavior")))
        drifted = self.user_90d().withColumn("created_timestamp", F.lit(LATER).cast("timestamp"))
        self.assertIn("feat_user_90d.created_matches_created_timestamp",
                      self.failed(feature_timestamp_results(drifted, "feat_user_90d",
                                                            legacy_created_column="created_timestamp")))

    def test_tables_from_different_materializations_are_detected(self):
        tables = self.tables()
        tables["feat_merchant_behavior"] = self.tables(LATER)["feat_merchant_behavior"]
        self.assertFalse(shared_created_result(tables, "dp3_features").passed)

    def test_changed_feature_value_breaks_the_pit_rule(self):
        frame = self.tables()["feat_user_behavior"].withColumn(
            "user_tx_count_1h",
            F.when(F.col("event_timestamp") == F.lit(datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)),
                   F.col("user_tx_count_1h") + 1).otherwise(F.col("user_tx_count_1h")))
        result = pit_sample_result(frame, self.transactions, PIT_PROBES[0], sample_modulus=1)
        self.assertFalse(result.passed)
        self.assertIn("mismatches=1", result.actual_value)

    def test_inclusive_upper_bound_breaks_the_pit_rule(self):
        """A builder that counts [T-1h, T] (current row and peers) is caught."""
        events = self.transactions.withColumn("_us", F.unix_micros("timestamp"))
        leaky = Window.partitionBy("user_id").orderBy("_us").rangeBetween(-3600 * 1_000_000, 0)
        frame = (events.select("user_id", F.col("timestamp").alias("event_timestamp"),
                               F.count(F.lit(1)).over(leaky).cast("long").alias("user_tx_count_1h"))
                 .dropDuplicates(["user_id", "event_timestamp"]))
        self.assertFalse(pit_sample_result(frame, self.transactions, PIT_PROBES[0], sample_modulus=1).passed)

    def test_merchant_probe_counts_payments_only(self):
        frame = self.tables()["feat_merchant_behavior"]
        self.assertTrue(pit_sample_result(frame, self.transactions, PIT_PROBES[3], sample_modulus=1).passed)



class MaterializationMetadataTest(unittest.TestCase):
    def test_round_trip_and_malformed_metadata(self):
        from data_platform.processing.spark.gold.materialization import (
            materialization_metadata,
            parse_materialization_metadata,
        )

        self.assertEqual(CLOCK, parse_materialization_metadata(materialization_metadata(CLOCK)))
        for value in (None, "", "not json", "[]", '{"other": 1}'):
            with self.subTest(value):
                self.assertIsNone(parse_materialization_metadata(value))
        with self.assertRaises(ValueError):
            materialization_metadata(datetime(2026, 1, 1))

if __name__ == "__main__":
    unittest.main()
