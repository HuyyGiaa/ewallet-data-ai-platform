from __future__ import annotations

import unittest
from pathlib import Path

from ops.gcp.submit_silver_to_gold import build_submission_command as gold_command
from ops.gcp.submit_source_to_bronze import load_cloud_runtime


OUTPUTS = {
    "project_id": {"value": "example-project"},
    "region": {"value": "asia-southeast1"},
    "data_bucket_name": {"value": "example-project-data"},
    "temporary_bucket_name": {"value": "example-project-temp"},
    "service_account_emails": {
        "value": {"spark": "ewallet-spark@example-project.iam.gserviceaccount.com"}
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
                "projects/example-project/regions/asia-southeast1/subnetworks/dev"
            ),
        }
    },
}


class SubmitGoldCloudTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = load_cloud_runtime(OUTPUTS)

    def test_core_command_has_immutable_roots_and_eleven_tables(self) -> None:
        command = gold_command(
            self.runtime,
            dataset_version="r4-pilot-1gb-v1",
            source_silver_run_id="s1b-silver-configfix-20261022-174352",
            run_id="s1b-gold-20261023-000000",
            script_path=Path("gold.py"),
            archive_path=Path("job.zip"),
            runtime_profile_path=Path("platform.yaml"),
            jar_paths=(Path("delta.jar"),),
        )
        self.assertIn("--expected-table-count=11", command)
        self.assertIn(
            "--silver-root=gs://example-project-data/lakehouse/runs/"
            "s1b-silver-configfix-20261022-174352/silver",
            command,
        )
        self.assertIn(
            "--gold-root=gs://example-project-data/lakehouse/runs/"
            "s1b-gold-20261023-000000/gold",
            command,
        )

    def test_core_command_accepts_explicit_canonical_ttl(self) -> None:
        command = gold_command(
            self.runtime,
            dataset_version="canonical-100gb-v1",
            source_silver_run_id="canonical-silver-20261023-120000",
            run_id="canonical-gold-20261023-120000",
            ttl="8h",
            jar_paths=(Path("delta.jar"),),
        )

        self.assertIn("--ttl=8h", command)

if __name__ == "__main__":
    unittest.main()
