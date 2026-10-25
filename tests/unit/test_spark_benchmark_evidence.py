from __future__ import annotations

import unittest
from pathlib import Path

from data_platform.storage.scripts.config import (
    CORE_GOLD_TABLES,
    HISTORICAL_FEATURE_TABLES,
    silver_tables,
)
from ops.gcp.collect_spark_benchmark_evidence import parse_batch_evidence
from ops.gcp.compare_spark_benchmark import (
    STAGES,
    build_comparison,
    comparison_row,
    percentage_change,
)
from ops.gcp.validate_spark_output_equivalence import compare_summaries
from ops.gcp.summarize_spark_output import (
    expected_output_root,
    fingerprint_payload,
    stage_run_kind,
)
from ops.gcp.submit_source_to_bronze import CloudRuntime
from ops.gcp.submit_spark_output_summary import build_submission_command


class SparkBenchmarkEvidenceTest(unittest.TestCase):
    def test_summary_fingerprint_is_deterministic_and_stage_bound(self) -> None:
        self.assertEqual(
            fingerprint_payload(rows=3, total="12", minimum=-2, maximum=9),
            fingerprint_payload(rows=3, total="12", minimum=-2, maximum=9),
        )

    def test_summary_scan_is_rendered_as_read_only_dataproc_batch(self) -> None:
        benchmark = "ab-20261025123456"
        run_id = f"{benchmark}-baseline-bronze-r261025123456"
        runtime = CloudRuntime(
            project_id="project",
            region="asia-southeast1",
            data_bucket="bucket",
            temporary_bucket="temp",
            subnet="projects/project/regions/asia-southeast1/subnetworks/dev",
            service_account_email="spark@project.iam.gserviceaccount.com",
            performance_tier="standard",
        )
        command = build_submission_command(
            runtime,
            batch_id="validate-ab-baseline-dp1",
            benchmark_id=benchmark,
            spark_profile="baseline",
            stage="dp1-bronze",
            output_run_id=run_id,
            dataset_version="canonical-100gb-v1",
            git_sha="a" * 40,
            summary_uri=(
                f"gs://bucket/evidence/spark_benchmark/{benchmark}/"
                "baseline/dp1-bronze-summary.json"
            ),
            jar_paths=(Path("delta.jar"),),
        )
        self.assertIn("--version=2.2", command)
        self.assertIn("--jars=delta.jar", command)
        self.assertIn(f"--output-run-id={run_id}", command)
        self.assertIn(
            f"--root=gs://bucket/lakehouse/runs/{run_id}/bronze", command
        )
        self.assertEqual("feat", stage_run_kind("dp3-features"))
        self.assertEqual(
            "gs://bucket/lakehouse/runs/run-1/gold",
            expected_output_root(
                data_bucket="bucket", output_run_id="run-1", stage="dp3-features"
            ),
        )

    def test_batch_description_parses_usage_and_dcu_hours(self) -> None:
        value = parse_batch_evidence(
            {
                "name": (
                    "projects/p/locations/r/batches/"
                    "ab-20261025123456-optimized-bronze-r261025123456"
                ),
                "state": "SUCCEEDED",
                "createTime": "2026-10-25T01:00:00Z",
                "stateTime": "2026-10-25T01:10:00Z",
                "runtimeConfig": {
                    "version": "2.2",
                    "properties": {
                        "dataproc.tier": "standard",
                        "spark.dynamicAllocation.enabled": "true",
                        "spark.dynamicAllocation.initialExecutors": "6",
                        "spark.dynamicAllocation.maxExecutors": "7",
                        "spark.dynamicAllocation.profile": "performance",
                        "spark.sql.shuffle.partitions": "400",
                        "spark.sql.adaptive.enabled": "true",
                        "spark.sql.adaptive.skewJoin.enabled": "true",
                    },
                },
                "labels": {
                    "benchmark_id": "ab-20261025123456",
                    "spark_profile": "optimized",
                    "git_sha": "a" * 40,
                },
                "approximateUsage": {
                    "milliDcuSeconds": 7_200_000,
                    "shuffleStorageGbSeconds": 123.5,
                },
                "runtimeInfo": {"outputUri": "gs://logs/driver"},
            },
            benchmark_id="ab-20261025123456",
            spark_profile="optimized",
            stage="dp1-bronze",
            dataset_version="canonical-100gb-v1",
            git_sha="a" * 40,
            upstream_run_ids=["canonical-100gb-v1"],
            output_run_ids=["bronze-run"],
            validation={"status": "PASS"},
        )
        self.assertEqual(600, value["wall_clock_seconds"])
        self.assertEqual(2.0, value["dcu_hours"])
        self.assertEqual(123.5, value["shuffle_storage_gb_seconds"])
        self.assertEqual("gs://logs/driver", value["driver_output_uri"])

    def _summary(self, stage: str, profile: str) -> dict[str, object]:
        inventories = {
            "dp1-bronze": silver_tables(include_fraud_labels=True),
            "dp2-silver": silver_tables(include_fraud_labels=True),
            "dp2-gold": CORE_GOLD_TABLES,
            "dp3-features": HISTORICAL_FEATURE_TABLES,
        }
        invariant_names = {
            "dp1-bronze": ("source_coverage", "source_counts_match"),
            "dp2-silver": (
                "logical_transaction_count_match",
                "transaction_id_unique",
                "fraud_labels_separate",
            ),
            "dp2-gold": ("business_invariants", "no_label_leakage"),
            "dp3-features": (
                "grain_invariants",
                "event_timestamp_populated",
                "no_label_leakage",
            ),
        }
        return {
            "benchmark_id": "ab-20261025123456",
            "dataset_version": "canonical-100gb-v1",
            "git_sha": "abc123",
            "spark_profile": profile,
            "stage": stage,
            "tables": {
                name: {
                    "schema_sha256": f"schema-{name}",
                    "row_count": 10,
                    "content_fingerprint": f"fp-{name}",
                }
                for name in inventories[stage]
            },
            "invariants": {name: True for name in invariant_names[stage]},
        }

    def test_equivalence_checks_all_four_stage_contracts(self) -> None:
        for stage in ("dp1-bronze", "dp2-silver", "dp2-gold", "dp3-features"):
            result = compare_summaries(
                self._summary(stage, "baseline"),
                self._summary(stage, "optimized"),
            )
            self.assertTrue(result["output_equivalent"])

    def test_equivalence_reports_schema_or_invariant_difference(self) -> None:
        baseline = self._summary("dp2-silver", "baseline")
        optimized = self._summary("dp2-silver", "optimized")
        optimized["tables"]["transactions"]["row_count"] = 9
        optimized["invariants"]["transaction_id_unique"] = False
        result = compare_summaries(baseline, optimized)
        self.assertFalse(result["output_equivalent"])
        self.assertIn("transactions.row_count", result["differences"])
        self.assertIn(
            "optimized.invariant.transaction_id_unique", result["differences"]
        )

    def test_comparison_math_reports_negative_change_as_regression(self) -> None:
        self.assertEqual(25.0, percentage_change(100, 75))
        row = comparison_row(
            {
                "benchmark_id": "ab", "dataset_version": "canonical",
                "git_sha": "sha", "stage": "dp1-bronze", "batch_state": "SUCCEEDED",
                "spark_profile": "baseline", "runtime_version": "2.2",
                "runtime_properties": {"dataproc.tier": "standard"},
                "wall_clock_seconds": 100, "dcu_hours": 1,
                "shuffle_storage_gb_seconds": 10,
            },
            {
                "benchmark_id": "ab", "dataset_version": "canonical",
                "git_sha": "sha", "stage": "dp1-bronze", "batch_state": "SUCCEEDED",
                "spark_profile": "optimized", "runtime_version": "2.2",
                "runtime_properties": {"dataproc.tier": "standard"},
                "wall_clock_seconds": 120, "dcu_hours": 1.2,
                "shuffle_storage_gb_seconds": 11,
            },
            {"output_equivalent": True, "benchmark_id": "ab",
             "dataset_version": "canonical", "git_sha": "sha",
             "stage": "dp1-bronze"},
        )
        self.assertEqual("regression", row["runtime_result"])
        self.assertEqual(-20.0, row["runtime_change_pct"])
        self.assertEqual("regression", row["dcu_result"])

    def test_non_comparable_commits_are_rejected(self) -> None:
        baseline = {"benchmark_id": "ab", "dataset_version": "d", "git_sha": "a",
                    "stage": "dp1-bronze", "batch_state": "SUCCEEDED",
                    "spark_profile": "baseline", "runtime_version": "2.2",
                    "runtime_properties": {"dataproc.tier": "standard"},
                    "wall_clock_seconds": 1, "dcu_hours": 1,
                    "shuffle_storage_gb_seconds": 0}
        optimized = {**baseline, "git_sha": "b", "spark_profile": "optimized"}
        with self.assertRaisesRegex(ValueError, "git_sha"):
            comparison_row(
                baseline,
                optimized,
                {"output_equivalent": True, "benchmark_id": "ab",
                 "dataset_version": "d", "git_sha": "a",
                 "stage": "dp1-bronze"},
            )

    def test_comparison_rejects_commit_drift_between_stages(self) -> None:
        evidence = {}
        for stage in STAGES:
            baseline = {
                "benchmark_id": "ab",
                "dataset_version": "canonical",
                "git_sha": "same" if stage != "dp3-features" else "changed",
                "stage": stage,
                "batch_state": "SUCCEEDED",
                "spark_profile": "baseline",
                "runtime_version": "2.2",
                "runtime_properties": {"dataproc.tier": "standard"},
                "wall_clock_seconds": 10,
                "dcu_hours": 1,
                "shuffle_storage_gb_seconds": 1,
            }
            optimized = {**baseline, "spark_profile": "optimized"}
            equivalence = {
                "output_equivalent": True,
                "benchmark_id": baseline["benchmark_id"],
                "dataset_version": baseline["dataset_version"],
                "git_sha": baseline["git_sha"],
                "stage": stage,
            }
            evidence[stage] = (baseline, optimized, equivalence)
        with self.assertRaisesRegex(ValueError, "all DP1/DP2/DP3"):
            build_comparison(evidence)


if __name__ == "__main__":
    unittest.main()
