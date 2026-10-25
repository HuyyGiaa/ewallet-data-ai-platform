"""Compare deterministic table-level validation summaries for Spark A/B runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping

from data_platform.storage.scripts.config import (
    CORE_GOLD_TABLES,
    HISTORICAL_FEATURE_TABLES,
    silver_tables,
)


BRONZE_TABLES = tuple(silver_tables(include_fraud_labels=True))
EXPECTED_TABLES = {
    "dp1-bronze": set(BRONZE_TABLES),
    "dp2-silver": set(BRONZE_TABLES),
    "dp2-gold": set(CORE_GOLD_TABLES),
    "dp3-features": set(HISTORICAL_FEATURE_TABLES),
}
REQUIRED_INVARIANTS = {
    "dp1-bronze": {"source_coverage", "source_counts_match"},
    "dp2-silver": {
        "logical_transaction_count_match",
        "transaction_id_unique",
        "fraud_labels_separate",
    },
    "dp2-gold": {"business_invariants", "no_label_leakage"},
    "dp3-features": {
        "grain_invariants",
        "event_timestamp_populated",
        "no_label_leakage",
    },
}


def compare_summaries(
    baseline: Mapping[str, object],
    optimized: Mapping[str, object],
) -> dict[str, object]:
    stage = str(baseline.get("stage", ""))
    if stage != optimized.get("stage") or stage not in EXPECTED_TABLES:
        raise ValueError("validation summaries must use one supported identical stage")
    for key in ("benchmark_id", "dataset_version", "git_sha"):
        if baseline.get(key) != optimized.get(key):
            raise ValueError(f"non-comparable {key}")
    if baseline.get("spark_profile") != "baseline":
        raise ValueError("first summary must be baseline")
    if optimized.get("spark_profile") != "optimized":
        raise ValueError("second summary must be optimized")
    baseline_tables = baseline.get("tables")
    optimized_tables = optimized.get("tables")
    if not isinstance(baseline_tables, Mapping) or not isinstance(
        optimized_tables, Mapping
    ):
        raise ValueError("tables must be objects")
    if set(baseline_tables) != EXPECTED_TABLES[stage] or set(
        optimized_tables
    ) != EXPECTED_TABLES[stage]:
        raise ValueError(f"{stage} table inventory is not exact")

    differences: list[str] = []
    for table in sorted(EXPECTED_TABLES[stage]):
        left = baseline_tables[table]
        right = optimized_tables[table]
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            raise ValueError(f"invalid table summary: {table}")
        for key in ("schema_sha256", "row_count", "content_fingerprint"):
            if left.get(key) != right.get(key):
                differences.append(f"{table}.{key}")

    for profile_name, summary in (("baseline", baseline), ("optimized", optimized)):
        invariants = summary.get("invariants")
        if not isinstance(invariants, Mapping):
            raise ValueError(f"{profile_name} invariants must be an object")
        failed = sorted(
            name
            for name in REQUIRED_INVARIANTS[stage]
            if invariants.get(name) is not True
        )
        differences.extend(f"{profile_name}.invariant.{name}" for name in failed)

    return {
        "benchmark_id": baseline["benchmark_id"],
        "dataset_version": baseline["dataset_version"],
        "git_sha": baseline["git_sha"],
        "stage": stage,
        "output_equivalent": not differences,
        "differences": differences,
        "method": (
            "exact table inventory/schema/row counts/invariants plus deterministic "
            "table content fingerprints; temporal [T-window,T) behavior is "
            "covered by same-Git executable tests, not inferred from output; "
            "not a byte-for-byte Delta comparison"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-summary", type=Path, required=True)
    parser.add_argument("--optimized-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare_summaries(
        json.loads(args.baseline_summary.read_text(encoding="utf-8")),
        json.loads(args.optimized_summary.read_text(encoding="utf-8")),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("PASS" if result["output_equivalent"] else "FAIL")
    return 0 if result["output_equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
