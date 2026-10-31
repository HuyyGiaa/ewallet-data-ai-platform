"""Build deterministic canonical entity samples and direct-Gold expectations."""

from __future__ import annotations

import argparse
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ai_platform.features.feast import RUNTIME_DATA_ROOT
from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.features.feast.validation.contracts import (
    CANONICAL_COMPARABLE_VALUE_COUNT,
    CANONICAL_SAMPLE_CASES,
    HISTORICAL_FEATURE_COUNT,
)
from ai_platform.storage import (
    join_uri,
    normalize_uri,
    parquet_row_count,
    require_absent,
    reset_directory,
    write_json,
    write_pandas_parquet,
)
from data_platform.config import (
    RuntimeConfig,
    load_runtime_config,
    load_runtime_config_file,
)
from data_platform.config.spark_profiles import FEATURES_STAGE, validate_git_sha
from data_platform.processing.spark.common.spark_session import create_spark_session
from data_platform.processing.spark.common.runtime_contract import (
    validate_gcp_spark_runtime,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--environment",
        choices=("local", "dev", "gcp"),
        default="local",
    )
    parser.add_argument(
        "--output-root",
        default=str(RUNTIME_DATA_ROOT / "validation"),
    )
    parser.add_argument("--benchmark-rows", type=int, default=100)
    parser.add_argument("--silver-root")
    parser.add_argument("--feature-gold-root")
    parser.add_argument("--dataset-version")
    parser.add_argument("--source-silver-run-id")
    parser.add_argument("--source-feature-run-id")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--source-git-sha")
    parser.add_argument("--execution-git-sha")
    parser.add_argument("--feature-selection-digest")
    parser.add_argument("--runtime-config-file", type=Path)
    parser.add_argument("--project-id")
    parser.add_argument("--region")
    parser.add_argument("--data-bucket")
    return parser.parse_args(argv)


def apply_validation_roots(
    config: RuntimeConfig,
    *,
    silver_root: str,
    feature_gold_root: str,
) -> RuntimeConfig:
    if config.environment != "gcp" or config.storage.scheme != "gs":
        raise ValueError("run-scoped validation requires the gcp profile")
    if "/lakehouse/runs/" not in silver_root or not silver_root.endswith("/silver"):
        raise ValueError("Silver validation root must be run-scoped")
    if "/lakehouse/runs/" not in feature_gold_root or not feature_gold_root.endswith(
        "/gold"
    ):
        raise ValueError("feature Gold validation root must be run-scoped")
    return replace(
        config,
        storage=replace(
            config.storage,
            silver_root=silver_root,
            gold_root=feature_gold_root,
        ),
    )


def read_delta(
    spark,
    runtime_config: RuntimeConfig,
    layer: str,
    table: str,
):
    return spark.read.format("delta").load(
        runtime_config.storage.table_uri(layer, table)
    )


