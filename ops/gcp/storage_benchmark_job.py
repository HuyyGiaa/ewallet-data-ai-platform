"""Managed Spark job for an isolated Delta physical-layout A/B benchmark.

This file is submitted only by ``run_storage_benchmark --execute``.  It never
writes to source paths: each selected source snapshot is shallow-cloned into an
immutable benchmark prefix. Z-order candidates receive separate compaction and
Z-order arms so their incremental effects can be measured.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from data_platform.config.spark_profiles import validate_git_sha
from data_platform.storage.cloud_optimization import (
    compare_equivalence,
    compare_layouts,
    validate_benchmark_paths,
)


GRAIN_KEYS = {
    "transactions": ("transaction_id",),
    "fact_transactions": ("transaction_id",),
    "obt_transaction_enriched": ("transaction_id",),
    "feat_user_behavior": ("user_id", "event_timestamp"),
    "feat_account_behavior": ("account_id", "event_timestamp"),
    "feat_device_behavior": ("device_id", "event_timestamp"),
    "feat_merchant_behavior": ("merchant_id", "event_timestamp"),
}
REQUIRED_COLUMNS = {
    "transactions": (
        "transaction_id", "account_id", "user_id", "device_id", "type",
        "amount", "currency", "status", "channel", "old_balance",
        "new_balance", "timestamp", "ingested_at", "event_date",
    ),
    "fact_transactions": (
        "transaction_id", "user_id", "account_id", "device_id", "date_key",
        "type", "status", "channel", "currency", "amount", "old_balance",
        "new_balance", "timestamp", "ingested_at", "event_date",
    ),
    "obt_transaction_enriched": (
        "transaction_id", "user_id", "kyc_verified", "user_created_at",
        "account_id", "account_type", "account_currency", "account_created_at",
        "device_id", "device_type", "os", "first_seen_at", "date_key",
        "day_of_week", "month", "quarter", "year", "is_weekend", "type",
        "status", "channel", "currency", "amount", "old_balance",
        "new_balance", "timestamp", "ingested_at", "event_date",
    ),
    "feat_user_behavior": (
        "user_id", "event_timestamp", "user_tx_count_5m", "user_tx_count_1h",
        "user_tx_count_24h", "user_amount_sum_1h",
        "user_amount_observation_count_30d", "user_distinct_merchants_24h",
    ),
    "feat_account_behavior": (
        "account_id", "event_timestamp", "account_tx_count_5m",
        "account_tx_count_1h", "account_tx_count_24h", "account_amount_sum_1h",
        "account_amount_sum_24h", "account_amount_observation_count_30d",
    ),
    "feat_device_behavior": (
        "device_id", "event_timestamp", "device_age_seconds",
        "device_tx_count_1h", "device_tx_count_24h", "device_amount_sum_24h",
    ),
    "feat_merchant_behavior": (
        "merchant_id", "event_timestamp", "merchant_tx_count_10m",
        "merchant_tx_count_1h", "merchant_tx_count_24h",
        "merchant_unique_users_10m", "merchant_unique_users_1h",
        "merchant_amount_sum_1h",
    ),
}


def _safe_delta_uri(uri: str) -> str:
    if not uri.startswith("gs://") or "`" in uri or ";" in uri:
        raise ValueError(f"unsafe Delta URI: {uri}")
    return uri.rstrip("/")


def _path_exists(spark: Any, uri: str) -> bool:
    path = spark._jvm.org.apache.hadoop.fs.Path(uri)
    filesystem = path.getFileSystem(spark._jsc.hadoopConfiguration())
    return bool(filesystem.exists(path))


QUERY_WARMUP_ITERATIONS = 1
QUERY_MEASURED_ITERATIONS = 5


def _create_shallow_clone(
    spark: Any, source: str, target: str, *, source_version: int
) -> None:
    source = _safe_delta_uri(source)
    target = _safe_delta_uri(target)
    if _path_exists(spark, target):
        raise ValueError(f"immutable benchmark target already exists: {target}")
    spark.sql(
        f"CREATE TABLE delta.`{target}` SHALLOW CLONE delta.`{source}` "
        f"VERSION AS OF {int(source_version)}"
    ).collect()


def _table_detail(delta_table: Any) -> dict[str, object]:
    row = delta_table.detail().first().asDict(recursive=True)
    return {
        "delta_version": int(delta_table.history(1).first()["version"]),
        "file_count": int(row["numFiles"]),
        "total_bytes": int(row["sizeInBytes"]),
        "partition_columns": list(row.get("partitionColumns") or []),
        "table_properties": dict(row.get("properties") or {}),
    }


def _layout_from_active_files(spark: Any, delta_table: Any) -> dict[str, object]:
    """Read active file names from the Delta relation and object sizes only."""

    frame = delta_table.toDF()
    sizes: list[int] = []
    partition_bytes: dict[str, int] = defaultdict(int)
    partition_files: dict[str, int] = defaultdict(int)
    detail = _table_detail(delta_table)
    partition_columns = set(detail["partition_columns"])
    for uri in frame.inputFiles():
        path = spark._jvm.org.apache.hadoop.fs.Path(uri)
        filesystem = path.getFileSystem(spark._jsc.hadoopConfiguration())
        size = int(filesystem.getFileStatus(path).getLen())
        sizes.append(size)
        parts = []
        for component in unquote(urlsplit(uri).path).split("/"):
            if "=" in component and component.split("=", 1)[0] in partition_columns:
                parts.append(component)
        partition = "/".join(sorted(parts)) if parts else "<unpartitioned>"
        partition_bytes[partition] += size
        partition_files[partition] += 1
    sizes.sort()
    count = len(sizes)
    p95_index = max(0, math.ceil(0.95 * count) - 1) if count else 0
    return {
        **detail,
        "file_size_bytes": {
            "minimum": sizes[0] if sizes else 0,
            "median": (
                int((sizes[count // 2 - 1] + sizes[count // 2]) / 2)
                if count and count % 2 == 0
                else sizes[count // 2] if count else 0
            ),
            "p95": sizes[p95_index] if sizes else 0,
            "maximum": sizes[-1] if sizes else 0,
            "average": int(sum(sizes) / count) if count else 0,
        },
        "very_small_file_count": sum(size < 16 * 1024 * 1024 for size in sizes),
        "partition_count": len(partition_bytes),
        "partitions": [
            {
                "partition": key,
                "file_count": partition_files[key],
                "bytes": partition_bytes[key],
            }
            for key in sorted(partition_bytes)
        ],
        "inspection_mode": "delta_active_file_index_plus_object_metadata",
    }


def _fingerprint(
    dataframe: Any, table_name: str, keys: Sequence[str]
) -> dict[str, object]:
    from pyspark.sql import functions as F

    import hashlib

    columns = sorted(dataframe.columns)
    row_hash = F.xxhash64(
        *[
            F.coalesce(F.col(column).cast("string"), F.lit("<NULL>"))
            for column in columns
        ]
    )
    aggregate = (
        dataframe.select(row_hash.alias("row_hash"))
        .agg(
            F.count("*").alias("rows"),
            F.sum(F.col("row_hash").cast("decimal(38,0)")).alias("total"),
            F.min("row_hash").alias("minimum"),
            F.max("row_hash").alias("maximum"),
        )
        .first()
    )
    rows = int(aggregate["rows"])
    null_key_condition = None
    for key in keys:
        condition = F.col(key).isNull()
        null_key_condition = (
            condition if null_key_condition is None else null_key_condition | condition
        )
    null_key_rows = dataframe.filter(null_key_condition).count()
    distinct_rows = dataframe.select(*keys).distinct().count()
    try:
        required_columns = list(REQUIRED_COLUMNS[table_name])
    except KeyError as exc:
        raise ValueError(f"missing required-column contract for {table_name}") from exc
    missing_columns = sorted(set(required_columns) - set(dataframe.columns))
    if missing_columns:
        raise ValueError(
            f"{table_name} is missing contract columns: {missing_columns}"
        )
    null_required_rows = 0
    if required_columns:
        required_condition = None
        for column in required_columns:
            condition = F.col(column).isNull()
            required_condition = (
                condition
                if required_condition is None
                else required_condition | condition
            )
        null_required_rows = dataframe.filter(required_condition).count()
    invalid_amount_rows = 0
    if "amount" in dataframe.columns:
        invalid_amount = (
            F.col("amount") <= 0
            if table_name == "transactions"
            else F.col("amount") < 0
        )
        invalid_amount_rows = dataframe.filter(invalid_amount).count()
    invalid_balance_rows = 0
    balance_columns = [
        column
        for column in ("old_balance", "new_balance")
        if column in dataframe.columns
    ]
    if balance_columns:
        invalid_balance = None
        for column in balance_columns:
            condition = F.col(column) < 0
            invalid_balance = (
                condition if invalid_balance is None else invalid_balance | condition
            )
        invalid_balance_rows = dataframe.filter(invalid_balance).count()
    invalid_status_rows = (
        dataframe.filter(~F.col("status").isin("success", "failed", "pending")).count()
        if "status" in dataframe.columns
        else 0
    )
    labels_absent = {
        "label", "fraud_type", "is_fraud"
    }.isdisjoint(dataframe.columns)
    business_details = {
        "null_grain_rows": null_key_rows,
        "null_required_rows": null_required_rows,
        "invalid_amount_rows": invalid_amount_rows,
        "invalid_balance_rows": invalid_balance_rows,
        "invalid_status_rows": invalid_status_rows,
        "label_columns_absent": labels_absent,
    }
    components = (
        rows,
        str(aggregate["total"] or 0),
        int(aggregate["minimum"] or 0),
        int(aggregate["maximum"] or 0),
    )
    payload = ":".join(map(str, components)).encode("utf-8")
    return {
        "schema_sha256": hashlib.sha256(
            dataframe.schema.json().encode("utf-8")
        ).hexdigest(),
        "row_count": rows,
        "grain_unique": rows == distinct_rows and null_key_rows == 0,
        "business_invariants": labels_absent and all(
            value == 0
            for key, value in business_details.items()
            if key != "label_columns_absent"
        ),
        "business_invariant_details": business_details,
        "content_fingerprint": hashlib.sha256(payload).hexdigest(),
        "fingerprint_components": {
            "xxhash64_decimal_sum": components[1],
            "xxhash64_min": components[2],
            "xxhash64_max": components[3],
        },
    }


def _date_range(layout: Mapping[str, Any]) -> tuple[str, str]:
    values = (layout.get("global_statistics") or {}).get("event_date") or {}
    maximum = values.get("maximum")
    if maximum is None:
        raise ValueError("event_date statistics are required for bounded queries")
    end = date.fromisoformat(str(maximum)[:10])
    return (str(end - timedelta(days=6)), str(end))


def _sql_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _query_sql(
    table: Mapping[str, Any], query: Mapping[str, Any], *, relation: str
) -> str:
    if relation not in {"baseline_table", "compacted_table", "optimized_table"}:
        raise ValueError("unsupported benchmark relation")
    table_name = str(table["table"])
    query_class = str(query["class"])
    if query_class == "date_range":
        start, end = _date_range(table)
        return (
            "SELECT COUNT(*) AS row_count, "
            "SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, "
            "CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) "
            "AS average_amount "
            f"FROM {relation} "
            f"WHERE event_date BETWEEN DATE '{start}' AND DATE '{end}'"
        )
    if query_class == "date_entity_lookup":
        start, end = _date_range(table)
        stats = (table.get("global_statistics") or {}).get("merchant_id") or {}
        merchant_id = stats.get("minimum")
        if merchant_id is None:
            raise ValueError(f"{table_name}.merchant_id statistics are required")
        return (
            "SELECT COUNT(*) AS row_count, "
            "SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount "
            f"FROM {relation} "
            f"WHERE event_date BETWEEN DATE '{start}' AND DATE '{end}' "
            f"AND merchant_id = {_sql_literal(merchant_id)}"
        )
    if query_class == "date_status_aggregation":
        start, end = _date_range(table)
        return (
            "SELECT channel, COUNT(*) AS row_count, "
            "SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount "
            f"FROM {relation} "
            f"WHERE event_date BETWEEN DATE '{start}' AND DATE '{end}' "
            "AND status = 'failed' GROUP BY channel ORDER BY channel"
        )
    if query_class == "entity_time_range":
        entity = str(query["predicate_columns"][0])
        stats = (table.get("global_statistics") or {}).get(entity) or {}
        value = stats.get("minimum")
        if value is None:
            raise ValueError(f"{table_name}.{entity} statistics are required")
        return (
            f"SELECT * FROM {relation} "
            f"WHERE {entity} = {_sql_literal(value)} "
            "ORDER BY event_timestamp LIMIT 1000"
        )
    raise ValueError(f"unsupported query class: {query_class}")


def _execute_query(spark: Any, sql: str) -> tuple[list[dict[str, object]], float, str]:
    started = time.perf_counter()
    frame = spark.sql(sql)
    rows = [row.asDict(recursive=True) for row in frame.collect()]
    elapsed = time.perf_counter() - started
    return rows, elapsed, frame._jdf.queryExecution().executedPlan().toString()


def _assert_representative_result(
    query: Mapping[str, Any], rows: Sequence[Mapping[str, object]]
) -> None:
    if not query.get("require_non_empty"):
        return
    if not rows:
        raise ValueError(f"representative query is empty: {query['name']}")
    counts = [row.get("row_count") for row in rows if "row_count" in row]
    if counts and sum(int(value or 0) for value in counts) <= 0:
        raise ValueError(f"representative query matched no rows: {query['name']}")


def _run_layout_queries(
    spark: Any,
    table: Mapping[str, Any],
    *,
    layout_uris: Mapping[str, str],
) -> dict[str, list[dict[str, object]]]:
    """Warm every layout, then rotate execution order for five measurements."""

    sides = tuple(layout_uris)
    if sides not in {("baseline", "optimized"), ("baseline", "compacted", "optimized")}:
        raise ValueError("benchmark layouts must be baseline/optimized with optional compacted")
    for side, uri in layout_uris.items():
        spark.read.format("delta").load(uri).createOrReplaceTempView(f"{side}_table")
    results: dict[str, list[dict[str, object]]] = {side: [] for side in sides}
    for query in table["queries"]:
        sqls = {
            side: _query_sql(table, query, relation=f"{side}_table")
            for side in sides
        }
        canonical_result: dict[str, str] = {}
        physical_plans: dict[str, str] = {}
        for _ in range(QUERY_WARMUP_ITERATIONS):
            for side in sides:
                _execute_query(spark, sqls[side])
        samples: dict[str, list[float]] = {side: [] for side in sides}
        rows_by_side: dict[str, list[dict[str, object]]] = {}
        for iteration in range(QUERY_MEASURED_ITERATIONS):
            offset = iteration % len(sides)
            order = sides[offset:] + sides[:offset]
            for side in order:
                rows, elapsed, physical_plan = _execute_query(spark, sqls[side])
                serialized = json.dumps(rows, sort_keys=True, default=str)
                _assert_representative_result(query, rows)
                if side in canonical_result and canonical_result[side] != serialized:
                    raise ValueError(f"non-deterministic query result: {query['name']}")
                canonical_result[side] = serialized
                rows_by_side[side] = rows
                physical_plans[side] = physical_plan
                samples[side].append(elapsed)
        if any(
            canonical_result[side] != canonical_result["baseline"]
            for side in sides[1:]
        ):
            raise ValueError(f"query result mismatch: {query['name']}")
        for side in sides:
            results[side].append(
                {
                    "name": query["name"],
                    "sql": sqls[side],
                    "runtime_seconds": median(samples[side]),
                    "runtime_samples_seconds": samples[side],
                    "warmup_iterations": QUERY_WARMUP_ITERATIONS,
                    "measured_iterations": QUERY_MEASURED_ITERATIONS,
                    "measurement_order": "rotating",
                    "result": rows_by_side[side],
                    "result_json": canonical_result[side],
                    "physical_plan": physical_plans[side],
                    "task_metrics": {
                        "available": False,
                        "reason": (
                            "task/shuffle/spill/straggler metrics remain in Managed "
                            "Spark event logs; no stable supported per-query API is "
                            "guaranteed"
                        ),
                    },
                }
            )
    return results


def _compare_query_sets(
    before_queries: Sequence[Mapping[str, Any]],
    after_queries: Sequence[Mapping[str, Any]],
    *,
    before_detail: Mapping[str, Any],
    after_detail: Mapping[str, Any],
    arm: str,
) -> list[dict[str, object]]:
    comparisons: list[dict[str, object]] = []
    for left, right in zip(before_queries, after_queries, strict=True):
        comparisons.append(
            {
                "comparison_arm": arm,
                "query": left["name"],
                **compare_layouts(
                    before={
                        "file_count": before_detail["file_count"],
                        "median_file_size": before_detail["file_size_bytes"]["median"],
                        "runtime_seconds": left["runtime_seconds"],
                    },
                    after={
                        "file_count": after_detail["file_count"],
                        "median_file_size": after_detail["file_size_bytes"]["median"],
                        "runtime_seconds": right["runtime_seconds"],
                    },
                ),
                "before_runtime_seconds": left["runtime_seconds"],
                "after_runtime_seconds": right["runtime_seconds"],
                "before_scan_bytes": None,
                "after_scan_bytes": None,
                "scan_bytes_status": "unavailable_from_supported_query_api",
                "output_equivalent": True,
            }
        )
    return comparisons


def _workload_verdict(comparisons: Sequence[Mapping[str, Any]]) -> str:
    before = sum(float(item["before_runtime_seconds"]) for item in comparisons)
    after = sum(float(item["after_runtime_seconds"]) for item in comparisons)
    if any(float(item["runtime_change_percent"]) > 5 for item in comparisons):
        return "REGRESSION"
    if after < before:
        return "IMPROVEMENT"
    if after > before:
        return "REGRESSION"
    return "NO_CHANGE"


def _recommend_layout(
    *,
    overall_verdict: str,
    compaction_verdict: str | None,
    zorder_incremental_verdict: str | None,
) -> str:
    if (
        overall_verdict == "IMPROVEMENT"
        and zorder_incremental_verdict == "IMPROVEMENT"
    ):
        return "KEEP_ZORDER"
    if compaction_verdict == "IMPROVEMENT":
        return "KEEP_COMPACTION_ONLY"
    if compaction_verdict is None and overall_verdict == "IMPROVEMENT":
        return "KEEP_COMPACTION"
    return "DO_NOT_ADOPT"


def _upload_json(uri: str, value: object) -> None:
    from google.cloud import storage

    parsed = urlsplit(uri)
    storage.Client().bucket(parsed.netloc).blob(parsed.path.lstrip("/")).upload_from_string(
        json.dumps(value, indent=2, default=str) + "\n",
        content_type="application/json",
        if_generation_match=0,
    )


def _upload_text(uri: str, value: str, content_type: str) -> None:
    from google.cloud import storage

    parsed = urlsplit(uri)
    storage.Client().bucket(parsed.netloc).blob(parsed.path.lstrip("/")).upload_from_string(
        value, content_type=content_type, if_generation_match=0
    )


def execute_plan(
    *,
    plan: Mapping[str, Any],
    benchmark_root: str,
    dataset_version: str,
    git_sha: str,
) -> dict[str, object]:
    from delta.tables import DeltaTable
    from pyspark.sql import SparkSession

    if plan.get("benchmark_root") != benchmark_root:
        raise ValueError("benchmark root differs from immutable plan")
    if plan.get("dataset_version") != dataset_version or plan.get("git_sha") != git_sha:
        raise ValueError("dataset version/Git SHA differs from immutable plan")
    validate_git_sha(str(plan.get("source_spark_git_sha", "")))
    spark = SparkSession.builder.appName("cloud-storage-layout-ab").getOrCreate()
    if spark.version != "3.5.3":
        raise ValueError(
            f"Managed Spark runtime drifted from audited Spark 3.5.3: {spark.version}"
        )
    spark.conf.set("spark.databricks.delta.optimize.repartition.enabled", "true")
    started_at = datetime.now(timezone.utc)
    evidence: dict[str, Any] = {
        "benchmark_id": plan["benchmark_id"],
        "dataset_version": dataset_version,
        "git_sha": git_sha,
        "source_spark_git_sha": plan["source_spark_git_sha"],
        "source_run_ids": plan["source_run_ids"],
        "managed_spark_runtime": "2.2",
        "spark_version": spark.version,
        "dataproc_tier": "standard",
        "delta_version": "3.2.0",
        "runtime_properties": {
            key: spark.conf.get(key, "<unset>")
            for key in (
                "spark.dynamicAllocation.enabled",
                "spark.dynamicAllocation.initialExecutors",
                "spark.dynamicAllocation.maxExecutors",
                "spark.dynamicAllocation.profile",
                "spark.sql.shuffle.partitions",
                "spark.sql.adaptive.enabled",
                "spark.sql.adaptive.skewJoin.enabled",
                "spark.databricks.delta.optimize.repartition.enabled",
            )
        },
        "started_at": started_at.isoformat(),
        "tables": [],
    }
    try:
        for table in plan["tables"]:
            if not table.get("selected"):
                continue
            table_name = str(table["table"])
            source = str(table["uri"])
            baseline = str(table["baseline_uri"])
            compacted = (
                str(table["compacted_uri"])
                if table.get("compacted_uri") is not None
                else None
            )
            optimized = str(table["optimized_uri"])
            validate_benchmark_paths(
                source_uri=source,
                benchmark_root=benchmark_root,
                baseline_uri=baseline,
                optimized_uri=optimized,
                compacted_uri=compacted,
            )
            source_version = int(table["delta_version"])
            live_source = DeltaTable.forPath(spark, source)
            live_version = int(live_source.history(1).first()["version"])
            if live_version != source_version:
                raise ValueError(
                    f"source Delta version changed for {table['table']}: "
                    f"planned={source_version}, live={live_version}"
                )
            _create_shallow_clone(
                spark, source, baseline, source_version=source_version
            )
            _create_shallow_clone(
                spark, source, optimized, source_version=source_version
            )
            if compacted is not None:
                _create_shallow_clone(
                    spark, source, compacted, source_version=source_version
                )
            before_delta = DeltaTable.forPath(spark, baseline)
            optimized_delta = DeltaTable.forPath(spark, optimized)
            before_fingerprint = _fingerprint(
                before_delta.toDF(), table_name, GRAIN_KEYS[table_name]
            )
            decision = table["optimization"]
            rewrite_evidence: dict[str, Any] = {}
            if decision["action"] == "ZORDER":
                if compacted is None:
                    raise ValueError("ZORDER plan requires an isolated compaction arm")
                compacted_delta = DeltaTable.forPath(spark, compacted)
                compact_started = time.perf_counter()
                compact_metrics = [
                    row.asDict(recursive=True)
                    for row in compacted_delta.optimize().executeCompaction().collect()
                ]
                rewrite_evidence["compaction"] = {
                    "seconds": time.perf_counter() - compact_started,
                    "metrics": compact_metrics,
                }
                zorder_started = time.perf_counter()
                zorder_metrics = [
                    row.asDict(recursive=True)
                    for row in optimized_delta.optimize().executeZOrderBy(
                        *decision["columns"]
                    ).collect()
                ]
                rewrite_evidence["zorder"] = {
                    "seconds": time.perf_counter() - zorder_started,
                    "metrics": zorder_metrics,
                }
            elif decision["action"] == "COMPACTION_ONLY":
                rewrite_started = time.perf_counter()
                metrics = [
                    row.asDict(recursive=True)
                    for row in optimized_delta.optimize().executeCompaction().collect()
                ]
                rewrite_evidence["compaction"] = {
                    "seconds": time.perf_counter() - rewrite_started,
                    "metrics": metrics,
                }
            else:
                raise ValueError("selected table lacks a supported optimization")
            layout_uris = {"baseline": baseline}
            if compacted is not None:
                layout_uris["compacted"] = compacted
            layout_uris["optimized"] = optimized
            query_results = _run_layout_queries(
                spark, table, layout_uris=layout_uris
            )
            after_fingerprint = _fingerprint(
                optimized_delta.toDF(), table_name, GRAIN_KEYS[table_name]
            )
            equivalence = compare_equivalence(before_fingerprint, after_fingerprint)
            if not equivalence["equivalent"]:
                raise ValueError(f"logical equivalence failed for {table['table']}")
            compacted_equivalence = None
            compacted_delta = None
            if compacted is not None:
                compacted_delta = DeltaTable.forPath(spark, compacted)
                compacted_fingerprint = _fingerprint(
                    compacted_delta.toDF(), table_name, GRAIN_KEYS[table_name]
                )
                compacted_equivalence = compare_equivalence(
                    before_fingerprint, compacted_fingerprint
                )
                if not compacted_equivalence["equivalent"]:
                    raise ValueError(
                        f"compaction equivalence failed for {table['table']}"
                    )
            before_detail = {
                "delta_version": int(table["delta_version"]),
                "file_count": int(table["file_count"]),
                "total_bytes": int(table["total_bytes"]),
                "partition_columns": list(table["partition_columns"]),
                "table_properties": dict(table["table_properties"]),
                "file_size_bytes": dict(table["file_size_bytes"]),
                "very_small_file_count": int(table["very_small_file_count"]),
                "partition_count": int(table["partition_count"]),
                "partitions": list(table["partitions"]),
                "statistics_columns": list(table["statistics_columns"]),
                "statistics_profiles": dict(table["statistics_profiles"]),
                "inspection_mode": table["inspection_mode"],
            }
            after_detail = _layout_from_active_files(spark, optimized_delta)
            compacted_detail = (
                _layout_from_active_files(spark, compacted_delta)
                if compacted_delta is not None
                else None
            )
            query_comparisons = _compare_query_sets(
                query_results["baseline"],
                query_results["optimized"],
                before_detail=before_detail,
                after_detail=after_detail,
                arm="baseline_to_optimized",
            )
            ablation_comparisons: list[dict[str, object]] = []
            compaction_comparisons: list[dict[str, object]] = []
            compaction_verdict = None
            zorder_incremental_verdict = None
            if compacted_detail is not None:
                compaction_comparisons = _compare_query_sets(
                    query_results["baseline"],
                    query_results["compacted"],
                    before_detail=before_detail,
                    after_detail=compacted_detail,
                    arm="baseline_to_compaction",
                )
                compaction_verdict = _workload_verdict(compaction_comparisons)
                ablation_comparisons = _compare_query_sets(
                    query_results["compacted"],
                    query_results["optimized"],
                    before_detail=compacted_detail,
                    after_detail=after_detail,
                    arm="compaction_to_zorder",
                )
                zorder_incremental_verdict = _workload_verdict(ablation_comparisons)
            workload_verdict = _workload_verdict(query_comparisons)
            recommendation = _recommend_layout(
                overall_verdict=workload_verdict,
                compaction_verdict=compaction_verdict,
                zorder_incremental_verdict=zorder_incremental_verdict,
            )
            evidence["tables"].append(
                {
                    "table": table["table"],
                    "stage": table["stage"],
                    "source_uri": source,
                    "baseline_uri": baseline,
                    "compacted_uri": compacted,
                    "optimized_uri": optimized,
                    "layout_before": before_detail,
                    "layout_compacted": compacted_detail,
                    "layout_after": after_detail,
                    "queries_before": query_results["baseline"],
                    "queries_compacted": query_results.get("compacted"),
                    "queries_after": query_results["optimized"],
                    "query_comparison": query_comparisons,
                    "compaction_comparison": compaction_comparisons,
                    "ablation_comparison": ablation_comparisons,
                    "workload_verdict": workload_verdict,
                    "compaction_verdict": compaction_verdict,
                    "zorder_incremental_verdict": zorder_incremental_verdict,
                    "recommendation": recommendation,
                    "equivalence": equivalence,
                    "compacted_equivalence": compacted_equivalence,
                    "rewrite": rewrite_evidence,
                }
            )
        evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
        evidence["status"] = "PASS"
        return evidence
    finally:
        spark.stop()


def write_evidence(root: str, evidence: Mapping[str, Any]) -> None:
    tables = list(evidence["tables"])
    _upload_json(f"{root}/evidence/layout_before.json", {
        "tables": [table["layout_before"] | {"table": table["table"]} for table in tables]
    })
    _upload_json(f"{root}/evidence/layout_after.json", {
        "tables": [table["layout_after"] | {"table": table["table"]} for table in tables]
    })
    compacted_tables = [table for table in tables if table["layout_compacted"] is not None]
    if compacted_tables:
        _upload_json(f"{root}/evidence/layout_compacted.json", {
            "tables": [
                table["layout_compacted"] | {"table": table["table"]}
                for table in compacted_tables
            ]
        })
    _upload_json(f"{root}/evidence/queries_before.json", {
        "tables": [{"table": table["table"], "queries": table["queries_before"]} for table in tables]
    })
    _upload_json(f"{root}/evidence/queries_after.json", {
        "tables": [{"table": table["table"], "queries": table["queries_after"]} for table in tables]
    })
    if compacted_tables:
        _upload_json(f"{root}/evidence/queries_compacted.json", {
            "tables": [
                {"table": table["table"], "queries": table["queries_compacted"]}
                for table in compacted_tables
            ]
        })
    _upload_json(f"{root}/evidence/equivalence.json", {
        "tables": [
            {
                "table": table["table"],
                "baseline_to_optimized": table["equivalence"],
                "baseline_to_compacted": table["compacted_equivalence"],
            }
            for table in tables
        ]
    })
    _upload_json(f"{root}/evidence/comparison.json", evidence)
    output = io.StringIO()
    columns = [
        "table", "comparison_arm", "query", "before_file_count", "after_file_count",
        "before_median_file_size", "after_median_file_size",
        "before_p95_file_size", "after_p95_file_size",
        "before_scan_bytes", "after_scan_bytes",
        "before_runtime_seconds", "after_runtime_seconds",
        "runtime_change_percent", "verdict", "output_equivalent",
        "workload_verdict", "compaction_verdict", "zorder_incremental_verdict",
        "recommendation",
        "rewrite_seconds",
    ]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for table in tables:
        comparisons = (
            table["query_comparison"]
            + table["compaction_comparison"]
            + table["ablation_comparison"]
        )
        for comparison in comparisons:
            is_ablation = comparison["comparison_arm"] == "compaction_to_zorder"
            is_compaction = comparison["comparison_arm"] == "baseline_to_compaction"
            before_layout = (
                table["layout_compacted"] if is_ablation else table["layout_before"]
            )
            after_layout = (
                table["layout_compacted"] if is_compaction else table["layout_after"]
            )
            rewrite_seconds = sum(
                float(operation["seconds"])
                for operation in table["rewrite"].values()
            )
            writer.writerow(
                {
                    "table": table["table"],
                    "comparison_arm": comparison["comparison_arm"],
                    "query": comparison["query"],
                    "before_file_count": before_layout["file_count"],
                    "after_file_count": after_layout["file_count"],
                    "before_median_file_size": before_layout["file_size_bytes"]["median"],
                    "after_median_file_size": after_layout["file_size_bytes"]["median"],
                    "before_p95_file_size": before_layout["file_size_bytes"]["p95"],
                    "after_p95_file_size": after_layout["file_size_bytes"]["p95"],
                    "before_scan_bytes": comparison["before_scan_bytes"],
                    "after_scan_bytes": comparison["after_scan_bytes"],
                    "before_runtime_seconds": comparison["before_runtime_seconds"],
                    "after_runtime_seconds": comparison["after_runtime_seconds"],
                    "runtime_change_percent": comparison["runtime_change_percent"],
                    "verdict": comparison["verdict"],
                    "output_equivalent": comparison["output_equivalent"],
                    "workload_verdict": table["workload_verdict"],
                    "compaction_verdict": table["compaction_verdict"],
                    "zorder_incremental_verdict": table["zorder_incremental_verdict"],
                    "recommendation": table["recommendation"],
                    "rewrite_seconds": rewrite_seconds,
                }
            )
    _upload_text(f"{root}/evidence/comparison.csv", output.getvalue(), "text/csv")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--benchmark-root", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--git-sha", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    validate_git_sha(args.git_sha)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    evidence = execute_plan(
        plan=plan,
        benchmark_root=args.benchmark_root.rstrip("/"),
        dataset_version=args.dataset_version,
        git_sha=args.git_sha,
    )
    write_evidence(args.benchmark_root.rstrip("/"), evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
