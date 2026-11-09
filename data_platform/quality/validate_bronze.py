from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from dataclasses import replace
from pathlib import Path

from data_platform.config import (
    RuntimeConfig,
    StorageConfig,
    load_runtime_config,
    load_runtime_config_file,
)
from data_platform.processing.spark.common.spark_session import create_spark_session
from data_platform.quality.reporting import (
    add_cloud_arguments,
    build_report,
    cloud_environ,
    report_context,
    write_report,
)
from data_platform.storage.scripts.config import OFFLINE_TABLES, offline_tables


BRONZE_TABLES = OFFLINE_TABLES

FRAUD_TYPES = (
    "velocity",
    "amount_anomaly",
    "account_takeover",
    "merchant_burst",
)

EXPECTED_TYPES = {
    "users": {
        "user_id": "string",
        "full_name": "string",
        "email": "string",
        "phone": "string",
        "kyc_verified": "boolean",
        "created_at": "timestamp",
    },
    "accounts": {
        "account_id": "string",
        "user_id": "string",
        "account_type": "string",
        "currency": "string",
        "created_at": "timestamp",
    },
    "merchants": {
        "merchant_id": "string",
        "merchant_name": "string",
        "category": "string",
    },
    "devices": {
        "device_id": "string",
        "user_id": "string",
        "device_type": "string",
        "os": "string",
        "first_seen_at": "timestamp",
    },
    "transactions": {
        "transaction_id": "string",
        "account_id": "string",
        "user_id": "string",
        "device_id": "string",
        "type": "string",
        "amount": "numeric",
        "currency": "string",
        "status": "string",
        "old_balance": "numeric",
        "new_balance": "numeric",
        "merchant_id": "string",
        "counterparty_account_id": "string",
        "timestamp": "timestamp",
        "ingested_at": "timestamp",
    },
    "balance_snapshots": {
        "account_id": "string",
        "snapshot_date": "date",
        "closing_balance": "numeric",
    },
    "login_events": {
        "login_id": "string",
        "user_id": "string",
        "device_id": "string",
        "login_ts": "timestamp",
        "is_success": "boolean",
    },
    "fraud_labels": {
        "transaction_id": "string",
        "label": "numeric",
        "fraud_type": "string",
    },
    "merchant_history": {
        "snapshot_id": "string",
        "merchant_id": "string",
        "merchant_name": "string",
        "category": "string",
        "snapshot_ts": "timestamp",
        "history_source": "string",
    },
}

COMPATIBLE_SPARK_TYPES = {
    "string": {
        "string",
        "char",
        "varchar",
    },
    "numeric": {
        "byte",
        "short",
        "integer",
        "long",
        "float",
        "double",
        "decimal",
    },
    "boolean": {
        "boolean",
    },
    "date": {
        "date",
    },
    "timestamp": {
        "timestamp",
        "timestamp_ntz",
    },
}


@dataclass
class ValidationResult:
    rule_name: str
    passed: bool
    actual_value: int | str
    expected: str


def read_bronze(
    spark,
    table_name: str,
    *,
    storage: StorageConfig,
) -> DataFrame:
    path = storage.table_uri("bronze", table_name)

    return (
        spark.read
        .format("delta")
        .load(path)
    )


def validate_required_columns(
    df: DataFrame,
    table_name: str,
) -> ValidationResult:
    expected_columns = set(
        EXPECTED_TYPES[table_name]
    )

    if table_name == "transactions":
        expected_columns.add("channel")

    missing_columns = sorted(
        expected_columns.difference(df.columns)
    )

    return ValidationResult(
        rule_name=f"{table_name}.required_columns",
        passed=not missing_columns,
        actual_value=(
            "all required columns present"
            if not missing_columns
            else f"missing={','.join(missing_columns)}"
        ),
        expected="all required columns present",
    )


def is_compatible_type(
    actual_type: str,
    expected_type: str,
) -> bool:
    return (
        actual_type
        in COMPATIBLE_SPARK_TYPES[expected_type]
    )


