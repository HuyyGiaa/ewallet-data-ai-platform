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
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from data_platform.config.spark_profiles import validate_git_sha
from data_platform.storage.cloud_optimization import (
    classify_runtime_change,
    compare_equivalence,
    compare_layouts,
    recommend_layout,
    rewrite_outcome,
    validate_benchmark_paths,
    workload_verdict,
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


def effective_conf(spark: Any, key: str) -> str:
    """Effective value of one Spark setting, or an explicit unset marker.

    No fallback is passed to ``spark.conf.get``: for an unset *typed* SQL key Spark
    3.5 converts the fallback with the key's type and raises (the defect fixed in
    ``aae22b08``). Without a fallback a SQL key returns Spark's default; a plain key
    with no default raises, which is recorded rather than guessed.
    """

    try:
        return str(spark.conf.get(key))
    except Exception as exc:  # NoSuchElementException from the JVM for unset plain keys
        return f"<unset: {type(exc).__name__}>"


def require_utc_session(spark: Any) -> None:
    """Refuse to measure unless the Spark session time zone is UTC.

    ``event_date`` is the UTC calendar day of ``timestamp`` in the campaign tables. The
    ``stats_only`` literal ``TIMESTAMP 'yyyy-MM-dd 00:00:00'`` and ``date_format`` are
    evaluated in the session zone, so any other zone shifts their window away from the
    partition window. The result-equality check would then fail late; this fails first.
    """

    zone = effective_conf(spark, "spark.sql.session.timeZone")
    if zone not in ("UTC", "Etc/UTC", "Z", "+00:00"):
        raise ValueError(f"spark.sql.session.timeZone must be UTC for the storage benchmark, got {zone!r}")


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
SUPPORTED_SPARK_PREFIX = "3.5."
RECORDED_PROPERTIES = (
    "spark.sql.session.timeZone",
    "spark.dynamicAllocation.enabled",
    "spark.dynamicAllocation.initialExecutors",
    "spark.dynamicAllocation.maxExecutors",
    "spark.sql.shuffle.partitions",
    "spark.sql.adaptive.enabled",
    "spark.sql.adaptive.skewJoin.enabled",
    "spark.databricks.delta.optimize.repartition.enabled",
    "spark.databricks.delta.optimize.maxFileSize",
    "spark.databricks.delta.optimize.minFileSize",
)


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


def _window(table: Mapping[str, Any]) -> tuple[str, str]:
    """Validated query window from the plan's data-derived parameters."""

    params = table.get("workload_parameters") or {}
    try:
        start = date.fromisoformat(str(params["window_start"]))
        end = date.fromisoformat(str(params["window_end"]))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{table.get('table')} lacks a valid workload window") from exc
    if end < start:
        raise ValueError("workload window ends before it starts")
    return str(start), str(end)


def _sql_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


RELATIONS = {"baseline_table", "compacted_table", "optimized_table"}


def _query_sql(
    table: Mapping[str, Any], query: Mapping[str, Any], *, relation: str
) -> str:
    """SQL of one workload query against one benchmark relation.

    The two partition-pruning controls express the ``date_range`` window without the
    partition column. Under the pinned UTC session ``event_date = to_date(timestamp)``,
    so all three return identical rows (the job verifies this before comparing):

    * ``date_range_stats_only`` - a ``timestamp`` range: no partition pruning, but Delta
      can still skip files with its per-file ``timestamp`` min/max statistics;
    * ``date_range_full_scan`` - the window on ``date_format(timestamp)``: neither
      partition pruning nor statistics-based skipping can apply.
    """

    if relation not in RELATIONS:
        raise ValueError("unsupported benchmark relation")
    start, end = _window(table)
    window = f"event_date BETWEEN DATE '{start}' AND DATE '{end}'"
    aggregate = (
        "SELECT COUNT(*) AS row_count, "
        "SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount, "
        "CAST(AVG(CAST(amount AS DECIMAL(38,6))) AS DECIMAL(38,6)) AS average_amount "
        f"FROM {relation} "
    )
    query_class = str(query["class"])
    if query_class == "date_range":
        return aggregate + f"WHERE {window}"
    if query_class == "date_range_stats_only":
        after_end = date.fromisoformat(end) + timedelta(days=1)
        return aggregate + (f"WHERE timestamp >= TIMESTAMP '{start} 00:00:00' "
                            f"AND timestamp < TIMESTAMP '{after_end} 00:00:00'")
    if query_class == "date_range_full_scan":
        return aggregate + f"WHERE date_format(timestamp, 'yyyy-MM-dd') BETWEEN '{start}' AND '{end}'"
    if query_class == "date_entity_lookup":
        merchant = (table.get("workload_parameters") or {}).get("merchant_id")
        if not merchant:
            raise ValueError(f"{table.get('table')} lacks a data-derived merchant_id")
        return aggregate + f"WHERE {window} AND merchant_id = {_sql_literal(merchant)}"
    if query_class == "date_status_aggregation":
        return (
            "SELECT channel, COUNT(*) AS row_count, "
            "SUM(CAST(amount AS DECIMAL(38,2))) AS total_amount "
            f"FROM {relation} WHERE {window} AND status = 'failed' "
            "GROUP BY channel ORDER BY channel"
        )
    raise ValueError(f"unsupported query class: {query_class}")


def scan_metrics(frame: Any) -> dict[str, object]:
    """File-scan metrics of an executed query, read from Spark's own plan metrics.

    Walks the executed plan (through adaptive and query-stage wrappers) and returns
    files, bytes and partitions read per scan node. When the plan cannot be read the
    result says so; no value is ever filled in.
    """

    try:
        nodes = [frame._jdf.queryExecution().executedPlan()]
        scans: list[dict[str, object]] = []
        while nodes:
            node = nodes.pop()
            name = str(node.nodeName())
            if name.startswith("AdaptiveSparkPlan"):
                nodes.append(node.executedPlan())
                continue
            if "QueryStage" in name:
                nodes.append(node.plan())
                continue
            if name.startswith("Scan "):
                metrics = node.metrics()

                def value(key: str) -> int | None:
                    return int(metrics.apply(key).value()) if metrics.contains(key) else None

                scans.append({"node": name, "files_read": value("numFiles"), "bytes_read": value("filesSize"),
                              "partitions_read": value("numPartitions"), "output_rows": value("numOutputRows")})
            children = node.children()
            nodes.extend(children.apply(index) for index in range(children.size()))
        return {"available": bool(scans), "scans": scans}
    except Exception as exc:  # the plan API is internal; report, never guess
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}


