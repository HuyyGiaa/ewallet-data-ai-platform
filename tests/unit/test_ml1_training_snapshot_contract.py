from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS
from ai_platform.ml.training.dvc_revision import (
    build_revision_descriptor,
    write_revision_descriptor,
)
from ai_platform.ml.training.export_training_source import bounded_sampling_sql
from ai_platform.ml.training.materialize import materialize
from ops.gcp.prepare_training_snapshot_dvc import main as dvc_main


def publication_evidence() -> dict[str, object]:
    objects = [
        {
            "name": "manifest.json",
            "generation": 11,
            "sha256": "1" * 64,
            "size_bytes": 100,
        },
        {
            "name": "split=train/part-00000.parquet",
            "generation": 12,
            "sha256": "2" * 64,
            "size_bytes": 200,
        },
    ]
    return {
        "schema_version": "training-snapshot-publication-evidence-v1",
        "status": "PUBLISHED_AND_VERIFIED",
        "mode": "create-only",
        "training_snapshot_root": (
            "gs://bucket/ml/training-snapshots/canonical-100gb-v1/"
            "training-20261031-ml1"
        ),
        "dataset_version": "canonical-100gb-v1",
        "source_silver_run_id": "canonical-silver-run",
        "source_feature_run_id": "canonical-feature-run",
        "snapshot_id": "training-20261031-ml1",
        "feast_snapshot_id": "canonical-training-v1-recovered",
        "source_git_sha": "a" * 40,
        "feature_selection_digest": "sha256:" + "b" * 64,
        "execution_git_sha": "c" * 40,
        "registry_uri": "gs://bucket/features/registries/registry.db",
        "registry_generation": 1793255806863364,
        "registry_sha256": "d" * 64,
        "manifest_sha256": "1" * 64,
        "completion_marker_generation": 13,
        "completion_marker_sha256": "e" * 64,
        "object_count": len(objects),
        "objects": objects,
    }


class ML1TrainingSnapshotContractTest(unittest.TestCase):
    def test_sampling_predicate_is_frozen_and_label_independent(self) -> None:
        expression = bounded_sampling_sql(10_000)
        self.assertEqual(
            "pmod(xxhash64(event_timestamp_us), 10000) = 0", expression
        )
        self.assertNotIn("label", expression)
        with self.assertRaisesRegex(ValueError, "positive"):
            bounded_sampling_sql(0)

    def test_dvc_descriptor_tracks_gcs_provenance_without_data_copy(self) -> None:
        evidence = publication_evidence()
        descriptor = build_revision_descriptor(evidence)
        self.assertEqual(
            evidence["training_snapshot_root"], descriptor["training_snapshot_uri"]
        )
        self.assertEqual(evidence["manifest_sha256"], descriptor["manifest_sha256"])
        self.assertNotIn("objects", descriptor)
        canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(),
            descriptor["publication_evidence_sha256"],
        )

    def test_dvc_rejects_unpublished_or_ambiguous_snapshot(self) -> None:
        for mutation in ("status", "same-id", "missing-manifest", "bad-registry"):
            with self.subTest(mutation=mutation):
                evidence = publication_evidence()
                if mutation == "status":
                    evidence["status"] = "INCOMPLETE"
                elif mutation == "same-id":
                    evidence["feast_snapshot_id"] = evidence["snapshot_id"]
                elif mutation == "missing-manifest":
                    evidence["objects"] = evidence["objects"][1:]
                    evidence["object_count"] = 1
                else:
                    evidence["registry_sha256"] = "wrong"
                with self.assertRaises(ValueError):
                    build_revision_descriptor(evidence)

    def test_dvc_dry_run_creates_no_pointer_or_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "not-created-until-ml2.json"
            descriptor = root / "training-snapshot-revision.json"
            output = io.StringIO()
            with redirect_stdout(output):
                result = dvc_main(
                    [
                        "--publication-evidence",
                        str(evidence),
                        "--descriptor-output",
                        str(descriptor),
                    ]
                )
            self.assertEqual(0, result)
            self.assertIn("DRY RUN", output.getvalue())
            self.assertFalse(evidence.exists())
            self.assertFalse(descriptor.exists())
            self.assertFalse(Path(str(descriptor) + ".dvc").exists())

    def test_descriptor_creation_is_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "publication.json"
            descriptor = root / "revision.json"
            evidence.write_text(json.dumps(publication_evidence()), encoding="utf-8")
            write_revision_descriptor(evidence, descriptor)
            with self.assertRaises(FileExistsError):
                write_revision_descriptor(evidence, descriptor)

    def test_bounded_materialization_resumes_without_duplicate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "dataset"
            timestamps = {
                "train": 1_760_000_000_000_000,
                "validation": 1_777_000_000_000_000,
                "test": 1_780_000_000_000_000,
            }
            for index, (split, timestamp) in enumerate(timestamps.items()):
                directory = source / f"split={split}"
                directory.mkdir(parents=True)
                pd.DataFrame(
                    {
                        "transaction_id": [f"tx-{index}"],
                        "event_timestamp_us": [timestamp],
                        "user_id": ["user"],
                        "account_id": ["account"],
                        "device_id": ["device"],
                        "merchant_id": [None if split == "test" else "merchant"],
                        "amount": [10.0],
                        "type": ["payment"],
                        "channel": ["mobile"],
                        "merchant_present": [split != "test"],
                        "label": [0],
                        "fraud_type": [None],
                        "split": [split],
                    }
                ).to_parquet(directory / "part-00000.parquet", index=False)
            (source / "manifest.json").write_text(
                json.dumps(
                    {
                        "written_rows": 3,
                        "scope": "hash_bounded",
                        "source_git_head": "a" * 40,
                        "label_integrity": {"transactions": 3},
                    }
                )
            )

            def historical(_store, entity_frame):
                frame = pd.DataFrame(
                    {
                        column: [1.0] * len(entity_frame)
                        for column in HISTORICAL_FEATURE_COLUMNS
                    }
                )
                frame["user_behavior__user_avg_amount_30d"] = 1.0
                frame["user_behavior__user_std_amount_30d"] = 1.0
                frame["account_behavior__account_avg_amount_30d"] = 1.0
                frame["account_behavior__account_std_amount_30d"] = 1.0
                frame["user_behavior__user_tx_count_1h"] = 1.0
                frame["account_behavior__account_tx_count_1h"] = 1.0
                frame["merchant_behavior__merchant_tx_count_10m"] = 1.0
                frame["merchant_behavior__merchant_tx_count_24h"] = 1.0
                return frame

            common = {
                "source": source,
                "output": output,
                "chunk_size": 1,
                "min_available_gib": 0.0,
                "max_rows": None,
                "max_new_chunks": 1,
            }
            with patch(
                "ai_platform.ml.training.materialize.get_store", return_value=object()
            ), patch(
                "ai_platform.ml.training.materialize.retrieve_combined",
                side_effect=historical,
            ):
                result = materialize(resume=False, **common)
                self.assertFalse(result["complete"])
                while (output / "_INCOMPLETE").exists():
                    result = materialize(resume=True, **common)
            self.assertEqual(3, result["materialized_rows"])
            rows = sum(
                len(pd.read_parquet(path))
                for path in output.glob("split=*/*.parquet")
            )
            self.assertEqual(3, rows)
            with patch(
                "ai_platform.ml.training.materialize.get_store", return_value=object()
            ), self.assertRaisesRegex(ValueError, "_INCOMPLETE"):
                materialize(resume=True, **common)


if __name__ == "__main__":
    unittest.main()
