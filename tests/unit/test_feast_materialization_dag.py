"""Airflow graph and parameter contract for the Feast materialization DAG."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from airflow.models import DagBag

from data_platform.orchestration.airflow.feast_params import (
    FEAST_PARAM_ENV,
    FEAST_PARAM_SCHEMAS,
    feast_task_environment,
)
from data_platform.orchestration.airflow.python_runtime import python_module_command


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DAG_ROOT = REPOSITORY_ROOT / "data_platform/orchestration/airflow/dags"
ORDER = (
    "validate_config",
    "validate_source_identity",
    "determine_window",
    "materialize_window",
    "verify_job_completion",
    "verify_online_readback",
    "record_run_provenance",
)


class FeastDagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bag = DagBag(dag_folder=str(DAG_ROOT))
        cls.dag = cls.bag.dags["feast_incremental_materialization"]

    def test_all_dags_still_parse(self):
        self.assertEqual({}, self.bag.import_errors)

    def test_the_graph_is_one_ordered_chain(self):
        self.assertEqual(set(ORDER), set(self.dag.task_ids))
        for left, right in zip(ORDER, ORDER[1:]):
            self.assertEqual({right}, self.dag.get_task(left).downstream_task_ids)
        self.assertEqual(set(), self.dag.get_task(ORDER[-1]).downstream_task_ids)

    def test_the_dag_is_manual_and_cannot_overlap_itself(self):
        self.assertIsNone(self.dag.schedule)
        self.assertFalse(self.dag.catchup)
        self.assertEqual(1, self.dag.max_active_runs)

    def test_the_default_is_a_dry_run(self):
        self.assertEqual("dry_run", self.dag.params["mode"])
        self.assertEqual("", self.dag.params["approval"])

    def test_commands_hold_no_parameter_templates(self):
        for task in self.dag.tasks:
            self.assertNotIn("{{", task.bash_command)
            self.assertIn("ops.mlops.feast_incremental", task.bash_command)

    def test_every_task_receives_every_parameter_as_a_variable(self):
        for task in self.dag.tasks:
            for variable in FEAST_PARAM_ENV.values():
                self.assertIn(variable, task.env)

    def test_write_steps_retry_and_have_a_deadline(self):
        for name in ("materialize_window", "verify_online_readback"):
            task = self.dag.get_task(name)
            self.assertEqual(1, task.retries)
            self.assertIsNotNone(task.execution_timeout)


class FeastParamsTest(unittest.TestCase):
    def test_every_parameter_has_a_variable(self):
        self.assertEqual(set(FEAST_PARAM_SCHEMAS), set(FEAST_PARAM_ENV))

    def test_the_retry_number_reaches_the_task(self):
        self.assertEqual("{{ ti.try_number }}", feast_task_environment()["C1_ATTEMPT"])

    def test_identifier_patterns_reject_shell_text(self):
        pattern = FEAST_PARAM_SCHEMAS["feature_run_id"]["pattern"]
        self.assertIsNotNone(re.search(pattern, "ab-1-optimized-feat-r1"))
        for value in ("a;rm -rf /", "$(id)", "abc\n"):
            self.assertIsNone(re.search(pattern, value))

    def test_the_module_is_on_the_allowlist(self):
        self.assertIn("ops.mlops.feast_incremental", python_module_command("ops.mlops.feast_incremental"))


if __name__ == "__main__":
    unittest.main()
