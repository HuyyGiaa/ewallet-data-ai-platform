from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from data_platform.config.spark_benchmark import (
    METHOD2_OUTPUT_RUN_PROPERTY,
    METHOD2_PAIR_DIGEST_PROPERTY,
    ORIGINAL_PERFORMANCE_SCHEMA,
    SEMANTIC_SUMMARY_SCHEMA,
    original_performance_digest,
    semantic_summary_digest,
)
from data_platform.storage.scripts.config import silver_tables
from ops.gcp.build_spark_validation_evidence import build_validation_evidence
from ops.gcp.prepare_spark_stage_pair import build_stage_pair_manifest
from ops.gcp.summarize_spark_event_log import summarize_event_logs
from ops.gcp.summarize_spark_event_log import DATAPROC_COHORT_PROPERTY
from ops.gcp.summarize_spark_output import write_summary


BENCHMARK = "ab-20261029174838"
GIT_SHA = "62f17ba6fe306b5cf74621b787249f8b71ccd8e8"
BASELINE_RUN = f"{BENCHMARK}-baseline-bronze-r261029174838"
OPTIMIZED_RUN = f"{BENCHMARK}-optimized-bronze-r261029174838"


def pair_manifest():
    return build_stage_pair_manifest(
        stage="dp1-bronze",
        dataset_version="canonical-100gb-v1",
        git_sha=GIT_SHA,
        upstream_run_ids=("canonical-100gb-v1",),
        baseline_output_run_id=BASELINE_RUN,
        baseline_output_root=f"gs://bucket/lakehouse/runs/{BASELINE_RUN}/bronze",
        candidate_output_run_id=OPTIMIZED_RUN,
        candidate_output_root=f"gs://bucket/lakehouse/runs/{OPTIMIZED_RUN}/bronze",
    )


def semantic_summary(manifest, profile="baseline"):
    member = next(run for run in manifest["runs"] if run["spark_profile"] == profile)
    result = {
        "schema_version": SEMANTIC_SUMMARY_SCHEMA,
        "status": "COMPLETE",
        "pair_manifest_digest": manifest["pair_manifest_digest"],
        "benchmark_id": BENCHMARK,
        "dataset_version": "canonical-100gb-v1",
        "git_sha": GIT_SHA,
        "spark_profile": profile,
        "stage": "dp1-bronze",
        "output_run_id": member["output_run_id"],
        "output_root": member["output_root"],
        "source_run_ids": ["canonical-100gb-v1"],
        "upstream_run_ids": ["canonical-100gb-v1"],
        "spark_properties": member["spark_properties"],
        "spark_properties_digest": member["spark_properties_digest"],
        "execution": {
            "kind": "read_only_semantic_scan",
            "validation_batch_id": f"validate-{profile}-261030120000",
            "validation_git_sha": "b" * 40,
            "source_output_run_id": member["output_run_id"],
        },
        "tables": {
            name: {
                "schema_sha256": f"schema-{name}",
                "row_count": 1,
                "semantic_content_fingerprint": f"fingerprint-{name}",
                "required_null_counts": {},
                "partition_columns": [],
                "physical_layout": {
                    "file_count": 2,
                    "total_bytes": 100,
                    "file_size_min": 50,
                    "file_size_median": 50,
                    "file_size_p95": 50,
                    "file_size_max": 50,
                },
            }
            for name in silver_tables(include_fraud_labels=True)
        },
        "invariants": {"source_coverage": True, "source_counts_match": True},
    }
    result["semantic_summary_digest"] = semantic_summary_digest(result)
    return result


def original_performance(manifest, output_run_id=BASELINE_RUN):
    result = {
        "schema_version": ORIGINAL_PERFORMANCE_SCHEMA,
        "source_execution_kind": "original_ingestion_batch",
        "batch_id": output_run_id,
        "output_run_id": output_run_id,
        "pair_manifest_digest": manifest["pair_manifest_digest"],
        "event_log_source_uri": "gs://bucket/spark-events/application-123",
        "application_runtime_seconds": 900.0,
        "spill_bytes": 123,
        "task_count": 456,
    }
    result["original_performance_digest"] = original_performance_digest(result)
    return result


