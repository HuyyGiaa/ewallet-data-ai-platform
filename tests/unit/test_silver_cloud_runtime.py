from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from data_platform.config import load_runtime_config
from data_platform.processing.spark.silver.silver_pipeline import (
    apply_run_scoped_storage,
    parse_args,
)


class SilverCloudRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.environ = {
            "GCP_PROJECT_ID": "example-project",
            "EWALLET_DATA_BUCKET": "example-project-data",
            "DATAHUB_GMS_URL": "http://datahub.invalid:8080",
        }
        with patch.dict(os.environ, self.environ, clear=False):
            self.config = load_runtime_config("gcp")

    def test_run_scoped_roots_replace_only_bronze_and_silver(self) -> None:
        bronze_root = (
            "gs://example-project-data/lakehouse/runs/"
            "r4-bronze-20261022-095608/bronze"
        )
        silver_root = (
            "gs://example-project-data/lakehouse/runs/"
            "s1a-silver-20261022-120000/silver"
        )

        updated = apply_run_scoped_storage(
            self.config,
            bronze_root=bronze_root,
            silver_root=silver_root,
        )

        self.assertEqual(bronze_root, updated.storage.bronze_root)
        self.assertEqual(silver_root, updated.storage.silver_root)
        self.assertEqual(self.config.storage.gold_root, updated.storage.gold_root)
        self.assertEqual(self.config.storage.source_root, updated.storage.source_root)
        self.assertEqual(
            "gs://example-project-data/lakehouse/bronze",
            self.config.storage.bronze_root,
        )

    def test_run_scoped_roots_reject_non_gcs_or_shared_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "gs://"):
            apply_run_scoped_storage(
                self.config,
                bronze_root="file:///tmp/bronze",
                silver_root=(
                    "gs://example-project-data/lakehouse/runs/"
                    "s1a-silver-20261022-120000/silver"
                ),
            )

        with self.assertRaisesRegex(ValueError, "run-scoped"):
            apply_run_scoped_storage(
                self.config,
                bronze_root=(
                    "gs://example-project-data/lakehouse/runs/"
                    "r4-bronze-20261022-095608/bronze"
                ),
                silver_root="gs://example-project-data/lakehouse/silver",
            )

    def test_cloud_lineage_arguments_are_required_together(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(
                [
                    "--environment=gcp",
                    "--bronze-root=gs://bucket/lakehouse/runs/source/bronze",
                ]
            )


if __name__ == "__main__":
    unittest.main()
