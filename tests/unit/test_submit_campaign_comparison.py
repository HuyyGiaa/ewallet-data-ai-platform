"""The comparison submitter reuses the campaign environment and refuses before paying."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from data_platform.config.dataset_identity import REHEARSAL_5GB, REHEARSAL_DATA_BUCKET
from ops.gcp import submit_campaign_comparison as submitter
from ops.gcp.submit_source_to_bronze import apply_dataset_bucket, load_cloud_runtime
from tests.unit.campaign_fakes import terraform_outputs


BENCHMARK = "ab-20261110225456"
SHA = "aae22b08eee20fa055c4aefc8d23c14cc3ae5f8c"
STAGES = ("bronze", "silver", "gold", "features")


def manifest(profile: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "status": "COMPLETE", "spark_profile": profile, "benchmark_id": BENCHMARK,
        "dataset_version": REHEARSAL_5GB, "git_sha": SHA, "source": {"manifest_sha256": "a" * 64},
        "stages": {stage: {"run_id": f"{BENCHMARK}-{profile}-{stage}", "output_root": f"gs://b/{stage}",
                           "validation": {"status": "PASS"}} for stage in STAGES},
    }
    value.update(overrides)
    return value


class FakeRunner:
    """Answers the read-only checks; records the submission."""

    def __init__(self, manifests, *, git_clean=True, report=None, batch_exists=False, report_exists=False):
        self.manifests, self.git_clean, self.report = manifests, git_clean, report
        self.batch_exists, self.report_exists = batch_exists, report_exists
        self.calls: list[list[str]] = []

    def __call__(self, command, check=False, text=False, capture_output=False):
        command = list(command)
        self.calls.append(command)
        done = lambda code=0, out="", err="": subprocess.CompletedProcess(command, code, out, err)  # noqa: E731
        if command[:2] == ["gcloud", "storage"] and command[2] == "cat":
            uri = command[3]
            if uri.endswith("comparison.json"):
                return done(out=json.dumps(self.report))
            return done(out=json.dumps(self.manifests["baseline" if "/baseline/" in uri else "optimized"]))
        if command[:3] == ["gcloud", "storage", "ls"]:
            return done(out=command[3]) if self.report_exists else done(1, err="One or more URLs matched no objects.")
        if command[:4] == ["gcloud", "dataproc", "batches", "describe"]:
            return done(out="{}") if self.batch_exists else done(1, err="NOT_FOUND: batch not found")
        if command[0] == "git":
            return done(0 if self.git_clean else 1)
        if command[:5] == ["gcloud", "dataproc", "batches", "submit", "pyspark"]:
            return done(0 if (self.report or {}).get("verdict") == "SEMANTIC_EQUIVALENT" else 1)
        raise AssertionError(f"unexpected command: {command}")

    @property
    def submitted(self) -> bool:
        return any(call[:4] == ["gcloud", "dataproc", "batches", "submit"] for call in self.calls)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.runtime = apply_dataset_bucket(load_cloud_runtime(terraform_outputs()), REHEARSAL_5GB)

    def command(self, **kwargs):
        return submitter.build_submission_command(
            self.runtime, benchmark_id=BENCHMARK, dataset_version=REHEARSAL_5GB, git_sha=SHA,
            jar_paths=(Path("/jars/delta-spark.jar"), Path("/jars/delta-storage.jar")), **kwargs)

    def test_reads_the_two_manifests_and_writes_one_report(self):
        command = self.command()
        root = f"gs://{REHEARSAL_DATA_BUCKET}/lakehouse/campaigns/{BENCHMARK}"
        self.assertEqual(command[command.index("--") + 1:], (
            f"--baseline-manifest-uri={root}/baseline/campaign_manifest.json",
            f"--optimized-manifest-uri={root}/optimized/campaign_manifest.json",
            f"--report-uri={root}/comparison.json",
        ))
        self.assertTrue(command[5].endswith("ops/gcp/compare_campaign_outputs.py"))
        self.assertIn("--batch=c1-20261110225456", command)
        self.assertIn("--version=2.2", command)

    def test_same_environment_as_the_validators_without_a_benchmark_profile(self):
        properties = dict(item.split("=", 1) for item in
                          next(a for a in self.command() if a.startswith("--properties=")).split("=", 1)[1].split(","))
        self.assertEqual(properties["spark.sql.extensions"], "io.delta.sql.DeltaSparkSessionExtension")
        self.assertEqual(properties["spark.sql.session.timeZone"], "UTC")
        self.assertEqual(properties["spark.eventLog.dir"], f"gs://{REHEARSAL_DATA_BUCKET}/spark-events")
        self.assertEqual(properties["spark.dynamicAllocation.maxExecutors"], "7")
        self.assertEqual(properties["spark.executor.memory"], "9600m")
        self.assertFalse(any(key.startswith("spark.sql.adaptive") or key == "spark.sql.shuffle.partitions"
                             for key in properties), "the comparison is not a Baseline/Optimized leg")

    def test_batch_id_per_attempt_within_contract(self):
        self.assertEqual(submitter.comparison_batch_id(BENCHMARK, 2), "c2-20261110225456")
        with self.assertRaises(ValueError):
            submitter.comparison_batch_id(BENCHMARK, 0)
        with self.assertRaises(ValueError):
            submitter.comparison_batch_id("not-a-benchmark", 1)

    def test_refuses_the_canonical_bucket_for_the_rehearsal(self):
        canonical = load_cloud_runtime(terraform_outputs())
        with self.assertRaises(ValueError):
            submitter.build_submission_command(canonical, benchmark_id=BENCHMARK, dataset_version=REHEARSAL_5GB,
                                               git_sha=SHA, jar_paths=(Path("/j.jar"),))


class MainTest(unittest.TestCase):
    def run_main(self, runner, *extra):
        with tempfile.TemporaryDirectory() as directory:
            outputs = Path(directory) / "terraform.json"
            outputs.write_text(json.dumps(terraform_outputs()), encoding="utf-8")
            with unittest.mock.patch.object(submitter, "ensure_delta_dependencies", return_value=(Path("/j.jar"),)), \
                 unittest.mock.patch.object(submitter, "build_python_archive", return_value=Path("/a.zip")):
                return submitter.main(["--benchmark-id", BENCHMARK, "--dataset-version", REHEARSAL_5GB,
                                       "--git-sha", SHA, "--terraform-output-json", str(outputs), *extra],
                                      runner=runner)

    def manifests(self, **optimized):
        return {"baseline": manifest("baseline"), "optimized": manifest("optimized", **optimized)}

    def test_dry_run_calls_nothing(self):
        runner = FakeRunner(self.manifests())
        self.assertEqual(self.run_main(runner), 0)
        self.assertEqual(runner.calls, [])

    def test_execute_checks_then_submits_and_reads_the_verdict(self):
        runner = FakeRunner(self.manifests(), report={"verdict": "SEMANTIC_EQUIVALENT"})
        self.assertEqual(self.run_main(runner, "--execute"), 0)
        kinds = [" ".join(call[:4]) for call in runner.calls]
        self.assertLess(kinds.index("gcloud dataproc batches describe"), kinds.index("gcloud dataproc batches submit"))
        self.assertIn("gcloud storage ls " + f"gs://{REHEARSAL_DATA_BUCKET}/lakehouse/campaigns/{BENCHMARK}/comparison.json",
                      [" ".join(call) for call in runner.calls])

    def test_mismatch_is_reported_not_hidden(self):
        runner = FakeRunner(self.manifests(), report={"verdict": "SEMANTIC_MISMATCH"})
        self.assertEqual(self.run_main(runner, "--execute"), 1)
        self.assertTrue(runner.submitted)

    def test_refusals_before_any_paid_batch(self):
        cases = {
            "optimized not complete": FakeRunner(self.manifests(status="RUNNING")),
            "different pinned commit": FakeRunner(self.manifests(git_sha="b" * 40)),
            "different Source": FakeRunner(self.manifests(source={"manifest_sha256": "c" * 64})),
            "comparison code changed": FakeRunner(self.manifests(), git_clean=False),
            "batch exists": FakeRunner(self.manifests(), batch_exists=True),
            "report exists": FakeRunner(self.manifests(), report_exists=True),
        }
        for name, runner in cases.items():
            with self.subTest(name):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.run_main(runner, "--execute")
                self.assertFalse(runner.submitted)


if __name__ == "__main__":
    unittest.main()
