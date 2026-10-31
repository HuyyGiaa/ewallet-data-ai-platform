"""Focused F2 tests using only fraud_dev and temporary local Delta tables."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import pyarrow as pa
import yaml
from deltalake import DeltaTable, write_deltalake
from pyspark.sql import SparkSession

from data_platform.processing.spark.silver.cleaners import clean_fraud_labels
from data_platform.processing.spark.silver.silver_pipeline import selected_pipelines
from data_platform.processing.spark.silver.transactions import clean_transactions
from data_platform.quality.validate_bronze import (
    validate_compatible_types,
    validate_fraud_label_coverage,
    validate_fraud_labels as validate_bronze_fraud_labels,
    validate_required_columns as validate_bronze_required_columns,
)
from data_platform.quality.validate_silver import (
    validate_bronze_silver_fraud_population,
    validate_exact_transaction_coverage,
    validate_fraud_labels as validate_silver_fraud_labels,
)
from data_platform.storage.scripts.config import (
    GOLD_TABLES,
    delta_table_uri,
    offline_tables,
    trino_registration_layers,
)
from data_platform.storage.scripts.delta_writer import (
    DeltaWriterError,
    dataframe_to_arrow,
    normalize_dataframe,
    validate_source_file,
)
from data_platform.storage.scripts.init_storage import (
    StorageInitializationError,
    validate_selected_tables,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRAUD_DEV = PROJECT_ROOT / "data_platform" / "generation" / "output" / "fraud_dev"


class FraudLabelPlatformIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        python = Path(os.environ.get("PYSPARK_PYTHON", os.sys.executable)).resolve()
        os.environ["PYSPARK_PYTHON"] = str(python)
        os.environ["PYSPARK_DRIVER_PYTHON"] = str(python)

        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.bronze_transactions_path = cls.root / "bronze" / "transactions"
        cls.bronze_labels_path = cls.root / "bronze" / "fraud_labels"
        cls.silver_transactions_path = cls.root / "silver" / "transactions"
        cls.silver_labels_path = cls.root / "silver" / "fraud_labels"

        v1 = normalize_dataframe(
            pd.read_parquet(FRAUD_DEV / "transactions_v1.parquet"),
            "transactions",
            allowed_missing_columns=("channel",),
        )
        v2 = normalize_dataframe(
            pd.read_parquet(FRAUD_DEV / "transactions_v2.parquet"),
            "transactions",
        )
        labels = normalize_dataframe(
            pd.read_parquet(FRAUD_DEV / "fraud_labels.parquet"),
            "fraud_labels",
        )

        write_deltalake(
            str(cls.bronze_transactions_path),
            dataframe_to_arrow(v1, "transactions_v1"),
            mode="overwrite",
            schema_mode="overwrite",
            engine="rust",
        )
        write_deltalake(
            str(cls.bronze_transactions_path),
            dataframe_to_arrow(v2, "transactions_v2"),
            mode="append",
            schema_mode="merge",
            engine="rust",
        )
        write_deltalake(
            str(cls.bronze_labels_path),
            dataframe_to_arrow(labels, "fraud_labels"),
            mode="overwrite",
            schema_mode="overwrite",
            engine="rust",
        )

        cls.bronze_transactions = DeltaTable(
            str(cls.bronze_transactions_path)
        ).to_pandas()
        cls.bronze_labels = DeltaTable(str(cls.bronze_labels_path)).to_pandas()

        cls.spark = (
            SparkSession.builder
            .master("local[2]")
            .appName("F2-Fraud-Label-Platform-Test")
            .config("spark.ui.enabled", "false")
            .getOrCreate()
        )
        cls.spark.sparkContext.setLogLevel("ERROR")
        cls.bronze_transactions_df = cls.spark.createDataFrame(
            cls.bronze_transactions
        ).cache()
        cls.bronze_labels_df = cls.spark.createDataFrame(cls.bronze_labels).cache()
        cls.silver_transactions_df = clean_transactions(
            cls.bronze_transactions_df
        ).cache()
        cls.silver_labels_df = clean_fraud_labels(cls.bronze_labels_df).cache()

        cls.metrics = {
            "physical_transactions": cls.bronze_transactions_df.count(),
            "distinct_bronze_transactions": (
                cls.bronze_transactions_df.select("transaction_id").distinct().count()
            ),
            "bronze_labels": cls.bronze_labels_df.count(),
            "silver_transactions": cls.silver_transactions_df.count(),
            "silver_labels": cls.silver_labels_df.count(),
        }

        write_deltalake(
            str(cls.silver_transactions_path),
            pa.Table.from_pandas(
                cls.silver_transactions_df.toPandas(), preserve_index=False
            ),
            mode="overwrite",
            schema_mode="overwrite",
            engine="rust",
        )
        write_deltalake(
            str(cls.silver_labels_path),
            pa.Table.from_pandas(cls.silver_labels_df.toPandas(), preserve_index=False),
            mode="overwrite",
            schema_mode="overwrite",
            engine="rust",
        )

    @classmethod
    def tearDownClass(cls):
        cls.spark.stop()
        cls.temporary.cleanup()

    def test_transition_inventory_and_missing_source_failure(self):
        self.assertEqual(7, len(offline_tables()))
        self.assertEqual(8, len(offline_tables(include_fraud_labels=True)))
        self.assertNotIn("fraud_labels", validate_selected_tables(None))
        self.assertIn(
            "fraud_labels",
            validate_selected_tables(None, include_fraud_labels=True),
        )
        with self.assertRaisesRegex(
            StorageInitializationError,
            "requires explicit --include-fraud-labels",
        ):
            validate_selected_tables(["fraud_labels"])
        with self.assertRaisesRegex(DeltaWriterError, "fraud_labels"):
            validate_source_file("fraud_labels", self.root / "missing.parquet")

    def test_v1_v2_local_delta_schema_evolution_is_unchanged(self):
        self.assertNotIn(
            "channel",
            pd.read_parquet(FRAUD_DEV / "transactions_v1.parquet").columns,
        )
        self.assertIn(
            "channel",
            pd.read_parquet(FRAUD_DEV / "transactions_v2.parquet").columns,
        )
        self.assertIn("channel", self.bronze_transactions.columns)
        self.assertEqual(
            len(pd.read_parquet(FRAUD_DEV / "transactions_v1.parquet")),
            int(self.bronze_transactions["channel"].isna().sum()),
        )
        self.assertGreater(
            self.metrics["physical_transactions"],
            self.metrics["distinct_bronze_transactions"],
        )

    def test_bronze_fraud_schema_rules_and_exact_coverage(self):
        schema_results = [
            validate_bronze_required_columns(self.bronze_labels_df, "fraud_labels"),
            validate_compatible_types(self.bronze_labels_df, "fraud_labels"),
        ]
        contract_results = validate_bronze_fraud_labels(self.bronze_labels_df)
        coverage_results = validate_fraud_label_coverage(
            self.bronze_labels_df,
            self.bronze_transactions_df,
        )
        self.assertTrue(all(result.passed for result in schema_results))
        self.assertTrue(all(result.passed for result in contract_results))
        self.assertTrue(all(result.passed for result in coverage_results))
        self.assertEqual(
            self.metrics["bronze_labels"],
            self.metrics["distinct_bronze_transactions"],
        )

    def test_silver_fraud_schema_rules_and_population_invariants(self):
        self.assertEqual(
            [
                ("transaction_id", "string"),
                ("label", "int"),
                ("fraud_type", "string"),
            ],
            self.silver_labels_df.dtypes,
        )
        contract_results = validate_silver_fraud_labels(self.silver_labels_df)
        coverage_results = validate_exact_transaction_coverage(
            self.silver_labels_df,
            self.silver_transactions_df,
            "fraud_labels",
        )
        population_result = validate_bronze_silver_fraud_population(
            self.bronze_labels_df,
            self.silver_labels_df,
        )
        self.assertTrue(all(result.passed for result in contract_results))
        self.assertTrue(all(result.passed for result in coverage_results))
        self.assertTrue(population_result.passed)
        self.assertEqual(
            self.metrics["silver_labels"],
            self.metrics["silver_transactions"],
        )
        self.assertEqual(
            self.metrics["bronze_labels"],
            self.metrics["silver_labels"],
        )
        self.assertTrue(self.silver_transactions_path.joinpath("_delta_log").is_dir())
        self.assertTrue(self.silver_labels_path.joinpath("_delta_log").is_dir())
        self.assertEqual(
            self.metrics["silver_transactions"],
            DeltaTable(str(self.silver_transactions_path))
            .to_pyarrow_dataset()
            .count_rows(),
        )
        self.assertEqual(
            self.metrics["silver_labels"],
            DeltaTable(str(self.silver_labels_path))
            .to_pyarrow_dataset()
            .count_rows(),
        )

    def test_fraud_validators_reject_invalid_values_and_coverage(self):
        invalid_labels = self.spark.createDataFrame(
            [
                ("T1", 0, "velocity"),
                ("T1", 1, None),
                ("T2", 2, "unknown_type"),
                ("T4", 1, "unknown_type"),
                (None, None, None),
            ],
            "transaction_id string, label integer, fraud_type string",
        )
        transactions = self.spark.createDataFrame(
            [("T1",), ("T3",)],
            "transaction_id string",
        )

        bronze_results = {
            result.rule_name: result
            for result in validate_bronze_fraud_labels(invalid_labels)
        }
        silver_results = {
            result.rule_name: result
            for result in validate_silver_fraud_labels(invalid_labels)
        }
        coverage_results = validate_fraud_label_coverage(
            invalid_labels,
            transactions,
        )

        for rule_name in (
            "fraud_labels.required_values_not_null",
            "fraud_labels.valid_label_domain",
            "fraud_labels.conditional_fraud_type",
            "fraud_labels.valid_fraud_type_domain",
            "fraud_labels.unique_transaction_id",
        ):
            self.assertFalse(bronze_results[rule_name].passed)

        for rule_name in (
            "fraud_labels.required_columns_not_null",
            "fraud_labels.duplicate_key",
            "fraud_labels.valid_label_domain",
            "fraud_labels.conditional_fraud_type",
            "fraud_labels.valid_fraud_type_domain",
        ):
            self.assertFalse(silver_results[rule_name].passed)

        self.assertTrue(any(not result.passed for result in coverage_results))

    def test_silver_pipeline_selection_and_no_gold_fraud_table(self):
        self.assertNotIn("fraud_labels", [item[0] for item in selected_pipelines()])
        self.assertIn(
            "fraud_labels",
            [item[0] for item in selected_pipelines(include_fraud_labels=True)],
        )
        self.assertEqual(15, len(GOLD_TABLES))
        self.assertFalse(any("fraud" in table for table in GOLD_TABLES))

    def test_trino_registration_inventory_and_locations(self):
        legacy = trino_registration_layers()
        fraud = trino_registration_layers(include_fraud_labels=True)
        self.assertEqual((7, 7, 15), tuple(len(legacy[x]["tables"]) for x in legacy))
        self.assertEqual((8, 8, 15), tuple(len(fraud[x]["tables"]) for x in fraud))
        self.assertEqual(
            "s3://bronze-zone/fraud_labels",
            delta_table_uri(str(fraud["bronze"]["bucket"]), "fraud_labels"),
        )
        self.assertEqual(
            "s3://silver-zone/fraud_labels",
            delta_table_uri(str(fraud["silver"]["bucket"]), "fraud_labels"),
        )

    def test_fraud_contract_parses(self):
        path = PROJECT_ROOT / "data_platform" / "contracts" / "silver_fraud_labels.yml"
        contract = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(1, contract["contract_version"])
        self.assertEqual("silver.fraud_labels", contract["dataset"])
        self.assertEqual(["transaction_id"], contract["logical_key"])
        self.assertIn("synthetic", " ".join(contract["notes"]).lower())


class DataHubFraudDefinitionTest(unittest.TestCase):
    def test_lineage_and_assertions_are_opt_in_and_deterministic(self):
        datahub_python = Path(
            os.environ.get(
                "DATAHUB_PYTHON",
                Path.home() / "miniconda3" / "envs" / "datahub" / "bin" / "python",
            )
        )
        if not datahub_python.is_file():
            self.skipTest("DATAHUB_PYTHON is unavailable")

        code = """
