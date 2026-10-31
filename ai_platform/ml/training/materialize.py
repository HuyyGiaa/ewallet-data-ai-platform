"""Chunked Feast materialization for the F7 trainable dataset."""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import os
import resource
import shutil
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from ai_platform.features.feast import FEATURE_REPO
from ai_platform.features.feast.retrieval.historical import (
    get_store,
    retrieve_combined,
)
from ai_platform.ml import ML_DATA_ROOT
from ai_platform.ml.training.build import (
    FEAST_ENTITY_COLUMNS,
    assemble_training_frame,
)
from ai_platform.ml.training.contract import (
    DERIVED_FEATURE_COLUMNS,
    HISTORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    OUTPUT_COLUMNS,
)
from ai_platform.ml.training.temporal import SPLIT_ORDER, TEST_START, VALIDATION_START
from ai_platform.ml.training.source_contract import (
    SOURCE_OUTPUT_COLUMNS,
    validate_source_manifest_contract,
)
from ai_platform.ml.training.validation import validate_training_frame


DEFAULT_SOURCE = ML_DATA_ROOT / "fraud_training_v1" / "trainable_source"
DEFAULT_OUTPUT = ML_DATA_ROOT / "fraud_training_v1" / "trainable_dataset"
DEFAULT_CHUNK_SIZE = 1_000
DEFAULT_MIN_AVAILABLE_GIB = 4.0
DEFAULT_MAX_NEW_CHUNKS = 1
CHECKPOINT_SCHEMA_VERSION = "training-materialization-checkpoint-v2"
SCHEMA_SELECTION_VERSION = "ml1-training-schema-v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--environment", choices=("local", "dev", "gcp"), default="local"
    )
    parser.add_argument("--dataset-version")
    parser.add_argument("--source-silver-run-id")
    parser.add_argument("--source-feature-run-id")
    parser.add_argument("--feast-snapshot-id")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--source-git-sha")
    parser.add_argument("--feature-selection-digest")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument(
        "--min-available-memory-gib",
        type=float,
        default=DEFAULT_MIN_AVAILABLE_GIB,
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Smoke-test cap. Omit for the complete exported source scope.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--max-new-chunks",
        type=int,
        default=DEFAULT_MAX_NEW_CHUNKS,
        help=(
            "Checkpoint after this many new chunks so the next process can resume; "
            "defaults to one measured-safe Feast request per process."
        ),
    )
    args = parser.parse_args(argv)
    if args.environment == "gcp" and any(
        value is None
        for value in (
            args.dataset_version,
            args.source_silver_run_id,
            args.source_feature_run_id,
            args.feast_snapshot_id,
            args.snapshot_id,
            args.source_git_sha,
            args.feature_selection_digest,
        )
    ):
        parser.error(
            "gcp materialization requires dataset and Silver/feature/snapshot IDs"
        )
    return args


def available_memory_gib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    raise RuntimeError("MemAvailable is unavailable")


def peak_rss_gib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


