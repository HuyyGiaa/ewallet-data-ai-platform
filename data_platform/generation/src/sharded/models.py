"""Value objects shared by source shard planning and publication."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ShardPlan:
    """Immutable geometry and identity for one source dataset."""

    schema_version: int
    dataset_version: str
    global_seed: int
    shard_count: int
    users_per_shard: int
    merchants_per_shard: int
    transactions_per_shard: int
    base_config_sha256: str
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShardFileMetadata:
    """Content-addressed metadata for one physical Parquet file."""

    table: str
    relative_path: str
    rows: int
    bytes: int
    sha256: str
    schema_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShardManifest:
    """Validation result for one complete deterministic cohort shard."""

    schema_version: int
    dataset_version: str
    plan_digest: str
    shard_id: int
    derived_seed: int
    logical_transactions: int
    physical_transactions: int
    fraud_scenario_counts: Mapping[str, int]
    files: tuple[ShardFileMetadata, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset_version": self.dataset_version,
            "plan_digest": self.plan_digest,
            "shard_id": self.shard_id,
            "derived_seed": self.derived_seed,
            "logical_transactions": self.logical_transactions,
            "physical_transactions": self.physical_transactions,
            "fraud_scenario_counts": dict(self.fraud_scenario_counts),
            "files": [item.to_dict() for item in self.files],
        }
