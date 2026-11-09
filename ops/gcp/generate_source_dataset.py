"""Plan or execute resumable source shard publication to immutable GCS paths."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, TextIO
from urllib.parse import urlsplit

from data_platform.config import load_runtime_config
from data_platform.config.dataset_identity import require_publishable_dataset
from data_platform.generation.src.offline.merchant_history import merchant_history_enabled
from data_platform.generation.src.offline.offline_generator import load_config
from data_platform.generation.src.sharded.generator import generate_shard, physical_tables
from data_platform.generation.src.sharded.manifest import (
    DatasetPublisher,
    IncompleteDatasetError,
)
from data_platform.generation.src.sharded.planner import build_shard_plan, canonical_json_sha256
from data_platform.generation.src.sharded.storage import GcloudObjectStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _positive_worker_count(value: str) -> int:
    workers = int(value)
    if workers < 1:
        raise argparse.ArgumentTypeError("workers must be at least 1")
    return workers


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
    parser.add_argument("--workers", type=_positive_worker_count, default=1)
    parser.add_argument("--staging-dir", type=Path, default=Path("tmp/source-datasets"))
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Publish to GCS. Without this flag the command is a dry run.",
    )
    return parser.parse_args(argv)


def _generate_shard_worker(
    base_config: dict,
    plan,
    shard_id: int,
    staging_root: Path,
):
    """Generate one shard in an isolated process-owned staging directory."""
    local_directory = Path(staging_root) / f"shard-{shard_id:05d}"
    if local_directory.exists():
        shutil.rmtree(local_directory)
    return generate_shard(base_config, plan, shard_id, Path(staging_root))


def find_missing_shards(
    publisher: DatasetPublisher,
    shard_ids: Iterable[int],
    *,
    stdout: TextIO,
) -> list[int]:
    """Let the coordinator validate resume state before scheduling any work."""
    missing: list[int] = []
    for shard_id in shard_ids:
        if publisher.completed_shard(shard_id) is not None:
            print(
                f"shard {shard_id:05d}: verified remote COMPLETE; skip",
                file=stdout,
            )
        else:
            missing.append(shard_id)
    return missing


def generate_and_publish_shards(
    *,
    base_config: dict,
    plan,
    shard_ids: Iterable[int],
    staging_root: Path,
    publisher: DatasetPublisher,
    workers: int,
    stdout: TextIO,
    executor_factory: Callable[..., ProcessPoolExecutor] = ProcessPoolExecutor,
) -> None:
    """Generate with bounded processes; publish manifests only in the coordinator."""
    requested = list(shard_ids)
    if len(requested) != len(set(requested)):
        raise ValueError("duplicate shard IDs cannot be scheduled")
    if workers < 1:
        raise ValueError("workers must be at least 1")

    def publish(shard_id: int, manifest) -> None:
        local_directory = staging_root / f"shard-{shard_id:05d}"
        publisher.publish_shard(
            local_directory,
            manifest,
            cleanup_local=True,
        )
        print(
            f"shard {shard_id:05d}: generated, uploaded, verified",
            file=stdout,
        )

    # Keep the original generate-then-publish sequence when parallelism is off.
    if workers == 1:
        for shard_id in requested:
            manifest = _generate_shard_worker(
                base_config,
                plan,
                shard_id,
                staging_root,
            )
            publish(shard_id, manifest)
        return

    # At most ``workers`` futures exist. Results are consumed in shard order so
    # uploads and manifest coordination remain deterministic in the main process.
    executor = executor_factory(max_workers=workers)
    pending = deque()
    remaining = iter(requested)

    def submit_next() -> bool:
        try:
            shard_id = next(remaining)
        except StopIteration:
            return False
        future = executor.submit(
            _generate_shard_worker,
            base_config,
            plan,
            shard_id,
            staging_root,
        )
        pending.append((shard_id, future))
        return True

    try:
        for _ in range(min(workers, len(requested))):
            submit_next()
        while pending:
            shard_id, future = pending.popleft()
            manifest = future.result()
            publish(shard_id, manifest)
            submit_next()
    except BaseException:
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)


def validate_publication_target(
    *,
    dataset_version: str,
    source_root: str,
    base_config: dict,
    global_seed: int,
    shard_count: int,
) -> None:
    """Fail closed before a registered dataset could be published with the wrong plan."""
    bucket = urlsplit(source_root).netloc
    identity = require_publishable_dataset(dataset_version, bucket)
    if identity is None:
        return
    if merchant_history_enabled(base_config) != identity.merchant_history:
        raise ValueError(
            f"{dataset_version} requires merchant_history={identity.merchant_history}; "
            "the generator config disagrees"
        )
    if identity.generator_config is not None:
        pinned = load_config(PROJECT_ROOT / identity.generator_config)
        if canonical_json_sha256(pinned) != canonical_json_sha256(base_config):
            raise ValueError(
                f"{dataset_version} must be generated with {identity.generator_config}"
            )
    if identity.global_seed is not None and global_seed != identity.global_seed:
        raise ValueError(f"{dataset_version} requires global seed {identity.global_seed}")
    if identity.source_shard_count is not None and shard_count != identity.source_shard_count:
        raise ValueError(f"{dataset_version} requires {identity.source_shard_count} shards")


def main(argv: list[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    args = parse_args(argv)
    base_config = load_config(resolve_generator_config_path(args.config))
    runtime = load_runtime_config(args.environment)
    if runtime.storage.scheme != "gs":
        raise ValueError("source publication requires a gs:// runtime profile")
    validate_publication_target(
        dataset_version=args.dataset_version,
        source_root=runtime.storage.source_root,
        base_config=base_config,
        global_seed=args.global_seed,
        shard_count=args.shard_count,
    )

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
    print(f"project_id: {runtime.project_id}", file=stdout)
    print(f"bucket: {urlsplit(dataset_root).netloc}", file=stdout)
    print(f"generator_config: {args.config}", file=stdout)
    print(f"source_root: {dataset_root}", file=stdout)
    print(f"physical_tables: {list(physical_tables(base_config))}", file=stdout)
    print(
        "estimated_logical_transactions: "
        f"{plan.shard_count * plan.transactions_per_shard}",
        file=stdout,
    )
    print(f"requested_shards: [{args.shard_start}, {shard_end})", file=stdout)
    print(f"workers: {args.workers}", file=stdout)
    if not args.execute:
        print("DRY RUN - no local shard generated and no GCS object written", file=stdout)
        return 0

    store = GcloudObjectStore()
    publisher = DatasetPublisher(
        store, dataset_root, plan, physical_tables=physical_tables(base_config)
    )
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
    missing_shards = find_missing_shards(
        publisher,
        range(args.shard_start, shard_end),
        stdout=stdout,
    )
    generate_and_publish_shards(
        base_config=base_config,
        plan=plan,
        shard_ids=missing_shards,
        staging_root=staging_root,
        publisher=publisher,
        workers=args.workers,
        stdout=stdout,
    )

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
