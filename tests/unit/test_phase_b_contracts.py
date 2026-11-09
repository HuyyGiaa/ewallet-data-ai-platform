"""Phase B contracts agree with the builders and the validators."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml
from pyspark.sql import functions as F

from data_platform.generation.src.offline.merchant_history import HISTORY_COLUMNS
from data_platform.processing.spark.gold.dimension_builder import build_dim_merchant, build_dim_merchant_scd2
from data_platform.processing.spark.silver.cleaners import clean_merchant_history
from data_platform.quality.feature_timestamp_quality import (
    feature_timestamp_results,
    historical_feature_timestamp_results,
)
from data_platform.quality.scd2_quality import merchant_history_results, merchant_scd2_results
from data_platform.storage.scripts.config import MERCHANT_HISTORY_TABLE, MERCHANT_SCD2_TABLE
from tests.unit.test_feature_created_timestamps import CLOCK, TABLE_ENTITIES, FeatureTimestampBase


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "data_platform/contracts"
PHASE_B_RULE_MARKERS = ("created", "pit_", "timestamp_columns")


def contract(name: str) -> dict:
    return yaml.safe_load((CONTRACTS / f"{name}.yml").read_text(encoding="utf-8"))


def columns(document: dict) -> list[tuple[str, str]]:
    return [(item["name"], item["type"]) for item in document["columns"]]


def spark_types(frame) -> list[tuple[str, str]]:
    names = {"bigint": "long"}
    return [(name, names.get(kind, kind)) for name, kind in frame.dtypes]


class PhaseBContractTest(FeatureTimestampBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t = lambda month: datetime(2026, month, 1, tzinfo=timezone.utc)
        cls.history = clean_merchant_history(cls.spark.createDataFrame(
            [("s1", "m1", "A", "food", t(1), "synthetic"), ("s2", "m1", "B", "food", t(2), "synthetic"),
             ("s3", "m2", "X", "retail", t(1), "synthetic")],
            "snapshot_id string, merchant_id string, merchant_name string, category string, "
            "snapshot_ts timestamp, history_source string"))
        cls.merchants = cls.spark.createDataFrame(
            [("m1", "B", "food"), ("m2", "X", "retail")], "merchant_id string, merchant_name string, category string")
        cls.scd2 = build_dim_merchant_scd2(cls.history)

    def test_feature_contracts_match_the_builder_schemas(self):
        frames = self.all_five()
        for table, frame in frames.items():
            with self.subTest(table):
                self.assertEqual(columns(contract(f"gold_{table}")), spark_types(frame))

    def test_extension_contracts_match_the_builder_schemas(self):
        self.assertEqual(columns(contract("gold_dim_merchant_scd2")), spark_types(self.scd2))
        self.assertEqual(columns(contract("silver_merchant_history")), spark_types(self.history))
        self.assertEqual([name for name, _ in columns(contract("silver_merchant_history"))], list(HISTORY_COLUMNS))
        self.assertEqual(MERCHANT_SCD2_TABLE, contract("gold_dim_merchant_scd2")["storage_table"])
        self.assertEqual(MERCHANT_HISTORY_TABLE, contract("silver_merchant_history")["storage_table"])

    def test_every_phase_b_contract_rule_is_produced_by_a_validator(self):
        transactions = self.transactions.withColumn("merchant_id", F.lit("m1"))
        produced = {result.rule_name for result in [
            *merchant_history_results(self.history, self.merchants),
            *merchant_scd2_results(self.scd2, build_dim_merchant(self.merchants), transactions=transactions),
            *historical_feature_timestamp_results(self.tables(), self.transactions, sample_modulus=1,
                                                  materialization_clocks={table: CLOCK for table in TABLE_ENTITIES}),
            *feature_timestamp_results(self.user_90d(), "feat_user_90d", legacy_created_column="created_timestamp",
                                       materialization_clock=CLOCK, require_clock=True),
        ]}
        expected = set()
        for name in ("silver_merchant_history", "gold_dim_merchant_scd2"):
            expected |= {rule["validator_rule"] for rule in contract(name)["quality_rules"]}
        for table in ("feat_user_90d", "feat_user_behavior", "feat_account_behavior",
                      "feat_device_behavior", "feat_merchant_behavior"):
            expected |= {rule["validator_rule"] for rule in contract(f"gold_{table}")["quality_rules"]
                         if any(marker in rule["validator_rule"] for marker in PHASE_B_RULE_MARKERS)}
        self.assertGreaterEqual(len(expected), 30)
        self.assertEqual(set(), expected - produced)
