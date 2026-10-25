from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from airflow.models import DagBag

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DAG_ROOT = REPOSITORY_ROOT / "data_platform/orchestration/airflow/dags"


class CloudAirflowDagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bag = DagBag(dag_folder=str(DAG_ROOT))

    def test_cloud_dags_parse_without_errors(self) -> None:
        self.assertEqual({}, self.bag.import_errors)

    def test_graphs_put_a_quota_guard_immediately_before_every_submit(self) -> None:
        dp1 = self.bag.dags["dp1_source_to_bronze"]
        self.assertEqual(
            {"verify_source_manifest", "wait_bronze_quota", "submit_bronze_dataproc", "verify_bronze_delta"},
            set(dp1.task_ids),
        )
        self.assertEqual({"wait_bronze_quota"}, dp1.get_task("verify_source_manifest").downstream_task_ids)
        self.assertEqual({"submit_bronze_dataproc"}, dp1.get_task("wait_bronze_quota").downstream_task_ids)

        dp2 = self.bag.dags["dp2_bronze_to_curated"]
        self.assertEqual(7, len(dp2.task_ids))
        self.assertEqual({"wait_silver_quota"}, dp2.get_task("verify_bronze_delta").downstream_task_ids)
        self.assertEqual({"submit_silver_dataproc"}, dp2.get_task("wait_silver_quota").downstream_task_ids)
        self.assertEqual({"wait_gold_quota"}, dp2.get_task("verify_silver_delta").downstream_task_ids)
        self.assertEqual({"submit_core_gold_dataproc"}, dp2.get_task("wait_gold_quota").downstream_task_ids)

        dp3 = self.bag.dags["dp3_historical_features"]
        self.assertEqual(5, len(dp3.task_ids))
        self.assertEqual(
            {"verify_silver_delta", "verify_core_gold_delta"},
            dp3.get_task("wait_features_quota").upstream_task_ids,
        )
        self.assertEqual({"submit_historical_features_dataproc"}, dp3.get_task("wait_features_quota").downstream_task_ids)
        for dag_id in ("dp1_source_to_bronze", "dp2_bronze_to_curated", "dp3_historical_features"):
            self.assertEqual(1, self.bag.dags[dag_id].max_active_runs)

    def test_python_tasks_keep_repository_root_cwd_and_import_without_pyspark(self) -> None:
        modules = {
            "dp1_source_to_bronze": {
                "wait_bronze_quota": "ops.gcp.wait_for_dataproc_quota",
                "submit_bronze_dataproc": "ops.gcp.submit_source_to_bronze",
                "verify_bronze_delta": "ops.gcp.verify_delta_run",
            },
            "dp2_bronze_to_curated": {
                "verify_bronze_delta": "ops.gcp.verify_delta_run",
                "wait_silver_quota": "ops.gcp.wait_for_dataproc_quota",
                "submit_silver_dataproc": "ops.gcp.submit_bronze_to_silver",
                "verify_silver_delta": "ops.gcp.verify_delta_run",
                "wait_gold_quota": "ops.gcp.wait_for_dataproc_quota",
                "submit_core_gold_dataproc": "ops.gcp.submit_silver_to_gold",
                "verify_core_gold_delta": "ops.gcp.verify_delta_run",
            },
            "dp3_historical_features": {
                "verify_silver_delta": "ops.gcp.verify_delta_run",
                "verify_core_gold_delta": "ops.gcp.verify_delta_run",
                "wait_features_quota": "ops.gcp.wait_for_dataproc_quota",
                "submit_historical_features_dataproc": "ops.gcp.submit_historical_features",
                "verify_historical_feature_delta": "ops.gcp.verify_delta_run",
            },
        }
        env = dict(os.environ); env.pop("PYTHONPATH", None)
        for dag_id, tasks in modules.items():
            for task_id, module in tasks.items():
                task = self.bag.dags[dag_id].get_task(task_id)
                self.assertEqual(str(REPOSITORY_ROOT), task.cwd)
                result = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=task.cwd,
                                        env=env, capture_output=True, text=True, check=False)
                self.assertEqual(0, result.returncode, result.stderr)

    def test_all_dags_accept_and_propagate_explicit_spark_profile(self) -> None:
        task_ids = {
            "dp1_source_to_bronze": ("submit_bronze_dataproc",),
            "dp2_bronze_to_curated": ("submit_silver_dataproc", "submit_core_gold_dataproc"),
            "dp3_historical_features": ("submit_historical_features_dataproc",),
        }
        for dag_id, submit_tasks in task_ids.items():
            dag = self.bag.dags[dag_id]
            self.assertEqual("optimized", dag.params["spark_profile"])
            self.assertEqual("ab-20260101000000", dag.params["benchmark_id"])
            self.assertEqual("8h", dag.params["dataproc_ttl"])
            for task_id in submit_tasks:
                command = dag.get_task(task_id).bash_command
                self.assertIn("--spark-profile '{{ params.spark_profile }}'", command)
                self.assertIn("--benchmark-id '{{ params.benchmark_id }}'", command)
                self.assertIn("--git-sha '{{ params.git_sha }}'", command)
                self.assertIn("--ttl '{{ params.dataproc_ttl }}'", command)

    def test_quota_guards_share_profile_and_emit_per_handoff_evidence(self) -> None:
        expected = {
            "dp1_source_to_bronze": {
                "wait_bronze_quota": ("external-to-bronze", "params.predecessor_batch_id"),
            },
            "dp2_bronze_to_curated": {
                "wait_silver_quota": ("bronze-to-silver", "params.bronze_run_id"),
                "wait_gold_quota": ("silver-to-gold", "params.silver_run_id"),
            },
            "dp3_historical_features": {
                "wait_features_quota": ("gold-to-features", "params.gold_run_id"),
            },
        }
        for dag_id, tasks in expected.items():
            dag = self.bag.dags[dag_id]
            self.assertEqual("ewallet-data-ai-platform-hg26", dag.params["project_id"])
            self.assertEqual("asia-southeast1", dag.params["region"])
            for task_id, (handoff, predecessor) in tasks.items():
                task = dag.get_task(task_id)
                command = task.bash_command
                self.assertIn("python -m ops.gcp.wait_for_dataproc_quota", command)
                self.assertIn(f"--handoff '{handoff}'", command)
                self.assertIn("--spark-profile '{{ params.spark_profile }}'", command)
                self.assertIn("--benchmark-id '{{ params.benchmark_id }}'", command)
                self.assertIn(f"{{{{ {predecessor} }}}}", command)
                self.assertIn("docs/evidence/quota_handoffs/", command)
                self.assertIn("--execute", command)
                self.assertEqual(20 * 60, task.execution_timeout.total_seconds())

    def test_dp1_baseline_command_renders_exact_profile_and_lineage(self) -> None:
        task = self.bag.dags["dp1_source_to_bronze"].get_task("submit_bronze_dataproc")
        command = task.render_template(task.bash_command, {"params": {
            "dataset_version": "canonical-100gb-v1", "benchmark_id": "ab-20261025123456",
            "spark_profile": "baseline",
            "git_sha": "a" * 40,
            "bronze_run_id": "ab-20261025123456-baseline-bronze-r261025123456",
            "dataproc_ttl": "8h",
        }})
        self.assertIn("--benchmark-id 'ab-20261025123456'", command)
        self.assertIn("--spark-profile 'baseline'", command)
        self.assertIn("--ttl '8h'", command)

    def test_no_cloud_dag_contains_local_absolute_paths(self) -> None:
        for dag_id in ("dp1_source_to_bronze", "dp2_bronze_to_curated", "dp3_historical_features"):
            for task in self.bag.dags[dag_id].tasks:
                command = getattr(task, "bash_command", "")
                self.assertNotIn("/home/", command)
                self.assertNotIn("fintech_python", command)
                if "submit_" in task.task_id:
                    self.assertGreaterEqual(task.execution_timeout.total_seconds(), 8 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
