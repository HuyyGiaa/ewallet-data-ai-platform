from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from data_platform.config import load_runtime_config
from data_platform.processing.spark.gold.gold_pipeline import (
    CORE_GOLD_TABLES,
    apply_run_scoped_storage as apply_core_roots,
    parse_args as parse_core_args,
)


class GoldCloudRuntimeTest(unittest.TestCase):
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

    def test_core_table_set_has_eleven_tables(self) -> None:
        self.assertEqual(11, len(CORE_GOLD_TABLES))

    def test_core_roots_are_run_scoped(self) -> None:
        config = apply_core_roots(
            self.config,
            silver_root="gs://bucket/lakehouse/runs/silver-run/silver",
            gold_root="gs://bucket/lakehouse/runs/gold-run/gold",
        )
        self.assertEqual(
            "gs://bucket/lakehouse/runs/gold-run/gold", config.storage.gold_root
        )

    def test_cloud_args_must_be_complete(self) -> None:
        with self.assertRaises(SystemExit):
            parse_core_args(["--environment=gcp", "--gold-root=gs://bucket/run/gold"])


if __name__ == "__main__":
    unittest.main()
