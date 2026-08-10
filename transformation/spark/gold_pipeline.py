"""
DP3 - Silver -> Gold Pipeline

Gold schema
-----------
Dimensions:
    - dim_user
    - dim_account
    - dim_merchant
    - dim_device
    - dim_date

Facts:
    - fact_transactions
    - fact_login_events
    - fact_balance_snapshot

Feature:
    - feat_user_90d
OBT:
    - obt_transaction_enriched
Analytical:
    - opt_merchant_performance

All tables are stored as Delta Lake tables in MinIO.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import timedelta

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from spark_session import create_spark_session


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
# Common IO
# ============================================================

def read_silver(
    spark,
    table_name: str,
) -> DataFrame:
    """
    Read one Delta table from Silver layer.
    """

    path = f"{SILVER_ROOT}/{table_name}"

    logger.info(
        "[%s] Reading Silver: %s",
        table_name,
        path,
    )

    return (
        spark.read
        .format("delta")
        .load(path)
    )


def write_gold(
    df: DataFrame,
    table_name: str,
    partition_columns: list[str] | None = None,
) -> None:
    """
    Write one DataFrame to Gold layer as Delta Lake.

    overwrite:
        Gold tables are rebuilt from Silver on every DP3 run.

    mergeSchema:
        Allows compatible schema evolution.
    """

    path = f"{GOLD_ROOT}/{table_name}"

    logger.info(
        "[%s] Writing Gold: %s",
        table_name,
        path,
    )

    writer = (
        df.write
        .format("delta")
        .mode("overwrite")
        .option("mergeSchema", "true")
    )

    if partition_columns:
        writer = writer.partitionBy(
            *partition_columns
        )

    writer.save(path)

    logger.info(
        "[%s] Gold write completed.",
        table_name,
    )


# ============================================================
# Dimensions
# ============================================================

def build_dim_user(
    users_df: DataFrame,
) -> DataFrame:
    """
    Grain:
        1 row / user.
    """

    return users_df.select(
        "user_id",
        "full_name",
        "email",
        "phone",
        "kyc_verified",
        "created_at",
    )


def build_dim_account(
    accounts_df: DataFrame,
) -> DataFrame:
    """
    Grain:
        1 row / account.
    """

    return accounts_df.select(
        "account_id",
        "user_id",
        "account_type",
        "currency",
        "created_at",
    )


def build_dim_merchant(
    merchants_df: DataFrame,
) -> DataFrame:
    """
    Grain:
        1 row / merchant.
    """

    return merchants_df.select(
        "merchant_id",
        "merchant_name",
        "category",
    )


def build_dim_device(
    devices_df: DataFrame,
) -> DataFrame:
    """
    Grain:
        1 row / device.
    """

    return devices_df.select(
        "device_id",
        "user_id",
        "device_type",
        "os",
        "first_seen_at",
    )


def build_dim_date(
    transactions_df: DataFrame,
    login_events_df: DataFrame,
    balance_snapshots_df: DataFrame,
) -> DataFrame:
    """
    Date Dimension.

    Dates are collected from all fact sources:
        - transactions.timestamp
        - login_events.login_ts
        - balance_snapshots.snapshot_date

    Grain:
        1 row / calendar date.
    """

    transaction_dates = (
        transactions_df
        .select(
            F.to_date(
                F.col("timestamp")
            ).alias("calendar_date")
        )
    )

    login_dates = (
        login_events_df
        .select(
            F.to_date(
                F.col("login_ts")
            ).alias("calendar_date")
        )
    )

    snapshot_dates = (
        balance_snapshots_df
        .select(
            F.to_date(
                F.col("snapshot_date")
            ).alias("calendar_date")
        )
    )

    all_dates = (
        transaction_dates
        .unionByName(login_dates)
        .unionByName(snapshot_dates)
        .filter(
            F.col("calendar_date").isNotNull()
        )
        .distinct()
    )

    return (
        all_dates

        .withColumn(
            "date_key",
            F.date_format(
                F.col("calendar_date"),
                "yyyyMMdd",
            ).cast("int"),
        )

        .withColumn(
            "day",
            F.dayofmonth(
                F.col("calendar_date")
            ),
        )

        .withColumn(
            "month",
            F.month(
                F.col("calendar_date")
            ),
        )

        .withColumn(
            "quarter",
            F.quarter(
                F.col("calendar_date")
            ),
        )

        .withColumn(
            "year",
            F.year(
                F.col("calendar_date")
            ),
        )

        .withColumn(
            "day_of_week",
            F.date_format(
                F.col("calendar_date"),
                "EEEE",
            ),
        )

        .withColumn(
            "is_weekend",
            F.dayofweek(
                F.col("calendar_date")
            ).isin(1, 7),
        )

        .select(
            "date_key",
            "calendar_date",
            "day",
            "month",
            "quarter",
            "year",
            "day_of_week",
            "is_weekend",
        )
    )


# ============================================================
# Fact Tables
# ============================================================

def build_fact_transactions(
    transactions_df: DataFrame,
) -> DataFrame:
    """
    Transaction Fact.

    Grain:
        1 row / transaction.
    """

    return (
        transactions_df

        .withColumn(
            "event_date",
            F.to_date(
                F.col("timestamp")
            ),
        )

        .withColumn(
            "date_key",
            F.date_format(
                F.col("event_date"),
                "yyyyMMdd",
            ).cast("int"),
        )

        .select(
            # Primary key
            "transaction_id",

            # Foreign / relationship keys
            "user_id",
            "account_id",
            "merchant_id",
            "device_id",
            "counterparty_account_id",
            "date_key",

            # Business attributes
            "type",
            "status",
            "channel",
            "currency",

            # Measures
            "amount",
            "old_balance",
            "new_balance",

            # Temporal columns
            "timestamp",
            "ingested_at",
            "event_date",
        )
    )


def build_fact_login_events(
    login_events_df: DataFrame,
) -> DataFrame:
    """
    Login Event Fact.

    Grain:
        1 row / login attempt.
    """

    return (
        login_events_df

        .withColumn(
            "event_date",
            F.to_date(
                F.col("login_ts")
            ),
        )

        .withColumn(
            "date_key",
            F.date_format(
                F.col("event_date"),
                "yyyyMMdd",
            ).cast("int"),
        )

        .select(
            # Primary key
            "login_id",

            # Foreign keys
            "user_id",
            "device_id",
            "date_key",

            # Event information
            "is_success",
            "login_ts",
            "event_date",
        )
    )


def build_fact_balance_snapshot(
    balance_snapshots_df: DataFrame,
) -> DataFrame:
    """
    Periodic Snapshot Fact.

    Grain:
        1 row / account / day.

    Logical composite key:
        (account_id, snapshot_date)
    """

    return (
        balance_snapshots_df

        .withColumn(
            "snapshot_date",
            F.to_date(
                F.col("snapshot_date")
            ),
        )

        .withColumn(
            "date_key",
            F.date_format(
                F.col("snapshot_date"),
                "yyyyMMdd",
            ).cast("int"),
        )

        .select(
            "account_id",
            "date_key",
            "snapshot_date",
            "closing_balance",
        )
    )

# ============================================================
# One Big Table (OBT)
# ============================================================

def build_obt_transaction_enriched(
    fact_transactions_df: DataFrame,
    dim_user_df: DataFrame,
    dim_account_df: DataFrame,
    dim_device_df: DataFrame,
    dim_merchant_df: DataFrame,
    dim_date_df: DataFrame,
) -> DataFrame:
    """
    Denormalized transaction table for BI / analytical queries.

    Grain:
        1 row / transaction.

    Purpose:
        Join commonly-used dimension attributes into the
        transaction fact so downstream analytical queries
        do not need to repeatedly join many dimension tables.

    All joins are LEFT JOINs to preserve every transaction.
    """

    # --------------------------------------------------------
    # Select only useful attributes from dimensions.
    #
    # Rename duplicated / ambiguous columns before joining.
    # --------------------------------------------------------

    user_attrs = (
        dim_user_df
        .select(
            "user_id",
            "kyc_verified",
            F.col("created_at").alias(
                "user_created_at"
            ),
        )
    )

    account_attrs = (
        dim_account_df
        .select(
            "account_id",
            "account_type",
            F.col("currency").alias(
                "account_currency"
            ),
            F.col("created_at").alias(
                "account_created_at"
            ),
        )
    )

    device_attrs = (
        dim_device_df
        .select(
            "device_id",
            "device_type",
            "os",
            "first_seen_at",
        )
    )

    merchant_attrs = (
        dim_merchant_df
        .select(
            "merchant_id",
            "merchant_name",
            F.col("category").alias(
                "merchant_category"
            ),
        )
    )

    date_attrs = (
        dim_date_df
        .select(
            "date_key",
            "day_of_week",
            "month",
            "quarter",
            "year",
            "is_weekend",
        )
    )

    # --------------------------------------------------------
    # Fact + Dimensions
    # --------------------------------------------------------

    return (
        fact_transactions_df

        .join(
            user_attrs,
            on="user_id",
            how="left",
        )

        .join(
            account_attrs,
            on="account_id",
            how="left",
        )

        .join(
            device_attrs,
            on="device_id",
            how="left",
        )

        .join(
            merchant_attrs,
            on="merchant_id",
            how="left",
        )

        .join(
            date_attrs,
            on="date_key",
            how="left",
        )

        .select(
            # ------------------------------------------------
            # Transaction identity
            # ------------------------------------------------
            "transaction_id",

            # ------------------------------------------------
            # User
            # ------------------------------------------------
            "user_id",
            "kyc_verified",
            "user_created_at",

            # ------------------------------------------------
            # Account
            # ------------------------------------------------
            "account_id",
            "account_type",
            "account_currency",
            "account_created_at",

            # ------------------------------------------------
            # Device
            # ------------------------------------------------
            "device_id",
            "device_type",
            "os",
            "first_seen_at",

            # ------------------------------------------------
            # Merchant
            # ------------------------------------------------
            "merchant_id",
            "merchant_name",
            "merchant_category",

            # ------------------------------------------------
            # Date
            # ------------------------------------------------
            "date_key",
            "day_of_week",
            "month",
            "quarter",
            "year",
            "is_weekend",

            # ------------------------------------------------
            # Transaction attributes
            # ------------------------------------------------
            "type",
            "status",
            "channel",
            "currency",

            # ------------------------------------------------
            # Measures
            # ------------------------------------------------
            "amount",
            "old_balance",
            "new_balance",

            # ------------------------------------------------
            # Additional relationships
            # ------------------------------------------------
            "counterparty_account_id",

            # ------------------------------------------------
            # Temporal columns
            # ------------------------------------------------
            "timestamp",
            "ingested_at",
            "event_date",
        )
    )
# ============================================================
# Offline Feature Table
# ============================================================

def build_feat_user_90d(
    fact_transactions_df: DataFrame,
    dim_user_df: DataFrame,
) -> DataFrame:
    """
    Build offline user features over the latest 90-day window.

    Reference time:
        max(timestamp) in the dataset.

    Grain:
        1 row / user.
    """

    reference_timestamp = (
        fact_transactions_df

        .agg(
            F.max(
                "timestamp"
            ).alias(
                "reference_timestamp"
            )
        )

        .first()[
            "reference_timestamp"
        ]
    )

    if reference_timestamp is None:
        raise RuntimeError(
            "Cannot build feat_user_90d: "
            "fact_transactions is empty."
        )

    cutoff_timestamp = (
        reference_timestamp
        - timedelta(days=90)
    )

    logger.info(
        "[feat_user_90d] Feature window: %s -> %s",
        cutoff_timestamp,
        reference_timestamp,
    )

    transactions_90d = (
        fact_transactions_df

        .filter(
            (
                F.col("timestamp")
                >= F.lit(cutoff_timestamp)
            )
            &
            (
                F.col("timestamp")
                <= F.lit(reference_timestamp)
            )
        )
    )

    user_features = (
        transactions_90d

        .groupBy(
            "user_id"
        )

        .agg(
            # Total number of transactions
            F.count(
                F.lit(1)
            ).alias(
                "f_user_total_transactions_90d"
            ),

            # Average transaction amount
            F.avg(
                "amount"
            ).alias(
                "f_user_avg_transaction_amount_90d"
            ),

            # Failed transaction rate
            (
                F.sum(
                    F.when(
                        F.col("status") == "failed",
                        1,
                    ).otherwise(0)
                )
                /
                F.count(
                    F.lit(1)
                )
            ).alias(
                "f_user_failed_transaction_rate_90d"
            ),

            # Number of distinct merchants
            F.countDistinct(
                "merchant_id"
            ).alias(
                "f_user_distinct_merchants_90d"
            ),
        )
    )

    # Start from dim_user so every user remains
    # in the feature table.
    return (
        dim_user_df

        .select(
            "user_id"
        )

        .join(
            user_features,
            on="user_id",
            how="left",
        )

        .fillna(
            {
                "f_user_total_transactions_90d": 0,
                "f_user_avg_transaction_amount_90d": 0.0,
                "f_user_failed_transaction_rate_90d": 0.0,
                "f_user_distinct_merchants_90d": 0,
            }
        )

        .withColumn(
            "event_timestamp",
            F.lit(
                reference_timestamp
            ).cast("timestamp"),
        )

        .withColumn(
            "created_timestamp",
            F.current_timestamp(),
        )
    )


# ============================================================
# Analytical Table
# ============================================================

def build_opt_merchant_performance(
    fact_transactions_df: DataFrame,
    dim_merchant_df: DataFrame,
) -> DataFrame:
    """
    Merchant analytical table.

    Grain:
        1 row / merchant.

    This workload is also useful later for the
    merchant-skew Spark benchmark.
    """

    merchant_metrics = (
        fact_transactions_df

        .filter(
            F.col("merchant_id").isNotNull()
        )

        .groupBy(
            "merchant_id"
        )

        .agg(
            F.count(
                F.lit(1)
            ).alias(
                "transaction_count"
            ),

            F.sum(
                "amount"
            ).alias(
                "total_amount"
            ),

            F.avg(
                "amount"
            ).alias(
                "avg_amount"
            ),

            F.sum(
                F.when(
                    F.col("status") == "success",
                    1,
                ).otherwise(0)
            ).alias(
                "success_count"
            ),

            F.sum(
                F.when(
                    F.col("status") == "failed",
                    1,
                ).otherwise(0)
            ).alias(
                "failed_count"
            ),

            F.countDistinct(
                "user_id"
            ).alias(
                "distinct_users"
            ),
        )
    )

    return (
        dim_merchant_df

        .join(
            merchant_metrics,
            on="merchant_id",
            how="left",
        )

        .fillna(
            {
                "transaction_count": 0,
                "total_amount": 0.0,
                "avg_amount": 0.0,
                "success_count": 0,
                "failed_count": 0,
                "distinct_users": 0,
            }
        )

        .select(
            "merchant_id",
            "merchant_name",
            "category",
            "transaction_count",
            "total_amount",
            "avg_amount",
            "success_count",
            "failed_count",
            "distinct_users",
        )
    )


# ============================================================
# Write + Runtime Helper
# ============================================================

def process_gold_table(
    table_name: str,
    dataframe: DataFrame,
    partition_columns: list[str] | None = None,
) -> None:
    """
    Write one Gold table and measure its write runtime.
    """

    started_at = time.perf_counter()

    write_gold(
        df=dataframe,
        table_name=table_name,
        partition_columns=partition_columns,
    )

    elapsed = (
        time.perf_counter()
        - started_at
    )

    logger.info(
        "[%s] Runtime: %.2f seconds",
        table_name,
        elapsed,
    )


# ============================================================
# DP3 Pipeline
# ============================================================

def run_gold_pipeline(
    optimized: bool = True,
) -> None:

    spark = None

    try:
        spark = create_spark_session(
            app_name="EWallet-DP3-Gold",
            optimized=optimized,
        )

        logger.info(
            "========================================="
        )
        logger.info(
            "DP3 SILVER -> GOLD"
        )
        logger.info(
            "Optimized mode: %s",
            optimized,
        )
        logger.info(
            "========================================="
        )

        pipeline_started_at = (
            time.perf_counter()
        )

        # ====================================================
        # 1. Read all Silver tables
        # ====================================================

        logger.info(
            "========== READ SILVER =========="
        )

        users_df = read_silver(
            spark,
            "users",
        )

        accounts_df = read_silver(
            spark,
            "accounts",
        )

        merchants_df = read_silver(
            spark,
            "merchants",
        )

        devices_df = read_silver(
            spark,
            "devices",
        )

        transactions_df = read_silver(
            spark,
            "transactions",
        )

        balance_snapshots_df = read_silver(
            spark,
            "balance_snapshots",
        )

        login_events_df = read_silver(
            spark,
            "login_events",
        )

        # ====================================================
        # 2. Dimensions
        # ====================================================

        logger.info(
            "========== BUILD DIMENSIONS =========="
        )

        dim_user_df = build_dim_user(
            users_df
        )

        dim_account_df = build_dim_account(
            accounts_df
        )

        dim_merchant_df = build_dim_merchant(
            merchants_df
        )

        dim_device_df = build_dim_device(
            devices_df
        )

        dim_date_df = build_dim_date(
            transactions_df,
            login_events_df,
            balance_snapshots_df,
        )

        process_gold_table(
            "dim_user",
            dim_user_df,
        )

        process_gold_table(
            "dim_account",
            dim_account_df,
        )

        process_gold_table(
            "dim_merchant",
            dim_merchant_df,
        )

        process_gold_table(
            "dim_device",
            dim_device_df,
        )

        process_gold_table(
            "dim_date",
            dim_date_df,
        )

        # ====================================================
        # 3. Facts
        # ====================================================

        logger.info(
            "========== BUILD FACTS =========="
        )

        fact_transactions_df = (
            build_fact_transactions(
                transactions_df
            )
        )

        fact_login_events_df = (
            build_fact_login_events(
                login_events_df
            )
        )

        fact_balance_snapshot_df = (
            build_fact_balance_snapshot(
                balance_snapshots_df
            )
        )

        process_gold_table(
            table_name="fact_transactions",
            dataframe=fact_transactions_df,
            partition_columns=[
                "event_date"
            ],
        )

        process_gold_table(
            table_name="fact_login_events",
            dataframe=fact_login_events_df,
            partition_columns=[
                "event_date"
            ],
        )

        process_gold_table(
            table_name="fact_balance_snapshot",
            dataframe=fact_balance_snapshot_df,
            partition_columns=[
                "snapshot_date"
            ],
        )
        # ====================================================
        # 4. Build OBT
        # ====================================================

        logger.info(
            "========== BUILD OBT =========="
        )

        obt_transaction_enriched_df = (
            build_obt_transaction_enriched(
                fact_transactions_df,
                dim_user_df,
                dim_account_df,
                dim_device_df,
                dim_merchant_df,
                dim_date_df,
            )
        )

        process_gold_table(
            table_name="obt_transaction_enriched",
            dataframe=obt_transaction_enriched_df,
            partition_columns=[
                "event_date"
            ],
        )
        # ====================================================
        # 5. Offline Features
        # ====================================================

        logger.info(
            "========== BUILD FEATURES =========="
        )

        feat_user_90d_df = (
            build_feat_user_90d(
                fact_transactions_df,
                dim_user_df,
            )
        )

        process_gold_table(
            "feat_user_90d",
            feat_user_90d_df,
        )

        # ====================================================
        # 6. Analytical Output
        # ====================================================

        logger.info(
            "========== BUILD ANALYTICS =========="
        )

        opt_merchant_performance_df = (
            build_opt_merchant_performance(
                fact_transactions_df,
                dim_merchant_df,
            )
        )

        process_gold_table(
            "opt_merchant_performance",
            opt_merchant_performance_df,
        )

        # ====================================================
        # Summary
        # ====================================================

        elapsed = (
            time.perf_counter()
            - pipeline_started_at
        )

        logger.info("")
        logger.info(
            "DP3 Silver -> Gold completed successfully."
        )

        logger.info(
            "Total DP3 runtime: %.2f seconds",
            elapsed,
        )

    except Exception:

        logger.exception(
            "DP3 Silver -> Gold failed."
        )

        raise

    finally:

        if spark is not None:
            spark.stop()

            logger.info(
                "SparkSession stopped."
            )


# ============================================================
# CLI
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="DP3 Silver -> Gold Pipeline"
    )

    parser.add_argument(
        "--mode",
        choices=[
            "baseline",
            "optimized",
        ],
        default="optimized",
        help=(
            "baseline: AQE OFF | "
            "optimized: AQE ON"
        ),
    )

    return parser.parse_args()


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":

    args = parse_args()

    optimized = (
        args.mode == "optimized"
    )

    try:
        run_gold_pipeline(
            optimized=optimized
        )

        sys.exit(0)

    except Exception:
        sys.exit(1)