def _execute_query(spark: Any, sql: str) -> tuple[list[dict[str, object]], float, str, dict[str, object]]:
    started = time.perf_counter()
    frame = spark.sql(sql)
    rows = [row.asDict(recursive=True) for row in frame.collect()]
    elapsed = time.perf_counter() - started
    return rows, elapsed, frame._jdf.queryExecution().executedPlan().toString(), scan_metrics(frame)


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


def _measure(
    spark: Any, sqls: Mapping[str, str], query: Mapping[str, Any]
) -> dict[str, dict[str, object]]:
    """Warm every side once, then measure with a rotating order; results must agree."""

    sides = tuple(sqls)
    for _ in range(QUERY_WARMUP_ITERATIONS):
        for side in sides:
            _execute_query(spark, sqls[side])
    measured: dict[str, dict[str, Any]] = {side: {"samples": [], "orders": []} for side in sides}
    for iteration in range(QUERY_MEASURED_ITERATIONS):
        offset = iteration % len(sides)
        order = sides[offset:] + sides[:offset]
        for position, side in enumerate(order):
            rows, elapsed, plan, scans = _execute_query(spark, sqls[side])
            _assert_representative_result(query, rows)
            serialized = json.dumps(rows, sort_keys=True, default=str)
            if "result_json" in measured[side] and measured[side]["result_json"] != serialized:
                raise ValueError(f"non-deterministic query result: {query['name']}")
            measured[side].update(result=rows, result_json=serialized, physical_plan=plan, scan_metrics=scans)
            measured[side]["samples"].append(elapsed)
            measured[side]["orders"].append(position)
    if any(measured[side]["result_json"] != measured[sides[0]]["result_json"] for side in sides[1:]):
        raise ValueError(f"query result mismatch across sides: {query['name']}")
    return {
        side: {
            "sql": sqls[side],
            "runtime_seconds": median(values["samples"]),
            "runtime_samples_seconds": values["samples"],
            "position_in_rotation": values["orders"],
            "warmup_iterations": QUERY_WARMUP_ITERATIONS,
            "measured_iterations": QUERY_MEASURED_ITERATIONS,
            "measurement_order": "rotating",
            "result": values["result"],
            "result_json": values["result_json"],
            "physical_plan": values["physical_plan"],
            "scan_metrics": values["scan_metrics"],
        }
        for side, values in measured.items()
    }


