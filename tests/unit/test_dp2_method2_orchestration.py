"""Offline DP2 Method2 gates; tests must never call GCP."""
from __future__ import annotations

import json
import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data_platform.config.spark_benchmark import winner_artifact_digest
from ops.gcp.dp2_method2 import (
    _action, _adopt_completed_batch, _batch_cohort, _dataproc_batch_id,
    _pair_path, _require_head, _require_pair, _validation_batch_id, _winner,
)
from ops.gcp.summarize_spark_event_log import DATAPROC_COHORT_PROPERTY
from ops.gcp.prepare_spark_stage_pair import build_stage_pair_manifest

SOURCE_BENCH = "ab-20261029174838"
BENCH = "ab-20261031083000"
OLD_SHA = "62f17ba6fe306b5cf74621b787249f8b71ccd8e8"
NEW_SHA = "c" * 40
BRONZE_RUN = f"{SOURCE_BENCH}-baseline-bronze-r261029174838"
BRONZE_ROOT = f"gs://bucket/lakehouse/runs/{BRONZE_RUN}/bronze"
SILVER_RUN = f"{BENCH}-baseline-silver-r261030104547"
SILVER_OPT = f"{BENCH}-optimized-silver-r261030104547"
GOLD_RUN = f"{BENCH}-baseline-gold-r261030104548"
GOLD_OPT = f"{BENCH}-optimized-gold-r261030104548"


def _json_file(path):
    return json.loads(path.read_text())


def winner(stage, run_id, source_sha, output_root):
    profile = "optimized" if "-optimized-" in run_id else "baseline"
    result = {
        "schema_version": "spark-method2-winner-v1",
        "stage": stage,
        "benchmark_id": SOURCE_BENCH if stage == "dp1-bronze" else BENCH,
        "dataset_version": "canonical-100gb-v1",
        "git_sha": source_sha,
        "selected_profile": profile,
        "selected_output_run_id": run_id,
        "selected_output_root": output_root,
        "compared_outputs": {profile: {
            "output_run_id": run_id, "output_root": output_root,
        }},
    }
    result["winner_artifact_digest"] = winner_artifact_digest(result)
    return result


class FakeArgs:
    pass


