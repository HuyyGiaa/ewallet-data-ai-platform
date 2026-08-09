"""
DP2 - Bronze -> Silver.

Nhiệm vụ:
- Đọc Delta tables từ bronze-zone.
- Chuẩn hóa schema.
- Xử lý schema evolution.
- Validate dữ liệu cơ bản.
- Deduplicate transactions.
- Giữ nguyên skew và high-cardinality distribution.
- Ghi Delta tables xuống silver-zone.
- Log các Data Quality metrics quan trọng.

Không xử lý:
- Time skew.
- Channel skew.
- Merchant skew.
- High cardinality.

Các vấn đề trên không phải dirty data và sẽ được sử dụng
ở Spark benchmark / DP3.
"""

from __future__ import annotations
import time
import argparse
import logging
import sys
from dataclasses import dataclass

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F

from spark_session import create_spark_session

# Paths
BRONZE_ROOT = "s3a://bronze-zone"
SILVER_ROOT = "s3a://silver-zone"


# Domain constants
SCHEMA_CHANGE_DATE = "2026-05-01"

VALID_TRANSACTION_TYPES = (
    "deposit",
    "withdraw",
    "transfer",
    "payment",
)

VALID_TRANSACTION_STATUS = (
    "success",
    "failed",
    "pending",
)

VALID_CHANNELS = (
    "app",
    "web",
    "atm",
    "UNKNOWN",
)

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


# Quality metrics

@dataclass
class TableQualityMetrics:
    table_name: str
    bronze_rows: int
    silver_rows: int

    @property
    def removed_rows(self) -> int:
        return self.bronze_rows - self.silver_rows


# IO
def read_bronze(spark, table_name: str,) -> DataFrame:
    """
    Đọc một Delta table từ Bronze Layer.
    """

    path = f"{BRONZE_ROOT}/{table_name}"

    logger.info(
        "[%s] Reading Bronze: %s",
        table_name,
        path,
    )

    return (
        spark.read
        .format("delta")
        .load(path)
    )


def write_silver(
    df: DataFrame,
    table_name: str,
    partition_columns: list[str] | None = None,
) -> None:
    """
    Ghi DataFrame thành Delta table xuống Silver Layer.
    """

    path = f"{SILVER_ROOT}/{table_name}"

    logger.info(
        "[%s] Writing Silver: %s",
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
        "[%s] Silver write completed.",
        table_name,
    )

# Schema evolution
def ensure_column(
    df: DataFrame,
    column_name: str,
    data_type: str,
    default_value=None,
) -> DataFrame:
    """
    Đảm bảo một field tồn tại.

    Dùng cho trường hợp schema cũ hoàn toàn chưa có column,
    không chỉ trường hợp column tồn tại nhưng chứa NULL.
    """

    if column_name in df.columns:
        return df

    logger.warning(
        "Missing column '%s' -> adding it with default=%s",
        column_name,
        default_value,
    )

    return df.withColumn(
        column_name,
        F.lit(default_value).cast(data_type),
    )

# Dimension cleaning
def clean_users(
    df: DataFrame,
) -> DataFrame:

    return (
        df
        .filter(
            F.col("user_id").isNotNull()
        )
        .dropDuplicates(
            ["user_id"]
        )
        .withColumn(
            "created_at",
            F.col("created_at").cast("timestamp"),
        )
    )


def clean_accounts(
    df: DataFrame,
) -> DataFrame:

    return (
        df
        .filter(
            F.col("account_id").isNotNull()
            & F.col("user_id").isNotNull()
        )
        .dropDuplicates(
            ["account_id"]
        )
        .withColumn(
            "created_at",
            F.col("created_at").cast("timestamp"),
        )
    )


def clean_merchants(
    df: DataFrame,
) -> DataFrame:

    return (
        df
        .filter(
            F.col("merchant_id").isNotNull()
        )
        .dropDuplicates(
            ["merchant_id"]
        )
    )


def clean_devices(
    df: DataFrame,
) -> DataFrame:

    return (
        df
        .filter(
            F.col("device_id").isNotNull()
            & F.col("user_id").isNotNull()
        )
        .dropDuplicates(
            ["device_id"]
        )
        .withColumn(
            "first_seen_at",
            F.col("first_seen_at").cast("timestamp"),
        )
    )


