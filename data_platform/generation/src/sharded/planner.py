"""Pure deterministic planning helpers for source dataset shards."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from .models import ShardPlan


DATASET_VERSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class ShardPlanError(ValueError):
    """Raised when a shard plan is unsafe or internally inconsistent."""


def canonical_json_sha256(value: Any) -> str:
    """Hash JSON-compatible configuration without depending on YAML layout."""
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_shard_plan(
    base_config: Mapping[str, Any],
    dataset_version: str,
    global_seed: int,
    shard_count: int,
    users_per_shard: int,
    merchants_per_shard: int,
    transactions_per_shard: int,
) -> ShardPlan:
    """Validate and freeze all logical inputs that determine a dataset."""
    if not isinstance(base_config, Mapping) or not base_config:
        raise ShardPlanError("base_config must be a non-empty mapping")
    if not isinstance(dataset_version, str) or not DATASET_VERSION_PATTERN.fullmatch(
        dataset_version
    ):
        raise ShardPlanError(
            "dataset_version must match [a-z0-9][a-z0-9._-]{0,62}"
        )
    if not isinstance(global_seed, int) or isinstance(global_seed, bool):
        raise ShardPlanError("global_seed must be an integer")

    dimensions = {
        "shard_count": shard_count,
        "users_per_shard": users_per_shard,
        "merchants_per_shard": merchants_per_shard,
        "transactions_per_shard": transactions_per_shard,
    }
    for name, value in dimensions.items():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ShardPlanError(f"{name} must be a positive integer")

    config_digest = canonical_json_sha256(base_config)
    identity = {
        "schema_version": 1,
        "dataset_version": dataset_version,
        "global_seed": global_seed,
        **dimensions,
        "base_config_sha256": config_digest,
    }
    return ShardPlan(
        **identity,
        digest=canonical_json_sha256(identity),
    )


def derive_shard_seed(plan: ShardPlan, shard_id: int) -> int:
    """Derive a stable 32-bit RNG seed for exactly one shard."""
    if not isinstance(shard_id, int) or isinstance(shard_id, bool):
        raise ShardPlanError("shard_id must be an integer")
    if not 0 <= shard_id < plan.shard_count:
        raise ShardPlanError(
            f"shard_id must be in [0, {plan.shard_count - 1}]"
        )
    digest = canonical_json_sha256(
        {
            "dataset_version": plan.dataset_version,
            "global_seed": plan.global_seed,
            "plan_digest": plan.digest,
            "shard_id": shard_id,
        }
    )
    return int(digest[:8], 16)
