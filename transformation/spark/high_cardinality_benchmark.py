"""
High Cardinality Benchmark

Compare:
1. Exact distinct counting:
   count_distinct(transaction_id)

2. Approximate distinct counting:
   approx_count_distinct(transaction_id)

Both methods use the same input data and Spark configuration.
Only the distinct-count algorithm changes.
"""

from __future__ import annotations

import argparse
import logging
import time

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from spark_session import create_spark_session


# Config
SILVER_TRANSACTIONS = "s3a://silver-zone/transactions"
DEFAULT_RSD = 0.05


# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


# Read data
def read_transactions(spark) -> DataFrame:
    """
    Read only valid transaction_id values from Silver.
    """

    logger.info("Reading transactions from %s", SILVER_TRANSACTIONS)

    return (
        spark.read
        .format("delta")
        .load(SILVER_TRANSACTIONS)
        .select("transaction_id")
        .filter(F.col("transaction_id").isNotNull())
    )

# Queries
def build_exact_query(transactions_df: DataFrame) -> DataFrame:
    """
    Build exact COUNT DISTINCT query.
    """

    return transactions_df.agg(
        F.count_distinct("transaction_id")
        .alias("distinct_transaction_ids")
    )


def build_approx_query(
    transactions_df: DataFrame,
    rsd: float,
) -> DataFrame:
    """
    Build approximate COUNT DISTINCT query.
    """

    return transactions_df.agg(
        F.approx_count_distinct(
            "transaction_id",
            rsd,
        ).alias("approx_distinct_transaction_ids")
    )

# Exact benchmark
def run_exact(
    transactions_df: DataFrame,
) -> tuple[int, float]:

    logger.info("========== EXACT DISTINCT ==========")

    result_df = build_exact_query(transactions_df)

    logger.info("Physical plan:")
    result_df.explain(mode="formatted")

    start = time.perf_counter()
    result = result_df.first()["distinct_transaction_ids"]
    elapsed = time.perf_counter() - start

    logger.info("Exact distinct count: %s", f"{result:,}")
    logger.info("Exact runtime: %.4f seconds", elapsed)

    return result, elapsed


# Approximate benchmark
def run_approx(
    transactions_df: DataFrame,
    rsd: float,
) -> tuple[int, float]:

    logger.info("========== APPROX DISTINCT ==========")
    logger.info("RSD: %.4f", rsd)

    result_df = build_approx_query(
        transactions_df,
        rsd,
    )

    logger.info("Physical plan:")
    result_df.explain(mode="formatted")

    start = time.perf_counter()
    result = result_df.first()["approx_distinct_transaction_ids"]
    elapsed = time.perf_counter() - start

    logger.info("Approx distinct count: %s", f"{result:,}")
    logger.info("Approx runtime: %.4f seconds", elapsed)

    return result, elapsed


# Compare
def run_compare(
    transactions_df: DataFrame,
    rsd: float,
) -> None:
    """
    Compare exact and approximate results.

    Use separate exact/approx processes for final
    runtime evidence. Compare mode is mainly for
    measuring approximation error.
    """

    logger.info("========== HIGH CARDINALITY COMPARISON ==========")

    exact_count, exact_time = run_exact(transactions_df)
    approx_count, approx_time = run_approx(
        transactions_df,
        rsd,
    )

    absolute_error = abs(
        approx_count - exact_count
    )

    if exact_count == 0:
        relative_error = 0.0
    else:
        relative_error = (
            absolute_error
            / exact_count
            * 100
        )

    logger.info("========== COMPARISON RESULT ==========")
    logger.info("Exact count: %s", f"{exact_count:,}")
    logger.info("Approx count: %s", f"{approx_count:,}")
    logger.info("Absolute error: %s", f"{absolute_error:,}")
    logger.info("Relative error: %.4f%%", relative_error)

    logger.info(
        "Exact runtime (reference): %.4f seconds",
        exact_time,
    )
    logger.info(
        "Approx runtime (reference): %.4f seconds",
        approx_time,
    )

    if approx_time > 0:
        logger.info(
            "Exact / Approx runtime ratio: %.2fx",
            exact_time / approx_time,
        )

    logger.info(
        "Use separate --mode exact and --mode approx "
        "runs for final performance comparison."
    )


# Spark configuration
def log_spark_config(spark, mode: str) -> None:

    logger.info("========== SPARK CONFIG ==========")
    logger.info("Mode: %s", mode)
    logger.info("Spark version: %s", spark.version)
    logger.info(
        "AQE: %s",
        spark.conf.get("spark.sql.adaptive.enabled"),
    )
    logger.info(
        "Shuffle partitions: %s",
        spark.conf.get("spark.sql.shuffle.partitions"),
    )


# Benchmark runner
def run_benchmark(
    mode: str,
    rsd: float,
    keep_ui: bool,
) -> None:

    spark = None

    try:
        spark = create_spark_session(
            app_name=f"EWallet-High-Cardinality-{mode}",
            optimized=False,
        )

        # Keep exact and approximate experiments
        # under the same Spark configuration.
        spark.conf.set(
            "spark.sql.adaptive.enabled",
            "false",
        )

        log_spark_config(
            spark,
            mode,
        )

        transactions_df = read_transactions(
            spark
        )

        if mode == "exact":
            run_exact(transactions_df)

        elif mode == "approx":
            run_approx(
                transactions_df,
                rsd,
            )

        elif mode == "compare":
            run_compare(
                transactions_df,
                rsd,
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
            "High-cardinality benchmark failed."
        )
        raise

    finally:
        if spark is not None:
            spark.stop()
            logger.info("SparkSession stopped.")

# CLI
def parse_args():

    parser = argparse.ArgumentParser(
        description=(
            "Benchmark exact vs approximate "
            "distinct counting on transaction_id."
        )
    )

    parser.add_argument(
        "--mode",
        choices=[
            "exact",
            "approx",
            "compare",
        ],
        required=True,
    )

    parser.add_argument(
        "--rsd",
        type=float,
        default=DEFAULT_RSD,
        help="RSD for approx_count_distinct (default: 0.05)",
    )

    parser.add_argument(
        "--keep-ui",
        action="store_true",
        help="Keep Spark UI alive after the benchmark.",
    )

    args = parser.parse_args()

    if not 0 < args.rsd < 1:
        parser.error("--rsd must be between 0 and 1.")

    return args


# Entry point
if __name__ == "__main__":

    args = parse_args()

    run_benchmark(
        mode=args.mode,
        rsd=args.rsd,
        keep_ui=args.keep_ui,
    )