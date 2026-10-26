"""Run-scoped contract between canonical DP3, Feast, and bounded training."""

from __future__ import annotations

import re
from dataclasses import dataclass

from data_platform.config.spark_profiles import resolve_spark_profile, validate_git_sha


FEATURE_TABLES = (
    "feat_user_behavior",
    "feat_account_behavior",
    "feat_device_behavior",
    "feat_merchant_behavior",
)
CANONICAL_TRAINING_SAMPLE_MODULUS = 10_000

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,127}$")
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,221}[a-z0-9]$")
_AMBIGUOUS_IDS = {"active", "current", "latest"}
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _validate_id(value: str, field: str) -> str:
    if value in _AMBIGUOUS_IDS or not _ID_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field} must be an immutable lowercase run identifier, got {value!r}"
        )
    return value


@dataclass(frozen=True)
class CloudFeastTrainingBoundary:
    """All immutable roots and lineage IDs for one bounded training snapshot."""

    bucket: str
    dataset_version: str
    silver_run_id: str
    feature_run_id: str
    snapshot_id: str
    source_git_sha: str
    spark_profile: str
    feature_selection_digest: str

    def __post_init__(self) -> None:
        if self.bucket.startswith("gs://") or not _BUCKET_PATTERN.fullmatch(
            self.bucket
        ):
            raise ValueError(f"bucket must be a bare valid GCS bucket name: {self.bucket!r}")
        for field in (
            "dataset_version",
            "silver_run_id",
            "feature_run_id",
            "snapshot_id",
        ):
            _validate_id(getattr(self, field), field)
        if self.silver_run_id == self.feature_run_id:
            raise ValueError("silver_run_id and feature_run_id must differ")
        validate_git_sha(self.source_git_sha)
        resolve_spark_profile(self.spark_profile)
        if not _DIGEST_PATTERN.fullmatch(self.feature_selection_digest):
            raise ValueError("feature_selection_digest must be sha256:<64 hex>")

    @property
    def silver_root(self) -> str:
        return f"gs://{self.bucket}/lakehouse/runs/{self.silver_run_id}/silver"

    @property
    def feature_gold_root(self) -> str:
        return f"gs://{self.bucket}/lakehouse/runs/{self.feature_run_id}/gold"

    @property
    def feast_snapshot_root(self) -> str:
        return (
            f"gs://{self.bucket}/features/offline/snapshots/"
            f"{self.dataset_version}/{self.feature_run_id}/{self.snapshot_id}"
        )

    @property
    def feast_registry_uri(self) -> str:
        return (
            f"gs://{self.bucket}/features/registries/"
            f"{self.dataset_version}/{self.snapshot_id}/registry.db"
        )

    @property
    def training_snapshot_root(self) -> str:
        return (
            f"gs://{self.bucket}/ml/training-snapshots/"
            f"{self.dataset_version}/{self.snapshot_id}"
        )

    def feast_environment(self) -> dict[str, str]:
        return {
            "EWALLET_FEAST_OFFLINE_DATA_ROOT": self.feast_snapshot_root,
            "EWALLET_FEAST_REGISTRY": self.feast_registry_uri,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "cloud-feast-training-boundary-v1",
            "dataset_version": self.dataset_version,
            "source_silver_run_id": self.silver_run_id,
            "source_feature_run_id": self.feature_run_id,
            "source_git_sha": self.source_git_sha,
            "spark_profile": self.spark_profile,
            "feature_selection_digest": self.feature_selection_digest,
            "snapshot_id": self.snapshot_id,
            "inputs": {
                "silver_root": self.silver_root,
                "feature_gold_root": self.feature_gold_root,
                "feature_tables": list(FEATURE_TABLES),
            },
            "outputs": {
                "feast_snapshot_root": self.feast_snapshot_root,
                "feast_registry_uri": self.feast_registry_uri,
                "training_snapshot_root": self.training_snapshot_root,
            },
            "temporal_contract": "[T-window,T)",
            "labels_enter_feast": False,
            "bounded_training_sample_modulus": CANONICAL_TRAINING_SAMPLE_MODULUS,
        }
