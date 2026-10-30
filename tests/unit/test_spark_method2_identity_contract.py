from __future__ import annotations

import copy
import unittest
from pathlib import Path

from data_platform.config.spark_benchmark import (
    PAIR_MANIFEST_SCHEMA,
    SEMANTIC_SUMMARY_SCHEMA,
    pair_manifest_digest,
    semantic_summary_digest,
    select_stage_winner,
    validate_pair_manifest,
    validate_pair_member,
)
from ops.gcp.prepare_spark_stage_pair import build_stage_pair_manifest
from ops.gcp.validate_spark_output_equivalence import compare_summaries
from ops.gcp.submit_bronze_to_silver import build_submission_command
from ops.gcp.submit_source_to_bronze import CloudRuntime


BENCHMARK = "ab-20261026123456"
ATTEMPT = "r261026123456"
GIT_SHA = "a" * 40
DATASET = "canonical-100gb-v1"
STAGE = "dp2-silver"
UPSTREAM = (f"{BENCHMARK}-optimized-bronze-{ATTEMPT}",)


def pair_manifest():
    baseline = f"{BENCHMARK}-baseline-silver-{ATTEMPT}"
    optimized = f"{BENCHMARK}-optimized-silver-{ATTEMPT}"
    return build_stage_pair_manifest(
        stage=STAGE,
        dataset_version=DATASET,
        git_sha=GIT_SHA,
        upstream_run_ids=UPSTREAM,
        baseline_output_run_id=baseline,
        baseline_output_root=f"gs://bucket/lakehouse/runs/{baseline}/silver",
        candidate_output_run_id=optimized,
        candidate_output_root=f"gs://bucket/lakehouse/runs/{optimized}/silver",
    )


def collected_evidence(manifest, profile):
    member = next(run for run in manifest["runs"] if run["spark_profile"] == profile)
    return {
        "status": "PASS",
        "batch_state": "SUCCEEDED",
        "pair_manifest_digest": manifest["pair_manifest_digest"],
        "benchmark_id": manifest["benchmark_id"],
        "stage": manifest["stage"],
        "dataset_version": manifest["dataset_version"],
        "git_sha": manifest["git_sha"],
        "spark_profile": profile,
        "output_run_id": member["output_run_id"],
        "output_root": member["output_root"],
        "upstream_run_ids": list(manifest["upstream_run_ids"]),
        "spark_properties": dict(member["spark_properties"]),
        "spark_properties_digest": member["spark_properties_digest"],
        "runtime_version": "2.2.89",
        "runtime_properties": {"dataproc.tier": "standard"},
        "application_runtime_seconds": 10 if profile == "baseline" else 8,
        "batch_runtime_seconds": 11,
        "dcu_hours": 1,
        "shuffle_storage_gb_seconds": 1,
        "spill_bytes": 0,
        "task_count": 1,
        "output_file_count": 1,
        "output_total_bytes": 1,
    }


def semantic_summary(manifest, profile):
    evidence = collected_evidence(manifest, profile)
    table = {
        "schema_sha256": "schema",
        "row_count": 1,
        "semantic_content_fingerprint": "fingerprint",
        "required_null_counts": {"transaction_id": 0},
        "partition_columns": [],
        "physical_layout": {
            "file_count": 1,
            "total_bytes": 10,
            "file_size_min": 10,
            "file_size_median": 10,
            "file_size_p95": 10,
            "file_size_max": 10,
        },
    }
    evidence.update(
        schema_version=SEMANTIC_SUMMARY_SCHEMA,
        status="COMPLETE",
        execution={
            "kind": "read_only_semantic_scan",
            "validation_batch_id": f"validate-{profile}-silver",
            "validation_git_sha": "b" * 40,
            "source_output_run_id": evidence["output_run_id"],
        },
        tables={name: copy.deepcopy(table) for name in (
            "users", "accounts", "merchants", "devices", "transactions",
            "balance_snapshots", "login_events", "fraud_labels",
        )},
        invariants={
            "logical_transaction_count_match": True,
            "transaction_id_unique": True,
            "fraud_labels_separate": True,
        },
    )
    evidence["semantic_summary_digest"] = semantic_summary_digest(evidence)
    return evidence


