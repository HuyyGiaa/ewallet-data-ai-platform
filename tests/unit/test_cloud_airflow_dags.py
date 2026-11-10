from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from airflow.models import DagBag

from data_platform.orchestration.airflow.python_runtime import (
    PYTHON_ENV_VAR,
    python_module_command,
    resolve_airflow_python,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DAG_ROOT = REPOSITORY_ROOT / "data_platform/orchestration/airflow/dags"


class CloudAirflowDagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bag = DagBag(dag_folder=str(DAG_ROOT))

    def test_cloud_dags_parse_without_errors(self) -> None:
        self.assertEqual({}, self.bag.import_errors)

    def test_pipeline_tasks_keep_repository_root_cwd_and_import_without_pyspark(self) -> None:
        env = dict(os.environ); env.pop("PYTHONPATH", None)
        for dag_id in ("dp1_source_to_bronze", "dp2_bronze_to_curated", "dp3_historical_features"):
            for task in self.bag.dags[dag_id].tasks:
                with self.subTest(dag_id=dag_id, task_id=task.task_id):
                    self.assertEqual(str(REPOSITORY_ROOT), task.cwd)
                    self.assertTrue(task.append_env)
                    self.assertEqual(resolve_airflow_python(REPOSITORY_ROOT), task.env[PYTHON_ENV_VAR])
                    self.assertIn("-m ops.gcp.campaign_task ", task.bash_command)
                    self.assertNotIn("fintech_python", task.bash_command)
        task = self.bag.dags["dp1_source_to_bronze"].get_task("bronze.submit_transformation")
        result = subprocess.run(
            [task.env[PYTHON_ENV_VAR], "-c", "import ops.gcp.campaign_task, sys; "
             "sys.exit('pyspark' in sys.modules)"],
            cwd=task.cwd, env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_quota_guard_sits_immediately_before_every_submit(self) -> None:
        for dag_id in ("dp1_source_to_bronze", "dp2_bronze_to_curated", "dp3_historical_features"):
            dag = self.bag.dags[dag_id]
            for task_id in dag.task_ids:
                if task_id.endswith(".submit_transformation"):
                    group = task_id.split(".", 1)[0]
                    with self.subTest(dag_id=dag_id, task_id=task_id):
                        self.assertEqual({f"{group}.wait_quota"}, dag.get_task(task_id).upstream_task_ids)
                        self.assertEqual(20 * 60, dag.get_task(f"{group}.wait_quota").execution_timeout.total_seconds())

    def test_project_and_region_defaults(self) -> None:
        for dag_id in ("dp1_source_to_bronze", "dp2_bronze_to_curated", "dp3_historical_features"):
            dag = self.bag.dags[dag_id]
            self.assertEqual("ewallet-data-ai-platform-hg26", dag.params["project_id"])
            self.assertEqual("asia-southeast1", dag.params["region"])
            self.assertEqual("8h", dag.params["dataproc_ttl"])

    def test_python_task_command_runs_with_airflow_path_removed(self) -> None:
        task = self.bag.dags["dp1_source_to_bronze"].get_task("bronze.wait_quota")
        env = {"PATH": "/usr/bin:/bin", **task.env}
        result = subprocess.run(
            [
                "/usr/bin/bash",
                "-c",
                python_module_command("ops.gcp.wait_for_dataproc_quota") + " --help",
            ],
            cwd=task.cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("quota-readiness gate", result.stdout)

    def test_python_task_command_fails_clearly_without_configured_interpreter(self) -> None:
        result = subprocess.run(
            [
                "/usr/bin/bash",
                "-c",
                python_module_command("ops.gcp.verify_delta_run") + " --help",
            ],
            cwd=REPOSITORY_ROOT,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn(f"{PYTHON_ENV_VAR} is not configured", result.stderr)

    def test_invalid_explicit_interpreter_fails_dag_parse_validation(self) -> None:
        resolve_airflow_python.cache_clear()
        try:
            with mock.patch.dict(
                os.environ,
                {PYTHON_ENV_VAR: "/definitely/missing/ewallet-python"},
            ):
                with self.assertRaisesRegex(RuntimeError, f"Invalid {PYTHON_ENV_VAR}"):
                    resolve_airflow_python(REPOSITORY_ROOT)
        finally:
            resolve_airflow_python.cache_clear()


if __name__ == "__main__":
    unittest.main()
