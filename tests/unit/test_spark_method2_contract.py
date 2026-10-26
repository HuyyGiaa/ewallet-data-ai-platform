from __future__ import annotations

import unittest

from data_platform.config.spark_benchmark import (
    EQUIVALENCE_SCHEMA,
    REQUIRED_RUNTIME_METRICS,
    StageRunPlan,
    equivalence_artifact_digest,
    output_identity,
    require_common_stage_inputs,
    select_stage_winner,
)
from ops.gcp.prepare_spark_stage_pair import build_stage_pair_manifest


BENCHMARK = "ab-20261026123456"
ATTEMPT = "r261026123456"


def plan(profile, upstream, output):
    return StageRunPlan(
        stage="dp2-silver",
        profile=profile,
        upstream_run_ids=(upstream,),
        output_run_id=output,
        output_root=f"gs://bucket/lakehouse/runs/{output}/silver",
    )


def evidence(manifest, profile, runtime=10.0):
    member = next(
        run for run in manifest["runs"] if run["spark_profile"] == profile
    )
    value = {name: 1 for name in REQUIRED_RUNTIME_METRICS}
    value.update(
        status="PASS",
        batch_state="SUCCEEDED",
        application_runtime_seconds=runtime,
        dcu_hours=1.0,
        benchmark_id=BENCHMARK,
        dataset_version="canonical-100gb-v1",
        git_sha="a" * 40,
        stage="dp2-silver",
        upstream_run_ids=["selected-bronze"],
        pair_manifest_digest=manifest["pair_manifest_digest"],
        spark_profile=profile,
        output_run_id=member["output_run_id"],
        output_root=member["output_root"],
        spark_properties=member["spark_properties"],
        spark_properties_digest=member["spark_properties_digest"],
        runtime_version="2.2",
        runtime_properties={"dataproc.tier": "standard"},
    )
    return value


def equivalence(manifest, *, equivalent=True):
    value = {
        "schema_version": EQUIVALENCE_SCHEMA,
        "high_confidence_equivalence": True,
        "pair_manifest_digest": manifest["pair_manifest_digest"],
        "benchmark_id": BENCHMARK,
        "dataset_version": "canonical-100gb-v1",
        "git_sha": "a" * 40,
        "stage": "dp2-silver",
        "source_run_ids": ["selected-bronze"],
        "compared_outputs": {
            "baseline": output_identity(manifest, "baseline"),
            "optimized": output_identity(manifest, "optimized"),
        },
    }
    value["high_confidence_equivalence"] = equivalent
    value["equivalence_artifact_digest"] = equivalence_artifact_digest(value)
    return value


def manifest():
    baseline = f"{BENCHMARK}-baseline-silver-{ATTEMPT}"
    candidate = f"{BENCHMARK}-optimized-silver-{ATTEMPT}"
    return build_stage_pair_manifest(
        stage="dp2-silver",
        dataset_version="canonical-100gb-v1",
        git_sha="a" * 40,
        upstream_run_ids=("selected-bronze",),
        baseline_output_run_id=baseline,
        baseline_output_root=f"gs://bucket/lakehouse/runs/{baseline}/silver",
        candidate_output_run_id=candidate,
        candidate_output_root=f"gs://bucket/lakehouse/runs/{candidate}/silver",
    )


class SparkMethod2ContractTest(unittest.TestCase):
    def test_stage_pair_requires_identical_explicit_upstream(self) -> None:
        require_common_stage_inputs(
            plan("baseline", "selected-bronze", f"{BENCHMARK}-baseline-silver-{ATTEMPT}"),
            plan("optimized", "selected-bronze", f"{BENCHMARK}-optimized-silver-{ATTEMPT}"),
        )
        with self.assertRaisesRegex(ValueError, "identical upstream"):
            require_common_stage_inputs(
                plan("baseline", "bronze-a", f"{BENCHMARK}-baseline-silver-{ATTEMPT}"),
                plan("optimized", "bronze-b", f"{BENCHMARK}-optimized-silver-{ATTEMPT}"),
            )

    def test_profile_name_does_not_infer_upstream_run(self) -> None:
        baseline = plan("baseline", "winner-from-previous-stage", f"{BENCHMARK}-baseline-silver-{ATTEMPT}")
        candidate = plan("optimized", "winner-from-previous-stage", f"{BENCHMARK}-optimized-silver-{ATTEMPT}")
        self.assertEqual(baseline.upstream_run_ids, candidate.upstream_run_ids)

    def test_pair_manifest_records_stage_profiles_and_common_input(self) -> None:
        baseline = f"{BENCHMARK}-baseline-silver-{ATTEMPT}"
        candidate = f"{BENCHMARK}-optimized-silver-{ATTEMPT}"
        manifest = build_stage_pair_manifest(
            stage="dp2-silver",
            dataset_version="canonical-100gb-v1",
            git_sha="a" * 40,
            upstream_run_ids=("selected-bronze",),
            baseline_output_run_id=baseline,
            baseline_output_root=f"gs://bucket/lakehouse/runs/{baseline}/silver",
            candidate_output_run_id=candidate,
            candidate_output_root=f"gs://bucket/lakehouse/runs/{candidate}/silver",
        )
        self.assertEqual(["selected-bronze"], manifest["upstream_run_ids"])
        self.assertEqual(304, int(manifest["runs"][1]["spark_properties"]["spark.sql.shuffle.partitions"]))

    def test_winner_rejects_incomplete_or_unequal_evidence(self) -> None:
        pair = manifest()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            select_stage_winner(
                pair_manifest=pair,
                baseline_evidence={**evidence(pair, "baseline"), "status": "FAIL"},
                candidate_evidence=evidence(pair, "optimized"),
                equivalence_evidence=equivalence(pair),
            )
        with self.assertRaisesRegex(ValueError, "unequal"):
            select_stage_winner(
                pair_manifest=pair,
                baseline_evidence=evidence(pair, "baseline"),
                candidate_evidence=evidence(pair, "optimized", 9),
                equivalence_evidence=equivalence(pair, equivalent=False),
            )

    def test_winner_rejects_cross_lineage_evidence(self) -> None:
        pair = manifest()
        with self.assertRaisesRegex(ValueError, "upstream_run_ids"):
            select_stage_winner(
                pair_manifest=pair,
                baseline_evidence=evidence(pair, "baseline"),
                candidate_evidence={
                    **evidence(pair, "optimized", 9),
                    "upstream_run_ids": ["different-bronze"],
                },
                equivalence_evidence=equivalence(pair),
            )


if __name__ == "__main__":
    unittest.main()
