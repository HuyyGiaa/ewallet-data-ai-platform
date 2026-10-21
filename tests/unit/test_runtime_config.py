from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from data_platform.config import (
    RuntimeConfig,
    StorageConfig,
    interpolate_environment,
    load_runtime_config,
)


PROFILE_TEMPLATE = """
environment: {environment}
project_id: {project_id}
region: {region}
storage:
  scheme: {scheme}
  source_root: {source_root}
  bronze_root: {bronze_root}
  silver_root: {silver_root}
  gold_root: {gold_root}
  feast_root: {feast_root}
  options:
    access_key: ${{STORAGE_ACCESS_KEY}}
spark:
  master: {master}
  shuffle_partitions: 32
  event_log_uri: {event_log_uri}
  packages: []
  configurations:
    spark.sql.session.timeZone: UTC
services:
  datahub_gms_url: ${{DATAHUB_GMS_URL:-http://localhost:8080}}
  trino_host: {trino_host}
  trino_port: {trino_port}
"""


def profile_text(environment: str) -> str:
    if environment == "local":
        values = {
            "environment": "local",
            "project_id": "null",
            "region": "null",
            "scheme": "s3a",
            "source_root": "file:///tmp/ewallet-source",
            "bronze_root": "s3a://bronze-zone",
            "silver_root": "s3a://silver-zone",
            "gold_root": "s3a://gold-zone",
            "feast_root": "file:///tmp/ewallet-feast",
            "master": "local[4]",
            "event_log_uri": "file:///tmp/spark-events",
            "trino_host": "localhost",
            "trino_port": "8081",
        }
    elif environment == "gcp":
        values = {
            "environment": "gcp",
            "project_id": "${GCP_PROJECT_ID}",
            "region": "${GCP_REGION:-asia-southeast1}",
            "scheme": "gs",
            "source_root": "gs://${EWALLET_DATA_BUCKET}/source",
            "bronze_root": "gs://${EWALLET_DATA_BUCKET}/lakehouse/bronze",
            "silver_root": "gs://${EWALLET_DATA_BUCKET}/lakehouse/silver",
            "gold_root": "gs://${EWALLET_DATA_BUCKET}/lakehouse/gold",
            "feast_root": "gs://${EWALLET_DATA_BUCKET}/features/offline",
            "master": "null",
            "event_log_uri": "gs://${EWALLET_DATA_BUCKET}/spark-events",
            "trino_host": "null",
            "trino_port": "null",
        }
    else:
        raise ValueError(f"Unsupported test profile: {environment}")

    return textwrap.dedent(PROFILE_TEMPLATE.format(**values)).lstrip()


class TemporaryProfiles:
    def __init__(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)

    def write(self, environment: str, content: str | None = None) -> Path:
        profile_directory = self.root / environment
        profile_directory.mkdir(parents=True, exist_ok=True)
        profile_path = profile_directory / "platform.yaml"
        profile_path.write_text(content or profile_text(environment))
        return profile_path

    def cleanup(self) -> None:
        self._temporary_directory.cleanup()


class RuntimeConfigLoaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = TemporaryProfiles()
        self.profiles.write("local")
        self.profiles.write("gcp")
        self.base_environ = {
            "STORAGE_ACCESS_KEY": "unit-test-access-key",
            "GCP_PROJECT_ID": "unit-test-project",
            "EWALLET_DATA_BUCKET": "unit-test-bucket",
        }

    def tearDown(self) -> None:
        self.profiles.cleanup()

    def test_loader_defaults_to_local_when_ewallet_env_is_absent(self) -> None:
        config = load_runtime_config(
            config_root=self.profiles.root,
            environ=self.base_environ,
        )

        self.assertEqual(config.environment, "local")
        self.assertEqual(config.storage.scheme, "s3a")

    def test_explicit_environment_overrides_ewallet_env(self) -> None:
        environ = {
            **self.base_environ,
            "EWALLET_ENV": "local",
        }

        config = load_runtime_config(
            "gcp",
            config_root=self.profiles.root,
            environ=environ,
        )

        self.assertEqual(config.environment, "gcp")
        self.assertEqual(config.storage.scheme, "gs")

    def test_required_placeholder_names_missing_variable_without_leaking_values(
        self,
    ) -> None:
        environ = {
            "UNRELATED_SECRET": "must-not-appear-in-errors",
        }

        with self.assertRaisesRegex(ValueError, "STORAGE_ACCESS_KEY") as raised:
            load_runtime_config(
                "local",
                config_root=self.profiles.root,
                environ=environ,
            )

        self.assertNotIn("must-not-appear-in-errors", str(raised.exception))

    def test_default_placeholder_uses_declared_default(self) -> None:
        rendered = interpolate_environment(
            {
                "endpoint": "${OPTIONAL_ENDPOINT:-http://localhost:8080}",
                "nested": ["${OPTIONAL_REGION:-asia-southeast1}"],
            },
            {},
        )

        self.assertEqual(rendered["endpoint"], "http://localhost:8080")
        self.assertEqual(rendered["nested"], ["asia-southeast1"])

    def test_unknown_environment_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            load_runtime_config(
                "production",
                config_root=self.profiles.root,
                environ=self.base_environ,
            )

    def test_unknown_top_level_key_is_rejected(self) -> None:
        invalid = profile_text("local") + "unexpected: true\n"
        self.profiles.write("local", invalid)

        with self.assertRaisesRegex(ValueError, "unexpected"):
            load_runtime_config(
                "local",
                config_root=self.profiles.root,
                environ=self.base_environ,
            )

    def test_missing_gold_root_is_rejected(self) -> None:
        invalid = profile_text("local").replace(
            "  gold_root: s3a://gold-zone\n",
            "",
        )
        self.profiles.write("local", invalid)

        with self.assertRaisesRegex(ValueError, "gold_root"):
            load_runtime_config(
                "local",
                config_root=self.profiles.root,
                environ=self.base_environ,
            )

    def test_spark_packages_must_be_a_yaml_list(self) -> None:
        invalid = profile_text("local").replace(
            "  packages: []\n",
            "  packages: io.delta:delta-spark_2.12:3.2.0\n",
        )
        self.profiles.write("local", invalid)

        with self.assertRaisesRegex(ValueError, "spark.packages"):
            load_runtime_config(
                "local",
                config_root=self.profiles.root,
                environ=self.base_environ,
            )

    def test_runtime_config_repr_redacts_storage_options(self) -> None:
        config = load_runtime_config(
            "local",
            config_root=self.profiles.root,
            environ=self.base_environ,
        )

        self.assertIsInstance(config, RuntimeConfig)
        self.assertNotIn("unit-test-access-key", repr(config))


class StorageConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = StorageConfig(
            scheme="gs",
            source_root="gs://bucket/source/",
            bronze_root="gs://bucket/lakehouse/bronze/",
            silver_root="gs://bucket/lakehouse/silver/",
            gold_root="gs://bucket/lakehouse/gold/",
            feast_root="gs://bucket/features/offline/",
            options={},
        )

    def test_table_uri_normalizes_trailing_slash(self) -> None:
        self.assertEqual(
            self.storage.table_uri("gold", "fact_transactions"),
            "gs://bucket/lakehouse/gold/fact_transactions",
        )

    def test_table_uri_rejects_empty_table_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "table_name"):
            self.storage.table_uri("gold", "")

    def test_table_uri_rejects_absolute_table_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "table_name"):
            self.storage.table_uri("gold", "/fact_transactions")

    def test_table_uri_rejects_parent_path_segment(self) -> None:
        with self.assertRaisesRegex(ValueError, "table_name"):
            self.storage.table_uri("gold", "../fact_transactions")


if __name__ == "__main__":
    unittest.main()
