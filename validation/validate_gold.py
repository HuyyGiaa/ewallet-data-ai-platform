"""
Gold Layer Data Quality Validation.

Checks:
    - Dimensions
    - Facts
    - OBT
    - Offline features
    - Analytical outputs
    - Silver -> Gold row-count contracts
    - Date dimension relationships
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from transformation.spark.spark_session import create_spark_session


# ============================================================
# Paths
# ============================================================

SILVER_ROOT = "s3a://silver-zone"
GOLD_ROOT = "s3a://gold-zone"


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


# ============================================================
# Result Model
# ============================================================

@dataclass
class ValidationResult:
    rule_name: str
    passed: bool
    actual_value: int | float | str
    expected: str


# ============================================================
# IO
# ============================================================

def read_delta_table(
    spark,
    root: str,
    table_name: str,
) -> DataFrame:

    path = f"{root}/{table_name}"

    logger.info(
        "[%s] Reading: %s",
        table_name,
        path,
    )

    return (
        spark.read
        .format("delta")
        .load(path)
    )


def read_gold(
    spark,
    table_name: str,
) -> DataFrame:

    return read_delta_table(
        spark,
        GOLD_ROOT,
        table_name,
    )


def read_silver(
    spark,
    table_name: str,
) -> DataFrame:

    return read_delta_table(
        spark,
        SILVER_ROOT,
        table_name,
    )


# ============================================================
# Common Rules
# ============================================================

def validate_non_empty(
    df: DataFrame,
    table_name: str,
) -> ValidationResult:

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

    condition = F.col(
        columns[0]
    ).isNull()

    for column_name in columns[1:]:
        condition = (
            condition
            | F.col(column_name).isNull()
        )

    null_count = (
        df
        .filter(condition)
        .count()
    )

    return ValidationResult(
        rule_name=f"{table_name}.required_columns_not_null",
        passed=null_count == 0,
        actual_value=null_count,
        expected="0 NULL required rows",
    )


def validate_unique_key(
    df: DataFrame,
    table_name: str,
    key_columns: list[str],
) -> ValidationResult:

    duplicate_groups = (
        df

        .groupBy(
            *key_columns
        )

        .count()

        .filter(
            F.col("count") > 1
        )

        .count()
    )

    key_name = ",".join(
        key_columns
    )

    return ValidationResult(
        rule_name=f"{table_name}.unique_key({key_name})",
        passed=duplicate_groups == 0,
        actual_value=duplicate_groups,
        expected="0 duplicate key groups",
    )


def validate_non_negative(
    df: DataFrame,
    table_name: str,
    columns: list[str],
) -> ValidationResult:

    condition = (
        F.col(columns[0]) < 0
    )

    for column_name in columns[1:]:
        condition = (
            condition
            | (F.col(column_name) < 0)
        )

    invalid_count = (
        df
        .filter(condition)
        .count()
    )

    return ValidationResult(
        rule_name=f"{table_name}.non_negative_metrics",
        passed=invalid_count == 0,
        actual_value=invalid_count,
        expected="0 negative metric rows",
    )


def validate_row_count_equal(
    left_df: DataFrame,
    right_df: DataFrame,
    rule_name: str,
) -> ValidationResult:

    left_count = left_df.count()
    right_count = right_df.count()

    return ValidationResult(
        rule_name=rule_name,
        passed=left_count == right_count,
        actual_value=(
            f"{left_count} vs {right_count}"
        ),
        expected="row counts must be equal",
    )


def validate_foreign_key(
    child_df: DataFrame,
    parent_df: DataFrame,
    child_column: str,
    parent_column: str,
    rule_name: str,
) -> ValidationResult:
    """
    Check that every non-null child key exists
    in the parent table.
    """

    child_keys = (
        child_df

        .select(
            F.col(
                child_column
            ).alias("fk")
        )

        .filter(
            F.col("fk").isNotNull()
        )

        .distinct()
    )

    parent_keys = (
        parent_df

        .select(
            F.col(
                parent_column
            ).alias("pk")
        )

        .filter(
            F.col("pk").isNotNull()
        )

        .distinct()
    )

    orphan_count = (
        child_keys

        .join(
            parent_keys,
            child_keys["fk"]
            == parent_keys["pk"],
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


# ============================================================
# Dimension Rules
# ============================================================

def validate_dimension(
    df: DataFrame,
    table_name: str,
    primary_key: str,
) -> list[ValidationResult]:

    return [
        validate_non_empty(
            df,
            table_name,
        ),

        validate_no_null_columns(
            df,
            table_name,
            [primary_key],
        ),

        validate_unique_key(
            df,
            table_name,
            [primary_key],
        ),
    ]


# ============================================================
# fact_transactions
# ============================================================

def validate_fact_transactions(
    df: DataFrame,
) -> list[ValidationResult]:

    return [
        validate_non_empty(
            df,
            "fact_transactions",
        ),

        validate_no_null_columns(
            df,
            "fact_transactions",
            [
                "transaction_id",
                "user_id",
                "account_id",
                "date_key",
                "event_date",
                "timestamp",
            ],
        ),

        validate_unique_key(
            df,
            "fact_transactions",
            ["transaction_id"],
        ),

        validate_non_negative(
            df,
            "fact_transactions",
            [
                "amount",
                "old_balance",
                "new_balance",
            ],
        ),
    ]


# ============================================================
# fact_login_events
# ============================================================

def validate_fact_login_events(
    df: DataFrame,
) -> list[ValidationResult]:

    return [
        validate_non_empty(
            df,
            "fact_login_events",
        ),

        validate_no_null_columns(
            df,
            "fact_login_events",
            [
                "login_id",
                "user_id",
                "device_id",
                "date_key",
                "login_ts",
                "event_date",
            ],
        ),

        validate_unique_key(
            df,
            "fact_login_events",
            ["login_id"],
        ),
    ]


# ============================================================
# fact_balance_snapshot
# ============================================================

def validate_fact_balance_snapshot(
    df: DataFrame,
) -> list[ValidationResult]:

    return [
        validate_non_empty(
            df,
            "fact_balance_snapshot",
        ),

        validate_no_null_columns(
            df,
            "fact_balance_snapshot",
            [
                "account_id",
                "date_key",
                "snapshot_date",
                "closing_balance",
            ],
        ),

        validate_unique_key(
            df,
            "fact_balance_snapshot",
            [
                "account_id",
                "snapshot_date",
            ],
        ),

        validate_non_negative(
            df,
            "fact_balance_snapshot",
            [
                "closing_balance",
            ],
        ),
    ]

# ============================================================
# obt_transaction_enriched
# ============================================================

def validate_obt_transaction_enriched(
    df: DataFrame,
) -> list[ValidationResult]:
    """
    Validate the denormalized transaction OBT.

    Grain:
        1 row / transaction.
    """

    results = [
        # ----------------------------------------------------
        # Table must contain data
        # ----------------------------------------------------
        validate_non_empty(
            df,
            "obt_transaction_enriched",
        ),

        # ----------------------------------------------------
        # Required transaction-level columns
        # ----------------------------------------------------
        validate_no_null_columns(
            df,
            "obt_transaction_enriched",
            [
                "transaction_id",
                "user_id",
                "account_id",
                "date_key",
                "timestamp",
                "event_date",
            ],
        ),

        # ----------------------------------------------------
        # Grain must remain 1 row / transaction
        # ----------------------------------------------------
        validate_unique_key(
            df,
            "obt_transaction_enriched",
            [
                "transaction_id",
            ],
        ),

        # ----------------------------------------------------
        # Measures must remain valid
        # ----------------------------------------------------
        validate_non_negative(
            df,
            "obt_transaction_enriched",
            [
                "amount",
                "old_balance",
                "new_balance",
            ],
        ),
    ]

    return results

# ============================================================
# feat_user_90d
# ============================================================

def validate_feat_user_90d(
    df: DataFrame,
) -> list[ValidationResult]:

    results = [
        validate_non_empty(
            df,
            "feat_user_90d",
        ),

        validate_no_null_columns(
            df,
            "feat_user_90d",
            [
                "user_id",
                "event_timestamp",
                "created_timestamp",
            ],
        ),

        validate_unique_key(
            df,
            "feat_user_90d",
            ["user_id"],
        ),

        validate_non_negative(
            df,
            "feat_user_90d",
            [
                "f_user_total_transactions_90d",
                "f_user_avg_transaction_amount_90d",
                "f_user_distinct_merchants_90d",
            ],
        ),
    ]

    invalid_rate_count = (
        df

        .filter(
            F.col(
                "f_user_failed_transaction_rate_90d"
            ).isNull()
            |
            (
                F.col(
                    "f_user_failed_transaction_rate_90d"
                ) < 0
            )
            |
            (
                F.col(
                    "f_user_failed_transaction_rate_90d"
                ) > 1
            )
        )

        .count()
    )

    results.append(
        ValidationResult(
            rule_name=(
                "feat_user_90d."
                "failed_transaction_rate_between_0_and_1"
            ),
            passed=invalid_rate_count == 0,
            actual_value=invalid_rate_count,
            expected="0 invalid rate rows",
        )
    )

    return results


# ============================================================
# opt_merchant_performance
# ============================================================

def validate_opt_merchant_performance(
    df: DataFrame,
) -> list[ValidationResult]:

    return [
        validate_non_empty(
            df,
            "opt_merchant_performance",
        ),

        validate_no_null_columns(
            df,
            "opt_merchant_performance",
            [
                "merchant_id",
            ],
        ),

        validate_unique_key(
            df,
            "opt_merchant_performance",
            [
                "merchant_id",
            ],
        ),

        validate_non_negative(
            df,
            "opt_merchant_performance",
            [
                "transaction_count",
                "total_amount",
                "avg_amount",
                "success_count",
                "failed_count",
                "distinct_users",
            ],
        ),
    ]


# ============================================================
# Report
# ============================================================

def log_results(
    results: list[ValidationResult],
) -> bool:

    passed_count = 0
    failed_count = 0

    logger.info("")
    logger.info(
        "================ GOLD DQ REPORT ================"
    )

    for result in results:

        status = (
            "PASS"
            if result.passed
            else "FAIL"
        )

        if result.passed:
            passed_count += 1
        else:
            failed_count += 1

        logger.info(
            "%-6s | %-58s | actual=%s | expected=%s",
            status,
            result.rule_name,
            result.actual_value,
            result.expected,
        )

    logger.info(
        "================================================"
    )

    logger.info(
        "Validation summary: %s PASS | %s FAIL",
        passed_count,
        failed_count,
    )

    return failed_count == 0


# ============================================================
# Validation Pipeline
# ============================================================

def run_validation() -> None:

    spark = None

    try:

        spark = create_spark_session(
            app_name="EWallet-Gold-Validation",
            optimized=True,
        )

        # ====================================================
        # Read Gold
        # ====================================================

        logger.info(
            "========== READ GOLD =========="
        )

        dim_user = read_gold(
            spark,
            "dim_user",
        )

        dim_account = read_gold(
            spark,
            "dim_account",
        )

        dim_merchant = read_gold(
            spark,
            "dim_merchant",
        )

        dim_device = read_gold(
            spark,
            "dim_device",
        )

        dim_date = read_gold(
            spark,
            "dim_date",
        )

        fact_transactions = read_gold(
            spark,
            "fact_transactions",
        )

        fact_login_events = read_gold(
            spark,
            "fact_login_events",
        )

        fact_balance_snapshot = read_gold(
            spark,
            "fact_balance_snapshot",
        )

        obt_transaction_enriched = read_gold(
            spark,
            "obt_transaction_enriched",
        )
        
        feat_user_90d = read_gold(
            spark,
            "feat_user_90d",
        )

        opt_merchant_performance = read_gold(
            spark,
            "opt_merchant_performance",
        )

        # ====================================================
        # Read Silver for cross-layer contracts
        # ====================================================

        logger.info(
            "========== READ SILVER FOR CONTRACTS =========="
        )

        silver_users = read_silver(
            spark,
            "users",
        )

        silver_accounts = read_silver(
            spark,
            "accounts",
        )

        silver_merchants = read_silver(
            spark,
            "merchants",
        )

        silver_devices = read_silver(
            spark,
            "devices",
        )

        silver_transactions = read_silver(
            spark,
            "transactions",
        )

        silver_login_events = read_silver(
            spark,
            "login_events",
        )

        silver_balance_snapshots = read_silver(
            spark,
            "balance_snapshots",
        )

        all_results = []

        # ====================================================
        # Dimensions
        # ====================================================

        all_results.extend(
            validate_dimension(
                dim_user,
                "dim_user",
                "user_id",
            )
        )

        all_results.extend(
            validate_dimension(
                dim_account,
                "dim_account",
                "account_id",
            )
        )

        all_results.extend(
            validate_dimension(
                dim_merchant,
                "dim_merchant",
                "merchant_id",
            )
        )

        all_results.extend(
            validate_dimension(
                dim_device,
                "dim_device",
                "device_id",
            )
        )

        all_results.extend(
            validate_dimension(
                dim_date,
                "dim_date",
                "date_key",
            )
        )

        # ====================================================
        # Facts
        # ====================================================

        all_results.extend(
            validate_fact_transactions(
                fact_transactions
            )
        )

        all_results.extend(
            validate_fact_login_events(
                fact_login_events
            )
        )

        all_results.extend(
            validate_fact_balance_snapshot(
                fact_balance_snapshot
            )
        )

        # ====================================================
        # OBT
        # ====================================================

        all_results.extend(
            validate_obt_transaction_enriched(
                obt_transaction_enriched
            )
        )
        
        # ====================================================
        # Feature + Analytical
        # ====================================================

        all_results.extend(
            validate_feat_user_90d(
                feat_user_90d
            )
        )

        all_results.extend(
            validate_opt_merchant_performance(
                opt_merchant_performance
            )
        )

        # ====================================================
        # Silver -> Gold row-count contracts
        # ====================================================

        all_results.extend([
            validate_row_count_equal(
                silver_users,
                dim_user,
                "silver.users_count_equals_dim_user_count",
            ),

            validate_row_count_equal(
                silver_accounts,
                dim_account,
                "silver.accounts_count_equals_dim_account_count",
            ),

            validate_row_count_equal(
                silver_merchants,
                dim_merchant,
                "silver.merchants_count_equals_dim_merchant_count",
            ),

            validate_row_count_equal(
                silver_devices,
                dim_device,
                "silver.devices_count_equals_dim_device_count",
            ),

            validate_row_count_equal(
                silver_transactions,
                fact_transactions,
                (
                    "silver.transactions_count_equals_"
                    "fact_transactions_count"
                ),
            ),

            validate_row_count_equal(
                silver_login_events,
                fact_login_events,
                (
                    "silver.login_events_count_equals_"
                    "fact_login_events_count"
                ),
            ),

            validate_row_count_equal(
                silver_balance_snapshots,
                fact_balance_snapshot,
                (
                    "silver.balance_snapshots_count_equals_"
                    "fact_balance_snapshot_count"
                ),
            ),
        ])

        # ====================================================
        # Gold internal contracts
        # ====================================================

        all_results.extend([
            validate_row_count_equal(
                fact_transactions,
                obt_transaction_enriched,
                (
                    "fact_transactions_count_equals_"
                    "obt_transaction_enriched_count"
                ),
            ),

            validate_row_count_equal(
                dim_user,
                feat_user_90d,
                (
                    "dim_user_count_equals_"
                    "feat_user_90d_count"
                ),
            ),

            validate_row_count_equal(
                dim_merchant,
                opt_merchant_performance,
                (
                    "dim_merchant_count_equals_"
                    "opt_merchant_performance_count"
                ),
            ),
        ])

        # ====================================================
        # Date dimension relationships
        # ====================================================

        all_results.extend([
            validate_foreign_key(
                fact_transactions,
                dim_date,
                "date_key",
                "date_key",
                (
                    "fact_transactions.date_key_"
                    "exists_in_dim_date"
                ),
            ),

            validate_foreign_key(
                fact_login_events,
                dim_date,
                "date_key",
                "date_key",
                (
                    "fact_login_events.date_key_"
                    "exists_in_dim_date"
                ),
            ),

            validate_foreign_key(
                fact_balance_snapshot,
                dim_date,
                "date_key",
                "date_key",
                (
                    "fact_balance_snapshot.date_key_"
                    "exists_in_dim_date"
                ),
            ),
        ])

        # ====================================================
        # Final Report
        # ====================================================

        passed = log_results(
            all_results
        )

        if not passed:
            raise RuntimeError(
                "Gold Data Quality validation FAILED."
            )

        logger.info("")
        logger.info(
            "Gold Data Quality validation PASSED."
        )

    finally:

        if spark is not None:
            spark.stop()

            logger.info(
                "SparkSession stopped."
            )


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":

    try:
        run_validation()
        sys.exit(0)

    except Exception:

        logger.exception(
            "Gold validation failed."
        )

        sys.exit(1)