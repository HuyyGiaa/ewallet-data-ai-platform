from __future__ import annotations

import unittest
from types import ModuleType
from unittest.mock import MagicMock, patch

from data_platform.config import load_runtime_config
from data_platform.processing.spark.common.spark_session import (
    _get_or_create_spark,
    spark_options,
)


LOCAL_ENVIRONMENT = {
    "MINIO_ACCESS_KEY": "spark-test-access",
    "MINIO_SECRET_KEY": "spark-test-secret",
}
GCP_ENVIRONMENT = {
    "GCP_PROJECT_ID": "spark-test-project",
    "GCP_REGION": "asia-southeast1",
    "EWALLET_DATA_BUCKET": "spark-test-bucket",
    "DATAHUB_GMS_URL": "http://datahub-gms.example:8080",
}


class SparkRuntimeOptionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.local_config = load_runtime_config(
            "local",
            environ=LOCAL_ENVIRONMENT,
        )
        cls.gcp_config = load_runtime_config(
            "gcp",
            environ=GCP_ENVIRONMENT,
        )

    def test_local_options_include_profile_s3a_connector(self) -> None:
        options = spark_options(self.local_config, optimized=True)

        self.assertEqual(
            options["spark.hadoop.fs.s3a.endpoint"],
            "http://localhost:9000",
        )
        self.assertEqual(
            options["spark.hadoop.fs.s3a.access.key"],
            "spark-test-access",
        )
        self.assertEqual(
            options["spark.hadoop.fs.s3a.secret.key"],
            "spark-test-secret",
        )
        self.assertEqual(
            options["spark.hadoop.fs.s3a.impl"],
            "org.apache.hadoop.fs.s3a.S3AFileSystem",
        )

    def test_gcp_options_exclude_local_master_and_s3a_settings(self) -> None:
        options = spark_options(self.gcp_config, optimized=True)

        self.assertIsNone(self.gcp_config.spark.master)
        self.assertNotIn("spark.master", options)
        self.assertFalse(any("s3a" in key.lower() for key in options))
        self.assertNotIn("spark-test-access", options.values())
        self.assertNotIn("spark-test-secret", options.values())

    def test_optimized_flag_changes_only_aqe_and_skew_options(self) -> None:
        optimized = spark_options(self.local_config, optimized=True)
        baseline = spark_options(self.local_config, optimized=False)
        differing_keys = {
            key
            for key in optimized
            if optimized[key] != baseline[key]
        }

        self.assertEqual(
            differing_keys,
            {
                "spark.sql.adaptive.enabled",
                "spark.sql.adaptive.skewJoin.enabled",
            },
        )
        self.assertEqual(optimized["spark.sql.adaptive.enabled"], "true")
        self.assertEqual(baseline["spark.sql.adaptive.enabled"], "false")
        self.assertEqual(
            optimized["spark.sql.adaptive.skewJoin.enabled"],
            "true",
        )
        self.assertEqual(
            baseline["spark.sql.adaptive.skewJoin.enabled"],
            "false",
        )

    def test_event_log_and_shuffle_partitions_come_from_profile(self) -> None:
        local_options = spark_options(self.local_config, optimized=True)
        gcp_options = spark_options(self.gcp_config, optimized=True)

        self.assertEqual(local_options["spark.sql.shuffle.partitions"], "32")
        self.assertEqual(
            local_options["spark.eventLog.dir"],
            "file:///tmp/spark-events",
        )
        self.assertEqual(gcp_options["spark.sql.shuffle.partitions"], "400")
        self.assertEqual(
            gcp_options["spark.eventLog.dir"],
            "gs://spark-test-bucket/spark-events",
        )

    def test_packages_are_managed_separately_from_spark_options(self) -> None:
        local_options = spark_options(self.local_config, optimized=True)
        gcp_options = spark_options(self.gcp_config, optimized=True)

        self.assertNotIn("spark.jars.packages", local_options)
        self.assertNotIn("spark.jars.packages", gcp_options)
        self.assertEqual(
            self.local_config.spark.packages,
            ("org.apache.hadoop:hadoop-aws:3.4.2",),
        )
        self.assertEqual(self.gcp_config.spark.packages, ())

    def test_gcp_session_uses_pre_supplied_jars_without_maven_resolution(
        self,
    ) -> None:
        builder = MagicMock()
        expected_session = builder.getOrCreate.return_value

        actual_session = _get_or_create_spark(builder, self.gcp_config)

        self.assertIs(actual_session, expected_session)
        builder.getOrCreate.assert_called_once_with()

    def test_local_session_resolves_configured_packages(self) -> None:
        builder = MagicMock()
        configured_builder = MagicMock()
        configure = MagicMock(return_value=configured_builder)
        delta_module = ModuleType("delta")
        delta_module.configure_spark_with_delta_pip = configure

        with patch.dict("sys.modules", {"delta": delta_module}):
            actual_session = _get_or_create_spark(builder, self.local_config)

        self.assertIs(actual_session, configured_builder.getOrCreate.return_value)
        configure.assert_called_once_with(
            builder,
            extra_packages=["org.apache.hadoop:hadoop-aws:3.4.2"],
        )
        configured_builder.getOrCreate.assert_called_once_with()
        builder.getOrCreate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