def _run_layout_queries(
    spark: Any,
    table: Mapping[str, Any],
    *,
    layout_uris: Mapping[str, str],
) -> dict[str, list[dict[str, object]]]:
    """Measure every workload query on every layout arm."""

    sides = tuple(layout_uris)
    if sides not in {("baseline", "optimized"), ("baseline", "compacted", "optimized")}:
        raise ValueError("benchmark layouts must be baseline/optimized with optional compacted")
    for side, uri in layout_uris.items():
        spark.read.format("delta").load(uri).createOrReplaceTempView(f"{side}_table")
    results: dict[str, list[dict[str, object]]] = {side: [] for side in sides}
    for query in table["queries"]:
        sqls = {side: _query_sql(table, query, relation=f"{side}_table") for side in sides}
        for side, value in _measure(spark, sqls, query).items():
            results[side].append({"name": query["name"], **value})
    return results


def _run_partition_pruning(spark: Any, table: Mapping[str, Any]) -> dict[str, object]:
    """ST-D: one logical query with partition pruning, with statistics-only skipping and as a full scan.

    All three run on the unchanged baseline arm, so the differences isolate how the
    files to read are chosen from any layout rewrite.
    """

    query = {"name": "partition_pruning", "require_non_empty": True}
    sqls = {
        "pruned": _query_sql(table, {"class": "date_range"}, relation="baseline_table"),
        "stats_only": _query_sql(table, {"class": "date_range_stats_only"}, relation="baseline_table"),
        "full_scan": _query_sql(table, {"class": "date_range_full_scan"}, relation="baseline_table"),
    }
    measured = _measure(spark, sqls, query)
    full = measured["full_scan"]["runtime_samples_seconds"]
    return {
        "sides": measured,
        "comparisons": {
            "full_scan_to_pruned": classify_runtime_change(full, measured["pruned"]["runtime_samples_seconds"]),
            "full_scan_to_stats_only": classify_runtime_change(full, measured["stats_only"]["runtime_samples_seconds"]),
            "stats_only_to_pruned": classify_runtime_change(measured["stats_only"]["runtime_samples_seconds"],
                                                            measured["pruned"]["runtime_samples_seconds"]),
        },
    }


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
        comparisons.append({
            "comparison_arm": arm,
            "query": left["name"],
            **compare_layouts(
                before={"file_count": before_detail["file_count"],
                        "median_file_size": before_detail["file_size_bytes"]["median"],
                        "runtime_samples_seconds": left["runtime_samples_seconds"]},
                after={"file_count": after_detail["file_count"],
                       "median_file_size": after_detail["file_size_bytes"]["median"],
                       "runtime_samples_seconds": right["runtime_samples_seconds"]},
            ),
            "before_scan_metrics": left["scan_metrics"],
            "after_scan_metrics": right["scan_metrics"],
            "output_equivalent": left["result_json"] == right["result_json"],
        })
    return comparisons


