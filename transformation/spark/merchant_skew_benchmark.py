"""
Merchant Skew Benchmark

Purpose
-------
Benchmark the same merchant analytical workload under:

1. profile
    - Inspect the actual merchant distribution.
    - Prove whether merchant skew exists in the dataset.

2. baseline
    - AQE OFF
    - AQE Skew Join OFF

3. optimized
    - AQE ON
    - AQE Skew Join ON

Important
---------
Baseline and optimized modes use exactly the same:
    - input data
    - filter
    - groupBy
    - aggregations
    - join
    - action

Only Spark optimization configuration changes.

This allows a fair before/after comparison.
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


# ============================================================
# Paths
# ============================================================

SILVER_TRANSACTIONS = (
    "s3a://silver-zone/transactions"
)

GOLD_DIM_MERCHANT = (
    "s3a://gold-zone/dim_merchant"
)


# ============================================================
# Logging
# ============================================================

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


# ============================================================
# IO
# ============================================================

def read_delta(
    spark,
    path: str,
) -> DataFrame:
    """
    Read one Delta table.
    """

    logger.info(
        "Reading Delta table: %s",
        path,
    )

    return (
        spark.read
        .format("delta")
        .load(path)
    )


# ============================================================
# Spark Configuration
# ============================================================

def log_spark_config(
    spark,
    mode: str,
) -> None:
    """
    Print important Spark settings used by the benchmark.
    """

    logger.info("")
    logger.info(
        "=============== SPARK CONFIG ==============="
    )

    logger.info(
        "Mode                   : %s",
        mode,
    )

    logger.info(
        "Spark Version          : %s",
        spark.version,
    )

    logger.info(
        "AQE                    : %s",
        spark.conf.get(
            "spark.sql.adaptive.enabled"
        ),
    )

    logger.info(
        "AQE Skew Join          : %s",
        spark.conf.get(
            "spark.sql.adaptive.skewJoin.enabled"
        ),
    )

    logger.info(
        "Shuffle partitions     : %s",
        spark.conf.get(
            "spark.sql.shuffle.partitions"
        ),
    )

    logger.info(
        "============================================"
    )


# ============================================================
# Merchant Skew Profile
# ============================================================

def profile_merchant_skew(
    transactions_df: DataFrame,
) -> None:
    """
    Measure the real merchant distribution.

    The goal is to verify skew from actual generated data,
    rather than relying only on generator configuration.
    """

    logger.info("")
    logger.info(
        "Profiling merchant distribution..."
    )

    # --------------------------------------------------------
    # Keep only transactions that actually have a merchant.
    # --------------------------------------------------------

    merchant_transactions = (
        transactions_df
        .filter(
            F.col(
                "merchant_id"
            ).isNotNull()
        )
    )

    # --------------------------------------------------------
    # Total merchant-related transactions
    # --------------------------------------------------------

    total_transactions = (
        merchant_transactions.count()
    )

    # --------------------------------------------------------
    # Count transactions per merchant
    # --------------------------------------------------------

    merchant_counts_df = (
        merchant_transactions

        .groupBy(
            "merchant_id"
        )

        .agg(
            F.count(
                F.lit(1)
            ).alias(
                "transaction_count"
            )
        )

        .orderBy(
            F.desc(
                "transaction_count"
            )
        )
    )

    # Only around 300 merchants are expected,
    # so collecting this small aggregated result is safe.
    merchant_rows = (
        merchant_counts_df.collect()
    )

    if not merchant_rows:
        raise RuntimeError(
            "No merchant transactions found."
        )

    counts = [
        row["transaction_count"]
        for row in merchant_rows
    ]

    distinct_merchants = len(
        counts
    )

    # --------------------------------------------------------
    # Top 5% merchants
    # --------------------------------------------------------

    top_5_percent_count = max(
        1,
        math.ceil(
            distinct_merchants
            * 0.05
        ),
    )

    top_5_transactions = sum(
        counts[
            :top_5_percent_count
        ]
    )

    top_5_share = (
        top_5_transactions
        / total_transactions
        * 100
    )

    # --------------------------------------------------------
    # Distribution statistics
    # --------------------------------------------------------

    maximum_count = max(
        counts
    )

    minimum_count = min(
        counts
    )

    average_count = (
        sum(counts)
        / len(counts)
    )

    median_count = (
        statistics.median(
            counts
        )
    )

    if median_count > 0:
        max_to_median_ratio = (
            maximum_count
            / median_count
        )
    else:
        max_to_median_ratio = (
            float("inf")
        )

    # --------------------------------------------------------
    # Report
    # --------------------------------------------------------

    logger.info("")
    logger.info(
        "=============== MERCHANT SKEW PROFILE ==============="
    )

    logger.info(
        "Merchant transactions       : %s",
        f"{total_transactions:,}",
    )

    logger.info(
        "Distinct merchants          : %s",
        f"{distinct_merchants:,}",
    )

    logger.info(
        "Top 5%% merchant count       : %s",
        top_5_percent_count,
    )

    logger.info(
        "Top 5%% transaction share    : %.2f%%",
        top_5_share,
    )

    logger.info(
        "Maximum transactions        : %s",
        f"{maximum_count:,}",
    )

    logger.info(
        "Average transactions        : %.2f",
        average_count,
    )

    logger.info(
        "Median transactions         : %.2f",
        median_count,
    )

    logger.info(
        "Minimum transactions        : %s",
        f"{minimum_count:,}",
    )

    logger.info(
        "Max / Median ratio          : %.2f",
        max_to_median_ratio,
    )

    logger.info("")
    logger.info(
        "Top 10 merchants:"
    )

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

    logger.info(
        "======================================================="
    )


# ============================================================
# Common Merchant Workload
# ============================================================

def build_merchant_workload(
    transactions_df: DataFrame,
    merchants_df: DataFrame,
) -> DataFrame:
    """
    Build the workload used by BOTH baseline and optimized mode.

    Grain of output:
        1 row / merchant.

    Workload:
        transactions
            -> filter merchant transactions
            -> groupBy merchant_id
            -> aggregate
            -> join dim_merchant

    IMPORTANT:
        Do not put different optimization logic here for
        baseline and optimized modes.

        Both modes must execute the same transformations.
    """

    # --------------------------------------------------------
    # Keep columns required by merchant aggregation
    # --------------------------------------------------------

    merchant_transactions = (
        transactions_df

        .filter(
            F.col(
                "merchant_id"
            ).isNotNull()
        )

        .select(
            "merchant_id",
            "amount",
            "status",
        )
    )

    # --------------------------------------------------------
    # Merchant aggregation
    # --------------------------------------------------------

    merchant_metrics = (
        merchant_transactions

        .groupBy(
            "merchant_id"
        )

        .agg(
            # Number of transactions
            F.count(
                F.lit(1)
            ).alias(
                "transaction_count"
            ),

            # Total transaction amount
            F.sum(
                "amount"
            ).alias(
                "total_amount"
            ),

            # Average transaction amount
            F.avg(
                "amount"
            ).alias(
                "avg_amount"
            ),

            # Successful transactions
            F.sum(
                F.when(
                    F.col(
                        "status"
                    ) == "success",
                    1,
                ).otherwise(0)
            ).alias(
                "success_count"
            ),

            # Failed transactions
            F.sum(
                F.when(
                    F.col(
                        "status"
                    ) == "failed",
                    1,
                ).otherwise(0)
            ).alias(
                "failed_count"
            ),
        )
    )

    # --------------------------------------------------------
    # Enrich aggregation with merchant attributes.
    #
    # Keep this join identical between baseline and optimized.
    # Spark itself is allowed to choose/adapt its plan.
    # --------------------------------------------------------

    result_df = (
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

    return result_df


# ============================================================
# Benchmark Workload
# ============================================================

def run_merchant_workload(
    transactions_df: DataFrame,
    merchants_df: DataFrame,
    mode: str,
) -> None:
    """
    Execute the common merchant workload and measure runtime.
    """

    logger.info("")
    logger.info(
        "=============== %s BENCHMARK ===============",
        mode.upper(),
    )

    result_df = (
        build_merchant_workload(
            transactions_df,
            merchants_df,
        )
    )

    # --------------------------------------------------------
    # Initial physical plan
    # --------------------------------------------------------

    logger.info("")
    logger.info(
        "Physical plan BEFORE execution:"
    )

    result_df.explain(
        mode="formatted"
    )

    # --------------------------------------------------------
    # Execute workload
    #
    # Spark transformations are lazy.
    # collect() forces the complete query to run.
    #
    # The final output contains only ~300 merchant rows,
    # so collecting the final result is safe here.
    # --------------------------------------------------------

    started_at = (
        time.perf_counter()
    )

    result_rows = (
        result_df.collect()
    )

    elapsed = (
        time.perf_counter()
        - started_at
    )

    # --------------------------------------------------------
    # Plan after execution.
    #
    # Useful when AQE is enabled because Spark may adapt
    # parts of the execution plan using runtime statistics.
    # --------------------------------------------------------

    logger.info("")
    logger.info(
        "Physical plan AFTER execution:"
    )

    result_df.explain(
        mode="formatted"
    )

    # --------------------------------------------------------
    # Correctness checksums
    #
    # Baseline and optimized must return the same results.
    # --------------------------------------------------------

    result_row_count = len(
        result_rows
    )

    aggregated_transactions = sum(
        (
            row["transaction_count"]
            or 0
        )
        for row in result_rows
    )

    aggregated_total_amount = sum(
        float(
            row["total_amount"]
            or 0.0
        )
        for row in result_rows
    )

    aggregated_success_count = sum(
        (
            row["success_count"]
            or 0
        )
        for row in result_rows
    )

    aggregated_failed_count = sum(
        (
            row["failed_count"]
            or 0
        )
        for row in result_rows
    )

    # --------------------------------------------------------
    # Report
    # --------------------------------------------------------

    logger.info("")
    logger.info(
        "================ BENCHMARK RESULT ================"
    )

    logger.info(
        "Mode                       : %s",
        mode,
    )

    logger.info(
        "Result rows                : %s",
        f"{result_row_count:,}",
    )

    logger.info(
        "Aggregated transactions    : %s",
        f"{aggregated_transactions:,}",
    )

    logger.info(
        "Aggregated total amount    : %.2f",
        aggregated_total_amount,
    )

    logger.info(
        "Success transactions       : %s",
        f"{aggregated_success_count:,}",
    )

    logger.info(
        "Failed transactions        : %s",
        f"{aggregated_failed_count:,}",
    )

    logger.info(
        "Workload runtime           : %.4f seconds",
        elapsed,
    )

    logger.info(
        "=================================================="
    )


# ============================================================
# Spark Session
# ============================================================

def create_benchmark_spark(
    mode: str,
):
    """
    Create SparkSession for the selected benchmark mode.

    profile / baseline:
        AQE = false
        Skew Join = false

    optimized:
        AQE = true
        Skew Join = true
    """

    optimized = (
        mode == "optimized"
    )

    spark = create_spark_session(
        app_name=(
            f"EWallet-Merchant-Skew-{mode}"
        ),
        optimized=optimized,
    )

    # Explicit settings make benchmark evidence clear
    # even if spark_session.py defaults change later.

    spark.conf.set(
        "spark.sql.adaptive.enabled",
        str(
            optimized
        ).lower(),
    )

    spark.conf.set(
        "spark.sql.adaptive.skewJoin.enabled",
        str(
            optimized
        ).lower(),
    )

    return spark


# ============================================================
# Main Benchmark
# ============================================================

def run_benchmark(
    mode: str,
    keep_ui: bool,
) -> None:

    spark = None

    try:

        # ====================================================
        # Create SparkSession
        # ====================================================

        spark = (
            create_benchmark_spark(
                mode
            )
        )

        log_spark_config(
            spark,
            mode,
        )

        # ====================================================
        # Read input
        # ====================================================

        transactions_df = (
            read_delta(
                spark,
                SILVER_TRANSACTIONS,
            )
        )

        merchants_df = (
            read_delta(
                spark,
                GOLD_DIM_MERCHANT,
            )
        )

        # ====================================================
        # Execute selected mode
        # ====================================================

        if mode == "profile":

            profile_merchant_skew(
                transactions_df
            )

        elif mode in (
            "baseline",
            "optimized",
        ):

            run_merchant_workload(
                transactions_df,
                merchants_df,
                mode,
            )

        else:

            raise ValueError(
                f"Unsupported mode: {mode}"
            )

        # ====================================================
        # Keep Spark UI alive if requested
        # ====================================================

        if keep_ui:

            logger.info("")
            logger.info(
                "Spark UI is still running."
            )

            logger.info(
                "Open: http://localhost:4040"
            )

            logger.info(
                "Inspect Jobs / Stages / SQL / Environment."
            )

            input(
                "\nPress ENTER after finishing Spark UI inspection: "
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


# ============================================================
# CLI
# ============================================================

def parse_args():

    parser = argparse.ArgumentParser(
        description=(
            "E-Wallet merchant skew "
            "Spark benchmark"
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
            "profile: inspect merchant skew | "
            "baseline: AQE OFF | "
            "optimized: AQE ON"
        ),
    )

    parser.add_argument(
        "--keep-ui",
        action="store_true",
        help=(
            "Keep the Spark application alive "
            "after execution so Spark UI can be inspected."
        ),
    )

    return parser.parse_args()


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":

    args = parse_args()

    run_benchmark(
        mode=args.mode,
        keep_ui=args.keep_ui,
    )