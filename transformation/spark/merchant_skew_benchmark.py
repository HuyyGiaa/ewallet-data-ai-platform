"""
Merchant Skew Benchmark

Modes:
1. profile
   - Measure the real merchant distribution.

2. baseline
   - AQE OFF
   - AQE Skew Join OFF

3. optimized
   - AQE ON
   - AQE Skew Join ON

Baseline and optimized use exactly the same workload.
Only Spark optimization configuration changes.
"""

from __future__ import annotations

import argparse
import logging
import math
import statistics
import time

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from spark_session import create_spark_session

# Config
SILVER_TRANSACTIONS = "s3a://silver-zone/transactions"
GOLD_DIM_MERCHANT = "s3a://gold-zone/dim_merchant"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

# Read data
def read_delta(
    spark,
    path: str,
) -> DataFrame:

    logger.info("Reading Delta table: %s", path)

    return (
        spark.read
        .format("delta")
        .load(path)
    )

# Spark configuration
def log_spark_config(
    spark,
    mode: str,
) -> None:

    logger.info("========== SPARK CONFIG ==========")
    logger.info("Mode: %s", mode)
    logger.info("Spark version: %s", spark.version)

    logger.info(
        "AQE: %s",
        spark.conf.get("spark.sql.adaptive.enabled"),
    )

    logger.info(
        "AQE Skew Join: %s",
        spark.conf.get(
            "spark.sql.adaptive.skewJoin.enabled"
        ),
    )

    logger.info(
        "Shuffle partitions: %s",
        spark.conf.get(
            "spark.sql.shuffle.partitions"
        ),
    )

# Merchant skew profile
def profile_merchant_skew(
    transactions_df: DataFrame,
) -> None:
    """
    Measure merchant transaction distribution from real data.
    """

    logger.info("========== MERCHANT SKEW PROFILE ==========")

    merchant_rows = (
        transactions_df

        .filter(
            F.col("merchant_id").isNotNull()
        )

        .groupBy("merchant_id")

        .agg(
            F.count("*").alias(
                "transaction_count"
            )
        )

        .orderBy(
            F.desc("transaction_count")
        )

        .collect()
    )

    if not merchant_rows:
        raise RuntimeError(
            "No merchant transactions found."
        )

    counts = [
        row["transaction_count"]
        for row in merchant_rows
    ]

    total_transactions = sum(counts)
    distinct_merchants = len(counts)

    top_5_count = max(
        1,
        math.ceil(
            distinct_merchants * 0.05
        ),
    )

    top_5_transactions = sum(
        counts[:top_5_count]
    )

    top_5_share = (
        top_5_transactions
        / total_transactions
        * 100
    )

    maximum_count = max(counts)
    minimum_count = min(counts)
    average_count = statistics.mean(counts)
    median_count = statistics.median(counts)

    if median_count > 0:
        max_median_ratio = (
            maximum_count
            / median_count
        )
    else:
        max_median_ratio = float("inf")

    logger.info(
        "Merchant transactions: %s",
        f"{total_transactions:,}",
    )

    logger.info(
        "Distinct merchants: %s",
        f"{distinct_merchants:,}",
    )

    logger.info(
        "Top 5%% merchant count: %s",
        top_5_count,
    )

    logger.info(
        "Top 5%% transaction share: %.2f%%",
        top_5_share,
    )

    logger.info(
        "Maximum transactions: %s",
        f"{maximum_count:,}",
    )

    logger.info(
        "Average transactions: %.2f",
        average_count,
    )

    logger.info(
        "Median transactions: %.2f",
        median_count,
    )

    logger.info(
        "Minimum transactions: %s",
        f"{minimum_count:,}",
    )

    logger.info(
        "Max / Median ratio: %.2f",
        max_median_ratio,
    )

    logger.info("Top 10 merchants:")

    for index, row in enumerate(
        merchant_rows[:10],
        start=1,
    ):
        logger.info(
            "%2d. merchant_id=%s | transactions=%s",
            index,
            row["merchant_id"],
            f"{row['transaction_count']:,}",
        )

# Common workload
def build_merchant_workload(
    transactions_df: DataFrame,
    merchants_df: DataFrame,
) -> DataFrame:
    """
    Build the exact same workload for baseline and optimized.

    Output grain:
        1 row / merchant.
    """

    merchant_metrics = (
        transactions_df

        .filter(
            F.col("merchant_id").isNotNull()
        )

        .select(
            "merchant_id",
            "amount",
            "status",
        )

        .groupBy("merchant_id")

        .agg(
            F.count("*")
            .alias("transaction_count"),

            F.sum("amount")
            .alias("total_amount"),

            F.avg("amount")
            .alias("avg_amount"),

            F.sum(
                F.when(
                    F.col("status") == "success",
                    1,
                ).otherwise(0)
            ).alias("success_count"),

            F.sum(
                F.when(
                    F.col("status") == "failed",
                    1,
                ).otherwise(0)
            ).alias("failed_count"),
        )
    )

    # Do not add mode-specific logic here.
    # Baseline and optimized must execute the same query.
    return (
        merchants_df

        .select(
            "merchant_id",
            "merchant_name",
            "category",
        )

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
        )
    )


