from __future__ import annotations

import argparse
import logging
import sys
import time

from data_platform.processing.spark.common.spark_session import (
    create_spark_session,
)
from data_platform.processing.spark.gold.analytics_builder import (
    build_opt_merchant_performance,
)
from data_platform.processing.spark.gold.dimension_builder import (
    build_dim_account,
    build_dim_date,
    build_dim_device,
    build_dim_merchant,
    build_dim_user,
)
from data_platform.processing.spark.gold.fact_builder import (
    build_fact_balance_snapshot,
    build_fact_login_events,
    build_fact_transactions,
)
from data_platform.processing.spark.gold.feature_builder import (
    build_feat_user_90d,
)
from data_platform.processing.spark.gold.features import (
    build_feat_account_behavior,
    build_feat_device_behavior,
    build_feat_merchant_behavior,
    build_feat_user_behavior,
)
from data_platform.processing.spark.gold.features.validation import (
    validate_feature_columns,
)
from data_platform.processing.spark.gold.delta_gold_io import (
    process_gold_table,
    read_silver,
)
from data_platform.processing.spark.gold.obt_builder import (
    build_obt_transaction_enriched,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


def run_gold_pipeline(optimized: bool = True,) -> None:
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

        historical_features = (
            (
                "feat_user_behavior",
                build_feat_user_behavior(transactions_df),
                (
                    "user_id", "event_timestamp", "user_tx_count_5m",
                    "user_tx_count_1h", "user_tx_count_24h",
                    "user_amount_sum_1h",
                    "user_amount_observation_count_30d",
                    "user_avg_amount_30d", "user_std_amount_30d",
                    "user_failed_rate_24h", "user_distinct_merchants_24h",
                ),
            ),
            (
                "feat_account_behavior",
                build_feat_account_behavior(transactions_df),
                (
                    "account_id", "event_timestamp", "account_tx_count_5m",
                    "account_tx_count_1h", "account_tx_count_24h",
                    "account_amount_sum_1h", "account_amount_sum_24h",
                    "account_amount_observation_count_30d",
                    "account_avg_amount_30d", "account_std_amount_30d",
                    "account_failed_rate_24h", "account_seconds_since_last_tx",
                ),
            ),
            (
                "feat_device_behavior",
                build_feat_device_behavior(transactions_df, devices_df),
                (
                    "device_id", "event_timestamp", "device_age_seconds",
                    "device_tx_count_1h", "device_tx_count_24h",
                    "device_amount_sum_24h", "device_failed_rate_24h",
                ),
            ),
            (
                "feat_merchant_behavior",
                build_feat_merchant_behavior(transactions_df),
                (
                    "merchant_id", "event_timestamp", "merchant_tx_count_10m",
                    "merchant_tx_count_1h", "merchant_tx_count_24h",
                    "merchant_unique_users_10m", "merchant_unique_users_1h",
                    "merchant_amount_sum_1h", "merchant_avg_amount_24h",
                ),
            ),
        )

        for table_name, feature_df, required_columns in historical_features:
            validate_feature_columns(feature_df, table_name, required_columns)
            process_gold_table(table_name, feature_df)

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


def main() -> int:
    args = parse_args()

    try:
        run_gold_pipeline(
            optimized=(
                args.mode == "optimized"
            )
        )
        return 0

    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
