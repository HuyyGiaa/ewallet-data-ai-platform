"""Export a memory-safe Silver source for chunked F7 materialization."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pyspark.sql import functions as F

from ai_platform.ml import ML_DATA_ROOT
from ai_platform.ml.training.export_source_sample import (
    audit_label_integrity,
    read_delta,
)
from ai_platform.ml.training.temporal import TEST_START, VALIDATION_START
from data_platform.processing.spark.common.spark_session import create_spark_session


DEFAULT_OUTPUT = ML_DATA_ROOT / "fraud_training_v1" / "trainable_source"
OUTPUT_COLUMNS = (
    "transaction_id",
    "event_timestamp_us",
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
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--sample-modulus",
        type=int,
        default=1,
        help="1 exports the full canonical population; N>1 keeps hash remainder 0/N.",
    )
    return parser.parse_args()


def request_profile(frame) -> dict:
    overall = frame.agg(
        F.count("*").alias("rows"),
        F.sum(F.col("amount").isNull().cast("long")).alias("amount_null"),
        F.countDistinct("amount").alias("amount_distinct"),
        F.min("amount").alias("amount_min"),
        F.max("amount").alias("amount_max"),
        F.sum(F.col("type").isNull().cast("long")).alias("type_null"),
        F.countDistinct("type").alias("type_distinct"),
        F.sort_array(F.collect_set("type")).alias("type_values"),
        F.sum(F.col("channel").isNull().cast("long")).alias("channel_null"),
        F.countDistinct("channel").alias("channel_distinct"),
        F.sort_array(F.collect_set("channel")).alias("channel_values"),
        F.sum(F.col("merchant_present").isNull().cast("long")).alias(
            "merchant_present_null"
        ),
        F.countDistinct("merchant_present").alias("merchant_present_distinct"),
        F.sort_array(F.collect_set("merchant_present")).alias(
            "merchant_present_values"
        ),
    ).first()

    def grouped(column: str) -> dict[str, int]:
        return {
            f"{row.split}:{getattr(row, column)}": row["count"]
            for row in frame.groupBy("split", column)
            .count()
            .orderBy("split", column)
            .collect()
        }

    return {
        "rows": overall.rows,
        "amount": {
            "null_count": overall.amount_null,
            "distinct_count": overall.amount_distinct,
            "min": overall.amount_min,
            "max": overall.amount_max,
        },
        "type": {
            "null_count": overall.type_null,
            "distinct_count": overall.type_distinct,
            "values": overall.type_values,
            "split_distribution": grouped("type"),
        },
        "channel": {
            "null_count": overall.channel_null,
            "distinct_count": overall.channel_distinct,
            "values": overall.channel_values,
            "split_distribution": grouped("channel"),
        },
        "merchant_present": {
            "null_count": overall.merchant_present_null,
            "distinct_count": overall.merchant_present_distinct,
            "values": overall.merchant_present_values,
            "split_distribution": grouped("merchant_present"),
        },
    }


def split_profile(frame) -> dict[str, dict[str, object]]:
    rows = frame.groupBy("split").agg(
        F.count("*").alias("rows"),
        F.sum(F.col("label")).alias("fraud"),
        F.sum((F.col("label") == 0).cast("long")).alias("normal"),
        F.min("event_timestamp_us").alias("min_timestamp_us"),
        F.max("event_timestamp_us").alias("max_timestamp_us"),
    )
    scenarios = {
        split: {}
        for split in ("train", "validation", "test")
    }
    for row in (
        frame.filter(F.col("label") == 1)
        .groupBy("split", "fraud_type")
        .count()
        .collect()
    ):
        scenarios[row.split][row.fraud_type] = row["count"]
    return {
        row.split: {
            "rows": row.rows,
            "normal": row.normal,
            "fraud": row.fraud,
            "fraud_rate": row.fraud / row.rows,
            "min_timestamp_us": row.min_timestamp_us,
            "max_timestamp_us": row.max_timestamp_us,
            "fraud_types": scenarios[row.split],
        }
        for row in rows.collect()
    }


def export_training_source(output: Path, sample_modulus: int) -> dict:
    if sample_modulus <= 0:
        raise ValueError("sample_modulus must be positive")
    spark = create_spark_session("F7-Training-Source-Export")
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
        validation_us = int(VALIDATION_START.timestamp() * 1_000_000)
        test_us = int(TEST_START.timestamp() * 1_000_000)
        canonical = (
            transactions.join(labels, "transaction_id", "inner")
            .withColumn("event_timestamp_us", F.expr("unix_micros(timestamp)"))
            .withColumn(
                "split",
                F.when(F.col("event_timestamp_us") < validation_us, "train")
                .when(F.col("event_timestamp_us") < test_us, "validation")
                .otherwise("test"),
            )
            .withColumn("merchant_present", F.col("merchant_id").isNotNull())
            .select(*OUTPUT_COLUMNS)
        )
        canonical_request_profile = request_profile(canonical)
        selected = canonical
        if sample_modulus > 1:
            selected = canonical.filter(
                F.pmod(
                    F.xxhash64("event_timestamp_us"), F.lit(sample_modulus)
                )
                == 0
            )
        selected_profile = split_profile(selected)

        output = output.resolve()
        shutil.rmtree(output, ignore_errors=True)
        (
            selected.repartition("split")
            .write.mode("overwrite")
            .partitionBy("split")
            .parquet(str(output))
        )
        written_rows = spark.read.parquet(str(output)).count()
        expected_rows = sum(item["rows"] for item in selected_profile.values())
        if written_rows != expected_rows:
            raise ValueError(
                f"Training source write mismatch: {written_rows} != {expected_rows}"
            )
        manifest = {
            "schema_version": "f7-training-source-v1",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "source_git_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "source": "active Silver Delta transactions + fraud_labels",
            "scope": "full_canonical" if sample_modulus == 1 else "hash_bounded",
            "sample_modulus": sample_modulus,
            "sampling_key": "event_timestamp_us",
            "sampling_reason": (
                "Label-independent deterministic selection that retains all "
                "transactions sharing an event timestamp."
            ),
            "label_integrity": label_integrity,
            "canonical_request_profile": canonical_request_profile,
            "selected_split_profile": selected_profile,
            "timestamp_storage": "signed UTC epoch microseconds",
            "output_columns": list(OUTPUT_COLUMNS),
            "written_rows": written_rows,
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"PASS | training source export | rows={written_rows}")
        return manifest
    finally:
        spark.stop()


def main() -> int:
    args = parse_args()
    try:
        export_training_source(args.output, args.sample_modulus)
    except Exception as exc:
        print(f"ERROR | {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
