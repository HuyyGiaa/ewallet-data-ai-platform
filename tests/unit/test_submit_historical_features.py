from __future__ import annotations

import unittest
from pathlib import Path

from ops.gcp.submit_historical_features import build_submission_command
from ops.gcp.submit_source_to_bronze import load_cloud_runtime
from tests.unit.test_submit_gold_cloud import OUTPUTS


class SubmitHistoricalFeaturesTest(unittest.TestCase):
    def test_command_has_both_source_roots_and_four_tables(self) -> None:
        command = build_submission_command(
            load_cloud_runtime(OUTPUTS),
            dataset_version="r4-pilot-1gb-v1",
            source_silver_run_id="s1b-silver-configfix-20261022-174352",
            source_core_gold_run_id="s1b-gold-20261022-180212",
            run_id="s1b-features-20261023-010000",
            script_path=Path("features.py"),
            archive_path=Path("job.zip"),
            runtime_profile_path=Path("platform.yaml"),
            jar_paths=(Path("delta.jar"),),
        )
        self.assertIn("--expected-table-count=4", command)
        self.assertIn(
            "--source-core-gold-root=gs://example-project-data/lakehouse/runs/"
            "s1b-gold-20261022-180212/gold",
            command,
        )
        self.assertIn(
            "--feature-gold-root=gs://example-project-data/lakehouse/runs/"
            "s1b-features-20261023-010000/gold",
            command,
        )

    def test_command_accepts_explicit_canonical_ttl(self) -> None:
        command = build_submission_command(
            load_cloud_runtime(OUTPUTS),
            dataset_version="canonical-100gb-v1",
            source_silver_run_id="canonical-silver-20261023-120000",
            source_core_gold_run_id="canonical-gold-20261023-120000",
            run_id="canonical-features-20261023-120000",
            ttl="8h",
            jar_paths=(Path("delta.jar"),),
        )

        self.assertIn("--ttl=8h", command)


if __name__ == "__main__":
    unittest.main()
