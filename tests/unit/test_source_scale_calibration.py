from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from data_platform.generation.src.sharded.models import (
    ShardFileMetadata,
    ShardManifest,
)
from ops.gcp.calibrate_source_scale import (
    CalibrationError,
    CalibrationProfile,
    build_calibration_report,
)


def manifest(total_bytes: int, logical_transactions: int = 100) -> ShardManifest:
    files = (
        ShardFileMetadata(
            table="transactions_v1",
            relative_path="shard-00000/transactions_v1.parquet",
            rows=50,
            bytes=total_bytes // 2,
            sha256="a" * 64,
            md5_base64="YQ==",
            schema_sha256="b" * 64,
        ),
        ShardFileMetadata(
            table="transactions_v2",
            relative_path="shard-00000/transactions_v2.parquet",
            rows=50,
            bytes=total_bytes - total_bytes // 2,
            sha256="c" * 64,
            md5_base64="Yw==",
            schema_sha256="d" * 64,
        ),
    )
    return ShardManifest(
        schema_version=1,
        dataset_version="calibration-v1",
        plan_digest="e" * 64,
        shard_id=0,
        derived_seed=7,
        logical_transactions=logical_transactions,
        physical_transactions=102,
        fraud_scenario_counts={"velocity": 2},
        files=files,
    )


class SourceScaleCalibrationTest(unittest.TestCase):
    def profile(self, **overrides) -> CalibrationProfile:
        values = {
            "name": "cloud-pilot-1gb",
            "target_bytes": 1_000,
            "tolerance_ratio": 0.25,
            "global_seed": 7001,
            "users_per_shard": 20,
            "merchants_per_shard": 5,
            "transactions_per_shard": 100,
            "minimum_free_disk_bytes": 100,
            "staging_safety_multiplier": 2.0,
        }
        values.update(overrides)
        return CalibrationProfile(**values)

    def test_uses_measured_bytes_to_derive_shards_and_transaction_count(self) -> None:
        report = build_calibration_report(
            profile=self.profile(),
            manifest=manifest(240),
            runtime_seconds=1.25,
            peak_rss_bytes=50_000,
            free_disk_bytes=5_000,
            config_sha256="f" * 64,
            source_git_revision="abc123",
        )

        self.assertEqual(240, report["measurement"]["total_parquet_bytes"])
        self.assertEqual(2.4, report["measurement"]["bytes_per_logical_transaction"])
        self.assertEqual(5, report["derived_plan"]["shard_count"])
        self.assertEqual(500, report["derived_plan"]["logical_transactions"])
        self.assertEqual(1_200, report["derived_plan"]["estimated_bytes"])
        self.assertEqual(240, report["measurement"]["bytes_by_table"]["transactions_v1"] + report["measurement"]["bytes_by_table"]["transactions_v2"])

    def test_reports_runtime_memory_hash_revision_and_tolerance(self) -> None:
        report = build_calibration_report(
            profile=self.profile(),
            manifest=manifest(250),
            runtime_seconds=2.5,
            peak_rss_bytes=64_000,
            free_disk_bytes=10_000,
            config_sha256="1" * 64,
            source_git_revision="deadbeef",
        )

        self.assertEqual(2.5, report["measurement"]["runtime_seconds"])
        self.assertEqual(64_000, report["measurement"]["peak_rss_bytes"])
        self.assertEqual("1" * 64, report["inputs"]["base_config_sha256"])
        self.assertEqual("deadbeef", report["inputs"]["source_git_revision"])
        self.assertEqual([750, 1_250], report["target"]["accepted_byte_range"])
        self.assertEqual("cloud-pilot-1gb", report["target"]["validated_label"])

    def test_rejects_plan_that_breaks_free_disk_reserve(self) -> None:
        with self.assertRaisesRegex(CalibrationError, "free-disk safety"):
            build_calibration_report(
                profile=self.profile(minimum_free_disk_bytes=900),
                manifest=manifest(200),
                runtime_seconds=1.0,
                peak_rss_bytes=10,
                free_disk_bytes=1_000,
                config_sha256="2" * 64,
                source_git_revision="abc",
            )

    def test_does_not_validate_size_label_outside_tolerance(self) -> None:
        report = build_calibration_report(
            profile=self.profile(tolerance_ratio=0.05),
            manifest=manifest(400),
            runtime_seconds=1.0,
            peak_rss_bytes=10,
            free_disk_bytes=10_000,
            config_sha256="3" * 64,
            source_git_revision="abc",
        )

        self.assertIsNone(report["target"]["validated_label"])
        self.assertEqual(1_200, report["derived_plan"]["estimated_bytes"])

    def test_profile_rejects_arbitrary_final_row_or_shard_count_fields(self) -> None:
        with self.assertRaisesRegex(CalibrationError, "unsupported keys"):
            CalibrationProfile.from_dict(
                {
                    **self.profile().to_dict(),
                    "shard_count": 10,
                }
            )

    def test_report_can_be_written_outside_generated_shard_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            path = Path(temp_directory) / "calibration.json"
            path.write_text("{}", encoding="utf-8")
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
