"""The three EDAI-1 pipeline DAGs in a real Airflow environment: structure and gates.

dp1_source_to_bronze, dp2_bronze_to_curated and dp3_historical_features are
independent DAGs; the integrated edai1_data_pipeline_campaign is retired.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from airflow.models import DagBag
from airflow.task.trigger_rule import TriggerRule
from jinja2 import Template

from ops.gcp.campaign_task import PIPELINES, CampaignError, actions_for, parse_parameters


ROOT = Path(__file__).resolve().parents[2]
DAG_FOLDER = ROOT / "data_platform/orchestration/airflow/dags"
STAGE_TASKS = ("wait_quota", "submit_transformation", "physical_validation", "semantic_validation")
EXPECTED_ORDER = {
    "dp1_source_to_bronze": [
        "preflight.verify_identity_and_absence", "preflight.verify_source_dataset",
        *[f"bronze.{task}" for task in STAGE_TASKS], "finalize.publish_dp1_handoff"],
    "dp2_bronze_to_curated": [
        "preflight.verify_identity_and_absence", "preflight.verify_dp1_handoff", "preflight.verify_bronze_inputs",
        *[f"silver.{task}" for task in STAGE_TASKS], *[f"gold.{task}" for task in STAGE_TASKS],
        "finalize.publish_dp2_handoff"],
    "dp3_historical_features": [
        "preflight.verify_identity_and_absence", "preflight.verify_dp2_handoff",
        "preflight.verify_silver_gold_inputs", *[f"features.{task}" for task in STAGE_TASKS],
        "finalize.publish_dp3_completion"],
}
PIPELINE_OF = {spec.dag_id: name for name, spec in PIPELINES.items()}


class PipelineDagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bag = DagBag(dag_folder=str(DAG_FOLDER))

    def tasks(self, dag_id: str):
        dag = self.bag.dags[dag_id]
        return {task_id: dag.get_task(task_id) for task_id in dag.task_ids}

    def upstream_closure(self, dag_id: str, task_id: str) -> set[str]:
        tasks = self.tasks(dag_id)
        seen: set[str] = set()
        pending = [task_id]
        while pending:
            for parent in tasks[pending.pop()].upstream_task_ids:
                if parent not in seen:
                    seen.add(parent)
                    pending.append(parent)
        return seen

    def test_exactly_three_edai1_pipeline_dags_and_no_integrated_campaign(self):
        self.assertEqual({}, self.bag.import_errors)
        edai1 = {dag_id for dag_id, dag in self.bag.dags.items() if "edai1" in dag.tags}
        self.assertEqual(set(EXPECTED_ORDER), edai1)
        self.assertNotIn("edai1_data_pipeline_campaign", self.bag.dags)
        self.assertFalse((DAG_FOLDER / "edai1_data_pipeline_campaign.py").exists())
        self.assertTrue((ROOT / "archive/legacy-orchestration/dags/edai1_data_pipeline_campaign.py").exists())
        for other in ("feast_incremental_materialization", "rag_fraud_policy", "rag_fraud_policy_ingestion"):
            self.assertIn(other, self.bag.dags)

    def test_every_dag_is_manual_and_serial(self):
        for dag_id in EXPECTED_ORDER:
            with self.subTest(dag_id):
                dag = self.bag.dags[dag_id]
                self.assertIsNone(dag.schedule)
                self.assertFalse(dag.catchup)
                self.assertEqual(1, dag.max_active_runs)
                self.assertEqual(1, dag.max_active_tasks)
                self.assertIn(PIPELINE_OF[dag_id], dag.tags)

    def test_each_graph_is_strictly_linear_in_the_expected_order(self):
        for dag_id, order in EXPECTED_ORDER.items():
            tasks = self.tasks(dag_id)
            self.assertEqual(sorted(order), sorted(tasks))
            for parent, child in zip(order, order[1:]):
                with self.subTest(dag_id=dag_id, parent=parent):
                    self.assertEqual({child}, set(tasks[parent].downstream_task_ids))
                    self.assertEqual({parent}, set(tasks[child].upstream_task_ids))

    def test_dp2_shows_silver_and_gold_as_separate_task_groups(self):
        groups = {task_id.split(".", 1)[0] for task_id in self.bag.dags["dp2_bronze_to_curated"].task_ids}
        self.assertEqual({"preflight", "silver", "gold", "finalize"}, groups)

    def test_every_action_of_a_pipeline_appears_exactly_once(self):
        for dag_id in EXPECTED_ORDER:
            with self.subTest(dag_id):
                actions = [task.bash_command.rsplit(" ", 1)[1] for task in self.tasks(dag_id).values()]
                self.assertEqual(sorted(actions_for(PIPELINE_OF[dag_id])), sorted(actions))

    def test_no_paid_stage_can_bypass_a_gate(self):
        for dag_id in EXPECTED_ORDER:
            for task_id in self.tasks(dag_id):
                if task_id.endswith(("submit_transformation", "wait_quota")):
                    with self.subTest(dag_id=dag_id, task_id=task_id):
                        closure = self.upstream_closure(dag_id, task_id)
                        self.assertIn("preflight.verify_identity_and_absence", closure)
        self.assertIn("preflight.verify_source_dataset",
                      self.upstream_closure("dp1_source_to_bronze", "bronze.submit_transformation"))
        for dag_id, handoff in (("dp2_bronze_to_curated", "verify_dp1_handoff"),
                                ("dp3_historical_features", "verify_dp2_handoff")):
            first = next(task for task in EXPECTED_ORDER[dag_id] if task.endswith("wait_quota"))
            self.assertIn(f"preflight.{handoff}", self.upstream_closure(dag_id, first))
        gold = self.upstream_closure("dp2_bronze_to_curated", "gold.submit_transformation")
        self.assertTrue({"silver.physical_validation", "silver.semantic_validation"} <= gold)
        for dag_id, order in EXPECTED_ORDER.items():
            publish = self.upstream_closure(dag_id, order[-1])
            for task_id in order[:-1]:
                self.assertIn(task_id, publish)

    def test_a_failure_anywhere_blocks_everything_after_it(self):
        for dag_id in EXPECTED_ORDER:
            tasks = self.tasks(dag_id)
            for task_id, task in tasks.items():
                self.assertEqual(TriggerRule.ALL_SUCCESS, TriggerRule(task.trigger_rule), task_id)
            for failed in tasks:
                with self.subTest(dag_id=dag_id, failed=failed):
                    downstream = {t for t in tasks if failed in self.upstream_closure(dag_id, t)}
                    self.assertFalse(self.simulate(tasks, failed) & downstream)

    @staticmethod
    def simulate(tasks, failed: str) -> set[str]:
        """Run the graph with ALL_SUCCESS semantics: return the tasks that would run."""
        succeeded: set[str] = set()
        ran: set[str] = set()
        progress = True
        while progress:
            progress = False
            for task_id, task in tasks.items():
                if task_id not in ran and task.upstream_task_ids <= succeeded:
                    ran.add(task_id)
                    if task_id != failed:
                        succeeded.add(task_id)
                    progress = True
        return ran

    def test_paid_tasks_never_retry_and_read_only_checks_may(self):
        for dag_id in EXPECTED_ORDER:
            for task_id, task in self.tasks(dag_id).items():
                with self.subTest(dag_id=dag_id, task_id=task_id):
                    paid = task_id.endswith(("submit_transformation", "semantic_validation", "wait_quota",
                                             "verify_identity_and_absence"))
                    self.assertEqual(0 if paid else 2, task.retries)
                    if task_id.endswith("submit_transformation"):
                        self.assertGreaterEqual(task.execution_timeout.total_seconds(), 8 * 3600)

    def test_parameters_reach_tasks_only_through_the_environment(self):
        for dag_id in EXPECTED_ORDER:
            pipeline = PIPELINE_OF[dag_id]
            dag = self.bag.dags[dag_id]
            self.assertEqual(set(PIPELINES[pipeline].parameter_names), set(dag.params.keys()))
            for task_id, task in self.tasks(dag_id).items():
                with self.subTest(dag_id=dag_id, task_id=task_id):
                    self.assertNotIn("{{", task.bash_command)
                    self.assertIn("-m ops.gcp.campaign_task ", task.bash_command)
                    self.assertNotIn("/home/", task.bash_command)
                    self.assertEqual(str(ROOT), task.cwd)
                    self.assertTrue(task.append_env)
                    self.assertEqual(pipeline, task.env["EDAI1_CAMPAIGN_PIPELINE"])
                    self.assertEqual("{{ dag.dag_id }}", task.env["EDAI1_CAMPAIGN_DAG_ID"])
                    self.assertEqual("{{ run_id }}", task.env["EDAI1_CAMPAIGN_DAG_RUN_ID"])
                    for name in PIPELINES[pipeline].parameter_names:
                        self.assertEqual("{{ params." + name + " }}", task.env[f"EDAI1_CAMPAIGN_{name.upper()}"])
                    for name in set(PIPELINES["dp3"].run_parameters) - set(PIPELINES[pipeline].run_parameters):
                        self.assertNotIn(f"EDAI1_CAMPAIGN_{name.upper()}", task.env)

    def test_a_hostile_value_stays_a_single_environment_value(self):
        dag = self.bag.dags["dp2_bronze_to_curated"]
        task = dag.get_task("silver.submit_transformation")
        hostile = "x'; rm -rf / #"
        rendered = Template(task.env["EDAI1_CAMPAIGN_DATA_BUCKET"]).render(params={"data_bucket": hostile})
        self.assertEqual(hostile, rendered)
        self.assertNotIn(hostile, task.bash_command)
        values = self.default_parameters(dag)
        values["data_bucket"] = hostile
        with self.assertRaises(CampaignError):
            parse_parameters(values, pipeline="dp2", require_approval=False)

    @staticmethod
    def default_parameters(dag) -> dict[str, str]:
        return {name: dag.params[name] for name in dag.params.keys()}

    def test_default_parameters_of_all_six_dag_profile_combinations_cannot_execute(self):
        for dag_id in EXPECTED_ORDER:
            pipeline = PIPELINE_OF[dag_id]
            dag = self.bag.dags[dag_id]
            self.assertEqual("NOT_APPROVED", dag.params["execution_approval"])
            self.assertEqual("rehearsal-5gb-v1", dag.params["dataset_version"])
            self.assertEqual("ewallet-data-ai-platform-hg26-rehearsal-5gb-data", dag.params["data_bucket"])
            for profile in ("baseline", "optimized"):
                with self.subTest(dag_id=dag_id, profile=profile):
                    values = self.default_parameters(dag)
                    values["spark_profile"] = profile
                    with self.assertRaisesRegex(CampaignError, "approval|run|git_sha"):
                        parse_parameters(values, pipeline=pipeline)

    def test_no_winner_selection_in_the_new_workflow(self):
        sources = [(ROOT / "data_platform/orchestration/airflow/edai1_pipeline.py").read_text(encoding="utf-8")]
        sources += [(DAG_FOLDER / f"{dag_id}.py").read_text(encoding="utf-8") for dag_id in EXPECTED_ORDER]
        for dag_id in EXPECTED_ORDER:
            self.assertNotIn("winner", repr(self.bag.dags[dag_id].task_ids).lower())
        for forbidden in ("pair_manifest", "dp2_method2", "select_stage_winner", "bronze_winner"):
            self.assertNotIn(forbidden, "\n".join(sources))


if __name__ == "__main__":
    unittest.main()
