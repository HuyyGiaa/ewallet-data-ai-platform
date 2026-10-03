"""Focused contract tests for Fraud Data Generator V1."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import yaml

from data_platform.generation.src.offline.fraud import FRAUD_TYPES, validate_fraud_config
from data_platform.generation.src.offline.offline_generator import (
    generate_offline_datasets,
    get_temporal_config,
    write_offline_datasets,
)


EXPECTED_PHASE1_SCHEMAS = {
    "users": [
        ("user_id", "string"), ("full_name", "string"), ("email", "string"),
        ("phone", "string"), ("kyc_verified", "bool"), ("created_at", "timestamp[us]"),
    ],
    "accounts": [
        ("account_id", "string"), ("user_id", "string"), ("account_type", "string"),
        ("currency", "string"), ("created_at", "timestamp[us]"),
    ],
    "merchants": [
        ("merchant_id", "string"), ("merchant_name", "string"), ("category", "string"),
    ],
    "devices": [
        ("device_id", "string"), ("user_id", "string"), ("device_type", "string"),
        ("os", "string"), ("first_seen_at", "timestamp[us]"),
    ],
    "transactions_v1": [
        ("transaction_id", "string"), ("account_id", "string"), ("user_id", "string"),
        ("device_id", "string"), ("type", "string"), ("amount", "double"),
        ("currency", "string"), ("status", "string"), ("old_balance", "double"),
        ("new_balance", "double"), ("merchant_id", "string"),
        ("counterparty_account_id", "string"), ("timestamp", "timestamp[us]"),
        ("ingested_at", "timestamp[us]"),
    ],
    "transactions_v2": [
        ("transaction_id", "string"), ("account_id", "string"), ("user_id", "string"),
        ("device_id", "string"), ("type", "string"), ("amount", "double"),
        ("currency", "string"), ("status", "string"), ("channel", "string"),
        ("old_balance", "double"), ("new_balance", "double"), ("merchant_id", "string"),
        ("counterparty_account_id", "string"), ("timestamp", "timestamp[us]"),
        ("ingested_at", "timestamp[us]"),
    ],
    "balance_snapshots": [
        ("account_id", "string"), ("snapshot_date", "date32[day]"),
        ("closing_balance", "double"),
    ],
    "login_events": [
        ("login_id", "string"), ("user_id", "string"), ("device_id", "string"),
        ("login_ts", "timestamp[us]"), ("is_success", "bool"),
    ],
}


def _test_config() -> dict:
    path = Path("data_platform/generation/config/settings_fraud_dev.yaml")
    with path.open(encoding="utf-8") as stream:
        cfg = yaml.safe_load(stream)
    cfg.update({"n_users": 30, "n_merchants": 8, "n_transactions": 600})
    cfg["fraud"]["prevalence"]["target_rate"] = 0.08
    return cfg


class FraudGeneratorContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = _test_config()
        cls.datasets, cls.metrics = generate_offline_datasets(copy.deepcopy(cls.cfg))
        cls.transactions = pd.concat(
            [cls.datasets["transactions_v1"], cls.datasets["transactions_v2"]],
            ignore_index=True,
        )
        cls.logical_transactions = cls.transactions.drop_duplicates("transaction_id")
        cls.labels = cls.datasets["fraud_labels"]

    def test_temporal_config_invariant_and_invalid_config(self):
        temporal = get_temporal_config(self.cfg)
        self.assertLess(temporal.start, temporal.cutover)
        self.assertLess(temporal.cutover, temporal.end)

        invalid = copy.deepcopy(self.cfg)
        invalid["schema_evolution"]["cutover_timestamp"] = invalid["generation"]["temporal"][
            "end_timestamp"
        ]
        with self.assertRaisesRegex(ValueError, "temporal invariant"):
            get_temporal_config(invalid)

    def test_v1_v2_nonempty_and_phase1_schemas_unchanged(self):
        self.assertFalse(self.datasets["transactions_v1"].empty)
        self.assertFalse(self.datasets["transactions_v2"].empty)
        self.assertNotIn("channel", self.datasets["transactions_v1"].columns)
        self.assertIn("channel", self.datasets["transactions_v2"].columns)

        with tempfile.TemporaryDirectory() as directory:
            write_offline_datasets(self.datasets, Path(directory))
            for name, expected in EXPECTED_PHASE1_SCHEMAS.items():
                actual = [(field.name, str(field.type)) for field in pq.read_schema(Path(directory) / f"{name}.parquet")]
                self.assertEqual(expected, actual, name)

    def test_label_contract_and_transaction_fk(self):
        self.assertTrue(self.labels["transaction_id"].notna().all())
        self.assertFalse(self.labels["transaction_id"].duplicated().any())
        self.assertTrue(self.labels["label"].notna().all())
        self.assertEqual({0, 1}, set(self.labels["label"].unique()))
        self.assertTrue(self.labels.loc[self.labels["label"] == 0, "fraud_type"].isna().all())
        self.assertTrue(
            set(self.labels.loc[self.labels["label"] == 1, "fraud_type"]).issubset(FRAUD_TYPES)
        )
        self.assertEqual(
            set(self.labels["transaction_id"]),
            set(self.logical_transactions["transaction_id"]),
        )

    def test_duplicates_do_not_duplicate_labels(self):
        self.assertGreater(len(self.transactions), self.transactions["transaction_id"].nunique())
        self.assertEqual(len(self.labels), self.transactions["transaction_id"].nunique())

    def test_relational_foreign_keys_and_transaction_domains(self):
        users = set(self.datasets["users"]["user_id"])
        accounts = set(self.datasets["accounts"]["account_id"])
        devices = set(self.datasets["devices"]["device_id"])
        merchants = set(self.datasets["merchants"]["merchant_id"])
        tx = self.logical_transactions

        self.assertTrue(set(tx["user_id"]).issubset(users))
        self.assertTrue(set(tx["account_id"]).issubset(accounts))
        self.assertTrue(set(tx["device_id"].dropna()).issubset(devices))
        self.assertFalse(self.datasets["devices"]["device_id"].duplicated().any())
        self.assertTrue(set(self.datasets["devices"]["user_id"]).issubset(users))
        self.assertTrue(set(tx["merchant_id"].dropna()).issubset(merchants))
        self.assertTrue(set(tx["counterparty_account_id"].dropna()).issubset(accounts))
        self.assertTrue(tx.loc[tx["type"] == "payment", "merchant_id"].notna().all())
        self.assertTrue(tx.loc[tx["type"] == "transfer", "counterparty_account_id"].notna().all())
        self.assertTrue(tx.loc[tx["type"] != "payment", "merchant_id"].isna().all())
        self.assertTrue(tx.loc[tx["type"] != "transfer", "counterparty_account_id"].isna().all())

        account_owner = self.datasets["accounts"].set_index("account_id")["user_id"]
        self.assertTrue((tx["account_id"].map(account_owner) == tx["user_id"]).all())
        device_owner = self.datasets["devices"].set_index("device_id")["user_id"]
        self.assertTrue((tx["device_id"].map(device_owner) == tx["user_id"]).all())
        login_events = self.datasets["login_events"]
        self.assertTrue(
            (login_events["device_id"].map(device_owner) == login_events["user_id"]).all()
        )
        self.assertEqual({"VND"}, set(tx["currency"]))
        self.assertTrue(set(tx["status"]).issubset({"success", "failed", "pending"}))
        self.assertTrue(set(self.datasets["transactions_v2"]["channel"]).issubset({"app", "web", "atm"}))

    def test_row_level_balance_semantics(self):
        joined = self.logical_transactions.merge(
            self.labels,
            on="transaction_id",
            validate="one_to_one",
        )

        def is_consistent(row):
            if row["type"] == "deposit":
                return (
                    row["status"] in {"success", "pending"}
                    and abs(row["new_balance"] - (row["old_balance"] + row["amount"])) < 1e-6
                )
            if row["status"] == "failed":
                return (
                    row["amount"] > row["old_balance"]
                    and abs(row["new_balance"] - row["old_balance"]) < 1e-6
                )
            return (
                row["status"] in {"success", "pending"}
                and row["amount"] <= row["old_balance"] + 1e-6
                and abs(row["new_balance"] - (row["old_balance"] - row["amount"])) < 1e-6
            )

        consistent = joined.apply(is_consistent, axis=1)
        self.assertTrue(consistent[joined["label"] == 0].all())
        self.assertTrue(consistent[joined["label"] == 1].all())

    def test_account_takeover_uses_recent_device_owned_by_victim(self):
        takeover_ids = set(
            self.labels.loc[self.labels["fraud_type"] == "account_takeover", "transaction_id"]
        )
        takeover = self.logical_transactions[
            self.logical_transactions["transaction_id"].isin(takeover_ids)
        ].merge(self.datasets["devices"], on="device_id", suffixes=("_transaction", "_device"))

        self.assertGreater(len(takeover), 0)
        self.assertTrue((takeover["user_id_transaction"] == takeover["user_id_device"]).all())
        self.assertTrue((takeover["first_seen_at"] <= takeover["timestamp"]).all())
        self.assertEqual(
            self.metrics["account_takeover_new_devices"],
            takeover["device_id"].nunique(),
        )

    def test_no_target_leakage_and_timestamp_bounds(self):
        for name, dataframe in self.datasets.items():
            if name == "fraud_labels":
                continue
            leaked = [
                column for column in dataframe.columns
                if any(token in column.lower() for token in ("fraud", "label", "scenario"))
            ]
            self.assertEqual([], leaked, name)

        temporal = get_temporal_config(self.cfg)
        timestamps = pd.to_datetime(self.transactions["timestamp"])
        self.assertGreaterEqual(timestamps.min(), pd.Timestamp(temporal.start))
        self.assertLessEqual(timestamps.max(), pd.Timestamp(temporal.end))
        self.assertLess(self.datasets["transactions_v1"]["timestamp"].max(), temporal.cutover)
        self.assertGreaterEqual(self.datasets["transactions_v2"]["timestamp"].min(), temporal.cutover)

    def test_fraud_is_not_mechanically_failed_status(self):
        joined = self.logical_transactions.merge(self.labels, on="transaction_id", validate="one_to_one")
        failed_indicator = joined["status"].eq("failed").astype("int8")
        self.assertFalse(failed_indicator.equals(joined["label"]))
        self.assertTrue(((joined["label"] == 1) & (joined["status"] == "success")).any())
        self.assertTrue(((joined["label"] == 0) & (joined["status"] == "failed")).any())

    def test_fraud_generation_is_reproducible(self):
        repeated, repeated_metrics = generate_offline_datasets(copy.deepcopy(self.cfg))
        for name in self.datasets:
            pd.testing.assert_frame_equal(self.datasets[name], repeated[name])
        self.assertEqual(self.metrics, repeated_metrics)

    def test_scenario_weight_validation(self):
        invalid = copy.deepcopy(self.cfg)
        invalid["fraud"]["scenarios"]["velocity"]["weight"] = 0.40
        with self.assertRaisesRegex(ValueError, "weights must sum to 1.0"):
            validate_fraud_config(invalid)

    def test_velocity_events_are_concentrated(self):
        durations = self.metrics["velocity_burst_durations_seconds"]
        configured_seconds = self.cfg["fraud"]["scenarios"]["velocity"]["window_minutes"] * 60
        self.assertGreater(len(durations), 0)
        self.assertTrue(all(duration <= configured_seconds for duration in durations))
        self.assertEqual(
            self.metrics["scenario_counts"]["velocity"],
            int((self.labels["fraud_type"] == "velocity").sum()),
        )

    def test_disabled_mode_keeps_stable_all_normal_label_output(self):
        disabled = copy.deepcopy(self.cfg)
        disabled["fraud"]["enabled"] = False
        datasets, metrics = generate_offline_datasets(disabled)
        self.assertEqual(9, len(datasets))
        self.assertEqual({0}, set(datasets["fraud_labels"]["label"]))
        self.assertTrue(datasets["fraud_labels"]["fraud_type"].isna().all())
        self.assertEqual(0, metrics["target_count"])


if __name__ == "__main__":
    unittest.main()
