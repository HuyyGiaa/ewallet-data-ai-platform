from __future__ import annotations

import unittest

import pandas as pd

from ai_platform.ml.training.build import assemble_training_frame
from ai_platform.ml.training.contract import (
    FORBIDDEN_MODEL_COLUMNS,
    HISTORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
)
from ai_platform.ml.training.source import join_transactions_labels
from ai_platform.ml.training.temporal import (
    TEST_START,
    VALIDATION_START,
    assign_temporal_split,
)


def source_frame(rows: int = 1, merchant_id: str | None = "m1") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "transaction_id": [f"tx-{index}" for index in range(rows)],
            "event_timestamp": [
                pd.Timestamp("2026-03-01 00:00:00", tz="UTC")
            ]
            * rows,
            "user_id": ["u1"] * rows,
            "account_id": ["a1"] * rows,
            "device_id": ["d1"] * rows,
            "merchant_id": [merchant_id] * rows,
            "amount": [120.0] * rows,
            "type": ["payment" if merchant_id else "transfer"] * rows,
            "channel": ["mobile"] * rows,
            "merchant_present": [merchant_id is not None] * rows,
            "label": [0] * rows,
            "fraud_type": [None] * rows,
            "split": ["train"] * rows,
        }
    )


def historical_frame(rows: int = 1) -> pd.DataFrame:
    frame = pd.DataFrame(
        {column: [1.0] * rows for column in HISTORICAL_FEATURE_COLUMNS}
    )
    frame["user_behavior__user_avg_amount_30d"] = 60.0
    frame["user_behavior__user_std_amount_30d"] = 30.0
    frame["account_behavior__account_avg_amount_30d"] = 40.0
    frame["account_behavior__account_std_amount_30d"] = 20.0
    frame["user_behavior__user_tx_count_1h"] = 4
    frame["account_behavior__account_tx_count_1h"] = 2
    frame["merchant_behavior__merchant_tx_count_10m"] = 2
    frame["merchant_behavior__merchant_tx_count_24h"] = 288
    return frame


class FraudTrainingDatasetTest(unittest.TestCase):
    def test_one_transaction_produces_one_training_row(self):
        result = assemble_training_frame(source_frame(), historical_frame())
        self.assertEqual(1, len(result))
        self.assertEqual("tx-0", result.iloc[0].transaction_id)

    def test_same_timestamp_peers_remain_separate_rows(self):
        result = assemble_training_frame(
            source_frame(rows=2), historical_frame(rows=2)
        )
        self.assertEqual(2, len(result))
        self.assertEqual({"tx-0", "tx-1"}, set(result.transaction_id))
        self.assertEqual(1, result.event_timestamp.nunique())

    def test_label_join_is_exact(self):
        transactions = pd.DataFrame(
            {"transaction_id": ["tx-1", "tx-2"], "amount": [1.0, 2.0]}
        )
        labels = pd.DataFrame(
            {
                "transaction_id": ["tx-2", "tx-1"],
                "label": [1, 0],
                "fraud_type": ["velocity", None],
            }
        )
        result = join_transactions_labels(transactions, labels)
        self.assertEqual([0, 1], result.sort_values("transaction_id").label.tolist())

    def test_blacklisted_fields_are_not_model_features(self):
        source = source_frame()
        source["status"] = "success"
        source["new_balance"] = 1.0
        source["ingested_at"] = source["event_timestamp"]
        result = assemble_training_frame(source, historical_frame())
        self.assertFalse(FORBIDDEN_MODEL_COLUMNS.intersection(MODEL_FEATURE_COLUMNS))
        self.assertFalse({"status", "new_balance", "ingested_at"}.intersection(result.columns))

    def test_derived_ratios_are_correct(self):
        row = assemble_training_frame(source_frame(), historical_frame()).iloc[0]
        self.assertEqual(2.0, row.amount_ratio_to_user_avg_30d)
        self.assertEqual(3.0, row.amount_ratio_to_account_avg_30d)
        self.assertEqual(0.5, row.account_tx_share_1h)
        self.assertEqual(1.0, row.merchant_activity_rate_ratio_10m_vs_24h)

    def test_derived_zscores_are_correct(self):
        row = assemble_training_frame(source_frame(), historical_frame()).iloc[0]
        self.assertEqual(2.0, row.amount_zscore_user_30d)
        self.assertEqual(4.0, row.amount_zscore_account_30d)

    def test_zero_or_missing_denominator_returns_null(self):
        historical = historical_frame()
        historical["user_behavior__user_avg_amount_30d"] = None
        historical["account_behavior__account_std_amount_30d"] = 0.0
        historical["user_behavior__user_tx_count_1h"] = 0
        result = assemble_training_frame(source_frame(), historical).iloc[0]
        self.assertTrue(pd.isna(result.amount_ratio_to_user_avg_30d))
        self.assertTrue(pd.isna(result.amount_zscore_account_30d))
        self.assertTrue(pd.isna(result.account_tx_share_1h))

    def test_merchant_null_row_is_retained(self):
        source = source_frame(merchant_id=None)
        historical = historical_frame()
        for column in historical.columns:
            if column.startswith("merchant_behavior__"):
                historical[column] = None
        result = assemble_training_frame(source, historical)
        self.assertEqual(1, len(result))
        self.assertTrue(pd.isna(result.iloc[0].merchant_id))
        self.assertTrue(
            pd.isna(result.iloc[0].merchant_activity_rate_ratio_10m_vs_24h)
        )

    def test_temporal_split_has_no_overlap_and_is_chronological(self):
        self.assertEqual(
            pd.Timestamp("2026-04-16 12:14:11.022705", tz="UTC"),
            VALIDATION_START,
        )
        self.assertEqual(
            pd.Timestamp("2026-05-09 02:38:44.058669", tz="UTC"),
            TEST_START,
        )
        timestamps = pd.Series(
            [
                VALIDATION_START - pd.Timedelta(microseconds=1),
                VALIDATION_START,
                TEST_START - pd.Timedelta(microseconds=1),
                TEST_START,
            ]
        )
        self.assertEqual(
            ["train", "validation", "validation", "test"],
            assign_temporal_split(timestamps).tolist(),
        )

    def test_temporal_split_is_deterministic(self):
        timestamps = pd.Series(
            [TEST_START, VALIDATION_START, VALIDATION_START, TEST_START]
        )
        first = assign_temporal_split(timestamps)
        second = assign_temporal_split(timestamps.sample(frac=1, random_state=7)).sort_index()
        self.assertEqual(first.tolist(), second.tolist())


if __name__ == "__main__":
    unittest.main()
