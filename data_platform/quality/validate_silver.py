from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from data_platform.processing.spark.common.spark_session import create_spark_session


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


def validate_no_null_columns(
    df: DataFrame,
    table_name: str,
    columns: list[str],
) -> ValidationResult:
    condition = None

    for column_name in columns:
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
        rule_name=f"{table_name}.required_columns_not_null",
        passed=invalid_count == 0,
        actual_value=invalid_count,
        expected="0 NULL required rows",
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


def validate_foreign_key(
    child_df: DataFrame,
    parent_df: DataFrame,
    child_column: str,
    parent_column: str,
    rule_name: str,
) -> ValidationResult:
    """Check that every non-null child key exists in the parent table."""

    child_keys = (
        child_df
        .select(F.col(child_column).alias("fk"))
        .filter(F.col("fk").isNotNull())
        .distinct()
    )

    parent_keys = (
        parent_df
        .select(F.col(parent_column).alias("pk"))
        .filter(F.col("pk").isNotNull())
        .distinct()
    )

    orphan_count = (
        child_keys
        .join(
            parent_keys,
            child_keys["fk"] == parent_keys["pk"],
            "left_anti",
        )
        .count()
    )

    return ValidationResult(
        rule_name=rule_name,
        passed=orphan_count == 0,
        actual_value=orphan_count,
        expected="0 orphan keys",
    )


def validate_conditional_not_null(
    df: DataFrame,
    table_name: str,
    condition,
    column_name: str,
    condition_description: str,
) -> ValidationResult:
    invalid_count = (
        df
        .filter(condition & F.col(column_name).isNull())
        .count()
    )

    return ValidationResult(
        rule_name=(
            f"{table_name}.{column_name}_required_"
            f"for_{condition_description}"
        ),
        passed=invalid_count == 0,
        actual_value=invalid_count,
        expected="0 rows missing conditionally required values",
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
        validate_no_null_columns(
            df,
            "transactions",
            [
                "transaction_id",
                "account_id",
                "user_id",
                "device_id",
                "type",
                "amount",
                "currency",
                "status",
                "channel",
                "old_balance",
                "new_balance",
                "timestamp",
                "ingested_at",
                "event_date",
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

    valid_channels = (
        "app",
        "web",
        "atm",
        "UNKNOWN",
    )

    invalid_channel_count = (
        df
        .filter(~F.col("channel").isin(*valid_channels))
        .count()
    )

    results.append(
        ValidationResult(
            rule_name="transactions.valid_channel",
            passed=invalid_channel_count == 0,
            actual_value=invalid_channel_count,
            expected="0 invalid transaction channel rows",
        )
    )

    invalid_currency_count = (
        df
        .filter(F.col("currency") != "VND")
        .count()
    )

    results.append(
        ValidationResult(
            rule_name="transactions.valid_currency",
            passed=invalid_currency_count == 0,
            actual_value=invalid_currency_count,
            expected="0 non-VND transaction rows",
        )
    )

    results.extend([
        validate_conditional_not_null(
            df,
            "transactions",
            F.col("type") == "payment",
            "merchant_id",
            "payment",
        ),
        validate_conditional_not_null(
            df,
            "transactions",
            F.col("type") == "transfer",
            "counterparty_account_id",
            "transfer",
        ),
    ])

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
        validate_no_null_columns(
            df,
            "users",
            [
                "user_id",
                "full_name",
                "email",
                "phone",
                "kyc_verified",
                "created_at",
            ],
        ),
        validate_unique_key(
            df,
            "users",
            ["user_id"],
        ),
    ]


def validate_accounts(df: DataFrame,) -> list[ValidationResult]:
    results = [
        validate_non_empty(
            df,
            "accounts",
        ),
        validate_no_null_columns(
            df,
            "accounts",
            [
                "account_id",
                "user_id",
                "account_type",
                "currency",
                "created_at",
            ],
        ),
        validate_unique_key(
            df,
            "accounts",
            ["account_id"],
        ),
    ]

    invalid_domain_count = (
        df
        .filter(
            ~F.col("account_type").isin(
                "wallet_vnd",
                "points",
            )
            | (F.col("currency") != "VND")
        )
        .count()
    )

    results.append(
        ValidationResult(
            rule_name="accounts.valid_domain",
            passed=invalid_domain_count == 0,
            actual_value=invalid_domain_count,
            expected="0 invalid account_type/currency rows",
        )
    )

    return results


def validate_merchants(df: DataFrame,) -> list[ValidationResult]:
    return [
        validate_non_empty(
            df,
            "merchants",
        ),
        validate_no_null_columns(
            df,
            "merchants",
            [
                "merchant_id",
                "merchant_name",
                "category",
            ],
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
        validate_no_null_columns(
            df,
            "devices",
            [
                "device_id",
                "user_id",
                "device_type",
                "os",
                "first_seen_at",
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
        validate_no_null_columns(
            df,
            "balance_snapshots",
            [
                "account_id",
                "snapshot_date",
                "closing_balance",
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
        validate_no_null_columns(
            df,
            "login_events",
            [
                "login_id",
                "user_id",
                "device_id",
                "login_ts",
                "login_date",
                "is_success",
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

        tables = {}
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

            tables[table_name] = df

            table_results = validator(
                df
            )

            all_results.extend(
                table_results
            )

        logger.info(
            "========== VALIDATE RELATIONSHIPS ==========",
        )

        all_results.extend([
            validate_foreign_key(
                tables["accounts"], tables["users"],
                "user_id", "user_id",
                "accounts.user_id_exists_in_users",
            ),
            validate_foreign_key(
                tables["devices"], tables["users"],
                "user_id", "user_id",
                "devices.user_id_exists_in_users",
            ),
            validate_foreign_key(
                tables["transactions"], tables["users"],
                "user_id", "user_id",
                "transactions.user_id_exists_in_users",
            ),
            validate_foreign_key(
                tables["transactions"], tables["accounts"],
                "account_id", "account_id",
                "transactions.account_id_exists_in_accounts",
            ),
            validate_foreign_key(
                tables["transactions"], tables["devices"],
                "device_id", "device_id",
                "transactions.device_id_exists_in_devices",
            ),
            validate_foreign_key(
                tables["transactions"], tables["merchants"],
                "merchant_id", "merchant_id",
                "transactions.merchant_id_exists_in_merchants",
            ),
            validate_foreign_key(
                tables["transactions"], tables["accounts"],
                "counterparty_account_id", "account_id",
                (
                    "transactions.counterparty_account_id_"
                    "exists_in_accounts"
                ),
            ),
            validate_foreign_key(
                tables["balance_snapshots"], tables["accounts"],
                "account_id", "account_id",
                "balance_snapshots.account_id_exists_in_accounts",
            ),
            validate_foreign_key(
                tables["login_events"], tables["users"],
                "user_id", "user_id",
                "login_events.user_id_exists_in_users",
            ),
            validate_foreign_key(
                tables["login_events"], tables["devices"],
                "device_id", "device_id",
                "login_events.device_id_exists_in_devices",
            ),
        ])

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
