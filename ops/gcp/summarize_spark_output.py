"""Produce deterministic read-only summaries for full-scale Spark A/B outputs.

This command scans both profiles with the same code. The content fingerprint is
an order-independent aggregate of Spark xxhash64 and Murmur3 values; together
with schema, row, null and business invariants it is a high-confidence table-
level equivalence signal, not a byte-for-byte or cryptographic row proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from data_platform.config.spark_benchmark import (
    METHOD2_OUTPUT_RUN_PROPERTY,
    METHOD2_PAIR_DIGEST_PROPERTY,
    SEMANTIC_SUMMARY_SCHEMA,
    semantic_summary_digest,
    validate_pair_member,
    validate_runtime_profile_properties,
)
from data_platform.config.spark_profiles import (
    require_profile_binding,
    validate_git_sha,
)
from data_platform.processing.spark.common.write_layout import resolve_write_layout
from ops.gcp.validate_spark_output_equivalence import EXPECTED_TABLES


FEATURE_GRAINS = {
    "feat_user_behavior": ("user_id", "event_timestamp"),
    "feat_account_behavior": ("account_id", "event_timestamp"),
    "feat_device_behavior": ("device_id", "event_timestamp"),
    "feat_merchant_behavior": ("merchant_id", "event_timestamp"),
}
GOLD_PRIMARY_KEYS = {
    "dim_user": ("user_id",),
    "dim_account": ("account_id",),
    "dim_merchant": ("merchant_id",),
    "dim_device": ("device_id",),
    "dim_date": ("date_key",),
    "fact_transactions": ("transaction_id",),
    "fact_login_events": ("login_id",),
    "fact_balance_snapshot": ("account_id", "snapshot_date"),
    "obt_transaction_enriched": ("transaction_id",),
    "feat_user_90d": ("user_id",),
    "opt_merchant_performance": ("merchant_id",),
}
# Write-time audit columns differ between two otherwise identical runs. They are
# excluded from the content fingerprint but remain schema- and null-checked.
VOLATILE_AUDIT_COLUMNS = {
    ("dp2-gold", "feat_user_90d"): ("created_timestamp",),
}


def stage_run_kind(stage: str) -> str:
    return {
        "dp1-bronze": "bronze",
        "dp2-silver": "silver",
        "dp2-gold": "gold",
        "dp3-features": "feat",
    }[stage]


def expected_output_root(*, data_bucket: str, output_run_id: str, stage: str) -> str:
    layer = "gold" if stage in {"dp2-gold", "dp3-features"} else stage.split("-", 1)[1]
    return (
        f"gs://{data_bucket}/lakehouse/runs/{output_run_id}/{layer}"
    )


def fingerprint_payload(*, rows: int, total: str, minimum: int, maximum: int) -> str:
    payload = f"{rows}:{total}:{minimum}:{maximum}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def semantic_fingerprint_payload(
    *, rows: int, xxhash_sum: str, murmur_sum: str, xxhash_min: int, xxhash_max: int
) -> str:
    payload = ":".join(
        (str(rows), xxhash_sum, murmur_sum, str(xxhash_min), str(xxhash_max))
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _inventory(spark: Any, root: str) -> set[str]:
    path = spark._jvm.org.apache.hadoop.fs.Path(root)
    filesystem = path.getFileSystem(spark._jsc.hadoopConfiguration())
    if not filesystem.exists(path):
        raise ValueError(f"output root does not exist: {root}")
    return {
        item.getPath().getName()
        for item in filesystem.listStatus(path)
        if item.isDirectory() and not item.getPath().getName().startswith("_")
    }


def _file_layout(spark: Any, table_path: str) -> dict[str, object]:
    path = spark._jvm.org.apache.hadoop.fs.Path(table_path)
    filesystem = path.getFileSystem(spark._jsc.hadoopConfiguration())
    iterator = filesystem.listFiles(path, True)
    sizes: list[int] = []
    while iterator.hasNext():
        item = iterator.next()
        value = str(item.getPath())
        if "/_delta_log/" not in value and value.endswith(".parquet"):
            sizes.append(int(item.getLen()))
    sizes.sort()
    if not sizes:
        return {
            "file_count": 0,
            "total_bytes": 0,
            "file_size_min": 0,
            "file_size_median": 0,
            "file_size_p95": 0,
            "file_size_max": 0,
        }
    p95_index = min(len(sizes) - 1, math.ceil(len(sizes) * 0.95) - 1)
    median = sizes[(len(sizes) - 1) // 2]
    return {
        "file_count": len(sizes),
        "total_bytes": sum(sizes),
        "file_size_min": sizes[0],
        "file_size_median": median,
        "file_size_p95": sizes[p95_index],
        "file_size_max": sizes[-1],
    }


def _partition_columns(spark: Any, table_path: str) -> list[str]:
    from delta.tables import DeltaTable

    row = DeltaTable.forPath(spark, table_path).detail().select(
        "partitionColumns"
    ).first()
    return list(row["partitionColumns"] or ())


def _required_columns(stage: str, table: str) -> tuple[str, ...]:
    if stage == "dp3-features":
        return FEATURE_GRAINS[table]
    if stage == "dp2-gold":
        return GOLD_PRIMARY_KEYS[table]
    if table == "transactions":
        return ("transaction_id",)
    if table == "fraud_labels":
        return ("transaction_id", "label")
    return ()


def _table_summary(
    dataframe: Any,
    *,
    stage: str,
    table: str,
    table_path: str,
) -> dict[str, object]:
    from pyspark.sql import functions as F

    volatile = VOLATILE_AUDIT_COLUMNS.get((stage, table), ())
    columns = sorted(name for name in dataframe.columns if name not in volatile)
    canonical_row = F.to_json(
        F.struct(*[F.col(name).alias(name) for name in columns]),
        options={
            "ignoreNullFields": "false",
            "timestampFormat": "yyyy-MM-dd'T'HH:mm:ss.SSSSSSXXX",
        },
    )
    row_xxhash = F.xxhash64(canonical_row)
    row_murmur = F.hash(canonical_row)
    required = (*_required_columns(stage, table), *volatile)
    aggregate = (
        dataframe.select(
            row_xxhash.alias("row_xxhash"),
            row_murmur.alias("row_murmur"),
            *[F.col(name).isNull().cast("long").alias(f"null__{name}") for name in required],
        )
        .agg(
            F.count("*").alias("rows"),
            F.sum(F.col("row_xxhash").cast("decimal(38,0)")).alias("xxhash_sum"),
            F.sum(F.col("row_murmur").cast("decimal(38,0)")).alias("murmur_sum"),
            F.min("row_xxhash").alias("xxhash_min"),
            F.max("row_xxhash").alias("xxhash_max"),
            *[F.sum(f"null__{name}").alias(f"null__{name}") for name in required],
        )
        .first()
    )
    rows = int(aggregate["rows"])
    xxhash_sum = str(aggregate["xxhash_sum"] or 0)
    murmur_sum = str(aggregate["murmur_sum"] or 0)
    minimum = int(aggregate["xxhash_min"] or 0)
    maximum = int(aggregate["xxhash_max"] or 0)
    spark = dataframe.sparkSession
    partition_columns = _partition_columns(spark, table_path)
    configured_layout = (
        None
        if stage == "dp1-bronze"
        else resolve_write_layout(stage=stage, table_name=table).evidence(
            partition_columns
        )
    )
    return {
        "schema_sha256": hashlib.sha256(
            dataframe.schema.json().encode("utf-8")
        ).hexdigest(),
        "row_count": rows,
        "semantic_content_fingerprint": semantic_fingerprint_payload(
            rows=rows,
            xxhash_sum=xxhash_sum,
            murmur_sum=murmur_sum,
            xxhash_min=minimum,
            xxhash_max=maximum,
        ),
        "fingerprint_components": {
            "xxhash64_decimal_sum": xxhash_sum,
            "murmur3_32_decimal_sum": murmur_sum,
            "xxhash64_min": minimum,
            "xxhash64_max": maximum,
        },
        "required_null_counts": {
            name: int(aggregate[f"null__{name}"] or 0) for name in required
        },
        "excluded_volatile_columns": list(volatile),
        "partition_columns": partition_columns,
        "configured_write_layout": configured_layout,
        "physical_layout": _file_layout(spark, table_path),
    }


def _invariants(
    stage: str,
    frames: Mapping[str, Any],
    *,
    expected_logical_transactions: int,
    expected_physical_transactions: int,
) -> dict[str, bool]:
    from pyspark.sql import functions as F

    if stage == "dp1-bronze":
        tx = frames["transactions"]
        counts = tx.agg(
            F.count("*").alias("physical"),
            F.countDistinct("transaction_id").alias("logical"),
        ).first()
        return {
            "source_coverage": int(counts["logical"]) == expected_logical_transactions,
            "source_counts_match": (
                int(counts["physical"]) == expected_physical_transactions
            ),
        }
    if stage == "dp2-silver":
        tx = frames["transactions"]
        counts = tx.agg(
            F.count("*").alias("rows"),
            F.countDistinct("transaction_id").alias("distinct_rows"),
        ).first()
        return {
            "logical_transaction_count_match": (
                int(counts["rows"]) == expected_logical_transactions
            ),
            "transaction_id_unique": counts["rows"] == counts["distinct_rows"],
            "fraud_labels_separate": (
                "fraud_labels" in frames
                and "label" not in tx.columns
                and "fraud_type" not in tx.columns
            ),
        }
    if stage == "dp2-gold":
        def unique(table: str, keys: tuple[str, ...]) -> bool:
            frame = frames[table]
            counts = frame.agg(
                F.count("*").alias("rows"),
                F.countDistinct(*keys).alias("distinct_rows"),
            ).first()
            return counts["rows"] == counts["distinct_rows"]

        def no_orphans(
            fact: str, dimension: str, key: str
        ) -> bool:
            left = frames[fact].where(F.col(key).isNotNull()).select(key).distinct()
            right = frames[dimension].select(key).distinct()
            return left.join(right, key, "left_anti").limit(1).count() == 0

        keys_unique = all(
            unique(table, keys) for table, keys in GOLD_PRIMARY_KEYS.items()
        )
        references_valid = all(
            no_orphans("fact_transactions", dimension, key)
            for dimension, key in (
                ("dim_user", "user_id"),
                ("dim_account", "account_id"),
                ("dim_merchant", "merchant_id"),
                ("dim_device", "device_id"),
                ("dim_date", "date_key"),
            )
        )
        row_parity = (
            frames["fact_transactions"].count()
            == frames["obt_transaction_enriched"].count()
        )
        no_leakage = all(
            {"label", "fraud_type", "is_fraud"}.isdisjoint(frame.columns)
            for frame in frames.values()
        )
        return {
            "business_invariants": (
                keys_unique and references_valid and row_parity
            ),
            "no_label_leakage": no_leakage,
        }
    grain_ok = True
    timestamps_ok = True
    no_leakage = True
    for table, frame in frames.items():
        grain = FEATURE_GRAINS[table]
        counts = frame.agg(
            F.count("*").alias("rows"),
            F.countDistinct(*grain).alias("distinct_rows"),
            F.sum(F.col("event_timestamp").isNull().cast("int")).alias("null_times"),
        ).first()
        grain_ok = grain_ok and counts["rows"] == counts["distinct_rows"]
        timestamps_ok = timestamps_ok and int(counts["null_times"] or 0) == 0
        no_leakage = no_leakage and {
            "label", "fraud_type", "is_fraud"
        }.isdisjoint(frame.columns)
    return {
        "grain_invariants": grain_ok,
        "event_timestamp_populated": timestamps_ok,
        "no_label_leakage": no_leakage,
    }


def build_summary(
    *,
    root: str,
    data_bucket: str,
    stage: str,
    benchmark_id: str,
    spark_profile: str,
    output_run_id: str,
    dataset_version: str,
    git_sha: str,
    validation_batch_id: str,
    validation_git_sha: str,
    source_run_ids: Sequence[str],
    expected_logical_transactions: int,
    expected_physical_transactions: int,
    pair_manifest: Mapping[str, object],
) -> dict[str, object]:
    from pyspark.sql import SparkSession

    validate_git_sha(git_sha)
    validate_git_sha(validation_git_sha)
    if not validation_batch_id or validation_batch_id == output_run_id:
        raise ValueError(
            "validation_batch_id must identify a separate read-only execution"
        )
    if not source_run_ids or any(not value for value in source_run_ids):
        raise ValueError("source_run_ids must be explicit")
    require_profile_binding(
        output_run_id,
        benchmark_id=benchmark_id,
        profile=spark_profile,
        stage=stage_run_kind(stage),
    )
    expected_root = expected_output_root(
        data_bucket=data_bucket,
        output_run_id=output_run_id,
        stage=stage,
    )
    if root.rstrip("/") != expected_root:
        raise ValueError(
            f"output root is not bound to output_run_id: expected {expected_root}"
        )
    member = validate_pair_member(
        pair_manifest,
        spark_profile=spark_profile,
        benchmark_id=benchmark_id,
        stage=stage,
        dataset_version=dataset_version,
        git_sha=git_sha,
        upstream_run_ids=source_run_ids,
        output_run_id=output_run_id,
        output_root=root,
    )
    spark = SparkSession.builder.appName("spark-ab-output-summary").getOrCreate()
    try:
        actual_properties = dict(spark.sparkContext.getConf().getAll())
        normalized_profile_properties = validate_runtime_profile_properties(
            pair_manifest,
            spark_profile=spark_profile,
            runtime_properties=actual_properties,
        )
        if actual_properties.get(METHOD2_PAIR_DIGEST_PROPERTY) != pair_manifest[
            "pair_manifest_digest"
        ]:
            raise ValueError("runtime pair manifest digest mismatch")
        if actual_properties.get(METHOD2_OUTPUT_RUN_PROPERTY) != output_run_id:
            raise ValueError("runtime output_run_id binding mismatch")
        expected = EXPECTED_TABLES[stage]
        inventory = _inventory(spark, root)
        if inventory != expected:
            raise ValueError(
                f"{stage} inventory mismatch: expected={sorted(expected)}, "
                f"actual={sorted(inventory)}"
            )
        frames = {
            table: spark.read.format("delta").load(f"{root.rstrip('/')}/{table}")
            for table in sorted(expected)
        }
        tables = {
            table: _table_summary(
                frame,
                stage=stage,
                table=table,
                table_path=f"{root.rstrip('/')}/{table}",
            )
            for table, frame in frames.items()
        }
        invariants = _invariants(
            stage,
            frames,
            expected_logical_transactions=expected_logical_transactions,
            expected_physical_transactions=expected_physical_transactions,
        )
        result: dict[str, object] = {
            "schema_version": SEMANTIC_SUMMARY_SCHEMA,
            "status": "COMPLETE",
            "benchmark_id": benchmark_id,
            "dataset_version": dataset_version,
            "git_sha": git_sha,
            "spark_profile": spark_profile,
            "stage": stage,
            "output_run_id": output_run_id,
            "output_root": root.rstrip("/"),
            "source_run_ids": list(source_run_ids),
            "upstream_run_ids": list(source_run_ids),
            "root": root,
            "pair_manifest_digest": pair_manifest["pair_manifest_digest"],
            "spark_properties": normalized_profile_properties,
            "spark_properties_digest": member["spark_properties_digest"],
            "execution": {
                "kind": "read_only_semantic_scan",
                "validation_batch_id": validation_batch_id,
                "validation_git_sha": validation_git_sha,
                "source_output_run_id": output_run_id,
            },
            "tables": tables,
            "invariants": invariants,
        }
        result["semantic_summary_digest"] = semantic_summary_digest(result)
        return result
    finally:
        spark.stop()


def write_summary(uri: str, summary: Mapping[str, object]) -> None:
    payload = json.dumps(summary, indent=2) + "\n"
    if uri.startswith("gs://"):
        from google.cloud import storage

        parsed = urlsplit(uri)
        storage.Client().bucket(parsed.netloc).blob(
            parsed.path.lstrip("/")
        ).upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=0,
        )
        return
    output = Path(uri)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--data-bucket", required=True)
    parser.add_argument("--stage", choices=tuple(EXPECTED_TABLES), required=True)
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--spark-profile", choices=("baseline", "optimized"), required=True)
    parser.add_argument("--output-run-id", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--validation-batch-id", required=True)
    parser.add_argument("--validation-git-sha", required=True)
    parser.add_argument("--expected-logical-transactions", type=int, required=True)
    parser.add_argument("--expected-physical-transactions", type=int, required=True)
    parser.add_argument("--source-run-id", action="append", dest="source_run_ids", required=True)
    parser.add_argument("--pair-manifest", type=Path, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    values = {key: value for key, value in vars(args).items() if key != "output"}
    pair_path = values.pop("pair_manifest")
    values["pair_manifest"] = json.loads(pair_path.read_text(encoding="utf-8"))
    summary = build_summary(**values)
    write_summary(args.output, summary)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
