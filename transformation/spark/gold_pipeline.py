"""
DP3 - Silver -> Gold.

Gold Layer gồm:

Dimensions
----------
- dim_user
- dim_account
- dim_merchant
- dim_date

Fact
----
- fact_transactions

Feature
-------
- feat_user_90d

Analytical table
----------------
- merchant_performance

Nguyên tắc:
- Chỉ đọc dữ liệu sạch từ Silver.
- Không thực hiện data cleaning lại ở Gold.
- Gold tập trung vào dimensional modeling, aggregation
  và feature engineering.
- Tất cả dữ liệu được đọc/ghi bằng Delta Lake.
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

# Paths
SILVER_ROOT = "s3a://silver-zone"
GOLD_ROOT = "s3a://gold-zone"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)-8s | "
        "%(message)s"
    ),
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


# Common IO
def read_silver(spark, table_name: str,) -> DataFrame:
    """
    Đọc Delta table từ Silver Layer.
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


def write_gold(df: DataFrame, table_name: str, partition_columns: list[str] | None = None,) -> None:
    """
    Ghi một DataFrame xuống Gold Layer bằng Delta Lake.
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
        .option(
            "mergeSchema",
            "true",
        )
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

# Dimensions
def build_dim_user(users_df: DataFrame,) -> DataFrame:
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


def build_dim_account(accounts_df: DataFrame,) -> DataFrame:
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


def build_dim_merchant(merchants_df: DataFrame,) -> DataFrame:
    """
    Grain:
        1 row / merchant.
    """

    return merchants_df.select(
        "merchant_id",
        "merchant_name",
        "category",
    )


def build_dim_date(transactions_df: DataFrame,) -> DataFrame:
    """
    Tạo Date Dimension từ các ngày xuất hiện trong transactions.

    Grain:
        1 row / calendar date.
    """

    return (
        transactions_df
        .select(
            F.to_date(
                F.col("timestamp")
            ).alias("calendar_date")
        )
        .filter(
            F.col("calendar_date").isNotNull()
        )
        .distinct()

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

        .orderBy(
            "calendar_date"
        )
    )


# Fact
def build_fact_transactions(transactions_df: DataFrame,) -> DataFrame:
    """
    Fact table trung tâm.

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
            "transaction_id",

            # Dimension / relationship keys
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

            # Temporal fields
            "timestamp",
            "ingested_at",
            "event_date",
        )
    )

# Offline Feature Table
def build_feat_user_90d(fact_transactions_df: DataFrame, dim_user_df: DataFrame,) -> DataFrame:
    """
    Offline features theo user trong cửa sổ 90 ngày.

    Reference time:
        timestamp mới nhất trong dataset.

    Việc dùng max(timestamp) thay vì datetime.now() giúp pipeline
    reproducible khi chạy lại cùng một dataset.

    Grain:
        1 row / user.
    """

    reference_timestamp = (
        fact_transactions_df
        .agg(
            F.max("timestamp").alias(
                "reference_timestamp"
            )
        )
        .first()["reference_timestamp"]
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
            (F.col("timestamp") > F.lit(cutoff_timestamp))
            & (
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
            # Total transactions
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

            # Distinct merchants
            # Exact count for the actual feature table.
            # Exact vs approx will be benchmarked separately.
            F.countDistinct(
                "merchant_id"
            ).alias(
                "f_user_distinct_merchants_90d"
            ),
        )
    )

    # Start from dim_user so users with zero transactions
    # still exist in the feature table.
    feature_table = (
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

    return feature_table


# Merchant Analytical Table
def build_merchant_performance(fact_transactions_df: DataFrame, dim_merchant_df: DataFrame,) -> DataFrame:
    """
    Analytical table theo merchant.

    Grain:
        1 row / merchant.

    Dataset generator cố tình tạo merchant skew:
        80% payment traffic -> top 5% merchants.

    Ta KHÔNG xử lý skew thủ công tại đây.
    Workload này sẽ được dùng ở bước benchmark Spark sau.
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

# Utility
def process_gold_table(
    table_name: str,
    dataframe: DataFrame,
    partition_columns: list[str] | None = None,
) -> None:
    """
    Log runtime riêng cho từng Gold table.
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

# DP3
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

        # 1. Read Silver
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

        transactions_df = read_silver(
            spark,
            "transactions",
        )

        # 2. Build dimensions
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

        dim_date_df = build_dim_date(
            transactions_df
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
            "dim_date",
            dim_date_df,
        )

        # 3. Build fact
        logger.info(
            "========== BUILD FACT =========="
        )

        fact_transactions_df = (
            build_fact_transactions(
                transactions_df
            )
        )

        process_gold_table(
            table_name="fact_transactions",
            dataframe=fact_transactions_df,
            partition_columns=[
                "event_date"
            ],
        )

        # 4. Build offline feature table
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

        # 5. Build merchant analytical table
        logger.info(
            "========== BUILD MERCHANT ANALYTICS =========="
        )

        merchant_performance_df = (
            build_merchant_performance(
                fact_transactions_df,
                dim_merchant_df,
            )
        )

        process_gold_table(
            "merchant_performance",
            merchant_performance_df,
        )

        # Summary
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


# CLI
def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "DP3 Silver -> Gold Pipeline"
        )
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