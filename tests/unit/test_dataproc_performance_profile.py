from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from ai_platform.ml.modeling.data import load_dataset

from data_platform.config import load_runtime_config
from data_platform.config.spark_profiles import (
    BASELINE_PROFILE,
    OPTIMIZED_PROFILE,
    benchmark_run_id,
    quota_ceiling,
)
from data_platform.processing.spark.common.spark_session import spark_options
from ops.gcp.dataproc_batch_properties import (
    CANONICAL_PERFORMANCE_PROFILE,
    DEFAULT_PERFORMANCE_PROFILE,
    build_dataproc_batch_properties,
)
from ops.gcp.submit_bronze_to_silver import build_submission_command as build_silver
from ops.gcp.submit_historical_features import build_submission_command as build_features
from ops.gcp.submit_silver_to_gold import build_submission_command as build_gold
from ops.gcp.submit_source_to_bronze import (
    CloudRuntime,
    assert_bronze_prefix_absent,
    build_submission_command as build_bronze,
)


def runtime() -> CloudRuntime:
    return CloudRuntime(
        project_id="example-project", region="asia-southeast1",
        data_bucket="example-project-data", temporary_bucket="example-project-temp",
        subnet="projects/example-project/regions/asia-southeast1/subnetworks/example",
        service_account_email="spark@example-project.iam.gserviceaccount.com",
        performance_tier="standard",
    )


def properties(command: tuple[str, ...]) -> set[str]:
    item = next(value for value in command if value.startswith("--properties="))
    return set(item.removeprefix("--properties=").split(","))


