from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from ops.gcp.package_python_job import build_python_archive
from ops.gcp.submit_source_to_bronze import (
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
            "subnet": "projects/example-project/regions/asia-southeast1/subnetworks/dev",
        }
    },
}


class SourceToBronzeSubmissionTest(unittest.TestCase):
    def test_command_pins_runtime_identity_paths_delta_and_event_log(self) -> None:
        runtime = load_cloud_runtime(TERRAFORM_OUTPUTS)
        command = build_submission_command(
            runtime,
            dataset_version="r4-pilot-1gb-v1",
            run_id="r4-bronze-20261022-170000",
            script_path=Path("data_platform/ingestion/spark/source_to_bronze.py"),
            archive_path=Path("tmp/ewallet-python.zip"),
            jar_paths=(Path("delta.jar"),),
        )

        joined = " ".join(command)
        self.assertIn("--version=2.2", command)
        self.assertIn("--service-account=ewallet-spark@example-project.iam.gserviceaccount.com", command)
        self.assertIn("--staging-bucket=example-project-temp", command)
        self.assertIn("--deps-bucket=gs://example-project-temp", command)
        self.assertIn("--py-files=tmp/ewallet-python.zip", command)
        self.assertIn("--jars=delta.jar", command)
        self.assertIn("spark.eventLog.enabled=true", joined)
        self.assertIn("spark.eventLog.dir=gs://example-project-data/spark-events", joined)
        self.assertIn("--dataset-version=r4-pilot-1gb-v1", command)
        self.assertIn(
            "--source-manifest-uri=gs://example-project-data/source/"
            "r4-pilot-1gb-v1/_control/dataset_manifest.json",
            command,
        )
        self.assertIn(
            "--bronze-root=gs://example-project-data/lakehouse/runs/"
            "r4-bronze-20261022-170000/bronze",
            command,
        )

    def test_command_accepts_explicit_canonical_ttl(self) -> None:
        runtime = load_cloud_runtime(TERRAFORM_OUTPUTS)
        command = build_submission_command(
            runtime,
            dataset_version="canonical-100gb-v1",
            run_id="canonical-bronze-20261023-120000",
            ttl="8h",
            jar_paths=(Path("delta.jar"),),
        )

        self.assertIn("--ttl=8h", command)

        with self.assertRaisesRegex(ValueError, "ttl"):
            build_submission_command(
                runtime,
                dataset_version="canonical-100gb-v1",
                run_id="canonical-bronze-20261023-120000",
                ttl="8h --quiet",
                jar_paths=(Path("delta.jar"),),
            )

    def test_default_cli_is_dry_run_and_does_not_call_runner(self) -> None:
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
                        "--run-id",
                        "r4-bronze-20261022-170000",
                        "--terraform-output-json",
                        str(path),
                    ],
                    runner=forbidden_runner,
                )

        self.assertEqual(0, result)
        self.assertIn("DRY RUN", stdout.getvalue())

    def test_python_archive_has_only_runtime_python_and_no_sensitive_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "repo"
            (root / "data_platform").mkdir(parents=True)
            (root / "ops").mkdir()
            (root / "infra").mkdir()
            (root / ".git").mkdir()
            (root / "docs/superpowers").mkdir(parents=True)
            (root / "data_platform/job.py").write_text("x = 1\n", encoding="utf-8")
            (root / "data_platform/.env").write_text("SECRET=x\n", encoding="utf-8")
            (root / "ops/helper.py").write_text("y = 2\n", encoding="utf-8")
            (root / "infra/terraform.tfstate").write_text("{}", encoding="utf-8")
            (root / ".git/config").write_text("secret", encoding="utf-8")
            (root / "docs/superpowers/plan.py").write_text("z=3", encoding="utf-8")
            archive = Path(temporary_directory) / "job.zip"

            build_python_archive(root, archive)
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())

        self.assertEqual(
            {"data_platform/job.py", "ops/helper.py"}, names
        )


if __name__ == "__main__":
    unittest.main()
