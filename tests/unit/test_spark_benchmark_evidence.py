from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from data_platform.config.spark_benchmark import (
    METHOD2_OUTPUT_RUN_PROPERTY,
    METHOD2_PAIR_DIGEST_PROPERTY,
    SEMANTIC_SUMMARY_SCHEMA,
    VALIDATION_EVIDENCE_SCHEMA,
    semantic_summary_digest,
    validation_evidence_digest,
)
from data_platform.config.spark_profiles import resolve_stage_profile
from data_platform.storage.scripts.config import (
    CORE_GOLD_TABLES,
    HISTORICAL_FEATURE_TABLES,
    silver_tables,
)
from ops.gcp.collect_spark_benchmark_evidence import parse_batch_evidence
from ops.gcp.compare_spark_benchmark import (
    comparison_row,
    main as compare_main,
    percentage_change,
)
from ops.gcp.prepare_spark_stage_pair import build_stage_pair_manifest
from ops.gcp.summarize_spark_output import (
    expected_output_root,
    fingerprint_payload,
    stage_run_kind,
)
from ops.gcp.submit_source_to_bronze import CloudRuntime
from ops.gcp.submit_spark_output_summary import (
    build_submission_command,
    require_clean_validation_source,
)
from ops.gcp.validate_spark_output_equivalence import compare_summaries


BENCHMARK = "ab-20261025123456"
ATTEMPT = "r261025123456"
GIT_SHA = "a" * 40
DATASET = "canonical-100gb-v1"
RUN_KIND = {
    "dp1-bronze": "bronze",
    "dp2-silver": "silver",
    "dp2-gold": "gold",
    "dp3-features": "feat",
}
LAYER = {
    "dp1-bronze": "bronze",
    "dp2-silver": "silver",
    "dp2-gold": "gold",
    "dp3-features": "gold",
}


def pair(stage="dp1-bronze"):
    upstream = (
        (DATASET,)
        if stage == "dp1-bronze"
        else (("selected-silver", "selected-gold") if stage == "dp3-features" else ("selected-run",))
    )
    kind = RUN_KIND[stage]
    baseline = f"{BENCHMARK}-baseline-{kind}-{ATTEMPT}"
    optimized = f"{BENCHMARK}-optimized-{kind}-{ATTEMPT}"
    return build_stage_pair_manifest(
        stage=stage,
        dataset_version=DATASET,
        git_sha=GIT_SHA,
        upstream_run_ids=upstream,
        baseline_output_run_id=baseline,
        baseline_output_root=f"gs://bucket/lakehouse/runs/{baseline}/{LAYER[stage]}",
        candidate_output_run_id=optimized,
        candidate_output_root=f"gs://bucket/lakehouse/runs/{optimized}/{LAYER[stage]}",
    )


def evidence(manifest, profile, runtime=10.0):
    member = next(
        item for item in manifest["runs"] if item["spark_profile"] == profile
    )
    return {
        "status": "PASS",
        "batch_state": "SUCCEEDED",
        "pair_manifest_digest": manifest["pair_manifest_digest"],
        "benchmark_id": manifest["benchmark_id"],
        "dataset_version": manifest["dataset_version"],
        "git_sha": manifest["git_sha"],
        "stage": manifest["stage"],
        "upstream_run_ids": list(manifest["upstream_run_ids"]),
        "spark_profile": profile,
        "output_run_id": member["output_run_id"],
        "output_root": member["output_root"],
        "spark_properties": member["spark_properties"],
        "spark_properties_digest": member["spark_properties_digest"],
        "runtime_version": "2.2",
        "runtime_properties": {"dataproc.tier": "standard"},
        "application_runtime_seconds": runtime,
        "batch_runtime_seconds": runtime + 1,
        "dcu_hours": runtime / 10,
        "shuffle_storage_gb_seconds": 1,
        "spill_bytes": 0,
        "task_count": 1,
        "output_file_count": 1,
        "output_total_bytes": 1,
    }


