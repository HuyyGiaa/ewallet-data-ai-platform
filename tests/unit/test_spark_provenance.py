"""Spark execution provenance for every campaign batch (G3), from mocked Dataproc metadata."""

from __future__ import annotations

import argparse
import json
import subprocess
import unittest

from data_platform.config.dataset_identity import CANONICAL_DATA_BUCKET, REHEARSAL_5GB, REHEARSAL_DATA_BUCKET, resolve_storage_context
from ops.gcp import spark_provenance as sp


BUCKET = REHEARSAL_DATA_BUCKET
BENCH = "ab-20261110000000"
SHA = "a" * 40
RUNS = {stage: f"{BENCH}-baseline-{alias}-r26111000000{i}"
        for i, (stage, alias) in enumerate((("bronze", "bronze"), ("silver", "silver"), ("gold", "gold"), ("features", "feat")))}
STORAGE = resolve_storage_context(REHEARSAL_5GB)


def batch(state="SUCCEEDED", **overrides):
    value = {
        "state": state,
        "createTime": "2026-11-10T00:00:00.000000Z",
        "stateTime": "2026-11-10T00:10:30.123456789Z",
        "stateHistory": [{"state": "PENDING", "stateStartTime": "2026-11-10T00:00:00Z"},
                         {"state": "RUNNING", "stateStartTime": "2026-11-10T00:01:00Z"}],
        "labels": {"benchmark_id": BENCH, "spark_profile": "baseline", "git_sha": SHA},
        "runtimeConfig": {"version": "2.2.89"},
        "runtimeInfo": {"outputUri": "gs://temp/driveroutput", "endpoints": {"Spark History Server": "https://history.example/app"}},
    }
    value.update(overrides)
    return value


def record(run_id, **overrides):
    return {"run_id": run_id, "dataset_version": REHEARSAL_5GB, "spark_application_id": "application_123",
            "spark_event_log_dir": f"gs://{BUCKET}/spark-events", **overrides}


class Dataproc:
    def __init__(self, batches=None, files=None, event_logs=("application_123",)):
        self.batches, self.files, self.event_logs = batches or {}, files or {}, event_logs
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        def done(code=0, out="", err=""):
            return subprocess.CompletedProcess(command, code, out, err)
        if command[:4] == ["gcloud", "dataproc", "batches", "describe"]:
            value = self.batches.get(command[4])
            return done(out=json.dumps(value)) if value else done(1, err="NOT_FOUND")
        if command[:3] == ["gcloud", "storage", "cat"]:
            value = self.files.get(command[3])
            return done(out=json.dumps(value)) if value else done(1, err="matched no objects")
        if command[:3] == ["gcloud", "storage", "ls"]:
            name = command[3].rsplit("/", 1)[1].rstrip("*")
            return done(out=command[3].rstrip("*") + "\n") if name in self.event_logs else done(1, err="matched no objects")
        raise AssertionError(command)


def context(stage="gold", kind="transformation", **overrides):
    values = dict(
        campaign_id=f"{BENCH}-baseline", dataset_version=REHEARSAL_5GB, spark_profile="baseline", stage_name=stage,
        kind=kind, git_sha=SHA, source_manifest_sha256="b" * 64, project_id="p", region="asia-southeast1",
        data_bucket=BUCKET, run_id=RUNS[stage],
        input_roots=sp.stage_inputs(STORAGE, stage, RUNS), output_roots=(sp.stage_output(STORAGE, stage, RUNS),),
    )
    values.update(overrides)
    return sp.ExecutionContext(**values)


def record_uri(run_id, batch_id):
    return f"gs://{BUCKET}/lakehouse/runs/{run_id}/evidence/spark_execution-{batch_id}.json"


