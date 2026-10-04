from __future__ import annotations

import math
import unittest
from datetime import datetime

from pyspark.sql import SparkSession

from data_platform.processing.spark.gold.features import (
    build_feat_account_behavior,
    build_feat_device_behavior,
    build_feat_merchant_behavior,
    build_feat_user_behavior,
)


class HistoricalFraudFeatureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spark = (
            SparkSession.builder
            .master("local[2]")
            .appName("historical-feature-tests")
            .config("spark.ui.enabled", "false")
            .getOrCreate()
        )
        cls.spark.sparkContext.setLogLevel("ERROR")

    @classmethod
    def tearDownClass(cls):
        cls.spark.stop()

    def transactions(self):
        rows = [
            ("t0", "u1", "a1", "d1", "m1", "payment", "success", 10.0, datetime(2026, 1, 1, 10, 0, 0)),
            ("t1", "u1", "a1", "d1", "m2", "payment", "failed", 20.0, datetime(2026, 1, 1, 11, 30, 0)),
            ("t2", "u1", "a1", "d1", "m3", "payment", "success", 100.0, datetime(2026, 1, 1, 12, 0, 0)),
            ("t3", "u1", "a1", "d1", "m3", "payment", "failed", 200.0, datetime(2026, 1, 1, 12, 0, 0)),
            ("t4", "u1", "a1", "d1", "m4", "payment", "success", 1000.0, datetime(2026, 1, 1, 13, 0, 0)),
        ]
        return self.spark.createDataFrame(
            rows,
            "transaction_id string, user_id string, account_id string, "
            "device_id string, merchant_id string, type string, status string, "
            "amount double, timestamp timestamp",
        )

    def test_user_pit_same_timestamp_and_amount_semantics(self):
        rows = {
            row.event_timestamp: row
            for row in build_feat_user_behavior(self.transactions()).collect()
        }
        at_t = rows[datetime(2026, 1, 1, 12, 0, 0)]
        self.assertEqual(1, at_t.user_tx_count_1h)
        self.assertEqual(2, at_t.user_tx_count_24h)
        self.assertEqual(20.0, at_t.user_amount_sum_1h)
        self.assertEqual(2, at_t.user_amount_observation_count_30d)
        self.assertEqual(15.0, at_t.user_avg_amount_30d)
        self.assertTrue(math.isclose(at_t.user_std_amount_30d, math.sqrt(50.0)))
        self.assertEqual(0.5, at_t.user_failed_rate_24h)
        self.assertEqual(2, at_t.user_distinct_merchants_24h)
        self.assertEqual(4, len(rows))

    def test_future_does_not_affect_prior_snapshot(self):
        all_rows = build_feat_user_behavior(self.transactions())
        prior_rows = build_feat_user_behavior(
            self.transactions().filter("transaction_id <> 't4'")
        )
        key = datetime(2026, 1, 1, 12, 0, 0)
        self.assertEqual(
            all_rows.filter(all_rows.event_timestamp == key).collect()[0],
            prior_rows.filter(prior_rows.event_timestamp == key).collect()[0],
        )

    def test_account_previous_time_is_strict(self):
        rows = {
            row.event_timestamp: row
            for row in build_feat_account_behavior(self.transactions()).collect()
        }
        at_t = rows[datetime(2026, 1, 1, 12, 0, 0)]
        self.assertEqual(1800.0, at_t.account_seconds_since_last_tx)
        self.assertEqual(1, at_t.account_tx_count_1h)
        self.assertIsNone(rows[datetime(2026, 1, 1, 10, 0, 0)].account_seconds_since_last_tx)

    def test_cold_start_null_and_zero_semantics(self):
        first = (
            build_feat_user_behavior(self.transactions())
            .orderBy("event_timestamp")
            .first()
        )
        self.assertEqual(0, first.user_tx_count_5m)
        self.assertEqual(0.0, first.user_amount_sum_1h)
        self.assertEqual(0, first.user_amount_observation_count_30d)
        self.assertIsNone(first.user_avg_amount_30d)
        self.assertIsNone(first.user_std_amount_30d)
        self.assertIsNone(first.user_failed_rate_24h)

    def test_device_history_and_age(self):
        devices = self.spark.createDataFrame(
            [("d1", datetime(2026, 1, 1, 9, 0, 0))],
            "device_id string, first_seen_at timestamp",
        )
        rows = {
            row.event_timestamp: row
            for row in build_feat_device_behavior(self.transactions(), devices).collect()
        }
        at_t = rows[datetime(2026, 1, 1, 12, 0, 0)]
        self.assertEqual(10800.0, at_t.device_age_seconds)
        self.assertEqual(2, at_t.device_tx_count_24h)
        self.assertEqual(30.0, at_t.device_amount_sum_24h)

    def test_merchant_eligible_population_and_shared_snapshot(self):
        rows = build_feat_merchant_behavior(self.transactions()).collect()
        self.assertEqual(4, len(rows))
        at_t = next(
            row for row in rows
            if row.merchant_id == "m3"
            and row.event_timestamp == datetime(2026, 1, 1, 12, 0, 0)
        )
        self.assertEqual(0, at_t.merchant_tx_count_10m)
        self.assertEqual(0, at_t.merchant_unique_users_1h)
        self.assertIsNone(at_t.merchant_avg_amount_24h)


if __name__ == "__main__":
    unittest.main()
