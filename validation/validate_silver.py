from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from transformation.spark.spark_session import create_spark_session


SILVER_ROOT = "s3a://silver-zone"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    rule_name: str
    passed: bool
    actual_value: int | float | str
    expected: str


def read_silver(spark, table_name: str,) -> DataFrame:
    path = f"{SILVER_ROOT}/{table_name}"

    logger.info(
        "[%s] Reading Silver table: %s",
        table_name,
        path,
    )

    return (
        spark.read
        .format("delta")
        .load(path)
    )


def validate_non_empty(df: DataFrame, table_name: str,) -> ValidationResult:
    row_count = df.count()

    return ValidationResult(
        rule_name=f"{table_name}.non_empty",
        passed=row_count > 0,
        actual_value=row_count,
        expected="row_count > 0",
    )


def validate_no_null_key(df: DataFrame, table_name: str, key_columns: list[str],) -> ValidationResult:
    condition = None

    for column_name in key_columns:
        current = F.col(column_name).isNull()

        if condition is None:
            condition = current
        else:
            condition = condition | current

    invalid_count = (
        df
        .filter(condition)
        .count()
    )

    return ValidationResult(
        rule_name=f"{table_name}.null_key",
        passed=invalid_count == 0,
        actual_value=invalid_count,
        expected="0 null-key rows",
    )


def validate_unique_key(df: DataFrame, table_name: str, key_columns: list[str],) -> ValidationResult:
    duplicate_groups = (
        df
        .groupBy(*key_columns)
        .count()
        .filter(F.col("count") > 1)
        .count()
    )

    return ValidationResult(
        rule_name=f"{table_name}.duplicate_key",
        passed=duplicate_groups == 0,
        actual_value=duplicate_groups,
        expected="0 duplicate key groups",
    )


def validate_transactions(df: DataFrame,) -> list[ValidationResult]:
    results = []

    # 1. Table must not be empty
    results.append(
        validate_non_empty(
            df,
            "transactions",
        )
    )
    # 2. Required keys must not be NULL
    results.append(
        validate_no_null_key(
            df,
            "transactions",
            [
                "transaction_id",
                "account_id",
                "user_id",
            ],
        )
    )
    
    # 3. transaction_id must be unique after DP2
    results.append(
        validate_unique_key(
            df,
            "transactions",
            ["transaction_id"],
        )
    )

    # 4. channel must not be NULL
    channel_null_count = (
        df
        .filter(
            F.col("channel").isNull()
        )
        .count()
    )

    results.append(
        ValidationResult(
            rule_name="transactions.channel_not_null",
            passed=channel_null_count == 0,
            actual_value=channel_null_count,
            expected="0 NULL channel rows",
        )
    )

    # 5. amount > 0
    invalid_amount_count = (
        df
        .filter(
            F.col("amount") <= 0
        )
        .count()
    )

    results.append(
        ValidationResult(
            rule_name="transactions.valid_amount",
            passed=invalid_amount_count == 0,
            actual_value=invalid_amount_count,
            expected="0 rows with amount <= 0",
        )
    )
    
    # 6. balance must not be negative
    invalid_balance_count = (
        df
        .filter(
            (F.col("old_balance") < 0)
            | (F.col("new_balance") < 0)
        )
        .count()
    )

    results.append(
        ValidationResult(
            rule_name="transactions.valid_balance",
            passed=invalid_balance_count == 0,
            actual_value=invalid_balance_count,
            expected="0 negative balance rows",
        )
    )

    # 7. Valid transaction types
    valid_types = (
        "deposit",
        "withdraw",
        "transfer",
        "payment",
    )

    invalid_type_count = (
        df
        .filter(
            ~F.col("type").isin(*valid_types)
        )
        .count()
    )

    results.append(
        ValidationResult(
            rule_name="transactions.valid_type",
            passed=invalid_type_count == 0,
            actual_value=invalid_type_count,
            expected="0 invalid transaction types",
        )
    )

    # 8. Valid transaction status
    valid_status = (
        "success",
        "failed",
        "pending",
    )

    invalid_status_count = (
        df
        .filter(
            ~F.col("status").isin(*valid_status)
        )
        .count()
    )

    results.append(
        ValidationResult(
            rule_name="transactions.valid_status",
            passed=invalid_status_count == 0,
            actual_value=invalid_status_count,
            expected="0 invalid transaction status rows",
        )
    )

    return results


