from __future__ import annotations

import unittest
from pathlib import Path

from ops.gcp.dataproc_batch_properties import (
    CANONICAL_PERFORMANCE_PROFILE,
    DEFAULT_PERFORMANCE_PROFILE,
    build_dataproc_batch_properties,
)
from ops.gcp.submit_bronze_to_silver import (
    build_submission_command as build_silver_command,
)
from ops.gcp.submit_historical_features import (
    build_submission_command as build_feature_command,
)
from ops.gcp.submit_silver_to_gold import (
    build_submission_command as build_gold_command,
)
from ops.gcp.submit_source_to_bronze import (
    CloudRuntime,
    build_submission_command as build_bronze_command,
)


CANONICAL_PROPERTIES = {
    "spark.dynamicAllocation.profile=performance",
    "spark.dynamicAllocation.initialExecutors=10",
    "spark.dynamicAllocation.maxExecutors=20",
}


def _runtime() -> CloudRuntime:
    return CloudRuntime(
        project_id="example-project",
        region="asia-southeast1",
        data_bucket="example-project-data",
        temporary_bucket="example-project-temp",
        subnet=(
            "projects/example-project/regions/asia-southeast1/"
            "subnetworks/example"
        ),
        service_account_email=(
            "ewallet-spark@example-project.iam.gserviceaccount.com"
        ),
        performance_tier="standard",
    )


def _properties(command: tuple[str, ...]) -> set[str]:
    value = next(item for item in command if item.startswith("--properties="))
    return set(value.removeprefix("--properties=").split(","))


class DataprocPerformanceProfileTest(unittest.TestCase):
    def test_default_profile_preserves_existing_bounded_properties(self) -> None:
        properties = set(
            build_dataproc_batch_properties(
                performance_tier="standard",
                data_bucket="example-project-data",
                profile=DEFAULT_PERFORMANCE_PROFILE,
            )
        )

        self.assertTrue(CANONICAL_PROPERTIES.isdisjoint(properties))
        self.assertIn("dataproc.tier=standard", properties)
        self.assertNotIn("dataproc.tier=premium", properties)

    def test_canonical_profile_adds_only_approved_resource_properties(self) -> None:
        default = set(
            build_dataproc_batch_properties(
                performance_tier="standard",
                data_bucket="example-project-data",
                profile=DEFAULT_PERFORMANCE_PROFILE,
            )
        )
        canonical = set(
            build_dataproc_batch_properties(
                performance_tier="standard",
                data_bucket="example-project-data",
                profile=CANONICAL_PERFORMANCE_PROFILE,
            )
        )

        self.assertEqual(CANONICAL_PROPERTIES, canonical - default)
        self.assertEqual(default, canonical - CANONICAL_PROPERTIES)

    def test_all_dp_submitters_accept_the_same_canonical_profile(self) -> None:
        runtime = _runtime()
        common = {
            "runtime": runtime,
            "dataset_version": "canonical-100gb-v1",
            "jar_paths": (Path("delta.jar"),),
            "ttl": "8h",
            "performance_profile": CANONICAL_PERFORMANCE_PROFILE,
        }
        commands = (
            build_bronze_command(
                **common,
                run_id="canonical-bronze-20261024-120000",
            ),
            build_silver_command(
                **common,
                source_bronze_run_id="canonical-bronze-20261024-120000",
                run_id="canonical-silver-20261024-120000",
            ),
            build_gold_command(
                **common,
                source_silver_run_id="canonical-silver-20261024-120000",
                run_id="canonical-gold-20261024-120000",
            ),
            build_feature_command(
                **common,
                source_silver_run_id="canonical-silver-20261024-120000",
                source_core_gold_run_id="canonical-gold-20261024-120000",
                run_id="canonical-features-20261024-120000",
            ),
        )

        for command in commands:
            properties = _properties(command)
            self.assertTrue(CANONICAL_PROPERTIES.issubset(properties))
            self.assertIn("dataproc.tier=standard", properties)
            self.assertNotIn("dataproc.tier=premium", properties)
            self.assertIn("--ttl=8h", command)

    def test_submitter_defaults_do_not_enable_canonical_profile(self) -> None:
        command = build_bronze_command(
            _runtime(),
            dataset_version="r4-pilot-1gb-v1",
            run_id="r4-bronze-20261024-120000",
            jar_paths=(Path("delta.jar"),),
        )

        self.assertTrue(CANONICAL_PROPERTIES.isdisjoint(_properties(command)))


if __name__ == "__main__":
    unittest.main()
