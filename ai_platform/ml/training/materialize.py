"""Chunked Feast materialization for the F7 trainable dataset."""

from __future__ import annotations

import argparse
import gc
import importlib.metadata
import json
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
from ai_platform.ml.training.validation import validate_training_frame


DEFAULT_SOURCE = ML_DATA_ROOT / "fraud_training_v1" / "trainable_source"
DEFAULT_OUTPUT = ML_DATA_ROOT / "fraud_training_v1" / "trainable_dataset"
DEFAULT_CHUNK_SIZE = 1_000
DEFAULT_MIN_AVAILABLE_GIB = 4.0
DEFAULT_MAX_NEW_CHUNKS = 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
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
    return parser.parse_args()


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
    checkpoint = {}
    if resume:
        try:
            checkpoint = json.loads(incomplete.read_text())
        except json.JSONDecodeError:
            checkpoint = {}
    else:
        incomplete.write_text("{}\n")

    store = get_store(FEATURE_REPO)
    total_rows = 0
    chunk_count = 0
    split_part_numbers = Counter()
    split_counts = {split: Counter() for split in SPLIT_ORDER}
    null_counts: Counter = Counter()
    if resume:
        for split in SPLIT_ORDER:
            existing = sorted((output / f"split={split}").glob("*.parquet"))
            split_part_numbers[split] = len(existing)
            for path in existing:
                frame = pd.read_parquet(path)
                update_counts(frame, split_counts, null_counts)
                total_rows += len(frame)
                chunk_count += 1
                del frame
        print(f"PASS | resume checkpoint chunks={chunk_count} rows={total_rows}")

    completed_source_chunks = chunk_count
    source_chunk_index = 0
    new_chunks = 0
    checkpoint_requested = False

    for split, path in source_files(source):
        parquet_file = pq.ParquetFile(path)
        for batch in parquet_file.iter_batches(batch_size=chunk_size):
            if source_chunk_index < completed_source_chunks:
                source_chunk_index += 1
                continue
            source_chunk_index += 1
            if max_rows is not None:
                remaining = max_rows - total_rows
                if remaining <= 0:
                    break
                if batch.num_rows > remaining:
                    batch = batch.slice(0, remaining)
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
            frame.to_parquet(target / f"part-{part_number:05d}.parquet", index=False)
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
        checkpoint_state = {
            "chunk_size": chunk_size,
            "chunks": chunk_count,
            "rows": total_rows,
            "runtime_seconds": round(accumulated_runtime_seconds, 4),
            "initial_available_gib": checkpoint.get(
                "initial_available_gib", invocation_initial_available_gib
            ),
            "minimum_available_gib_observed_before_chunks": round(
                accumulated_minimum_available_gib, 4
            ),
            "peak_process_rss_gib": round(accumulated_peak_rss_gib, 4),
        }
        incomplete.write_text(json.dumps(checkpoint_state, indent=2) + "\n")
        print(
            f"PASS | checkpoint rows={total_rows}/{requested_rows} "
            f"chunks={chunk_count}"
        )
        return {"complete": False, **checkpoint_state}

    complete_source_scope = max_rows is None and total_rows == expected_rows
    if total_rows != requested_rows:
        raise ValueError(f"Materialized rows {total_rows} != source rows {expected_rows}")
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
            "model_features": len(MODEL_FEATURE_COLUMNS),
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
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    incomplete.unlink()
    print(
        f"PASS | materialization rows={total_rows} chunks={chunk_count} "
        f"runtime_seconds={runtime_seconds}"
    )
    return manifest


def main() -> int:
    args = parse_args()
    try:
        materialize(
            args.source,
            args.output,
            args.chunk_size,
            args.min_available_memory_gib,
            args.max_rows,
            args.resume,
            args.max_new_chunks,
        )
    except Exception as exc:
        print(f"ERROR | {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