# Transactions
def clean_transactions(
    df: DataFrame,
) -> DataFrame:
    """
    Xử lý bảng transactions.

    Các bước:
    1. Schema evolution.
    2. Explicit type casting.
    3. Normalize channel.
    4. Basic validation.
    5. Domain validation.
    6. Deduplicate theo transaction_id.
    7. Tạo event_date.
    """

    # 1. Schema evolution
    df = ensure_column(
        df=df,
        column_name="channel",
        data_type="string",
        default_value=None,
    )

    # 2. Explicit type casting
    df = (
        df
        .withColumn(
            "timestamp",
            F.col("timestamp").cast("timestamp"),
        )
        .withColumn(
            "ingested_at",
            F.col("ingested_at").cast("timestamp"),
        )
        .withColumn(
            "amount",
            F.col("amount").cast("double"),
        )
        .withColumn(
            "old_balance",
            F.col("old_balance").cast("double"),
        )
        .withColumn(
            "new_balance",
            F.col("new_balance").cast("double"),
        )
    )

    # 3. Schema evolution normalization
    #
    # channel NULL trước schema_change_date là expected.
    # Silver cần schema thống nhất cho downstream nên biểu diễn
    # missing historical value bằng UNKNOWN.
    #
    # Ta không gọi đây là dirty-data correction.
    df = df.withColumn(
        "channel",
        F.when(
            F.col("channel").isNull(),
            F.lit("UNKNOWN"),
        ).otherwise(
            F.lower(
                F.trim(
                    F.col("channel")
                )
            )
        ),
    )

    # 4. Basic structural / financial validation
    df = df.filter(
        F.col("transaction_id").isNotNull()
        & F.col("account_id").isNotNull()
        & F.col("user_id").isNotNull()
        & F.col("timestamp").isNotNull()
        & F.col("ingested_at").isNotNull()
        & (F.col("amount") > 0)
        & (F.col("old_balance") >= 0)
        & (F.col("new_balance") >= 0)
    )

    # 5. Domain validation
    #
    # failed transaction vẫn là transaction hợp lệ.
    # Chỉ reject status/type ngoài domain.
    df = df.filter(
        F.col("type").isin(
            *VALID_TRANSACTION_TYPES
        )
        & F.col("status").isin(
            *VALID_TRANSACTION_STATUS
        )
        & F.col("channel").isin(
            *VALID_CHANNELS
        )
        & (F.col("currency") == "VND")
    )

    # 6. Deduplicate
    #
    # Generator tạo duplicate có cùng transaction_id nhưng
    # ingested_at mới hơn.
    #
    # Quy tắc:
    # transaction_id giống nhau -> giữ bản ingest mới nhất.
    window_spec = (
        Window
        .partitionBy(
            "transaction_id"
        )
        .orderBy(
            F.col("ingested_at").desc()
        )
    )

    df = (
        df
        .withColumn(
            "_row_number",
            F.row_number().over(
                window_spec
            ),
        )
        .filter(
            F.col("_row_number") == 1
        )
        .drop(
            "_row_number"
        )
    )

    # 7. Derived column
    df = df.withColumn(
        "event_date",
        F.to_date(
            F.col("timestamp")
        ),
    )

    return df

# Balance snapshots
def clean_balance_snapshots(
    df: DataFrame,
) -> DataFrame:

    return (
        df
        .withColumn(
            "snapshot_date",
            F.col("snapshot_date").cast("date"),
        )
        .withColumn(
            "closing_balance",
            F.col("closing_balance").cast("double"),
        )
        .filter(
            F.col("account_id").isNotNull()
            & F.col("snapshot_date").isNotNull()
            & (F.col("closing_balance") >= 0)
        )
        .dropDuplicates(
            [
                "account_id",
                "snapshot_date",
            ]
        )
    )

# Login events
def clean_login_events(
    df: DataFrame,
) -> DataFrame:

    return (
        df
        .withColumn(
            "login_ts",
            F.col("login_ts").cast("timestamp"),
        )
        .filter(
            F.col("login_id").isNotNull()
            & F.col("user_id").isNotNull()
            & F.col("device_id").isNotNull()
            & F.col("login_ts").isNotNull()
        )
        .dropDuplicates(
            ["login_id"]
        )
        .withColumn(
            "login_date",
            F.to_date(
                F.col("login_ts")
            ),
        )
    )

# Data Quality metrics
def measure_table_quality(
    table_name: str,
    bronze_df: DataFrame,
    silver_df: DataFrame,
) -> TableQualityMetrics:

    bronze_rows = bronze_df.count()
    silver_rows = silver_df.count()

    metrics = TableQualityMetrics(
        table_name=table_name,
        bronze_rows=bronze_rows,
        silver_rows=silver_rows,
    )

    logger.info(
        "[%s] Bronze=%s | Silver=%s | Removed=%s",
        table_name,
        f"{metrics.bronze_rows:,}",
        f"{metrics.silver_rows:,}",
        f"{metrics.removed_rows:,}",
    )

    return metrics


