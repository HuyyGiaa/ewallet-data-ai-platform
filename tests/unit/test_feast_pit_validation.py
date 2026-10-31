from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.features.feast.validation import canonical_sample
from ai_platform.features.feast.validation.contracts import (
    CANONICAL_SAMPLE_CASES,
    require_registry_readback_evidence,
    require_validation_export_manifest,
)


def canonical_frame() -> pd.DataFrame:
    rows = []
    for index, case in enumerate(CANONICAL_SAMPLE_CASES):
        is_payment = case == "payment_with_merchant"
        without_merchant = case == "non_payment_without_merchant"
        row = {
            "sample_case": case,
            "transaction_id": f"tx-{index}",
            "user_id": f"user-{index}",
            "account_id": f"account-{index}",
            "device_id": f"device-{index}",
            "merchant_id": None if without_merchant else f"merchant-{index}",
            "event_timestamp": pd.Timestamp(
                f"2026-01-{index + 1:02d} 00:00:00", tz="UTC"
            ),
            "type": "payment" if is_payment else "transfer",
            "label": int(index > 0),
            "fraud_type": None if index == 0 else case,
        }
        for view_name, spec in FEATURE_SPECS.items():
            for feature_index, feature in enumerate(spec["features"]):
                row[f"expected__{view_name}__{feature}"] = float(
                    index * 100 + feature_index
                )
        rows.append(row)
    return pd.DataFrame(rows)


def retrieved_frame(entity_df: pd.DataFrame, sample: pd.DataFrame) -> pd.DataFrame:
    result = entity_df.copy()
    sample_by_case = sample.sort_values("sample_case").reset_index(drop=True)
    for view_name, spec in FEATURE_SPECS.items():
        for feature in spec["features"]:
            result[f"{view_name}__{feature}"] = sample_by_case[
                f"expected__{view_name}__{feature}"
            ]
    return result


