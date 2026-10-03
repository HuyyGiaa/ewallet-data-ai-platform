from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from data_platform.processing.spark.common.spark_session import create_spark_session
from data_platform.storage.scripts.config import OFFLINE_TABLES, offline_tables


BRONZE_ROOT = "s3a://bronze-zone"

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
) -> DataFrame:
    path = f"{BRONZE_ROOT}/{table_name}"

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


def run_validation(include_fraud_labels: bool = False) -> bool:
    spark = None
    passed_count = 0
    failed_count = 0
    tables: dict[str, DataFrame] = {}

    try:
        spark = create_spark_session(
            app_name="EWallet-Bronze-Validation",
            optimized=True,
        )

        for table_name in offline_tables(include_fraud_labels):
            print(
                f"========== VALIDATE {table_name} =========="
            )

            try:
                df = read_bronze(
                    spark,
                    table_name,
                )

                print_result(
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
                print(
                    f"ERROR | Unable to read Bronze table "
                    f"'{table_name}': {exc}"
                )
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
                print_result(result)

                if result.passed:
                    passed_count += 1
                else:
                    failed_count += 1

            try:
                row_count = scan_persisted_data(
                    df
                )

                print_result(
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
                print_result(non_empty_result)

                if non_empty_result.passed:
                    passed_count += 1
                else:
                    failed_count += 1

            except Exception as exc:
                print(
                    f"ERROR | Unable to scan persisted Bronze "
                    f"data for '{table_name}': {exc}"
                )
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
                print_result(result)
                if result.passed:
                    passed_count += 1
                else:
                    failed_count += 1

    except Exception as exc:
        print(
            f"ERROR | Unable to start Bronze validation: {exc}"
        )
        failed_count += 1

    finally:
        if spark is not None:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate persisted Bronze Delta data.")
    parser.add_argument(
        "--include-fraud-labels",
        action="store_true",
        help="Require and validate Bronze fraud_labels in transitional fraud mode.",
    )
    args = parser.parse_args()
    return (
        0
        if run_validation(include_fraud_labels=args.include_fraud_labels)
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
