"""Phase B end to end on local Spark + Delta, with no cloud or shared storage.

A small dataset from the real generator (merchant_history enabled) is ingested
into Bronze Delta, then the real DP2 Silver, DP2 core Gold and DP3 feature
pipelines run against temporary file:// roots, and the real validators read the
persisted Delta tables back:

    generator -> Bronze Delta -> Silver (merchant_history) -> Gold dim_merchant_scd2
    Silver transactions -> DP3 features (event_timestamp, created) -> validation

Run with: EWALLET_RUN_SPARK_INTEGRATION=1 (it starts Spark several times).
"""

from __future__ import annotations

import copy
import importlib.metadata
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import yaml

from data_platform.config import StorageConfig, load_runtime_config


ROOT = Path(__file__).resolve().parents[2]
CORE_CLOCK = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)
FEATURE_CLOCK = datetime(2026, 11, 9, 2, 0, tzinfo=timezone.utc)
BRONZE_FROM_PHYSICAL = (
    "users", "accounts", "merchants", "devices", "balance_snapshots",
    "login_events", "fraud_labels", "merchant_history",
)
FEATURE_TABLES = ("feat_user_behavior", "feat_account_behavior",
                  "feat_device_behavior", "feat_merchant_behavior")


@unittest.skipUnless(os.environ.get("EWALLET_RUN_SPARK_INTEGRATION") == "1",
                     "set EWALLET_RUN_SPARK_INTEGRATION=1 to run the local Spark/Delta pipeline")
class PhaseBPipelineDeltaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from data_platform.generation.src.offline.offline_generator import (
            generate_offline_datasets,
            write_offline_datasets,
        )
        from data_platform.processing.spark.gold.gold_pipeline import run_gold_pipeline
        from data_platform.processing.spark.gold.historical_feature_pipeline import (
            run_historical_feature_pipeline,
        )
        from data_platform.processing.spark.silver.silver_pipeline import run_silver_pipeline

        cls.directory = Path(tempfile.mkdtemp(prefix="phase-b-delta-"))
        uri = lambda name: (cls.directory / name).as_uri()
        base = load_runtime_config("local", environ={"MINIO_ACCESS_KEY": "unused", "MINIO_SECRET_KEY": "unused"})
        storage = StorageConfig(
            scheme="file", source_root=uri("source"), bronze_root=uri("bronze"),
            silver_root=uri("silver"), gold_root=uri("gold"), feast_root=uri("feast"),
        )
        # Only Delta artifacts that match the installed delta-spark (file:// storage
        # needs no hadoop-aws), so the same test runs on Spark 4.1 and on 3.5.
        delta_storage = f"io.delta:delta-storage:{importlib.metadata.version('delta-spark')}"
        spark = replace(
            base.spark, master="local[2]", shuffle_partitions=4, event_log_uri=uri("events"),
            packages=(delta_storage,),
            configurations={"spark.sql.session.timeZone": "UTC", "spark.ui.enabled": "false",
                            "spark.eventLog.enabled": "false", "spark.driver.memory": "2g"},
        )
        cls.config = replace(base, storage=storage, spark=spark)
        cls.feature_config = replace(cls.config, storage=replace(storage, gold_root=uri("features/gold")))
        cls.legacy_config = replace(cls.config, storage=replace(storage, gold_root=uri("legacy/gold")))

        settings = yaml.safe_load(
            (ROOT / "data_platform/generation/config/settings_merchant_history.yaml").read_text(encoding="utf-8"))
        settings = copy.deepcopy(settings)
        settings.update({"n_users": 120, "n_merchants": 15, "n_transactions": 3000, "random_seed": 11})
        settings["fraud"]["random_seed"] = 11
        datasets, _ = generate_offline_datasets(settings)
        cls.datasets = datasets
        write_offline_datasets(datasets, cls.directory / "source")
        cls._ingest_bronze()

        run_silver_pipeline(include_fraud_labels=True, include_merchant_history=True,
                            runtime_config=cls.config)
        run_gold_pipeline(runtime_config=cls.config, include_merchant_scd2=True, materialized_at=CORE_CLOCK)
        run_gold_pipeline(runtime_config=cls.legacy_config, include_merchant_scd2=False,
                          materialized_at=CORE_CLOCK)
        run_historical_feature_pipeline(runtime_config=cls.feature_config, materialized_at=FEATURE_CLOCK)

    @classmethod
    def _ingest_bronze(cls):
        """Mirror source_to_bronze for local files: one Delta table per Bronze table."""
        from data_platform.ingestion.spark.source_to_bronze import combine_transaction_versions
        from data_platform.processing.spark.common.spark_session import create_spark_session

        spark = create_spark_session("phase-b-bronze", stage="dp2-silver", runtime_config=cls.config)
        try:
            source = cls.directory / "source"
            read = lambda table: spark.read.parquet(str(source / f"{table}.parquet"))
            for table in BRONZE_FROM_PHYSICAL:
                read(table).write.format("delta").save(cls.config.storage.table_uri("bronze", table))
            combine_transaction_versions(read("transactions_v1"), read("transactions_v2")).write.format(
                "delta").save(cls.config.storage.table_uri("bronze", "transactions"))
        finally:
            spark.stop()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.session().stop()
        finally:
            shutil.rmtree(cls.directory, ignore_errors=True)

    @classmethod
    def session(cls):
        from data_platform.processing.spark.common.spark_session import create_spark_session

        return create_spark_session("phase-b-readback", stage="dp2-gold", runtime_config=cls.config)

    def read(self, config, layer: str, table: str):
        return self.session().read.format("delta").load(config.storage.table_uri(layer, table))

    def failed(self, results):
        return sorted(result.rule_name for result in results if not result.passed)

    # ----------------------------------------------------------------- Silver
    def test_silver_validator_passes_with_merchant_history(self):
        from data_platform.quality.validate_silver import run_validation

        run_validation(include_fraud_labels=True, runtime_config=self.config, include_merchant_history=True)

    # -------------------------------------------------------------- Core Gold
    def test_core_gold_validator_passes_with_scd2_and_created(self):
        from data_platform.quality.validate_gold import run_validation

        results = run_validation(self.config, scope="core-gold", include_merchant_scd2=True)
        self.assertEqual([], self.failed(results))
        names = {result.rule_name for result in results}
        for rule in ("dim_merchant_scd2.one_current_version_per_merchant",
                     "dim_merchant_scd2.contiguous_non_overlapping_intervals",
                     "dim_merchant_scd2.transactions_resolve_to_one_version",
                     "merchant_history.latest_snapshot_equals_merchants",
                     "feat_user_90d.created_matches_created_timestamp"):
            self.assertIn(rule, names)

    def test_scd2_delta_readback_has_versions_and_the_rubric_columns(self):
        scd2 = self.read(self.config, "gold", "dim_merchant_scd2")
        types = dict(scd2.dtypes)
        self.assertEqual({"valid_from_ts": "timestamp", "valid_to_ts": "timestamp", "is_current": "boolean"},
                         {key: types[key] for key in ("valid_from_ts", "valid_to_ts", "is_current")})
        merchants = self.datasets["merchants"]
        self.assertEqual(len(merchants), scd2.filter("is_current").count())
        history = self.datasets["merchant_history"]
        self.assertGreater(scd2.count(), len(merchants), "some merchants have more than one version")
        self.assertLess(scd2.count(), len(history), "unchanged snapshots collapse")

    def test_dim_merchant_is_unchanged_and_one_row_per_merchant(self):
        current = self.read(self.config, "gold", "dim_merchant")
        legacy = self.read(self.legacy_config, "gold", "dim_merchant")
        self.assertEqual(["merchant_id", "merchant_name", "category"], current.columns)
        self.assertEqual(sorted(legacy.collect()), sorted(current.collect()))
        self.assertEqual(current.count(), current.select("merchant_id").distinct().count())

    def test_legacy_run_without_history_writes_the_eleven_core_tables(self):
        from data_platform.quality.validate_gold import run_validation
        from data_platform.storage.scripts.config import CORE_GOLD_TABLES

        written = {path.name for path in (self.directory / "legacy/gold").iterdir() if path.is_dir()}
        self.assertEqual(set(CORE_GOLD_TABLES), written)
        self.assertNotIn("dim_merchant_scd2", written)
        results = run_validation(self.legacy_config, scope="core-gold")
        self.assertEqual([], self.failed(results))

    # ----------------------------------------------------------- DP3 features
    def test_feature_validator_passes_including_pit_recomputation(self):
        from data_platform.quality.validate_gold import run_validation

        results = run_validation(self.config, scope="features", feature_storage=self.feature_config.storage,
                                 pit_sample_modulus=1)
        self.assertEqual([], self.failed(results))
        self.assertEqual(4, sum(".pit_" in result.rule_name for result in results))

    def test_all_five_feature_tables_persist_event_timestamp_and_created(self):
        from pyspark.sql import functions as F

        expected = {table: FEATURE_CLOCK for table in FEATURE_TABLES}
        expected["feat_user_90d"] = CORE_CLOCK
        for table, clock in expected.items():
            config = self.config if table == "feat_user_90d" else self.feature_config
            with self.subTest(table):
                frame = self.read(config, "gold", table)
                types = dict(frame.dtypes)
                self.assertEqual(("timestamp", "timestamp"), (types["event_timestamp"], types["created"]))
                row = frame.agg(F.min(F.unix_micros("created")).alias("lo"),
                                F.max(F.unix_micros("created")).alias("hi"),
                                F.sum(F.col("created").isNull().cast("int")).alias("nulls")).first()
                self.assertEqual((int(clock.timestamp() * 1e6),) * 2, (row.lo, row.hi))
                self.assertEqual(0, row.nulls or 0)

    def test_the_materialization_clock_is_recorded_in_delta_history(self):
        from data_platform.processing.spark.gold.materialization import read_materialization_clock

        session = self.session()
        for table in FEATURE_TABLES:
            with self.subTest(table):
                uri = self.feature_config.storage.table_uri("gold", table)
                self.assertEqual(FEATURE_CLOCK, read_materialization_clock(session, uri))
        uri = self.config.storage.table_uri("gold", "feat_user_90d")
        self.assertEqual(CORE_CLOCK, read_materialization_clock(session, uri))
        self.assertIsNone(read_materialization_clock(session, self.config.storage.table_uri("gold", "dim_user")))

    def test_scd2_without_source_history_fails_before_any_write(self):
        from data_platform.processing.spark.gold.gold_pipeline import run_gold_pipeline

        silver = self.directory / "silver"
        no_history = self.directory / "silver_without_history"
        for table in silver.iterdir():
            if table.name != "merchant_history":
                shutil.copytree(table, no_history / table.name)
        gold = self.directory / "no_history/gold"
        config = replace(self.config, storage=replace(self.config.storage, silver_root=no_history.as_uri(),
                                                      gold_root=gold.as_uri()))
        with self.assertRaises(Exception) as raised:
            run_gold_pipeline(runtime_config=config, include_merchant_scd2=True, materialized_at=CORE_CLOCK)
        self.assertIn("merchant_history", str(raised.exception))
        self.assertFalse(gold.exists() and any(gold.iterdir()), "nothing may be written")

    # --------------------------------------------------- campaign stage reports
    def report(self, name: str) -> tuple[str, Path]:
        path = self.directory / "reports" / f"{name}.json"
        return path.as_uri(), path

    def test_bronze_validator_covers_merchant_history_and_writes_its_report(self):
        from data_platform.quality.validate_bronze import run_validation

        uri, path = self.report("bronze")
        self.assertTrue(run_validation(include_fraud_labels=True, runtime_config=self.config,
                                       include_merchant_history=True, report_uri=uri,
                                       report_details={"run_id": "r", "dataset_version": "d"}))
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(("PASS", "r", "bronze"), (report["status"], report["run_id"], report["scope"]))
        self.assertIn("merchant_history.delta_readable", [item["rule_name"] for item in report["results"]])

    def test_a_broken_bronze_table_fails_validation_with_a_report(self):
        from pyspark.sql import functions as F

        from data_platform.quality.validate_bronze import run_validation

        copy = self.directory / "bronze_broken"
        shutil.copytree(self.directory / "bronze", copy)
        config = replace(self.config, storage=replace(self.config.storage, bronze_root=copy.as_uri()))
        session = self.session()
        table = config.storage.table_uri("bronze", "merchant_history")
        broken = session.read.format("delta").load(table).drop("snapshot_ts").withColumn("x", F.lit(1))
        broken.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(table)
        uri, path = self.report("bronze_broken")
        self.assertFalse(run_validation(include_fraud_labels=True, runtime_config=config,
                                        include_merchant_history=True, report_uri=uri, report_details={}))
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("FAIL", report["status"])
        self.assertIn("merchant_history.required_columns", report["failed_rules"])

    def test_silver_gold_and_feature_validators_write_pass_reports(self):
        from data_platform.quality import validate_gold, validate_silver

        uri, path = self.report("silver")
        validate_silver.run_validation(include_fraud_labels=True, runtime_config=self.config,
                                       include_merchant_history=True, report_uri=uri, report_details={})
        self.assertEqual("PASS", json.loads(path.read_text(encoding="utf-8"))["status"])
        uri, path = self.report("core-gold")
        validate_gold.run_validation(self.config, scope="core-gold", include_merchant_scd2=True,
                                     report_uri=uri, report_details={})
        core = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(("PASS", "core-gold"), (core["status"], core["scope"]))
        self.assertIn("feat_user_90d.created_matches_materialization_clock", [r["rule_name"] for r in core["results"]])
        uri, path = self.report("features")
        validate_gold.run_validation(self.config, scope="features", feature_storage=self.feature_config.storage,
                                     pit_sample_modulus=1, report_uri=uri, report_details={})
        features = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("PASS", features["status"])
        with self.assertRaises(Exception):  # reports are immutable
            validate_gold.run_validation(self.config, scope="features", feature_storage=self.feature_config.storage,
                                         pit_sample_modulus=1, report_uri=uri, report_details={})

    def test_comparison_passes_two_runs_of_the_same_input_and_flags_a_difference(self):
        from pyspark.sql import functions as F

        from ops.gcp.compare_campaign_outputs import compare_table

        session = self.session()
        for table, key in (("fact_transactions", ("transaction_id",)), ("feat_user_90d", ("user_id",))):
            with self.subTest(table):
                left = session.read.format("delta").load(self.config.storage.table_uri("gold", table))
                right = session.read.format("delta").load(self.legacy_config.storage.table_uri("gold", table))
                self.assertTrue(compare_table(left, right, table, key)["passed"])
        left = session.read.format("delta").load(self.config.storage.table_uri("gold", "dim_merchant"))
        changed = left.withColumn("merchant_name", F.concat(F.col("merchant_name"), F.lit("x")))
        report = compare_table(left, changed, "dim_merchant", ("merchant_id",))
        self.assertFalse(report["passed"])
        self.assertIn("merchant_name", report["differences"]["columns"])

    # ------------------------------------------------------- Delta negatives
    def test_corrupted_delta_outputs_fail_validation(self):
        from pyspark.sql import functions as F

        from data_platform.quality.feature_timestamp_quality import feature_timestamp_results
        from data_platform.quality.scd2_quality import merchant_scd2_results

        scd2 = self.read(self.config, "gold", "dim_merchant_scd2")
        corrupt_path = (self.directory / "corrupt/dim_merchant_scd2").as_uri()
        scd2.withColumn("is_current", F.lit(True)).write.format("delta").save(corrupt_path)
        corrupted = self.session().read.format("delta").load(corrupt_path)
        dim_merchant = self.read(self.config, "gold", "dim_merchant")
        failed = self.failed(merchant_scd2_results(corrupted, dim_merchant))
        self.assertIn("dim_merchant_scd2.current_flag_matches_open_end", failed)
        self.assertIn("dim_merchant_scd2.one_current_version_per_merchant", failed)

        features = self.read(self.feature_config, "gold", "feat_user_behavior")
        broken_path = (self.directory / "corrupt/feat_user_behavior").as_uri()
        features.withColumn("created", F.lit(None).cast("timestamp")).write.format("delta").save(broken_path)
        broken = self.session().read.format("delta").load(broken_path)
        self.assertIn("feat_user_behavior.timestamp_columns_not_null",
                      self.failed(feature_timestamp_results(broken, "feat_user_behavior")))


if __name__ == "__main__":
    unittest.main()