class DataprocPerformanceProfileTest(unittest.TestCase):
    def test_default_profile_preserves_bounded_submission_behavior(self) -> None:
        values = set(build_dataproc_batch_properties(
            performance_tier="standard", data_bucket="example-project-data",
            profile=DEFAULT_PERFORMANCE_PROFILE,
        ))
        self.assertIn("dataproc.tier=standard", values)
        self.assertFalse(any(value.startswith("spark.dynamicAllocation.maxExecutors") for value in values))

    def test_baseline_and_optimized_have_explicit_defensible_differences(self) -> None:
        baseline = set(build_dataproc_batch_properties(
            performance_tier="standard", data_bucket="example-project-data", profile="baseline"
        ))
        optimized = set(build_dataproc_batch_properties(
            performance_tier="standard", data_bucket="example-project-data", profile="optimized"
        ))
        for values in (baseline, optimized):
            self.assertIn("dataproc.tier=standard", values)
            self.assertIn("spark.dynamicAllocation.maxExecutors=7", values)
        self.assertIn("spark.sql.adaptive.enabled=false", baseline)
        self.assertIn("spark.sql.adaptive.skewJoin.enabled=false", baseline)
        self.assertIn("spark.sql.shuffle.partitions=200", baseline)
        self.assertIn("spark.dynamicAllocation.initialExecutors=2", baseline)
        self.assertFalse(any(value == "spark.dynamicAllocation.profile=performance" for value in baseline))
        self.assertIn("spark.sql.adaptive.enabled=true", optimized)
        self.assertIn("spark.sql.adaptive.skewJoin.enabled=true", optimized)
        self.assertIn("spark.sql.shuffle.partitions=400", optimized)
        self.assertIn("spark.dynamicAllocation.initialExecutors=6", optimized)
        self.assertIn("spark.dynamicAllocation.profile=performance", optimized)

    def test_canonical_alias_is_the_optimized_production_profile(self) -> None:
        canonical = build_dataproc_batch_properties(
            performance_tier="standard", data_bucket="example-project-data",
            profile=CANONICAL_PERFORMANCE_PROFILE,
        )
        optimized = build_dataproc_batch_properties(
            performance_tier="standard", data_bucket="example-project-data",
            profile="optimized",
        )
        self.assertEqual(canonical, optimized)

    def test_profiles_share_the_same_quota_safe_ceiling(self) -> None:
        self.assertEqual({"process_count": 8, "vcpus": 32, "disk_gb": 3200}, quota_ceiling("baseline"))
        self.assertEqual(quota_ceiling("baseline"), quota_ceiling("optimized"))
        self.assertEqual(7, BASELINE_PROFILE.max_executors)
        self.assertEqual(7, OPTIMIZED_PROFILE.max_executors)

    def test_spark_application_profile_matches_submission_profile(self) -> None:
        config = load_runtime_config("gcp", environ={
            "GCP_PROJECT_ID": "example-project", "GCP_REGION": "asia-southeast1",
            "EWALLET_DATA_BUCKET": "example-project-data",
            "DATAHUB_GMS_URL": "http://datahub.invalid:8080",
        })
        baseline = spark_options(config, optimized=False)
        optimized = spark_options(config, optimized=True)
        self.assertEqual("200", baseline["spark.sql.shuffle.partitions"])
        self.assertEqual("false", baseline["spark.sql.adaptive.enabled"])
        self.assertEqual("400", optimized["spark.sql.shuffle.partitions"])
        self.assertEqual("true", optimized["spark.sql.adaptive.enabled"])

    def test_all_stages_propagate_profile_and_benchmark_lineage(self) -> None:
        benchmark = "ab-20261025123456"; attempt = "r261025123456"
        bronze = benchmark_run_id(benchmark, "baseline", "bronze", attempt)
        silver = benchmark_run_id(benchmark, "baseline", "silver", attempt)
        gold = benchmark_run_id(benchmark, "baseline", "gold", attempt)
        feat = benchmark_run_id(benchmark, "baseline", "feat", attempt)
        common = dict(runtime=runtime(), dataset_version="canonical-100gb-v1",
                      jar_paths=(Path("delta.jar"),), ttl="8h",
                      spark_profile="baseline", benchmark_id=benchmark,
                      git_sha="a" * 40)
        commands = (
            build_bronze(**common, run_id=bronze),
            build_silver(**common, source_bronze_run_id=bronze, run_id=silver),
            build_gold(**common, source_silver_run_id=silver, run_id=gold),
            build_features(**common, source_silver_run_id=silver,
                           source_core_gold_run_id=gold, run_id=feat),
        )
        for command in commands:
            self.assertIn("--version=2.2", command)
            self.assertIn("--ttl=8h", command)
            self.assertIn(f"--benchmark-id={benchmark}", command)
            self.assertIn("--spark-profile=baseline", command)
            self.assertIn("--git-sha=" + "a" * 40, command)
            self.assertIn("dataproc.tier=standard", properties(command))
        self.assertIn("--mode=baseline", commands[1])
        self.assertIn("--mode=baseline", commands[2])
        self.assertIn("--mode=baseline", commands[3])

    def test_cross_profile_upstream_is_rejected_before_submission(self) -> None:
        benchmark = "ab-20261025123456"; attempt = "r261025123456"
        optimized_bronze = benchmark_run_id(benchmark, "optimized", "bronze", attempt)
        with self.assertRaisesRegex(ValueError, "not bound"):
            build_silver(
                runtime(), dataset_version="canonical-100gb-v1",
                source_bronze_run_id=optimized_bronze,
                run_id=benchmark_run_id(benchmark, "baseline", "silver", attempt),
                spark_profile="baseline", benchmark_id=benchmark,
                git_sha="a" * 40,
                ttl="8h",
                jar_paths=(Path("delta.jar"),),
            )

    def test_partial_benchmark_metadata_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be supplied together"):
            build_bronze(
                runtime(),
                dataset_version="canonical-100gb-v1",
                run_id="safe-default-run",
                spark_profile="baseline",
                jar_paths=(Path("delta.jar"),),
            )

    def test_canonical_ml_loader_rejects_baseline_lineage_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "dataset_version": "canonical-100gb-v1",
                        "spark_profile": "baseline",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "optimized Spark lineage"):
                load_dataset(root)

    def test_abandoned_canonical_run_cannot_enter_benchmark_lineage(self) -> None:
        with self.assertRaisesRegex(ValueError, "not bound"):
            build_silver(
                runtime(), dataset_version="canonical-100gb-v1",
                source_bronze_run_id="canonical-bronze-20261025-084645",
                run_id="canonical-silver-20261025-100451",
                spark_profile="optimized", benchmark_id="ab-20261025123456",
                git_sha="a" * 40,
                ttl="8h",
                jar_paths=(Path("delta.jar"),),
            )

    def test_existing_bronze_prefix_is_an_immutable_collision(self) -> None:
        class Existing:
            returncode = 0
            stdout = "gs://example-project-data/lakehouse/runs/run/bronze/users/\n"
            stderr = ""

        with self.assertRaisesRegex(ValueError, "immutable"):
            assert_bronze_prefix_absent(
                runtime(),
                run_id="ab-20261025123456-baseline-bronze-r261025123456",
                runner=lambda *args, **kwargs: Existing(),
            )


if __name__ == "__main__":
    unittest.main()
