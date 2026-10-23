from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from ai_platform.features.feast.cloud_boundary import (
    FEATURE_TABLES,
    CloudFeastTrainingBoundary,
)
from data_platform.config import (
    RuntimeConfig,
    ServiceConfig,
    SparkConfig,
    StorageConfig,
)
from ops.gcp.prepare_feast_training_boundary import render_user_commands


class CloudFeastTrainingBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.boundary = CloudFeastTrainingBoundary(
            bucket="course-data",
            dataset_version="canonical-100gb-v1",
            silver_run_id="canonical-silver-20261024-010203",
            feature_run_id="canonical-features-20261024-010203",
            snapshot_id="training-v1-20261024-010203",
        )

    def test_contract_uses_immutable_run_scoped_gcs_roots(self) -> None:
        self.assertEqual(
            "gs://course-data/lakehouse/runs/"
            "canonical-features-20261024-010203/gold",
            self.boundary.feature_gold_root,
        )
        self.assertEqual(
            "gs://course-data/lakehouse/runs/"
            "canonical-silver-20261024-010203/silver",
            self.boundary.silver_root,
        )
        self.assertEqual(
            "gs://course-data/features/offline/snapshots/canonical-100gb-v1/"
            "canonical-features-20261024-010203/training-v1-20261024-010203",
            self.boundary.feast_snapshot_root,
        )
        self.assertEqual(
            "gs://course-data/ml/training-snapshots/canonical-100gb-v1/"
            "training-v1-20261024-010203",
            self.boundary.training_snapshot_root,
        )

    def test_contract_exposes_exact_four_feature_tables(self) -> None:
        self.assertEqual(
            (
                "feat_user_behavior",
                "feat_account_behavior",
                "feat_device_behavior",
                "feat_merchant_behavior",
            ),
            FEATURE_TABLES,
        )

    def test_contract_rejects_unsafe_or_ambiguous_identifiers(self) -> None:
        with self.assertRaisesRegex(ValueError, "snapshot_id"):
            CloudFeastTrainingBoundary(
                bucket="course-data",
                dataset_version="canonical-100gb-v1",
                silver_run_id="canonical-silver-1",
                feature_run_id="canonical-features-1",
                snapshot_id="latest",
            )
        with self.assertRaisesRegex(ValueError, "bucket"):
            CloudFeastTrainingBoundary(
                bucket="gs://course-data",
                dataset_version="canonical-100gb-v1",
                silver_run_id="canonical-silver-1",
                feature_run_id="canonical-features-1",
                snapshot_id="training-v1",
            )

    def test_environment_is_explicit_and_contains_no_local_service(self) -> None:
        environment = self.boundary.feast_environment()
        self.assertEqual(
            self.boundary.feast_snapshot_root,
            environment["EWALLET_FEAST_OFFLINE_DATA_ROOT"],
        )
        self.assertEqual(
            self.boundary.feast_registry_uri,
            environment["EWALLET_FEAST_REGISTRY"],
        )
        self.assertNotIn("localhost", " ".join(environment.values()))
        self.assertNotIn("minio", " ".join(environment.values()).lower())

    def test_manifest_contract_records_lineage_and_temporal_rules(self) -> None:
        contract = self.boundary.as_dict()
        self.assertEqual("[T-window,T)", contract["temporal_contract"])
        self.assertFalse(contract["labels_enter_feast"])
        self.assertEqual(
            "canonical-features-20261024-010203",
            contract["source_feature_run_id"],
        )
        self.assertEqual(
            "canonical-silver-20261024-010203",
            contract["source_silver_run_id"],
        )
        self.assertEqual(10000, contract["bounded_training_sample_modulus"])

    def test_future_commands_bind_every_lineage_root(self) -> None:
        commands = render_user_commands(self.boundary, "runtime/cloud-training")
        rendered = "\n".join(commands)
        for expected in (
            self.boundary.feature_gold_root,
            self.boundary.silver_root,
            self.boundary.feast_snapshot_root,
            self.boundary.feast_registry_uri,
            self.boundary.training_snapshot_root,
            "--sample-modulus 10000",
            "--environment gcp",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("localhost", rendered)
        self.assertNotIn("minio", rendered.lower())
        self.assertIn(
            "--feature-store-yaml "
            "ai_platform/features/feast/feature_repo/feature_store.gcp.yaml",
            rendered,
        )

    def test_runtime_root_overrides_are_run_scoped(self) -> None:
        from ai_platform.features.feast.sources.export_gold_to_parquet import (
            apply_feature_gold_root,
        )
        from ai_platform.ml.training.export_training_source import (
            apply_silver_root,
        )

        config = RuntimeConfig(
            environment="gcp",
            project_id="course-project",
            region="asia-southeast1",
            storage=StorageConfig(
                scheme="gs",
                source_root="gs://course-data/source",
                bronze_root="gs://course-data/lakehouse/bronze",
                silver_root="gs://course-data/lakehouse/silver",
                gold_root="gs://course-data/lakehouse/gold",
                feast_root="gs://course-data/features/offline",
                options={},
            ),
            spark=SparkConfig(
                master=None,
                shuffle_partitions=4,
                event_log_uri="gs://course-data/spark-events",
                packages=(),
                configurations={},
            ),
            services=ServiceConfig(
                datahub_gms_url="https://datahub.example.test",
                trino_host=None,
                trino_port=None,
            ),
        )
        self.assertEqual(
            self.boundary.feature_gold_root,
            apply_feature_gold_root(config, self.boundary.feature_gold_root)
            .storage.gold_root,
        )
        self.assertEqual(
            self.boundary.silver_root,
            apply_silver_root(config, self.boundary.silver_root).storage.silver_root,
        )
        with self.assertRaisesRegex(ValueError, "run-scoped"):
            apply_feature_gold_root(
                config, "gs://course-data/lakehouse/gold"
            )

    def test_immutable_export_refuses_existing_table_directory(self) -> None:
        from ai_platform.features.feast.sources.export_gold_to_parquet import (
            _write_dataset,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.parquet"
            target = root / "target"
            pq.write_table(
                pa.table(
                    {
                        "entity_id": ["one"],
                        "event_timestamp": pa.array(
                            [0], type=pa.timestamp("us", tz="UTC")
                        ),
                    }
                ),
                source,
            )
            snapshot = ds.dataset(source, format="parquet")
            _write_dataset(str(target), snapshot, replace_existing=False)
            with self.assertRaisesRegex(FileExistsError, "immutable"):
                _write_dataset(str(target), snapshot, replace_existing=False)


if __name__ == "__main__":
    unittest.main()
