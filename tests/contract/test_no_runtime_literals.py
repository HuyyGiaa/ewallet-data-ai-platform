from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from data_platform.config import load_runtime_config
from ops.validation.validate_runtime_config import scan_runtime_literals


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class RuntimeLiteralContractTest(unittest.TestCase):
    def test_operational_sources_have_no_stale_runtime_literals(self) -> None:
        violations = scan_runtime_literals(REPOSITORY_ROOT)
        self.assertEqual([], violations, "\n".join(map(str, violations)))

    def test_scanner_reports_relative_file_line_and_literal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "data_platform" / "stale.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                'SILVER = "s3a://silver-zone/transactions"\n',
                encoding="utf-8",
            )

            violations = scan_runtime_literals(root)

        self.assertEqual(1, len(violations))
        self.assertEqual("data_platform/stale.py", violations[0].path)
        self.assertEqual(1, violations[0].line)
        self.assertEqual("s3a://silver-zone", violations[0].literal)

    def test_local_allowlist_does_not_allow_cloud_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            local = root / "configs" / "environments" / "local" / "platform.yaml"
            gcp = root / "configs" / "environments" / "gcp" / "platform.yaml"
            local.parent.mkdir(parents=True)
            gcp.parent.mkdir(parents=True)
            local.write_text("gold_root: s3a://gold-zone\n", encoding="utf-8")
            gcp.write_text("gold_root: s3a://gold-zone\n", encoding="utf-8")

            violations = scan_runtime_literals(root)

        self.assertEqual(1, len(violations))
        self.assertEqual(
            "configs/environments/gcp/platform.yaml",
            violations[0].path,
        )

    def test_training_source_reader_uses_selected_profile(self) -> None:
        from ai_platform.ml.training.export_source_sample import read_delta

        config = load_runtime_config(
            "gcp",
            environ={
                "GCP_PROJECT_ID": "contract-project",
                "EWALLET_DATA_BUCKET": "contract-bucket",
                "DATAHUB_GMS_URL": "https://datahub.example.test",
            },
        )
        spark = MagicMock()

        read_delta(spark, config, "transactions")

        spark.read.format.assert_called_once_with("delta")
        spark.read.format.return_value.load.assert_called_once_with(
            "gs://contract-bucket/lakehouse/silver/transactions"
        )


if __name__ == "__main__":
    unittest.main()
