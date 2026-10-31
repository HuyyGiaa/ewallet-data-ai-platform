"""Streaming validation for the materialized F7 trainable dataset."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from ai_platform.ml import ML_DATA_ROOT
from ai_platform.ml.training.contract import (
    DERIVED_FEATURE_COLUMNS,
    FORBIDDEN_MODEL_COLUMNS,
    FORBIDDEN_OUTPUT_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    OUTPUT_COLUMNS,
)
from ai_platform.ml.training.derived import add_derived_features
from ai_platform.ml.training.temporal import SPLIT_ORDER, assign_temporal_split


DEFAULT_SOURCE = ML_DATA_ROOT / "fraud_training_v1" / "trainable_source"
DEFAULT_DATASET = ML_DATA_ROOT / "fraud_training_v1" / "trainable_dataset"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    return parser.parse_args()


def source_audit(source: Path) -> dict:
    transaction_ids: set[str] = set()
    timestamp_groups: Counter = Counter()
    rows = 0
    merchant_null = 0
    split_counts = Counter()
    for split in SPLIT_ORDER:
        for path in sorted((source / f"split={split}").glob("*.parquet")):
            parquet_file = pq.ParquetFile(path)
            for batch in parquet_file.iter_batches(
                columns=["transaction_id", "event_timestamp_us", "merchant_id"]
            ):
                frame = batch.to_pandas()
                ids = set(frame["transaction_id"])
                if transaction_ids.intersection(ids):
                    raise ValueError("Duplicate transaction ID in materialization source")
                transaction_ids.update(ids)
                timestamp_groups.update(frame["event_timestamp_us"].tolist())
                rows += len(frame)
                split_counts[split] += len(frame)
                merchant_null += int(frame["merchant_id"].isna().sum())
    return {
        "transaction_ids": transaction_ids,
        "rows": rows,
        "merchant_null": merchant_null,
        "split_counts": split_counts,
        "same_timestamp_groups": sum(
            1 for count in timestamp_groups.values() if count > 1
        ),
        "same_timestamp_rows": sum(
            count for count in timestamp_groups.values() if count > 1
        ),
    }


def values_match(actual: pd.Series, expected: pd.Series) -> bool:
    actual_values = pd.to_numeric(actual, errors="coerce").to_numpy(dtype=float)
    expected_values = pd.to_numeric(expected, errors="coerce").to_numpy(dtype=float)
    return bool(
        np.allclose(actual_values, expected_values, rtol=1e-10, atol=1e-10, equal_nan=True)
    )


def validate(source: Path, dataset: Path) -> dict:
    source = source.resolve()
    dataset = dataset.resolve()
    source_stats = source_audit(source)
    manifest_path = dataset / "manifest.json"
    if not manifest_path.exists() or (dataset / "_INCOMPLETE").exists():
        raise ValueError("Materialized dataset is incomplete")
    manifest = json.loads(manifest_path.read_text())

    output_ids: set[str] = set()
    rows = 0
    merchant_null = 0
    labels = Counter()
    split_ids = {split: set() for split in SPLIT_ORDER}
    split_min: dict[str, pd.Timestamp] = {}
    split_max: dict[str, pd.Timestamp] = {}
    formula_rows = 0
    files = 0

    for split in SPLIT_ORDER:
        paths = sorted((dataset / f"split={split}").glob("*.parquet"))
        if not paths:
            raise ValueError(f"No output Parquet files for split={split}")
        for path in paths:
            frame = pd.read_parquet(path)
            files += 1
            if tuple(frame.columns) != OUTPUT_COLUMNS:
                raise ValueError(f"Output schema mismatch in {path}")
            if not frame["split"].eq(split).all():
                raise ValueError(f"Partition/split mismatch in {path}")
            expected_split = assign_temporal_split(frame["event_timestamp"])
            if not frame["split"].astype("string").equals(expected_split):
                raise ValueError(f"Temporal split mismatch in {path}")
            if frame["transaction_id"].duplicated().any():
                raise ValueError(f"Duplicate transaction ID in {path}")
            ids = set(frame["transaction_id"])
            if output_ids.intersection(ids):
                raise ValueError("Transaction appears in multiple output files")
            output_ids.update(ids)
            split_ids[split].update(ids)
            if frame["label"].isna().any() or not frame["label"].isin([0, 1]).all():
                raise ValueError(f"Invalid label in {path}")
            labels.update(frame["label"].astype(int).tolist())
            merchant_null += int(frame["merchant_id"].isna().sum())
            rows += len(frame)
            minimum = frame["event_timestamp"].min()
            maximum = frame["event_timestamp"].max()
            split_min[split] = min(split_min.get(split, minimum), minimum)
            split_max[split] = max(split_max.get(split, maximum), maximum)

            expected_derived = add_derived_features(frame)
            for column in DERIVED_FEATURE_COLUMNS:
                if not values_match(frame[column], expected_derived[column]):
                    raise ValueError(f"Derived formula mismatch: {column} in {path}")
            formula_rows += len(frame)

    checks = {
        "parquet_clean_reload": files > 0,
        "row_count_parity": rows == source_stats["rows"] == manifest["materialized_rows"],
        "transaction_id_unique": len(output_ids) == rows,
        "transaction_ids_preserved": output_ids == source_stats["transaction_ids"],
        "labels_complete_and_binary": sum(labels.values()) == rows
        and set(labels).issubset({0, 1}),
        "split_transaction_overlap": not (
            split_ids["train"].intersection(split_ids["validation"])
            or split_ids["train"].intersection(split_ids["test"])
            or split_ids["validation"].intersection(split_ids["test"])
        ),
        "chronological_ordering": split_max["train"] < split_min["validation"]
        and split_max["validation"] < split_min["test"],
        "merchant_null_rows_preserved": merchant_null == source_stats["merchant_null"],
        "same_timestamp_rows_preserved": output_ids == source_stats["transaction_ids"],
        "derived_formulas": formula_rows == rows,
        "exact_output_columns": len(OUTPUT_COLUMNS) == 50,
        "exact_model_features": len(MODEL_FEATURE_COLUMNS) == 41,
        "forbidden_model_columns_absent": not bool(
            FORBIDDEN_MODEL_COLUMNS.intersection(MODEL_FEATURE_COLUMNS)
        ),
        "post_transaction_columns_absent": not bool(
            FORBIDDEN_OUTPUT_COLUMNS.intersection(OUTPUT_COLUMNS)
        ),
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise ValueError(f"Materialized validation failed: {failed}")
    result = {
        "status": "PASS",
        "checks": checks,
        "rows": rows,
        "files": files,
        "label_counts": {str(key): value for key, value in sorted(labels.items())},
        "merchant_null_rows": merchant_null,
        "same_timestamp_groups": source_stats["same_timestamp_groups"],
        "same_timestamp_rows": source_stats["same_timestamp_rows"],
        "split_min": {key: value.isoformat() for key, value in split_min.items()},
        "split_max": {key: value.isoformat() for key, value in split_max.items()},
    }
    manifest["validation"] = result
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"PASS | materialized dataset validation | rows={rows} files={files}")
    return result


def main() -> int:
    args = parse_args()
    try:
        validate(args.source, args.dataset)
    except Exception as exc:
        print(f"ERROR | {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