def write_event_log(path: Path, manifest, output_run_id=BASELINE_RUN):
    events = [
        {
            "Event": "SparkListenerApplicationStart",
            "App Name": f"projects/p/locations/r/batches/{output_run_id}",
            "App ID": "application-123",
            "Timestamp": 1_000,
        },
        {
            "Event": "SparkListenerEnvironmentUpdate",
            "Spark Properties": {
                "spark.app.id": "application-123",
                METHOD2_PAIR_DIGEST_PROPERTY: manifest["pair_manifest_digest"],
                METHOD2_OUTPUT_RUN_PROPERTY: output_run_id,
                DATAPROC_COHORT_PROPERTY: "cohort-123",
            },
        },
        {
            "Event": "SparkListenerTaskEnd",
            "Task End Reason": {"Reason": "Success"},
            "Task Metrics": {
                "Memory Bytes Spilled": 7,
                "Disk Bytes Spilled": 11,
            },
        },
        {"Event": "SparkListenerJobEnd"},
        {"Event": "SparkListenerStageCompleted"},
        {"Event": "SparkListenerApplicationEnd", "Timestamp": 11_000},
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")


class SparkPostRunIntegrationTest(unittest.TestCase):
    def test_event_log_metrics_are_bound_to_original_execution(self) -> None:
        manifest = pair_manifest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "eventlog"
            write_event_log(path, manifest)
            result = summarize_event_logs(
                (path,),
                batch_id=BASELINE_RUN,
                output_run_id=BASELINE_RUN,
                pair_manifest_digest=manifest["pair_manifest_digest"],
                source_uri="gs://bucket/spark-events/application-123",
                expected_dataproc_cohort_id="cohort-123",
                expected_application_name=f"projects/p/locations/r/batches/{BASELINE_RUN}",
                application_not_before_epoch_ms=0,
                application_not_after_epoch_ms=20_000,
            )
        self.assertEqual(10.0, result["application_runtime_seconds"])
        self.assertEqual(18, result["spill_bytes"])
        self.assertEqual(1, result["task_count"])
        self.assertEqual("original_ingestion_batch", result["source_execution_kind"])

    def test_event_log_rejects_wrong_output_or_pair_identity(self) -> None:
        manifest = pair_manifest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "eventlog"
            write_event_log(path, manifest)
            with self.assertRaisesRegex(ValueError, "batch_id"):
                summarize_event_logs(
                    (path,), batch_id="", output_run_id=BASELINE_RUN,
                    pair_manifest_digest=manifest["pair_manifest_digest"],
                    source_uri="gs://bucket/spark-events/application-123",
                    expected_dataproc_cohort_id="cohort-123",
                    expected_application_name=f"projects/p/locations/r/batches/{BASELINE_RUN}",
                    application_not_before_epoch_ms=0,
                    application_not_after_epoch_ms=20_000,
                )
            recovered = summarize_event_logs(
                (path,), batch_id="distinct-recovery-attempt",
                output_run_id=BASELINE_RUN,
                pair_manifest_digest=manifest["pair_manifest_digest"],
                source_uri="gs://bucket/spark-events/application-123",
                expected_dataproc_cohort_id="cohort-123",
                expected_application_name=f"projects/p/locations/r/batches/{BASELINE_RUN}",
                application_not_before_epoch_ms=0,
                application_not_after_epoch_ms=20_000,
            )
            self.assertEqual("distinct-recovery-attempt", recovered["batch_id"])
            self.assertEqual(BASELINE_RUN, recovered["output_run_id"])
            with self.assertRaisesRegex(ValueError, "property mismatch"):
                summarize_event_logs(
                    (path,), batch_id=BASELINE_RUN, output_run_id=BASELINE_RUN,
                    pair_manifest_digest="sha256:" + "0" * 64,
                    source_uri="gs://bucket/spark-events/application-123",
                    expected_dataproc_cohort_id="cohort-123",
                    expected_application_name=f"projects/p/locations/r/batches/{BASELINE_RUN}",
                    application_not_before_epoch_ms=0,
                    application_not_after_epoch_ms=20_000,
                )

    def test_event_log_rejects_wrong_cohort_and_batch_interval(self) -> None:
        manifest = pair_manifest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "eventlog"
            write_event_log(path, manifest)
            common = dict(
                batch_id=BASELINE_RUN, output_run_id=BASELINE_RUN,
                pair_manifest_digest=manifest["pair_manifest_digest"],
                source_uri="gs://bucket/spark-events/application-123",
                expected_application_name=f"projects/p/locations/r/batches/{BASELINE_RUN}",
                application_not_before_epoch_ms=0,
                application_not_after_epoch_ms=20_000,
            )
            with self.assertRaisesRegex(ValueError, "cohort"):
                summarize_event_logs((path,), expected_dataproc_cohort_id="wrong", **common)
            common["application_not_before_epoch_ms"] = 2_000
            with self.assertRaisesRegex(ValueError, "outside"):
                summarize_event_logs((path,), expected_dataproc_cohort_id="cohort-123", **common)

    def test_builder_preserves_original_metrics_and_layout_metrics(self) -> None:
        manifest = pair_manifest()
        result = build_validation_evidence(
            semantic_summary(manifest),
            original_performance(manifest),
            pair_manifest=manifest,
            spark_profile="baseline",
        )
        self.assertEqual(900.0, result["application_runtime_seconds"])
        self.assertEqual(123, result["spill_bytes"])
        self.assertEqual(456, result["task_count"])
        self.assertEqual(16, result["output_file_count"])
        self.assertEqual(800, result["output_total_bytes"])
        self.assertNotEqual(
            result["metric_provenance"]["validation_batch_id"],
            result["metric_provenance"]["ingestion_batch_id"],
        )

    def test_builder_preserves_distinct_ingestion_attempt_identity(self) -> None:
        manifest = pair_manifest()
        performance = original_performance(manifest)
        performance["batch_id"] = "distinct-recovery-attempt"
        performance["original_performance_digest"] = original_performance_digest(
            performance
        )
        result = build_validation_evidence(
            semantic_summary(manifest), performance,
            pair_manifest=manifest, spark_profile="baseline",
        )
        self.assertEqual(
            "distinct-recovery-attempt",
            result["metric_provenance"]["ingestion_batch_id"],
        )
        self.assertEqual(BASELINE_RUN, result["output_run_id"])

    def test_builder_rejects_missing_metrics_and_inconsistent_identity(self) -> None:
        manifest = pair_manifest()
        for mutation, message in (
            (lambda item: item.pop("task_count"), "task_count"),
            (lambda item: item.update(output_run_id=OPTIMIZED_RUN), "output_run_id"),
        ):
            performance = original_performance(manifest)
            mutation(performance)
            performance["original_performance_digest"] = original_performance_digest(
                performance
            )
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                build_validation_evidence(
                    semantic_summary(manifest), performance,
                    pair_manifest=manifest, spark_profile="baseline",
                )

    def test_builder_rejects_validation_job_reusing_ingestion_identity(self) -> None:
        manifest = pair_manifest()
        summary = semantic_summary(manifest)
        summary["execution"]["validation_batch_id"] = summary["output_run_id"]
        summary["semantic_summary_digest"] = semantic_summary_digest(summary)
        with self.assertRaisesRegex(ValueError, "distinct"):
            build_validation_evidence(
                summary, original_performance(manifest),
                pair_manifest=manifest, spark_profile="baseline",
            )

    def test_local_summary_output_is_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.json"
            write_summary(str(output), {"status": "COMPLETE"})
            with self.assertRaises(FileExistsError):
                write_summary(str(output), {"status": "REPLACED"})
            self.assertEqual("COMPLETE", json.loads(output.read_text())["status"])

    def test_tampered_summary_digest_is_rejected(self) -> None:
        manifest = pair_manifest()
        summary = copy.deepcopy(semantic_summary(manifest))
        summary["tables"]["transactions"]["row_count"] = 2
        with self.assertRaisesRegex(ValueError, "digest"):
            build_validation_evidence(
                summary, original_performance(manifest),
                pair_manifest=manifest, spark_profile="baseline",
            )


if __name__ == "__main__":
    unittest.main()