def validate_users(df: DataFrame,) -> list[ValidationResult]:
    return [
        validate_non_empty(
            df,
            "users",
        ),
        validate_no_null_key(
            df,
            "users",
            ["user_id"],
        ),
        validate_unique_key(
            df,
            "users",
            ["user_id"],
        ),
    ]


def validate_accounts(df: DataFrame,) -> list[ValidationResult]:
    return [
        validate_non_empty(
            df,
            "accounts",
        ),
        validate_no_null_key(
            df,
            "accounts",
            [
                "account_id",
                "user_id",
            ],
        ),
        validate_unique_key(
            df,
            "accounts",
            ["account_id"],
        ),
    ]


def validate_merchants(df: DataFrame,) -> list[ValidationResult]:
    return [
        validate_non_empty(
            df,
            "merchants",
        ),
        validate_no_null_key(
            df,
            "merchants",
            ["merchant_id"],
        ),
        validate_unique_key(
            df,
            "merchants",
            ["merchant_id"],
        ),
    ]


def validate_devices(df: DataFrame,) -> list[ValidationResult]:
    return [
        validate_non_empty(
            df,
            "devices",
        ),
        validate_no_null_key(
            df,
            "devices",
            [
                "device_id",
                "user_id",
            ],
        ),
        validate_unique_key(
            df,
            "devices",
            ["device_id"],
        ),
    ]


def validate_balance_snapshots(df: DataFrame,) -> list[ValidationResult]:
    results = [
        validate_non_empty(
            df,
            "balance_snapshots",
        ),
        validate_no_null_key(
            df,
            "balance_snapshots",
            [
                "account_id",
                "snapshot_date",
            ],
        ),
        validate_unique_key(
            df,
            "balance_snapshots",
            [
                "account_id",
                "snapshot_date",
            ],
        ),
    ]

    invalid_balance_count = (
        df
        .filter(
            F.col("closing_balance") < 0
        )
        .count()
    )

    results.append(
        ValidationResult(
            rule_name="balance_snapshots.valid_balance",
            passed=invalid_balance_count == 0,
            actual_value=invalid_balance_count,
            expected="0 negative closing_balance rows",
        )
    )

    return results


def validate_login_events(df: DataFrame,) -> list[ValidationResult]:
    return [
        validate_non_empty(
            df,
            "login_events",
        ),
        validate_no_null_key(
            df,
            "login_events",
            [
                "login_id",
                "user_id",
                "device_id",
            ],
        ),
        validate_unique_key(
            df,
            "login_events",
            ["login_id"],
        ),
    ]


def log_results(results: list[ValidationResult],) -> bool:
    all_passed = True

    logger.info("")
    logger.info(
        "============== SILVER DQ REPORT =============="
    )

    for result in results:
        status = (
            "PASS"
            if result.passed
            else "FAIL"
        )

        if not result.passed:
            all_passed = False

        logger.info(
            "%-6s | %-40s | actual=%s | expected=%s",
            status,
            result.rule_name,
            result.actual_value,
            result.expected,
        )

    logger.info(
        "=============================================="
    )

    return all_passed


def run_validation() -> None:
    spark = None

    try:
        spark = create_spark_session(
            app_name="EWallet-Silver-Validation",
            optimized=True,
        )

        validators = [
            (
                "users",
                validate_users,
            ),
            (
                "accounts",
                validate_accounts,
            ),
            (
                "merchants",
                validate_merchants,
            ),
            (
                "devices",
                validate_devices,
            ),
            (
                "transactions",
                validate_transactions,
            ),
            (
                "balance_snapshots",
                validate_balance_snapshots,
            ),
            (
                "login_events",
                validate_login_events,
            ),
        ]

        all_results = []

        for (
            table_name,
            validator,
        ) in validators:

            logger.info(
                "========== VALIDATE %s ==========",
                table_name,
            )

            df = read_silver(
                spark,
                table_name,
            )

            table_results = validator(
                df
            )

            all_results.extend(
                table_results
            )

        passed = log_results(
            all_results
        )

        if not passed:
            raise RuntimeError(
                "Silver Data Quality validation FAILED."
            )

        logger.info(
            "Silver Data Quality validation PASSED."
        )

    finally:
        if spark is not None:
            spark.stop()

            logger.info(
                "SparkSession stopped."
            )


if __name__ == "__main__":
    try:
        run_validation()
        sys.exit(0)

    except Exception:
        logger.exception(
            "Silver validation failed."
        )
        sys.exit(1)