"""Contract-safe loading for the partitioned F7 training dataset."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from ai_platform.ml.training.contract import (
    MODEL_FEATURE_COLUMNS,
    NON_FEATURE_COLUMNS,
    OUTPUT_COLUMNS,
)


@dataclass(frozen=True)
class DatasetSplits:
    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame


@dataclass(frozen=True)
class ModelingSplit:
    X: pd.DataFrame
    y: pd.Series
    diagnostics: pd.DataFrame


def _read_split(root: Path, split: str) -> pd.DataFrame:
    parts = sorted((root / f"split={split}").glob("*.parquet"))
    if not parts:
        raise ValueError(f"{split} partition is empty")
    frames = [pd.read_parquet(part) for part in parts]
    frame = pd.concat(frames, ignore_index=True)
    if tuple(frame.columns) != OUTPUT_COLUMNS:
        missing = sorted(set(OUTPUT_COLUMNS).difference(frame.columns))
        extra = sorted(set(frame.columns).difference(OUTPUT_COLUMNS))
        raise ValueError(
            f"{split} columns do not match contract; missing={missing} extra={extra}"
        )
    if frame.empty:
        raise ValueError(f"{split} partition is empty")
    if frame["transaction_id"].isna().any():
        raise ValueError(f"{split} transaction_id contains null")
    if frame["transaction_id"].duplicated().any():
        raise ValueError(f"{split} contains duplicate transaction_id")
    if frame["label"].isna().any() or not set(frame["label"].unique()).issubset(
        {0, 1}
    ):
        raise ValueError(f"{split} label must be non-null and binary")
    if set(frame["label"].unique()) != {0, 1}:
        raise ValueError(f"{split} must contain both label classes")
    if set(frame["split"].unique()) != {split}:
        raise ValueError(f"{split} stored split value does not match partition")
    return frame


def load_dataset(path: Path) -> DatasetSplits:
    """Load the three explicit temporal partitions without Hive inference."""
    root = Path(path)
    dataset = DatasetSplits(
        train=_read_split(root, "train"),
        validation=_read_split(root, "validation"),
        test=_read_split(root, "test"),
    )
    identifiers = {
        name: set(getattr(dataset, name)["transaction_id"])
        for name in ("train", "validation", "test")
    }
    pairs = (("train", "validation"), ("train", "test"), ("validation", "test"))
    for left, right in pairs:
        overlap = identifiers[left].intersection(identifiers[right])
        if overlap:
            raise ValueError(
                f"transaction_id overlap between {left} and {right}: {len(overlap)}"
            )
    return dataset


def prepare_modeling_splits(
    dataset: DatasetSplits,
) -> dict[str, ModelingSplit]:
    result: dict[str, ModelingSplit] = {}
    for name in ("train", "validation", "test"):
        frame = getattr(dataset, name).reset_index(drop=True)
        result[name] = ModelingSplit(
            X=frame[list(MODEL_FEATURE_COLUMNS)].copy(),
            y=frame["label"].copy(),
            diagnostics=frame[list(NON_FEATURE_COLUMNS)].copy(),
        )
    return result
