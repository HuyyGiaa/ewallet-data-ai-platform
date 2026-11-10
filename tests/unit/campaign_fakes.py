"""Shared fakes for the EDAI-1 pipeline tests: no test ever reaches gcloud, git or Airflow.

``RecordingRunner`` answers every subprocess call the campaign makes (git, Dataproc,
GCS listings, reads and write-once copies) from in-memory state and fails the test on
anything else.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from data_platform.config.dataset_identity import CANONICAL_DATA_BUCKET, REHEARSAL_5GB, REHEARSAL_DATA_BUCKET
from ops.gcp import (
    campaign_task,
    submit_bronze_to_silver,
    submit_historical_features,
    submit_silver_to_gold,
    submit_source_to_bronze,
    submit_stage_validation,
    wait_for_dataproc_quota,
)
from ops.gcp.campaign_task import PIPELINES, Producer, parse_parameters, run_action


BUCKET = REHEARSAL_DATA_BUCKET
SHA = "a" * 40
SOURCE_SHA = "b" * 64
BENCH = "ab-20261110000000"
ATTEMPT_SUFFIX = {"bronze": "r261110000000", "silver": "r261110000001", "gold": "r261110000002", "feat": "r261110000003"}
ALIAS = {"bronze": "bronze", "silver": "silver", "gold": "gold", "features": "feat"}
STAGE_PIPELINE = {"bronze": "dp1", "silver": "dp2", "gold": "dp2", "features": "dp3"}
SOURCE_REPORT = {"status": "PASS", "manifest_uri": f"gs://{BUCKET}/source/{REHEARSAL_5GB}/_control/dataset_manifest.json",
                 "manifest_sha256": SOURCE_SHA, "plan_digest": "p" * 64, "physical_bytes": 5_000_608_241}


def run_id(profile: str, stage: str, benchmark: str = BENCH) -> str:
    """``stage`` is a lineage alias: bronze, silver, gold or feat."""

    return f"{benchmark}-{profile}-{stage}-{ATTEMPT_SUFFIX[stage]}"


def values(profile: str = "baseline", pipeline: str = "dp3", **overrides: str) -> dict[str, str]:
    """A complete, approved parameter set of one pipeline."""

    result = {
        "dataset_version": REHEARSAL_5GB, "benchmark_id": BENCH, "spark_profile": profile, "git_sha": SHA,
        "source_manifest_sha256": SOURCE_SHA,
        "project_id": "ewallet-data-ai-platform-hg26", "region": "asia-southeast1", "data_bucket": BUCKET,
        "dataproc_ttl": "8h", "terraform_output_json": "", "execution_approval": "",
    }
    for name in PIPELINES[pipeline].run_parameters:
        result[name] = run_id(profile, {"bronze_run_id": "bronze", "silver_run_id": "silver",
                                         "gold_run_id": "gold", "feature_run_id": "feat"}[name])
    result.update(overrides)
    if "execution_approval" not in overrides:
        result["execution_approval"] = campaign_task.expected_approval(result, pipeline)
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


def finished_batches(profile: str, stages=("bronze", "silver", "gold", "features")) -> list[str]:
    """Transformation and first validator batch of each stage."""

    runs = [run_id(profile, ALIAS[stage]) for stage in stages]
    return runs + [submit_stage_validation.validation_batch_id(item, 1) for item in runs]


class RecordingRunner:
    """Answers the gcloud/git calls the campaign makes; everything else is an error."""

    def __init__(self, *, git_sha: str = SHA, dirty: str = "", existing_prefixes=(), existing_batches=(),
                 report_status: str = "PASS", submit_fails: bool = False, tables: set[str] | None = None,
                 batch_state: str = "SUCCEEDED", bucket_exists: bool = True, failing_scopes=(),
                 objects: dict[str, str] | None = None, branch: str = "main") -> None:
        self.calls: list[list[str]] = []
        self.git_sha, self.dirty, self.branch = git_sha, dirty, branch
        self.existing_prefixes, self.existing_batches = tuple(existing_prefixes), set(existing_batches)
        self.report_status, self.submit_fails, self.tables = report_status, submit_fails, tables
        self.failing_scopes = set(failing_scopes)
        self.batch_state = batch_state
        self.bucket_exists = bucket_exists
        self.objects: dict[str, str] = dict(objects or {})
        self.written: list[str] = []
        self.bodies: dict[str, str] = {}

    @staticmethod
    def _done(returncode: int = 0, stdout: str = "", stderr: str = ""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, command, **kwargs):
        command = [str(item) for item in command]
        self.calls.append(command)
        if command[0] == "git":
            arguments = command[3:] if command[1:2] == ["-C"] else command[1:]
            if arguments[:2] == ["rev-parse", "--abbrev-ref"]:
                return self._done(stdout=self.branch + "\n")
            if arguments[:1] == ["rev-parse"]:
                return self._done(stdout=self.git_sha + "\n")
            if arguments[:1] == ["status"]:
                return self._done(stdout=self.dirty)
        if command[:4] == ["gcloud", "dataproc", "batches", "describe"]:
            batch = command[4]
            if batch in self.existing_batches:
                profile = "optimized" if "-optimized-" in batch else "baseline"
                benchmark = "ab-" + batch.split("-")[1]  # ab-<14 digits>-... or v<n>-<14 digits>-...
                return self._done(stdout=json.dumps({
                    "state": self.batch_state, "uuid": batch, "createTime": "2026-11-10T00:00:00Z",
                    "stateTime": "2026-11-10T00:10:00Z",
                    "labels": {"benchmark_id": benchmark, "spark_profile": profile, "git_sha": self.git_sha},
                    "runtimeInfo": {"endpoints": {"Spark History Server": f"https://history.example/{batch}"}},
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
        if command[:4] == ["gcloud", "storage", "objects", "describe"]:
            if command[4] in self.objects:
                return self._done(stdout=json.dumps({"name": command[4]}))
            return self._done(1, stderr="ERROR: not found: 404")
        if command[:3] == ["gcloud", "storage", "cat"]:
            uri = command[3]
            if uri in self.objects:
                return self._done(stdout=self.objects[uri])
            if "spark_execution-" in uri:
                run = uri.split("/runs/", 1)[1].split("/", 1)[0]
                return self._done(stdout=json.dumps({
                    "run_id": run, "dataset_version": REHEARSAL_5GB, "spark_application_id": "app-1",
                    "spark_event_log_dir": f"gs://{BUCKET}/spark-events"}))
            return self._done(stdout=json.dumps(self.report(uri)))
        if command[:3] == ["gcloud", "storage", "ls"]:
            return self.listing(command[3])
        if command[:3] == ["gcloud", "storage", "cp"]:
            target = command[4]
            if "--if-generation-match=0" in command and target in self.objects:
                return self._done(1, stderr="PreconditionFailed: 412")
            text = Path(command[3]).read_text(encoding="utf-8")
            self.written.append(target)
            self.bodies[target] = text
            self.objects[target] = text
            return self._done()
        raise AssertionError(f"unexpected command: {command}")

    def report(self, uri: str) -> dict[str, object]:
        scope = uri.rsplit("validation-", 1)[1].rsplit("-a", 1)[0]
        run = uri.split("/runs/", 1)[1].split("/", 1)[0]
        status = "FAIL" if scope in self.failing_scopes else self.report_status
        return {"scope": scope, "run_id": run, "dataset_version": REHEARSAL_5GB, "status": status,
                "failed_rules": ["x"] if status != "PASS" else [],
                "results": [{"rule_name": "x", "passed": status == "PASS"}]}

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
        tables = self.tables
        if tables is None:
            from ops.gcp.validate_spark_output_equivalence import expected_tables
            zone_stage = {"/bronze/": "dp1-bronze", "/silver/": "dp2-silver"}
            stage = next((value for key, value in zone_stage.items() if uri.endswith(key)), None)
            if stage is None:
                stage = "dp3-features" if "-feat-" in uri else "dp2-gold"
            tables = expected_tables(stage, REHEARSAL_5GB)
        return self._done(stdout="\n".join(f"{uri}{table}/" for table in sorted(tables)))


def producer(pipeline: str, profile: str = "baseline") -> Producer:
    return Producer(PIPELINES[pipeline].dag_id, f"{BENCH}-{profile}-{pipeline}")


class TerraformFixture(unittest.TestCase):
    """Saved Terraform outputs, no-op archive builds and an always-ready quota gate."""

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
        source = patch.object(campaign_task, "verify_source_dataset", return_value=dict(SOURCE_REPORT))
        self.source = source.start()
        self.addCleanup(source.stop)

    def params(self, profile: str = "baseline", pipeline: str = "dp3", **overrides: str):
        return parse_parameters(values(profile, pipeline, terraform_output_json=str(self.outputs), **overrides),
                                pipeline=pipeline)

    def stage_params(self, stage: str, profile: str = "baseline", **overrides: str):
        return self.params(profile, STAGE_PIPELINE[stage], **overrides)

    def submits(self, runner: RecordingRunner) -> list[list[str]]:
        return [call for call in runner.calls if call[:4] == ["gcloud", "dataproc", "batches", "submit"]]

    def publish(self, pipeline: str, runner: RecordingRunner, profile: str = "baseline", **overrides: str) -> dict[str, Any]:
        """Publish one pipeline's handoff through the real action; returns the stored document."""

        params = self.params(profile, pipeline, **overrides)
        run_action("publish-handoff", params, attempt=1, runner=runner, producer=producer(pipeline, profile))
        return json.loads(runner.objects[campaign_task.handoff_uri(params, pipeline)])

    def completed_runner(self, profile: str = "baseline", upto: str = "dp2", **kwargs) -> RecordingRunner:
        """A runner whose batches all succeeded and whose handoffs up to ``upto`` are published."""

        runner = RecordingRunner(existing_batches=finished_batches(profile) + finished_batches(
            "optimized" if profile == "baseline" else "baseline"), **kwargs)
        for pipeline in ("dp1", "dp2", "dp3")[:("dp1", "dp2", "dp3").index(upto) + 1]:
            self.publish(pipeline, runner, profile)
        return runner
