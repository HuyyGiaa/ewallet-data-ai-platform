"""Generate and validate one complete source cohort at a time."""

from __future__ import annotations

import base64
import copy
import gc
import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from data_platform.generation.src.offline.offline_generator import (
    generate_offline_datasets,
    write_offline_datasets,
)

from .models import ShardFileMetadata, ShardManifest, ShardPlan
from .planner import ShardPlanError, canonical_json_sha256, derive_shard_seed


PHYSICAL_TABLES = (
    "users",
    "accounts",
    "merchants",
    "devices",
    "transactions_v1",
    "transactions_v2",
    "balance_snapshots",
    "login_events",
    "fraud_labels",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5_base64_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def _safe_output_root(output_root: Path) -> Path:
    root = output_root.expanduser().resolve()
    unsafe = {Path("/").resolve(), Path.home().resolve()}
    if root in unsafe:
        raise ShardPlanError(f"unsafe shard output root: {root}")
    return root


def _validate_config_matches_plan(base_config: dict, plan: ShardPlan) -> None:
    actual = canonical_json_sha256(base_config)
    if actual != plan.base_config_sha256:
        raise ShardPlanError(
            "base config differs from the immutable shard plan: "
            f"expected {plan.base_config_sha256}, got {actual}"
        )


def _shard_config(base_config: dict, plan: ShardPlan, shard_id: int) -> dict:
    config = copy.deepcopy(base_config)
    seed = derive_shard_seed(plan, shard_id)
    config.update(
        {
            "n_users": plan.users_per_shard,
            "n_merchants": plan.merchants_per_shard,
            "n_transactions": plan.transactions_per_shard,
            "random_seed": seed,
        }
    )
    if not isinstance(config.get("fraud"), dict):
        raise ShardPlanError("base config must contain fraud configuration")
    config["fraud"]["random_seed"] = seed
    return config


def _file_metadata(
    shard_directory: Path,
    final_directory_name: str,
    table: str,
) -> ShardFileMetadata:
    path = shard_directory / f"{table}.parquet"
    parquet_file = pq.ParquetFile(path)
    schema_text = str(parquet_file.schema_arrow)
    return ShardFileMetadata(
        table=table,
        relative_path=f"{final_directory_name}/{table}.parquet",
        rows=parquet_file.metadata.num_rows,
        bytes=path.stat().st_size,
        sha256=_sha256_file(path),
        md5_base64=_md5_base64_file(path),
        schema_sha256=hashlib.sha256(schema_text.encode("utf-8")).hexdigest(),
    )


def generate_shard(
    base_config: dict,
    plan: ShardPlan,
    shard_id: int,
    output_root: Path,
) -> ShardManifest:
    """Generate one bounded shard and atomically publish it to local staging."""
    root = _safe_output_root(output_root)
    _validate_config_matches_plan(base_config, plan)
    derived_seed = derive_shard_seed(plan, shard_id)
    final_name = f"shard-{shard_id:05d}"
    final_directory = root / final_name
    partial_directory = root / f".{final_name}.partial"

    if final_directory.exists():
        raise FileExistsError(f"shard output already exists: {final_directory}")
    root.mkdir(parents=True, exist_ok=True)
    if partial_directory.exists():
        shutil.rmtree(partial_directory)
    partial_directory.mkdir()

    datasets: dict[str, pd.DataFrame] | None = None
    try:
        datasets, fraud_metrics = generate_offline_datasets(
            _shard_config(base_config, plan, shard_id)
        )
        if tuple(datasets) != PHYSICAL_TABLES:
            raise ShardPlanError(
                "offline generator physical table contract changed: "
                f"expected {PHYSICAL_TABLES}, got {tuple(datasets)}"
            )

        transactions = pd.concat(
            [datasets["transactions_v1"], datasets["transactions_v2"]],
            ignore_index=True,
        )
        logical_transactions = int(transactions["transaction_id"].nunique())
        physical_transactions = len(transactions)
        del transactions

        write_offline_datasets(datasets, partial_directory)
        files = tuple(
            _file_metadata(partial_directory, final_name, table)
            for table in PHYSICAL_TABLES
        )
        scenario_counts = {
            str(name): int(count)
            for name, count in fraud_metrics["scenario_counts"].items()
        }
        manifest = ShardManifest(
            schema_version=1,
            dataset_version=plan.dataset_version,
            plan_digest=plan.digest,
            shard_id=shard_id,
            derived_seed=derived_seed,
            logical_transactions=logical_transactions,
            physical_transactions=physical_transactions,
            fraud_scenario_counts=scenario_counts,
            files=files,
        )
        (partial_directory / "_manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        partial_directory.rename(final_directory)
        return manifest
    except Exception:
        shutil.rmtree(partial_directory, ignore_errors=True)
        raise
    finally:
        if datasets is not None:
            datasets.clear()
        gc.collect()
