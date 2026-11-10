"""Handoff contracts between the three independent pipeline DAGs.

DP1 -> dp1_handoff.json -> DP2 -> dp2_handoff.json -> DP3 -> dp3_completion.json and
campaign_manifest.json. Every manifest is write-once; every consumer verifies the
identity, the upstream validation reports and the upstream chain before paid work.
"""

from __future__ import annotations

import hashlib
import json

from ops.gcp import campaign_task
from ops.gcp.campaign_task import CampaignError, handoff_problems, handoff_uri, run_action, verify_handoff
from tests.unit.campaign_fakes import (
    BENCH,
    BUCKET,
    SHA,
    SOURCE_SHA,
    RecordingRunner,
    TerraformFixture,
    finished_batches,
    producer,
    run_id,
)


class HandoffPublicationTest(TerraformFixture):
    def test_dp1_handoff_records_identity_provenance_and_validation(self):
        runner = RecordingRunner(existing_batches=finished_batches("baseline"))
        document = self.publish("dp1", runner)
        self.assertEqual(f"gs://{BUCKET}/lakehouse/campaigns/{BENCH}/baseline/dp1_handoff.json",
                         handoff_uri(self.params(pipeline="dp1"), "dp1"))
        self.assertEqual(("edai1-pipeline-handoff-v1", "dp1", "SUCCESS"),
                         (document["schema_version"], document["pipeline"], document["status"]))
        self.assertEqual((BENCH, "rehearsal-5gb-v1", "baseline", SHA, SOURCE_SHA),
                         (document["benchmark_id"], document["dataset_version"], document["spark_profile"],
                          document["git_sha"], document["source_manifest_sha256"]))
        self.assertEqual({"dag_id": "dp1_source_to_bronze", "dag_run_id": f"{BENCH}-baseline-dp1"}, document["producer"])
        self.assertIsNone(document["upstream"])
        self.assertEqual({"bronze": run_id("baseline", "bronze")}, document["run_ids"])
        bronze = document["stages"]["bronze"]
        self.assertEqual(f"gs://{BUCKET}/lakehouse/runs/{run_id('baseline', 'bronze')}/bronze", bronze["output_root"])
        self.assertEqual("SUCCEEDED", bronze["execution_metadata"]["dataproc_batch_state"])
        self.assertEqual("app-1", bronze["execution_metadata"]["spark_application_id"])
        self.assertTrue(bronze["execution_metadata"]["history_server_uri"].startswith("https://history.example/"))
        self.assertEqual("PASS", bronze["validation"]["status"])
        self.assertTrue(bronze["validation"]["report_uri"].endswith("/evidence/validation-bronze-a1.json"))
        self.assertEqual(SOURCE_SHA, document["source"]["manifest_sha256"])
        self.assertTrue(document["completed_at"])

    def test_dp2_handoff_binds_the_exact_dp1_handoff(self):
        runner = self.completed_runner(upto="dp2")
        dp1_text = runner.objects[handoff_uri(self.params(pipeline="dp1"), "dp1")]
        document = json.loads(runner.objects[handoff_uri(self.params(pipeline="dp2"), "dp2")])
        self.assertEqual({"silver", "gold"}, set(document["stages"]))
        self.assertEqual("dp1", document["upstream"]["pipeline"])
        self.assertEqual(hashlib.sha256(dp1_text.encode()).hexdigest(), document["upstream"]["sha256"])
        self.assertEqual(f"{BENCH}-baseline-dp1", document["upstream"]["dag_run_id"])

    def test_dp3_completion_points_at_the_campaign_manifest(self):
        runner = self.completed_runner(upto="dp3")
        params = self.params()
        completion = json.loads(runner.objects[handoff_uri(params, "dp3")])
        manifest_text = runner.objects[campaign_task.campaign_manifest_uri(params)]
        self.assertEqual({"features"}, set(completion["stages"]))
        self.assertEqual(campaign_task.campaign_manifest_uri(params), completion["campaign_manifest"]["uri"])
        self.assertEqual(hashlib.sha256(manifest_text.encode()).hexdigest(), completion["campaign_manifest"]["sha256"])
        verify_handoff(params, "dp3", runner)

    def test_a_retried_publication_is_idempotent(self):
        runner = self.completed_runner(upto="dp1")
        uri = handoff_uri(self.params(pipeline="dp1"), "dp1")
        before = runner.objects[uri]
        self.publish("dp1", runner)
        self.assertEqual(before, runner.objects[uri])

    def test_a_conflicting_manifest_is_never_overwritten(self):
        runner = self.completed_runner(upto="dp1")
        uri = handoff_uri(self.params(pipeline="dp1"), "dp1")
        conflicting = json.loads(runner.objects[uri])
        conflicting["producer"]["dag_run_id"] = "another-dag-run"
        runner.objects[uri] = json.dumps(conflicting)
        with self.assertRaisesRegex(CampaignError, "different manifest already exists"):
            self.publish("dp1", runner)
        self.assertEqual("another-dag-run", json.loads(runner.objects[uri])["producer"]["dag_run_id"])

    def test_a_publish_race_fails_closed(self):
        runner = RecordingRunner(existing_batches=finished_batches("baseline"))
        uri = handoff_uri(self.params(pipeline="dp1"), "dp1")
        original = runner.__call__

        def racing(command, **kwargs):  # another writer creates the object between check and write
            if command[:3] == ["gcloud", "storage", "cp"] and command[4] == uri:
                runner.objects[uri] = "{}"
            return original(command, **kwargs)

        with self.assertRaisesRegex(CampaignError, "could not be published"):
            run_action("publish-handoff", self.params(pipeline="dp1"), attempt=1, runner=racing,
                       producer=producer("dp1"))


