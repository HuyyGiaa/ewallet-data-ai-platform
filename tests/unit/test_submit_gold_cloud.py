from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from ops.gcp.prepare_spark_stage_pair import build_stage_pair_manifest
from ops.gcp.submit_silver_to_gold import (
    _load_outputs,
    assert_batch_id_absent,
    build_submission_command as gold_command,
    require_clean_repository,
)
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

    def test_recovery_batch_is_distinct_from_immutable_output_identity(self) -> None:
        benchmark = "ab-20261029174838"
        output_run = f"{benchmark}-baseline-gold-r261030135156"
        source_run = f"{benchmark}-optimized-silver-r261030135156"
        manifest = build_stage_pair_manifest(
            stage="dp2-gold", dataset_version="canonical-100gb-v1",
            git_sha="a" * 40, upstream_run_ids=(source_run,),
            baseline_output_run_id=output_run,
            baseline_output_root=(
                f"gs://example-project-data/lakehouse/runs/{output_run}/gold"
            ),
            candidate_output_run_id=f"{benchmark}-optimized-gold-r261030135156",
            candidate_output_root=(
                "gs://example-project-data/lakehouse/runs/"
                f"{benchmark}-optimized-gold-r261030135156/gold"
            ),
        )
        attempt = "dp2g-b-recovery-r261030192000"
        command = gold_command(
            self.runtime, dataset_version="canonical-100gb-v1",
            source_silver_run_id=source_run, run_id=output_run,
            dataproc_batch_id=attempt, recovery_git_sha="b" * 40,
            ttl="8h", benchmark_id=benchmark, spark_profile="baseline",
            git_sha="a" * 40, pair_manifest=manifest,
            script_path=Path("/canonical/data_platform/processing/spark/gold/gold_pipeline.py"),
            runtime_profile_path=Path("/canonical/configs/environments/gcp/platform.yaml"),
            jar_paths=(Path("delta.jar"),),
        )
        self.assertIn(f"--batch={attempt}", command)
        self.assertIn(f"--run-id={output_run}", command)
        labels = next(item for item in command if item.startswith("--labels="))
        self.assertIn(f"output_run_id={output_run}", labels)
        self.assertIn("git_sha=" + "a" * 40, labels)
        self.assertIn("recovery_git_sha=" + "b" * 40, labels)
        properties = next(item for item in command if item.startswith("--properties="))
        self.assertIn(manifest["pair_manifest_digest"], properties)
        self.assertIn(output_run, properties)

    def test_recovery_attempt_refuses_duplicate_or_ambiguous_lookup(self) -> None:
        existing = Mock(return_value=SimpleNamespace(
            returncode=0, stdout='{"state":"CANCELLED"}', stderr=""
        ))
        with self.assertRaisesRegex(ValueError, "already exists"):
            assert_batch_id_absent(
                self.runtime, batch_id="dp2g-b-recovery-r261030192000",
                runner=existing,
            )
        unavailable = Mock(return_value=SimpleNamespace(
            returncode=1, stdout="", stderr="permission denied"
        ))
        with self.assertRaisesRegex(RuntimeError, "Could not verify"):
            assert_batch_id_absent(
                self.runtime, batch_id="dp2g-b-recovery-r261030192000",
                runner=unavailable,
            )

    def test_compute_and_controller_git_sources_are_checked_independently(self) -> None:
        runner = Mock(side_effect=[
            SimpleNamespace(stdout="a" * 40 + "\n"),
            SimpleNamespace(stdout=""),
        ])
        require_clean_repository(Path("/canonical"), "a" * 40, runner)
        self.assertEqual(Path("/canonical"), runner.call_args_list[0].kwargs["cwd"])
        dirty = Mock(side_effect=[
            SimpleNamespace(stdout="a" * 40 + "\n"),
            SimpleNamespace(stdout=" M changed.py\n"),
        ])
        with self.assertRaisesRegex(ValueError, "declared clean Git source"):
            require_clean_repository(Path("/canonical"), "a" * 40, dirty)

    def test_recovery_can_read_terraform_state_from_canonical_compute_repo(self) -> None:
        runner = Mock(return_value=SimpleNamespace(stdout='{"project_id": {}}'))
        root = Path("/canonical/infra/terraform/gcp/environments/dev")
        self.assertEqual({"project_id": {}}, _load_outputs(
            None, runner, terraform_root=root
        ))
        self.assertEqual(
            ["terraform", f"-chdir={root}", "output", "-json"],
            runner.call_args.args[0],
        )

if __name__ == "__main__":
    unittest.main()
