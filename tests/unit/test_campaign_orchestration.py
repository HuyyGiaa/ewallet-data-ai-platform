"""Pipeline tasks: parameters, approval, command construction and fail-closed gates.

No test reaches gcloud: a recording runner stands in for every subprocess call.
Handoff contracts between the three DAGs are in test_campaign_handoffs.py.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from dataclasses import replace
from unittest.mock import patch

from data_platform.config.dataset_identity import (
    APPROVED_DATASETS,
    CANONICAL_100GB,
    CANONICAL_DATA_BUCKET,
    REHEARSAL_5GB,
)
from ops.gcp import campaign_task, submit_bronze_to_silver, submit_source_to_bronze, submit_stage_validation
from ops.gcp.campaign_task import PIPELINES, CampaignError, actions_for, parse_parameters, run_action
from tests.unit.campaign_fakes import (
    BENCH,
    BUCKET,
    SHA,
    RecordingRunner,
    TerraformFixture,
    finished_batches,
    producer,
    run_id,
    terraform_outputs,
    values,
)


class ParameterTest(unittest.TestCase):
    def assert_refused(self, message: str, pipeline: str = "dp3", **overrides: str) -> None:
        with self.assertRaisesRegex(CampaignError, message):
            parse_parameters(values(pipeline=pipeline, **overrides), pipeline=pipeline)

    def test_valid_parameters_for_every_pipeline_and_profile(self):
        for pipeline in PIPELINES:
            for profile in ("baseline", "optimized"):
                with self.subTest(pipeline=pipeline, profile=profile):
                    params = parse_parameters(values(profile, pipeline), pipeline=pipeline)
                    self.assertEqual(profile, params.spark_profile)
                    for stage in PIPELINES[pipeline].stages:
                        self.assertIn(f"-{profile}-", params.stage_run_id(stage))
        params = parse_parameters(values(pipeline="dp2"), pipeline="dp2")
        self.assertEqual(f"gs://{BUCKET}/lakehouse/runs/{params.silver_run_id}/silver", params.run_root("silver"))
        self.assertIsNone(params.feature_run_id)

    def test_each_pipeline_accepts_only_its_own_run_parameters(self):
        self.assertEqual(("bronze_run_id",), PIPELINES["dp1"].run_parameters)
        self.assertEqual(("bronze_run_id", "silver_run_id", "gold_run_id"), PIPELINES["dp2"].run_parameters)
        supplied = values(pipeline="dp1")
        supplied["silver_run_id"] = run_id("baseline", "silver")
        with self.assertRaisesRegex(CampaignError, "do not belong to DP1"):
            parse_parameters(supplied, pipeline="dp1")
        with self.assertRaisesRegex(CampaignError, "pipeline must be one of"):
            parse_parameters(values(), pipeline="dp4")

    def test_a_future_registered_dataset_version_is_accepted(self):
        future = "final-100gb-v2"
        identity = replace(APPROVED_DATASETS[REHEARSAL_5GB], version=future)
        with patch.dict(APPROVED_DATASETS, {future: identity}):
            parse_parameters(values(dataset_version=future), pipeline="dp3")

    def test_invalid_profile_sha_and_dataset(self):
        self.assert_refused("spark_profile", spark_profile="fast")
        self.assert_refused("git_sha", git_sha="abc")
        self.assert_refused("approved campaign dataset", dataset_version="rehearsal-9gb-v1")

    def test_run_ids_are_bound_to_benchmark_profile_and_stage(self):
        self.assert_refused("", bronze_run_id=run_id("optimized", "bronze"))
        self.assert_refused("", silver_run_id=run_id("baseline", "gold"))
        self.assert_refused("", gold_run_id=run_id("baseline", "gold", "ab-20261110000001"))
        self.assert_refused("", feature_run_id="not-a-run")
        self.assert_refused("", pipeline="dp2", bronze_run_id=run_id("optimized", "bronze"))

    def test_a_rehearsal_in_the_canonical_bucket_is_refused(self):
        self.assert_refused("must use bucket", data_bucket=CANONICAL_DATA_BUCKET)

    def test_canonical_dataset_runs_only_in_its_contract(self):
        parse_parameters(values(dataset_version=CANONICAL_100GB, data_bucket=CANONICAL_DATA_BUCKET), pipeline="dp3")

    def test_unsafe_strings_are_refused(self):
        for name, value in (("data_bucket", "b;rm -rf /"), ("project_id", "p$(id)"), ("region", "asia`x`"),
                            ("terraform_output_json", "/tmp/a b.json"), ("source_manifest_sha256", "x" * 64)):
            with self.subTest(name):
                self.assert_refused("unsafe or invalid", **{name: value})

    def test_ttl_is_pinned(self):
        self.assert_refused("dataproc_ttl", dataproc_ttl="1h")

    def test_missing_parameter_is_refused(self):
        supplied = values(pipeline="dp2")
        del supplied["bronze_run_id"]
        with self.assertRaisesRegex(CampaignError, "missing DP2 parameters"):
            parse_parameters(supplied, pipeline="dp2")

    def test_only_the_exact_approval_token_is_accepted(self):
        token = values()["execution_approval"]
        self.assertTrue(token.startswith("EXECUTE-PAID-DATAPROC:DP3:"))
        for bad in ("NOT_APPROVED", "", "I_APPROVE_DP2_PAID_JOBS", token + "x", token.replace("baseline", "optimized")):
            with self.subTest(bad):
                self.assert_refused("approval", execution_approval=bad)

    def test_approval_for_one_profile_does_not_cover_the_other(self):
        baseline_token = values("baseline")["execution_approval"]
        with self.assertRaisesRegex(CampaignError, "approval"):
            parse_parameters(values("optimized", execution_approval=baseline_token), pipeline="dp3")

    def test_approval_for_one_pipeline_does_not_cover_another(self):
        dp1_token = values(pipeline="dp1")["execution_approval"]
        self.assertIn(":DP1:", dp1_token)
        with self.assertRaisesRegex(CampaignError, "approval"):
            parse_parameters(values(pipeline="dp2", execution_approval=dp1_token), pipeline="dp2")

    def test_read_only_tooling_can_parse_without_approval(self):
        parse_parameters(values(execution_approval="NOT_APPROVED"), pipeline="dp3", require_approval=False)

    def test_environment_parameters_and_attempt(self):
        environ = {f"EDAI1_CAMPAIGN_{k.upper()}": v for k, v in values(pipeline="dp2").items()}
        with self.assertRaisesRegex(CampaignError, "pipeline"):
            campaign_task.parameters_from_environment(environ)
        environ["EDAI1_CAMPAIGN_PIPELINE"] = "dp2"
        params = campaign_task.parameters_from_environment(environ)
        self.assertEqual(("baseline", "dp2"), (params.spark_profile, params.pipeline))
        with self.assertRaises(CampaignError):
            campaign_task.attempt_from_environment({})
        self.assertEqual(3, campaign_task.attempt_from_environment({"EDAI1_CAMPAIGN_ATTEMPT": "3"}))

    def test_unapproved_task_exits_nonzero_without_calling_anything(self):
        for pipeline in PIPELINES:
            environ = {f"EDAI1_CAMPAIGN_{k.upper()}": v
                       for k, v in values(pipeline=pipeline, execution_approval="NOT_APPROVED").items()}
            environ.update(EDAI1_CAMPAIGN_ATTEMPT="1", EDAI1_CAMPAIGN_PIPELINE=pipeline)
            runner = RecordingRunner()
            for action in actions_for(pipeline):
                with self.subTest(pipeline=pipeline, action=action):
                    self.assertEqual(1, campaign_task.main([action], environ=environ, runner=runner))
            self.assertEqual([], runner.calls)

    def test_publication_requires_the_dagrun_identity_of_its_own_dag(self):
        params = parse_parameters(values(pipeline="dp1"), pipeline="dp1")
        with self.assertRaisesRegex(CampaignError, "publishing DAG"):
            campaign_task.producer_from_environment(params, {"EDAI1_CAMPAIGN_DAG_ID": "dp2_bronze_to_curated",
                                                             "EDAI1_CAMPAIGN_DAG_RUN_ID": "x"})
        with self.assertRaisesRegex(CampaignError, "DagRun ID"):
            campaign_task.producer_from_environment(params, {"EDAI1_CAMPAIGN_DAG_ID": "dp1_source_to_bronze",
                                                             "EDAI1_CAMPAIGN_DAG_RUN_ID": "a b"})


class PipelineActionTest(unittest.TestCase):
    def test_actions_are_scoped_to_their_pipeline(self):
        self.assertEqual(
            ("preflight", "verify-source", "quota-bronze", "submit-bronze", "physical-bronze", "validate-bronze",
             "publish-handoff"), actions_for("dp1"))
        self.assertEqual(
            ("preflight", "verify-upstream-handoff", "verify-upstream-inputs",
             "quota-silver", "submit-silver", "physical-silver", "validate-silver",
             "quota-gold", "submit-gold", "physical-gold", "validate-gold", "publish-handoff"), actions_for("dp2"))
        self.assertEqual(
            ("preflight", "verify-upstream-handoff", "verify-upstream-inputs", "quota-features", "submit-features",
             "physical-features", "validate-features", "publish-handoff"), actions_for("dp3"))
        for pipeline, foreign in (("dp1", "submit-silver"), ("dp2", "submit-bronze"), ("dp2", "submit-features"),
                                  ("dp3", "submit-gold"), ("dp1", "verify-upstream-handoff")):
            with self.subTest(pipeline=pipeline, action=foreign), self.assertRaisesRegex(CampaignError, "not part of"):
                run_action(foreign, parse_parameters(values(pipeline=pipeline), pipeline=pipeline),
                           attempt=1, runner=RecordingRunner())

    def test_the_runner_has_no_winner_selection(self):
        text = (campaign_task.REPOSITORY_ROOT / "ops/gcp/campaign_task.py").read_text(encoding="utf-8").lower()
        for forbidden in ("pair_manifest", "dp2_method2", "select_stage_winner", "_winner", "winner_"):
            self.assertNotIn(forbidden, text)


class SubmissionCommandTest(TerraformFixture):
    def run_submit(self, stage: str, profile: str = "baseline") -> list[str]:
        runner = self.completed_runner(profile, upto={"bronze": "dp1", "silver": "dp1", "gold": "dp1",
                                                      "features": "dp2"}[stage])
        runner.existing_batches -= {run_id(profile, {"features": "feat"}.get(stage, stage))}
        before = len(self.submits(runner))
        run_action(f"submit-{stage}", self.stage_params(stage, profile), attempt=1, runner=runner)
        submits = self.submits(runner)[before:]
        self.assertEqual(1, len(submits))
        return submits[0]

    def test_each_stage_reads_only_its_own_profile_upstream(self):
        for profile in ("baseline", "optimized"):
            with self.subTest(profile):
                silver = self.run_submit("silver", profile)
                self.assertIn(f"--bronze-root=gs://{BUCKET}/lakehouse/runs/{run_id(profile, 'bronze')}/bronze", silver)
                gold = self.run_submit("gold", profile)
                self.assertIn(f"--silver-root=gs://{BUCKET}/lakehouse/runs/{run_id(profile, 'silver')}/silver", gold)
                features = self.run_submit("features", profile)
                self.assertIn(f"--silver-root=gs://{BUCKET}/lakehouse/runs/{run_id(profile, 'silver')}/silver", features)
                self.assertIn(f"--source-core-gold-root=gs://{BUCKET}/lakehouse/runs/{run_id(profile, 'gold')}/gold", features)
                other = "optimized" if profile == "baseline" else "baseline"
                for command in (silver, gold, features):
                    self.assertFalse([item for item in command if f"-{other}-" in item])

    def test_rehearsal_stages_carry_the_extended_inventory(self):
        bronze = self.run_submit("bronze")
        self.assertIn(f"--source-manifest-uri=gs://{BUCKET}/source/{REHEARSAL_5GB}/_control/dataset_manifest.json", bronze)
        silver = self.run_submit("silver")
        self.assertIn("--include-merchant-history", silver)
        self.assertIn("--expected-table-count=9", silver)
        gold = self.run_submit("gold")
        self.assertIn("--include-merchant-scd2", gold)
        self.assertIn("--expected-table-count=12", gold)

    def test_profile_properties_are_the_existing_contract(self):
        properties = {"baseline": "spark.sql.adaptive.enabled=false", "optimized": "spark.sql.adaptive.enabled=true"}
        for profile, expected in properties.items():
            with self.subTest(profile):
                gold = self.run_submit("gold", profile)
                joined = next(item for item in gold if item.startswith("--properties="))
                self.assertIn(expected, joined)
                self.assertIn(f"spark.eventLog.dir=gs://{BUCKET}/spark-events", joined)
                self.assertNotIn("spark.ewallet.method2", joined)
        self.assertNotIn("winner", " ".join(self.run_submit("silver")).lower())

    def test_validator_commands_use_explicit_roots_and_never_defaults(self):
        expected = {
            "bronze": ("validate_bronze.py", ["--include-fraud-labels", "--include-merchant-history"], ["--bronze-root="]),
            "silver": ("validate_silver.py", ["--include-fraud-labels", "--include-merchant-history"],
                       ["--bronze-root=", "--silver-root="]),
            "gold": ("validate_gold.py", ["--scope=core-gold", "--include-merchant-scd2"], ["--silver-root=", "--gold-root="]),
            "features": ("validate_gold.py", ["--scope=features", "--pit-sample-modulus=1000"],
                         ["--silver-root=", "--gold-root=", "--feature-gold-root="]),
        }
        for profile in ("baseline", "optimized"):
            for stage, (script, flags, roots) in expected.items():
                with self.subTest(profile=profile, stage=stage):
                    runner = RecordingRunner()
                    run_action(f"validate-{stage}", self.stage_params(stage, profile), attempt=2, runner=runner)
                    command = self.submits(runner)[0]
                    self.assertTrue(command[5].endswith(script))
                    for flag in flags:
                        self.assertIn(flag, command)
                    for root in roots:
                        value = next(item for item in command if item.startswith(root))
                        self.assertIn(f"/lakehouse/runs/ab-20261110000000-{profile}-", value)
                    self.assertIn("--environment=gcp", command)
                    batch = next(item for item in command if item.startswith("--batch=")).split("=", 1)[1]
                    self.assertTrue(batch.startswith("v2-"))
                    self.assertLessEqual(len(batch), 50)
                    report = next(item for item in command if item.startswith("--report-uri="))
                    self.assertIn("/evidence/validation-", report)
                    self.assertTrue(report.endswith("-a2.json"))

    def test_a_campaign_submission_rejects_a_pair_manifest_and_cross_profile_upstream(self):
        runtime = submit_source_to_bronze.apply_dataset_bucket(
            submit_source_to_bronze.load_cloud_runtime(terraform_outputs()), REHEARSAL_5GB)
        with self.assertRaisesRegex(ValueError, "no pair manifest"):
            submit_source_to_bronze.build_submission_command(
                runtime, dataset_version=REHEARSAL_5GB, run_id=run_id("baseline", "bronze"), ttl="8h",
                spark_profile="baseline", benchmark_id=BENCH, git_sha=SHA, pair_manifest={}, campaign=True)
        with self.assertRaises(ValueError):
            submit_bronze_to_silver.build_submission_command(
                runtime, dataset_version=REHEARSAL_5GB, source_bronze_run_id=run_id("optimized", "bronze"),
                run_id=run_id("baseline", "silver"), ttl="8h", spark_profile="baseline", benchmark_id=BENCH,
                git_sha=SHA, campaign=True)


class FailClosedTest(TerraformFixture):
    def test_preflight_passes_with_clean_identity_and_absent_outputs(self):
        for pipeline in PIPELINES:
            with self.subTest(pipeline):
                run_action("preflight", self.params(pipeline=pipeline), attempt=1, runner=RecordingRunner())

    def test_preflight_refuses_wrong_or_dirty_code(self):
        for pipeline in PIPELINES:
            with self.subTest(pipeline):
                with self.assertRaises(ValueError):
                    run_action("preflight", self.params(pipeline=pipeline), attempt=1,
                               runner=RecordingRunner(git_sha="b" * 40))
                with self.assertRaises(ValueError):
                    run_action("preflight", self.params(pipeline=pipeline), attempt=1,
                               runner=RecordingRunner(dirty=" M file\n"))

    def test_preflight_refuses_its_own_existing_outputs_or_batches(self):
        cases = (("dp1", "bronze"), ("dp2", "silver"), ("dp2", "gold"), ("dp3", "feat"))
        for pipeline, alias in cases:
            with self.subTest(pipeline=pipeline, stage=alias):
                with self.assertRaisesRegex(ValueError, "immutable"):
                    run_action("preflight", self.params(pipeline=pipeline), attempt=1,
                               runner=RecordingRunner(existing_prefixes=[run_id("baseline", alias)]))
                with self.assertRaises(ValueError):
                    run_action("preflight", self.params(pipeline=pipeline), attempt=1,
                               runner=RecordingRunner(existing_batches=[run_id("baseline", alias)]))

    def test_preflight_accepts_existing_valid_upstream_outputs(self):
        """DP2 is not rejected because Bronze exists; DP3 is not rejected because Silver/Gold exist."""
        upstream = {"dp2": ["bronze"], "dp3": ["bronze", "silver", "gold"]}
        for pipeline, aliases in upstream.items():
            with self.subTest(pipeline):
                runner = RecordingRunner(existing_prefixes=[run_id("baseline", alias) for alias in aliases],
                                         existing_batches=[run_id("baseline", alias) for alias in aliases])
                run_action("preflight", self.params(pipeline=pipeline), attempt=1, runner=runner)

    def test_preflight_refuses_an_already_published_handoff(self):
        runner = self.completed_runner(upto="dp1")
        with self.assertRaisesRegex(CampaignError, "already exists and is immutable"):
            run_action("preflight", self.params(pipeline="dp1"), attempt=1,
                       runner=RecordingRunner(objects=runner.objects))

    def test_preflight_refuses_a_deployment_other_than_the_dagrun(self):
        self.outputs.write_text(json.dumps(terraform_outputs(project="other-project-id")), encoding="utf-8")
        with self.assertRaisesRegex(CampaignError, "differs from the DagRun"):
            run_action("preflight", self.params(), attempt=1, runner=RecordingRunner())

    def test_preflight_fails_closed_when_the_rehearsal_bucket_is_missing(self):
        with self.assertRaisesRegex(CampaignError, "GCS_PUBLICATION_BLOCKED_BY_BUCKET_PROVISIONING"):
            run_action("preflight", self.params(), attempt=1, runner=RecordingRunner(bucket_exists=False))

    def test_quota_failure_stops_the_stage(self):
        self.quota.return_value = 1
        with self.assertRaisesRegex(CampaignError, "quota"):
            run_action("quota-silver", self.params(pipeline="dp2"), attempt=1, runner=RecordingRunner())

    def test_quota_names_the_predecessor_batch_of_the_same_profile(self):
        run_action("quota-gold", self.params("optimized", "dp2"), attempt=1, runner=RecordingRunner())
        arguments = self.quota.call_args.args[0]
        self.assertEqual(run_id("optimized", "silver"), arguments[arguments.index("--predecessor-batch-id") + 1])
        self.assertEqual("silver-to-gold", arguments[arguments.index("--handoff") + 1])

    def test_transformation_refuses_an_existing_output_prefix(self):
        runner = RecordingRunner(existing_prefixes=[run_id("baseline", "bronze")])
        with self.assertRaisesRegex(ValueError, "immutable"):
            run_action("submit-bronze", self.params(pipeline="dp1"), attempt=1, runner=runner)
        self.assertEqual([], self.submits(runner))

    def test_dataproc_submission_failure_propagates(self):
        for stage in ("bronze", "silver", "gold", "features"):
            with self.subTest(stage), self.assertRaises(subprocess.CalledProcessError):
                runner = self.completed_runner(upto="dp2")
                runner.existing_batches.clear()
                runner.submit_fails = True
                run_action(f"submit-{stage}", self.stage_params(stage), attempt=1, runner=runner)

    def test_validation_batch_failure_propagates(self):
        for stage in ("bronze", "silver", "gold", "features"):
            with self.subTest(stage), self.assertRaises(subprocess.CalledProcessError):
                run_action(f"validate-{stage}", self.stage_params(stage), attempt=1, runner=RecordingRunner(submit_fails=True))

    def test_a_failing_report_fails_the_task_even_if_the_batch_succeeded(self):
        for stage in ("bronze", "silver", "gold", "features"):
            with self.subTest(stage), self.assertRaisesRegex(ValueError, "failed"):
                run_action(f"validate-{stage}", self.stage_params(stage), attempt=1,
                           runner=RecordingRunner(report_status="FAIL"))

    def test_gold_never_starts_before_silver_validation_passed(self):
        runner = self.completed_runner(upto="dp1", failing_scopes={"silver"})
        with self.assertRaisesRegex(ValueError, "failed"):
            run_action("submit-gold", self.params(pipeline="dp2"), attempt=1, runner=runner)
        self.assertFalse([call for call in self.submits(runner) if "-gold-" in " ".join(call)])

    def test_a_report_for_another_run_is_refused(self):
        report = {"scope": "silver", "run_id": "other", "dataset_version": REHEARSAL_5GB, "status": "PASS", "results": [1]}
        with self.assertRaisesRegex(ValueError, "run_id"):
            submit_stage_validation.require_passing_report(report, scope="silver", run_id="mine", dataset_version=REHEARSAL_5GB)

    def test_an_empty_report_is_refused(self):
        report = {"scope": "silver", "run_id": "mine", "dataset_version": REHEARSAL_5GB, "status": "PASS", "results": []}
        with self.assertRaisesRegex(ValueError, "no rules"):
            submit_stage_validation.require_passing_report(report, scope="silver", run_id="mine", dataset_version=REHEARSAL_5GB)

    def test_an_existing_validation_batch_or_report_blocks_a_rerun(self):
        batch = submit_stage_validation.validation_batch_id(run_id("baseline", "gold"), 1)
        runner = RecordingRunner(existing_batches=[batch])
        with self.assertRaises(ValueError):
            run_action("validate-gold", self.params(pipeline="dp2"), attempt=1, runner=runner)
        self.assertEqual([], self.submits(runner))
        self.assertRaises(ValueError, submit_stage_validation.validation_batch_id, "x", 0)

    def test_physical_validation_requires_the_exact_inventory(self):
        gold = {"dim_user", "dim_account", "dim_merchant", "dim_device", "dim_date", "fact_transactions",
                "fact_login_events", "fact_balance_snapshot", "obt_transaction_enriched", "feat_user_90d",
                "opt_merchant_performance", "dim_merchant_scd2"}
        run_action("physical-gold", self.params(pipeline="dp2"), attempt=1, runner=RecordingRunner(tables=gold))
        for tables in (gold - {"dim_merchant_scd2"}, gold | {"dim_extra"}):
            with self.subTest(len(tables)), self.assertRaisesRegex(ValueError, "mismatch"):
                run_action("physical-gold", self.params(pipeline="dp2"), attempt=1, runner=RecordingRunner(tables=tables))

    def test_a_failed_transformation_keeps_a_provenance_record(self):
        runner = self.completed_runner(upto="dp1")
        runner.existing_batches.discard(run_id("baseline", "gold"))
        runner.submit_fails = True
        written = len(runner.written)
        with self.assertRaises(subprocess.CalledProcessError):
            run_action("submit-gold", self.params(pipeline="dp2"), attempt=1, runner=runner)
        run = run_id("baseline", "gold")
        uri = f"gs://{BUCKET}/lakehouse/runs/{run}/evidence/execution_provenance-{run}.json"
        self.assertEqual([uri], runner.written[written:])
        self.assertEqual("transformation", json.loads(runner.bodies[uri])["kind"])

    def test_a_submission_that_never_started_leaves_no_record(self):
        runner = RecordingRunner(existing_prefixes=[run_id("baseline", "bronze")])
        with self.assertRaisesRegex(ValueError, "immutable"):
            run_action("submit-bronze", self.params(pipeline="dp1"), attempt=1, runner=runner)
        self.assertEqual([], runner.written)

    def test_a_validator_batch_failure_keeps_its_attempt_record(self):
        runner = RecordingRunner(submit_fails=True)
        with self.assertRaises(subprocess.CalledProcessError):
            run_action("validate-silver", self.params(pipeline="dp2"), attempt=1, runner=runner)
        run = run_id("baseline", "silver")
        batch = submit_stage_validation.validation_batch_id(run, 1)
        self.assertEqual([f"gs://{BUCKET}/lakehouse/runs/{run}/evidence/execution_provenance-{batch}.json"], runner.written)

    def test_every_stage_submits_with_the_rehearsal_bucket_and_execution_record(self):
        for action in ("submit-bronze", "submit-silver", "submit-gold", "submit-features",
                       "validate-bronze", "validate-silver", "validate-gold", "validate-features"):
            with self.subTest(action):
                stage = action.split("-", 1)[1]
                runner = self.completed_runner(upto="dp2") if action.startswith("submit") else RecordingRunner()
                if action.startswith("submit"):
                    runner.existing_batches.clear()
                before = len(self.submits(runner))
                run_action(action, self.stage_params(stage), attempt=1, runner=runner)
                command = " ".join(self.submits(runner)[before])
                self.assertIn(f"spark.eventLog.dir=gs://{BUCKET}/spark-events", command)
                self.assertIn(f"--execution-record-uri=gs://{BUCKET}/lakehouse/runs/", command)
                self.assertNotIn(CANONICAL_DATA_BUCKET, command)


class CampaignManifestTest(TerraformFixture):
    def test_dp3_publishes_a_complete_comparator_compatible_manifest(self):
        runner = self.completed_runner(upto="dp3")
        params = self.params(pipeline="dp3")
        manifest = json.loads(runner.objects[campaign_task.campaign_manifest_uri(params)])
        self.assertEqual("edai1-pipeline-campaign-v2", manifest["schema_version"])
        self.assertEqual("COMPLETE", manifest["status"])
        self.assertEqual(BUCKET, manifest["storage"]["data_bucket"])
        self.assertEqual(["source", "bronze", "silver", "gold", "features"], manifest["lineage"])
        self.assertEqual({"dp1", "dp2"}, set(manifest["handoffs"]))
        for stage, alias in (("bronze", "bronze"), ("silver", "silver"), ("gold", "gold"), ("features", "feat")):
            with self.subTest(stage):
                entry = manifest["stages"][stage]
                execution = entry["execution_metadata"]
                self.assertEqual(run_id("baseline", alias), execution["dataproc_batch_id"])
                self.assertEqual("app-1", execution["spark_application_id"])
                self.assertEqual("transformation", execution["kind"])
                self.assertEqual(f"gs://{BUCKET}/spark-events/app-1", execution["spark_event_log_uri"])
                self.assertEqual("validation", entry["validation"]["execution_metadata"]["kind"])
                self.assertEqual("PASS", entry["validation"]["status"])
                self.assertEqual("PASS", entry["validation"]["execution_metadata"]["validation_status"])

    def test_the_comparator_accepts_two_completed_profiles(self):
        from ops.gcp.compare_campaign_outputs import manifest_problems
        manifests = {}
        for profile in ("baseline", "optimized"):
            runner = self.completed_runner(profile, upto="dp3")
            manifests[profile] = json.loads(runner.objects[campaign_task.campaign_manifest_uri(self.params(profile))])
        self.assertEqual([], manifest_problems(manifests["baseline"], manifests["optimized"]))
        self.assertNotEqual(manifests["baseline"]["stages"]["silver"]["output_root"],
                            manifests["optimized"]["stages"]["silver"]["output_root"])

    def test_dp3_publication_requires_every_batch_to_have_succeeded(self):
        runner = self.completed_runner(upto="dp2")
        runner.batch_state = "FAILED"
        with self.assertRaisesRegex(CampaignError, "SUCCEEDED|refused"):
            run_action("publish-handoff", self.params(), attempt=1, runner=runner, producer=producer("dp3"))
        self.assertNotIn(campaign_task.campaign_manifest_uri(self.params()), runner.objects)

    def test_no_handoff_is_published_after_a_failed_validation(self):
        for pipeline, scope in (("dp1", "bronze"), ("dp2", "core-gold"), ("dp3", "features")):
            with self.subTest(pipeline):
                upstream = {"dp1": None, "dp2": "dp1", "dp3": "dp2"}[pipeline]
                runner = self.completed_runner(upto=upstream) if upstream else RecordingRunner(
                    existing_batches=finished_batches("baseline"))
                runner.failing_scopes = {scope}
                with self.assertRaisesRegex(ValueError, "failed"):
                    run_action("publish-handoff", self.params(pipeline=pipeline), attempt=1, runner=runner,
                               producer=producer(pipeline))
                self.assertNotIn(campaign_task.handoff_uri(self.params(pipeline=pipeline), pipeline), runner.objects)


if __name__ == "__main__":
    unittest.main()
