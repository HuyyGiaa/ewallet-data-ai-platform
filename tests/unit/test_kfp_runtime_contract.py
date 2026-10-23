from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from ai_platform.ml.pipelines.runtime import verify_chronological_splits
from ai_platform.ml.tracking.snapshot import SnapshotProvenance


def _frame(timestamp: str, labels: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "transaction_id": [f"tx-{timestamp}-{index}" for index in range(len(labels))],
            "event_timestamp": [pd.Timestamp(timestamp, tz="UTC")] * len(labels),
            "label": labels,
        }
    )


class KfpRuntimeGateTest(unittest.TestCase):
    def test_split_gate_matches_actual_rows_classes_and_chronology(self) -> None:
        dataset = SimpleNamespace(
            train=_frame("2026-01-01", [0, 1]),
            validation=_frame("2026-04-20", [0, 1]),
            test=_frame("2026-05-20", [0, 1]),
        )
        provenance = SnapshotProvenance(
            dataset_version="canonical-100gb-v1",
            silver_run_id="silver-1",
            feature_run_id="features-1",
            snapshot_id="snapshot-1",
            training_snapshot_uri="gs://bucket/snapshot-1",
            manifest_sha256="a" * 64,
            row_count=6,
            split_counts={"train": 2, "validation": 2, "test": 2},
            positive_count=3,
            negative_count=3,
            feature_count=41,
            feature_schema_sha256="b" * 64,
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "ai_platform.ml.pipelines.runtime._download_snapshot",
            return_value=Path(temporary),
        ), patch(
            "ai_platform.ml.pipelines.runtime._validate_manifest",
            return_value=(None, provenance),
        ), patch(
            "ai_platform.ml.pipelines.runtime.load_dataset",
            return_value=dataset,
        ):
            output = Path(temporary) / "gate.json"
            verify_chronological_splits(
                Namespace(snapshot_uri=provenance.training_snapshot_uri, output=output)
            )
            self.assertIn('"status": "PASS"', output.read_text())

    def test_split_gate_fails_when_actual_rows_differ_from_manifest(self) -> None:
        dataset = SimpleNamespace(
            train=_frame("2026-01-01", [0, 1]),
            validation=_frame("2026-04-20", [0, 1]),
            test=_frame("2026-05-20", [0, 1]),
        )
        provenance = SnapshotProvenance(
            dataset_version="canonical-100gb-v1",
            silver_run_id="silver-1",
            feature_run_id="features-1",
            snapshot_id="snapshot-1",
            training_snapshot_uri="gs://bucket/snapshot-1",
            manifest_sha256="a" * 64,
            row_count=7,
            split_counts={"train": 3, "validation": 2, "test": 2},
            positive_count=3,
            negative_count=4,
            feature_count=41,
            feature_schema_sha256="b" * 64,
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "ai_platform.ml.pipelines.runtime._download_snapshot",
            return_value=Path(temporary),
        ), patch(
            "ai_platform.ml.pipelines.runtime._validate_manifest",
            return_value=(None, provenance),
        ), patch(
            "ai_platform.ml.pipelines.runtime.load_dataset",
            return_value=dataset,
        ):
            with self.assertRaisesRegex(ValueError, "Actual train rows"):
                verify_chronological_splits(
                    Namespace(
                        snapshot_uri=provenance.training_snapshot_uri,
                        output=Path(temporary) / "gate.json",
                    )
                )


if __name__ == "__main__":
    unittest.main()