# Run workload
def run_merchant_workload(
    transactions_df: DataFrame,
    merchants_df: DataFrame,
    mode: str,
) -> None:

    logger.info(
        "========== %s BENCHMARK ==========",
        mode.upper(),
    )

    result_df = build_merchant_workload(
        transactions_df,
        merchants_df,
    )

    logger.info(
        "Physical plan BEFORE execution:"
    )

    result_df.explain(
        mode="formatted"
    )

    start = time.perf_counter()

    # Final output is only about 300 merchant rows,
    # so collect() is safe for this benchmark.
    result_rows = result_df.collect()

    elapsed = (
        time.perf_counter()
        - start
    )

    # With AQE enabled, the final plan may differ
    # from the initial plan after runtime statistics are known.
    logger.info(
        "Physical plan AFTER execution:"
    )

    result_df.explain(
        mode="formatted"
    )

    result_row_count = len(result_rows)

    aggregated_transactions = sum(
        row["transaction_count"] or 0
        for row in result_rows
    )

    aggregated_total_amount = sum(
        float(row["total_amount"] or 0.0)
        for row in result_rows
    )

    aggregated_success_count = sum(
        row["success_count"] or 0
        for row in result_rows
    )

    aggregated_failed_count = sum(
        row["failed_count"] or 0
        for row in result_rows
    )

    logger.info("========== BENCHMARK RESULT ==========")

    logger.info(
        "Mode: %s",
        mode,
    )

    logger.info(
        "Result rows: %s",
        f"{result_row_count:,}",
    )

    logger.info(
        "Aggregated transactions: %s",
        f"{aggregated_transactions:,}",
    )

    logger.info(
        "Aggregated total amount: %.2f",
        aggregated_total_amount,
    )

    logger.info(
        "Success transactions: %s",
        f"{aggregated_success_count:,}",
    )

    logger.info(
        "Failed transactions: %s",
        f"{aggregated_failed_count:,}",
    )

    logger.info(
        "Workload runtime: %.4f seconds",
        elapsed,
    )

# Spark session
def create_benchmark_spark(
    mode: str,
):

    optimized = (
        mode == "optimized"
    )

    spark = create_spark_session(
        app_name=f"EWallet-Merchant-Skew-{mode}",
        optimized=optimized,
    )

    # Explicitly control the variables being benchmarked.
    spark.conf.set(
        "spark.sql.adaptive.enabled",
        str(optimized).lower(),
    )

    spark.conf.set(
        "spark.sql.adaptive.skewJoin.enabled",
        str(optimized).lower(),
    )

    return spark


# Benchmark runner
def run_benchmark(
    mode: str,
    keep_ui: bool,
) -> None:

    spark = None

    try:
        spark = create_benchmark_spark(
            mode
        )

        log_spark_config(
            spark,
            mode,
        )

        transactions_df = read_delta(
            spark,
            SILVER_TRANSACTIONS,
        )

        if mode == "profile":

            profile_merchant_skew(
                transactions_df
            )

        else:

            merchants_df = read_delta(
                spark,
                GOLD_DIM_MERCHANT,
            )

            run_merchant_workload(
                transactions_df,
                merchants_df,
                mode,
            )

        if keep_ui:
            logger.info(
                "Spark UI: http://localhost:4040"
            )

            input(
                "Press ENTER after Spark UI inspection: "
            )

    except Exception:

        logger.exception(
            "Merchant skew benchmark failed."
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
            "E-Wallet merchant skew Spark benchmark."
        )
    )

    parser.add_argument(
        "--mode",
        required=True,
        choices=[
            "profile",
            "baseline",
            "optimized",
        ],
        help=(
            "profile: inspect skew | "
            "baseline: AQE OFF | "
            "optimized: AQE ON"
        ),
    )

    parser.add_argument(
        "--keep-ui",
        action="store_true",
        help=(
            "Keep Spark UI alive after execution."
        ),
    )

    return parser.parse_args()

# Entry point
if __name__ == "__main__":

    args = parse_args()

    run_benchmark(
        mode=args.mode,
        keep_ui=args.keep_ui,
    )