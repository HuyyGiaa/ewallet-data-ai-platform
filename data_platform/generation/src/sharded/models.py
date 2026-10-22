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

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ShardPlan":
        return cls(
            schema_version=int(value["schema_version"]),
            dataset_version=str(value["dataset_version"]),
            global_seed=int(value["global_seed"]),
            shard_count=int(value["shard_count"]),
            users_per_shard=int(value["users_per_shard"]),
            merchants_per_shard=int(value["merchants_per_shard"]),
            transactions_per_shard=int(value["transactions_per_shard"]),
            base_config_sha256=str(value["base_config_sha256"]),
            digest=str(value["digest"]),
        )


@dataclass(frozen=True)
class ShardFileMetadata:
    """Content-addressed metadata for one physical Parquet file."""

    table: str
    relative_path: str
    rows: int
    bytes: int
    sha256: str
    md5_base64: str
    schema_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ShardFileMetadata":
        return cls(
            table=str(value["table"]),
            relative_path=str(value["relative_path"]),
            rows=int(value["rows"]),
            bytes=int(value["bytes"]),
            sha256=str(value["sha256"]),
            md5_base64=str(value["md5_base64"]),
            schema_sha256=str(value["schema_sha256"]),
        )


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

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ShardManifest":
        raw_counts = value["fraud_scenario_counts"]
        if not isinstance(raw_counts, Mapping):
            raise ValueError("fraud_scenario_counts must be a mapping")
        raw_files = value["files"]
        if not isinstance(raw_files, list):
            raise ValueError("files must be a list")
        return cls(
            schema_version=int(value["schema_version"]),
            dataset_version=str(value["dataset_version"]),
            plan_digest=str(value["plan_digest"]),
            shard_id=int(value["shard_id"]),
            derived_seed=int(value["derived_seed"]),
            logical_transactions=int(value["logical_transactions"]),
            physical_transactions=int(value["physical_transactions"]),
            fraud_scenario_counts={
                str(name): int(count) for name, count in raw_counts.items()
            },
            files=tuple(ShardFileMetadata.from_dict(item) for item in raw_files),
        )