def validation_evidence(manifest, profile, runtime=10.0):
    value = evidence(manifest, profile, runtime)
    value.update(
        schema_version=VALIDATION_EVIDENCE_SCHEMA,
        status="PASS",
        metric_provenance={
            "application_runtime_seconds": "original_spark_event_log",
            "spill_bytes": "original_spark_event_log",
            "task_count": "original_spark_event_log",
            "output_file_count": "semantic_summary_physical_layout",
            "output_total_bytes": "semantic_summary_physical_layout",
            "ingestion_batch_id": value["output_run_id"],
            "validation_batch_id": f"validate-{profile}-unique",
        },
    )
    value["validation_evidence_digest"] = validation_evidence_digest(value)
    return value


def seal_summary(value):
    value["semantic_summary_digest"] = semantic_summary_digest(value)
    return value


def summary(manifest, profile):
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
    value = evidence(manifest, profile)
    value.update(
        schema_version=SEMANTIC_SUMMARY_SCHEMA,
        status="COMPLETE",
        execution={
            "kind": "read_only_semantic_scan",
            "validation_batch_id": f"validate-{profile}-{manifest['stage']}",
            "validation_git_sha": "b" * 40,
            "source_output_run_id": value["output_run_id"],
        },
        source_run_ids=list(manifest["upstream_run_ids"]),
        tables={
            name: {
                "schema_sha256": f"schema-{name}",
                "row_count": 10,
                "semantic_content_fingerprint": f"fp-{name}",
                "required_null_counts": {},
                "partition_columns": [],
                "physical_layout": {
                    "file_count": 1,
                    "total_bytes": 1,
                    "file_size_min": 1,
                    "file_size_median": 1,
                    "file_size_p95": 1,
                    "file_size_max": 1,
                },
            }
            for name in inventories[manifest["stage"]]
        },
        invariants={
            name: True for name in invariant_names[manifest["stage"]]
        },
    )
    return seal_summary(value)


