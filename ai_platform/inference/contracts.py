"""Source-level model schema shared by training and online assembly.

This module deliberately does not describe a KServe tensor signature. The
registered MLflow artifact is the authority for that runtime-specific layout.
"""

from __future__ import annotations

import hashlib
import json

from ai_platform.ml.training.contract import (
    DERIVED_FEATURE_COLUMNS,
    HISTORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    REQUEST_TIME_FEATURE_COLUMNS,
)


MODEL_INPUT_COLUMNS = MODEL_FEATURE_COLUMNS
MODEL_OUTPUT_COLUMNS = ("fraud_probability", "fraud_prediction")

# This fingerprints only the source DataFrame column contract. It must not be
# presented as the serving tensor signature of an uninspected model artifact.
MODEL_SCHEMA_SHA256 = hashlib.sha256(
    json.dumps(
        {
            "input_columns": MODEL_INPUT_COLUMNS,
            "output_columns": MODEL_OUTPUT_COLUMNS,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


def validate_model_schema() -> None:
    if len(REQUEST_TIME_FEATURE_COLUMNS) != 4:
        raise RuntimeError("request-time feature contract must contain 4 columns")
    if len(HISTORICAL_FEATURE_COLUMNS) != 31:
        raise RuntimeError("historical feature contract must contain 31 columns")
    if len(DERIVED_FEATURE_COLUMNS) != 6:
        raise RuntimeError("derived feature contract must contain 6 columns")
    if len(MODEL_INPUT_COLUMNS) != 41:
        raise RuntimeError("model input contract must contain 41 columns")
    if MODEL_INPUT_COLUMNS != MODEL_FEATURE_COLUMNS:
        raise RuntimeError("online input order differs from training contract")


validate_model_schema()
