"""The synthetic merchant_history source extension: generation, manifest, ingestion contract."""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from data_platform.config.dataset_identity import (
    CANONICAL_100GB,
    REHEARSAL_5GB,
    dataset_has_merchant_history,
)
from data_platform.generation.src.offline.merchant_history import (
    HISTORY_COLUMNS,
    generate_merchant_history,
    merchant_history_enabled,
    snapshot_times,
    validate_merchant_history_config,
)
from data_platform.generation.src.offline.offline_generator import (
    generate_offline_datasets,
    get_temporal_config,
)
from data_platform.generation.src.sharded.generator import (
    LEGACY_PHYSICAL_TABLES,
    generate_shard,
    physical_tables,
)
from data_platform.generation.src.sharded.manifest import DatasetPublisher
from data_platform.generation.src.sharded.planner import build_shard_plan
from data_platform.storage.scripts.config import core_gold_tables, silver_tables
from ops.gcp import submit_bronze_to_silver, submit_silver_to_gold
from ops.gcp.validate_spark_output_equivalence import expected_tables
from tests.unit.test_dataset_isolation import ATTEMPT, BENCH, runtime
from tests.unit.test_source_dataset_publisher import FakeObjectStore


ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "data_platform/generation/config"
REHEARSAL_BUCKET = "ewallet-rehearsal-5gb-data"


def load(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def small(config: dict, seed: int = 7) -> dict:
    config = copy.deepcopy(config)
    config.update({"n_users": 40, "n_merchants": 12, "n_transactions": 600, "random_seed": seed})
    config["fraud"]["random_seed"] = seed
    return config


class GeneratorConfigTest(unittest.TestCase):
    def test_history_config_is_settings_plus_only_the_extension_block(self):
        legacy, extended = load("settings.yaml"), load("settings_merchant_history.yaml")
        self.assertEqual({"merchant_history"}, set(extended) - set(legacy))
        extended.pop("merchant_history")
        self.assertEqual(legacy, extended)

    def test_only_the_extension_config_enables_history(self):
        self.assertFalse(merchant_history_enabled(load("settings.yaml")))
        self.assertTrue(merchant_history_enabled(load("settings_merchant_history.yaml")))
        self.assertEqual(LEGACY_PHYSICAL_TABLES, physical_tables(load("settings.yaml")))
        self.assertEqual(LEGACY_PHYSICAL_TABLES + ("merchant_history",),
                         physical_tables(load("settings_merchant_history.yaml")))

    def test_invalid_history_settings_are_refused(self):
        for section in ({"snapshot_interval_days": 0, "one_change_probability": 0.1, "two_change_probability": 0.1},
                        {"snapshot_interval_days": 30, "one_change_probability": 0.8, "two_change_probability": 0.3},
                        {"snapshot_interval_days": 30, "one_change_probability": -1, "two_change_probability": 0}):
            with self.subTest(section), self.assertRaises(ValueError):
                validate_merchant_history_config({"merchant_history": section})


class MerchantHistoryGenerationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy, _ = generate_offline_datasets(small(load("settings.yaml")))
        cls.extended, _ = generate_offline_datasets(small(load("settings_merchant_history.yaml")))
        cls.repeat, _ = generate_offline_datasets(small(load("settings_merchant_history.yaml")))
        cls.other_seed, _ = generate_offline_datasets(small(load("settings_merchant_history.yaml"), seed=8))
        cls.history = cls.extended["merchant_history"]
        cls.temporal = get_temporal_config(load("settings.yaml"))

    def test_the_nine_legacy_tables_are_identical_with_or_without_history(self):
        self.assertEqual(LEGACY_PHYSICAL_TABLES, tuple(self.legacy))
        self.assertEqual(LEGACY_PHYSICAL_TABLES + ("merchant_history",), tuple(self.extended))
        for table in LEGACY_PHYSICAL_TABLES:
            with self.subTest(table):
                pd.testing.assert_frame_equal(self.legacy[table], self.extended[table])

    def test_fixed_seed_reproduces_the_history_and_another_seed_does_not(self):
        pd.testing.assert_frame_equal(self.history, self.repeat["merchant_history"])
        self.assertFalse(self.history.equals(self.other_seed["merchant_history"]))

    def test_schema_identity_and_provenance(self):
        self.assertEqual(list(HISTORY_COLUMNS), list(self.history.columns))
        self.assertTrue(self.history["snapshot_id"].is_unique)
        self.assertFalse(self.history.duplicated(["merchant_id", "snapshot_ts"]).any())
        self.assertEqual({"synthetic"}, set(self.history["history_source"]))
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(self.history["snapshot_ts"]))

    def test_latest_snapshot_equals_the_current_merchant_state(self):
        latest = (self.history.sort_values("snapshot_ts").groupby("merchant_id").tail(1)
                  .set_index("merchant_id")[["merchant_name", "category"]].sort_index())
        current = self.extended["merchants"].set_index("merchant_id")[["merchant_name", "category"]].sort_index()
        pd.testing.assert_frame_equal(current, latest)

    def test_every_merchant_has_one_snapshot_per_scheduled_time_inside_the_event_domain(self):
        times = snapshot_times(self.temporal.start, self.temporal.end, 30)
        self.assertEqual(self.temporal.start, times[0])
        self.assertTrue(all(self.temporal.start <= item <= self.temporal.end for item in times))
        counts = self.history.groupby("merchant_id")["snapshot_ts"].nunique()
        self.assertEqual(set(self.extended["merchants"]["merchant_id"]), set(counts.index))
        self.assertEqual({len(times)}, set(counts))
        first_tx = min(self.extended["transactions_v1"]["timestamp"].min(),
                       self.extended["transactions_v2"]["timestamp"].min())
        self.assertLessEqual(pd.Timestamp(times[0]), first_tx)

    def test_history_contains_real_changes_and_repeated_unchanged_snapshots(self):
        ordered = self.history.sort_values(["merchant_id", "snapshot_ts"])
        state = ordered["merchant_name"] + "|" + ordered["category"]
        changed = state.ne(state.groupby(ordered["merchant_id"]).shift()) & ordered["merchant_id"].duplicated()
        self.assertGreater(changed.sum(), 0, "some merchants must change attributes")
        self.assertGreater((~changed & ordered["merchant_id"].duplicated()).sum(), 0,
                           "unchanged repeats must exist for the SCD2 collapse")

    def test_snapshot_ids_are_content_derived(self):
        merchants = pd.DataFrame({"merchant_id": ["m1"], "merchant_name": ["A"], "category": ["food"]})
        config = {"random_seed": 1, "merchant_history": {"snapshot_interval_days": 30,
                                                         "one_change_probability": 0.5,
                                                         "two_change_probability": 0.5}}
        first = generate_merchant_history(merchants, config, datetime(2026, 1, 1), datetime(2026, 3, 1))
        config["random_seed"] = 2
        second = generate_merchant_history(merchants, config, datetime(2026, 1, 1), datetime(2026, 3, 1))
        self.assertEqual(list(first["snapshot_id"]), list(second["snapshot_id"]))


