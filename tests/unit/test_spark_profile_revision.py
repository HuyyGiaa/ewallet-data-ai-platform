"""Regression tests for the 2026-11-11 Spark profile revision (5GB rehearsal).

Silver and DP3 optimized switch to size-driven AQE coalescing; every other
stage/profile must keep the exact properties it had before the revision.
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace

from data_platform.config import load_runtime_config
from data_platform.config.spark_profiles import (
    PROFILE_SQL_PROPERTY_KEYS,
    SPARK_PROFILES,
    SPARK_STAGES,
    SparkExecutionProfile,
    resolve_stage_profile,
)
from data_platform.processing.spark.common.spark_session import spark_options
from ops.gcp.dataproc_batch_properties import (
    build_dataproc_batch_properties,
    profile_summary,
    serialize_dataproc_batch_properties,
)

RESOURCES = (
    "spark.driver.cores=4",
    "spark.executor.cores=4",
    "spark.driver.memory=9600m",
    "spark.executor.memory=9600m",
    "spark.driver.memoryOverhead=3840m",
    "spark.executor.memoryOverhead=3840m",
    "spark.dynamicAllocation.enabled=true",
    "spark.dynamicAllocation.minExecutors=2",
    "spark.dynamicAllocation.initialExecutors=6",
    "spark.dynamicAllocation.maxExecutors=7",
    "spark.dynamicAllocation.executorAllocationRatio=0.3",
    "spark.sql.autoBroadcastJoinThreshold=192m",
    "spark.dynamicAllocation.executorIdleTimeout=300s",
)
BASELINE_SQL = (
    "spark.sql.shuffle.partitions=200",
    "spark.sql.adaptive.enabled=false",
    "spark.sql.adaptive.skewJoin.enabled=false",
)
REVISED_SQL = (
    "spark.sql.shuffle.partitions=200",
    "spark.sql.adaptive.enabled=true",
    "spark.sql.adaptive.skewJoin.enabled=false",
    "spark.sql.adaptive.coalescePartitions.enabled=true",
    "spark.sql.adaptive.coalescePartitions.parallelismFirst=false",
    "spark.sql.adaptive.advisoryPartitionSizeInBytes=67108864",
)
# Exact properties before the revision (commit 351cce3b), for the six unchanged profiles.
UNCHANGED = {
    ("dp1-bronze", "baseline"): RESOURCES + BASELINE_SQL,
    ("dp1-bronze", "optimized"): RESOURCES + (
        "spark.sql.shuffle.partitions=200",
        "spark.sql.adaptive.enabled=true",
        "spark.sql.adaptive.skewJoin.enabled=false",
        "spark.dynamicAllocation.profile=performance",
    ),
    ("dp2-silver", "baseline"): RESOURCES + BASELINE_SQL,
    ("dp2-gold", "baseline"): RESOURCES + BASELINE_SQL,
    ("dp2-gold", "optimized"): RESOURCES + (
        "spark.sql.shuffle.partitions=400",
        "spark.sql.adaptive.enabled=true",
        "spark.sql.adaptive.skewJoin.enabled=false",
    ),
    ("dp3-features", "baseline"): RESOURCES + BASELINE_SQL,
}
REVISED = {
    ("dp2-silver", "optimized"): RESOURCES + REVISED_SQL,
    ("dp3-features", "optimized"): RESOURCES + REVISED_SQL,
}
COALESCE_KEYS = (
    "spark.sql.adaptive.coalescePartitions.enabled",
    "spark.sql.adaptive.coalescePartitions.parallelismFirst",
    "spark.sql.adaptive.advisoryPartitionSizeInBytes",
)


def _gcp_config():
    return load_runtime_config("gcp", environ={
        "GCP_PROJECT_ID": "example-project",
        "GCP_REGION": "asia-southeast1",
        "EWALLET_DATA_BUCKET": "example-project-data",
        "DATAHUB_GMS_URL": "http://datahub.invalid:8080",
    })


class ProfileResolutionTest(unittest.TestCase):
    def test_all_eight_combinations_are_exact(self) -> None:
        expected = {**UNCHANGED, **REVISED}
        self.assertEqual(8, len(expected))
        for stage in SPARK_STAGES:
            for profile in SPARK_PROFILES:
                with self.subTest(stage=stage, profile=profile):
                    resolved = resolve_stage_profile(stage, profile)
                    self.assertEqual(expected[(stage, profile)], resolved.spark_properties)

    def test_revised_profiles_have_no_allocation_profile_and_differ_from_baseline(self) -> None:
        for stage in ("dp2-silver", "dp3-features"):
            optimized = resolve_stage_profile(stage, "optimized")
            baseline = resolve_stage_profile(stage, "baseline")
            self.assertIsNone(optimized.dynamic_allocation_profile)
            self.assertFalse(any(p.startswith("spark.dynamicAllocation.profile")
                                 for p in optimized.spark_properties))
            self.assertNotEqual(baseline.spark_properties, optimized.spark_properties)
            self.assertEqual(baseline.resources, optimized.resources)
            self.assertFalse(optimized.skew_join_enabled)

    def test_baselines_and_unchanged_optimized_emit_no_coalescing_keys(self) -> None:
        for (stage, profile), properties in UNCHANGED.items():
            keys = {item.partition("=")[0] for item in resolve_stage_profile(stage, profile).spark_properties}
            self.assertTrue(keys.isdisjoint(COALESCE_KEYS), (stage, profile))

    def test_evidence_exposes_the_revised_settings(self) -> None:
        evidence = resolve_stage_profile("silver", "optimized").evidence()
        self.assertEqual("dp2-silver:optimized", evidence["stage_profile"])
        self.assertIs(True, evidence["adaptive_enabled"])
        self.assertIs(True, evidence["coalesce_partitions_enabled"])
        self.assertIs(False, evidence["coalesce_parallelism_first"])
        self.assertEqual(67108864, evidence["advisory_partition_size_bytes"])
        self.assertIsNone(evidence["dynamic_allocation_profile"])
        self.assertEqual("67108864", evidence["spark_properties"]["spark.sql.adaptive.advisoryPartitionSizeInBytes"])
        baseline = resolve_stage_profile("feat", "baseline").evidence()
        self.assertIsNone(baseline["coalesce_partitions_enabled"])
        self.assertNotIn("spark.sql.adaptive.advisoryPartitionSizeInBytes", baseline["spark_properties"])

    def test_profile_summary_reports_every_setting(self) -> None:
        silver = "\n".join(profile_summary("optimized", stage="dp2-silver"))
        for line in ("AQE: true", "SHUFFLE_PARTITIONS: 200", "SKEW_JOIN: false",
                     "COALESCE_PARTITIONS: true", "COALESCE_PARALLELISM_FIRST: false",
                     "ADVISORY_PARTITION_SIZE_BYTES: 67108864",
                     "DYNAMIC_ALLOCATION_PROFILE: platform-neutral"):
            self.assertIn(line, silver)
        gold = "\n".join(profile_summary("optimized", stage="dp2-gold"))
        self.assertIn("COALESCE_PARTITIONS: spark-default", gold)
        self.assertIn("SHUFFLE_PARTITIONS: 400", gold)


class InvalidProfileTest(unittest.TestCase):
    def _base(self, **changes) -> SparkExecutionProfile:
        profile = SparkExecutionProfile(
            stage="dp2-silver", name="optimized", adaptive_enabled=True,
            skew_join_enabled=False, shuffle_partitions=200,
            coalesce_partitions_enabled=True, coalesce_parallelism_first=False,
            advisory_partition_size_bytes=64 * 1024 * 1024)
        return replace(profile, **changes)

    def test_advisory_size_must_be_positive(self) -> None:
        for bad in (0, -1, -67108864, True, 1.5, "64m"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self._base(advisory_partition_size_bytes=bad)

    def test_conflicting_settings_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._base(adaptive_enabled=False)  # coalescing needs AQE
        with self.assertRaises(ValueError):
            self._base(coalesce_partitions_enabled=None)  # advisory without coalescing
        with self.assertRaises(ValueError):
            self._base(coalesce_partitions_enabled=False)
        with self.assertRaises(ValueError):
            self._base(adaptive_enabled=False, coalesce_partitions_enabled=None,
                       coalesce_parallelism_first=None, advisory_partition_size_bytes=None,
                       skew_join_enabled=True)
        with self.assertRaises(ValueError):
            self._base(shuffle_partitions=0)
        with self.assertRaises(ValueError):
            self._base(coalesce_parallelism_first="false")

    def test_profiles_are_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            resolve_stage_profile("dp2-silver", "optimized").shuffle_partitions = 304  # type: ignore[misc]

    def test_unknown_stage_and_profile_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            resolve_stage_profile("dp4-serving", "optimized")
        with self.assertRaises(ValueError):
            resolve_stage_profile("dp2-silver", "fast")


class DataprocPropertiesTest(unittest.TestCase):
    def test_revised_stages_submit_exact_properties(self) -> None:
        for stage in ("dp2-silver", "dp3-features"):
            with self.subTest(stage=stage):
                properties = build_dataproc_batch_properties(
                    performance_tier="standard", data_bucket="example-bucket",
                    profile="optimized", stage=stage)
                common = (
                    "dataproc.tier=standard",
                    "spark.sql.extensions=io.delta.sql.DeltaSparkSessionExtension",
                    "spark.sql.catalog.spark_catalog=org.apache.spark.sql.delta.catalog.DeltaCatalog",
                    "spark.sql.session.timeZone=UTC",
                    "spark.eventLog.enabled=true",
                    "spark.eventLog.dir=gs://example-bucket/spark-events",
                )
                self.assertEqual(common + REVISED[(stage, "optimized")], properties)
                keys = [item.partition("=")[0] for item in properties]
                self.assertEqual(len(keys), len(set(keys)))
                serialized = serialize_dataproc_batch_properties(list(properties))
                self.assertEqual(properties, tuple(serialized.split(",")))
                self.assertNotIn("spark.dynamicAllocation.profile", serialized)

    def test_baseline_submission_has_no_coalescing_keys(self) -> None:
        for stage in ("dp2-silver", "dp3-features"):
            serialized = serialize_dataproc_batch_properties(list(build_dataproc_batch_properties(
                performance_tier="standard", data_bucket="b", profile="baseline", stage=stage)))
            for key in COALESCE_KEYS:
                self.assertNotIn(key, serialized)
            self.assertIn("spark.sql.adaptive.enabled=false", serialized)

    def test_duplicate_properties_are_rejected_at_the_submission_boundary(self) -> None:
        properties = list(build_dataproc_batch_properties(
            performance_tier="standard", data_bucket="b", profile="optimized", stage="dp2-silver"))
        properties.append("spark.sql.adaptive.enabled=false")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            serialize_dataproc_batch_properties(properties)


class SparkSessionOptionsTest(unittest.TestCase):
    def test_gcp_session_options_match_the_submitted_profile(self) -> None:
        config = _gcp_config()
        for stage in SPARK_STAGES:
            for optimized in (False, True):
                with self.subTest(stage=stage, optimized=optimized):
                    profile = resolve_stage_profile(stage, "optimized" if optimized else "baseline")
                    options = spark_options(config, stage=stage, optimized=optimized)
                    for key in PROFILE_SQL_PROPERTY_KEYS:
                        self.assertEqual(profile.sql_properties().get(key), options.get(key), key)

    def test_environment_cannot_leak_controlled_keys_into_a_profile(self) -> None:
        config = _gcp_config()
        polluted = replace(config, spark=replace(config.spark, configurations={
            **config.spark.configurations,
            "spark.sql.adaptive.advisoryPartitionSizeInBytes": "1m",
            "spark.sql.adaptive.coalescePartitions.enabled": "false",
            "spark.sql.adaptive.enabled": "true",
        }))
        baseline = spark_options(polluted, stage="dp2-silver", optimized=False)
        self.assertEqual("false", baseline["spark.sql.adaptive.enabled"])
        self.assertNotIn("spark.sql.adaptive.advisoryPartitionSizeInBytes", baseline)
        self.assertNotIn("spark.sql.adaptive.coalescePartitions.enabled", baseline)
        optimized = spark_options(polluted, stage="dp2-silver", optimized=True)
        self.assertEqual("67108864", optimized["spark.sql.adaptive.advisoryPartitionSizeInBytes"])
        self.assertEqual("true", optimized["spark.sql.adaptive.coalescePartitions.enabled"])

    def test_local_session_keeps_its_environment_shuffle_count(self) -> None:
        config = load_runtime_config("local", environ={"MINIO_ACCESS_KEY": "test-access", "MINIO_SECRET_KEY": "test-secret"})
        options = spark_options(config, stage="dp2-silver", optimized=True)
        self.assertEqual(str(config.spark.shuffle_partitions), options["spark.sql.shuffle.partitions"])
        self.assertEqual("true", options["spark.sql.adaptive.enabled"])
        self.assertEqual("67108864", options["spark.sql.adaptive.advisoryPartitionSizeInBytes"])


if __name__ == "__main__":
    unittest.main()