def log_transaction_quality(
    bronze_df: DataFrame,
    silver_df: DataFrame,
) -> None:
    """
    Log các metric đặc biệt cho transactions.

    Những metric này phục vụ Quality Report và demo.
    """

    bronze_rows = bronze_df.count()

    bronze_unique_transactions = (
        bronze_df
        .select(
            "transaction_id"
        )
        .distinct()
        .count()
    )

    duplicate_rows = (
        bronze_rows
        - bronze_unique_transactions
    )

    bronze_channel_null = (
        bronze_df
        .filter(
            F.col("channel").isNull()
        )
        .count()
    )

    silver_channel_null = (
        silver_df
        .filter(
            F.col("channel").isNull()
        )
        .count()
    )

    silver_duplicate = (
        silver_df
        .groupBy(
            "transaction_id"
        )
        .count()
        .filter(
            F.col("count") > 1
        )
        .count()
    )

    logger.info(
        "========== TRANSACTION QUALITY =========="
    )

    logger.info(
        "Bronze rows              : %s",
        f"{bronze_rows:,}",
    )

    logger.info(
        "Bronze unique tx         : %s",
        f"{bronze_unique_transactions:,}",
    )

    logger.info(
        "Bronze duplicate rows    : %s",
        f"{duplicate_rows:,}",
    )

    logger.info(
        "Bronze channel NULL      : %s",
        f"{bronze_channel_null:,}",
    )

    logger.info(
        "Silver channel NULL      : %s",
        f"{silver_channel_null:,}",
    )

    logger.info(
        "Silver duplicated tx IDs : %s",
        f"{silver_duplicate:,}",
    )

    # Validation / SLA đơn giản
    if silver_duplicate != 0:
        raise RuntimeError(
            "DQ failed: Silver transactions still contain duplicates."
        )

    if silver_channel_null != 0:
        raise RuntimeError(
            "DQ failed: Silver transactions still contain NULL channel."
        )

    if silver_df.count() == 0:
        raise RuntimeError(
            "DQ failed: Silver transactions is empty."
        )

    logger.info(
        "Transaction DQ validation: PASSED"
    )


# Main pipeline
def run_silver_pipeline(
    optimized: bool = True,
) -> None:

    spark = None

    try:
        spark = create_spark_session(
            app_name="EWallet-DP2-Silver",
            optimized=optimized,
        )

        logger.info(
            "========================================="
        )
        logger.info(
            "DP2 BRONZE -> SILVER"
        )
        logger.info(
            "Optimized mode: %s",
            optimized,
        )
        logger.info(
            "========================================="
        )

        pipelines = [
            (
                "users",
                clean_users,
                None,
            ),
            (
                "accounts",
                clean_accounts,
                None,
            ),
            (
                "merchants",
                clean_merchants,
                None,
            ),
            (
                "devices",
                clean_devices,
                None,
            ),
            (
                "transactions",
                clean_transactions,
                ["event_date"],
            ),
            (
                "balance_snapshots",
                clean_balance_snapshots,
                ["snapshot_date"],
            ),
            (
                "login_events",
                clean_login_events,
                ["login_date"],
            ),
        ]
        pipeline_started_at = time.perf_counter()
        for (table_name, cleaner, partition_columns,) in pipelines:

            logger.info("")
            logger.info(
                "========== %s ==========",
                table_name,
            )

            bronze_df = read_bronze(
                spark,
                table_name,
            )

            silver_df = cleaner(
                bronze_df
            )

            measure_table_quality(
                table_name,
                bronze_df,
                silver_df,
            )

            if table_name == "transactions":
                log_transaction_quality(
                    bronze_df,
                    silver_df,
                )

            write_silver(
                df=silver_df,
                table_name=table_name,
                partition_columns=partition_columns,
            )

        logger.info("")
        logger.info(
            "DP2 Bronze -> Silver completed successfully."
        )
        elapsed = time.perf_counter() - pipeline_started_at

        logger.info(
            "Total DP2 runtime: %.2f seconds",
            elapsed,
        )

    except Exception:
        logger.exception(
            "DP2 Bronze -> Silver failed."
        )
        raise

    finally:
        if spark is not None:
            input(
                "\n[INFO] Open Spark UI at http://localhost:4040 "
                "and take screenshots.\n"
                "Press ENTER when finished..."
            )
            spark.stop()

            logger.info(
                "SparkSession stopped."
            )

# CLI
def parse_args():
    parser = argparse.ArgumentParser(
        description="DP2 Bronze -> Silver Pipeline"
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
        run_silver_pipeline(
            optimized=optimized
        )

        sys.exit(0)

    except Exception:
        sys.exit(1)