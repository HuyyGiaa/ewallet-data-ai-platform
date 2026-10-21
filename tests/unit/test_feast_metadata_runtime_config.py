from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from ai_platform.features.feast.repository import (
    build_export_manifest,
    feature_data_uri,
    feature_store_config_path,
)
from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.config import (
    RuntimeConfig,
    ServiceConfig,
    SparkConfig,
    StorageConfig,
)


def runtime(environment: str) -> RuntimeConfig:
    cloud = environment != "local"
    bucket_root = "gs://course-bucket" if cloud else "s3a://gold-zone"
    return RuntimeConfig(
        environment=environment,
        project_id="course-project" if cloud else None,
        region="asia-southeast1" if cloud else None,
        storage=StorageConfig(
            scheme="gs" if cloud else "s3a",
            source_root=(
                "gs://course-bucket/source"
                if cloud
                else "file:///tmp/ewallet-source"
            ),
            bronze_root=(
                "gs://course-bucket/lakehouse/bronze"
                if cloud
                else "s3a://bronze-zone"
            ),
            silver_root=(
                "gs://course-bucket/lakehouse/silver"
                if cloud
                else "s3a://silver-zone"
            ),
            gold_root=(
                "gs://course-bucket/lakehouse/gold" if cloud else bucket_root
            ),
            feast_root=(
                "gs://course-bucket/features/offline"
                if cloud
                else "file:///tmp/ewallet-feast"
            ),
            options={},
        ),
        spark=SparkConfig(
            master=None if cloud else "local[1]",
            shuffle_partitions=4,
            event_log_uri=(
                "gs://course-bucket/spark-events"
                if cloud
                else "file:///tmp/spark-events"
            ),
            packages=(),
            configurations={},
        ),
        services=ServiceConfig(
            datahub_gms_url=(
                "https://datahub.example.test" if cloud else "http://localhost:8080"
            ),
            trino_host=None if cloud else "localhost",
            trino_port=None if cloud else 8081,
        ),
    )


class FeastRepositoryConfigTest(unittest.TestCase):
    def test_gcs_feature_uri_is_not_resolved_as_a_local_path(self) -> None:
        config = runtime("gcp")
        self.assertEqual(
            "gs://course-bucket/features/offline/feat_user_behavior",
            feature_data_uri(config, "feat_user_behavior"),
        )

    def test_environment_selects_tracked_feature_store_config(self) -> None:
        local = feature_store_config_path("local")
        gcp = feature_store_config_path("gcp")
        self.assertEqual("feature_store.yaml", local.name)
        self.assertEqual("feature_store.gcp.yaml", gcp.name)
        self.assertTrue(local.is_file())
        self.assertTrue(gcp.is_file())
        self.assertEqual(gcp, feature_store_config_path("dev"))
        with self.assertRaisesRegex(ValueError, "unsupported environment"):
            feature_store_config_path("production")

    def test_gcp_repository_uses_gcs_registry_and_no_online_store(self) -> None:
        config_path = feature_store_config_path("gcp")
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.assertEqual("gcp", config["provider"])
        self.assertEqual("dask", config["offline_store"]["type"])
        self.assertIsNone(config["online_store"])
        self.assertEqual(
            "${EWALLET_FEAST_REGISTRY}",
            config["registry"]["path"],
        )

    def test_manifest_records_environment_and_dataset_version(self) -> None:
        manifest = build_export_manifest(
            environment="gcp",
            dataset_version="f7-canonical-v1",
            tables={"user_behavior": {"output_rows": 10}},
            exported_at="2026-10-21T00:00:00+00:00",
        )
        self.assertEqual("gcp", manifest["environment"])
        self.assertEqual("f7-canonical-v1", manifest["dataset_version"])
        self.assertEqual("microsecond", manifest["timestamp_precision"])

    def test_feature_contract_contains_no_labels(self) -> None:
        forbidden = {"label", "fraud_type", "is_fraud"}
        feature_names = {
            feature
            for spec in FEATURE_SPECS.values()
            for feature in spec["features"]
        }
        self.assertTrue(feature_names.isdisjoint(forbidden))


