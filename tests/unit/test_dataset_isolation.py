"""A 5 GB rehearsal stays isolated from the canonical 100 GB dataset and its outputs."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from data_platform.config.dataset_identity import (
    APPROVED_DATASETS,
    CANONICAL_100GB,
    CANONICAL_DATA_BUCKET,
    REHEARSAL_5GB,
    refuse_misplaced_dataset,
    require_approved_dataset,
    require_dataset_bucket,
)
from data_platform.config.spark_benchmark import validate_pair_manifest
from data_platform.generation.src.sharded.planner import build_shard_plan, derive_shard_seed
from ops.gcp import dp2_method2
from ops.gcp import submit_bronze_to_silver, submit_historical_features, submit_silver_to_gold, submit_source_to_bronze
from ops.gcp.calibrate_source_scale import load_profile
from ops.gcp.prepare_spark_stage_pair import build_stage_pair_manifest
from ops.gcp.submit_source_to_bronze import CloudRuntime
from tests.unit.test_dp2_method2_orchestration import BRONZE_ROOT, BRONZE_RUN, OLD_SHA, SOURCE_BENCH, winner


ROOT = Path(__file__).resolve().parents[2]
REHEARSAL_BUCKET = "ewallet-rehearsal-5gb-data"
BENCH = "ab-20261109120000"
ATTEMPT = "r261109120000"


def runtime(bucket: str) -> CloudRuntime:
    return CloudRuntime("project", "asia-southeast1", bucket, "temp-bucket", "subnet", "sa@example.com", "standard")


class DatasetIdentityTest(unittest.TestCase):
    def test_both_identities_are_explicit(self):
        self.assertEqual({CANONICAL_100GB, REHEARSAL_5GB}, set(APPROVED_DATASETS))
        self.assertEqual("canonical", require_approved_dataset(CANONICAL_100GB).role)
        self.assertEqual("rehearsal", require_approved_dataset(REHEARSAL_5GB).role)
        with self.assertRaises(ValueError):
            require_approved_dataset("canonical-100gb-v2")

    def test_rehearsal_is_refused_in_the_canonical_bucket(self):
        with self.assertRaisesRegex(ValueError, "must not use bucket"):
            require_dataset_bucket(REHEARSAL_5GB, CANONICAL_DATA_BUCKET)
        require_dataset_bucket(REHEARSAL_5GB, REHEARSAL_BUCKET)
        require_dataset_bucket(CANONICAL_100GB, CANONICAL_DATA_BUCKET)

    def test_non_campaign_datasets_keep_their_existing_freedom(self):
        refuse_misplaced_dataset("cloud-smoke-v1", CANONICAL_DATA_BUCKET)
        refuse_misplaced_dataset("calibration-cloud-pilot-1gb", CANONICAL_DATA_BUCKET)
        with self.assertRaises(ValueError):
            refuse_misplaced_dataset(REHEARSAL_5GB, CANONICAL_DATA_BUCKET)

    def test_the_canonical_100gb_gate_is_unchanged(self):
        # source_to_bronze imports pyspark, so the gate is read from its source text.
        source = (ROOT / "data_platform/ingestion/spark/source_to_bronze.py").read_text(encoding="utf-8")
        self.assertIn("CANONICAL_MINIMUM_BYTES = 100_000_000_000\n", source)
        self.assertIn('expected_dataset_version == "canonical-100gb-v1"\n        and total_bytes < CANONICAL_MINIMUM_BYTES',
                      source)


class StageSubmitterTest(unittest.TestCase):
    CASES = (
        (submit_source_to_bronze, {}),
        (submit_bronze_to_silver, {"source_bronze_run_id": f"{BENCH}-optimized-bronze-{ATTEMPT}"}),
        (submit_silver_to_gold, {"source_silver_run_id": f"{BENCH}-optimized-silver-{ATTEMPT}"}),
        (submit_historical_features, {"source_silver_run_id": f"{BENCH}-optimized-silver-{ATTEMPT}",
                                      "source_core_gold_run_id": f"{BENCH}-optimized-gold-{ATTEMPT}"}),
    )

    def test_every_stage_refuses_a_rehearsal_in_the_canonical_bucket(self):
        for module, extra in self.CASES:
            with self.subTest(module.__name__):
                with self.assertRaisesRegex(ValueError, "must not use bucket"):
                    module.build_submission_command(runtime(CANONICAL_DATA_BUCKET), dataset_version=REHEARSAL_5GB,
                                                    run_id=f"{BENCH}-baseline-x-{ATTEMPT}", **extra)

    def test_existing_output_prefixes_are_refused_at_every_stage(self):
        guards = (
            submit_source_to_bronze.assert_bronze_prefix_absent,
            submit_bronze_to_silver.assert_silver_prefix_absent,
            submit_silver_to_gold.assert_output_prefix_absent,
            submit_historical_features.assert_output_prefix_absent,
        )
        existing = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="gs://b/x/part-0.parquet\n", stderr="")
        absent = lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="",
                                                          stderr="ERROR: One or more URLs matched no objects.")
        for guard in guards:
            with self.subTest(guard.__module__):
                with self.assertRaisesRegex(ValueError, "already exists"):
                    guard(runtime(REHEARSAL_BUCKET), run_id=f"{BENCH}-baseline-x-{ATTEMPT}", runner=existing)
                guard(runtime(REHEARSAL_BUCKET), run_id=f"{BENCH}-baseline-x-{ATTEMPT}", runner=absent)


class Dp1PairManifestTest(unittest.TestCase):
    def manifest(self, dataset_version, upstream):
        baseline, optimized = f"{BENCH}-baseline-bronze-{ATTEMPT}", f"{BENCH}-optimized-bronze-{ATTEMPT}"
        return build_stage_pair_manifest(
            stage="dp1-bronze", dataset_version=dataset_version, git_sha="a" * 40, upstream_run_ids=upstream,
            baseline_output_run_id=baseline, baseline_output_root=f"gs://b/lakehouse/runs/{baseline}/bronze",
            candidate_output_run_id=optimized, candidate_output_root=f"gs://b/lakehouse/runs/{optimized}/bronze",
        )

    def test_both_approved_datasets_bind_themselves_as_upstream(self):
        for version in (CANONICAL_100GB, REHEARSAL_5GB):
            with self.subTest(version):
                validate_pair_manifest(self.manifest(version, (version,)))

    def test_unknown_or_mismatched_datasets_are_refused(self):
        with self.assertRaises(ValueError):
            validate_pair_manifest(self.manifest("canonical-100gb-v2", ("canonical-100gb-v2",)))
        with self.assertRaises(ValueError):
            validate_pair_manifest(self.manifest(REHEARSAL_5GB, (CANONICAL_100GB,)))


class Dp2DatasetTest(unittest.TestCase):
    def argv(self, root: Path, dataset_version: str, bucket: str) -> list[str]:
        argv = ["--action", "approve", "--root", str(root), "--bronze-winner", str(root / "w.json"),
                "--bronze-source-benchmark-id", SOURCE_BENCH, "--bronze-winner-sha256", "0" * 64,
                "--benchmark-id", BENCH, "--dataset-version", dataset_version, "--git-sha", "c" * 40,
                "--data-bucket", bucket, "--project", "p", "--region", "asia-southeast1"]
        for stage in dp2_method2.STAGES:
            for profile in dp2_method2.PROFILES:
                argv += [f"--{profile}-{stage}-run-id", f"{BENCH}-{profile}-{stage}-{ATTEMPT}"]
        return argv

    def test_dp2_accepts_approved_datasets_in_their_buckets_only(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(dp2_method2, "_action") as action:
            root = Path(temporary)
            dp2_method2.main(self.argv(root, CANONICAL_100GB, CANONICAL_DATA_BUCKET))
            dp2_method2.main(self.argv(root, REHEARSAL_5GB, REHEARSAL_BUCKET))
            self.assertEqual(2, action.call_count)
            for version, bucket in ((REHEARSAL_5GB, CANONICAL_DATA_BUCKET), ("canonical-100gb-v2", REHEARSAL_BUCKET)):
                with self.subTest(version), self.assertRaises(ValueError):
                    dp2_method2.main(self.argv(root, version, bucket))
            self.assertEqual(2, action.call_count)

    def test_a_winner_must_carry_the_run_dataset_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bronze.json"
            path.write_text(json.dumps(winner("dp1-bronze", BRONZE_RUN, OLD_SHA, BRONZE_ROOT)))
            dp2_method2._winner(path, stage="dp1-bronze", benchmark=SOURCE_BENCH, bucket="bucket",
                                dataset_version=CANONICAL_100GB)
            with self.assertRaisesRegex(ValueError, "Wrong winner dataset_version"):
                dp2_method2._winner(path, stage="dp1-bronze", benchmark=SOURCE_BENCH, bucket="bucket",
                                    dataset_version=REHEARSAL_5GB)

    def test_every_winner_lookup_passes_the_run_dataset_version(self):
        tree = ast.parse((ROOT / "ops/gcp/dp2_method2.py").read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_winner"]
        self.assertEqual(4, len(calls))
        for call in calls:
            keywords = {item.arg: ast.unparse(item.value) for item in call.keywords}
            self.assertEqual("args.dataset_version", keywords.get("dataset_version"))

    def test_the_pipeline_dags_default_to_the_rehearsal_identity_and_never_execute(self):
        source = (ROOT / "data_platform/orchestration/airflow/edai1_pipeline.py").read_text(encoding="utf-8")
        self.assertIn('"dataset_version": Param(REHEARSAL_5GB,', source)
        self.assertIn('"data_bucket": Param(REHEARSAL_DATA_BUCKET,', source)
        self.assertIn('"execution_approval": Param(NOT_APPROVED,', source)

    def test_historical_recovery_stays_bound_to_the_canonical_run(self):
        source = (ROOT / "ops/gcp/recover_dp2_method2.py").read_text(encoding="utf-8")
        self.assertIn('if config["dataset_version"] != "canonical-100gb-v1":', source)


class GenerationProfileTest(unittest.TestCase):
    def test_the_5gb_profile_shares_shard_shape_with_100gb(self):
        rehearsal = load_profile(ROOT / "configs/generation/cloud-rehearsal-5gb.yaml")
        canonical = load_profile(ROOT / "configs/generation/cloud-canonical-100gb.yaml")
        self.assertEqual(5_000_000_000, rehearsal.target_bytes)
        self.assertEqual(APPROVED_DATASETS[REHEARSAL_5GB].target_source_bytes, rehearsal.target_bytes)
        for name in ("users_per_shard", "merchants_per_shard", "transactions_per_shard"):
            self.assertEqual(getattr(canonical, name), getattr(rehearsal, name))

    def test_a_different_dataset_version_yields_different_shard_seeds(self):
        config = yaml.safe_load((ROOT / "data_platform/generation/config/settings.yaml").read_text(encoding="utf-8"))
        plans = [build_shard_plan(config, version, 7001, 4, 12500, 300, 100000)
                 for version in (CANONICAL_100GB, REHEARSAL_5GB)]
        seeds = [{derive_shard_seed(plan, shard) for shard in range(4)} for plan in plans]
        self.assertFalse(seeds[0] & seeds[1])


if __name__ == "__main__":
    unittest.main()