def equivalence(manifest, baseline, optimized):
    return compare_summaries(
        baseline,
        optimized,
        pair_manifest=manifest,
        baseline_evidence=collected_evidence(manifest, "baseline"),
        optimized_evidence=collected_evidence(manifest, "optimized"),
    )


class SparkMethod2IdentityContractTest(unittest.TestCase):
    def test_complete_pair_binds_equivalence_and_winner(self) -> None:
        manifest = pair_manifest()
        self.assertEqual(PAIR_MANIFEST_SCHEMA, validate_pair_manifest(manifest)["schema_version"])
        baseline = semantic_summary(manifest, "baseline")
        optimized = semantic_summary(manifest, "optimized")
        result = equivalence(manifest, baseline, optimized)
        winner = select_stage_winner(
            pair_manifest=manifest,
            baseline_evidence=collected_evidence(manifest, "baseline"),
            candidate_evidence=collected_evidence(manifest, "optimized"),
            equivalence_evidence=result,
        )
        self.assertTrue(result["high_confidence_equivalence"])
        self.assertEqual(manifest["pair_manifest_digest"], result["pair_manifest_digest"])
        self.assertEqual("optimized", winner["selected_profile"])
        self.assertEqual(
            manifest["runs"][1]["output_run_id"], winner["selected_output_run_id"]
        )

    def test_baseline_can_win_when_original_runtime_is_better(self) -> None:
        manifest = pair_manifest()
        result = equivalence(
            manifest,
            semantic_summary(manifest, "baseline"),
            semantic_summary(manifest, "optimized"),
        )
        baseline = collected_evidence(manifest, "baseline")
        optimized = collected_evidence(manifest, "optimized")
        baseline["application_runtime_seconds"] = 8
        optimized["application_runtime_seconds"] = 10
        winner = select_stage_winner(
            pair_manifest=manifest,
            baseline_evidence=baseline,
            candidate_evidence=optimized,
            equivalence_evidence=result,
        )
        self.assertEqual("baseline", winner["selected_profile"])

    def test_missing_or_tampered_pair_identity_fails(self) -> None:
        manifest = pair_manifest()
        missing = dict(manifest)
        missing.pop("pair_manifest_digest")
        with self.assertRaisesRegex(ValueError, "pair_manifest_digest"):
            validate_pair_manifest(missing)
        tampered = copy.deepcopy(manifest)
        tampered["dataset_version"] = "other-dataset"
        with self.assertRaisesRegex(ValueError, "digest"):
            validate_pair_manifest(tampered)

    def test_member_rejects_missing_or_stale_output_attempt(self) -> None:
        manifest = pair_manifest()
        with self.assertRaisesRegex(ValueError, "output_run_id"):
            validate_pair_member(
                manifest, spark_profile="baseline", benchmark_id=BENCHMARK,
                stage=STAGE, dataset_version=DATASET, git_sha=GIT_SHA,
                upstream_run_ids=UPSTREAM, output_run_id="", output_root="",
            )
        stale = f"{BENCHMARK}-baseline-silver-r261026999999"
        with self.assertRaisesRegex(ValueError, "output_run_id"):
            validate_pair_member(
                manifest, spark_profile="baseline", benchmark_id=BENCHMARK,
                stage=STAGE, dataset_version=DATASET, git_sha=GIT_SHA,
                upstream_run_ids=UPSTREAM, output_run_id=stale,
                output_root=f"gs://bucket/lakehouse/runs/{stale}/silver",
            )

    def test_member_rejects_wrong_stage_dataset_git_or_upstream(self) -> None:
        manifest = pair_manifest()
        member = manifest["runs"][0]
        common = dict(
            spark_profile="baseline", benchmark_id=BENCHMARK, stage=STAGE,
            dataset_version=DATASET, git_sha=GIT_SHA,
            upstream_run_ids=UPSTREAM, output_run_id=member["output_run_id"],
            output_root=member["output_root"],
        )
        for field, value in (
            ("stage", "dp2-gold"),
            ("dataset_version", "wrong"),
            ("git_sha", "b" * 40),
            ("upstream_run_ids", ("wrong",)),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                validate_pair_member(manifest, **{**common, field: value})

    def test_swapped_duplicate_or_wrong_profile_roles_fail(self) -> None:
        manifest = pair_manifest()
        baseline = semantic_summary(manifest, "baseline")
        optimized = semantic_summary(manifest, "optimized")
        for left, right in (
            (optimized, baseline),
            (baseline, baseline),
            (optimized, optimized),
        ):
            with self.assertRaisesRegex(ValueError, "spark_profile"):
                compare_summaries(
                    left, right, pair_manifest=manifest,
                    baseline_evidence=collected_evidence(manifest, "baseline"),
                    optimized_evidence=collected_evidence(manifest, "optimized"),
                )

    def test_missing_or_mismatched_spark_snapshot_fails(self) -> None:
        manifest = pair_manifest()
        baseline = collected_evidence(manifest, "baseline")
        baseline.pop("spark_properties")
        with self.assertRaisesRegex(ValueError, "spark_properties"):
            select_stage_winner(
                pair_manifest=manifest,
                baseline_evidence=baseline,
                candidate_evidence=collected_evidence(manifest, "optimized"),
                equivalence_evidence={},
            )
        optimized = collected_evidence(manifest, "optimized")
        optimized["spark_properties"] = dict(manifest["runs"][0]["spark_properties"])
        with self.assertRaisesRegex(ValueError, "spark_properties"):
            select_stage_winner(
                pair_manifest=manifest,
                baseline_evidence=collected_evidence(manifest, "baseline"),
                candidate_evidence=optimized,
                equivalence_evidence={},
            )

    def test_equivalence_for_other_outputs_cannot_select_winner(self) -> None:
        manifest = pair_manifest()
        result = equivalence(
            manifest,
            semantic_summary(manifest, "baseline"),
            semantic_summary(manifest, "optimized"),
        )
        stale = copy.deepcopy(result)
        stale["compared_outputs"]["optimized"]["output_run_id"] = "stale-output"
        with self.assertRaisesRegex(ValueError, "equivalence"):
            select_stage_winner(
                pair_manifest=manifest,
                baseline_evidence=collected_evidence(manifest, "baseline"),
                candidate_evidence=collected_evidence(manifest, "optimized"),
                equivalence_evidence=stale,
            )

    def test_manifest_digest_changes_with_pair_attempt(self) -> None:
        manifest = pair_manifest()
        changed = copy.deepcopy(manifest)
        changed["runs"][0]["output_run_id"] = "different"
        self.assertNotEqual(
            manifest["pair_manifest_digest"], pair_manifest_digest(changed)
        )

    def test_submitter_requires_pair_and_dry_run_command_binds_digest(self) -> None:
        manifest = pair_manifest()
        member = manifest["runs"][1]
        runtime = CloudRuntime(
            project_id="project",
            region="asia-southeast1",
            data_bucket="bucket",
            temporary_bucket="temp",
            subnet="projects/project/regions/asia-southeast1/subnetworks/dev",
            service_account_email="spark@project.iam.gserviceaccount.com",
            performance_tier="standard",
        )
        common = dict(
            runtime=runtime,
            dataset_version=DATASET,
            source_bronze_run_id=UPSTREAM[0],
            run_id=member["output_run_id"],
            ttl="8h",
            spark_profile="optimized",
            benchmark_id=BENCHMARK,
            git_sha=GIT_SHA,
            jar_paths=(Path("delta.jar"),),
        )
        with self.assertRaisesRegex(ValueError, "pair-manifest"):
            build_submission_command(**common)
        command = build_submission_command(
            **common, pair_manifest=manifest
        )
        properties = next(
            value for value in command if value.startswith("--properties=")
        )
        self.assertIn(manifest["pair_manifest_digest"], properties)
        self.assertIn(member["output_run_id"], properties)

    def test_cross_stage_winner_reuse_fails(self) -> None:
        manifest = pair_manifest()
        result = equivalence(
            manifest,
            semantic_summary(manifest, "baseline"),
            semantic_summary(manifest, "optimized"),
        )
        other = copy.deepcopy(manifest)
        other["stage"] = "dp2-gold"
        other["pair_manifest_digest"] = pair_manifest_digest(other)
        with self.assertRaises(ValueError):
            select_stage_winner(
                pair_manifest=other,
                baseline_evidence=collected_evidence(manifest, "baseline"),
                candidate_evidence=collected_evidence(manifest, "optimized"),
                equivalence_evidence=result,
            )


if __name__ == "__main__":
    unittest.main()
