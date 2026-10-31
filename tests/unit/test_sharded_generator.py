from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from data_platform.generation.src.offline.offline_generator import load_config
from data_platform.generation.src.sharded.generator import generate_shard
from data_platform.generation.src.sharded.models import ShardManifest
from data_platform.generation.src.sharded.planner import (
    ShardPlanError,
    build_shard_plan,
    derive_shard_seed,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRAUD_DEV_CONFIG = (
    PROJECT_ROOT / "data_platform/generation/config/settings_fraud_dev.yaml"
)


def small_config() -> dict:
    config = copy.deepcopy(load_config(FRAUD_DEV_CONFIG))
    config.update(
        {
            "n_users": 30,
            "n_merchants": 8,
            "n_transactions": 600,
            "random_seed": 42,
        }
    )
    config["fraud"]["random_seed"] = 1042
    return config


class ShardPlannerTest(unittest.TestCase):
    def test_same_inputs_produce_same_plan_digest_and_seed(self) -> None:
        config = small_config()
        first = build_shard_plan(
            base_config=config,
            dataset_version="r4-test-v1",
            global_seed=7001,
            shard_count=2,
            users_per_shard=30,
            merchants_per_shard=8,
            transactions_per_shard=600,
        )
        second = build_shard_plan(
            base_config=copy.deepcopy(config),
            dataset_version="r4-test-v1",
            global_seed=7001,
            shard_count=2,
            users_per_shard=30,
            merchants_per_shard=8,
            transactions_per_shard=600,
        )

        self.assertEqual(first, second)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(
            derive_shard_seed(first, 0),
            derive_shard_seed(second, 0),
        )

    def test_version_and_shard_id_are_part_of_seed(self) -> None:
        config = small_config()
        first = build_shard_plan(
            config, "r4-test-v1", 7001, 2, 30, 8, 600
        )
        second = build_shard_plan(
            config, "r4-test-v2", 7001, 2, 30, 8, 600
        )

        self.assertNotEqual(
            derive_shard_seed(first, 0),
            derive_shard_seed(first, 1),
        )
        self.assertNotEqual(
            derive_shard_seed(first, 0),
            derive_shard_seed(second, 0),
        )

    def test_invalid_plans_and_shard_ids_fail_closed(self) -> None:
        config = small_config()
        invalid_cases = (
            {"dataset_version": ""},
            {"dataset_version": "../../escape"},
            {"shard_count": 0},
            {"users_per_shard": 0},
            {"merchants_per_shard": 0},
            {"transactions_per_shard": 0},
        )
        defaults = {
            "dataset_version": "r4-test-v1",
            "global_seed": 7001,
            "shard_count": 2,
            "users_per_shard": 30,
            "merchants_per_shard": 8,
            "transactions_per_shard": 600,
        }
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides):
                values = defaults | overrides
                with self.assertRaises(ShardPlanError):
                    build_shard_plan(base_config=config, **values)

        plan = build_shard_plan(base_config=config, **defaults)
        with self.assertRaises(ShardPlanError):
            derive_shard_seed(plan, -1)
        with self.assertRaises(ShardPlanError):
            derive_shard_seed(plan, 2)


class ShardedGenerationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = small_config()
        cls.plan = build_shard_plan(
            base_config=cls.config,
            dataset_version="r4-test-v1",
            global_seed=7001,
            shard_count=2,
            users_per_shard=30,
            merchants_per_shard=8,
            transactions_per_shard=600,
        )
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp_dir.name)
        cls.manifests = [
            generate_shard(cls.config, cls.plan, shard_id, cls.root)
            for shard_id in range(cls.plan.shard_count)
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def read(self, shard_id: int, table: str) -> pd.DataFrame:
        return pd.read_parquet(
            self.root / f"shard-{shard_id:05d}" / f"{table}.parquet"
        )

    def test_generation_returns_metadata_not_dataframes(self) -> None:
        for manifest in self.manifests:
            self.assertIsInstance(manifest, ShardManifest)
            self.assertEqual(9, len(manifest.files))
            self.assertTrue(all(item.rows >= 0 for item in manifest.files))
            self.assertTrue(all(item.bytes > 0 for item in manifest.files))
            self.assertTrue(all(len(item.sha256) == 64 for item in manifest.files))

    def test_retry_in_fresh_directory_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as second_directory:
            repeated = generate_shard(
                self.config,
                self.plan,
                0,
                Path(second_directory),
            )

        first_files = {
            item.table: (item.rows, item.bytes, item.sha256, item.schema_sha256)
            for item in self.manifests[0].files
        }
        repeated_files = {
            item.table: (item.rows, item.bytes, item.sha256, item.schema_sha256)
            for item in repeated.files
        }
        self.assertEqual(first_files, repeated_files)
        self.assertEqual(
            self.manifests[0].fraud_scenario_counts,
            repeated.fraud_scenario_counts,
        )

    def test_shards_have_disjoint_primary_keys(self) -> None:
        primary_keys = {
            "users": "user_id",
            "accounts": "account_id",
            "devices": "device_id",
            "merchants": "merchant_id",
            "fraud_labels": "transaction_id",
        }
        for table, key in primary_keys.items():
            first = set(self.read(0, table)[key].astype(str))
            second = set(self.read(1, table)[key].astype(str))
            self.assertFalse(first.intersection(second), table)

        first_transactions = pd.concat(
            [self.read(0, "transactions_v1"), self.read(0, "transactions_v2")]
        )
        second_transactions = pd.concat(
            [self.read(1, "transactions_v1"), self.read(1, "transactions_v2")]
        )
        self.assertFalse(
            set(first_transactions["transaction_id"].astype(str)).intersection(
                second_transactions["transaction_id"].astype(str)
            )
        )

    def test_references_are_resolved_inside_each_shard(self) -> None:
        for shard_id in range(self.plan.shard_count):
            users = self.read(shard_id, "users")
            accounts = self.read(shard_id, "accounts")
            devices = self.read(shard_id, "devices")
            merchants = self.read(shard_id, "merchants")
            labels = self.read(shard_id, "fraud_labels")
            transactions = pd.concat(
                [
                    self.read(shard_id, "transactions_v1"),
                    self.read(shard_id, "transactions_v2"),
                ],
                ignore_index=True,
            )

            user_ids = set(users["user_id"].astype(str))
            account_ids = set(accounts["account_id"].astype(str))
            device_ids = set(devices["device_id"].astype(str))
            merchant_ids = set(merchants["merchant_id"].astype(str))
            transaction_ids = set(transactions["transaction_id"].astype(str))

            self.assertTrue(set(accounts["user_id"].astype(str)) <= user_ids)
            self.assertTrue(set(devices["user_id"].astype(str)) <= user_ids)
            self.assertTrue(set(transactions["user_id"].astype(str)) <= user_ids)
            self.assertTrue(
                set(transactions["account_id"].astype(str)) <= account_ids
            )
            self.assertTrue(
                set(transactions["device_id"].dropna().astype(str)) <= device_ids
            )
            self.assertTrue(
                set(transactions["merchant_id"].dropna().astype(str)) <= merchant_ids
            )
            self.assertTrue(set(labels["transaction_id"].astype(str)) <= transaction_ids)

    def test_schema_evolution_and_all_fraud_scenarios_are_preserved(self) -> None:
        expected_scenarios = {
            "velocity",
            "amount_anomaly",
            "account_takeover",
            "merchant_burst",
        }
        for shard_id, manifest in enumerate(self.manifests):
            v1 = self.read(shard_id, "transactions_v1")
            v2 = self.read(shard_id, "transactions_v2")
            self.assertGreater(len(v1), 0)
            self.assertGreater(len(v2), 0)
            self.assertNotIn("channel", v1.columns)
            self.assertIn("channel", v2.columns)
            self.assertEqual(
                expected_scenarios,
                set(manifest.fraud_scenario_counts),
            )
            self.assertTrue(
                all(count > 0 for count in manifest.fraud_scenario_counts.values())
            )

    def test_existing_or_unsafe_output_path_is_rejected(self) -> None:
        with self.assertRaises(ShardPlanError):
            generate_shard(self.config, self.plan, 0, Path("/"))
        with self.assertRaises(FileExistsError):
            generate_shard(self.config, self.plan, 0, self.root)


if __name__ == "__main__":
    unittest.main()
