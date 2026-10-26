from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_platform.ml.modeling.data import load_dataset
from data_platform.config import load_runtime_config
from data_platform.config.spark_profiles import (
    COMMON_RESOURCE_CONTRACT,
    SPARK_STAGES,
    benchmark_run_id,
    quota_ceiling,
    resolve_stage_profile,
)
from data_platform.processing.spark.common.spark_session import spark_options
from ops.gcp.dataproc_batch_properties import (
    CANONICAL_PERFORMANCE_PROFILE,
    DEFAULT_PERFORMANCE_PROFILE,
    build_dataproc_batch_properties,
    resolve_submission_profile,
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
        project_id="example-project",
        region="asia-southeast1",
        data_bucket="example-project-data",
        temporary_bucket="example-project-temp",
        subnet="projects/example-project/regions/asia-southeast1/subnetworks/example",
        service_account_email="spark@example-project.iam.gserviceaccount.com",
        performance_tier="standard",
    )


def properties(command: tuple[str, ...]) -> set[str]:
    item = next(value for value in command if value.startswith("--properties="))
    return set(item.removeprefix("--properties=").split(","))


class DataprocPerformanceProfileTest(unittest.TestCase):
    def test_default_profile_preserves_bounded_submission_behavior(self) -> None:
        values = set(
            build_dataproc_batch_properties(
                performance_tier="standard",
                data_bucket="example-project-data",
                profile=DEFAULT_PERFORMANCE_PROFILE,
            )
        )
        self.assertIn("dataproc.tier=standard", values)
        self.assertFalse(
            any(value.startswith("spark.dynamicAllocation.maxExecutors") for value in values)
        )

    def test_legacy_canonical_flag_resolves_to_stage_candidate_identity(self) -> None:
        self.assertEqual(
            "optimized",
            resolve_submission_profile(
                spark_profile=None,
                performance_profile=CANONICAL_PERFORMANCE_PROFILE,
            ),
        )

    def test_four_candidate_profiles_and_baselines_resolve_authoritatively(self) -> None:
        expected = {
            "dp1-bronze": (True, 200, "performance"),
            "dp2-silver": (False, 304, None),
            "dp2-gold": (True, 400, None),
            "dp3-features": (False, 200, None),
        }
        for stage in SPARK_STAGES:
            baseline = resolve_stage_profile(stage, "baseline")
            candidate = resolve_stage_profile(stage, "optimized")
            self.assertEqual((False, False, 200, None), (
                baseline.adaptive_enabled,
                baseline.skew_join_enabled,
                baseline.shuffle_partitions,
                baseline.dynamic_allocation_profile,
            ))
            self.assertEqual(
                expected[stage],
                (
                    candidate.adaptive_enabled,
                    candidate.shuffle_partitions,
                    candidate.dynamic_allocation_profile,
                ),
            )
            self.assertFalse(candidate.skew_join_enabled)
            self.assertEqual(baseline.resources, candidate.resources)

    def test_unknown_stage_and_profile_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            resolve_stage_profile("unknown", "baseline")
        with self.assertRaises(ValueError):
            resolve_stage_profile("dp1-bronze", "unknown")

    def test_profiles_share_quota_safe_common_resource_ceiling(self) -> None:
        self.assertEqual(
            {"process_count": 8, "vcpus": 32, "disk_gb": 3200},
            quota_ceiling("baseline"),
        )
        self.assertEqual(quota_ceiling("baseline"), quota_ceiling("optimized"))
        self.assertEqual(2, COMMON_RESOURCE_CONTRACT.min_executors)
        self.assertEqual(6, COMMON_RESOURCE_CONTRACT.initial_executors)
        self.assertEqual(7, COMMON_RESOURCE_CONTRACT.max_executors)

    def test_spark_application_uses_stage_specific_candidate(self) -> None:
        config = load_runtime_config(
            "gcp",
            environ={
                "GCP_PROJECT_ID": "example-project",
                "GCP_REGION": "asia-southeast1",
                "EWALLET_DATA_BUCKET": "example-project-data",
                "DATAHUB_GMS_URL": "http://datahub.invalid:8080",
            },
        )
        silver = spark_options(config, stage="dp2-silver", optimized=True)
        gold = spark_options(config, stage="dp2-gold", optimized=True)
        self.assertEqual("304", silver["spark.sql.shuffle.partitions"])
        self.assertEqual("false", silver["spark.sql.adaptive.enabled"])
        self.assertEqual("400", gold["spark.sql.shuffle.partitions"])
        self.assertEqual("true", gold["spark.sql.adaptive.enabled"])
        self.assertEqual("false", gold["spark.sql.adaptive.skewJoin.enabled"])

    def test_submitters_emit_only_their_stage_candidate_properties(self) -> None:
        benchmark = "ab-20261025123456"
        attempt = "r261025123456"
        common = dict(
            runtime=runtime(),
            dataset_version="canonical-100gb-v1",
            jar_paths=(Path("delta.jar"),),
            ttl="8h",
            spark_profile="optimized",
            benchmark_id=benchmark,
            git_sha="a" * 40,
        )
        bronze = benchmark_run_id(benchmark, "optimized", "bronze", attempt)
        silver = benchmark_run_id(benchmark, "optimized", "silver", attempt)
        gold = benchmark_run_id(benchmark, "optimized", "gold", attempt)
        feat = benchmark_run_id(benchmark, "optimized", "feat", attempt)
        commands = {
            "dp1-bronze": build_bronze(**common, run_id=bronze),
            "dp2-silver": build_silver(
                **common, source_bronze_run_id="selected-bronze-run", run_id=silver
            ),
            "dp2-gold": build_gold(
                **common, source_silver_run_id="selected-silver-run", run_id=gold
            ),
            "dp3-features": build_features(
                **common,
                source_silver_run_id="selected-silver-run",
                source_core_gold_run_id="selected-gold-run",
                run_id=feat,
            ),
        }
        expected_shuffle = {
            "dp1-bronze": "200",
            "dp2-silver": "304",
            "dp2-gold": "400",
            "dp3-features": "200",
        }
        for stage, command in commands.items():
            values = properties(command)
            self.assertIn("dataproc.tier=standard", values)
            self.assertIn("spark.dynamicAllocation.initialExecutors=6", values)
            self.assertIn("spark.dynamicAllocation.maxExecutors=7", values)
            self.assertIn(
                f"spark.sql.shuffle.partitions={expected_shuffle[stage]}", values
            )
            self.assertIn("spark.sql.adaptive.skewJoin.enabled=false", values)
            self.assertIn("--version=2.2", command)
            self.assertIn("--ttl=8h", command)
        self.assertIn("spark.dynamicAllocation.profile=performance", properties(commands["dp1-bronze"]))
        for stage in ("dp2-silver", "dp2-gold", "dp3-features"):
            self.assertNotIn("spark.dynamicAllocation.profile=performance", properties(commands[stage]))

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

    def test_invalid_output_lineage_is_rejected_even_with_explicit_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "not bound"):
            build_silver(
                runtime(),
                dataset_version="canonical-100gb-v1",
                source_bronze_run_id="selected-bronze-run",
                run_id="canonical-silver-20261025-100451",
                spark_profile="optimized",
                benchmark_id="ab-20261025123456",
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