class CollectExecutionTest(unittest.TestCase):
    def collect(self, ctx, fake, batch_id=None, report=None):
        batch_id = batch_id or ctx.run_id
        return sp.collect_execution(ctx, batch_id=batch_id, record_uri=record_uri(ctx.run_id, batch_id),
                                    report_uri=report, runner=fake)

    def test_a_successful_stage_has_full_metadata(self):
        for stage, run_id in RUNS.items():
            with self.subTest(stage):
                ctx = context(stage)
                fake = Dataproc({run_id: batch()}, {record_uri(run_id, run_id): record(run_id)})
                result = self.collect(ctx, fake)
                self.assertEqual("SUCCEEDED", result["dataproc_batch_state"])
                self.assertEqual("application_123", result["spark_application_id"])
                self.assertEqual(f"gs://{BUCKET}/spark-events/application_123", result["spark_event_log_uri"])
                self.assertEqual("https://history.example/app", result["history_server_uri"])
                self.assertEqual("2.2.89", result["runtime_version"])
                self.assertEqual(570.123456, result["job_duration_seconds"])
                self.assertEqual([sp.stage_output(STORAGE, stage, RUNS)], result["output_roots"])
                self.assertEqual(stage, result["stage_name"])
                self.assertEqual(f"{BENCH}-baseline", result["campaign_id"])

    def test_a_validator_attempt_has_its_report(self):
        run_id = RUNS["silver"]
        batch_id = f"{run_id}-v2"
        report = f"gs://{BUCKET}/lakehouse/runs/{run_id}/evidence/validation-silver-a2.json"
        ctx = context("silver", "validation", output_roots=(), input_roots=sp.validation_inputs(STORAGE, "silver", RUNS))
        fake = Dataproc({batch_id: batch()}, {record_uri(run_id, batch_id): record(run_id), report: {"status": "PASS"}})
        result = self.collect(ctx, fake, batch_id, report)
        self.assertEqual("PASS", result["validation_status"])
        self.assertEqual(report, result["validation_report_uri"])

    def test_a_failed_batch_is_reported_honestly(self):
        run_id = RUNS["gold"]
        result = self.collect(context(), Dataproc({run_id: batch("FAILED")}, {record_uri(run_id, run_id): record(run_id)}))
        self.assertEqual("FAILED", result["dataproc_batch_state"])
        self.assertIsNotNone(result["job_end_timestamp"])
        self.assertEqual("application_123", result["spark_application_id"])

    def test_a_running_batch_has_no_end_time(self):
        run_id = RUNS["gold"]
        result = self.collect(context(), Dataproc({run_id: batch("RUNNING")}, {record_uri(run_id, run_id): record(run_id)}))
        self.assertEqual("RUNNING", result["dataproc_batch_state"])
        self.assertIsNone(result["job_end_timestamp"])
        self.assertIsNone(result["job_duration_seconds"])

    def test_a_missing_batch_is_recorded_as_not_found(self):
        result = self.collect(context(), Dataproc())
        self.assertFalse(result["dataproc_batch_found"])
        self.assertIsNone(result["dataproc_batch_state"])
        self.assertIsNone(result["spark_application_id"])

    def test_a_missing_execution_record_leaves_the_application_id_null(self):
        run_id = RUNS["gold"]
        result = self.collect(context(), Dataproc({run_id: batch()}))
        self.assertIsNone(result["spark_application_id"])
        self.assertIsNone(result["spark_event_log_uri"])
        self.assertEqual("SUCCEEDED", result["dataproc_batch_state"])

    def test_a_missing_event_log_object_leaves_the_uri_null(self):
        run_id = RUNS["gold"]
        fake = Dataproc({run_id: batch()}, {record_uri(run_id, run_id): record(run_id)}, event_logs=())
        result = self.collect(context(), fake)
        self.assertIsNone(result["spark_event_log_uri"])
        self.assertEqual(f"gs://{BUCKET}/spark-events", result["spark_event_log_dir"])

    def test_no_history_endpoint_stays_null(self):
        run_id = RUNS["gold"]
        fake = Dataproc({run_id: batch(runtimeInfo={})}, {record_uri(run_id, run_id): record(run_id)})
        self.assertIsNone(self.collect(context(), fake)["history_server_uri"])

    def test_wrong_bucket_identity_or_mapping_is_refused(self):
        run_id = RUNS["gold"]
        uri = record_uri(run_id, run_id)
        cases = {
            "event log bucket": Dataproc({run_id: batch()}, {uri: record(run_id, spark_event_log_dir=f"gs://{CANONICAL_DATA_BUCKET}/spark-events")}),
            "label benchmark_id": Dataproc({run_id: batch(labels={"benchmark_id": "other", "spark_profile": "baseline", "git_sha": SHA})}),
            "label spark_profile": Dataproc({run_id: batch(labels={"benchmark_id": BENCH, "spark_profile": "optimized", "git_sha": SHA})}),
            "record run_id": Dataproc({run_id: batch()}, {uri: record("other-run")}),
            "record dataset_version": Dataproc({run_id: batch()}, {uri: record(run_id, dataset_version="canonical-100gb-v1")}),
        }
        for name, fake in cases.items():
            with self.subTest(name), self.assertRaises(sp.ProvenanceError):
                self.collect(context(), fake)
        with self.assertRaisesRegex(sp.ProvenanceError, "does not map"):
            self.collect(context(), Dataproc({"other": batch()}), batch_id="other")
        with self.assertRaisesRegex(sp.ProvenanceError, "does not map"):
            self.collect(context(kind="validation"), Dataproc(), batch_id=f"{run_id}-x1")

    def test_repeated_collection_is_identical(self):
        run_id = RUNS["gold"]
        fake = Dataproc({run_id: batch()}, {record_uri(run_id, run_id): record(run_id)})
        self.assertEqual(self.collect(context(), fake), self.collect(context(), fake))

    def test_transient_describe_error_raises(self):
        def fake(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "PERMISSION_DENIED")
        with self.assertRaises(sp.ProvenanceError):
            self.collect(context(), fake)


class CollectCampaignTest(unittest.TestCase):
    def test_all_stages_and_every_validator_attempt_are_collected(self):
        batches = {run_id: batch() for run_id in RUNS.values()}
        batches[f"{RUNS['silver']}-v1"] = batch("FAILED")
        batches[f"{RUNS['silver']}-v2"] = batch()
        args = argparse.Namespace(
            dataset_version=REHEARSAL_5GB, benchmark_id=BENCH, spark_profile="baseline", git_sha=SHA,
            source_manifest_sha256="b" * 64, bronze_run_id=RUNS["bronze"], silver_run_id=RUNS["silver"],
            gold_run_id=RUNS["gold"], features_run_id=RUNS["features"], project_id="p", region="r", data_bucket=BUCKET)
        result = sp.collect_campaign(args, Dataproc(batches))
        self.assertEqual({"bronze", "silver", "gold", "features"}, set(result["stages"]))
        attempts = result["stages"]["silver"]["validation_attempts"]
        self.assertEqual(["FAILED", "SUCCEEDED"], [item["dataproc_batch_state"] for item in attempts])
        self.assertEqual([], result["stages"]["gold"]["validation_attempts"])
        args.data_bucket = CANONICAL_DATA_BUCKET
        with self.assertRaises(ValueError):
            sp.collect_campaign(args, Dataproc(batches))


if __name__ == "__main__":
    unittest.main()