def _optimize(delta_table: Any, columns: Sequence[str] | None) -> dict[str, object]:
    """Run bin-packing compaction (``columns`` None) or Z-order; return timing and metrics."""

    started = time.perf_counter()
    builder = delta_table.optimize()
    frame = builder.executeZOrderBy(*columns) if columns else builder.executeCompaction()
    rows = [row.asDict(recursive=True) for row in frame.collect()]
    return {"seconds": time.perf_counter() - started, "metrics": rows, "outcome": rewrite_outcome(rows)}


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
    spark = SparkSession.builder.appName("edai1-storage-layout-ab").getOrCreate()
    if not spark.version.startswith(SUPPORTED_SPARK_PREFIX):
        raise ValueError(f"Spark {spark.version} is outside the Delta 3.2 compatible line {SUPPORTED_SPARK_PREFIX}x")
    require_utc_session(spark)
    spark.conf.set("spark.databricks.delta.optimize.repartition.enabled", "true")
    started_at = datetime.now(timezone.utc)
    evidence: dict[str, Any] = {
        "benchmark_id": plan["benchmark_id"],
        "dataset_version": dataset_version,
        "git_sha": git_sha,
        "source_spark_git_sha": plan["source_spark_git_sha"],
        "source_campaign": plan.get("source_campaign"),
        "source_run_ids": plan["source_run_ids"],
        "managed_spark_runtime": "2.2",
        "spark_version": spark.version,
        "delta_version": "3.2.0",
        "runtime_properties": {key: effective_conf(spark, key) for key in RECORDED_PROPERTIES},
        "measurement": {"warmup_iterations": QUERY_WARMUP_ITERATIONS,
                        "measured_iterations": QUERY_MEASURED_ITERATIONS,
                        "order": "rotating per iteration", "verdict_rule": "classify_runtime_change"},
        "started_at": started_at.isoformat(),
        "tables": [],
    }
    try:
        for table in plan["tables"]:
            if not table.get("selected"):
                continue
            table_name = str(table["table"])
            source = str(table["uri"])
            baseline, compacted, optimized = (str(table[key]) for key in ("baseline_uri", "compacted_uri", "optimized_uri"))
            validate_benchmark_paths(source_uri=source, benchmark_root=benchmark_root, baseline_uri=baseline,
                                     optimized_uri=optimized, compacted_uri=compacted)
            if table["optimization"]["action"] != "ZORDER":
                raise ValueError("plan tables must carry the three-arm ZORDER ablation")
            source_version = int(table["delta_version"])
            live_version = int(DeltaTable.forPath(spark, source).history(1).first()["version"])
            if live_version != source_version:
                raise ValueError(f"source Delta version changed for {table_name}: "
                                 f"planned={source_version}, live={live_version}")
            for target in (baseline, compacted, optimized):
                _create_shallow_clone(spark, source, target, source_version=source_version)
            before_fingerprint = _fingerprint(DeltaTable.forPath(spark, baseline).toDF(), table_name, GRAIN_KEYS[table_name])
            compacted_delta = DeltaTable.forPath(spark, compacted)
            optimized_delta = DeltaTable.forPath(spark, optimized)
            rewrite = {
                "compaction": _optimize(compacted_delta, None),
                "zorder": _optimize(optimized_delta, table["optimization"]["columns"]),
            }
            layout_uris = {"baseline": baseline, "compacted": compacted, "optimized": optimized}
            query_results = _run_layout_queries(spark, table, layout_uris=layout_uris)
            pruning = _run_partition_pruning(spark, table)
            equivalence = {
                "baseline_to_compacted": compare_equivalence(before_fingerprint, _fingerprint(
                    compacted_delta.toDF(), table_name, GRAIN_KEYS[table_name])),
                "baseline_to_optimized": compare_equivalence(before_fingerprint, _fingerprint(
                    optimized_delta.toDF(), table_name, GRAIN_KEYS[table_name])),
            }
            for arm, result in equivalence.items():
                if not result["equivalent"]:
                    raise ValueError(f"logical equivalence failed for {table_name} ({arm})")
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
            compacted_detail = _layout_from_active_files(spark, compacted_delta)
            after_detail = _layout_from_active_files(spark, optimized_delta)
            arms = {
                "baseline_to_compaction": _compare_query_sets(query_results["baseline"], query_results["compacted"],
                                                              before_detail=before_detail, after_detail=compacted_detail,
                                                              arm="baseline_to_compaction"),
                "compaction_to_zorder": _compare_query_sets(query_results["compacted"], query_results["optimized"],
                                                            before_detail=compacted_detail, after_detail=after_detail,
                                                            arm="compaction_to_zorder"),
                "baseline_to_optimized": _compare_query_sets(query_results["baseline"], query_results["optimized"],
                                                             before_detail=before_detail, after_detail=after_detail,
                                                             arm="baseline_to_optimized"),
            }
            verdicts = {arm: workload_verdict([item["verdict"] for item in items]) for arm, items in arms.items()}
            evidence["tables"].append({
                "table": table_name,
                "stage": table["stage"],
                "source_uri": source,
                "source_delta_version": source_version,
                "baseline_uri": baseline,
                "compacted_uri": compacted,
                "optimized_uri": optimized,
                "workload_parameters": table.get("workload_parameters"),
                "layout_before": before_detail,
                "layout_compacted": compacted_detail,
                "layout_after": after_detail,
                "queries_before": query_results["baseline"],
                "queries_compacted": query_results["compacted"],
                "queries_after": query_results["optimized"],
                "comparisons": arms,
                "verdicts": verdicts,
                "partition_pruning": pruning,
                "recommendation": recommend_layout(
                    overall_verdict=verdicts["baseline_to_optimized"],
                    compaction_verdict=verdicts["baseline_to_compaction"],
                    zorder_incremental_verdict=verdicts["compaction_to_zorder"],
                    compaction_no_op=bool(rewrite["compaction"]["outcome"]["no_op"]),
                    zorder_no_op=bool(rewrite["zorder"]["outcome"]["no_op"]),
                ),
                "equivalence": equivalence,
                "rewrite": rewrite,
            })
        evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
        evidence["status"] = "PASS"
        return evidence
    finally:
        spark.stop()