class SparkBenchmarkEvidenceTest(unittest.TestCase):
    def test_validation_execution_requires_declared_clean_commit(self) -> None:
        def runner(command, **_kwargs):
            if command[-2:] == ["rev-parse", "HEAD"]:
                return SimpleNamespace(stdout="b" * 40 + "\n")
            return SimpleNamespace(stdout="")

        require_clean_validation_source("b" * 40, runner)

        def dirty_runner(command, **_kwargs):
            if command[-2:] == ["rev-parse", "HEAD"]:
                return SimpleNamespace(stdout="b" * 40 + "\n")
            return SimpleNamespace(stdout=" M changed.py\n")

        with self.assertRaisesRegex(ValueError, "clean committed"):
            require_clean_validation_source("b" * 40, dirty_runner)

    def test_summary_fingerprint_is_deterministic_and_stage_bound(self) -> None:
        self.assertEqual(
            fingerprint_payload(rows=3, total="12", minimum=-2, maximum=9),
            fingerprint_payload(rows=3, total="12", minimum=-2, maximum=9),
        )
        self.assertEqual("feat", stage_run_kind("dp3-features"))

    def test_summary_scan_requires_and_preserves_pair_identity(self) -> None:
        manifest = pair()
        member = manifest["runs"][0]
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
            benchmark_id=BENCHMARK,
            spark_profile="baseline",
            stage="dp1-bronze",
            output_run_id=member["output_run_id"],
            dataset_version=DATASET,
            git_sha=GIT_SHA,
            validation_git_sha="b" * 40,
            summary_uri=(
                f"gs://bucket/evidence/spark_benchmark/{BENCHMARK}/"
                f"baseline/dp1-bronze/{member['output_run_id']}-summary.json"
            ),
            source_run_ids=(DATASET,),
            pair_manifest=manifest,
            pair_manifest_path=Path("pair.json"),
            jar_paths=(Path("delta.jar"),),
        )
        self.assertIn("--files=pair.json", command)
        self.assertIn("--pair-manifest=pair.json", command)
        self.assertIn("--validation-git-sha=" + "b" * 40, command)
        self.assertIn("--validation-batch-id=validate-ab-baseline-dp1", command)
        self.assertTrue(
            any(manifest["pair_manifest_digest"] in item for item in command)
        )
        self.assertEqual(
            f"gs://bucket/lakehouse/runs/{member['output_run_id']}/bronze",
            expected_output_root(
                data_bucket="bucket",
                output_run_id=member["output_run_id"],
                stage="dp1-bronze",
            ),
        )
        with self.assertRaisesRegex(ValueError, "distinct"):
            build_submission_command(
                runtime,
                batch_id=member["output_run_id"],
                benchmark_id=BENCHMARK,
                spark_profile="baseline",
                stage="dp1-bronze",
                output_run_id=member["output_run_id"],
                dataset_version=DATASET,
                git_sha=GIT_SHA,
                validation_git_sha="b" * 40,
                summary_uri=(
                    f"gs://bucket/evidence/spark_benchmark/{BENCHMARK}/"
                    f"baseline/dp1-bronze/{member['output_run_id']}-summary.json"
                ),
                source_run_ids=(DATASET,),
                pair_manifest=manifest,
                pair_manifest_path=Path("pair.json"),
                jar_paths=(Path("delta.jar"),),
            )

    def test_collector_binds_exact_output_and_normalized_properties(self) -> None:
        manifest = pair()
        member = manifest["runs"][1]
        properties = {
            "dataproc.tier": "standard",
            **dict(
                item.split("=", 1)
                for item in resolve_stage_profile(
                    "dp1-bronze", "optimized"
                ).spark_properties
            ),
            METHOD2_PAIR_DIGEST_PROPERTY: manifest["pair_manifest_digest"],
            METHOD2_OUTPUT_RUN_PROPERTY: member["output_run_id"],
        }
        validation = validation_evidence(manifest, "optimized")
        validation.update(
            application_runtime_seconds=580,
            spill_bytes=0,
            task_count=100,
            output_file_count=64,
            output_total_bytes=1000,
        )
        validation["validation_evidence_digest"] = validation_evidence_digest(
            validation
        )
        value = parse_batch_evidence(
            {
                "name": f"projects/p/locations/r/batches/{member['output_run_id']}",
                "state": "SUCCEEDED",
                "createTime": "2026-10-25T01:00:00Z",
                "stateTime": "2026-10-25T01:10:00Z",
                "runtimeConfig": {
                    "version": "2.2.89",
                    "properties": {
                        ("dataproc:dataproc.tier" if key == "dataproc.tier" else f"spark:{key}"): value
                        for key, value in properties.items()
                    },
                },
                "labels": {
                    "benchmark_id": BENCHMARK,
                    "spark_profile": "optimized",
                    "git_sha": GIT_SHA,
                },
                "approximateUsage": None,
                "runtimeInfo": {
                    "outputUri": "gs://logs/driver",
                    "approximateUsage": {
                        "milliDcuSeconds": "7200000",
                        "shuffleStorageGbSeconds": "123.5",
                    },
                },
            },
            benchmark_id=BENCHMARK,
            spark_profile="optimized",
            stage="dp1-bronze",
            dataset_version=DATASET,
            git_sha=GIT_SHA,
            upstream_run_ids=[DATASET],
            output_run_id=member["output_run_id"],
            output_root=member["output_root"],
            pair_manifest=manifest,
            validation=validation,
        )
        self.assertEqual(600, value["wall_clock_seconds"])
        self.assertEqual(2.0, value["dcu_hours"])
        self.assertEqual(member["output_run_id"], value["output_run_id"])
        self.assertEqual(
            manifest["pair_manifest_digest"], value["pair_manifest_digest"]
        )

    def test_collector_binds_distinct_recovery_attempt_to_same_output(self) -> None:
        manifest = pair("dp2-gold")
        member = manifest["runs"][0]
        attempt = "dp2g-b-recovery-r261030192000"
        recovery_sha = "c" * 40
        properties = {
            "dataproc.tier": "standard",
            **dict(
                item.split("=", 1)
                for item in resolve_stage_profile(
                    "dp2-gold", "baseline"
                ).spark_properties
            ),
            METHOD2_PAIR_DIGEST_PROPERTY: manifest["pair_manifest_digest"],
            METHOD2_OUTPUT_RUN_PROPERTY: member["output_run_id"],
        }
        validation = validation_evidence(manifest, "baseline")
        validation["metric_provenance"]["ingestion_batch_id"] = attempt
        validation["validation_evidence_digest"] = validation_evidence_digest(validation)
        description = {
            "name": f"projects/p/locations/r/batches/{attempt}",
            "state": "SUCCEEDED", "createTime": "2026-10-30T01:00:00Z",
            "stateTime": "2026-10-30T01:10:00Z",
            "runtimeConfig": {"version": "2.2.89", "properties": properties},
            "labels": {
                "benchmark_id": BENCHMARK, "spark_profile": "baseline",
                "git_sha": GIT_SHA, "output_run_id": member["output_run_id"],
                "recovery_git_sha": recovery_sha,
            },
            "runtimeInfo": {
                "outputUri": "gs://logs/driver",
                "approximateUsage": {
                    "milliDcuSeconds": "7200000",
                    "shuffleStorageGbSeconds": "123.5",
                },
            },
        }
        value = parse_batch_evidence(
            description, benchmark_id=BENCHMARK, spark_profile="baseline",
            stage="dp2-gold", dataset_version=DATASET, git_sha=GIT_SHA,
            upstream_run_ids=manifest["upstream_run_ids"],
            output_run_id=member["output_run_id"],
            output_root=member["output_root"], pair_manifest=manifest,
            validation=validation, dataproc_batch_id=attempt,
            recovery_git_sha=recovery_sha,
        )
        self.assertEqual(attempt, value["batch_id"])
        self.assertEqual(member["output_run_id"], value["output_run_id"])
        self.assertEqual(GIT_SHA, value["git_sha"])
        self.assertEqual(recovery_sha, value["recovery_git_sha"])
        with self.assertRaisesRegex(ValueError, "recovery batch label"):
            parse_batch_evidence(
                description, benchmark_id=BENCHMARK, spark_profile="baseline",
                stage="dp2-gold", dataset_version=DATASET, git_sha=GIT_SHA,
                upstream_run_ids=manifest["upstream_run_ids"],
                output_run_id=member["output_run_id"],
                output_root=member["output_root"], pair_manifest=manifest,
                validation=validation, dataproc_batch_id=attempt,
                recovery_git_sha="d" * 40,
            )

    def test_collector_rejects_validation_for_stale_output_attempt(self) -> None:
        manifest = pair()
        member = manifest["runs"][1]
        properties = {
            "dataproc.tier": "standard",
            **dict(
                item.split("=", 1)
                for item in resolve_stage_profile(
                    "dp1-bronze", "optimized"
                ).spark_properties
            ),
            METHOD2_PAIR_DIGEST_PROPERTY: manifest["pair_manifest_digest"],
            METHOD2_OUTPUT_RUN_PROPERTY: member["output_run_id"],
        }
        stale_validation = validation_evidence(manifest, "optimized")
        stale_validation["output_run_id"] = "stale-output-attempt"
        with self.assertRaisesRegex(ValueError, "output_run_id"):
            parse_batch_evidence(
                {
                    "name": (
                        "projects/p/locations/r/batches/"
                        f"{member['output_run_id']}"
                    ),
                    "state": "SUCCEEDED",
                    "createTime": "2026-10-25T01:00:00Z",
                    "stateTime": "2026-10-25T01:10:00Z",
                    "runtimeConfig": {
                        "version": "2.2",
                        "properties": properties,
                    },
                    "labels": {
                        "benchmark_id": BENCHMARK,
                        "spark_profile": "optimized",
                        "git_sha": GIT_SHA,
                    },
                    "approximateUsage": {
                        "milliDcuSeconds": 1,
                        "shuffleStorageGbSeconds": 0,
                    },
                },
                benchmark_id=BENCHMARK,
                spark_profile="optimized",
                stage="dp1-bronze",
                dataset_version=DATASET,
                git_sha=GIT_SHA,
                upstream_run_ids=[DATASET],
                output_run_id=member["output_run_id"],
                output_root=member["output_root"],
                pair_manifest=manifest,
                validation=stale_validation,
            )

    def test_equivalence_checks_all_four_stage_contracts(self) -> None:
        for stage in RUN_KIND:
            manifest = pair(stage)
            baseline = summary(manifest, "baseline")
            optimized = summary(manifest, "optimized")
            result = compare_summaries(
                baseline,
                optimized,
                pair_manifest=manifest,
                baseline_evidence=evidence(manifest, "baseline"),
                optimized_evidence=evidence(manifest, "optimized"),
            )
            self.assertTrue(result["output_equivalent"])

    def test_equivalence_reports_semantic_difference(self) -> None:
        manifest = pair("dp2-silver")
        baseline = summary(manifest, "baseline")
        optimized = summary(manifest, "optimized")
        optimized["tables"]["transactions"]["row_count"] = 9
        optimized["invariants"]["transaction_id_unique"] = False
        seal_summary(optimized)
        result = compare_summaries(
            baseline,
            optimized,
            pair_manifest=manifest,
            baseline_evidence=evidence(manifest, "baseline"),
            optimized_evidence=evidence(manifest, "optimized"),
        )
        self.assertFalse(result["output_equivalent"])
        self.assertIn("transactions.row_count", result["differences"])

    def test_comparison_math_reports_negative_change_as_regression(self) -> None:
        manifest = pair()
        baseline = evidence(manifest, "baseline", 100)
        optimized = evidence(manifest, "optimized", 120)
        equivalence = compare_summaries(
            summary(manifest, "baseline"),
            summary(manifest, "optimized"),
            pair_manifest=manifest,
            baseline_evidence=baseline,
            optimized_evidence=optimized,
        )
        row = comparison_row(baseline, optimized, equivalence, manifest)
        self.assertEqual(25.0, percentage_change(100, 75))
        self.assertEqual("regression", row["runtime_result"])
        self.assertEqual(-20.0, row["runtime_change_pct"])

    def test_comparison_rejects_stale_manifest_identity(self) -> None:
        manifest = pair()
        baseline = evidence(manifest, "baseline")
        optimized = evidence(manifest, "optimized")
        stale = copy.deepcopy(optimized)
        stale["output_run_id"] = "stale"
        with self.assertRaisesRegex(ValueError, "output_run_id"):
            comparison_row(
                baseline,
                stale,
                {"high_confidence_equivalence": True},
                manifest,
            )

    def test_single_stage_comparison_writes_create_only_json_and_csv(self) -> None:
        manifest = pair()
        baseline = evidence(manifest, "baseline", 100)
        optimized = evidence(manifest, "optimized", 120)
        equivalence = compare_summaries(
            summary(manifest, "baseline"),
            summary(manifest, "optimized"),
            pair_manifest=manifest,
            baseline_evidence=baseline,
            optimized_evidence=optimized,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = {
                "pair": manifest,
                "baseline": baseline,
                "optimized": optimized,
                "equivalence": equivalence,
            }
            for name, value in inputs.items():
                (root / f"{name}.json").write_text(json.dumps(value))
            argv = [
                "--pair-manifest", str(root / "pair.json"),
                "--baseline-evidence", str(root / "baseline.json"),
                "--optimized-evidence", str(root / "optimized.json"),
                "--equivalence-evidence", str(root / "equivalence.json"),
                "--output-json", str(root / "comparison.json"),
                "--output-csv", str(root / "comparison.csv"),
            ]
            self.assertEqual(0, compare_main(argv))
            self.assertEqual(0, compare_main(argv))
            (root / "comparison.csv").write_text("tampered\n")
            with self.assertRaisesRegex(ValueError, "immutable comparison"):
                compare_main(argv)


if __name__ == "__main__":
    unittest.main()