class FeastPitValidationTest(unittest.TestCase):
    def validate(self, sample: pd.DataFrame, *, mutate_retrieved=None) -> tuple[dict, tuple]:
        observed = {}

        def retrieve(_store, entity_df):
            observed["columns"] = tuple(entity_df.columns)
            result = retrieved_frame(entity_df, sample)
            if mutate_retrieved is not None:
                result = mutate_retrieved(result)
            return result

        with (
            patch.object(canonical_sample.pd, "read_parquet", return_value=sample),
            patch.object(canonical_sample, "get_store", return_value=object()),
            patch.object(canonical_sample, "retrieve_combined", side_effect=retrieve),
        ):
            result = canonical_sample.validate_canonical_sample(Path("unused"))
        return result, observed["columns"]

    def test_complete_sample_compares_exactly_310_values_without_labels(self) -> None:
        result, retrieval_columns = self.validate(canonical_frame())

        self.assertEqual("PASS", result["status"])
        self.assertEqual(10, result["sample_rows"])
        self.assertEqual(10, result["distinct_sample_cases"])
        self.assertEqual(31, result["historical_features_per_case"])
        self.assertEqual(310, result["comparable_values"])
        self.assertEqual(0, result["mismatch_count"])
        self.assertEqual(canonical_sample.ENTITY_COLUMNS, retrieval_columns)
        self.assertNotIn("label", retrieval_columns)
        self.assertNotIn("fraud_type", retrieval_columns)

    def test_incomplete_sample_is_rejected_before_retrieval(self) -> None:
        sample = canonical_frame().iloc[:-1].copy()
        with patch.object(canonical_sample.pd, "read_parquet", return_value=sample):
            with self.assertRaisesRegex(ValueError, "exactly 10 rows"):
                canonical_sample.validate_canonical_sample(Path("unused"))

    def test_duplicate_case_hiding_missing_case_is_rejected(self) -> None:
        sample = canonical_frame()
        sample.loc[9, "sample_case"] = sample.loc[0, "sample_case"]
        with patch.object(canonical_sample.pd, "read_parquet", return_value=sample):
            with self.assertRaisesRegex(ValueError, "case coverage mismatch"):
                canonical_sample.validate_canonical_sample(Path("unused"))

    def test_missing_expected_feature_column_is_rejected(self) -> None:
        sample = canonical_frame().drop(
            columns="expected__user_behavior__user_tx_count_5m"
        )
        with patch.object(canonical_sample.pd, "read_parquet", return_value=sample):
            with self.assertRaisesRegex(ValueError, "expected feature columns missing"):
                canonical_sample.validate_canonical_sample(Path("unused"))

    def test_unexpected_expected_feature_column_is_rejected(self) -> None:
        sample = canonical_frame()
        sample["expected__user_behavior__unfrozen_feature"] = 1
        with patch.object(canonical_sample.pd, "read_parquet", return_value=sample):
            with self.assertRaisesRegex(ValueError, "unexpected expected-feature"):
                canonical_sample.validate_canonical_sample(Path("unused"))

    def test_missing_actual_feature_column_is_rejected(self) -> None:
        def remove_feature(result: pd.DataFrame) -> pd.DataFrame:
            return result.drop(columns="user_behavior__user_tx_count_5m")

        with self.assertRaisesRegex(ValueError, "retrieval feature columns missing"):
            self.validate(canonical_frame(), mutate_retrieved=remove_feature)

    def test_value_mismatch_fails_closed(self) -> None:
        def change_value(result: pd.DataFrame) -> pd.DataFrame:
            result.loc[0, "user_behavior__user_tx_count_5m"] = -1.0
            return result

        result, _ = self.validate(canonical_frame(), mutate_retrieved=change_value)
        self.assertEqual("FAIL", result["status"])
        self.assertEqual(310, result["comparable_values"])
        self.assertEqual(1, result["mismatch_count"])

    def test_naive_timestamp_is_rejected(self) -> None:
        sample = canonical_frame()
        sample["event_timestamp"] = sample["event_timestamp"].dt.tz_localize(None)
        with patch.object(canonical_sample.pd, "read_parquet", return_value=sample):
            with self.assertRaisesRegex(ValueError, "timezone-aware UTC"):
                canonical_sample.validate_canonical_sample(Path("unused"))

    def test_nullable_merchant_case_semantics_are_enforced(self) -> None:
        sample = canonical_frame()
        sample.loc[
            sample["sample_case"] == "non_payment_without_merchant", "merchant_id"
        ] = "unexpected-merchant"
        with patch.object(canonical_sample.pd, "read_parquet", return_value=sample):
            with self.assertRaisesRegex(ValueError, "invalid merchant semantics"):
                canonical_sample.validate_canonical_sample(Path("unused"))

    def test_validation_manifest_requires_exact_lineage_and_counts(self) -> None:
        lineage = {
            "dataset_version": "canonical-100gb-v1",
            "snapshot_id": "snapshot-recovered",
        }
        manifest = {
            "schema_version": "feast-validation-samples-v1",
            "status": "COMPLETE",
            "canonical_sample_rows": 10,
            "canonical_sample_cases": list(CANONICAL_SAMPLE_CASES),
            "historical_feature_count": 31,
            "expected_comparable_values": 310,
            "benchmark_rows": 100,
            "lineage": dict(lineage),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text(json.dumps(manifest))
            observed = require_validation_export_manifest(
                root, expected_lineage=lineage
            )
            self.assertEqual(310, observed["expected_comparable_values"])
            manifest["lineage"]["snapshot_id"] = "wrong-snapshot"
            (root / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "lineage mismatch"):
                require_validation_export_manifest(root, expected_lineage=lineage)

    def test_registry_evidence_is_bound_to_published_generation_and_sha(self) -> None:
        expected_sha = "d3" * 32
        evidence = {
            "schema_version": "feast-registry-operation-evidence-v1",
            "status": "READBACK_VERIFIED",
            "mode": "readback-only",
            "registry_uri": "gs://bucket/registry.db",
            "snapshot_id": "snapshot-recovered",
            "snapshot_root": "gs://bucket/snapshot",
            "execution_git_sha": "8" * 40,
            "registry_generation": 19,
            "registry_sha256": expected_sha,
            "historical_feature_count": 31,
            "model_input_count": 41,
            "feature_views": [{"name": name} for name in FEATURE_SPECS],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "registry-readback.json"
            path.write_text(json.dumps(evidence))
            observed = require_registry_readback_evidence(
                path,
                expected_registry_uri="gs://bucket/registry.db",
                expected_snapshot_id="snapshot-recovered",
                expected_snapshot_root="gs://bucket/snapshot",
                expected_execution_git_sha="8" * 40,
                expected_registry_generation=19,
                expected_registry_sha256=expected_sha,
            )
            self.assertEqual(19, observed["registry_generation"])
            with self.assertRaisesRegex(ValueError, "identity is incompatible"):
                require_registry_readback_evidence(
                    path,
                    expected_registry_uri="gs://bucket/registry.db",
                    expected_snapshot_id="snapshot-recovered",
                    expected_snapshot_root="gs://bucket/snapshot",
                    expected_execution_git_sha="8" * 40,
                    expected_registry_generation=20,
                    expected_registry_sha256=expected_sha,
                )


if __name__ == "__main__":
    unittest.main()