Writer = Callable[[str, str, str], None]


def gcs_writer(root: str) -> Writer:
    """Create-only upload below ``root``; an existing object makes the upload fail."""

    from google.cloud import storage

    parsed = urlsplit(root)
    bucket = storage.Client().bucket(parsed.netloc)
    prefix = parsed.path.strip("/")

    def write(name: str, text: str, content_type: str) -> None:
        bucket.blob(f"{prefix}/{name}").upload_from_string(text, content_type=content_type, if_generation_match=0)

    return write


def local_writer(directory: Path) -> Writer:
    """Create-only writer into a local directory (local verification runs)."""

    def write(name: str, text: str, content_type: str) -> None:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)

    return write


CSV_COLUMNS = (
    "table", "comparison_arm", "query", "file_count_change", "median_file_size_change",
    "before_median_seconds", "after_median_seconds", "runtime_change_percent",
    "before_range_seconds", "after_range_seconds", "verdict", "output_equivalent",
    "before_files_read", "after_files_read", "before_bytes_read", "after_bytes_read",
)


def _scan_total(metrics: Mapping[str, Any], key: str) -> int | None:
    if not metrics.get("available"):
        return None
    values = [scan.get(key) for scan in metrics.get("scans", [])]
    return None if any(value is None for value in values) else sum(int(value) for value in values)


def write_evidence(evidence: Mapping[str, Any], writer: Writer) -> list[str]:
    """Write the evidence documents under ``evidence/``; return the names written."""

    def dump(name: str, value: object) -> None:
        writer(f"evidence/{name}", json.dumps(value, indent=2, default=str) + "\n", "application/json")
        written.append(name)

    written: list[str] = []
    tables = list(evidence["tables"])
    for arm, key in (("baseline", "layout_before"), ("compacted", "layout_compacted"), ("optimized", "layout_after")):
        dump(f"layout_{arm}.json", {"tables": [{"table": t["table"], **t[key]} for t in tables]})
    for arm, key in (("baseline", "queries_before"), ("compacted", "queries_compacted"), ("optimized", "queries_after")):
        dump(f"queries_{arm}.json", {"tables": [{"table": t["table"], "queries": t[key]} for t in tables]})
    dump("rewrite.json", {"tables": [{"table": t["table"], **t["rewrite"]} for t in tables]})
    dump("partition_pruning.json", {"tables": [{"table": t["table"], **t["partition_pruning"]} for t in tables]})
    dump("equivalence.json", {"tables": [{"table": t["table"], **t["equivalence"]} for t in tables]})
    dump("comparison.json", evidence)
    output = io.StringIO()
    csv_writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    csv_writer.writeheader()
    for table in tables:
        for arm, items in table["comparisons"].items():
            for item in items:
                csv_writer.writerow({
                    "table": table["table"], "comparison_arm": arm, "query": item["query"],
                    **{key: item[key] for key in ("file_count_change", "median_file_size_change",
                                                  "before_median_seconds", "after_median_seconds",
                                                  "runtime_change_percent", "verdict", "output_equivalent")},
                    "before_range_seconds": json.dumps(item["before_range_seconds"]),
                    "after_range_seconds": json.dumps(item["after_range_seconds"]),
                    "before_files_read": _scan_total(item["before_scan_metrics"], "files_read"),
                    "after_files_read": _scan_total(item["after_scan_metrics"], "files_read"),
                    "before_bytes_read": _scan_total(item["before_scan_metrics"], "bytes_read"),
                    "after_bytes_read": _scan_total(item["after_scan_metrics"], "bytes_read"),
                })
    writer("evidence/comparison.csv", output.getvalue(), "text/csv")
    written.append("comparison.csv")
    return written


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
    root = args.benchmark_root.rstrip("/")
    evidence = execute_plan(plan=plan, benchmark_root=root, dataset_version=args.dataset_version,
                            git_sha=args.git_sha)
    write_evidence(evidence, gcs_writer(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
