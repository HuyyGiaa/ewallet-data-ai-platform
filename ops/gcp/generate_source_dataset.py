"""Plan or execute resumable source shard publication to immutable GCS paths."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import TextIO

from data_platform.config import load_runtime_config
from data_platform.generation.src.offline.offline_generator import load_config
from data_platform.generation.src.sharded.generator import generate_shard
from data_platform.generation.src.sharded.manifest import (
    DatasetPublisher,
    IncompleteDatasetError,
)
from data_platform.generation.src.sharded.planner import build_shard_plan
from data_platform.generation.src.sharded.storage import GcloudObjectStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_generator_config_path(value: str) -> Path:
    """Accept absolute, current-directory, repo-relative, and legacy paths."""
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    if candidate.is_file():
        return candidate.resolve()
    repo_candidate = PROJECT_ROOT / candidate
    if repo_candidate.is_file():
        return repo_candidate.resolve()
    # The legacy loader resolves values such as config/settings.yaml relative
    # to data_platform/generation, so keep that compatibility spelling.
    return candidate


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate deterministic source shards and publish them to GCS."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--environment", choices=("dev", "gcp"), default="dev")
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--global-seed", required=True, type=int)
    parser.add_argument("--shard-count", required=True, type=int)
    parser.add_argument("--users-per-shard", required=True, type=int)
    parser.add_argument("--merchants-per-shard", required=True, type=int)
    parser.add_argument("--transactions-per-shard", required=True, type=int)
    parser.add_argument("--shard-start", type=int, default=0)
    parser.add_argument("--shard-end", type=int)
    parser.add_argument("--staging-dir", type=Path, default=Path("tmp/source-datasets"))
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Publish to GCS. Without this flag the command is a dry run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    base_config = load_config(resolve_generator_config_path(args.config))
    runtime = load_runtime_config(args.environment)
    if runtime.storage.scheme != "gs":
        raise ValueError("source publication requires a gs:// runtime profile")

    plan = build_shard_plan(
        base_config,
        args.dataset_version,
        args.global_seed,
        args.shard_count,
        args.users_per_shard,
        args.merchants_per_shard,
        args.transactions_per_shard,
    )
    dataset_root = (
        f"{runtime.storage.source_root.rstrip('/')}/{plan.dataset_version}"
    )
    shard_end = plan.shard_count if args.shard_end is None else args.shard_end
    if not 0 <= args.shard_start <= shard_end <= plan.shard_count:
        raise ValueError(
            f"shard range must satisfy 0 <= start <= end <= {plan.shard_count}"
        )

    print("SOURCE DATASET PLAN", file=stdout)
    print(json.dumps(plan.to_dict(), indent=2, sort_keys=True), file=stdout)
    print(f"source_root: {dataset_root}", file=stdout)
    print(
        "estimated_logical_transactions: "
        f"{plan.shard_count * plan.transactions_per_shard}",
        file=stdout,
    )
    print(f"requested_shards: [{args.shard_start}, {shard_end})", file=stdout)
    if not args.execute:
        print("DRY RUN - no local shard generated and no GCS object written", file=stdout)
        return 0

    store = GcloudObjectStore()
    publisher = DatasetPublisher(store, dataset_root, plan)
    if publisher.dataset_complete():
        raise RuntimeError(
            f"dataset version is already complete and immutable: {dataset_root}"
        )
    publisher.ensure_plan()

    staging_root = (
        args.staging_dir.expanduser().resolve()
        / plan.dataset_version
        / plan.digest
    )
    for shard_id in range(args.shard_start, shard_end):
        completed = publisher.completed_shard(shard_id)
        if completed is not None:
            print(f"shard {shard_id:05d}: verified remote COMPLETE; skip", file=stdout)
            continue
        local_directory = staging_root / f"shard-{shard_id:05d}"
        if local_directory.exists():
            shutil.rmtree(local_directory)
        manifest = generate_shard(base_config, plan, shard_id, staging_root)
        publisher.publish_shard(
            local_directory,
            manifest,
            cleanup_local=True,
        )
        print(f"shard {shard_id:05d}: generated, uploaded, verified", file=stdout)

    try:
        manifest = publisher.finalize()
        print(
            "dataset COMPLETE: "
            f"{manifest['logical_transactions']} logical transactions, "
            f"{manifest['total_bytes']} bytes",
            file=stdout,
        )
    except IncompleteDatasetError:
        completed_count = sum(
            store.exists(publisher.shard_manifest_uri(shard_id))
            for shard_id in range(plan.shard_count)
        )
        print(
            f"dataset INCOMPLETE: {completed_count}/{plan.shard_count} shards",
            file=stdout,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