import json
from data_platform.metadata.datahub.lineage.create_lineage import (
    get_lineage,
    validate_fraud_table_inventory,
)
from data_platform.metadata.datahub.assertions.publish_assertions import get_assertions

legacy_lineage = get_lineage()
fraud_lineage = get_lineage(True)
legacy_assertions = get_assertions()
fraud_assertions = get_assertions(True)
feature_assertions = get_assertions(True, True)
missing_inventory_failed = False
try:
    validate_fraud_table_inventory({"bronze_zone": set(), "silver_zone": set()})
except RuntimeError:
    missing_inventory_failed = True
print(json.dumps({
    "legacy_edges": sum(len(v) for v in legacy_lineage.values()),
    "fraud_edges": sum(len(v) for v in fraud_lineage.values()),
    "fraud_edge": any(
        "silver_zone.fraud_labels" in str(downstream)
        and [str(item) for item in upstreams]
        == ["urn:li:dataset:(urn:li:dataPlatform:trino,delta.bronze_zone.fraud_labels,PROD)"]
        for downstream, upstreams in fraud_lineage.items()
    ),
    "legacy_assertions": len(legacy_assertions),
    "fraud_assertions": len(fraud_assertions),
    "feature_assertions": len(feature_assertions),
    "unique_ids": len({x.assertion_id for x in fraud_assertions}) == len(fraud_assertions),
    "stable_legacy_ids": [x.assertion_id for x in legacy_assertions]
        == [x.assertion_id for x in fraud_assertions[:len(legacy_assertions)]],
    "missing_inventory_failed": missing_inventory_failed,
}))
"""
        completed = subprocess.run(
            [str(datahub_python), "-c", code],
            cwd=PROJECT_ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(15, result["legacy_edges"])
        self.assertEqual(16, result["fraud_edges"])
        self.assertTrue(result["fraud_edge"])
        self.assertEqual(8, result["legacy_assertions"])
        self.assertEqual(11, result["fraud_assertions"])
        self.assertEqual(15, result["feature_assertions"])
        self.assertTrue(result["unique_ids"])
        self.assertTrue(result["stable_legacy_ids"])
        self.assertTrue(result["missing_inventory_failed"])


if __name__ == "__main__":
    unittest.main()