def source_files(source: Path) -> list[tuple[str, Path]]:
    result = []
    for split in SPLIT_ORDER:
        directory = source / f"split={split}"
        result.extend((split, path) for path in sorted(directory.glob("*.parquet")))
    if not result:
        raise FileNotFoundError(f"No partitioned Parquet source under {source}")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_inventory(source: Path) -> tuple[list[dict[str, object]], str]:
    paths = [source / "manifest.json", *(path for _, path in source_files(source))]
    inventory = []
    for path in paths:
        item: dict[str, object] = {
            "name": path.relative_to(source).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        if path.suffix == ".parquet":
            item["rows"] = pq.ParquetFile(path).metadata.num_rows
        inventory.append(item)
    payload = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    return inventory, hashlib.sha256(payload).hexdigest()


def _schema_selection_digest() -> str:
    payload = {
        "version": SCHEMA_SELECTION_VERSION,
        "source": list(SOURCE_OUTPUT_COLUMNS),
        "output": list(OUTPUT_COLUMNS),
        "model": list(MODEL_FEATURE_COLUMNS),
        "historical": list(HISTORICAL_FEATURE_COLUMNS),
        "derived": list(DERIVED_FEATURE_COLUMNS),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _checkpoint_binding(
    *,
    source_inventory: list[dict[str, object]],
    source_inventory_digest: str,
    chunk_size: int,
    max_rows: int | None,
    dataset_version: str | None,
    source_silver_run_id: str | None,
    source_feature_run_id: str | None,
    feast_snapshot_id: str | None,
    snapshot_id: str | None,
    source_git_sha: str | None,
    feature_selection_digest: str | None,
) -> dict[str, object]:
    return {
        "source_inventory": source_inventory,
        "source_inventory_digest": source_inventory_digest,
        "chunk_size": chunk_size,
        "max_rows": max_rows,
        "identity": {
            "dataset_version": dataset_version,
            "source_silver_run_id": source_silver_run_id,
            "source_feature_run_id": source_feature_run_id,
            "feast_snapshot_id": feast_snapshot_id,
            "snapshot_id": snapshot_id,
            "source_git_sha": source_git_sha,
            "feature_selection_digest": feature_selection_digest,
        },
        "schema_selection_version": SCHEMA_SELECTION_VERSION,
        "schema_selection_digest": _schema_selection_digest(),
    }


def _write_checkpoint(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _verify_resume_outputs(output: Path, completed: list[dict[str, object]]) -> None:
    expected = {str(item["output_name"]): item for item in completed}
    actual = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file() and path.name != "_INCOMPLETE"
    }
    if actual != set(expected):
        raise ValueError("Resume output inventory does not match the checkpoint")
    for name, item in expected.items():
        path = output / name
        if (
            path.stat().st_size != item["size_bytes"]
            or _sha256_file(path) != item["sha256"]
            or pq.ParquetFile(path).metadata.num_rows != item["rows"]
        ):
            raise ValueError(f"Resume output part is missing or corrupted: {name}")


def normalize_source(batch, split: str) -> pd.DataFrame:
    frame = batch.to_pandas()
    frame["event_timestamp"] = pd.to_datetime(
        frame.pop("event_timestamp_us"), unit="us", utc=True
    )
    frame["split"] = split
    return frame


def update_counts(
    frame: pd.DataFrame,
    split_counts: dict[str, Counter],
    null_counts: Counter,
) -> None:
    split = str(frame["split"].iloc[0])
    stats = split_counts[split]
    stats["rows"] += len(frame)
    stats["fraud"] += int(frame["label"].sum())
    stats["normal"] += int((frame["label"] == 0).sum())
    for fraud_type, count in (
        frame.loc[frame["label"] == 1, "fraud_type"].value_counts().items()
    ):
        stats[f"fraud_type:{fraud_type}"] += int(count)
    minimum = frame["event_timestamp"].min()
    maximum = frame["event_timestamp"].max()
    if "min_timestamp" not in stats or minimum < stats["min_timestamp"]:
        stats["min_timestamp"] = minimum
    if "max_timestamp" not in stats or maximum > stats["max_timestamp"]:
        stats["max_timestamp"] = maximum
    for column in (*HISTORICAL_FEATURE_COLUMNS, *DERIVED_FEATURE_COLUMNS):
        null_counts[column] += int(frame[column].isna().sum())


def serializable_split_counts(
    split_counts: dict[str, Counter],
) -> dict[str, dict[str, object]]:
    result = {}
    for split in SPLIT_ORDER:
        stats = split_counts[split]
        if stats["rows"] == 0:
            result[split] = {
                "rows": 0,
                "normal": 0,
                "fraud": 0,
                "fraud_rate": None,
                "min_timestamp": None,
                "max_timestamp": None,
                "fraud_types": {},
            }
            continue
        fraud_types = {
            key.removeprefix("fraud_type:"): value
            for key, value in stats.items()
            if key.startswith("fraud_type:")
        }
        result[split] = {
            "rows": stats["rows"],
            "normal": stats["normal"],
            "fraud": stats["fraud"],
            "fraud_rate": stats["fraud"] / stats["rows"],
            "min_timestamp": stats["min_timestamp"].isoformat(),
            "max_timestamp": stats["max_timestamp"].isoformat(),
            "fraud_types": fraud_types,
        }
    return result


def materialize(
    source: Path,
    output: Path,
    chunk_size: int,
    min_available_gib: float,
    max_rows: int | None,
    resume: bool,
    max_new_chunks: int | None,
    *,
    environment: str = "local",
    dataset_version: str | None = None,
    source_silver_run_id: str | None = None,
    source_feature_run_id: str | None = None,
    feast_snapshot_id: str | None = None,
    snapshot_id: str | None = None,
    source_git_sha: str | None = None,
    feature_selection_digest: str | None = None,
) -> dict:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if max_rows is not None and max_rows <= 0:
        raise ValueError("max_rows must be positive")
    if max_new_chunks is not None and max_new_chunks <= 0:
        raise ValueError("max_new_chunks must be positive")
    source = source.resolve()
    output = output.resolve()
    source_manifest = json.loads((source / "manifest.json").read_text())
    if environment == "gcp":
        validate_source_manifest_contract(
            source_manifest,
            dataset_version=str(dataset_version),
            source_silver_run_id=str(source_silver_run_id),
            source_feature_run_id=str(source_feature_run_id),
            snapshot_id=str(snapshot_id),
            source_git_sha=str(source_git_sha),
            feature_selection_digest=str(feature_selection_digest),
            expected_output_columns=SOURCE_OUTPUT_COLUMNS,
        )
    source_inventory, source_inventory_digest = _source_inventory(source)
    binding = _checkpoint_binding(
        source_inventory=source_inventory,
        source_inventory_digest=source_inventory_digest,
        chunk_size=chunk_size,
        max_rows=max_rows,
        dataset_version=dataset_version,
        source_silver_run_id=source_silver_run_id,
        source_feature_run_id=source_feature_run_id,
        feast_snapshot_id=feast_snapshot_id,
        snapshot_id=snapshot_id,
        source_git_sha=source_git_sha,
        feature_selection_digest=feature_selection_digest,
    )
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    invocation_initial_available_gib = available_memory_gib()
    minimum_available_gib = invocation_initial_available_gib
    if not resume:
        shutil.rmtree(output, ignore_errors=True)
        output.mkdir(parents=True)
    incomplete = output / "_INCOMPLETE"
    if resume and not incomplete.exists():
        raise ValueError("Cannot resume without an _INCOMPLETE checkpoint")
    checkpoint: dict[str, object]
    if resume:
        try:
            checkpoint = json.loads(incomplete.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError("Materialization checkpoint is unreadable") from exc
        if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("Materialization checkpoint schema is incompatible")
        if checkpoint.get("binding") != binding:
            raise ValueError("Materialization checkpoint binding mismatch")
        completed = checkpoint.get("completed_source_chunks")
        if not isinstance(completed, list):
            raise ValueError("Materialization checkpoint progress is invalid")
        _verify_resume_outputs(output, completed)
    else:
        checkpoint = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "binding": binding,
            "completed_source_chunks": [],
            "runtime_seconds": 0.0,
            "initial_available_gib": invocation_initial_available_gib,
            "minimum_available_gib_observed_before_chunks": invocation_initial_available_gib,
            "peak_process_rss_gib": peak_rss_gib(),
        }
        _write_checkpoint(incomplete, checkpoint)

    store = get_store(FEATURE_REPO, environment=environment)
    total_rows = 0
    chunk_count = 0
    split_part_numbers = Counter()
    split_counts = {split: Counter() for split in SPLIT_ORDER}
    null_counts: Counter = Counter()
    completed_chunks = list(checkpoint["completed_source_chunks"])
    completed_by_position = {
        str(item["source_position"]): item for item in completed_chunks
    }
    if len(completed_by_position) != len(completed_chunks):
        raise ValueError("Materialization checkpoint contains duplicate source positions")
    if resume:
        for item in completed_chunks:
            frame = pd.read_parquet(output / str(item["output_name"]))
            update_counts(frame, split_counts, null_counts)
            total_rows += len(frame)
            split_part_numbers[str(item["split"])] += 1
            chunk_count += 1
            del frame
        print(f"PASS | resume checkpoint chunks={chunk_count} rows={total_rows}")

    encountered_positions: set[str] = set()
    source_rows_planned = 0
    new_chunks = 0
    checkpoint_requested = False

    for split, path in source_files(source):
        parquet_file = pq.ParquetFile(path)
        relative_source = path.relative_to(source).as_posix()
        for batch_index, batch in enumerate(
            parquet_file.iter_batches(batch_size=chunk_size)
        ):
            if max_rows is not None:
                remaining = max_rows - source_rows_planned
                if remaining <= 0:
                    break
                if batch.num_rows > remaining:
                    batch = batch.slice(0, remaining)
            source_rows_planned += batch.num_rows
            source_position = f"{relative_source}#{batch_index}"
            encountered_positions.add(source_position)
            completed_item = completed_by_position.get(source_position)
            if completed_item is not None:
                if completed_item.get("source_rows") != batch.num_rows:
                    raise ValueError("Resume source chunk row count mismatch")
                continue
            available_gib = available_memory_gib()
            minimum_available_gib = min(minimum_available_gib, available_gib)
            if available_gib < min_available_gib:
                raise MemoryError(
                    f"Available memory {available_gib:.2f} GiB is below "
                    f"the {min_available_gib:.2f} GiB safety floor"
                )

            source_frame = normalize_source(batch, split)
            entity_frame = source_frame[list(FEAST_ENTITY_COLUMNS)].copy()
            historical = retrieve_combined(store, entity_frame)
            frame = assemble_training_frame(source_frame, historical)
            validate_training_frame(frame, source_frame)
            update_counts(frame, split_counts, null_counts)

            target = output / f"split={split}"
            target.mkdir(exist_ok=True)
            part_number = split_part_numbers[split]
            output_name = f"split={split}/part-{part_number:05d}.parquet"
            final_path = output / output_name
            temporary_path = final_path.with_suffix(".parquet.tmp")
            frame.to_parquet(temporary_path, index=False)
            os.replace(temporary_path, final_path)
            completed_item = {
                "source_position": source_position,
                "source_rows": batch.num_rows,
                "split": split,
                "output_name": output_name,
                "rows": len(frame),
                "size_bytes": final_path.stat().st_size,
                "sha256": _sha256_file(final_path),
            }
            completed_chunks.append(completed_item)
            completed_by_position[source_position] = completed_item
            split_part_numbers[split] += 1
            total_rows += len(frame)
            chunk_count += 1
            new_chunks += 1
            print(
                f"PASS | chunk={chunk_count} split={split} "
                f"rows={len(frame)} total={total_rows} "
                f"available_gib={available_gib:.2f} peak_rss_gib={peak_rss_gib():.2f}"
            )
            del source_frame, entity_frame, historical, frame, batch
            gc.collect()
            if max_new_chunks is not None and new_chunks >= max_new_chunks:
                checkpoint_requested = True
                break
        if max_rows is not None and total_rows >= max_rows:
            break
        if checkpoint_requested:
            break

    expected_rows = source_manifest["written_rows"]
    requested_rows = min(max_rows, expected_rows) if max_rows else expected_rows
    invocation_runtime_seconds = time.perf_counter() - started
    accumulated_runtime_seconds = (
        float(checkpoint.get("runtime_seconds", 0.0)) + invocation_runtime_seconds
    )
    accumulated_peak_rss_gib = max(
        float(checkpoint.get("peak_process_rss_gib", 0.0)), peak_rss_gib()
    )
    accumulated_minimum_available_gib = min(
        float(
            checkpoint.get(
                "minimum_available_gib_observed_before_chunks",
                invocation_initial_available_gib,
            )
        ),
        minimum_available_gib,
    )
    if total_rows < requested_rows:
        checkpoint_state: dict[str, object] = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "binding": binding,
            "completed_source_chunks": completed_chunks,
            "runtime_seconds": round(accumulated_runtime_seconds, 4),
            "initial_available_gib": checkpoint.get(
                "initial_available_gib", invocation_initial_available_gib
            ),
            "minimum_available_gib_observed_before_chunks": round(
                accumulated_minimum_available_gib, 4
            ),
            "peak_process_rss_gib": round(accumulated_peak_rss_gib, 4),
        }
        _write_checkpoint(incomplete, checkpoint_state)
        print(
            f"PASS | checkpoint rows={total_rows}/{requested_rows} "
            f"chunks={chunk_count}"
        )
        return {
            "complete": False,
            "chunk_size": chunk_size,
            "chunks": chunk_count,
            "rows": total_rows,
            **checkpoint_state,
        }

    complete_source_scope = max_rows is None and total_rows == expected_rows
    if total_rows != requested_rows:
        raise ValueError(f"Materialized rows {total_rows} != source rows {expected_rows}")
    if set(completed_by_position) != encountered_positions:
        raise ValueError("Materialization completed source positions are inconsistent")
    split_summary = serializable_split_counts(split_counts)
    runtime_seconds = round(accumulated_runtime_seconds, 4)
    manifest = {
        "schema_version": "f7-training-dataset-v1",
        "built_at": started_at.isoformat(),
        "source_git_head": source_manifest["source_git_head"],
        "feast_version": importlib.metadata.version("feast"),
        "source_scope": source_manifest["scope"],
        "complete_source_scope": complete_source_scope,
        "canonical_source_rows": source_manifest["label_integrity"]["transactions"],
        "materialized_rows": total_rows,
        "chunk_size": chunk_size,
        "chunks": chunk_count,
        "partition_strategy": "Hive-style split=train|validation|test with chunk Parquet files",
        "split_boundaries_utc": {
            "validation_start": VALIDATION_START.isoformat(),
            "test_start": TEST_START.isoformat(),
        },
        "split_summary": split_summary,
        "schema": {
            "output_columns": len(OUTPUT_COLUMNS),
            "ordered_output_columns": list(OUTPUT_COLUMNS),
            "model_features": len(MODEL_FEATURE_COLUMNS),
            "ordered_model_feature_columns": list(MODEL_FEATURE_COLUMNS),
            "historical_features": len(HISTORICAL_FEATURE_COLUMNS),
            "derived_features": len(DERIVED_FEATURE_COLUMNS),
        },
        "derived_formulas": {
            "amount_ratio_to_user_avg_30d": "amount / user_avg_amount_30d",
            "amount_zscore_user_30d": "(amount - user_avg_amount_30d) / user_std_amount_30d",
            "amount_ratio_to_account_avg_30d": "amount / account_avg_amount_30d",
            "amount_zscore_account_30d": "(amount - account_avg_amount_30d) / account_std_amount_30d",
            "account_tx_share_1h": "account_tx_count_1h / user_tx_count_1h",
            "merchant_activity_rate_ratio_10m_vs_24h": "(merchant_tx_count_10m * 144) / merchant_tx_count_24h",
        },
        "null_profile": {
            column: {
                "null_count": null_counts[column],
                "null_rate": null_counts[column] / total_rows,
            }
            for column in (*HISTORICAL_FEATURE_COLUMNS, *DERIVED_FEATURE_COLUMNS)
        },
        "runtime_seconds": runtime_seconds,
        "rows_per_second": round(total_rows / runtime_seconds, 4),
        "memory": {
            "initial_available_gib": round(
                float(
                    checkpoint.get(
                        "initial_available_gib", invocation_initial_available_gib
                    )
                ),
                4,
            ),
            "minimum_available_gib_observed_before_chunks": round(
                accumulated_minimum_available_gib, 4
            ),
            "peak_process_rss_gib": round(accumulated_peak_rss_gib, 4),
            "safety_floor_gib": min_available_gib,
        },
        "optimization_performed": False,
        "performance_label": "F7 TRAINING DATASET MATERIALIZATION BASELINE - NOT OPTIMIZED",
        "lineage": {
            "dataset_version": dataset_version,
            "source_silver_run_id": source_silver_run_id,
            "source_feature_run_id": source_feature_run_id,
            "feast_snapshot_id": feast_snapshot_id,
            "snapshot_id": snapshot_id,
            "source_git_sha": source_git_sha,
            "feature_selection_digest": feature_selection_digest,
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    incomplete.unlink()
    print(
        f"PASS | materialization rows={total_rows} chunks={chunk_count} "
        f"runtime_seconds={runtime_seconds}"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        materialize(
            args.source,
            args.output,
            args.chunk_size,
            args.min_available_memory_gib,
            args.max_rows,
            args.resume,
            args.max_new_chunks,
            environment=args.environment,
            dataset_version=args.dataset_version,
            source_silver_run_id=args.source_silver_run_id,
            source_feature_run_id=args.source_feature_run_id,
            feast_snapshot_id=args.feast_snapshot_id,
            snapshot_id=args.snapshot_id,
            source_git_sha=args.source_git_sha,
            feature_selection_digest=args.feature_selection_digest,
        )
    except Exception as exc:
        print(f"ERROR | {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
