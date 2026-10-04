"""Fail-hard validation for built F7 training dataset partitions."""

from __future__ import annotations

import pandas as pd

from ai_platform.ml.training.contract import (
    FORBIDDEN_MODEL_COLUMNS,
    FORBIDDEN_OUTPUT_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    OUTPUT_COLUMNS,
)
from ai_platform.ml.training.temporal import assign_temporal_split


def validate_training_frame(
    frame: pd.DataFrame,
    source: pd.DataFrame,
) -> dict[str, object]:
    checks: dict[str, bool] = {}
    checks["row_count_parity"] = len(frame) == len(source)
    checks["transaction_id_unique"] = not bool(
        frame["transaction_id"].duplicated().any()
    )
    checks["transaction_ids_preserved"] = set(frame["transaction_id"]) == set(
        source["transaction_id"]
    )
    checks["labels_complete"] = not bool(frame["label"].isna().any())
    checks["label_domain"] = bool(frame["label"].isin([0, 1]).all())
    checks["required_columns_exact"] = tuple(frame.columns) == OUTPUT_COLUMNS
    checks["forbidden_model_columns_absent"] = not bool(
        FORBIDDEN_MODEL_COLUMNS.intersection(MODEL_FEATURE_COLUMNS)
    )
    checks["post_transaction_columns_absent"] = not bool(
        FORBIDDEN_OUTPUT_COLUMNS.intersection(frame.columns)
    )
    expected_split = assign_temporal_split(frame["event_timestamp"])
    checks["temporal_split_deterministic"] = frame["split"].astype(
        "string"
    ).equals(expected_split)
    checks["merchant_null_rows_preserved"] = int(frame["merchant_id"].isna().sum()) == int(
        source["merchant_id"].isna().sum()
    )

    peer_source = source[
        source.duplicated(["user_id", "event_timestamp"], keep=False)
    ]
    checks["same_timestamp_peer_rows_preserved"] = set(
        peer_source["transaction_id"]
    ).issubset(set(frame["transaction_id"]))

    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise ValueError(f"Training validation failed: {failed}")
    return {
        "status": "PASS",
        "checks": checks,
        "rows": len(frame),
        "model_feature_count": len(MODEL_FEATURE_COLUMNS),
    }
