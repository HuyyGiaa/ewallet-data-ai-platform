"""Produce deterministic read-only summaries for full-scale Spark A/B outputs.

This command scans both profiles with the same code.  The content fingerprint is
an order-independent aggregate of Spark xxhash64 values; it is a strong table-
level equivalence signal, not a byte-for-byte or cryptographic row proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from data_platform.config.spark_profiles import (
    require_profile_binding,
    validate_git_sha,
)
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


def _table_summary(dataframe: Any) -> dict[str, object]:
    from pyspark.sql import functions as F

    columns = sorted(dataframe.columns)
    row_hash = F.xxhash64(
        *[
            F.coalesce(F.col(name).cast("string"), F.lit("<NULL>"))
            for name in columns
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
    total = str(aggregate["total"] or 0)
    minimum = int(aggregate["minimum"] or 0)
    maximum = int(aggregate["maximum"] or 0)
    return {
        "schema_sha256": hashlib.sha256(
            dataframe.schema.json().encode("utf-8")
        ).hexdigest(),
        "row_count": rows,
        "content_fingerprint": fingerprint_payload(
            rows=rows, total=total, minimum=minimum, maximum=maximum
        ),
        "fingerprint_components": {
            "xxhash64_decimal_sum": total,
            "xxhash64_min": minimum,
            "xxhash64_max": maximum,
        },
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
    expected_logical_transactions: int,
    expected_physical_transactions: int,
) -> dict[str, object]:
    from pyspark.sql import SparkSession

    validate_git_sha(git_sha)
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
    spark = SparkSession.builder.appName("spark-ab-output-summary").getOrCreate()
    try:
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
        return {
            "benchmark_id": benchmark_id,
            "dataset_version": dataset_version,
            "git_sha": git_sha,
            "spark_profile": spark_profile,
            "stage": stage,
            "output_run_id": output_run_id,
            "root": root,
            "tables": {
                table: _table_summary(frame) for table, frame in frames.items()
            },
            "invariants": _invariants(
                stage,
                frames,
                expected_logical_transactions=expected_logical_transactions,
                expected_physical_transactions=expected_physical_transactions,
            ),
        }
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
    output.write_text(payload, encoding="utf-8")


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
    parser.add_argument("--expected-logical-transactions", type=int, required=True)
    parser.add_argument("--expected-physical-transactions", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    summary = build_summary(**{key: value for key, value in vars(args).items() if key != "output"})
    write_summary(args.output, summary)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
