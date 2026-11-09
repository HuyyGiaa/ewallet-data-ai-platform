"""The campaign DAG in a real Airflow environment: structure and fail-closed gates."""

from __future__ import annotations

import unittest
from pathlib import Path

from airflow.models import DagBag
from airflow.task.trigger_rule import TriggerRule
from jinja2 import Template

from ops.gcp.campaign_task import ACTIONS, PARAMETER_NAMES, CampaignError, parse_parameters


ROOT = Path(__file__).resolve().parents[2]
DAG_ID = "edai1_data_pipeline_campaign"
GROUPS = ("preflight", "dp1_bronze", "dp2_silver", "dp2_gold", "dp3_features", "finalize")
STAGE_GROUPS = GROUPS[1:5]
STAGE_TASKS = ("wait_quota", "submit_transformation", "physical_validation", "semantic_validation")


class CampaignDagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bag = DagBag(dag_folder=str(ROOT / "data_platform/orchestration/airflow/dags"))
        cls.dag = cls.bag.dags[DAG_ID]
        cls.tasks = {task_id: cls.dag.get_task(task_id) for task_id in cls.dag.task_ids}

    def upstream_closure(self, task_id: str) -> set[str]:
        seen: set[str] = set()
        pending = [task_id]
        while pending:
            for parent in self.tasks[pending.pop()].upstream_task_ids:
                if parent not in seen:
                    seen.add(parent)
                    pending.append(parent)
        return seen

    def test_imports_without_errors_and_stays_manual(self):
        self.assertEqual({}, self.bag.import_errors)
        self.assertIsNone(self.dag.schedule)
        self.assertFalse(self.dag.catchup)
        self.assertEqual(1, self.dag.max_active_runs)
        self.assertEqual(1, self.dag.max_active_tasks)

    def test_expected_groups_and_unique_tasks(self):
        self.assertEqual(19, len(self.dag.task_ids))
        self.assertEqual(19, len(set(self.dag.task_ids)))
        self.assertEqual(set(GROUPS), {task_id.split(".", 1)[0] for task_id in self.dag.task_ids})
        for group in STAGE_GROUPS:
            for task in STAGE_TASKS:
                self.assertIn(f"{group}.{task}", self.dag.task_ids)
        self.assertEqual(sorted(ACTIONS), sorted(self.task_action(t) for t in self.dag.task_ids))

    def task_action(self, task_id: str) -> str:
        return self.tasks[task_id].bash_command.rsplit(" ", 1)[1]

    def test_the_chain_is_strictly_linear(self):
        order = ["preflight.verify_identity_and_absence", "preflight.verify_source_dataset"]
        for group in STAGE_GROUPS:
            order += [f"{group}.{task}" for task in STAGE_TASKS]
        order.append("finalize.write_campaign_manifest")
        self.assertEqual(sorted(order), sorted(self.dag.task_ids))
        for parent, child in zip(order, order[1:]):
            with self.subTest(parent=parent, child=child):
                self.assertEqual({child}, set(self.tasks[parent].downstream_task_ids))
                self.assertEqual({parent}, set(self.tasks[child].upstream_task_ids))

    def test_no_downstream_stage_can_bypass_a_validation_gate(self):
        for earlier, later in zip(STAGE_GROUPS, STAGE_GROUPS[1:]):
            closure = self.upstream_closure(f"{later}.submit_transformation")
            self.assertIn(f"{earlier}.semantic_validation", closure)
            self.assertIn(f"{earlier}.physical_validation", closure)
        self.assertIn("preflight.verify_source_dataset", self.upstream_closure("dp1_bronze.submit_transformation"))
        final = self.upstream_closure("finalize.write_campaign_manifest")
        self.assertIn("dp3_features.semantic_validation", final)

    def test_a_failure_anywhere_blocks_everything_after_it(self):
        for task_id, task in self.tasks.items():
            self.assertEqual(TriggerRule.ALL_SUCCESS, TriggerRule(task.trigger_rule), task_id)
        for failed in self.dag.task_ids:
            with self.subTest(failed):
                downstream = {t for t in self.dag.task_ids if failed in self.upstream_closure(t)}
                runnable = self.simulate(failed)
                self.assertFalse(runnable & downstream)

    def simulate(self, failed: str) -> set[str]:
        """Run the graph with ALL_SUCCESS semantics: return the tasks that would run."""
        succeeded: set[str] = set()
        ran: set[str] = set()
        progress = True
        while progress:
            progress = False
            for task_id, task in self.tasks.items():
                if task_id not in ran and task.upstream_task_ids <= succeeded:
                    ran.add(task_id)
                    if task_id != failed:
                        succeeded.add(task_id)
                    progress = True
        return ran

    def test_paid_tasks_never_retry_automatically(self):
        for task_id, task in self.tasks.items():
            paid = task_id.endswith(("submit_transformation", "semantic_validation", "wait_quota"))
            if paid:
                self.assertEqual(0, task.retries, task_id)
        self.assertEqual(2, self.tasks["dp1_bronze.physical_validation"].retries)
        self.assertEqual(2, self.tasks["preflight.verify_source_dataset"].retries)

    def test_parameters_reach_tasks_only_through_the_environment(self):
        for task_id, task in self.tasks.items():
            with self.subTest(task_id):
                self.assertNotIn("{{", task.bash_command)
                self.assertTrue(task.bash_command.endswith(f"-m ops.gcp.campaign_task {self.task_action(task_id)}"))
                self.assertTrue(task.append_env)
                for name in PARAMETER_NAMES:
                    self.assertEqual("{{ params." + name + " }}", task.env[f"EDAI1_CAMPAIGN_{name.upper()}"])

    def test_a_hostile_value_stays_a_single_environment_value(self):
        task = self.tasks["dp1_bronze.submit_transformation"]
        hostile = "x'; rm -rf / #"
        rendered = Template(task.env["EDAI1_CAMPAIGN_DATA_BUCKET"]).render(params={"data_bucket": hostile})
        self.assertEqual(hostile, rendered)
        self.assertNotIn(hostile, task.bash_command)
        values = self.default_parameters()
        values["data_bucket"] = hostile
        with self.assertRaises(CampaignError):
            parse_parameters(values)

    def default_parameters(self) -> dict[str, str]:
        return {name: self.dag.params[name] for name in PARAMETER_NAMES}

    def test_default_parameters_cannot_execute(self):
        self.assertEqual("NOT_APPROVED", self.dag.params["execution_approval"])
        with self.assertRaises(CampaignError):
            parse_parameters(self.default_parameters())

    def test_the_new_workflow_has_no_winner_selection(self):
        text = (ROOT / "data_platform/orchestration/airflow/dags/edai1_data_pipeline_campaign.py").read_text(encoding="utf-8")
        module = (ROOT / "ops/gcp/campaign_task.py").read_text(encoding="utf-8")
        self.assertNotIn("winner", self.dag.task_ids.__repr__().lower())
        self.assertNotIn("pair_manifest", text + module)
        self.assertNotIn("dp2_method2", text + module)
        self.assertNotIn("select_stage_winner", text + module)

    def test_existing_dags_are_still_registered(self):
        for dag_id in ("dp1_source_to_bronze", "dp2_bronze_to_curated", "dp3_historical_features"):
            self.assertIn(dag_id, self.bag.dags)


if __name__ == "__main__":
    unittest.main()
