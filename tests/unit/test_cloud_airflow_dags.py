from __future__ import annotations

import unittest
from pathlib import Path

from airflow.models import DagBag


DAG_ROOT = (
    Path(__file__).resolve().parents[2]
    / "data_platform/orchestration/airflow/dags"
)


class CloudAirflowDagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bag = DagBag(dag_folder=str(DAG_ROOT))

    def test_cloud_dags_parse_without_errors(self) -> None:
        self.assertEqual({}, self.bag.import_errors)

    def test_dp1_graph(self) -> None:
        dag = self.bag.dags["dp1_source_to_bronze"]
        self.assertEqual(1, dag.max_active_runs)
        self.assertEqual(
            {
                "verify_source_manifest",
                "submit_bronze_dataproc",
                "verify_bronze_delta",
            },
            set(dag.task_ids),
        )

    def test_dp2_graph(self) -> None:
        dag = self.bag.dags["dp2_bronze_to_curated"]
        self.assertEqual(1, dag.max_active_runs)
        self.assertEqual(5, len(dag.task_ids))
        submit = dag.get_task("submit_core_gold_dataproc")
        self.assertIn("ops.gcp.submit_silver_to_gold", submit.bash_command)

    def test_dp3_graph(self) -> None:
        dag = self.bag.dags["dp3_historical_features"]
        self.assertEqual(1, dag.max_active_runs)
        self.assertEqual(4, len(dag.task_ids))
        submit = dag.get_task("submit_historical_features_dataproc")
        self.assertIn("ops.gcp.submit_historical_features", submit.bash_command)

    def test_no_cloud_dag_contains_local_absolute_paths(self) -> None:
        for dag_id in (
            "dp1_source_to_bronze",
            "dp2_bronze_to_curated",
            "dp3_historical_features",
        ):
            for task in self.bag.dags[dag_id].tasks:
                command = getattr(task, "bash_command", "")
                self.assertNotIn("/home/", command)
                self.assertNotIn("fintech_python", command)

    def test_dataproc_ttl_is_a_dag_parameter(self) -> None:
        task_ids = {
            "dp1_source_to_bronze": ("submit_bronze_dataproc",),
            "dp2_bronze_to_curated": (
                "submit_silver_dataproc",
                "submit_core_gold_dataproc",
            ),
            "dp3_historical_features": (
                "submit_historical_features_dataproc",
            ),
        }
        for dag_id, submit_tasks in task_ids.items():
            dag = self.bag.dags[dag_id]
            self.assertEqual("2h", dag.params["dataproc_ttl"])
            for task_id in submit_tasks:
                task = dag.get_task(task_id)
                self.assertIn(
                    "--ttl '{{ params.dataproc_ttl }}'",
                    task.bash_command,
                )
                self.assertGreaterEqual(
                    task.execution_timeout.total_seconds(),
                    8 * 60 * 60,
                )

    def test_dataproc_performance_profile_is_explicit_and_defaults_to_pilot(self) -> None:
        task_ids = {
            "dp1_source_to_bronze": ("submit_bronze_dataproc",),
            "dp2_bronze_to_curated": (
                "submit_silver_dataproc",
                "submit_core_gold_dataproc",
            ),
            "dp3_historical_features": (
                "submit_historical_features_dataproc",
            ),
        }
        for dag_id, submit_tasks in task_ids.items():
            dag = self.bag.dags[dag_id]
            self.assertEqual("default", dag.params["dataproc_performance_profile"])
            for task_id in submit_tasks:
                task = dag.get_task(task_id)
                self.assertIn(
                    "--performance-profile "
                    "'{{ params.dataproc_performance_profile }}'",
                    task.bash_command,
                )


if __name__ == "__main__":
    unittest.main()
