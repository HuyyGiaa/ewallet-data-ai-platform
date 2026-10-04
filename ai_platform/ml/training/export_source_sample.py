"""Export a deterministic bounded Silver/label sample for the F7 build."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pandas as pd
from pyspark.sql import Window
from pyspark.sql import functions as F

from ai_platform.ml import ML_DATA_ROOT
from ai_platform.ml.training.temporal import TEST_START, VALIDATION_START
from data_platform.processing.spark.common.spark_session import create_spark_session


DEFAULT_OUTPUT = ML_DATA_ROOT / "fraud_training_v1" / "source_sample"
SOURCE_COLUMNS = (
    "transaction_id",
    "event_timestamp",
    "user_id",
    "account_id",
    "device_id",
    "merchant_id",
    "amount",
    "type",
    "channel",
    "merchant_present",
    "label",
    "fraud_type",
    "split",
    "sampling_group",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--rows-per-group", type=int, default=20)
    return parser.parse_args()


def read_delta(spark, table: str):
    return spark.read.format("delta").load(f"s3a://silver-zone/{table}")


def audit_label_integrity(transactions, labels) -> dict[str, int | str]:
    transaction_metrics = transactions.agg(
        F.count("*").alias("rows"),
        F.countDistinct("transaction_id").alias("distinct_ids"),
    ).first()
    label_metrics = labels.agg(
        F.count("*").alias("rows"),
        F.countDistinct("transaction_id").alias("distinct_ids"),
        F.sum(
            F.when(F.col("label").isNull() | ~F.col("label").isin(0, 1), 1)
            .otherwise(0)
        ).alias("invalid_labels"),
    ).first()
    transaction_keys = transactions.select("transaction_id")
    label_keys = labels.select("transaction_id")
    missing = transaction_keys.join(label_keys, "transaction_id", "left_anti").count()
    orphan = label_keys.join(transaction_keys, "transaction_id", "left_anti").count()
    duplicate_transactions = transaction_metrics.rows - transaction_metrics.distinct_ids
    duplicate_labels = label_metrics.rows - label_metrics.distinct_ids
    if any(
        (
            duplicate_transactions,
            duplicate_labels,
            label_metrics.invalid_labels,
            missing,
            orphan,
        )
    ):
        raise ValueError(
            "Canonical label integrity failed: "
            f"duplicate_transactions={duplicate_transactions} "
            f"duplicate_labels={duplicate_labels} "
            f"invalid_labels={label_metrics.invalid_labels} "
            f"missing={missing} orphan={orphan}"
        )
    return {
        "status": "PASS",
        "transactions": transaction_metrics.rows,
        "labels": label_metrics.rows,
        "matched": transaction_metrics.rows,
        "duplicate_transactions": duplicate_transactions,
        "duplicate_labels": duplicate_labels,
        "invalid_labels": label_metrics.invalid_labels,
        "missing_labels": missing,
        "orphan_labels": orphan,
    }


def export_source_sample(output: Path, rows_per_group: int) -> dict:
    if rows_per_group <= 0:
        raise ValueError("rows_per_group must be positive")
    spark = create_spark_session("F7-Bounded-Training-Source")
    try:
        transactions = read_delta(spark, "transactions").select(
            "transaction_id",
            "user_id",
            "account_id",
            "device_id",
            "merchant_id",
            "amount",
            "type",
            "channel",
            "timestamp",
        )
        labels = read_delta(spark, "fraud_labels").select(
            "transaction_id", "label", "fraud_type"
        )
        label_integrity = audit_label_integrity(transactions, labels)
        joined = (
            transactions.join(labels, "transaction_id", "inner")
            .withColumn(
                "split",
                F.when(
                    F.col("timestamp")
                    < F.to_timestamp(F.lit(VALIDATION_START.isoformat())),
                    "train",
                )
                .when(
                    F.col("timestamp")
                    < F.to_timestamp(F.lit(TEST_START.isoformat())),
                    "validation",
                )
                .otherwise("test"),
            )
            .withColumn(
                "sampling_group",
                F.when(F.col("label") == 0, F.lit("normal")).otherwise(
                    F.col("fraud_type")
                ),
            )
        )
        sampling_window = Window.partitionBy("split", "sampling_group").orderBy(
            "transaction_id"
        )
        sampled = (
            joined.withColumn("_sample_rank", F.row_number().over(sampling_window))
            .filter(F.col("_sample_rank") <= rows_per_group)
            .drop("_sample_rank")
        )

        peer_key = (
            joined.groupBy("user_id", "timestamp")
            .agg(F.countDistinct("transaction_id").alias("peer_count"))
            .filter(F.col("peer_count") > 1)
            .orderBy("timestamp", "user_id")
            .first()
        )
        if peer_key is None:
            raise ValueError("Canonical source has no same-timestamp peer group")
        peers = joined.filter(
            (F.col("user_id") == peer_key.user_id)
            & (F.col("timestamp") == peer_key.timestamp)
        )

        selected_columns = (
            "transaction_id",
            "user_id",
            "account_id",
            "device_id",
            "merchant_id",
            "amount",
            "type",
            "channel",
            "label",
            "fraud_type",
            "split",
            "sampling_group",
        )

        def collect_rows(frame):
            return (
                frame.select(
                    *selected_columns,
                    F.expr("unix_micros(timestamp)").alias(
                        "_event_timestamp_us"
                    ),
                )
                .collect()
            )

        sampled_rows = collect_rows(sampled)
        peer_rows = collect_rows(peers)
        rows = sampled_rows + peer_rows
        peer_transaction_ids = {row.transaction_id for row in peer_rows}
        peer_timestamp = pd.to_datetime(
            peer_rows[0]._event_timestamp_us, unit="us", utc=True
        )
        frame = pd.DataFrame([row.asDict(recursive=True) for row in rows])
        frame = frame.drop_duplicates("transaction_id").sort_values(
            ["split", "sampling_group", "transaction_id"]
        )
        frame["event_timestamp"] = pd.to_datetime(
            frame.pop("_event_timestamp_us"), unit="us", utc=True
        )
        frame["merchant_present"] = frame["merchant_id"].notna()
        frame = frame[list(SOURCE_COLUMNS)].reset_index(drop=True)

        if frame["transaction_id"].duplicated().any():
            raise ValueError("Bounded source contains duplicate transaction IDs")
        if frame["label"].isna().any() or not frame["label"].isin([0, 1]).all():
            raise ValueError("Bounded source contains invalid labels")

        output = output.resolve()
        shutil.rmtree(output, ignore_errors=True)
        output.mkdir(parents=True)
        frame.to_parquet(output / "part-00000.parquet", index=False)
        manifest = {
            "mode": "deterministic_bounded_stratified_sample",
            "rows_per_split_and_class": rows_per_group,
            "rows": len(frame),
            "label_integrity": label_integrity,
            "peer_group": {
                "user_id": peer_key.user_id,
                "event_timestamp": peer_timestamp.isoformat(),
                "source_rows": peer_key.peer_count,
                "retained_rows": int(
                    frame["transaction_id"].isin(peer_transaction_ids).sum()
                ),
            },
            "group_counts": {
                f"{split}:{group}": int(count)
                for (split, group), count in frame.groupby(
                    ["split", "sampling_group"], dropna=False
                ).size().items()
            },
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        print(f"PASS | bounded source rows={len(frame)}")
        print(
            "PASS | same-timestamp peer rows="
            f"{manifest['peer_group']['retained_rows']}"
        )
        return manifest
    finally:
        spark.stop()


def main() -> int:
    args = parse_args()
    try:
        export_source_sample(args.output, args.rows_per_group)
    except Exception as exc:
        print(f"ERROR | {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