@unittest.skipUnless(
    importlib.util.find_spec("deltalake")
    and importlib.util.find_spec("pyspark"),
    "Feast exporter dependencies are installed in the platform environment",
)
class FeastExporterRuntimeConfigTest(unittest.TestCase):
    def test_relative_local_output_is_resolved_without_touching_object_uris(self) -> None:
        from ai_platform.features.feast.sources.export_gold_to_parquet import (
            _filesystem_path,
        )

        filesystem, path = _filesystem_path("runtime/feast")
        self.assertEqual("LocalFileSystem", type(filesystem).__name__)
        self.assertEqual(
            str((Path.cwd() / "runtime/feast").resolve()),
            path,
        )

    def test_gold_delta_source_comes_from_runtime_profile(self) -> None:
        from ai_platform.features.feast.sources.export_gold_to_parquet import (
            gold_source_uri,
        )

        self.assertEqual(
            "gs://course-bucket/lakehouse/gold/feat_user_behavior",
            gold_source_uri(runtime("gcp"), "feat_user_behavior"),
        )

    def test_validation_sample_reader_uses_profile_layer_root(self) -> None:
        from ai_platform.features.feast.sources.export_validation_samples import (
            read_delta,
        )

        spark = unittest.mock.MagicMock()
        read_delta(spark, runtime("gcp"), "silver", "transactions")
        spark.read.format.assert_called_once_with("delta")
        spark.read.format.return_value.load.assert_called_once_with(
            "gs://course-bucket/lakehouse/silver/transactions"
        )

    def test_precision_audit_source_uses_profile_gold_root(self) -> None:
        from ai_platform.features.feast.validation.precision_audit import (
            gold_table_uri,
        )

        self.assertEqual(
            "gs://course-bucket/lakehouse/gold/feat_device_behavior",
            gold_table_uri(runtime("gcp"), "feat_device_behavior"),
        )


@unittest.skipUnless(
    importlib.util.find_spec("datahub"),
    "DataHub SDK is installed in the metadata environment",
)
class DataHubRuntimeConfigTest(unittest.TestCase):
    @patch(
        "data_platform.metadata.datahub.lineage.create_lineage.DatahubRestEmitter"
    )
    def test_emitter_uses_profile_gms_endpoint(self, emitter) -> None:
        from data_platform.metadata.datahub.lineage.create_lineage import (
            create_emitter,
        )

        config = runtime("gcp")
        create_emitter(config)
        emitter.assert_called_once_with(
            gms_server="https://datahub.example.test"
        )

    def test_trino_verification_is_optional_when_profile_has_no_endpoint(self) -> None:
        from data_platform.metadata.datahub.lineage.create_lineage import (
            should_verify_trino,
        )

        self.assertTrue(should_verify_trino(runtime("local")))
        self.assertFalse(should_verify_trino(runtime("gcp")))

    @patch("data_platform.metadata.datahub.lineage.create_lineage.trino_cursor")
    def test_lineage_precheck_passes_selected_profile_to_trino(self, cursor) -> None:
        from data_platform.metadata.datahub.lineage.create_lineage import (
            FEATURE_LINEAGE_TABLES,
            verify_feature_tables_registered,
        )

        config = runtime("local")
        cursor.return_value.__enter__.return_value = unittest.mock.MagicMock()
        with patch(
            "data_platform.metadata.datahub.lineage.create_lineage."
            "check_trino_connection"
        ), patch(
            "data_platform.metadata.datahub.lineage.create_lineage."
            "list_schema_tables",
            return_value=FEATURE_LINEAGE_TABLES,
        ):
            verify_feature_tables_registered(config)
        cursor.assert_called_once_with(config)

    def test_existing_and_feature_lineage_edges_are_preserved(self) -> None:
        from data_platform.metadata.datahub.lineage.create_lineage import (
            dataset,
            get_lineage,
        )

        mapping = get_lineage(include_fraud_labels=False)
        edge_count = sum(len(upstreams) for upstreams in mapping.values())
        self.assertEqual(15, edge_count)
        device_upstreams = mapping[
            dataset("delta.gold_zone.feat_device_behavior")
        ]
        self.assertEqual(
            {
                dataset("delta.silver_zone.transactions"),
                dataset("delta.silver_zone.devices"),
            },
            set(device_upstreams),
        )

    def test_no_fraud_label_or_feast_ml_edge_targets_a_feature(self) -> None:
        from data_platform.metadata.datahub.lineage.create_lineage import (
            get_lineage,
        )

        mapping = get_lineage(include_fraud_labels=True)
        for downstream, upstreams in mapping.items():
            if ".feat_" not in str(downstream):
                continue
            serialized = " ".join(str(upstream) for upstream in upstreams)
            self.assertNotIn("fraud_labels", serialized)
            self.assertNotIn("feast", serialized.lower())
            self.assertNotIn("ml", serialized.lower())

    def test_assertion_endpoint_defaults_to_runtime_profile(self) -> None:
        from data_platform.metadata.datahub.assertions.publish_assertions import (
            resolve_datahub_gms_url,
        )

        config = runtime("gcp")
        self.assertEqual(
            "https://datahub.example.test",
            resolve_datahub_gms_url(config, None),
        )
        self.assertEqual(
            "https://override.example.test",
            resolve_datahub_gms_url(
                config,
                "https://override.example.test/",
            ),
        )

    @patch(
        "data_platform.metadata.datahub.assertions.publish_assertions."
        "create_runtime_trino_connection"
    )
    def test_assertion_reader_passes_runtime_profile_to_trino_client(
        self,
        connection,
    ) -> None:
        from data_platform.metadata.datahub.assertions.publish_assertions import (
            create_trino_connection,
        )

        config = runtime("local")
        create_trino_connection(config)
        connection.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