def validate_compatible_types(
    df: DataFrame,
    table_name: str,
) -> ValidationResult:
    fields_by_name = {
        field.name: field
        for field in df.schema.fields
    }

    mismatches = []

    for column_name, expected_type in EXPECTED_TYPES[table_name].items():
        field = fields_by_name.get(column_name)

        if field is None:
            mismatches.append(
                f"{column_name}:missing"
            )
            continue

        actual_type = field.dataType.typeName()

        if not is_compatible_type(
            actual_type,
            expected_type,
        ):
            mismatches.append(
                f"{column_name}:{field.dataType.simpleString()}"
            )

    return ValidationResult(
        rule_name=f"{table_name}.compatible_types",
        passed=not mismatches,
        actual_value=(
            "all configured types compatible"
            if not mismatches
            else f"incompatible={','.join(mismatches)}"
        ),
        expected="all configured columns have compatible data types",
    )


def validate_transaction_channel(
    df: DataFrame,
) -> ValidationResult:
    field = next(
        (
            item
            for item in df.schema.fields
            if item.name == "channel"
        ),
        None,
    )

    if field is None:
        actual_value = "missing"
        passed = False
    else:
        actual_type = field.dataType.typeName()
        actual_value = field.dataType.simpleString()
        passed = is_compatible_type(
            actual_type,
            "string",
        )

    return ValidationResult(
        rule_name="transactions.channel_schema",
        passed=passed,
        actual_value=actual_value,
        expected="channel exists with a string-compatible type",
    )


def scan_persisted_data(
    df: DataFrame,
) -> int:
    row_hash = F.xxhash64(
        *[
            F.col(column_name)
            for column_name in df.columns
        ]
    )

    scan_result = (
        df
        .select(
            row_hash.alias("_bronze_row_hash")
        )
        .agg(
            F.count(F.lit(1)).alias("row_count"),
            F.min("_bronze_row_hash").alias("min_row_hash"),
            F.max("_bronze_row_hash").alias("max_row_hash"),
        )
        .first()
    )

    return int(
        scan_result["row_count"]
    )


def validate_non_empty(
    table_name: str,
    row_count: int,
) -> ValidationResult:
    return ValidationResult(
        rule_name=f"{table_name}.non_empty",
        passed=row_count > 0,
        actual_value=row_count,
        expected="row_count > 0",
    )


def validate_fraud_labels(df: DataFrame) -> list[ValidationResult]:
    """Validate raw fraud ground truth without repairing source values."""
    required_nulls = df.filter(
        F.col("transaction_id").isNull()
        | F.col("label").isNull()
    ).count()
    invalid_labels = df.filter(
        F.col("label").isNotNull()
        & ~F.col("label").isin(0, 1)
    ).count()
    invalid_conditional_types = df.filter(
        ((F.col("label") == 0) & F.col("fraud_type").isNotNull())
        | ((F.col("label") == 1) & F.col("fraud_type").isNull())
    ).count()
    invalid_fraud_types = df.filter(
        (F.col("label") == 1)
        & F.col("fraud_type").isNotNull()
        & ~F.col("fraud_type").isin(*FRAUD_TYPES)
    ).count()
    duplicate_groups = (
        df.groupBy("transaction_id")
        .count()
        .filter(F.col("count") > 1)
        .count()
    )

    return [
        ValidationResult(
            "fraud_labels.required_values_not_null",
            required_nulls == 0,
            required_nulls,
            "0 rows with NULL transaction_id or label",
        ),
        ValidationResult(
            "fraud_labels.valid_label_domain",
            invalid_labels == 0,
            invalid_labels,
            "0 labels outside {0,1}",
        ),
        ValidationResult(
            "fraud_labels.conditional_fraud_type",
            invalid_conditional_types == 0,
            invalid_conditional_types,
            "label=0 has NULL fraud_type and label=1 has non-NULL fraud_type",
        ),
        ValidationResult(
            "fraud_labels.valid_fraud_type_domain",
            invalid_fraud_types == 0,
            invalid_fraud_types,
            "0 fraud types outside the controlled domain",
        ),
        ValidationResult(
            "fraud_labels.unique_transaction_id",
            duplicate_groups == 0,
            duplicate_groups,
            "0 duplicate transaction_id groups",
        ),
    ]


