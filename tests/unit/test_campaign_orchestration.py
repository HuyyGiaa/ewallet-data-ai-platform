"""Campaign tasks: parameters, approval, command construction and fail-closed gates.

No test reaches gcloud: a recording runner stands in for every subprocess call.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from data_platform.config.dataset_identity import (
    APPROVED_DATASETS,
    CANONICAL_100GB,
    CANONICAL_DATA_BUCKET,
    REHEARSAL_5GB,
    REHEARSAL_DATA_BUCKET,
)
from ops.gcp import (
    campaign_task,
    submit_bronze_to_silver,
    submit_historical_features,
    submit_silver_to_gold,
    submit_source_to_bronze,
    submit_stage_validation,
    wait_for_dataproc_quota,
)
from ops.gcp.campaign_task import CampaignError, parse_parameters, run_action


BUCKET = REHEARSAL_DATA_BUCKET
SHA = "a" * 40
BENCH = "ab-20261110000000"
ATTEMPT_SUFFIX = {"bronze": "r261110000000", "silver": "r261110000001", "gold": "r261110000002", "feat": "r261110000003"}


def run_id(profile: str, stage: str, benchmark: str = BENCH) -> str:
    return f"{benchmark}-{profile}-{stage}-{ATTEMPT_SUFFIX[stage]}"


def values(profile: str = "baseline", **overrides: str) -> dict[str, str]:
    result = {
        "dataset_version": REHEARSAL_5GB, "benchmark_id": BENCH, "spark_profile": profile, "git_sha": SHA,
        "source_manifest_sha256": "b" * 64,
        "bronze_run_id": run_id(profile, "bronze"), "silver_run_id": run_id(profile, "silver"),
        "gold_run_id": run_id(profile, "gold"), "feature_run_id": run_id(profile, "feat"),
        "project_id": "ewallet-data-ai-platform-hg26", "region": "asia-southeast1", "data_bucket": BUCKET,
        "dataproc_ttl": "8h", "terraform_output_json": "", "execution_approval": "",
    }
    result.update(overrides)
    if "execution_approval" not in overrides:
        result["execution_approval"] = campaign_task.expected_approval(result)
    return result


def terraform_outputs(bucket: str = CANONICAL_DATA_BUCKET, project: str = "ewallet-data-ai-platform-hg26") -> dict[str, dict[str, object]]:
    return {
        "project_id": {"value": project},
        "region": {"value": "asia-southeast1"},
        "data_bucket_name": {"value": bucket},
        "temporary_bucket_name": {"value": "temp-bucket"},
        "service_account_emails": {"value": {"spark": "spark@example.com"}},
        "dataproc_batch_runtime": {"value": {
            "project_id": project, "region": "asia-southeast1",
            "staging_bucket": "temp-bucket", "service_account_email": "spark@example.com",
            "execution_mode": "serverless-batch", "performance_tier": "standard", "subnet": "subnet-1",
        }},
    }


class RecordingRunner:
    """Answers the gcloud/git calls the campaign makes; everything else is an error."""

    def __init__(self, *, git_sha: str = SHA, dirty: str = "", existing_prefixes=(), existing_batches=(),
                 report_status: str = "PASS", submit_fails: bool = False, tables: set[str] | None = None,
                 batch_state: str = "SUCCEEDED", bucket_exists: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.git_sha, self.dirty = git_sha, dirty
        self.existing_prefixes, self.existing_batches = tuple(existing_prefixes), set(existing_batches)
        self.report_status, self.submit_fails, self.tables = report_status, submit_fails, tables
        self.batch_state = batch_state
        self.bucket_exists = bucket_exists
        self.written: list[str] = []
        self.bodies: dict[str, str] = {}

    @staticmethod
    def _done(returncode: int = 0, stdout: str = "", stderr: str = ""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, command, **kwargs):
        command = [str(item) for item in command]
        self.calls.append(command)
        if command[:2] == ["git", "rev-parse"]:
            return self._done(stdout=self.git_sha + "\n")
        if command[:2] == ["git", "status"]:
            return self._done(stdout=self.dirty)
        if command[:4] == ["gcloud", "dataproc", "batches", "describe"]:
            batch = command[4]
            if batch in self.existing_batches:
                return self._done(stdout=json.dumps({
                    "state": self.batch_state, "uuid": batch, "createTime": "2026-11-10T00:00:00Z",
                    "stateTime": "2026-11-10T00:10:00Z",
                    "labels": {"benchmark_id": BENCH, "spark_profile": batch.split("-")[2], "git_sha": SHA},
                }))
            return self._done(1, stderr="NOT_FOUND: batch")
        if command[:4] == ["gcloud", "dataproc", "batches", "submit"]:
            batch = next(item.split("=", 1)[1] for item in command if item.startswith("--batch="))
            self.existing_batches.add(batch)
            if self.submit_fails:
                if kwargs.get("check"):
                    raise subprocess.CalledProcessError(1, command)
                return self._done(1, stderr="batch failed")
            return self._done()
        if command[:4] == ["gcloud", "storage", "buckets", "describe"]:
            if not self.bucket_exists:
                return self._done(1, stderr="not found: 404")
            return self._done(stdout=json.dumps({
                "location": "ASIA-SOUTHEAST1", "uniform_bucket_level_access": True,
                "public_access_prevention": "enforced", "versioning_enabled": True}))
        if command[:4] == ["gcloud", "storage", "buckets", "get-iam-policy"]:
            return self._done(stdout=json.dumps({"bindings": [
                {"role": "roles/storage.objectUser", "members": ["serviceAccount:spark@example.com"]}]}))
        if command[:3] == ["gcloud", "storage", "cat"]:
            if "spark_execution-" in command[3]:
                run = command[3].split("/runs/", 1)[1].split("/", 1)[0]
                return self._done(stdout=json.dumps({
                    "run_id": run, "dataset_version": REHEARSAL_5GB, "spark_application_id": "app-1",
                    "spark_event_log_dir": f"gs://{BUCKET}/spark-events"}))
            return self._done(stdout=json.dumps(self.report(command[3])))
        if command[:3] == ["gcloud", "storage", "ls"]:
            return self.listing(command[3])
        if command[:3] == ["gcloud", "storage", "cp"]:
            self.written.append(command[4])
            self.bodies[command[4]] = Path(command[3]).read_text(encoding="utf-8")
            return self._done()
        raise AssertionError(f"unexpected command: {command}")

    def report(self, uri: str) -> dict[str, object]:
        scope = uri.rsplit("validation-", 1)[1].rsplit("-a", 1)[0]
        run = uri.split("/runs/", 1)[1].split("/", 1)[0]
        return {"scope": scope, "run_id": run, "dataset_version": REHEARSAL_5GB, "status": self.report_status,
                "failed_rules": ["x"] if self.report_status != "PASS" else [],
                "results": [{"rule_name": "x", "passed": self.report_status == "PASS"}]}

    def listing(self, uri: str):
        if "/spark-events/" in uri:
            return self._done(stdout=uri.rstrip("*") + "\n")
        if uri.endswith("/_delta_log/*.json"):
            return self._done(stdout=uri.replace("*.json", "0.json") + "\n")
        if uri.endswith("-a*.json"):
            return self._done(stdout=uri.replace("*.json", "1.json") + "\n")
        if uri.endswith("/**"):
            if any(prefix in uri for prefix in self.existing_prefixes):
                return self._done(stdout=uri + "x\n")
            return self._done(1, stderr="ERROR: One or more URLs matched no objects.")
        if uri.endswith(".json"):  # a report that must be absent
            return self._done(1, stderr="ERROR: One or more URLs matched no objects.")
        # A run root: return the expected table directories unless told otherwise.
        return self._done(stdout="\n".join(f"{uri}{table}/" for table in sorted(self.tables or set())))


class TerraformFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.outputs = Path(directory.name) / "terraform.json"
        self.outputs.write_text(json.dumps(terraform_outputs()), encoding="utf-8")
        for module in (submit_source_to_bronze, submit_bronze_to_silver, submit_silver_to_gold,
                       submit_historical_features, submit_stage_validation):
            for name, value in (("ensure_delta_dependencies", lambda: ()), ("build_python_archive", lambda *a: Path("job.zip"))):
                patcher = patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        quota = patch.object(wait_for_dataproc_quota, "main", return_value=0)
        self.quota = quota.start()
        self.addCleanup(quota.stop)

    def params(self, profile: str = "baseline", **overrides: str):
        return parse_parameters(values(profile, terraform_output_json=str(self.outputs), **overrides))

    def submits(self, runner: RecordingRunner) -> list[list[str]]:
        return [call for call in runner.calls if call[:4] == ["gcloud", "dataproc", "batches", "submit"]]


class ParameterTest(unittest.TestCase):
    def assert_refused(self, message: str, **overrides: str) -> None:
        with self.assertRaisesRegex(CampaignError, message):
            parse_parameters(values(**overrides))

    def test_valid_parameters_for_both_profiles(self):
        for profile in ("baseline", "optimized"):
            with self.subTest(profile):
                params = parse_parameters(values(profile))
                self.assertEqual(profile, params.spark_profile)
                self.assertEqual(f"gs://{BUCKET}/lakehouse/runs/{params.silver_run_id}/silver",
                                 params.run_root("silver"))

    def test_a_future_registered_dataset_version_is_accepted(self):
        future = "final-100gb-v2"
        identity = replace(APPROVED_DATASETS[REHEARSAL_5GB], version=future)
        with patch.dict(APPROVED_DATASETS, {future: identity}):
            parse_parameters(values(dataset_version=future))

    def test_invalid_profile_sha_and_dataset(self):
        self.assert_refused("spark_profile", spark_profile="fast")
        self.assert_refused("git_sha", git_sha="abc")
        self.assert_refused("approved campaign dataset", dataset_version="rehearsal-9gb-v1")

    def test_run_ids_are_bound_to_benchmark_profile_and_stage(self):
        self.assert_refused("", bronze_run_id=run_id("optimized", "bronze"))
        self.assert_refused("", silver_run_id=run_id("baseline", "gold"))
        self.assert_refused("", gold_run_id=run_id("baseline", "gold", "ab-20261110000001"))
        self.assert_refused("", feature_run_id="not-a-run")

    def test_a_rehearsal_in_the_canonical_bucket_is_refused(self):
        self.assert_refused("must use bucket", data_bucket=CANONICAL_DATA_BUCKET)

    def test_canonical_dataset_runs_only_in_its_contract(self):
        parse_parameters(values(dataset_version=CANONICAL_100GB, data_bucket=CANONICAL_DATA_BUCKET))

    def test_unsafe_strings_are_refused(self):
        for name, value in (("data_bucket", "b;rm -rf /"), ("project_id", "p$(id)"), ("region", "asia`x`"),
                            ("terraform_output_json", "/tmp/a b.json"), ("source_manifest_sha256", "x" * 64)):
            with self.subTest(name):
                self.assert_refused("unsafe or invalid", **{name: value})

    def test_ttl_is_pinned(self):
        self.assert_refused("dataproc_ttl", dataproc_ttl="1h")

    def test_missing_parameter_is_refused(self):
        supplied = values()
        del supplied["bronze_run_id"]
        with self.assertRaisesRegex(CampaignError, "missing campaign parameters"):
            parse_parameters(supplied)

    def test_only_the_exact_approval_token_is_accepted(self):
        token = values()["execution_approval"]
        self.assertTrue(token.startswith("EXECUTE-PAID-DATAPROC:"))
        for bad in ("NOT_APPROVED", "", "I_APPROVE_DP2_PAID_JOBS", token + "x", token.replace("baseline", "optimized")):
            with self.subTest(bad):
                self.assert_refused("approval", execution_approval=bad)

    def test_approval_for_one_profile_does_not_cover_the_other(self):
        baseline_token = values("baseline")["execution_approval"]
        with self.assertRaisesRegex(CampaignError, "approval"):
            parse_parameters(values("optimized", execution_approval=baseline_token))

    def test_environment_parameters_and_attempt(self):
        environ = {f"EDAI1_CAMPAIGN_{k.upper()}": v for k, v in values().items()}
        self.assertEqual("baseline", campaign_task.parameters_from_environment(environ).spark_profile)
        with self.assertRaises(CampaignError):
            campaign_task.attempt_from_environment({})
        self.assertEqual(3, campaign_task.attempt_from_environment({"EDAI1_CAMPAIGN_ATTEMPT": "3"}))

    def test_unapproved_task_exits_nonzero_without_calling_anything(self):
        environ = {f"EDAI1_CAMPAIGN_{k.upper()}": v for k, v in values(execution_approval="NOT_APPROVED").items()}
        environ["EDAI1_CAMPAIGN_ATTEMPT"] = "1"
        runner = RecordingRunner()
        for action in campaign_task.ACTIONS:
            with self.subTest(action):
                self.assertEqual(1, campaign_task.main([action], environ=environ, runner=runner))
        self.assertEqual([], runner.calls)


class SubmissionCommandTest(TerraformFixture):
    def run_submit(self, stage: str, profile: str = "baseline") -> list[str]:
        runner = RecordingRunner()
        run_action(f"submit-{stage}", self.params(profile), attempt=1, runner=runner)
        submits = self.submits(runner)
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
        for stage, (script, flags, roots) in expected.items():
            with self.subTest(stage):
                runner = RecordingRunner()
                run_action(f"validate-{stage}", self.params(), attempt=2, runner=runner)
                command = self.submits(runner)[0]
                self.assertTrue(command[5].endswith(script))
                for flag in flags:
                    self.assertIn(flag, command)
                for root in roots:
                    value = next(item for item in command if item.startswith(root))
                    self.assertIn("/lakehouse/runs/ab-", value)
                self.assertIn("--environment=gcp", command)
                batch = next(item for item in command if item.startswith("--batch="))
                self.assertTrue(batch.endswith("-v2"))
                self.assertLessEqual(len(batch.split("=", 1)[1]), 50)
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
        run_action("preflight", self.params(), attempt=1, runner=RecordingRunner())

    def test_preflight_refuses_wrong_or_dirty_code(self):
        with self.assertRaises(ValueError):
            run_action("preflight", self.params(), attempt=1, runner=RecordingRunner(git_sha="b" * 40))
        with self.assertRaises(ValueError):
            run_action("preflight", self.params(), attempt=1, runner=RecordingRunner(dirty=" M file\n"))

    def test_preflight_refuses_existing_outputs_or_batches(self):
        with self.assertRaisesRegex(ValueError, "immutable"):
            run_action("preflight", self.params(), attempt=1,
                       runner=RecordingRunner(existing_prefixes=[run_id("baseline", "gold")]))
        with self.assertRaises(ValueError):
            run_action("preflight", self.params(), attempt=1,
                       runner=RecordingRunner(existing_batches=[run_id("baseline", "silver")]))

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
            run_action("quota-silver", self.params(), attempt=1, runner=RecordingRunner())

    def test_transformation_refuses_an_existing_output_prefix(self):
        runner = RecordingRunner(existing_prefixes=[run_id("baseline", "bronze")])
        with self.assertRaisesRegex(ValueError, "immutable"):
            run_action("submit-bronze", self.params(), attempt=1, runner=runner)
        self.assertEqual([], self.submits(runner))

    def test_dataproc_submission_failure_propagates(self):
        for stage in ("bronze", "silver", "gold", "features"):
            with self.subTest(stage), self.assertRaises(subprocess.CalledProcessError):
                run_action(f"submit-{stage}", self.params(), attempt=1, runner=RecordingRunner(submit_fails=True))

    def test_validation_batch_failure_propagates(self):
        for stage in ("bronze", "silver", "gold", "features"):
            with self.subTest(stage), self.assertRaises(subprocess.CalledProcessError):
                run_action(f"validate-{stage}", self.params(), attempt=1, runner=RecordingRunner(submit_fails=True))

    def test_a_failing_report_fails_the_task_even_if_the_batch_succeeded(self):
        for stage in ("bronze", "silver", "gold", "features"):
            with self.subTest(stage), self.assertRaisesRegex(ValueError, "failed"):
                run_action(f"validate-{stage}", self.params(), attempt=1, runner=RecordingRunner(report_status="FAIL"))

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
            run_action("validate-gold", self.params(), attempt=1, runner=runner)
        self.assertEqual([], self.submits(runner))
        self.assertRaises(ValueError, submit_stage_validation.validation_batch_id, "x", 0)

    def test_physical_validation_requires_the_exact_inventory(self):
        gold = {"dim_user", "dim_account", "dim_merchant", "dim_device", "dim_date", "fact_transactions",
                "fact_login_events", "fact_balance_snapshot", "obt_transaction_enriched", "feat_user_90d",
                "opt_merchant_performance", "dim_merchant_scd2"}
        run_action("physical-gold", self.params(), attempt=1, runner=RecordingRunner(tables=gold))
        for tables in (gold - {"dim_merchant_scd2"}, gold | {"dim_extra"}):
            with self.subTest(len(tables)), self.assertRaisesRegex(ValueError, "mismatch"):
                run_action("physical-gold", self.params(), attempt=1, runner=RecordingRunner(tables=tables))

    def test_finalize_requires_every_batch_to_have_succeeded(self):
        params = self.params()
        batches = [run_id("baseline", s) for s in ("bronze", "silver", "gold", "feat")]
        batches += [f"{item}-v1" for item in batches]
        report = {"status": "PASS", "manifest_uri": "m", "manifest_sha256": "s", "plan_digest": "p", "physical_bytes": 1}
        with patch.object(campaign_task, "verify_source_dataset", return_value=report):
            runner = RecordingRunner(existing_batches=batches)
            run_action("finalize", params, attempt=1, runner=runner)
            self.assertEqual([f"gs://{BUCKET}/lakehouse/campaigns/{BENCH}/baseline/campaign_manifest.json"], runner.written)
            with self.assertRaisesRegex(CampaignError, "SUCCEEDED"):
                run_action("finalize", params, attempt=1, runner=RecordingRunner(existing_batches=batches, batch_state="FAILED"))
            with self.assertRaisesRegex(ValueError, "failed"):
                run_action("finalize", params, attempt=1, runner=RecordingRunner(existing_batches=batches, report_status="FAIL"))

    def finalize_manifest(self, runner, params=None):
        report = {"status": "PASS", "manifest_uri": "m", "manifest_sha256": "b" * 64, "plan_digest": "p", "physical_bytes": 1}
        with patch.object(campaign_task, "verify_source_dataset", return_value=report):
            run_action("finalize", params or self.params(), attempt=1, runner=runner)
        return json.loads(runner.bodies[runner.written[-1]])

    def test_the_manifest_records_execution_metadata_for_every_stage(self):
        batches = [run_id("baseline", s) for s in ("bronze", "silver", "gold", "feat")]
        batches += [f"{item}-v1" for item in batches]
        manifest = self.finalize_manifest(RecordingRunner(existing_batches=batches))
        self.assertEqual("edai1-pipeline-campaign-v2", manifest["schema_version"])
        self.assertEqual(BUCKET, manifest["storage"]["data_bucket"])
        for stage, alias in (("bronze", "bronze"), ("silver", "silver"), ("gold", "gold"), ("features", "feat")):
            with self.subTest(stage):
                entry = manifest["stages"][stage]
                execution = entry["execution_metadata"]
                self.assertEqual(run_id("baseline", alias), execution["dataproc_batch_id"])
                self.assertEqual("app-1", execution["spark_application_id"])
                self.assertEqual("transformation", execution["kind"])
                self.assertEqual(f"gs://{BUCKET}/spark-events/app-1", execution["spark_event_log_uri"])
                self.assertEqual("validation", entry["validation"]["execution_metadata"]["kind"])
                self.assertEqual("PASS", entry["validation"]["execution_metadata"]["validation_status"])

    def test_a_failed_transformation_keeps_a_provenance_record(self):
        runner = RecordingRunner(submit_fails=True)
        with self.assertRaises(subprocess.CalledProcessError):
            run_action("submit-gold", self.params(), attempt=1, runner=runner)
        run = run_id("baseline", "gold")
        uri = f"gs://{BUCKET}/lakehouse/runs/{run}/evidence/execution_provenance-{run}.json"
        self.assertEqual([uri], runner.written)
        self.assertEqual("transformation", json.loads(runner.bodies[uri])["kind"])

    def test_a_submission_that_never_started_leaves_no_record(self):
        runner = RecordingRunner(existing_prefixes=[run_id("baseline", "bronze")])
        with self.assertRaisesRegex(ValueError, "immutable"):
            run_action("submit-bronze", self.params(), attempt=1, runner=runner)
        self.assertEqual([], runner.written)

    def test_a_validator_batch_failure_keeps_its_attempt_record(self):
        runner = RecordingRunner(submit_fails=True)
        with self.assertRaises(subprocess.CalledProcessError):
            run_action("validate-silver", self.params(), attempt=1, runner=runner)
        run = run_id("baseline", "silver")
        self.assertEqual([f"gs://{BUCKET}/lakehouse/runs/{run}/evidence/execution_provenance-{run}-v1.json"], runner.written)

    def test_every_stage_submits_with_the_rehearsal_bucket_and_execution_record(self):
        for action in ("submit-bronze", "submit-silver", "submit-gold", "submit-features",
                       "validate-bronze", "validate-silver", "validate-gold", "validate-features"):
            with self.subTest(action):
                runner = RecordingRunner()
                run_action(action, self.params(), attempt=1, runner=runner)
                command = " ".join(self.submits(runner)[0])
                self.assertIn(f"spark.eventLog.dir=gs://{BUCKET}/spark-events", command)
                self.assertIn(f"--execution-record-uri=gs://{BUCKET}/lakehouse/runs/", command)
                self.assertNotIn(CANONICAL_DATA_BUCKET, command)

    def test_a_transformation_never_targets_the_canonical_lakehouse(self):
        runner = RecordingRunner()
        run_action("submit-gold", self.params(), attempt=1, runner=runner)
        self.assertNotIn(CANONICAL_DATA_BUCKET, " ".join(self.submits(runner)[0]))


if __name__ == "__main__":
    unittest.main()
