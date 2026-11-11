"""One authoritative bucket per dataset, across generator, submitters and validators (G1)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data_platform.config import load_runtime_config
from data_platform.config.dataset_identity import (
    CANONICAL_100GB,
    CANONICAL_DATA_BUCKET,
    REHEARSAL_5GB,
    REHEARSAL_DATA_BUCKET,
    isolated_data_bucket,
    refuse_misplaced_dataset,
    require_same_bucket,
    resolve_storage_context,
    validate_feature_store_storage,
)
from ops.gcp import generate_source_dataset, rehearsal_storage, submit_stage_validation
from ops.gcp.dataproc_batch_properties import build_dataproc_batch_properties
from ops.gcp.rehearsal_storage import StorageNotReady, StoragePlan, check_bucket, provisioning_commands
from ops.gcp.submit_source_to_bronze import apply_dataset_bucket, load_cloud_runtime
from tests.unit.test_campaign_orchestration import (
    BENCH,
    SHA,
    run_id,
    terraform_outputs,
)


RUNTIME = load_cloud_runtime(terraform_outputs())
CONFIG = "data_platform/generation/config/settings_merchant_history.yaml"
PLAN_ARGS = ["--config", CONFIG, "--environment", "gcp", "--dataset-version", REHEARSAL_5GB, "--global-seed", "7001",
             "--shard-count", "186", "--users-per-shard", "12500", "--merchants-per-shard", "300",
             "--transactions-per-shard", "100000"]
ENV = {"GCP_PROJECT_ID": "ewallet-data-ai-platform-hg26", "DATAHUB_GMS_URL": "http://datahub.invalid"}


class StorageContextTest(unittest.TestCase):
    def test_feature_store_storage_is_bound_to_dataset_campaign_and_run(self):
        bucket = CANONICAL_DATA_BUCKET
        result = validate_feature_store_storage(
            dataset_version=CANONICAL_100GB,
            offline_feature_root=f"gs://{bucket}/streaming-features",
            feast_offline_data_root=(
                f"gs://{bucket}/streaming-features/campaigns/verify-campaign-1/"
                "runs/verify-campaign-1/offline"
            ),
            history_root=f"gs://{bucket}/streaming-history/verify-campaign-1",
            online_registry_uri=(
                f"gs://{bucket}/features/registries/{CANONICAL_100GB}/online/registry-1/registry.db"
            ),
            campaign_id="verify-campaign-1",
            run_id="verify-campaign-1",
        )
        self.assertEqual(bucket, result.data_bucket)

    def test_feature_store_storage_rejects_cross_bucket_and_wrong_nesting(self):
        canonical = CANONICAL_DATA_BUCKET
        rehearsal = REHEARSAL_DATA_BUCKET
        valid = {
            "dataset_version": REHEARSAL_5GB,
            "offline_feature_root": f"gs://{rehearsal}/streaming-features",
            "feast_offline_data_root": (
                f"gs://{rehearsal}/streaming-features/campaigns/verify-rehearsal/"
                "runs/verify-rehearsal/offline"
            ),
            "history_root": f"gs://{rehearsal}/streaming-history/verify-rehearsal",
            "online_registry_uri": (
                f"gs://{rehearsal}/features/registries/{REHEARSAL_5GB}/online/registry-1/registry.db"
            ),
            "campaign_id": "verify-rehearsal",
            "run_id": "verify-rehearsal",
        }
        for field in (
            "offline_feature_root",
            "feast_offline_data_root",
            "history_root",
            "online_registry_uri",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "must use bucket"):
                validate_feature_store_storage(
                    **{**valid, field: valid[field].replace(rehearsal, canonical, 1)}
                )
        with self.assertRaisesRegex(ValueError, "exact campaign/run path"):
            validate_feature_store_storage(
                **{**valid, "feast_offline_data_root": valid["feast_offline_data_root"].replace(
                    "/runs/verify-rehearsal/", "/runs/other/"
                )}
            )

    def test_feature_store_storage_rejects_malformed_ambiguous_and_missing_identity(self):
        bucket = CANONICAL_DATA_BUCKET
        valid = {
            "dataset_version": CANONICAL_100GB,
            "offline_feature_root": f"gs://{bucket}/streaming-features",
            "feast_offline_data_root": (
                f"gs://{bucket}/streaming-features/campaigns/verify-campaign/"
                "runs/verify-campaign/offline"
            ),
            "history_root": f"gs://{bucket}/streaming-history/verify-campaign",
            "online_registry_uri": (
                f"gs://{bucket}/features/registries/{CANONICAL_100GB}/online/r/registry.db"
            ),
            "campaign_id": "verify-campaign",
            "run_id": "verify-campaign",
        }
        cases = (
            ("dataset_version", ""),
            ("offline_feature_root", "gs:///streaming-features"),
            ("offline_feature_root", "gs://bucket:443/streaming-features"),
            ("offline_feature_root", "gs://bad bucket/streaming-features"),
            ("history_root", f"gs://{bucket}/streaming-history/../escape"),
            ("online_registry_uri", f"gs://{bucket}/features/registries/%2e%2e/online/r/registry.db"),
            ("feast_offline_data_root", f"gs://{bucket}//streaming-features/campaigns/verify-campaign/runs/verify-campaign/offline"),
        )
        for field, value in cases:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_feature_store_storage(**{**valid, field: value})

    def test_each_dataset_resolves_its_own_bucket(self):
        self.assertEqual(CANONICAL_DATA_BUCKET, resolve_storage_context(CANONICAL_100GB).data_bucket)
        self.assertEqual(REHEARSAL_DATA_BUCKET, resolve_storage_context(REHEARSAL_5GB).data_bucket)

    def test_context_derives_every_location_from_the_bucket(self):
        context = resolve_storage_context(REHEARSAL_5GB)
        self.assertEqual(f"gs://{REHEARSAL_DATA_BUCKET}/source/{REHEARSAL_5GB}", context.source_root)
        self.assertEqual(f"gs://{REHEARSAL_DATA_BUCKET}/lakehouse/runs/r/gold", context.run_root("r", "gold"))
        self.assertEqual(f"gs://{REHEARSAL_DATA_BUCKET}/lakehouse/runs/r/evidence", context.evidence_root("r"))
        self.assertEqual(f"gs://{REHEARSAL_DATA_BUCKET}/spark-events", context.spark_event_log_root)

    def test_wrong_bucket_or_unknown_dataset_is_refused(self):
        with self.assertRaisesRegex(ValueError, "must use bucket"):
            resolve_storage_context(REHEARSAL_5GB, CANONICAL_DATA_BUCKET)
        with self.assertRaisesRegex(ValueError, "must use bucket"):
            resolve_storage_context(CANONICAL_100GB, REHEARSAL_DATA_BUCKET)
        with self.assertRaisesRegex(ValueError, "not an approved"):
            resolve_storage_context("unknown-v1")

    def test_phase_a_guards_are_kept_in_both_directions(self):
        with self.assertRaises(ValueError):
            refuse_misplaced_dataset(REHEARSAL_5GB, CANONICAL_DATA_BUCKET)
        with self.assertRaises(ValueError):
            refuse_misplaced_dataset(CANONICAL_100GB, REHEARSAL_DATA_BUCKET)

    def test_only_isolated_datasets_override_the_terraform_bucket(self):
        self.assertIsNone(isolated_data_bucket(CANONICAL_100GB))
        self.assertIsNone(isolated_data_bucket("cloud-smoke-v1"))
        self.assertEqual(CANONICAL_DATA_BUCKET, apply_dataset_bucket(RUNTIME, CANONICAL_100GB).data_bucket)
        self.assertEqual("x", apply_dataset_bucket(load_cloud_runtime(terraform_outputs("x")), "cloud-smoke-v1").data_bucket)
        self.assertEqual(REHEARSAL_DATA_BUCKET, apply_dataset_bucket(RUNTIME, REHEARSAL_5GB).data_bucket)
        self.assertEqual("temp-bucket", apply_dataset_bucket(RUNTIME, REHEARSAL_5GB).temporary_bucket)

    def test_event_logs_follow_the_resolved_bucket(self):
        for version, bucket in ((REHEARSAL_5GB, REHEARSAL_DATA_BUCKET), (CANONICAL_100GB, CANONICAL_DATA_BUCKET)):
            with self.subTest(version):
                runtime = apply_dataset_bucket(RUNTIME, version)
                properties = build_dataproc_batch_properties(
                    performance_tier="standard", data_bucket=runtime.data_bucket, profile="baseline", stage="dp2-gold")
                self.assertIn(f"spark.eventLog.dir=gs://{bucket}/spark-events", properties)

    def test_mixed_bucket_roots_fail_closed(self):
        self.assertEqual("b", require_same_bucket("gs://b/x/silver", "gs://b/y/gold"))
        with self.assertRaisesRegex(ValueError, "share one bucket"):
            require_same_bucket("gs://a/x/silver", "gs://b/y/gold")

    def test_validators_refuse_mixed_bucket_roots(self):
        pyspark = __import__("importlib").util.find_spec("pyspark")
        if pyspark is None:
            self.skipTest("pyspark unavailable")
        from data_platform.quality import validate_gold, validate_silver

        gcp = load_runtime_config("gcp", environ={**ENV, "EWALLET_DATA_BUCKET": REHEARSAL_DATA_BUCKET})
        runs = f"gs://{REHEARSAL_DATA_BUCKET}/lakehouse/runs"
        other = f"gs://{CANONICAL_DATA_BUCKET}/lakehouse/runs"
        with self.assertRaisesRegex(ValueError, "share one bucket"):
            validate_gold.resolve_storage(gcp, scope="core-gold", silver_root=f"{runs}/s/silver",
                                          gold_root=f"{other}/g/gold", feature_gold_root=None)
        with self.assertRaisesRegex(ValueError, "share one bucket"):
            validate_silver.apply_explicit_roots(gcp, bronze_root=f"{other}/b/bronze", silver_root=f"{runs}/s/silver")


class GeneratorResolutionTest(unittest.TestCase):
    def run_dry(self, environ):
        output = io.StringIO()
        with patch.dict(os.environ, environ, clear=False):
            code = generate_source_dataset.main(PLAN_ARGS, stdout=output)
        return code, output.getvalue()

    def test_dry_run_resolves_the_rehearsal_root_without_any_bucket_variable(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EWALLET_DATA_BUCKET", None)
            code, text = self.run_dry(ENV)
        self.assertEqual(0, code)
        self.assertIn(f"source_root: gs://{REHEARSAL_DATA_BUCKET}/source/{REHEARSAL_5GB}", text)
        self.assertIn("DRY RUN", text)
        self.assertIn("422c15a169165158fcd7d432a7943d50812b5f3e55e7d0c6aec7c457371c6c83", text)

    def test_a_conflicting_bucket_variable_is_refused(self):
        with self.assertRaisesRegex(ValueError, "must use bucket"):
            self.run_dry({**ENV, "EWALLET_DATA_BUCKET": CANONICAL_DATA_BUCKET})

    def test_generator_and_dp1_resolve_the_same_source_root(self):
        from ops.gcp.submit_source_to_bronze import build_submission_command

        runtime = apply_dataset_bucket(RUNTIME, REHEARSAL_5GB)
        command = build_submission_command(runtime, dataset_version=REHEARSAL_5GB, run_id=run_id("baseline", "bronze"),
                                           ttl="8h", spark_profile="baseline", benchmark_id=BENCH, git_sha=SHA, campaign=True)
        self.assertIn(f"--source-manifest-uri={resolve_storage_context(REHEARSAL_5GB).source_root}/_control/dataset_manifest.json", command)

    def test_execute_stops_before_generation_when_the_bucket_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = Path(directory) / "t.json"
            outputs.write_text(json.dumps(terraform_outputs()), encoding="utf-8")
            argv = [*PLAN_ARGS, "--execute", f"--terraform-output-json={outputs}", f"--staging-dir={directory}"]
            with patch.dict(os.environ, ENV, clear=False), patch.object(
                generate_source_dataset, "check_bucket", side_effect=StorageNotReady("absent")
            ), patch.object(generate_source_dataset, "GcloudObjectStore") as store:
                with self.assertRaises(StorageNotReady):
                    generate_source_dataset.main(argv, stdout=io.StringIO())
                store.assert_not_called()


def runner(describe=None, describe_error="not found: 404", iam=None):
    calls = []

    def run(command, **kwargs):
        calls.append([str(item) for item in command])
        if command[3] == "describe":
            if describe is None:
                return subprocess.CompletedProcess(command, 1, "", describe_error)
            return subprocess.CompletedProcess(command, 0, json.dumps(describe), "")
        if command[3] == "get-iam-policy":
            return subprocess.CompletedProcess(command, 0, json.dumps({"bindings": iam or []}), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    run.calls = calls
    return run


GOOD = {"location": "ASIA-SOUTHEAST1", "uniform_bucket_level_access": True, "public_access_prevention": "enforced",
        "versioning_enabled": True}
SPARK_IAM = [{"role": "roles/storage.objectUser", "members": ["serviceAccount:spark@example.com"]}]
PLAN = StoragePlan("ewallet-data-ai-platform-hg26", REHEARSAL_DATA_BUCKET, "spark@example.com")


class ProvisioningTest(unittest.TestCase):
    def test_a_ready_bucket_passes(self):
        self.assertEqual("READY", check_bucket(PLAN, runner(GOOD, iam=SPARK_IAM))["status"])

    def test_a_missing_bucket_reports_the_blocker_code(self):
        with self.assertRaisesRegex(StorageNotReady, "GCS_PUBLICATION_BLOCKED_BY_BUCKET_PROVISIONING"):
            check_bucket(PLAN, runner(None))

    def test_a_non_404_error_is_not_mistaken_for_absence(self):
        with self.assertRaisesRegex(StorageNotReady, "could not inspect"):
            check_bucket(PLAN, runner(None, describe_error="403 forbidden"))

    def test_each_defect_is_reported(self):
        cases = {
            "location": dict(GOOD, location="US"),
            "uniform": dict(GOOD, uniform_bucket_level_access=False),
            "public access": dict(GOOD, public_access_prevention="inherited"),
            "versioning": dict(GOOD, versioning_enabled=False),
        }
        for message, bucket in cases.items():
            with self.subTest(message), self.assertRaisesRegex(StorageNotReady, message):
                check_bucket(PLAN, runner(bucket, iam=SPARK_IAM))
        with self.assertRaisesRegex(StorageNotReady, "does not hold"):
            check_bucket(PLAN, runner(GOOD, iam=[]))
        with self.assertRaisesRegex(StorageNotReady, "public IAM"):
            check_bucket(PLAN, runner(GOOD, iam=[*SPARK_IAM, {"role": "roles/storage.objectViewer", "members": ["allUsers"]}]))
        conditional = [{"role": "roles/storage.objectUser", "members": ["serviceAccount:spark@example.com"],
                        "condition": {"expression": "x"}}]
        with self.assertRaisesRegex(StorageNotReady, "does not hold"):
            check_bucket(PLAN, runner(GOOD, iam=conditional))

    def test_commands_create_only_the_rehearsal_bucket_with_safe_defaults(self):
        create, versioning, iam = provisioning_commands(PLAN)
        self.assertIn(f"gs://{REHEARSAL_DATA_BUCKET}", create)
        for flag in ("--uniform-bucket-level-access", "--public-access-prevention", "--location=ASIA-SOUTHEAST1"):
            self.assertIn(flag, create)
        self.assertIn("--versioning", versioning)
        self.assertEqual("--role=roles/storage.objectUser", iam[-1])
        self.assertNotIn(CANONICAL_DATA_BUCKET, " ".join([*create, *versioning, *iam]))
        with self.assertRaisesRegex(ValueError, "canonical"):
            provisioning_commands(StoragePlan("p", CANONICAL_DATA_BUCKET, "s"))

    def test_apply_needs_execute_and_the_exact_bucket_name(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = Path(directory) / "t.json"
            outputs.write_text(json.dumps(terraform_outputs()), encoding="utf-8")
            for extra in ([], ["--execute"], ["--execute", "--confirm-bucket", "wrong"]):
                with self.subTest(extra):
                    fake = runner(None)
                    self.assertEqual(1, rehearsal_storage.main(["apply", f"--terraform-output-json={outputs}", *extra], runner=fake))
                    self.assertEqual([], fake.calls)

    def test_plan_and_check_change_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = Path(directory) / "t.json"
            outputs.write_text(json.dumps(terraform_outputs()), encoding="utf-8")
            fake = runner(None)
            self.assertEqual(0, rehearsal_storage.main(["plan", f"--terraform-output-json={outputs}"], runner=fake))
            self.assertEqual([], fake.calls)
            self.assertEqual(1, rehearsal_storage.main(["check", f"--terraform-output-json={outputs}"], runner=fake))
            self.assertEqual(["describe"], [call[3] for call in fake.calls])

    def test_explicit_identity_needs_no_terraform_and_a_terraform_failure_is_clean(self):
        fake = runner(None)
        args = ["plan", "--project-id", "p", "--spark-service-account", "spark@example.com"]
        self.assertEqual(0, rehearsal_storage.main(args, runner=fake))
        self.assertEqual([], fake.calls)

        def failing(command, **kwargs):
            raise subprocess.CalledProcessError(1, command)

        self.assertEqual(1, rehearsal_storage.main(["plan"], runner=failing))

    def test_apply_is_idempotent_and_verifies(self):
        created = runner(None, iam=SPARK_IAM)
        with self.assertRaises(StorageNotReady):  # the fake bucket never appears
            rehearsal_storage.apply_plan(PLAN, created)
        self.assertEqual(["describe", "create", "update", "add-iam-policy-binding", "describe"],
                         [call[3] for call in created.calls])
        existing = runner(GOOD, iam=SPARK_IAM)
        self.assertEqual("READY", rehearsal_storage.apply_plan(PLAN, existing)["status"])
        self.assertNotIn("create", [call[3] for call in existing.calls])


class CanonicalRegressionTest(unittest.TestCase):
    def test_canonical_paths_are_unchanged(self):
        from ops.gcp.submit_silver_to_gold import build_submission_command, run_roots

        silver, gold = run_roots(RUNTIME, source_silver_run_id="silver-run-1", run_id="gold-run-22")
        self.assertEqual(f"gs://{CANONICAL_DATA_BUCKET}/lakehouse/runs/silver-run-1/silver", silver)
        self.assertEqual(f"gs://{CANONICAL_DATA_BUCKET}/lakehouse/runs/gold-run-22/gold", gold)
        command = build_submission_command(RUNTIME, dataset_version=CANONICAL_100GB, source_silver_run_id="silver-run-1",
                                           run_id="gold-run-22")
        self.assertIn("--expected-table-count=11", command)
        self.assertNotIn("--include-merchant-scd2", command)
        self.assertFalse([item for item in command if item.startswith("--execution-record-uri")])
        self.assertIn("--version=2.2", command)

    def test_stage_validation_uses_the_resolved_bucket(self):
        runtime = apply_dataset_bucket(RUNTIME, REHEARSAL_5GB)
        runs = submit_stage_validation.ValidationRuns(run_id("baseline", "bronze"))
        command = submit_stage_validation.build_submission_command(
            runtime, scope="bronze", dataset_version=REHEARSAL_5GB, benchmark_id=BENCH, spark_profile="baseline",
            git_sha=SHA, runs=runs)
        joined = " ".join(command)
        self.assertIn(f"--report-uri=gs://{REHEARSAL_DATA_BUCKET}/lakehouse/runs/", joined)
        self.assertIn(f"--execution-record-uri=gs://{REHEARSAL_DATA_BUCKET}/lakehouse/runs/", joined)
        self.assertIn(f"spark.eventLog.dir=gs://{REHEARSAL_DATA_BUCKET}/spark-events", joined)
        self.assertNotIn(CANONICAL_DATA_BUCKET, joined)


if __name__ == "__main__":
    unittest.main()
