from __future__ import annotations

import argparse
import logging
import sys
import time

from transformation.spark.common.spark_session import (
    create_spark_session,
)
from transformation.spark.silver.cleaners import (
    clean_accounts,
    clean_balance_snapshots,
    clean_devices,
    clean_login_events,
    clean_merchants,
    clean_users,
)
from transformation.spark.silver.io import (
    read_bronze,
    write_silver,
)
from transformation.spark.silver.quality import (
    log_transaction_quality,
    measure_table_quality,
)
from transformation.spark.silver.transactions import (
    clean_transactions,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


PIPELINES = [
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


def run_silver_pipeline(optimized: bool = True,) -> None:
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

        pipeline_started_at = (
            time.perf_counter()
        )

        for (table_name, cleaner, partition_columns,) in PIPELINES:
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

        elapsed = time.perf_counter() - pipeline_started_at
        

        logger.info("")
        logger.info(
            "DP2 Bronze -> Silver completed successfully."
        )
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
            spark.stop()
            logger.info(
                "SparkSession stopped."
            )


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


def main() -> int:
    args = parse_args()

    try:
        run_silver_pipeline(
            optimized=(
                args.mode == "optimized"
            )
        )
        return 0

    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())