class ShardAndManifestTest(unittest.TestCase):
    def publish(self, config: dict) -> tuple[DatasetPublisher, dict]:
        plan = build_shard_plan(config, REHEARSAL_5GB, 7001, 1, 30, 6, 300)
        store = FakeObjectStore()
        publisher = DatasetPublisher(store, "gs://rehearsal/source/x", plan,
                                     physical_tables=physical_tables(config))
        publisher.ensure_plan()
        with tempfile.TemporaryDirectory() as directory:
            manifest = generate_shard(config, plan, 0, Path(directory))
            publisher.publish_shard(Path(directory) / "shard-00000", manifest, cleanup_local=True)
        return publisher, publisher.finalize()

    def test_extended_shards_publish_ten_tables_and_declare_synthetic_provenance(self):
        _, manifest = self.publish(load("settings_merchant_history.yaml"))
        self.assertEqual(set(LEGACY_PHYSICAL_TABLES) | {"merchant_history"}, set(manifest["tables"]))
        self.assertEqual("synthetic", manifest["source_extensions"]["merchant_history"]["provenance"])

    def test_legacy_manifests_carry_no_extension_key(self):
        _, manifest = self.publish(load("settings.yaml"))
        self.assertEqual(set(LEGACY_PHYSICAL_TABLES), set(manifest["tables"]))
        self.assertNotIn("source_extensions", manifest)

    def test_a_publisher_refuses_shards_with_another_table_contract(self):
        config = load("settings_merchant_history.yaml")
        plan = build_shard_plan(config, REHEARSAL_5GB, 7001, 1, 30, 6, 300)
        publisher = DatasetPublisher(FakeObjectStore(), "gs://rehearsal/source/x", plan)
        publisher.ensure_plan()
        with tempfile.TemporaryDirectory() as directory:
            manifest = generate_shard(config, plan, 0, Path(directory))
            with self.assertRaisesRegex(Exception, "physical tables changed"):
                publisher.publish_shard(Path(directory) / "shard-00000", manifest, cleanup_local=False)
        with self.assertRaises(ValueError):
            DatasetPublisher(FakeObjectStore(), "gs://x/y", plan, physical_tables=("users",))


