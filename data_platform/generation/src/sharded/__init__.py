"""Deterministic bounded-memory source dataset generation."""

from .models import ShardFileMetadata, ShardManifest, ShardPlan
from .planner import ShardPlanError, build_shard_plan, derive_shard_seed


def __getattr__(name: str):
    """Keep metadata-only tools usable without loading the PyArrow runtime."""
    if name == "generate_shard":
        from .generator import generate_shard
        return generate_shard
    raise AttributeError(name)

__all__ = [
    "ShardFileMetadata",
    "ShardManifest",
    "ShardPlan",
    "ShardPlanError",
    "build_shard_plan",
    "derive_shard_seed",
    "generate_shard",
]
