from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from ops.gcp.submit_bronze_to_silver import (
    assert_silver_prefix_absent,
    build_submission_command,
    load_cloud_runtime,
    main,
)


TERRAFORM_OUTPUTS = {
    "project_id": {"value": "example-project"},
    "region": {"value": "asia-southeast1"},
    "data_bucket_name": {"value": "example-project-data"},
    "temporary_bucket_name": {"value": "example-project-temp"},
    "service_account_emails": {
        "value": {
            "spark": "ewallet-spark@example-project.iam.gserviceaccount.com"
        }
    },
    "dataproc_batch_runtime": {
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
                "projects/example-project/regions/asia-southeast1/"
                "subnetworks/dev"
            ),
        }
    },
}


class BronzeToSilverSubmissionTest(unittest.TestCase):
    def test_existing_silver_prefix_is_rejected_before_submission(self) -> None:
        runtime = load_cloud_runtime(TERRAFORM_OUTPUTS)

        class ExistingPrefix:
            returncode = 0
            stdout = "gs://example-project-data/lakehouse/runs/run/silver/users/\n"
            stderr = ""

        with self.assertRaisesRegex(ValueError, "already exists"):
            assert_silver_prefix_absent(
                runtime,
                run_id="s1a-silver-20261022-120000",
                runner=lambda *args, **kwargs: ExistingPrefix(),
            )

    def test_prefix_check_does_not_treat_cloud_error_as_absent(self) -> None:
        runtime = load_cloud_runtime(TERRAFORM_OUTPUTS)

        class PermissionFailure:
            returncode = 1
            stdout = ""
            stderr = "ERROR: permission denied"

        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            assert_silver_prefix_absent(
                runtime,
                run_id="s1a-silver-20261022-120000",
                runner=lambda *args, **kwargs: PermissionFailure(),
            )

    def test_missing_silver_prefix_is_allowed(self) -> None:
        runtime = load_cloud_runtime(TERRAFORM_OUTPUTS)

        class MissingPrefix:
            returncode = 1
            stdout = ""
            stderr = "One or more URLs matched no objects."

        assert_silver_prefix_absent(
            runtime,
            run_id="s1a-silver-20261022-120000",
            runner=lambda *args, **kwargs: MissingPrefix(),
        )

    def test_command_pins_lineage_run_roots_and_all_eight_tables(self) -> None:
        runtime = load_cloud_runtime(TERRAFORM_OUTPUTS)
        command = build_submission_command(
            runtime,
            dataset_version="r4-pilot-1gb-v1",
            source_bronze_run_id="r4-bronze-20261022-095608",
            run_id="s1a-silver-20261022-120000",
            script_path=Path(
                "data_platform/processing/spark/silver/silver_pipeline.py"
            ),
            archive_path=Path("tmp/ewallet-python.zip"),
            runtime_profile_path=Path("configs/gcp-platform.yaml"),
            jar_paths=(Path("delta.jar"),),
        )

        joined = " ".join(command)
        self.assertIn("--version=2.2", command)
        self.assertIn("--project=example-project", command)
        self.assertIn("--region=asia-southeast1", command)
        self.assertIn(
            "--service-account=ewallet-spark@"
            "example-project.iam.gserviceaccount.com",
            command,
        )
        self.assertIn(
            "--subnet=projects/example-project/regions/asia-southeast1/"
            "subnetworks/dev",
            command,
        )
        self.assertIn("--dataset-version=r4-pilot-1gb-v1", command)
        self.assertIn(
            "--source-bronze-run-id=r4-bronze-20261022-095608",
            command,
        )
        self.assertIn(
            "--bronze-root=gs://example-project-data/lakehouse/runs/"
            "r4-bronze-20261022-095608/bronze",
            command,
        )
        self.assertIn(
            "--silver-root=gs://example-project-data/lakehouse/runs/"
            "s1a-silver-20261022-120000/silver",
            command,
        )
        self.assertIn("--include-fraud-labels", command)
        self.assertIn("--expected-table-count=8", command)
        self.assertIn("--environment=gcp", command)
        self.assertIn("--files=configs/gcp-platform.yaml", command)
        self.assertIn("--runtime-config-file=gcp-platform.yaml", command)
        self.assertIn("spark.sql.session.timeZone=UTC", joined)

    def test_command_accepts_explicit_canonical_ttl(self) -> None:
        runtime = load_cloud_runtime(TERRAFORM_OUTPUTS)
        command = build_submission_command(
            runtime,
            dataset_version="canonical-100gb-v1",
            source_bronze_run_id="canonical-bronze-20261023-120000",
            run_id="canonical-silver-20261023-120000",
            ttl="8h",
            jar_paths=(Path("delta.jar"),),
        )

        self.assertIn("--ttl=8h", command)

    def test_output_run_must_differ_from_source_bronze_run(self) -> None:
        runtime = load_cloud_runtime(TERRAFORM_OUTPUTS)

        with self.assertRaisesRegex(ValueError, "different"):
            build_submission_command(
                runtime,
                dataset_version="r4-pilot-1gb-v1",
                source_bronze_run_id="r4-bronze-20261022-095608",
                run_id="r4-bronze-20261022-095608",
            )

    def test_default_cli_prints_complete_dry_run_without_submitting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "outputs.json"
            path.write_text(json.dumps(TERRAFORM_OUTPUTS), encoding="utf-8")

            def forbidden_runner(*args, **kwargs):
                raise AssertionError("dry run attempted an external command")

            stdout = StringIO()
            with redirect_stdout(stdout):
                result = main(
                    [
                        "--dataset-version",
                        "r4-pilot-1gb-v1",
                        "--source-bronze-run-id",
                        "r4-bronze-20261022-095608",
                        "--run-id",
                        "s1a-silver-20261022-120000",
                        "--terraform-output-json",
                        str(path),
                    ],
                    runner=forbidden_runner,
                )

        output = stdout.getvalue()
        self.assertEqual(0, result)
        self.assertIn("DRY RUN", output)
        self.assertIn("Dataset version: r4-pilot-1gb-v1", output)
        self.assertIn("Source Bronze run: r4-bronze-20261022-095608", output)
        self.assertIn("Bronze input root: gs://example-project-data/", output)
        self.assertIn("Silver output root: gs://example-project-data/", output)
        self.assertIn("Project: example-project", output)
        self.assertIn("Region: asia-southeast1", output)
        self.assertIn("Subnet: projects/example-project/", output)
        self.assertIn(
            "Service account: ewallet-spark@"
            "example-project.iam.gserviceaccount.com",
            output,
        )
        self.assertIn("Tables (8):", output)
        self.assertIn("fraud_labels", output)
        self.assertIn("gcloud dataproc batches submit pyspark", output)


if __name__ == "__main__":
    unittest.main()
