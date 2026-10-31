from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from data_platform.config import load_runtime_config
from data_platform.processing.spark.gold.gold_pipeline import CORE_GOLD_TABLES
from data_platform.processing.spark.gold.historical_feature_pipeline import (
    FEATURE_TRANSACTION_COLUMNS,
    HISTORICAL_FEATURE_TABLES,
    apply_run_scoped_storage,
    parse_args,
    project_feature_transactions,
)


class HistoricalFeatureCloudRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GCP_PROJECT_ID": "example-project",
                "EWALLET_DATA_BUCKET": "example-project-data",
                "DATAHUB_GMS_URL": "http://datahub.invalid:8080",
            },
            clear=False,
        ):
            self.config = load_runtime_config("gcp")

    def test_historical_tables_are_four_and_disjoint_from_core(self) -> None:
        self.assertEqual(4, len(HISTORICAL_FEATURE_TABLES))
        self.assertFalse(set(CORE_GOLD_TABLES) & set(HISTORICAL_FEATURE_TABLES))

    def test_feature_output_is_run_scoped(self) -> None:
        updated = apply_run_scoped_storage(
            self.config,
            silver_root="gs://bucket/lakehouse/runs/silver-run/silver",
            feature_gold_root="gs://bucket/lakehouse/runs/feature-run/gold",
        )
        self.assertEqual(
            "gs://bucket/lakehouse/runs/feature-run/gold",
            updated.storage.gold_root,
        )

    def test_cloud_lineage_args_are_required_together(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(
                ["--environment=gcp", "--feature-gold-root=gs://bucket/run/gold"]
            )

    def test_shared_projected_base_is_narrow_and_preserves_pit_inputs(self) -> None:
        dataframe = Mock()
        dataframe.columns = [*FEATURE_TRANSACTION_COLUMNS, "currency", "ingested_at"]
        projected = Mock()
        dataframe.select.return_value = projected
        self.assertIs(project_feature_transactions(dataframe), projected)
        dataframe.select.assert_called_once_with(*FEATURE_TRANSACTION_COLUMNS)
        self.assertEqual(
            {
                "user_id", "account_id", "device_id", "merchant_id",
                "timestamp", "amount", "status", "type",
            },
            set(FEATURE_TRANSACTION_COLUMNS),
        )


if __name__ == "__main__":
    unittest.main()
