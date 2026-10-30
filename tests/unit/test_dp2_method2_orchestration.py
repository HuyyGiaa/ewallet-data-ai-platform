"""Offline DP2 Method2 gates; tests must never call GCP."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data_platform.config.spark_benchmark import winner_artifact_digest
from ops.gcp.dp2_method2 import (
    _action, _batch_cohort, _pair_path, _require_head, _require_pair,
    _validation_batch_id, _winner,
)
from ops.gcp.summarize_spark_event_log import DATAPROC_COHORT_PROPERTY
from ops.gcp.prepare_spark_stage_pair import build_stage_pair_manifest

BENCH = "ab-20261029174838"
OLD_SHA = "62f17ba6fe306b5cf74621b787249f8b71ccd8e8"
NEW_SHA = "c" * 40
BRONZE_RUN = f"{BENCH}-baseline-bronze-r261029174838"
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
        "benchmark_id": BENCH,
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
                    benchmark=BENCH, bucket="bucket")

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

    def test_prepare_is_immutable(self):
        with patch("ops.gcp.dp2_method2._require_clean_head"):
            _action(self.args)
            with self.assertRaises(FileExistsError):
                _action(self.args)

    def test_cli_rejects_unapproved_execution(self):
        from ops.gcp.dp2_method2 import main
        argv = ["--action", "approve", "--root", str(self.root),
                "--bronze-winner", str(self.args.bronze_winner),
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


class TestEventLogProvenance(unittest.TestCase):
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
                                            digest, batch, output)
            self.assertEqual(result['application_id'], 'app-original')
            self.assertEqual(result['dataproc_cohort_id'], cohort)

    def test_batch_cohort_identity_must_be_exact(self):
        run = SILVER_RUN
        batch = {'name': f'projects/p/locations/r/batches/{run}', 'uuid': 'one',
                 'labels': {'goog-dataproc-batch-uuid': 'two'},
                 'runtimeInfo': {'cohortInfo': {'cohort': 'one'}}}
        with self.assertRaisesRegex(ValueError, 'cohort identities disagree'):
            _batch_cohort(batch, run)