def export_samples(
    output_root: str | Path,
    benchmark_rows: int,
    runtime_config: RuntimeConfig,
    *,
    dataset_version: str | None = None,
    source_silver_run_id: str | None = None,
    source_feature_run_id: str | None = None,
    snapshot_id: str | None = None,
    source_git_sha: str | None = None,
    execution_git_sha: str | None = None,
    feature_selection_digest: str | None = None,
) -> None:
    output_uri = normalize_uri(output_root)
    spark = create_spark_session(
        "F6-Canonical-Entity-Sample",
        stage=FEATURES_STAGE,
        runtime_config=runtime_config,
    )
    try:
        from pyspark.sql import functions as F

        validate_gcp_spark_runtime(
            spark,
            runtime_config,
            output_uri=output_uri,
        )
        if runtime_config.environment == "gcp":
            require_absent(output_uri)
        else:
            reset_directory(output_uri)
        transactions = read_delta(
            spark,
            runtime_config,
            "silver",
            "transactions",
        ).select(
            "transaction_id",
            "user_id",
            "account_id",
            "device_id",
            "merchant_id",
            "type",
            "timestamp",
            F.expr("unix_micros(timestamp)").alias("_event_timestamp_us"),
        )
        labels = read_delta(
            spark,
            runtime_config,
            "silver",
            "fraud_labels",
        ).select(
            "transaction_id",
            "label",
            "fraud_type",
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
            read_delta(spark, runtime_config, "gold", "feat_user_behavior")
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
            read_delta(spark, runtime_config, "silver", "accounts")
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
            read_delta(spark, runtime_config, "gold", "feat_device_behavior")
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
            feature = read_delta(spark, runtime_config, "gold", spec["table"])
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

        expected_cases = set(CANONICAL_SAMPLE_CASES)
        actual_cases = {row["sample_case"] for row in sample_rows}
        if actual_cases != expected_cases:
            raise ValueError(
                f"Canonical sample coverage mismatch: {sorted(actual_cases)}"
            )
        sample_frame = pd.DataFrame(sample_rows).sort_values("sample_case")
        sample_frame["event_timestamp"] = pd.to_datetime(
            sample_frame.pop("_event_timestamp_us"), unit="us", utc=True
        )
        sample_path = join_uri(output_uri, "canonical_sample")
        write_pandas_parquet(
            sample_frame,
            join_uri(sample_path, "part-00000.parquet"),
        )

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
        benchmark_path = join_uri(output_uri, "benchmark_entities")
        write_pandas_parquet(
            benchmark_frame,
            join_uri(benchmark_path, "part-00000.parquet"),
        )
        written_sample_rows = parquet_row_count(sample_path)
        written_benchmark_rows = parquet_row_count(benchmark_path)
        if written_sample_rows != len(expected_cases):
            raise ValueError("Canonical validation sample output is incomplete")
        if written_benchmark_rows != benchmark_rows:
            raise ValueError("Benchmark validation sample output is incomplete")
        manifest = {
            "schema_version": "feast-validation-samples-v1",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "status": "COMPLETE",
            "canonical_sample_rows": written_sample_rows,
            "canonical_sample_cases": list(CANONICAL_SAMPLE_CASES),
            "historical_feature_count": HISTORICAL_FEATURE_COUNT,
            "expected_comparable_values": CANONICAL_COMPARABLE_VALUE_COUNT,
            "benchmark_rows": written_benchmark_rows,
            "output_root": output_uri,
            "lineage": {
                "dataset_version": dataset_version,
                "source_silver_run_id": source_silver_run_id,
                "source_feature_run_id": source_feature_run_id,
                "source_silver_root": runtime_config.storage.silver_root,
                "source_feature_root": runtime_config.storage.gold_root,
                "snapshot_id": snapshot_id,
                "source_git_sha": source_git_sha,
                "execution_git_sha": execution_git_sha,
                "feature_selection_digest": feature_selection_digest,
            },
        }
        write_json(join_uri(output_uri, "manifest.json"), manifest)
        print(f"PASS | canonical sample | cases={len(actual_cases)}")
        print(f"PASS | benchmark entities | rows={benchmark_rows}")
    finally:
        spark.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        runtime_environ = None
        if args.environment == "gcp":
            if not all((args.project_id, args.region, args.data_bucket)):
                raise ValueError("GCP export requires project, region and data bucket")
            if args.silver_root and not args.silver_root.startswith(
                f"gs://{args.data_bucket}/"
            ):
                raise ValueError("GCP Silver root and data bucket mismatch")
            runtime_environ = {
                **os.environ,
                "GCP_PROJECT_ID": args.project_id,
                "GCP_REGION": args.region,
                "EWALLET_DATA_BUCKET": args.data_bucket,
                "DATAHUB_GMS_URL": (
                    os.environ.get("DATAHUB_GMS_URL")
                    or "http://datahub-gms.invalid:8080"
                ),
            }
        config = (
            load_runtime_config_file(
                args.runtime_config_file, args.environment,
                environ=runtime_environ,
            )
            if args.runtime_config_file is not None
            else load_runtime_config(args.environment, environ=runtime_environ)
        )
        if args.environment == "gcp":
            required = (
                args.silver_root,
                args.feature_gold_root,
                args.dataset_version,
                args.source_silver_run_id,
                args.source_feature_run_id,
                args.snapshot_id,
                args.source_git_sha,
                args.execution_git_sha,
                args.feature_selection_digest,
            )
            if any(value is None for value in required):
                raise ValueError(
                    "gcp validation requires immutable Silver/feature roots, "
                    "dataset/run/snapshot IDs, and --source-git-sha"
                )
            validate_git_sha(args.source_git_sha)
            validate_git_sha(args.execution_git_sha)
            bucket = args.silver_root.removeprefix("gs://").split("/", 1)[0]
            expected_silver = (
                f"gs://{bucket}/lakehouse/runs/{args.source_silver_run_id}/silver"
            )
            expected_features = (
                f"gs://{bucket}/lakehouse/runs/{args.source_feature_run_id}/gold"
            )
            if args.silver_root != expected_silver:
                raise ValueError("gcp validation Silver lineage is inconsistent")
            if args.feature_gold_root != expected_features:
                raise ValueError("gcp validation feature lineage is inconsistent")
            expected_output = (
                f"gs://{bucket}/"
                f"ml/training-staging/{args.dataset_version}/{args.snapshot_id}/validation"
            )
            if normalize_uri(args.output_root) != expected_output:
                raise ValueError("gcp validation output root is not canonical")
            config = apply_validation_roots(
                config,
                silver_root=args.silver_root,
                feature_gold_root=args.feature_gold_root,
            )
        export_samples(
            args.output_root,
            args.benchmark_rows,
            config,
            dataset_version=args.dataset_version,
            source_silver_run_id=args.source_silver_run_id,
            source_feature_run_id=args.source_feature_run_id,
            snapshot_id=args.snapshot_id,
            source_git_sha=args.source_git_sha,
            execution_git_sha=args.execution_git_sha,
            feature_selection_digest=args.feature_selection_digest,
        )
    except Exception as exc:
        print(f"ERROR | {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
