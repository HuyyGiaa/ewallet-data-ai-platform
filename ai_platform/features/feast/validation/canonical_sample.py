"""Compare Feast retrieval with direct Gold values for canonical cases."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from ai_platform.features.feast import FEATURE_REPO, RUNTIME_DATA_ROOT
from ai_platform.features.feast.retrieval.historical import (
    get_store,
    retrieve_combined,
)
from ai_platform.features.feast.schema import FEATURE_SPECS


ENTITY_COLUMNS = (
    "user_id",
    "account_id",
    "device_id",
    "merchant_id",
    "event_timestamp",
)


def values_equal(expected, actual) -> bool:
    if pd.isna(expected) and pd.isna(actual):
        return True
    if isinstance(expected, float) or isinstance(actual, float):
        return math.isclose(float(expected), float(actual), rel_tol=1e-9, abs_tol=1e-9)
    return expected == actual


def validate_canonical_sample(
    sample_path: Path = RUNTIME_DATA_ROOT / "validation" / "canonical_sample",
) -> dict:
    sample = pd.read_parquet(sample_path).sort_values("sample_case").reset_index(drop=True)
    entity_df = sample[list(ENTITY_COLUMNS)].copy()
    retrieved = retrieve_combined(get_store(FEATURE_REPO), entity_df)
    mismatches = []
    comparable = 0
    for row_index, expected_row in sample.iterrows():
        actual_row = retrieved.iloc[row_index]
        for view_name, spec in FEATURE_SPECS.items():
            for feature in spec["features"]:
                expected_column = f"expected__{view_name}__{feature}"
                actual_column = f"{view_name}__{feature}"
                expected = expected_row[expected_column]
                actual = actual_row[actual_column]
                comparable += 1
                if not values_equal(expected, actual):
                    mismatches.append(
                        {
                            "case": expected_row["sample_case"],
                            "feature": actual_column,
                            "expected": None if pd.isna(expected) else expected,
                            "actual": None if pd.isna(actual) else actual,
                        }
                    )
    result = {
        "sample_rows": len(sample),
        "sample_cases": sample["sample_case"].tolist(),
        "comparable_values": comparable,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "status": "PASS" if not mismatches else "FAIL",
        "labels_passed_to_feast": False,
    }
    print(json.dumps(result, indent=2, default=str))
    return result


def main() -> int:
    result = validate_canonical_sample()
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