def validate_fraud_label_coverage(
    fraud_labels_df: DataFrame,
    transactions_df: DataFrame,
) -> list[ValidationResult]:
    """Require exact equality with distinct Bronze transaction IDs."""
    label_ids = fraud_labels_df.select("transaction_id").distinct()
    transaction_ids = transactions_df.select("transaction_id").distinct()
    orphan_labels = label_ids.join(transaction_ids, "transaction_id", "left_anti").count()
    missing_labels = transaction_ids.join(label_ids, "transaction_id", "left_anti").count()
    label_rows = fraud_labels_df.count()
    transaction_count = transaction_ids.count()

    return [
        ValidationResult(
            "fraud_labels.transaction_fk",
            orphan_labels == 0,
            orphan_labels,
            "0 label IDs absent from distinct Bronze transactions",
        ),
        ValidationResult(
            "fraud_labels.complete_transaction_coverage",
            missing_labels == 0,
            missing_labels,
            "0 distinct Bronze transaction IDs without labels",
        ),
        ValidationResult(
            "fraud_labels.population_matches_distinct_transactions",
            label_rows == transaction_count,
            f"labels={label_rows},distinct_transactions={transaction_count}",
            "fraud label rows equal distinct Bronze transaction IDs",
        ),
    ]


def print_result(
    result: ValidationResult,
) -> None:
    if result.passed:
        print(
            f"PASS | {result.rule_name}"
        )
        return

    print(
        f"FAIL | {result.rule_name} | "
        f"actual={result.actual_value} "
        f"expected={result.expected}"
    )


def print_summary(
    passed_count: int,
    failed_count: int,
) -> None:
    print(
        "=" * 60
    )
    print(
        f"Validation summary: {passed_count} PASS | "
        f"{failed_count} FAIL"
    )
    print("")

    if failed_count == 0:
        print(
            "Bronze Data Quality validation PASSED."
        )
    else:
        print(
            "Bronze Data Quality validation FAILED."
        )