class HandoffVerificationTest(TerraformFixture):
    def tamper(self, runner: RecordingRunner, pipeline: str, change) -> None:
        uri = handoff_uri(self.params(pipeline=pipeline), pipeline)
        document = json.loads(runner.objects[uri])
        change(document)
        runner.objects[uri] = json.dumps(document)

    def assert_dp2_refused(self, runner: RecordingRunner, message: str) -> None:
        submits_before = len(self.submits(runner))
        for action in ("verify-upstream-handoff", "submit-silver", "publish-handoff"):
            with self.subTest(action), self.assertRaisesRegex(CampaignError, message):
                run_action(action, self.params(pipeline="dp2"), attempt=1, runner=runner, producer=producer("dp2"))
        self.assertEqual(submits_before, len(self.submits(runner)))
        self.assertNotIn(handoff_uri(self.params(pipeline="dp2"), "dp2"), runner.objects)

    def test_a_missing_handoff_refuses_all_downstream_work(self):
        self.assert_dp2_refused(RecordingRunner(existing_batches=finished_batches("baseline")), "DP1 handoff is missing")
        runner = self.completed_runner(upto="dp1")
        for action in ("verify-upstream-handoff", "submit-features"):
            with self.subTest(action), self.assertRaisesRegex(CampaignError, "DP2 handoff is missing"):
                run_action(action, self.params(pipeline="dp3"), attempt=1, runner=runner)

    def test_identity_mismatches_are_refused(self):
        cases = {
            "benchmark_id": "ab-20261110000001",
            "dataset_version": "canonical-100gb-v1",
            "source_manifest_sha256": "c" * 64,
            "git_sha": "d" * 40,
            "spark_profile": "optimized",
            "data_bucket": "another-bucket",
            "status": "FAILED",
            "schema_version": "edai1-pipeline-handoff-v0",
        }
        for name, wrong in cases.items():
            with self.subTest(name):
                runner = self.completed_runner(upto="dp1")
                self.tamper(runner, "dp1", lambda document: document.update({name: wrong}))
                self.assert_dp2_refused(runner, name)

    def test_a_handoff_from_another_dag_or_run_is_refused(self):
        runner = self.completed_runner(upto="dp1")
        self.tamper(runner, "dp1", lambda document: document["producer"].update(dag_id="dp2_bronze_to_curated"))
        self.assert_dp2_refused(runner, "was not produced by a dp1_source_to_bronze DagRun")
        runner = self.completed_runner(upto="dp1")
        other = run_id("baseline", "bronze").replace("r261110000000", "r261110000009")

        def other_run(document):
            document["run_ids"]["bronze"] = other
            document["stages"]["bronze"]["run_id"] = other
        self.tamper(runner, "dp1", other_run)
        self.assert_dp2_refused(runner, "bronze run")

    def test_an_upstream_validation_failure_is_refused(self):
        runner = self.completed_runner(upto="dp1")
        self.tamper(runner, "dp1", lambda document: document["stages"]["bronze"]["validation"].update(status="FAIL"))
        self.assert_dp2_refused(runner, "validation is not PASS")
        runner = self.completed_runner(upto="dp1")
        self.tamper(runner, "dp1", lambda document: document["stages"]["bronze"]["execution_metadata"].update(
            dataproc_batch_state="FAILED"))
        self.assert_dp2_refused(runner, "transformation batch")

    def test_the_upstream_report_is_re_read_and_must_still_pass(self):
        runner = self.completed_runner(upto="dp1")
        runner.failing_scopes = {"bronze"}
        with self.assertRaisesRegex(CampaignError, "validation report .* refused"):
            run_action("verify-upstream-handoff", self.params(pipeline="dp2"), attempt=1, runner=runner)

    def test_partial_upstream_output_is_refused(self):
        runner = self.completed_runner(upto="dp1", tables={"users", "accounts"})
        with self.assertRaisesRegex(CampaignError, "upstream bronze output is incomplete"):
            run_action("verify-upstream-inputs", self.params(pipeline="dp2"), attempt=1, runner=runner)
        runner = self.completed_runner(upto="dp1")
        run_action("verify-upstream-inputs", self.params(pipeline="dp2"), attempt=1, runner=runner)

    def test_dp3_refuses_a_dp2_handoff_built_on_another_dp1_handoff(self):
        runner = self.completed_runner(upto="dp2")
        run_action("verify-upstream-handoff", self.params(), attempt=1, runner=runner)
        self.tamper(runner, "dp1", lambda document: document.update(completed_at="2026-01-01T00:00:00+00:00"))
        with self.assertRaisesRegex(CampaignError, "built on a different DP1 handoff"):
            run_action("verify-upstream-handoff", self.params(), attempt=1, runner=runner)

    def test_baseline_and_optimized_never_mix(self):
        runner = self.completed_runner("baseline", upto="dp2")
        with self.assertRaisesRegex(CampaignError, "DP1 handoff is missing"):
            run_action("verify-upstream-handoff", self.params("optimized", "dp2"), attempt=1, runner=runner)
        # A baseline handoff copied under the optimized prefix is refused on identity.
        baseline_uri = handoff_uri(self.params("baseline", "dp1"), "dp1")
        runner.objects[handoff_uri(self.params("optimized", "dp1"), "dp1")] = runner.objects[baseline_uri]
        with self.assertRaisesRegex(CampaignError, "spark_profile"):
            run_action("verify-upstream-handoff", self.params("optimized", "dp2"), attempt=1, runner=runner)
        self.assertNotEqual(handoff_uri(self.params("baseline", "dp2"), "dp2"),
                            handoff_uri(self.params("optimized", "dp2"), "dp2"))

    def test_handoff_problems_is_pure_and_complete(self):
        params = self.params(pipeline="dp2")
        self.assertIn("DP1 handoff is not a JSON object", handoff_problems([], params, "dp1"))
        problems = handoff_problems({}, params, "dp1")
        for fragment in ("schema_version", "pipeline", "status", "benchmark_id", "git_sha", "source_manifest_sha256",
                         "was not produced", "validation is not PASS", "covers"):
            self.assertTrue(any(fragment in problem for problem in problems), fragment)
        runner = self.completed_runner(upto="dp2")
        document = json.loads(runner.objects[handoff_uri(params, "dp2")])
        self.assertEqual([], handoff_problems(document, params, "dp2"))
        document["upstream"] = None
        self.assertTrue(any("does not reference the DP1 handoff" in item for item in handoff_problems(document, params, "dp2")))
