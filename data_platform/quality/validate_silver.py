from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from data_platform.processing.spark.common.spark_session import create_spark_session


SILVER_ROOT = "s3a://silver-zone"
BRONZE_ROOT = "s3a://bronze-zone"

FRAUD_TYPES = (
    "velocity",
    "amount_anomaly",
    "account_takeover",
    "merchant_burst",
)


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


def read_bronze(spark, table_name: str) -> DataFrame:
    return spark.read.format("delta").load(f"{BRONZE_ROOT}/{table_name}")


def validate_required_columns(
    df: DataFrame,
    table_name: str,
    columns: list[str],
) -> ValidationResult:
    missing = sorted(set(columns).difference(df.columns))
    return ValidationResult(
        rule_name=f"{table_name}.required_columns",
        passed=not missing,
        actual_value="all present" if not missing else f"missing={','.join(missing)}",
        expected="all required columns present",
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


def validate_fraud_labels(df: DataFrame) -> list[ValidationResult]:
    required_columns = ["transaction_id", "label", "fraud_type"]
    results = [
        validate_required_columns(df, "fraud_labels", required_columns),
        validate_non_empty(df, "fraud_labels"),
        validate_no_null_columns(
            df,
            "fraud_labels",
            ["transaction_id", "label"],
        ),
        validate_unique_key(df, "fraud_labels", ["transaction_id"]),
    ]

    invalid_labels = df.filter(~F.col("label").isin(0, 1)).count()
    invalid_conditional_types = df.filter(
        ((F.col("label") == 0) & F.col("fraud_type").isNotNull())
        | ((F.col("label") == 1) & F.col("fraud_type").isNull())
    ).count()
    invalid_fraud_types = df.filter(
        (F.col("label") == 1)
        & F.col("fraud_type").isNotNull()
        & ~F.col("fraud_type").isin(*FRAUD_TYPES)
    ).count()

    results.extend(
        [
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
        ]
    )
    return results


def validate_exact_transaction_coverage(
    labels_df: DataFrame,
    transactions_df: DataFrame,
    prefix: str,
) -> list[ValidationResult]:
    label_ids = labels_df.select("transaction_id").distinct()
    transaction_ids = transactions_df.select("transaction_id").distinct()
    orphan_count = label_ids.join(transaction_ids, "transaction_id", "left_anti").count()
    missing_count = transaction_ids.join(label_ids, "transaction_id", "left_anti").count()
    label_count = labels_df.count()
    transaction_count = transactions_df.count()

    return [
        ValidationResult(
            f"{prefix}.transaction_fk",
            orphan_count == 0,
            orphan_count,
            "0 orphan label transaction IDs",
        ),
        ValidationResult(
            f"{prefix}.complete_transaction_coverage",
            missing_count == 0,
            missing_count,
            "0 transactions without labels",
        ),
        ValidationResult(
            f"{prefix}.population_matches_transactions",
            label_count == transaction_count,
            f"labels={label_count},transactions={transaction_count}",
            "fraud label rows equal Silver transaction rows",
        ),
    ]


def validate_bronze_silver_fraud_population(
    bronze_df: DataFrame,
    silver_df: DataFrame,
) -> ValidationResult:
    bronze_count = bronze_df.count()
    silver_count = silver_df.count()
    return ValidationResult(
        "fraud_labels.bronze_silver_population",
        bronze_count == silver_count,
        f"bronze={bronze_count},silver={silver_count}",
        "Bronze and Silver fraud label row counts are equal",
    )


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


def run_validation(include_fraud_labels: bool = False) -> None:
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

        if include_fraud_labels:
            validators.append(("fraud_labels", validate_fraud_labels))

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

        if include_fraud_labels:
            all_results.extend(
                validate_exact_transaction_coverage(
                    tables["fraud_labels"],
                    tables["transactions"],
                    "fraud_labels",
                )
            )
            bronze_fraud_labels = read_bronze(spark, "fraud_labels")
            all_results.append(
                validate_bronze_silver_fraud_population(
                    bronze_fraud_labels,
                    tables["fraud_labels"],
                )
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate persisted Silver Delta data.")
    parser.add_argument(
        "--include-fraud-labels",
        action="store_true",
        help="Require and validate Silver fraud_labels in transitional fraud mode.",
    )
    args = parser.parse_args()
    try:
        run_validation(include_fraud_labels=args.include_fraud_labels)
        return 0

    except Exception:
        logger.exception(
            "Silver validation failed."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