class TestDP2Workflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = FakeArgs()
        self.args.root = self.root
        self.args.benchmark_id = BENCH
        self.args.dataset_version = "canonical-100gb-v1"
        self.args.git_sha = NEW_SHA
        self.args.data_bucket = "bucket"
        self.args.baseline_silver_run_id = SILVER_RUN
        self.args.optimized_silver_run_id = SILVER_OPT
        self.args.baseline_gold_run_id = GOLD_RUN
        self.args.optimized_gold_run_id = GOLD_OPT
        self.args.bronze_winner = self.root / "bronze.json"
        self.args.bronze_winner.write_text(json.dumps(winner("dp1-bronze", BRONZE_RUN, OLD_SHA, BRONZE_ROOT)))
        self.args.bronze_source_benchmark_id = SOURCE_BENCH
        self.args.bronze_winner_sha256 = hashlib.sha256(
            self.args.bronze_winner.read_bytes()
        ).hexdigest()
        self.args.action = "prepare-silver"
        self.args.profile = None
        self.args.stage = None

    def test_silver_pair_uses_immutable_bronze_winner(self):
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            _action(self.args)
        pair = json.loads(_pair_path(self.root, "silver").read_text())
        self.assertEqual(pair["upstream_run_ids"], [BRONZE_RUN])
        self.assertEqual([m["spark_profile"] for m in pair["runs"]], ["baseline", "optimized"])
        self.assertEqual(pair["git_sha"], NEW_SHA)

    def test_new_dp2_benchmark_reuses_exact_trusted_prior_bronze_winner(self):
        self.assertNotEqual(self.args.benchmark_id, self.args.bronze_source_benchmark_id)
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            _action(self.args)
        pair = json.loads(_pair_path(self.root, "silver").read_text())
        self.assertEqual(BENCH, pair["benchmark_id"])
        self.assertEqual([BRONZE_RUN], pair["upstream_run_ids"])

    def test_rejects_wrong_trusted_bronze_winner_checksum(self):
        self.args.bronze_winner_sha256 = "0" * 64
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                _action(self.args)

    def test_missing_bronze_winner_fails_before_pair_creation(self):
        self.args.bronze_winner.unlink()
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            with self.assertRaises(FileNotFoundError):
                _action(self.args)
        self.assertFalse(_pair_path(self.root, "silver").exists())

    def test_rejects_wrong_bronze_source_benchmark(self):
        self.args.bronze_source_benchmark_id = BENCH
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            with self.assertRaisesRegex(ValueError, "Wrong winner benchmark_id"):
                _action(self.args)

    def test_silver_winner_is_required_before_gold_pair(self):
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            with self.assertRaises(FileNotFoundError):
                self.args.action = "prepare-gold"
                _action(self.args)

    def test_gold_pair_binds_same_silver_winner(self):
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            self.args.action = "prepare-silver"
            _action(self.args)
            silver_root = f"gs://bucket/lakehouse/runs/{SILVER_RUN}/silver"
            (self.root / "dp2-silver-winner.json").write_text(
                json.dumps(winner("dp2-silver", SILVER_RUN, NEW_SHA, silver_root)))
            self.args.action = "prepare-gold"
            _action(self.args)
        pair = json.loads(_pair_path(self.root, "gold").read_text())
        self.assertEqual(pair["upstream_run_ids"], [SILVER_RUN])
        self.args.stage, self.args.profile = "gold", "optimized"
        self.assertEqual(_require_pair(self.args, "gold")[3], [SILVER_RUN])

    def test_rejects_tampered_winner(self):
        obj = json.loads(self.args.bronze_winner.read_text())
        obj["selected_output_root"] = "gs://bucket/other"
        self.args.bronze_winner.write_text(json.dumps(obj))
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            _winner(self.args.bronze_winner, stage="dp1-bronze",
                    benchmark=SOURCE_BENCH, bucket="bucket")

    def test_rejects_gold_pair_mismatching_winner(self):
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            self.args.action = "prepare-silver"
            _action(self.args)
            silver_root = f"gs://bucket/lakehouse/runs/{SILVER_RUN}/silver"
            (self.root / "dp2-silver-winner.json").write_text(
                json.dumps(winner("dp2-silver", SILVER_RUN, NEW_SHA, silver_root)))
        bad_gold = build_stage_pair_manifest(
            stage="dp2-gold", dataset_version="canonical-100gb-v1", git_sha=NEW_SHA,
            upstream_run_ids=[SILVER_OPT], baseline_output_run_id=GOLD_RUN,
            baseline_output_root=f"gs://bucket/lakehouse/runs/{GOLD_RUN}/gold",
            candidate_output_run_id=GOLD_OPT,
            candidate_output_root=f"gs://bucket/lakehouse/runs/{GOLD_OPT}/gold",
        )
        _pair_path(self.root, "gold").write_text(json.dumps(bad_gold))
        self.args.stage, self.args.profile = "gold", "baseline"
        with self.assertRaisesRegex(ValueError, "upstream"):
            _require_pair(self.args, "gold")

    def test_stage_only_finalize_binds_a_profile_before_building_paths(self):
        # Airflow supplies --stage silver without --profile to the finalize task.
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            _action(self.args)  # prepare Silver pair
        self.args.action = "finalize"
        self.args.stage = "silver"
        self.args.profile = None
        with patch("ops.gcp.dp2_method2._module") as command, patch(
            "ops.gcp.dp2_method2._winner",
            return_value={"selected_output_run_id": BRONZE_RUN},
        ):
            _action(self.args)
        self.assertEqual(self.args.profile, "baseline")
        modules = [call.args[0] for call in command.call_args_list]
        self.assertEqual(modules, [
            "ops.gcp.validate_spark_output_equivalence",
            "ops.gcp.compare_spark_benchmark",
            "ops.gcp.select_spark_stage_winner",
        ])

    def test_validation_jobs_have_distinct_ids(self):
        ids = [_validation_batch_id(run, stage, profile) for stage in ("silver", "gold")
               for profile, run in (("baseline", SILVER_RUN if stage == "silver" else GOLD_RUN),
                                    ("optimized", SILVER_OPT if stage == "silver" else GOLD_OPT))]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(len(x) <= 50 for x in ids))

    def test_prepare_is_idempotent_only_for_exact_immutable_manifest(self):
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            _action(self.args)
            original = _pair_path(self.root, "silver").read_bytes()
            _action(self.args)
            self.assertEqual(original, _pair_path(self.root, "silver").read_bytes())
            value = _json_file(_pair_path(self.root, "silver"))
            value["upstream_run_ids"] = ["tampered"]
            _pair_path(self.root, "silver").write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "immutable artifact differs"):
                _action(self.args)

    def _completed_batch(self, *, state="SUCCEEDED", pair_digest=None):
        pair_digest = pair_digest or "sha256:" + "a" * 64
        cohort = "cohort-exact"
        return {
            "name": f"projects/p/locations/r/batches/{SILVER_RUN}",
            "uuid": cohort,
            "state": state,
            "runtimeInfo": {"cohortInfo": {"cohort": cohort}},
            "labels": {
                "goog-dataproc-batch-uuid": cohort,
                "benchmark_id": BENCH,
                "spark_profile": "baseline",
                "git_sha": NEW_SHA,
            },
            "runtimeConfig": {"properties": {
                "spark:spark.ewallet.method2.pairManifestDigest": pair_digest,
                "spark:spark.ewallet.method2.outputRunId": SILVER_RUN,
            }},
        }

    def test_restart_adopts_only_exact_succeeded_batch(self):
        self.args.project, self.args.region = "project", "region"
        digest = "sha256:" + "a" * 64
        with patch(
            "ops.gcp.dp2_method2._describe_batch_if_present",
            return_value=self._completed_batch(pair_digest=digest),
        ):
            self.assertTrue(_adopt_completed_batch(
                self.args, batch_id=SILVER_RUN, output_run_id=SILVER_RUN,
                pair_digest=digest, profile="baseline", recovery_git_sha=None,
            ))
        with patch(
            "ops.gcp.dp2_method2._describe_batch_if_present",
            return_value=self._completed_batch(pair_digest="sha256:" + "b" * 64),
        ):
            with self.assertRaisesRegex(ValueError, "runtime identity"):
                _adopt_completed_batch(
                    self.args, batch_id=SILVER_RUN, output_run_id=SILVER_RUN,
                    pair_digest=digest, profile="baseline", recovery_git_sha=None,
                )

    def test_restart_never_resubmits_existing_non_success_batch(self):
        self.args.project, self.args.region = "project", "region"
        digest = "sha256:" + "a" * 64
        for state in ("FAILED", "CANCELLED"):
            with self.subTest(state=state), patch(
                "ops.gcp.dp2_method2._describe_batch_if_present",
                return_value=self._completed_batch(state=state, pair_digest=digest),
            ):
                with self.assertRaisesRegex(ValueError, "refusing duplicate"):
                    _adopt_completed_batch(
                        self.args, batch_id=SILVER_RUN,
                        output_run_id=SILVER_RUN, pair_digest=digest,
                        profile="baseline", recovery_git_sha=None,
                    )

    def test_restart_waits_for_exact_active_batch_then_adopts(self):
        self.args.project, self.args.region = "project", "region"
        digest = "sha256:" + "a" * 64
        states = [
            self._completed_batch(state=state, pair_digest=digest)
            for state in ("PENDING", "RUNNING", "SUCCEEDED")
        ]
        with patch(
            "ops.gcp.dp2_method2._describe_batch_if_present", side_effect=states,
        ), patch("ops.gcp.dp2_method2.time.sleep") as sleep:
            self.assertTrue(_adopt_completed_batch(
                self.args, batch_id=SILVER_RUN, output_run_id=SILVER_RUN,
                pair_digest=digest, profile="baseline", recovery_git_sha=None,
            ))
        self.assertEqual(sleep.call_count, 2)

    def test_active_batch_that_fails_is_never_adopted_or_resubmitted(self):
        self.args.project, self.args.region = "project", "region"
        digest = "sha256:" + "a" * 64
        for final in ("FAILED", "CANCELLED"):
            states = [self._completed_batch(state=s, pair_digest=digest)
                      for s in ("RUNNING", "CANCELLING", final)]
            with self.subTest(final=final), patch(
                "ops.gcp.dp2_method2._describe_batch_if_present", side_effect=states,
            ), patch("ops.gcp.dp2_method2.time.sleep"):
                with self.assertRaisesRegex(ValueError, "refusing duplicate"):
                    _adopt_completed_batch(
                        self.args, batch_id=SILVER_RUN, output_run_id=SILVER_RUN,
                        pair_digest=digest, profile="baseline", recovery_git_sha=None,
                    )

    def test_foreign_active_batch_fails_fast_without_waiting(self):
        self.args.project, self.args.region = "project", "region"
        foreign = self._completed_batch(state="RUNNING")
        foreign["labels"]["benchmark_id"] = SOURCE_BENCH
        with patch(
            "ops.gcp.dp2_method2._describe_batch_if_present", return_value=foreign,
        ), patch("ops.gcp.dp2_method2.time.sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "labels do not match"):
                _adopt_completed_batch(
                    self.args, batch_id=SILVER_RUN, output_run_id=SILVER_RUN,
                    pair_digest="sha256:" + "a" * 64, profile="baseline",
                    recovery_git_sha=None,
                )
        sleep.assert_not_called()

    def _submit_args(self):
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            _action(self.args)
        self.args.action = "submit"
        self.args.stage, self.args.profile = "silver", "baseline"
        self.args.project, self.args.region = "project", "region"
        self.args.ttl = "8h"
        self.args.compute_repository = None
        self.args.recovery_git_sha = None
        return _json_file(_pair_path(self.root, "silver"))["pair_manifest_digest"]

    def test_submit_client_failure_adopts_exact_batch_that_succeeded(self):
        digest = self._submit_args()
        succeeded = self._completed_batch(pair_digest=digest)
        failure = subprocess.CalledProcessError(1, ["gcloud"])
        with patch("ops.gcp.dp2_method2._require_clean_head"), patch(
            "ops.gcp.dp2_method2._module", side_effect=failure,
        ) as module, patch(
            "ops.gcp.dp2_method2._describe_batch_if_present",
            side_effect=[None, succeeded],
        ):
            _action(self.args)
        self.assertEqual(module.call_count, 1)
        self.assertIn("--execute", module.call_args.args)

    def test_submit_failure_without_created_batch_propagates(self):
        self._submit_args()
        failure = subprocess.CalledProcessError(1, ["gcloud"])
        with patch("ops.gcp.dp2_method2._require_clean_head"), patch(
            "ops.gcp.dp2_method2._module", side_effect=failure,
        ) as module, patch(
            "ops.gcp.dp2_method2._describe_batch_if_present", return_value=None,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                _action(self.args)
        self.assertEqual(module.call_count, 1)

    def test_approve_rejects_new_run_reusing_bronze_source_benchmark(self):
        self.args.action = "approve"
        self.args.approval = "I_APPROVE_DP2_PAID_JOBS"
        self.args.require_clean = False
        self.args.bronze_source_benchmark_id = BENCH
        with patch("ops.gcp.dp2_method2._require_head"):
            with self.assertRaisesRegex(ValueError, "must differ"):
                _action(self.args)

    def test_historical_replay_with_matching_source_benchmark_is_not_rejected(self):
        from ops.gcp.dp2_method2 import _bronze_winner
        self.args.benchmark_id = SOURCE_BENCH
        winner_data = _bronze_winner(self.args)
        self.assertEqual(winner_data["selected_output_run_id"], BRONZE_RUN)

    def test_absent_batch_allows_one_submission_path(self):
        self.args.project, self.args.region = "project", "region"
        with patch(
            "ops.gcp.dp2_method2._describe_batch_if_present", return_value=None,
        ):
            self.assertFalse(_adopt_completed_batch(
                self.args, batch_id=SILVER_RUN, output_run_id=SILVER_RUN,
                pair_digest="sha256:" + "a" * 64,
                profile="baseline", recovery_git_sha=None,
            ))

    def test_cli_rejects_unapproved_execution(self):
        from ops.gcp.dp2_method2 import main
        argv = ["--action", "approve", "--root", str(self.root),
                "--bronze-winner", str(self.args.bronze_winner),
                "--bronze-source-benchmark-id", SOURCE_BENCH,
                "--bronze-winner-sha256", self.args.bronze_winner_sha256,
                "--benchmark-id", BENCH, "--git-sha", NEW_SHA,
                "--data-bucket", "bucket", "--project", "project", "--region", "region"]
        for stage in ("silver", "gold"):
            for profile in ("baseline", "optimized"):
                argv += [f"--{profile}-{stage}-run-id", getattr(self.args, f"{profile}_{stage}_run_id")]
        with self.assertRaisesRegex(ValueError, "approval"):
            main(argv)

    def test_evidence_resumes_verified_partial_files_without_overwrite(self):
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            _action(self.args)
        self.args.action = "evidence"
        self.args.stage, self.args.profile = "silver", "baseline"
        self.args.project, self.args.region = "project", "region"
        base = self.root / "silver" / "baseline"
        base.mkdir(parents=True)
        summary = {"trusted": "summary"}
        cohort = "cohort"
        batch = {
            "name": f"projects/p/locations/r/batches/{SILVER_RUN}",
            "uuid": cohort, "state": "SUCCEEDED",
            "createTime": "2026-10-30T10:00:00Z",
            "stateTime": "2026-10-30T10:20:00Z",
            "runtimeConfig": {"version": "2.2"},
            "runtimeInfo": {"cohortInfo": {"cohort": cohort}},
            "labels": {"goog-dataproc-batch-uuid": cohort,
                       "benchmark_id": BENCH, "spark_profile": "baseline",
                       "git_sha": NEW_SHA},
        }
        (base / "summary.json").write_text(json.dumps(summary))
        (base / "original-batch.json").write_text(json.dumps(batch))
        before = {name: (base / name).read_bytes() for name in
                  ("summary.json", "original-batch.json")}
        performance = {"trusted": "performance"}
        def metrics(*args, **kwargs):
            Path(args[-1]).write_text(json.dumps(performance))
            return performance
        with patch("ops.gcp.dp2_method2._describe_batch", return_value=batch), patch(
            "ops.gcp.dp2_method2._get_event_metrics", side_effect=metrics
        ), patch(
            "ops.gcp.dp2_method2._evidence_payload",
            return_value=({"trusted": "validation"}, {"trusted": "evidence"}),
        ):
            _action(self.args)
            _action(self.args)  # completed recovery is idempotent
        self.assertEqual(before["summary.json"], (base / "summary.json").read_bytes())
        self.assertEqual(before["original-batch.json"],
                         (base / "original-batch.json").read_bytes())
        self.assertEqual(_json_file(base / "batch-evidence.json"), {"trusted": "evidence"})

    @patch("ops.gcp.dp2_method2._run")
    def test_plan_identity_requires_matching_head_but_not_clean_status(self, run):
        run.return_value = NEW_SHA
        _require_head(NEW_SHA)
        run.assert_called_once_with("git", "rev-parse", "HEAD", capture=True)

    def _prepare_gold_recovery(self):
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            self.args.action = "prepare-silver"; _action(self.args)
            silver_root = f"gs://bucket/lakehouse/runs/{SILVER_RUN}/silver"
            (self.root / "dp2-silver-winner.json").write_text(
                json.dumps(winner("dp2-silver", SILVER_RUN, NEW_SHA, silver_root)))
            self.args.action = "prepare-gold"; _action(self.args)
        self.args.baseline_gold_batch_id = "dp2g-b-recovery-r261030192000"
        self.args.optimized_gold_batch_id = "dp2g-o-recovery-r261030192000"
        self.args.recovery_git_sha = "d" * 40
        self.args.compute_repository = Path("/canonical")
        self.args.project, self.args.region, self.args.ttl = "p", "r", "8h"

    def test_gold_recovery_attempts_preserve_pair_output_identity(self):
        self._prepare_gold_recovery()
        digest = _json_file(_pair_path(self.root, "gold"))["pair_manifest_digest"]
        self.assertEqual(
            self.args.baseline_gold_batch_id,
            _dataproc_batch_id(self.args, "gold", "baseline", GOLD_RUN),
        )
        self.args.action, self.args.stage, self.args.profile = (
            "plan-submit", "gold", "baseline"
        )
        with patch("ops.gcp.dp2_method2._require_clean_head"), patch(
            "ops.gcp.dp2_method2._module"
        ) as module:
            _action(self.args)
        command = module.call_args.args
        self.assertIn("--dataproc-batch-id", command)
        self.assertIn(self.args.baseline_gold_batch_id, command)
        self.assertIn("--run-id", command)
        self.assertIn(GOLD_RUN, command)
        self.assertNotIn("--execute", command)
        self.assertEqual(
            digest, _json_file(_pair_path(self.root, "gold"))["pair_manifest_digest"]
        )

    def test_optimized_gold_quota_waits_for_baseline_recovery_attempt(self):
        self._prepare_gold_recovery()
        self.args.action, self.args.stage, self.args.profile = "quota", "gold", "optimized"
        with patch("ops.gcp.dp2_method2._module") as module:
            _action(self.args)
        command = module.call_args.args
        index = command.index("--predecessor-batch-id")
        self.assertEqual(self.args.baseline_gold_batch_id, command[index + 1])
        evidence_index = command.index("--evidence-output")
        self.assertIn(self.args.optimized_gold_batch_id, command[evidence_index + 1])

    def test_both_gold_evidence_paths_describe_their_distinct_attempts(self):
        self._prepare_gold_recovery()
        for profile, attempt in (
            ("baseline", self.args.baseline_gold_batch_id),
            ("optimized", self.args.optimized_gold_batch_id),
        ):
            with self.subTest(profile=profile):
                base = self.root / "gold" / profile
                base.mkdir(parents=True, exist_ok=True)
                for name, value in (
                    ("summary.json", {"summary": profile}),
                    ("original-batch.json", {"batch": profile}),
                    ("original-performance.json", {"performance": profile}),
                ):
                    (base / name).write_text(json.dumps(value))
                self.args.action, self.args.stage, self.args.profile = (
                    "evidence", "gold", profile
                )
                with patch(
                    "ops.gcp.dp2_method2._describe_batch",
                    return_value={"batch": profile},
                ) as describe, patch(
                    "ops.gcp.dp2_method2._require_saved_batch"
                ), patch(
                    "ops.gcp.dp2_method2._evidence_payload",
                    return_value=(
                        {"validation": profile}, {"evidence": profile}
                    ),
                ):
                    _action(self.args)
                describe.assert_called_once_with(self.args, attempt)
                self.assertEqual(
                    {"evidence": profile}, _json_file(base / "batch-evidence.json")
                )


if __name__ == "__main__":
    unittest.main()


class TestDAGStructure(unittest.TestCase):
    def test_one_dag_has_a_silver_winner_gate_before_any_gold_work(self):
        """Import with tiny Airflow stubs (production Airflow is not in this archive)."""
        import runpy
        import sys
        import types
        from unittest.mock import patch

        class DAG:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None

        class Param:
            def __init__(self, *args, **kwargs):
                self.default = args[0]

        edges = []
        class BashOperator:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.task_id = kwargs['task_id']
            def __rshift__(self, other):
                edges.append((self.task_id, other.task_id))
                return other

        airflow = types.ModuleType('airflow')
        sdk = types.ModuleType('airflow.sdk')
        sdk.DAG, sdk.Param = DAG, Param
        providers = types.ModuleType('airflow.providers')
        standard = types.ModuleType('airflow.providers.standard')
        ops = types.ModuleType('airflow.providers.standard.operators')
        bash = types.ModuleType('airflow.providers.standard.operators.bash')
        bash.BashOperator = BashOperator
        pendulum = types.ModuleType('pendulum')
        pendulum.datetime = lambda *args, **kwargs: None
        runtime_name = 'data_platform.orchestration.airflow.python_runtime'
        runtime = types.ModuleType(runtime_name)
        runtime.python_task_environment = lambda root: {'EWALLET_AIRFLOW_PYTHON': '/usr/bin/python3'}
        runtime.python_module_command = lambda module: f'python -m {module}'

        modules = {'airflow': airflow, 'airflow.sdk': sdk,
                   'airflow.providers': providers,
                   'airflow.providers.standard': standard,
                   'airflow.providers.standard.operators': ops,
                   'airflow.providers.standard.operators.bash': bash,
                   'pendulum': pendulum, runtime_name: runtime}
        dag_source = (Path(__file__).resolve().parents[2] /
                      'data_platform/orchestration/airflow/dags/dp2_bronze_to_curated.py')
        with patch.dict(sys.modules, modules):
            namespace = runpy.run_path(str(dag_source))
        self.assertEqual(namespace['dag'].kwargs['dag_id'], 'dp2_bronze_to_curated')
        params = namespace['dag'].kwargs['params']
        self.assertIn('bronze_source_benchmark_id', params)
        self.assertIn('bronze_winner_sha256', params)
        for operator in namespace.values():
            if isinstance(operator, BashOperator):
                self.assertIn(
                    "--bronze-source-benchmark-id '{{ params.bronze_source_benchmark_id }}'",
                    operator.kwargs['bash_command'],
                )
                self.assertIn(
                    "--bronze-winner-sha256 '{{ params.bronze_winner_sha256 }}'",
                    operator.kwargs['bash_command'],
                )
        self.assertEqual(len(edges), 24)
        ordered = [edges[0][0], *(end for _, end in edges)]
        self.assertEqual(len(ordered), len(set(ordered)))
        self.assertLess(ordered.index('silver_baseline_submit'),
                        ordered.index('silver_optimized_submit'))
        self.assertLess(ordered.index('silver_optimized_evidence'),
                        ordered.index('validate_and_select_silver_winner'))
        self.assertLess(ordered.index('validate_and_select_silver_winner'),
                        ordered.index('prepare_gold_pair_from_silver_winner'))
        self.assertLess(ordered.index('prepare_gold_pair_from_silver_winner'),
                        ordered.index('gold_baseline_submit'))
        self.assertEqual(ordered[-1], 'validate_and_select_gold_winner')


class TestDP2Launcher(unittest.TestCase):
    def test_dry_run_keeps_new_execution_and_trusted_bronze_identities_distinct(self):
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bronze = root / "dp1-bronze-winner.json"
            bronze.write_text(json.dumps(
                winner("dp1-bronze", BRONZE_RUN, OLD_SHA, BRONZE_ROOT)
            ))
            bronze_sha = hashlib.sha256(bronze.read_bytes()).hexdigest()
            fake_airflow = root / "airflow"
            fake_airflow.write_text(
                "#!/usr/bin/env bash\n[[ ${1:-} == version ]] && printf '3.3.1\\n'\n"
            )
            fake_airflow.chmod(fake_airflow.stat().st_mode | stat.S_IXUSR)
            stamp = "261031083001"
            command = [
                str(repository / "scripts/run_dp2_method2.sh"), str(bronze),
                "--benchmark-id", BENCH,
                "--bronze-source-benchmark-id", SOURCE_BENCH,
                "--bronze-winner-sha256", bronze_sha,
                "--artifact-root", str(root / "evidence"),
                "--baseline-silver-run-id", f"{BENCH}-baseline-silver-r{stamp}",
                "--optimized-silver-run-id", f"{BENCH}-optimized-silver-r{stamp}",
                "--baseline-gold-run-id", f"{BENCH}-baseline-gold-r{stamp}",
                "--optimized-gold-run-id", f"{BENCH}-optimized-gold-r{stamp}",
            ]
            environment = dict(os.environ)
            environment.update(
                AIRFLOW_BIN=str(fake_airflow),
                EWALLET_DATA_BUCKET="bucket",
            )
            result = subprocess.run(
                command, cwd=repository, env=environment, capture_output=True,
                text=True, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(f'benchmark_id\\":\\"{BENCH}', result.stdout)
            self.assertIn(
                f'bronze_source_benchmark_id\\":\\"{SOURCE_BENCH}', result.stdout
            )
            self.assertIn(f'bronze_winner_sha256\\":\\"{bronze_sha}', result.stdout)
            self.assertIn(f"dp2-method2-{BENCH}", result.stdout)

    def test_launcher_rejects_same_new_and_bronze_source_benchmark(self):
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bronze = root / "bronze.json"
            bronze.write_text("{}")
            fake_airflow = root / "airflow"
            fake_airflow.write_text(
                "#!/usr/bin/env bash\n[[ ${1:-} == version ]] && printf '3.3.1\\n'\n"
            )
            fake_airflow.chmod(fake_airflow.stat().st_mode | stat.S_IXUSR)
            result = subprocess.run(
                [
                    str(repository / "scripts/run_dp2_method2.sh"), str(bronze),
                    "--benchmark-id", BENCH,
                    "--bronze-source-benchmark-id", BENCH,
                    "--bronze-winner-sha256", "a" * 64,
                    "--artifact-root", str(root / "evidence"),
                    "--baseline-silver-run-id", SILVER_RUN,
                    "--optimized-silver-run-id", SILVER_OPT,
                    "--baseline-gold-run-id", GOLD_RUN,
                    "--optimized-gold-run-id", GOLD_OPT,
                ],
                cwd=repository,
                env={**os.environ, "AIRFLOW_BIN": str(fake_airflow),
                     "EWALLET_DATA_BUCKET": "bucket"},
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("must differ", result.stderr)


class TestEventLogProvenance(unittest.TestCase):
    def test_multiple_identity_matching_event_logs_fail_without_evidence(self):
        from ops.gcp.dp2_method2 import _get_event_metrics
        args = FakeArgs()
        args.data_bucket = "bucket"
        args.benchmark_id = BENCH
        cohort = "cohort-original"
        batch = {
            "state": "SUCCEEDED",
            "createTime": "2026-10-30T10:00:00Z",
            "stateTime": "2026-10-30T10:20:00Z",
            "name": f"projects/p/locations/r/batches/{SILVER_RUN}",
            "uuid": cohort,
            "labels": {"goog-dataproc-batch-uuid": cohort},
            "runtimeInfo": {"cohortInfo": {"cohort": cohort}},
        }
        uris = (
            "gs://bucket/spark-events/app-20261030100500123-0001.jsonl",
            "gs://bucket/spark-events/app-20261030101000123-0002.jsonl",
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "performance.json"

            def fake_run(*cmd, capture=False):
                if cmd[:3] == ("gcloud", "storage", "ls"):
                    return "\n".join(uris)
                if cmd[:3] == ("gcloud", "storage", "cp"):
                    Path(cmd[-1]).write_text("{}\n")
                    return ""
                raise AssertionError(cmd)

            with patch(
                "ops.gcp.dp2_method2._run", side_effect=fake_run
            ), patch(
                "ops.gcp.dp2_method2.summarize_event_logs",
                return_value={"identity": "valid"},
            ):
                with self.assertRaisesRegex(ValueError, "found 2"):
                    _get_event_metrics(
                        args, "silver", "baseline", SILVER_RUN, SILVER_RUN,
                        "sha256:" + "a" * 64, batch, output,
                    )
            self.assertFalse(output.exists())

    def test_requires_original_run_provenance_not_validation_job(self):
        from ops.gcp.dp2_method2 import _get_event_metrics
        from data_platform.config.spark_benchmark import (
            METHOD2_OUTPUT_RUN_PROPERTY, METHOD2_PAIR_DIGEST_PROPERTY,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = FakeArgs()
            args.data_bucket = 'bucket'
            args.benchmark_id = BENCH
            cohort = 'cohort-original'
            batch = {'state': 'SUCCEEDED', 'createTime': '2026-10-30T10:00:00Z',
                     'stateTime': '2026-10-30T10:20:00Z',
                     'name': f'projects/p/locations/r/batches/{SILVER_RUN}',
                     'uuid': cohort,
                     'labels': {'goog-dataproc-batch-uuid': cohort},
                     'runtimeInfo': {'cohortInfo': {'cohort': cohort}}}
            digest = 'sha256:' + 'a' * 64
            uri = 'gs://bucket/spark-events/app-20261030101000123-0001.jsonl'
            start = 1_793_354_700_000
            def make_event(run_id, event_cohort=cohort, app_name='EWallet-DP2-Silver',
                           started=start, ended=start + 11_000):
                return [
                    {'Event': 'SparkListenerApplicationStart', 'App Name': app_name,
                     'App ID': 'app-20261030101000123-0001', 'Timestamp': started},
                    {'Event': 'SparkListenerEnvironmentUpdate', 'Spark Properties': {
                        'spark.app.id': 'app-20261030101000123-0001',
                        METHOD2_OUTPUT_RUN_PROPERTY: run_id,
                        METHOD2_PAIR_DIGEST_PROPERTY: digest,
                        DATAPROC_COHORT_PROPERTY: event_cohort,
                    }},
                    {'Event': 'SparkListenerTaskEnd', 'Task End Reason': {'Reason':'Success'},
                     'Task Metrics': {'Memory Bytes Spilled':0,'Disk Bytes Spilled':0}},
                    {'Event': 'SparkListenerJobEnd'}, {'Event': 'SparkListenerStageCompleted'},
                    {'Event': 'SparkListenerApplicationEnd', 'Timestamp': ended},
                ]
            def fake_run(*cmd, capture=False):
                if cmd[:3] == ('gcloud', 'storage', 'ls'):
                    return uri
                if cmd[:3] == ('gcloud', 'storage', 'cp'):
                    Path(cmd[-1]).write_text('\n'.join(map(json.dumps, make_event('validator-other-run'))))
                    return ''
                raise AssertionError(cmd)
            output = root / 'performance.json'
            with patch('ops.gcp.dp2_method2._run', side_effect=fake_run):
                with self.assertRaisesRegex(ValueError, 'found 0'):
                    _get_event_metrics(args, 'silver', 'baseline', SILVER_RUN,
                                       SILVER_RUN,
                                       digest, batch, output)
            self.assertFalse(output.exists())

    def test_validator_with_same_method2_properties_is_not_original(self):
        from ops.gcp.dp2_method2 import _get_event_metrics
        from data_platform.config.spark_benchmark import (
            METHOD2_OUTPUT_RUN_PROPERTY, METHOD2_PAIR_DIGEST_PROPERTY,
        )
        with tempfile.TemporaryDirectory() as temp:
            args = FakeArgs(); args.data_bucket = 'bucket'; args.benchmark_id = BENCH
            cohort = 'cohort-original'; digest = 'sha256:' + 'a' * 64
            batch = {
                'state': 'SUCCEEDED', 'createTime': '2026-10-30T10:00:00Z',
                'stateTime': '2026-10-30T10:20:00Z',
                'name': f'projects/p/locations/r/batches/{SILVER_RUN}', 'uuid': cohort,
                'labels': {'goog-dataproc-batch-uuid': cohort},
                'runtimeInfo': {'cohortInfo': {'cohort': cohort}},
            }
            uris = [
                'gs://bucket/spark-events/app-20261030100500123-0001.jsonl',
                'gs://bucket/spark-events/app-20261030102500123-0002.jsonl',
            ]
            def events(app_id, *, name, event_cohort, start, end):
                return [
                    {'Event':'SparkListenerApplicationStart','App Name':name,
                     'App ID':app_id,'Timestamp':start},
                    {'Event':'SparkListenerEnvironmentUpdate','Spark Properties':{
                        'spark.app.id':app_id, METHOD2_OUTPUT_RUN_PROPERTY:SILVER_RUN,
                        METHOD2_PAIR_DIGEST_PROPERTY:digest,
                        DATAPROC_COHORT_PROPERTY:event_cohort}},
                    {'Event':'SparkListenerTaskEnd','Task End Reason':{'Reason':'Success'},
                     'Task Metrics':{}}, {'Event':'SparkListenerJobEnd'},
                    {'Event':'SparkListenerStageCompleted'},
                    {'Event':'SparkListenerApplicationEnd','Timestamp':end},
                ]
            original = events('app-original', name='EWallet-DP2-Silver',
                              event_cohort=cohort, start=1793354700000, end=1793355300000)
            validator = events('app-validator', name='spark-ab-output-summary',
                               event_cohort='cohort-validator',
                               start=1793355900000, end=1793356200000)
            def fake_run(*cmd, capture=False):
                if cmd[:3] == ('gcloud','storage','ls'):
                    return '\n'.join(uris)
                if cmd[:3] == ('gcloud','storage','cp'):
                    source = original if cmd[3] == uris[0] else validator
                    Path(cmd[-1]).write_text('\n'.join(map(json.dumps, source)))
                    return ''
                raise AssertionError(cmd)
            output = Path(temp) / 'performance.json'
            with patch('ops.gcp.dp2_method2._run', side_effect=fake_run):
                result = _get_event_metrics(args, 'silver', 'baseline', SILVER_RUN,
                                            SILVER_RUN,
                                            digest, batch, output)
            self.assertEqual(result['application_id'], 'app-original')
            self.assertEqual(result['dataproc_cohort_id'], cohort)

    def test_undecodable_foreign_zstd_log_does_not_fail_original_evidence(self):
        from ops.gcp.dp2_method2 import _get_event_metrics
        args = FakeArgs(); args.data_bucket = 'bucket'; args.benchmark_id = BENCH
        cohort = 'cohort-original'
        batch = {
            'state': 'SUCCEEDED', 'createTime': '2026-10-30T10:00:00Z',
            'stateTime': '2026-10-30T10:20:00Z',
            'name': f'projects/p/locations/r/batches/{SILVER_RUN}', 'uuid': cohort,
            'labels': {'goog-dataproc-batch-uuid': cohort},
            'runtimeInfo': {'cohortInfo': {'cohort': cohort}},
        }
        uris = [
            'gs://bucket/spark-events/app-20261030100500123-0001.jsonl',
            'gs://bucket/spark-events/app-20261030101000123-0002.zstd',
        ]

        def fake_run(*cmd, capture=False):
            if cmd[:3] == ('gcloud', 'storage', 'ls'):
                return '\n'.join(uris)
            if cmd[:3] == ('gcloud', 'storage', 'cp'):
                Path(cmd[-1]).write_bytes(b'not-a-zstd-frame')
                return ''
            raise AssertionError(cmd)

        def fake_subprocess(command, **kwargs):
            raise subprocess.CalledProcessError(1, command)

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'performance.json'
            with patch('ops.gcp.dp2_method2._run', side_effect=fake_run), patch(
                'ops.gcp.dp2_method2.subprocess.run', side_effect=fake_subprocess,
            ), patch(
                'ops.gcp.dp2_method2.summarize_event_logs',
                return_value={'application_id': 'app-original'},
            ) as summarize:
                result = _get_event_metrics(args, 'silver', 'baseline', SILVER_RUN,
                                            SILVER_RUN, 'sha256:' + 'a' * 64,
                                            batch, output)
            self.assertEqual(result['application_id'], 'app-original')
            self.assertEqual(summarize.call_count, 1)
            self.assertEqual(_json_file(output), result)

    def test_batch_cohort_identity_must_be_exact(self):
        run = SILVER_RUN
        batch = {'name': f'projects/p/locations/r/batches/{run}', 'uuid': 'one',
                 'labels': {'goog-dataproc-batch-uuid': 'two'},
                 'runtimeInfo': {'cohortInfo': {'cohort': 'one'}}}
        with self.assertRaisesRegex(ValueError, 'cohort identities disagree'):
            _batch_cohort(batch, run)
