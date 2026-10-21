from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import BytesIO
from io import StringIO
from pathlib import Path

from ops.gcp.submit_dataproc_delta_smoke import (
    DELTA_DEPENDENCIES,
    PinnedDependency,
    build_submission_command,
    ensure_delta_dependencies,
    load_runtime_from_terraform_output,
    main,
    validate_evidence,
)


TERRAFORM_OUTPUTS = {
    "project_id": {"sensitive": False, "type": "string", "value": "example-project"},
    "region": {"sensitive": False, "type": "string", "value": "asia-southeast1"},
    "temporary_bucket_name": {
        "sensitive": False,
        "type": "string",
        "value": "example-project-temp",
    },
    "service_account_emails": {
        "sensitive": False,
        "type": ["object", {}],
        "value": {
            "spark": "ewallet-spark@example-project.iam.gserviceaccount.com",
            "generator": "ewallet-generator@example-project.iam.gserviceaccount.com",
        },
    },
    "dataproc_batch_runtime": {
        "sensitive": False,
        "type": ["object", {}],
        "value": {
            "execution_mode": "serverless-batch",
            "performance_tier": "standard",
            "project_id": "example-project",
            "region": "asia-southeast1",
            "service_account_email": (
                "ewallet-spark@example-project.iam.gserviceaccount.com"
            ),
            "staging_bucket": "example-project-temp",
            "subnet": (
                "projects/example-project/regions/asia-southeast1/subnetworks/"
                "ewallet-dev-asia-southeast1"
            ),
        },
    },
}


class DataprocSmokeSubmissionTest(unittest.TestCase):
    def test_terraform_outputs_produce_one_exact_bounded_submission(self) -> None:
        runtime = load_runtime_from_terraform_output(TERRAFORM_OUTPUTS)
        command = build_submission_command(
            runtime,
            "r3-smoke-20261022-012345",
            script_path=Path("ops/gcp/dataproc_delta_smoke.py"),
        )

        self.assertEqual("gcloud", command[0])
        self.assertIn("--batch=r3-smoke-20261022-012345", command)
        self.assertIn("--project=example-project", command)
        self.assertIn("--region=asia-southeast1", command)
        self.assertIn("--version=2.2", command)
        self.assertIn(
            "--service-account=ewallet-spark@example-project.iam.gserviceaccount.com",
            command,
        )
        self.assertIn(
            "--subnet=projects/example-project/regions/asia-southeast1/"
            "subnetworks/ewallet-dev-asia-southeast1",
            command,
        )
        self.assertIn("--staging-bucket=example-project-temp", command)
        self.assertNotIn("--staging-bucket=gs://example-project-temp", command)
        self.assertIn("--deps-bucket=gs://example-project-temp", command)
        jars = next(item for item in command if item.startswith("--jars="))
        self.assertIn("delta-spark_2.13-3.2.0.jar", jars)
        self.assertIn("delta-storage-3.2.0.jar", jars)
        self.assertIn("antlr4-runtime-4.9.3.jar", jars)
        self.assertNotIn("spark.jars.packages", " ".join(command))
        self.assertIn("--ttl=20m", command)
        self.assertIn(
            "--run-uri=gs://example-project-temp/dataproc-smoke/delta/"
            "r3-smoke-20261022-012345",
            command,
        )
        properties = next(item for item in command if item.startswith("--properties="))
        self.assertIn("dataproc.tier=standard", properties)
        self.assertNotIn("spark.dataproc.tier", properties)
        self.assertIn("io.delta.sql.DeltaSparkSessionExtension", properties)
        self.assertIn("org.apache.spark.sql.delta.catalog.DeltaCatalog", properties)
        self.assertNotIn("shell=True", " ".join(command))

    def test_missing_or_inconsistent_outputs_fail_before_submission(self) -> None:
        missing = dict(TERRAFORM_OUTPUTS)
        missing.pop("temporary_bucket_name")
        with self.assertRaisesRegex(ValueError, "temporary_bucket_name"):
            load_runtime_from_terraform_output(missing)

        inconsistent = json.loads(json.dumps(TERRAFORM_OUTPUTS))
        inconsistent["dataproc_batch_runtime"]["value"]["region"] = "us-central1"
        with self.assertRaisesRegex(ValueError, "region"):
            load_runtime_from_terraform_output(inconsistent)

    def test_default_cli_is_dry_run_and_never_invokes_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            outputs_path = Path(temporary_directory) / "outputs.json"
            outputs_path.write_text(json.dumps(TERRAFORM_OUTPUTS), encoding="utf-8")

            def forbidden_runner(*args, **kwargs):
                raise AssertionError("dry-run attempted an external command")

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--run-id",
                        "r3-smoke-20261022-012345",
                        "--terraform-output-json",
                        str(outputs_path),
                    ],
                    runner=forbidden_runner,
                )

        self.assertEqual(0, exit_code)
        self.assertIn("DRY RUN", stdout.getvalue())
        self.assertIn("gcloud dataproc batches submit pyspark", stdout.getvalue())

    def test_remote_evidence_requires_all_delta_and_precision_checks(self) -> None:
        evidence = {
            "status": "PASS",
            "runtime_version": "2.2",
            "initial_rows": 4,
            "final_rows": 6,
            "distinct_transaction_ids": 6,
            "amount_sum": 210.75,
            "initial_delta_version": 0,
            "final_delta_version": 1,
            "microsecond_timestamp": "2026-01-01T00:00:00.123456+00:00",
            "timestamp_precision_pass": True,
            "schema": [
                {"name": "transaction_id", "type": "string"},
                {"name": "user_id", "type": "string"},
                {"name": "amount", "type": "double"},
                {"name": "event_timestamp", "type": "timestamp"},
            ],
        }
        self.assertEqual(evidence, validate_evidence(evidence))

        broken = dict(evidence, timestamp_precision_pass=False)
        with self.assertRaisesRegex(ValueError, "timestamp_precision_pass"):
            validate_evidence(broken)

        broken_schema = dict(evidence, schema=evidence["schema"][:-1])
        with self.assertRaisesRegex(ValueError, "schema"):
            validate_evidence(broken_schema)

    def test_delta_dependencies_are_pinned_and_checksum_verified(self) -> None:
        payload = b"pinned-test-dependency"
        dependency = PinnedDependency(
            filename="test-dependency.jar",
            url="https://example.invalid/test-dependency.jar",
            sha256=hashlib.sha256(payload).hexdigest(),
        )

        def opener(url: str, **kwargs):
            self.assertEqual(dependency.url, url)
            return BytesIO(payload)

        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = ensure_delta_dependencies(
                Path(temporary_directory),
                dependencies=(dependency,),
                opener=opener,
            )

            self.assertEqual(1, len(paths))
            self.assertTrue(all(path.is_file() for path in paths))

            def broken_opener(url: str, **kwargs):
                return BytesIO(b"tampered")

            paths[0].unlink()
            with self.assertRaisesRegex(ValueError, "checksum"):
                ensure_delta_dependencies(
                    Path(temporary_directory),
                    dependencies=(dependency,),
                    opener=broken_opener,
                )


if __name__ == "__main__":
    unittest.main()
