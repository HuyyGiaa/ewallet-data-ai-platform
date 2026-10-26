"""Compare deterministic table-level validation summaries for Spark A/B runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping

from data_platform.config.spark_benchmark import (
    EQUIVALENCE_SCHEMA,
    equivalence_artifact_digest,
    output_identity,
    validate_pair_manifest,
    validate_stage_evidence,
)
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
    *,
    pair_manifest: Mapping[str, object],
    baseline_evidence: Mapping[str, object],
    optimized_evidence: Mapping[str, object],
) -> dict[str, object]:
    pair = validate_pair_manifest(pair_manifest)
    validate_stage_evidence(pair, baseline_evidence, expected_profile="baseline")
    validate_stage_evidence(pair, optimized_evidence, expected_profile="optimized")
    validate_stage_evidence(pair, baseline, expected_profile="baseline")
    validate_stage_evidence(pair, optimized, expected_profile="optimized")
    stage = str(pair["stage"])
    if stage not in EXPECTED_TABLES:
        raise ValueError("validation summaries must use one supported stage")
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
        required_table_fields = (
            "schema_sha256",
            "row_count",
            "semantic_content_fingerprint",
            "required_null_counts",
            "partition_columns",
        )
        for key in required_table_fields:
            if key not in left or key not in right:
                differences.append(f"{table}.{key}.missing")
                continue
            if left.get(key) != right.get(key):
                differences.append(f"{table}.{key}")
        for side, item in (("baseline", left), ("optimized", right)):
            layout = item.get("physical_layout")
            required_layout = {
                "file_count",
                "total_bytes",
                "file_size_min",
                "file_size_median",
                "file_size_p95",
                "file_size_max",
            }
            if not isinstance(layout, Mapping) or not required_layout.issubset(layout):
                differences.append(f"{side}.{table}.physical_layout.missing")

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

    equivalent = not differences
    result: dict[str, object] = {
        "schema_version": EQUIVALENCE_SCHEMA,
        "pair_manifest_digest": pair["pair_manifest_digest"],
        "benchmark_id": pair["benchmark_id"],
        "dataset_version": pair["dataset_version"],
        "git_sha": pair["git_sha"],
        "stage": stage,
        "source_run_ids": list(pair["upstream_run_ids"]),
        "compared_outputs": {
            "baseline": output_identity(pair, "baseline"),
            "optimized": output_identity(pair, "optimized"),
        },
        "physical_layouts": {
            "baseline": {
                table: baseline_tables[table].get("physical_layout")
                for table in sorted(EXPECTED_TABLES[stage])
            },
            "optimized": {
                table: optimized_tables[table].get("physical_layout")
                for table in sorted(EXPECTED_TABLES[stage])
            },
        },
        "output_equivalent": equivalent,
        "high_confidence_equivalence": equivalent,
        "differences": differences,
        "method": (
            "exact inventory/schema/partition/null/row invariants plus two "
            "independent commutative row-hash aggregates; this is high-confidence "
            "semantic equivalence, not cryptographic identity. Temporal "
            "[T-window,T) behavior is "
            "covered by same-Git executable tests, not inferred from output; "
            "not a byte-for-byte Delta comparison"
        ),
    }
    result["equivalence_artifact_digest"] = equivalence_artifact_digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-summary", type=Path, required=True)
    parser.add_argument("--optimized-summary", type=Path, required=True)
    parser.add_argument("--pair-manifest", type=Path, required=True)
    parser.add_argument("--baseline-evidence", type=Path, required=True)
    parser.add_argument("--optimized-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare_summaries(
        json.loads(args.baseline_summary.read_text(encoding="utf-8")),
        json.loads(args.optimized_summary.read_text(encoding="utf-8")),
        pair_manifest=json.loads(args.pair_manifest.read_text(encoding="utf-8")),
        baseline_evidence=json.loads(
            args.baseline_evidence.read_text(encoding="utf-8")
        ),
        optimized_evidence=json.loads(
            args.optimized_evidence.read_text(encoding="utf-8")
        ),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print("PASS" if result["output_equivalent"] else "FAIL")
    return 0 if result["output_equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
