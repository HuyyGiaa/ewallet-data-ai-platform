"""Dependency-neutral contract checks for bounded Silver training sources."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

SOURCE_OUTPUT_COLUMNS = (
    "transaction_id",
    "event_timestamp_us",
    "user_id",
    "account_id",
    "device_id",
    "merchant_id",
    "amount",
    "type",
    "channel",
    "merchant_present",
    "label",
    "fraud_type",
    "split",
)


def validate_source_manifest_contract(
    manifest: Mapping[str, object],
    *,
    dataset_version: str,
    source_silver_run_id: str,
    source_feature_run_id: str,
    snapshot_id: str,
    source_git_sha: str,
    feature_selection_digest: str,
    expected_output_columns: Sequence[str],
) -> None:
    expected = {
        "schema_version": "f7-training-source-v1",
        "status": "COMPLETE",
        "scope": "hash_bounded",
        "sample_modulus": 10_000,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("training source manifest is incomplete or incompatible")
    if manifest.get("output_columns") != list(expected_output_columns):
        raise ValueError("training source output-column contract mismatch")
    if not isinstance(manifest.get("written_rows"), int) or manifest["written_rows"] <= 0:
        raise ValueError("training source manifest has no completed rows")
    profile = manifest.get("selected_split_profile")
    if not isinstance(profile, Mapping) or set(profile) != {
        "train",
        "validation",
        "test",
    }:
        raise ValueError("training source split profile is incomplete")
    lineage = manifest.get("lineage")
    expected_lineage = {
        "dataset_version": dataset_version,
        "source_silver_run_id": source_silver_run_id,
        "source_feature_run_id": source_feature_run_id,
        "snapshot_id": snapshot_id,
        "source_git_sha": source_git_sha,
        "feature_selection_digest": feature_selection_digest,
    }
    if not isinstance(lineage, Mapping) or any(
        lineage.get(key) != value for key, value in expected_lineage.items()
    ):
        raise ValueError("training source lineage does not match requested snapshot")
