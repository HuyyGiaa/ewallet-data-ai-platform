"""Pure planning primitives for the isolated cloud Delta storage benchmark.

The module deliberately has no Spark, GCS, or Delta runtime imports.  The
metadata inspector and the future Managed Spark job use these functions, while
unit tests can exercise the safety and decision contracts without cloud access.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from statistics import mean, median
from typing import Any, Iterable, Mapping, Sequence


MIB = 1024 * 1024
GIB = 1024 * MIB
DEFAULT_SMALL_FILE_THRESHOLD_BYTES = 16 * MIB
HIGH_CARDINALITY_IDENTIFIERS = {
    "transaction_id",
    "user_id",
    "account_id",
    "device_id",
    "merchant_id",
}

PINNED_CAPABILITIES: dict[str, object] = {
    "managed_spark_runtime": "2.2",
    "spark": "3.5.3",
    "delta": "3.2.0",
    "scala": "2.13",
    "optimize": True,
    "zorder": True,
    "data_skipping": True,
    "optimize_repartition_control": True,
    "optimized_write": True,
    "auto_compaction": True,
    # Architecture is frozen; this experiment does not introduce clustering.
    "liquid_clustering_in_scope": False,
}


@dataclass(frozen=True)
class TableAccessPattern:
    """Known downstream access behavior used to interpret physical metadata."""

    query_importance: str
    partition_predicates: tuple[str, ...] = ()
    zorder_candidates: tuple[str, ...] = ()


ACCESS_PATTERNS: dict[str, TableAccessPattern] = {
    "transactions": TableAccessPattern(
        "high", ("event_date",), ("merchant_id", "account_id")
    ),
    "fact_transactions": TableAccessPattern(
        "high", ("event_date",), ("merchant_id", "account_id")
    ),
    "obt_transaction_enriched": TableAccessPattern(
        "high", ("event_date",), ("merchant_id", "user_id")
    ),
    "feat_user_behavior": TableAccessPattern("high", (), ("user_id",)),
    "feat_account_behavior": TableAccessPattern("high", (), ("account_id",)),
    "feat_device_behavior": TableAccessPattern("medium", (), ("device_id",)),
    "feat_merchant_behavior": TableAccessPattern(
        "medium", (), ("merchant_id",)
    ),
}


def _nearest_rank(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return int(ordered[index])


def _partition_key(values: Mapping[str, object]) -> str:
    if not values:
        return "<unpartitioned>"
    return "/".join(f"{key}={values[key]}" for key in sorted(values))


def summarize_layout(
    *,
    table: str,
    stage: str,
    uri: str,
    delta_version: int,
    partition_columns: Sequence[str],
    properties: Mapping[str, str],
    actions: Iterable[Mapping[str, Any]],
    small_file_threshold_bytes: int = DEFAULT_SMALL_FILE_THRESHOLD_BYTES,
) -> dict[str, object]:
    """Summarize active AddFile metadata without scanning Parquet payloads."""

    action_list = list(actions)
    sizes = [int(action["size_bytes"]) for action in action_list]
    row_values = [
        int(action["num_records"])
        for action in action_list
        if action.get("num_records") is not None
    ]
    partitions: dict[str, dict[str, int]] = defaultdict(
        lambda: {"file_count": 0, "bytes": 0, "rows": 0, "files_with_rows": 0}
    )
    stats_columns: set[str] = set()
    stats_profiles: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "files_with_min": 0,
            "files_with_max": 0,
            "distinct_minimums": set(),
            "distinct_maximums": set(),
        }
    )
    global_min: dict[str, object] = {}
    global_max: dict[str, object] = {}
    for action in action_list:
        partition_values = action.get("partition_values") or {}
        key = _partition_key(partition_values)
        partitions[key]["file_count"] += 1
        partitions[key]["bytes"] += int(action["size_bytes"])
        if action.get("num_records") is not None:
            partitions[key]["rows"] += int(action["num_records"])
            partitions[key]["files_with_rows"] += 1
        stats_columns.update((action.get("min_values") or {}).keys())
        stats_columns.update((action.get("max_values") or {}).keys())
        # Delta does not duplicate partition values in AddFile min/max stats.
        # Preserve partition bounds separately so deterministic date predicates
        # can be planned without scanning Parquet payloads.
        for column, value in partition_values.items():
            if value is not None:
                try:
                    global_min[column] = min(global_min.get(column, value), value)
                    global_max[column] = max(global_max.get(column, value), value)
                except TypeError:
                    pass
        for column, value in (action.get("min_values") or {}).items():
            if value is not None:
                profile = stats_profiles[column]
                profile["files_with_min"] = int(profile["files_with_min"]) + 1
                profile["distinct_minimums"].add(str(value))
                try:
                    global_min[column] = min(global_min.get(column, value), value)
                except TypeError:
                    pass
        for column, value in (action.get("max_values") or {}).items():
            if value is not None:
                profile = stats_profiles[column]
                profile["files_with_max"] = int(profile["files_with_max"]) + 1
                profile["distinct_maximums"].add(str(value))
                try:
                    global_max[column] = max(global_max.get(column, value), value)
                except TypeError:
                    pass

    partition_rows = []
    for key, values in sorted(partitions.items()):
        complete = values["files_with_rows"] == values["file_count"]
        partition_rows.append(
            {
                "partition": key,
                "file_count": values["file_count"],
                "bytes": values["bytes"],
                "rows": values["rows"] if complete else None,
                "row_count_complete": complete,
            }
        )
    very_small = sum(size < small_file_threshold_bytes for size in sizes)
    total_bytes = sum(sizes)
    file_count = len(sizes)
    files_per_partition = [part["file_count"] for part in partition_rows]
    bytes_per_partition = [part["bytes"] for part in partition_rows]
    return {
        "table": table,
        "stage": stage,
        "uri": uri.rstrip("/"),
        "delta_version": int(delta_version),
        "row_count": sum(row_values) if len(row_values) == file_count else None,
        "row_count_source": (
            "delta_add_file_stats" if len(row_values) == file_count else "unavailable"
        ),
        "total_bytes": total_bytes,
        "file_count": file_count,
        "file_size_bytes": {
            "minimum": min(sizes) if sizes else 0,
            "median": int(median(sizes)) if sizes else 0,
            "p95": _nearest_rank(sizes, 0.95),
            "maximum": max(sizes) if sizes else 0,
            "average": int(mean(sizes)) if sizes else 0,
        },
        "small_file_threshold_bytes": int(small_file_threshold_bytes),
        "very_small_file_count": very_small,
        "very_small_file_ratio": (
            round(very_small / file_count, 6) if file_count else 0.0
        ),
        "partition_columns": list(partition_columns),
        "partition_count": len(partition_rows),
        "files_per_partition_p95": _nearest_rank(files_per_partition, 0.95),
        "partition_size_bytes": {
            "minimum": min(bytes_per_partition) if bytes_per_partition else 0,
            "median": int(median(bytes_per_partition)) if bytes_per_partition else 0,
            "p95": _nearest_rank(bytes_per_partition, 0.95),
            "maximum": max(bytes_per_partition) if bytes_per_partition else 0,
        },
        "small_partition_count": sum(
            value < 64 * MIB for value in bytes_per_partition
        ),
        "partitions": partition_rows,
        "table_properties": dict(sorted(properties.items())),
        "statistics_columns": sorted(stats_columns),
        "partition_value_columns": sorted(
            {
                column
                for action in action_list
                for column in (action.get("partition_values") or {})
            }
        ),
        "statistics_profiles": {
            column: {
                "files_with_min_max": min(
                    int(profile["files_with_min"]),
                    int(profile["files_with_max"]),
                ),
                "coverage_ratio": round(
                    min(
                        int(profile["files_with_min"]),
                        int(profile["files_with_max"]),
                    )
                    / file_count,
                    6,
                )
                if file_count
                else 0.0,
                "distinct_file_minimums": len(profile["distinct_minimums"]),
                "distinct_file_maximums": len(profile["distinct_maximums"]),
            }
            for column, profile in sorted(stats_profiles.items())
        },
        "global_statistics": {
            column: {"minimum": global_min.get(column), "maximum": global_max.get(column)}
            for column in sorted(set(global_min) | set(global_max))
        },
        "inspection_mode": "delta_log_metadata_only",
    }


def select_table(
    summary: Mapping[str, Any], *, query_importance: str
) -> dict[str, object]:
    """Select only material, queried tables with evidence of fragmentation."""

    if query_importance not in {"high", "medium", "low", "none"}:
        raise ValueError(f"unsupported query importance: {query_importance}")
    if query_importance == "none":
        return {"selected": False, "reasons": ["no representative read workload"]}
    file_count = int(summary.get("file_count", 0))
    total_bytes = int(summary.get("total_bytes", 0))
    small_count = int(summary.get("very_small_file_count", 0))
    median_size = int((summary.get("file_size_bytes") or {}).get("median", 0))
    small_ratio = small_count / file_count if file_count else 0.0
    fragmented = file_count >= 32 and (
        small_ratio >= 0.5 or median_size < 32 * MIB
    )
    material = total_bytes >= 256 * MIB
    selected = material and fragmented
    reasons: list[str] = []
    if selected:
        reasons.append(
            f"small-file evidence: {small_count}/{file_count} files below threshold"
        )
        reasons.append(f"{query_importance}-importance downstream reads")
    else:
        if not material:
            reasons.append("table is below the 256 MiB materiality floor")
        if not fragmented:
            reasons.append("no material small-file fragmentation")
    return {"selected": selected, "reasons": reasons}


def decide_partitioning(
    *,
    partition_columns: Sequence[str],
    partition_count: int,
    total_bytes: int,
    files_per_partition_p95: int,
    partition_size_bytes: Mapping[str, int] | None,
    small_partition_count: int,
    access: TableAccessPattern,
) -> dict[str, object]:
    """Audit current partitions; the first A/B never changes logical schema."""

    existing = list(partition_columns)
    if existing:
        matched = all(column in access.partition_predicates for column in existing)
        avg_bytes = total_bytes // max(1, partition_count)
        distribution = dict(partition_size_bytes or {})
        median_bytes = int(distribution.get("median", avg_bytes))
        maximum_bytes = int(distribution.get("maximum", median_bytes))
        skew_ratio = maximum_bytes / max(1, median_bytes)
        small_ratio = small_partition_count / max(1, partition_count)
        if (
            matched
            and median_bytes >= 64 * MIB
            and small_ratio <= 0.1
            and skew_ratio <= 10
        ):
            return {
                "action": "KEEP",
                "columns": existing,
                "reason": (
                    "time predicates support pruning and average partition size is "
                    "large enough; compaction addresses files within partitions"
                ),
                "average_partition_bytes": avg_bytes,
                "median_partition_bytes": median_bytes,
                "small_partition_ratio": round(small_ratio, 6),
                "partition_byte_skew_ratio": round(skew_ratio, 3),
                "files_per_partition_p95": files_per_partition_p95,
            }
        return {
            "action": "REVIEW_AFTER_A_B",
            "columns": existing,
            "reason": "metadata suggests possible over-partitioning; no change in this benchmark",
            "average_partition_bytes": avg_bytes,
            "median_partition_bytes": median_bytes,
            "small_partition_ratio": round(small_ratio, 6),
            "partition_byte_skew_ratio": round(skew_ratio, 3),
            "files_per_partition_p95": files_per_partition_p95,
        }

    proposed = set(access.partition_predicates)
    if proposed & HIGH_CARDINALITY_IDENTIFIERS:
        return {
            "action": "KEEP_UNPARTITIONED",
            "columns": [],
            "reason": (
                "candidate predicate is a high-cardinality identifier; use data "
                "skipping/Z-order instead of creating many tiny partitions"
            ),
        }
    return {
        "action": "KEEP_UNPARTITIONED",
        "columns": [],
        "reason": "no evidence-backed low-cardinality partition column is available",
    }


def decide_zorder(
    *,
    access: TableAccessPattern,
    partition_columns: Sequence[str],
    statistics_columns: Sequence[str],
    statistics_profiles: Mapping[str, Mapping[str, object]],
    equality_predicate_columns: Sequence[str],
) -> dict[str, object]:
    """Choose useful equality predicates with broad, non-constant file stats."""

    partitions = set(partition_columns)
    stats = set(statistics_columns)
    equality = set(equality_predicate_columns)
    columns = []
    evidence: dict[str, object] = {}
    for column in access.zorder_candidates:
        profile = statistics_profiles.get(column) or {}
        qualifies = (
            column not in partitions
            and column in stats
            and column in equality
            and float(profile.get("coverage_ratio", 0.0)) >= 0.9
            and max(
                int(profile.get("distinct_file_minimums", 0)),
                int(profile.get("distinct_file_maximums", 0)),
            )
            >= 8
        )
        if qualifies:
            columns.append(column)
            evidence[column] = dict(profile)
        if len(columns) == 2:
            break
    if not columns:
        return {
            "action": "COMPACTION_ONLY",
            "columns": [],
            "reason": (
                "no non-partition equality predicate has >=90% min/max coverage "
                "and at least eight distinct file-level bounds"
            ),
            "evidence": {},
        }
    return {
        "action": "ZORDER",
        "columns": columns,
        "reason": (
            "columns occur in bounded entity predicates, are not partition keys, "
            "and have broad, non-constant Delta min/max statistics"
        ),
        "evidence": evidence,
        "cost": "full selected-table rewrite; benchmark separately from compaction",
    }


def validate_benchmark_paths(
    *,
    source_uri: str,
    benchmark_root: str,
    baseline_uri: str,
    optimized_uri: str,
    compacted_uri: str | None = None,
) -> None:
    """Reject plans that could write into canonical or Spark A/B run prefixes."""

    values = tuple(
        value
        for value in (
            source_uri,
            benchmark_root,
            baseline_uri,
            compacted_uri,
            optimized_uri,
        )
        if value is not None
    )
    if any(not value.startswith("gs://") for value in values):
        raise ValueError("all storage benchmark URIs must use gs://")
    marker = "/lakehouse/benchmarks/storage/"
    if marker not in benchmark_root or "/lakehouse/runs/" in benchmark_root:
        raise ValueError("benchmark root must use isolated lakehouse/benchmarks/storage")
    root = benchmark_root.rstrip("/") + "/"
    if not baseline_uri.startswith(root + "baseline/"):
        raise ValueError("baseline URI is outside isolated benchmark root")
    if not optimized_uri.startswith(root + "optimized/"):
        raise ValueError("optimized URI is outside isolated benchmark root")
    if compacted_uri is not None and not compacted_uri.startswith(root + "compacted/"):
        raise ValueError("compacted URI is outside isolated benchmark root")
    targets = [baseline_uri, optimized_uri]
    if compacted_uri is not None:
        targets.append(compacted_uri)
    if source_uri.startswith(root) or len(set(targets)) != len(targets):
        raise ValueError("source and benchmark layout paths must be distinct")


def compare_equivalence(
    before: Mapping[str, object], after: Mapping[str, object]
) -> dict[str, object]:
    required = (
        "schema_sha256",
        "row_count",
        "grain_unique",
        "business_invariants",
        "content_fingerprint",
    )
    differences = [key for key in required if before.get(key) != after.get(key)]
    failed = [
        key
        for key in ("grain_unique", "business_invariants")
        if before.get(key) is not True or after.get(key) is not True
    ]
    differences.extend(key for key in failed if key not in differences)
    return {
        "equivalent": not differences,
        "differences": differences,
        "method": (
            "schema hash, exact row count, grain/business invariants and "
            "partition-independent aggregate row fingerprint"
        ),
    }


def compare_layouts(
    *, before: Mapping[str, float], after: Mapping[str, float]
) -> dict[str, object]:
    before_runtime = float(before["runtime_seconds"])
    after_runtime = float(after["runtime_seconds"])
    change = ((after_runtime - before_runtime) / before_runtime) * 100
    return {
        "file_count_change": int(after["file_count"] - before["file_count"]),
        "median_file_size_change": (
            float(after["median_file_size"]) - float(before["median_file_size"])
        ),
        "runtime_change_percent": round(change, 3),
        "verdict": "IMPROVEMENT" if change < 0 else "REGRESSION" if change > 0 else "NO_CHANGE",
    }
