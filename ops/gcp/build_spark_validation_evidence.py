"""Join semantic output evidence with original Spark application metrics.

This keeps the read-only validation batch runtime separate from the ingestion
performance measurements used for Method 2 winner selection.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from data_platform.config.spark_benchmark import (
    ORIGINAL_PERFORMANCE_SCHEMA,
    SEMANTIC_SUMMARY_SCHEMA,
    VALIDATION_EVIDENCE_SCHEMA,
    original_performance_digest,
    semantic_summary_digest,
    validate_stage_evidence,
    validation_evidence_digest,
)
from data_platform.config.spark_profiles import validate_git_sha
from ops.gcp.validate_spark_output_equivalence import (
    EXPECTED_TABLES,
    REQUIRED_INVARIANTS,
)


def build_validation_evidence(
    summary: Mapping[str, object],
    original_performance: Mapping[str, object],
    *,
    pair_manifest: Mapping[str, object],
    spark_profile: str,
) -> dict[str, object]:
    """Build collector input without copying validation-job performance."""

    checked_summary = validate_stage_evidence(
        pair_manifest, summary, expected_profile=spark_profile
    )
    if checked_summary.get("schema_version") != SEMANTIC_SUMMARY_SCHEMA:
        raise ValueError("semantic summary schema_version is missing or unsupported")
    if checked_summary.get("semantic_summary_digest") != semantic_summary_digest(
        checked_summary
    ):
        raise ValueError("semantic summary digest mismatch")
    if checked_summary.get("status") != "COMPLETE":
        raise ValueError("semantic summary status must be COMPLETE")
    execution = checked_summary.get("execution")
    if not isinstance(execution, Mapping):
        raise ValueError("semantic summary execution identity is required")
    if execution.get("kind") != "read_only_semantic_scan":
        raise ValueError("semantic summary must come from a read-only scan")
    if execution.get("source_output_run_id") != checked_summary["output_run_id"]:
        raise ValueError("semantic summary source execution mismatch")
    validation_batch_id = execution.get("validation_batch_id")
    if not isinstance(validation_batch_id, str) or not validation_batch_id:
        raise ValueError("semantic summary validation_batch_id is required")
    if validation_batch_id == checked_summary["output_run_id"]:
        raise ValueError("validation and ingestion executions must be distinct")
    validation_git_sha = validate_git_sha(
        str(execution.get("validation_git_sha", ""))
    )

    stage = str(checked_summary["stage"])
    tables = checked_summary.get("tables")
    if not isinstance(tables, Mapping) or set(tables) != EXPECTED_TABLES[stage]:
        raise ValueError("semantic summary table inventory is not exact")
    invariants = checked_summary.get("invariants")
    if not isinstance(invariants, Mapping) or any(
        invariants.get(name) is not True for name in REQUIRED_INVARIANTS[stage]
    ):
        raise ValueError("semantic summary business invariants did not pass")

    if original_performance.get("schema_version") != ORIGINAL_PERFORMANCE_SCHEMA:
        raise ValueError("original performance schema_version is missing or unsupported")
    if original_performance.get(
        "original_performance_digest"
    ) != original_performance_digest(original_performance):
        raise ValueError("original performance digest mismatch")
    expected_performance_identity = {
        "source_execution_kind": "original_ingestion_batch",
        "batch_id": checked_summary["output_run_id"],
        "output_run_id": checked_summary["output_run_id"],
        "pair_manifest_digest": checked_summary["pair_manifest_digest"],
    }
    for field, expected in expected_performance_identity.items():
        if original_performance.get(field) != expected:
            raise ValueError(f"original performance {field} mismatch")
    source_uri = original_performance.get("event_log_source_uri")
    if not isinstance(source_uri, str) or not source_uri.startswith("gs://"):
        raise ValueError("original performance event_log_source_uri is required")

    required_metrics = (
        "application_runtime_seconds",
        "spill_bytes",
        "task_count",
    )
    for name in required_metrics:
        value = original_performance.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"original performance metric {name} is invalid")
    if float(original_performance["application_runtime_seconds"]) <= 0:
        raise ValueError("application_runtime_seconds must be positive")
    if int(original_performance["task_count"]) <= 0:
        raise ValueError("task_count must be positive")

    output_file_count = 0
    output_total_bytes = 0
    for table_name, table in tables.items():
        if not isinstance(table, Mapping):
            raise ValueError(f"semantic table summary is invalid: {table_name}")
        layout = table.get("physical_layout")
        if not isinstance(layout, Mapping):
            raise ValueError(f"physical layout is missing: {table_name}")
        for metric in ("file_count", "total_bytes"):
            value = layout.get(metric)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"physical layout metric is invalid: {table_name}.{metric}")
        output_file_count += int(layout["file_count"])
        output_total_bytes += int(layout["total_bytes"])

    identity_fields = (
        "pair_manifest_digest",
        "benchmark_id",
        "dataset_version",
        "git_sha",
        "spark_profile",
        "stage",
        "output_run_id",
        "output_root",
        "upstream_run_ids",
        "spark_properties",
        "spark_properties_digest",
    )
    result: dict[str, object] = {
        "schema_version": VALIDATION_EVIDENCE_SCHEMA,
        "status": "PASS",
        **{field: checked_summary[field] for field in identity_fields},
        "source_run_ids": list(checked_summary["upstream_run_ids"]),
        "application_runtime_seconds": float(
            original_performance["application_runtime_seconds"]
        ),
        "spill_bytes": int(original_performance["spill_bytes"]),
        "task_count": int(original_performance["task_count"]),
        "output_file_count": output_file_count,
        "output_total_bytes": output_total_bytes,
        "metric_provenance": {
            "application_runtime_seconds": "original_spark_event_log",
            "spill_bytes": "original_spark_event_log",
            "task_count": "original_spark_event_log",
            "output_file_count": "semantic_summary_physical_layout",
            "output_total_bytes": "semantic_summary_physical_layout",
            "original_performance_digest": original_performance[
                "original_performance_digest"
            ],
            "original_event_log_source_uri": source_uri,
            "semantic_summary_digest": checked_summary[
                "semantic_summary_digest"
            ],
            "validation_batch_id": validation_batch_id,
            "validation_git_sha": validation_git_sha,
            "ingestion_batch_id": checked_summary["output_run_id"],
        },
    }
    result["validation_evidence_digest"] = validation_evidence_digest(result)
    return result


def _load(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary-json", type=Path, required=True)
    parser.add_argument("--original-performance-json", type=Path, required=True)
    parser.add_argument("--pair-manifest", type=Path, required=True)
    parser.add_argument("--spark-profile", choices=("baseline", "optimized"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_validation_evidence(
        _load(args.summary_json),
        _load(args.original_performance_json),
        pair_manifest=_load(args.pair_manifest),
        spark_profile=args.spark_profile,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
