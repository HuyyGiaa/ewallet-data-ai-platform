"""Build a partitioned point-in-time fraud training dataset through Feast."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from ai_platform.features.feast import FEATURE_REPO
from ai_platform.features.feast.retrieval.historical import (
    get_store,
    retrieve_combined,
)
from ai_platform.ml import ML_DATA_ROOT
from ai_platform.ml.training.contract import (
    DERIVED_FEATURE_COLUMNS,
    HISTORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    OUTPUT_COLUMNS,
)
from ai_platform.ml.training.derived import add_derived_features
from ai_platform.ml.training.temporal import SPLIT_ORDER, assign_temporal_split
from ai_platform.ml.training.validation import validate_training_frame


DEFAULT_SOURCE = ML_DATA_ROOT / "fraud_training_v1" / "source_sample"
DEFAULT_OUTPUT = ML_DATA_ROOT / "fraud_training_v1" / "bounded_dataset"
DEFAULT_MAX_SOURCE_ROWS = 10_000
FEAST_ENTITY_COLUMNS = (
    "user_id",
    "account_id",
    "device_id",
    "merchant_id",
    "event_timestamp",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--max-source-rows", type=int, default=DEFAULT_MAX_SOURCE_ROWS
    )
    return parser.parse_args()


def read_parquet_parts(path: Path, max_rows: int) -> pd.DataFrame:
    parts = sorted(path.glob("*.parquet"))
    if not parts:
        raise FileNotFoundError(f"No Parquet parts under {path}")
    row_count = sum(pq.ParquetFile(part).metadata.num_rows for part in parts)
    if row_count > max_rows:
        raise ValueError(
            f"Bounded build refuses {row_count} source rows; "
            f"limit={max_rows}. Build canonical partitions separately."
        )
    return pd.concat((pd.read_parquet(part) for part in parts), ignore_index=True)


def assemble_training_frame(
    source: pd.DataFrame,
    historical: pd.DataFrame,
) -> pd.DataFrame:
    """Combine request/label rows with Feast output and shared transforms."""
    if len(source) != len(historical):
        raise ValueError("Source and historical row counts differ")
    source = source.reset_index(drop=True).copy()
    historical = historical.reset_index(drop=True)
    missing = set(HISTORICAL_FEATURE_COLUMNS).difference(historical.columns)
    if missing:
        raise ValueError(f"Historical features missing: {sorted(missing)}")
    result = pd.concat(
        [source, historical[list(HISTORICAL_FEATURE_COLUMNS)]], axis=1
    )
    result["split"] = assign_temporal_split(result["event_timestamp"])
    result = add_derived_features(result)
    return result[list(OUTPUT_COLUMNS)]


def null_rates(frame: pd.DataFrame) -> dict[str, float]:
    columns = (*HISTORICAL_FEATURE_COLUMNS, *DERIVED_FEATURE_COLUMNS)
    return {
        column: round(float(frame[column].isna().mean()), 6)
        for column in columns
    }


def split_summary(frame: pd.DataFrame) -> dict[str, dict[str, object]]:
    result = {}
    for split in SPLIT_ORDER:
        part = frame[frame["split"] == split]
        result[split] = {
            "rows": len(part),
            "fraud": int(part["label"].sum()),
            "normal": int((part["label"] == 0).sum()),
            "min_timestamp": part["event_timestamp"].min().isoformat(),
            "max_timestamp": part["event_timestamp"].max().isoformat(),
        }
    return result


def build_dataset(source_path: Path, output_path: Path, max_rows: int) -> dict:
    started = time.perf_counter()
    if max_rows <= 0:
        raise ValueError("max_rows must be positive")
    source = read_parquet_parts(source_path.resolve(), max_rows)
    if source["transaction_id"].duplicated().any():
        raise ValueError("Source sample has duplicate transaction IDs")
    if source["label"].isna().any() or not source["label"].isin([0, 1]).all():
        raise ValueError("Source sample has missing or invalid labels")

    # Labels and evaluation metadata never enter the Feast entity dataframe.
    entity_frame = source[list(FEAST_ENTITY_COLUMNS)].copy()
    retrieved = retrieve_combined(get_store(FEATURE_REPO), entity_frame)
    frame = assemble_training_frame(source, retrieved)
    validation = validate_training_frame(frame, source)

    output_path = output_path.resolve()
    shutil.rmtree(output_path, ignore_errors=True)
    for split in SPLIT_ORDER:
        target = output_path / f"split={split}"
        target.mkdir(parents=True, exist_ok=True)
        frame[frame["split"] == split].to_parquet(
            target / "part-00000.parquet", index=False
        )

    manifest = {
        "mode": "deterministic_bounded_validation_build",
        "source_rows": len(source),
        "output_rows": len(frame),
        "output_columns": len(frame.columns),
        "model_feature_columns": len(MODEL_FEATURE_COLUMNS),
        "historical_features": len(HISTORICAL_FEATURE_COLUMNS),
        "derived_features": len(DERIVED_FEATURE_COLUMNS),
        "labels_passed_to_feast": False,
        "runtime_seconds": round(time.perf_counter() - started, 4),
        "split_summary": split_summary(frame),
        "null_rates": null_rates(frame),
        "validation": validation,
    }
    output_path.mkdir(parents=True, exist_ok=True)
    (output_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    print(
        f"PASS | training dataset | rows={len(frame)} "
        f"model_features={len(MODEL_FEATURE_COLUMNS)}"
    )
    return manifest


def main() -> int:
    args = parse_args()
    try:
        build_dataset(args.source, args.output, args.max_source_rows)
    except Exception as exc:
        print(f"ERROR | {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
