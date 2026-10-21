from __future__ import annotations

import unittest
from unittest.mock import patch

from data_platform.config import RuntimeConfig, ServiceConfig, SparkConfig, StorageConfig
from data_platform.storage.scripts import config as storage_config
from data_platform.storage.scripts.trino_client import create_trino_connection


def runtime(environment: str, scheme: str = "gs") -> RuntimeConfig:
    if scheme == "gs":
        storage = StorageConfig(
            scheme="gs",
            source_root="gs://course-bucket/source",
            bronze_root="gs://course-bucket/lakehouse/bronze",
            silver_root="gs://course-bucket/lakehouse/silver",
            gold_root="gs://course-bucket/lakehouse/gold",
            feast_root="gs://course-bucket/features/offline",
            options={},
        )
    else:
        storage = StorageConfig(
            scheme="s3a",
            source_root="file:///tmp/source",
            bronze_root="s3a://bronze-zone",
            silver_root="s3a://silver-zone",
            gold_root="s3a://gold-zone",
            feast_root="file:///tmp/feast",
            options={
                "spark.hadoop.fs.s3a.endpoint": "http://localhost:9000",
                "spark.hadoop.fs.s3a.access.key": "test-access",
                "spark.hadoop.fs.s3a.secret.key": "test-secret",
            },
        )
    return RuntimeConfig(
        environment=environment,
        project_id=None if environment == "local" else "project-id",
        region=None if environment == "local" else "asia-southeast1",
        storage=storage,
        spark=SparkConfig(
            master="local[1]" if environment == "local" else None,
            shuffle_partitions=4,
            event_log_uri="file:///tmp/events",
            packages=(),
            configurations={},
        ),
        services=ServiceConfig(
            datahub_gms_url="http://localhost:8080",
            trino_host="trino.internal" if environment == "local" else None,
            trino_port=8081 if environment == "local" else None,
        ),
    )


class StorageScriptConfigTest(unittest.TestCase):
    def test_config_import_does_not_define_or_default_minio_password(self) -> None:
        self.assertFalse(hasattr(storage_config, "MINIO_SECRET_KEY"))
        self.assertFalse(hasattr(storage_config, "MINIO_ACCESS_KEY"))

    def test_local_only_guard_rejects_cloud_before_mutation(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "local-only command"):
            storage_config.require_local_runtime(runtime("gcp"), "init_storage")

    def test_table_inventories_remain_backward_compatible(self) -> None:
        self.assertEqual(7, len(storage_config.offline_tables()))
        self.assertEqual(8, len(storage_config.offline_tables(True)))
        self.assertEqual(7, len(storage_config.silver_tables()))
        self.assertEqual(8, len(storage_config.silver_tables(True)))
        self.assertEqual(15, len(storage_config.GOLD_TABLES))

    def test_registration_locations_come_from_runtime_profile(self) -> None:
        config = runtime("gcp")
        layers = storage_config.trino_registration_layers(runtime_config=config)

        self.assertEqual(
            "gs://course-bucket/lakehouse/silver/transactions",
            storage_config.registration_table_uri(
                layers["silver"],
                "transactions",
            ),
        )
        self.assertNotIn("bucket", layers["silver"])

    @patch("data_platform.storage.scripts.trino_client.connect")
    def test_trino_connection_uses_runtime_service_endpoint(self, connect) -> None:
        config = runtime("local", scheme="s3a")

        create_trino_connection(config)

        connect.assert_called_once_with(
            host="trino.internal",
            port=8081,
            user="trino",
            catalog="delta",
            http_scheme="http",
        )


if __name__ == "__main__":
    unittest.main()
