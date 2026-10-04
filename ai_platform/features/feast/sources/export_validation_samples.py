"""Build deterministic canonical entity samples and direct-Gold expectations."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pandas as pd

from pyspark.sql import functions as F

from ai_platform.features.feast import RUNTIME_DATA_ROOT
from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.processing.spark.common.spark_session import create_spark_session


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-root",
        type=Path,
        default=RUNTIME_DATA_ROOT / "validation",
    )
    parser.add_argument("--benchmark-rows", type=int, default=100)
    return parser.parse_args()


def read_delta(spark, layer: str, table: str):
    return spark.read.format("delta").load(f"s3a://{layer}-zone/{table}")


def export_samples(output_root: Path, benchmark_rows: int) -> None:
    spark = create_spark_session("F6-Canonical-Entity-Sample")
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        transactions = read_delta(spark, "silver", "transactions").select(
            "transaction_id",
            "user_id",
            "account_id",
            "device_id",
            "merchant_id",
            "type",
            "timestamp",
            F.expr("unix_micros(timestamp)").alias("_event_timestamp_us"),
        )
        labels = read_delta(spark, "silver", "fraud_labels").select(
            "transaction_id", "label", "fraud_type"
        )
        def first_transaction_id(frame):
            row = frame.orderBy("transaction_id").select("transaction_id").first()
            if row is None:
                raise ValueError("A required canonical sample population is empty")
            return row.transaction_id

        fraud_types = (
            "velocity",
            "amount_anomaly",
            "account_takeover",
            "merchant_burst",
        )
        label_minima = labels.agg(
            F.min(F.when(F.col("label") == 0, F.col("transaction_id"))).alias(
                "normal"
            ),
            *[
                F.min(
                    F.when(
                        F.col("fraud_type") == fraud_type,
                        F.col("transaction_id"),
                    )
                ).alias(fraud_type)
                for fraud_type in fraud_types
            ],
        ).first()
        selections = [("normal", label_minima.normal)] + [
            (fraud_type, label_minima[fraud_type]) for fraud_type in fraud_types
        ]
        if any(transaction_id is None for _, transaction_id in selections):
            raise ValueError("A required canonical fraud population is empty")

        cold_snapshot = (
            read_delta(spark, "gold", "feat_user_behavior")
            .filter(F.col("user_tx_count_24h") == 0)
            .orderBy("user_id", "event_timestamp")
            .select("user_id", "event_timestamp")
            .first()
        )
        selections.append(
            (
                "cold_start",
                first_transaction_id(
                    transactions.filter(
                        (F.col("user_id") == cold_snapshot.user_id)
                        & (F.col("timestamp") == cold_snapshot.event_timestamp)
                    )
                ),
            )
        )

        multi_user = (
            read_delta(spark, "silver", "accounts")
            .groupBy("user_id")
            .agg(F.countDistinct("account_id").alias("account_count"))
            .filter(F.col("account_count") > 1)
            .orderBy("user_id")
            .select("user_id")
            .first()
        )
        selections.append(
            (
                "multi_account_user",
                first_transaction_id(
                    transactions.filter(F.col("user_id") == multi_user.user_id)
                ),
            )
        )

        recent_snapshot = (
            read_delta(spark, "gold", "feat_device_behavior")
            .filter(F.col("device_age_seconds") <= 7 * 24 * 60 * 60)
            .orderBy("device_id", "event_timestamp")
            .select("device_id", "event_timestamp")
            .first()
        )
        selections.append(
            (
                "recent_device",
                first_transaction_id(
                    transactions.filter(
                        (F.col("device_id") == recent_snapshot.device_id)
                        & (F.col("timestamp") == recent_snapshot.event_timestamp)
                    )
                ),
            )
        )
        selections.extend(
            (
                (
                    "payment_with_merchant",
                    first_transaction_id(
                        transactions.filter(
                            (F.col("type") == "payment")
                            & F.col("merchant_id").isNotNull()
                        )
                    ),
                ),
                (
                    "non_payment_without_merchant",
                    first_transaction_id(
                        transactions.filter(
                            (F.col("type") != "payment")
                            & F.col("merchant_id").isNull()
                        )
                    ),
                ),
            )
        )

        selected_ids = [transaction_id for _, transaction_id in selections]
        transaction_rows = {
            row.transaction_id: row.asDict(recursive=True)
            for row in transactions.filter(
                F.col("transaction_id").isin(selected_ids)
            ).collect()
        }
        label_rows = {
            row.transaction_id: row.asDict(recursive=True)
            for row in labels.filter(F.col("transaction_id").isin(selected_ids)).collect()
        }
        sample_rows = []
        for sample_case, transaction_id in selections:
            if transaction_id not in transaction_rows or transaction_id not in label_rows:
                raise ValueError(f"Missing canonical transaction: {transaction_id}")
            transaction = transaction_rows[transaction_id]
            label = label_rows[transaction_id]
            sample_rows.append(
                {
                    "sample_case": sample_case,
                    **transaction,
                    "event_timestamp": transaction["timestamp"],
                    "label": label["label"],
                    "fraud_type": label["fraud_type"],
                }
            )
            sample_rows[-1].pop("timestamp")

        # From this point forward only the ten selected rows are held locally. Each
        # Gold read is restricted to their exact entity/timestamp keys, so no Gold
        # table is collected to the driver.
        for view_name, spec in FEATURE_SPECS.items():
            entity_key = spec["entity_key"]
            feature = read_delta(spark, "gold", spec["table"])
            keys = {
                (row[entity_key], row["event_timestamp"])
                for row in sample_rows
                if row[entity_key] is not None
            }
            predicate = None
            for entity_value, event_timestamp in keys:
                key_predicate = (
                    (F.col(entity_key) == F.lit(entity_value))
                    & (F.col("event_timestamp") == F.lit(event_timestamp))
                )
                predicate = (
                    key_predicate
                    if predicate is None
                    else predicate | key_predicate
                )
            selected_rows = (
                [] if predicate is None else feature.filter(predicate).collect()
            )
            values_by_key = {
                (row[entity_key], row.event_timestamp): row.asDict(recursive=True)
                for row in selected_rows
            }
            for sample_row in sample_rows:
                values = values_by_key.get(
                    (sample_row[entity_key], sample_row["event_timestamp"]), {}
                )
                for name in spec["features"]:
                    sample_row[f"expected__{view_name}__{name}"] = values.get(name)

        expected_cases = {
            "normal",
            "velocity",
            "amount_anomaly",
            "account_takeover",
            "merchant_burst",
            "cold_start",
            "multi_account_user",
            "recent_device",
            "payment_with_merchant",
            "non_payment_without_merchant",
        }
        actual_cases = {row["sample_case"] for row in sample_rows}
        if actual_cases != expected_cases:
            raise ValueError(
                f"Canonical sample coverage mismatch: {sorted(actual_cases)}"
            )
        sample_frame = pd.DataFrame(sample_rows).sort_values("sample_case")
        sample_frame["event_timestamp"] = pd.to_datetime(
            sample_frame.pop("_event_timestamp_us"), unit="us", utc=True
        )
        sample_path = output_root / "canonical_sample"
        shutil.rmtree(sample_path, ignore_errors=True)
        sample_path.mkdir(parents=True)
        sample_frame.to_parquet(sample_path / "part-00000.parquet", index=False)

        benchmark_rows_local = (
            transactions.orderBy("transaction_id")
            .limit(benchmark_rows)
            .select(
                "transaction_id",
                "user_id",
                "account_id",
                "device_id",
                "merchant_id",
                F.expr("unix_micros(timestamp)").alias("_event_timestamp_us"),
            )
            .collect()
        )
        if len(benchmark_rows_local) != benchmark_rows:
            raise ValueError("Benchmark entity population is incomplete")
        benchmark_frame = pd.DataFrame(
            [row.asDict(recursive=True) for row in benchmark_rows_local]
        )
        benchmark_frame["event_timestamp"] = pd.to_datetime(
            benchmark_frame.pop("_event_timestamp_us"), unit="us", utc=True
        )
        benchmark_path = output_root / "benchmark_entities"
        shutil.rmtree(benchmark_path, ignore_errors=True)
        benchmark_path.mkdir(parents=True)
        benchmark_frame.to_parquet(
            benchmark_path / "part-00000.parquet", index=False
        )
        print(f"PASS | canonical sample | cases={len(actual_cases)}")
        print(f"PASS | benchmark entities | rows={benchmark_rows}")
    finally:
        spark.stop()


def main() -> int:
    args = parse_args()
    try:
        export_samples(args.output_root, args.benchmark_rows)
    except Exception as exc:
        print(f"ERROR | {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