class BronzeManifestContractTest(unittest.TestCase):
    """Source manifest validation; source_to_bronze imports pyspark."""

    @classmethod
    def setUpClass(cls):
        try:
            from data_platform.ingestion.spark import source_to_bronze
        except ImportError as error:  # the generation uv project has no pyspark
            raise unittest.SkipTest(f"pyspark unavailable: {error}") from error

        cls.module = source_to_bronze

    def manifest(self, *, history: bool, declare: bool, dataset_version: str = REHEARSAL_5GB) -> dict:
        tables = list(self.module.PHYSICAL_TABLES) + (["merchant_history"] if history else [])
        value = {
            "status": "COMPLETE", "dataset_version": dataset_version, "shard_count": 1,
            "physical_transactions": 2, "logical_transactions": 1, "fraud_label_rows": 1,
            "fraud_rows": 0, "fraud_scenario_counts": {}, "total_bytes": len(tables),
            "tables": {
                table: {"rows": 1, "bytes": 1, "schema_sha256": ["a"],
                        "files": [{"path": f"{table}/shard-00000.parquet", "rows": 1, "bytes": 1,
                                   "sha256": "s", "md5_base64": "m"}]}
                for table in tables
            },
        }
        if declare:
            value["source_extensions"] = {"merchant_history": {"provenance": "synthetic"}}
        return value

    def validate(self, manifest: dict, version: str = REHEARSAL_5GB):
        return self.module.validate_source_manifest(
            manifest, expected_dataset_version=version,
            merchant_history=self.module.expected_merchant_history(version),
        )

    def test_legacy_and_declared_extension_manifests_are_accepted(self):
        self.validate(self.manifest(history=True, declare=True))
        self.validate(self.manifest(history=False, declare=False, dataset_version="cloud-smoke-v1"),
                      "cloud-smoke-v1")
        extended = self.manifest(history=True, declare=True)
        self.assertEqual(self.module.BRONZE_TABLES + ("merchant_history",),
                         self.module.manifest_bronze_tables(extended))

    def test_undeclared_missing_or_mislabelled_history_is_refused(self):
        cases = {
            "undeclared table": self.manifest(history=True, declare=False, dataset_version="cloud-smoke-v1"),
            "declared but absent": self.manifest(history=False, declare=True, dataset_version="cloud-smoke-v1"),
        }
        mislabelled = self.manifest(history=True, declare=True, dataset_version="cloud-smoke-v1")
        mislabelled["source_extensions"]["merchant_history"]["provenance"] = "observed"
        cases["not synthetic"] = mislabelled
        for name, manifest in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                self.validate(manifest, "cloud-smoke-v1")

    def test_the_registry_decides_whether_an_approved_dataset_carries_history(self):
        with self.assertRaisesRegex(ValueError, "must carry the merchant_history"):
            self.validate(self.manifest(history=False, declare=False))
        with self.assertRaisesRegex(ValueError, "must not carry the merchant_history"):
            self.validate(self.manifest(history=True, declare=True, dataset_version=CANONICAL_100GB),
                          CANONICAL_100GB)


class StageInventoryTest(unittest.TestCase):
    def test_registry_flags(self):
        self.assertFalse(dataset_has_merchant_history(CANONICAL_100GB))
        self.assertTrue(dataset_has_merchant_history(REHEARSAL_5GB))
        self.assertFalse(dataset_has_merchant_history("cloud-smoke-v1"))

    def test_canonical_inventories_are_unchanged(self):
        self.assertEqual(8, len(silver_tables(include_fraud_labels=True)))
        self.assertEqual(11, len(core_gold_tables()))
        for stage, count in (("dp1-bronze", 8), ("dp2-silver", 8), ("dp2-gold", 11), ("dp3-features", 4)):
            self.assertEqual(count, len(expected_tables(stage, CANONICAL_100GB)), stage)

    def test_rehearsal_inventories_add_history_and_scd2(self):
        self.assertIn("merchant_history", expected_tables("dp1-bronze", REHEARSAL_5GB))
        self.assertIn("merchant_history", expected_tables("dp2-silver", REHEARSAL_5GB))
        self.assertEqual(set(core_gold_tables(True)), expected_tables("dp2-gold", REHEARSAL_5GB))
        self.assertEqual(expected_tables("dp3-features", CANONICAL_100GB),
                         expected_tables("dp3-features", REHEARSAL_5GB))

    def test_submitters_pass_the_extension_flags_only_for_history_datasets(self):
        silver_extra = {"source_bronze_run_id": f"{BENCH}-optimized-bronze-{ATTEMPT}"}
        gold_extra = {"source_silver_run_id": f"{BENCH}-optimized-silver-{ATTEMPT}"}
        for version, bucket, enabled in ((REHEARSAL_5GB, REHEARSAL_BUCKET, True),
                                         (CANONICAL_100GB, "ewallet-data-ai-platform-hg26-data", False)):
            with self.subTest(version):
                silver = submit_bronze_to_silver.build_submission_command(
                    runtime(bucket), dataset_version=version, run_id=f"{BENCH}-baseline-s-{ATTEMPT}",
                    **silver_extra)
                gold = submit_silver_to_gold.build_submission_command(
                    runtime(bucket), dataset_version=version, run_id=f"{BENCH}-baseline-g-{ATTEMPT}",
                    **gold_extra)
                self.assertEqual(enabled, "--include-merchant-history" in silver)
                self.assertIn(f"--expected-table-count={9 if enabled else 8}", silver)
                self.assertEqual(enabled, "--include-merchant-scd2" in gold)
                self.assertIn(f"--expected-table-count={12 if enabled else 11}", gold)


if __name__ == "__main__":
    unittest.main()