def run_validation(
    include_fraud_labels: bool = False,
    runtime_config: RuntimeConfig | None = None,
    include_merchant_history: bool = False,
    report_uri: str | None = None,
    report_details: dict | None = None,
) -> bool:
    spark = None
    passed_count = 0
    failed_count = 0
    tables: dict[str, DataFrame] = {}
    config = runtime_config or load_runtime_config()
    collected: list[ValidationResult] = []

    def emit(result: ValidationResult) -> None:
        print_result(result)
        collected.append(result)

    try:
        spark = create_spark_session(
            app_name="EWallet-Bronze-Validation",
            stage="dp1-bronze",
            optimized=True,
            runtime_config=config,
        )

        for table_name in offline_tables(include_fraud_labels, include_merchant_history):
            print(
                f"========== VALIDATE {table_name} =========="
            )

            try:
                df = read_bronze(
                    spark,
                    table_name,
                    storage=config.storage,
                )

                emit(
                    ValidationResult(
                        rule_name=f"{table_name}.delta_readable",
                        passed=True,
                        actual_value="readable",
                        expected="persisted Delta table is readable",
                    )
                )
                passed_count += 1
                tables[table_name] = df

            except Exception as exc:
                emit(ValidationResult(f"{table_name}.delta_readable", False, str(exc), "persisted Delta table is readable"))
                failed_count += 1
                continue

            results = [
                validate_required_columns(
                    df,
                    table_name,
                ),
                validate_compatible_types(
                    df,
                    table_name,
                ),
            ]

            if table_name == "transactions":
                results.append(
                    validate_transaction_channel(
                        df
                    )
                )

            if table_name == "fraud_labels":
                results.extend(validate_fraud_labels(df))

            for result in results:
                emit(result)

                if result.passed:
                    passed_count += 1
                else:
                    failed_count += 1

            try:
                row_count = scan_persisted_data(
                    df
                )

                emit(
                    ValidationResult(
                        rule_name=(
                            f"{table_name}.persisted_data_scan"
                        ),
                        passed=True,
                        actual_value="scan completed",
                        expected="persisted Delta data can be scanned",
                    )
                )
                passed_count += 1

                non_empty_result = validate_non_empty(
                    table_name,
                    row_count,
                )
                emit(non_empty_result)

                if non_empty_result.passed:
                    passed_count += 1
                else:
                    failed_count += 1

            except Exception as exc:
                emit(ValidationResult(f"{table_name}.persisted_data_scan", False, str(exc), "persisted Delta data can be scanned"))
                failed_count += 1

        if include_fraud_labels and {
            "transactions",
            "fraud_labels",
        }.issubset(tables):
            print("========== VALIDATE fraud_labels relationships ==========")
            for result in validate_fraud_label_coverage(
                tables["fraud_labels"],
                tables["transactions"],
            ):
                emit(result)
                if result.passed:
                    passed_count += 1
                else:
                    failed_count += 1

    except Exception as exc:
        emit(ValidationResult("bronze.validation_started", False, str(exc), "Bronze validation runs"))
        failed_count += 1

    finally:
        if spark is not None:
            if report_uri is not None:
                report = build_report("bronze", collected, context=report_details)
                write_report(spark, report_uri, report)
                print(f"Report written: {report_uri}")
            try:
                spark.stop()
            except Exception as exc:
                print(
                    f"ERROR | Unable to stop SparkSession: {exc}"
                )
                failed_count += 1

    print_summary(
        passed_count,
        failed_count,
    )

    return failed_count == 0


def apply_explicit_root(config: RuntimeConfig, bronze_root: str | None) -> RuntimeConfig:
    """Bind validation to one run root; required on gcp so no default root is read."""

    if config.environment != "gcp" and bronze_root is None:
        return config
    if (
        not bronze_root
        or not bronze_root.startswith("gs://")
        or "/lakehouse/runs/" not in bronze_root
        or not bronze_root.rstrip("/").endswith("/bronze")
    ):
        raise ValueError("--bronze-root must be a run-scoped gs://.../lakehouse/runs/<run>/bronze root")
    return replace(config, storage=replace(config.storage, bronze_root=bronze_root.rstrip("/")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate persisted Bronze Delta data.")
    parser.add_argument(
        "--environment",
        choices=["local", "dev", "gcp"],
        default="local",
        help="Runtime storage and Spark profile (default: local).",
    )
    parser.add_argument(
        "--include-fraud-labels",
        action="store_true",
        help="Require and validate Bronze fraud_labels in transitional fraud mode.",
    )
    parser.add_argument(
        "--include-merchant-history",
        action="store_true",
        help="Require and validate Bronze merchant_history.",
    )
    parser.add_argument("--bronze-root", help="Run-scoped Bronze root (required on gcp).")
    parser.add_argument("--runtime-config-file", type=Path)
    add_cloud_arguments(parser)
    args = parser.parse_args(argv)
    environ = cloud_environ(args)
    if args.runtime_config_file is not None:
        config = load_runtime_config_file(args.runtime_config_file, args.environment, environ=environ)
    else:
        config = load_runtime_config(args.environment, environ=environ)
    config = apply_explicit_root(config, args.bronze_root)
    print(f"Bronze root: {config.storage.bronze_root}")
    return (
        0
        if run_validation(
            include_fraud_labels=args.include_fraud_labels,
            runtime_config=config,
            include_merchant_history=args.include_merchant_history,
            report_uri=args.report_uri,
            report_details=report_context(args, bronze_root=config.storage.bronze_root),
        )
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
