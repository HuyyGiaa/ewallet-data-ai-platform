from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from functools import partial

from data_platform.config import (
    RuntimeConfig,
    load_runtime_config,
    load_runtime_config_file,
)
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
from data_platform.processing.spark.gold.delta_gold_io import (
    process_gold_table,
    read_silver,
)
from data_platform.processing.spark.gold.obt_builder import (
    build_obt_transaction_enriched,
)
from data_platform.storage.scripts.config import CORE_GOLD_TABLES


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


def apply_run_scoped_storage(
    config: RuntimeConfig,
    *,
    silver_root: str,
    gold_root: str,
) -> RuntimeConfig:
    """Bind core Gold execution to immutable Silver and Gold run roots."""

    if config.environment != "gcp" or config.storage.scheme != "gs":
        raise ValueError("run-scoped Gold storage requires the gcp profile")
    if not silver_root.startswith("gs://") or not gold_root.startswith("gs://"):
        raise ValueError("Silver and Gold roots must use gs:// URIs")
    if "/lakehouse/runs/" not in silver_root or not silver_root.endswith("/silver"):
        raise ValueError("Silver root must be a run-scoped Silver path")
    if "/lakehouse/runs/" not in gold_root or not gold_root.endswith("/gold"):
        raise ValueError("Gold root must be a run-scoped Gold path")
    return replace(
        config,
        storage=replace(
            config.storage,
            silver_root=silver_root,
            gold_root=gold_root,
        ),
    )


def run_gold_pipeline(
    optimized: bool = True,
    runtime_config: RuntimeConfig | None = None,
) -> None:
    spark = None
    config = runtime_config or load_runtime_config()
    persist_gold_table = partial(
        process_gold_table,
        storage=config.storage,
    )

    try:
        spark = create_spark_session(
            app_name="EWallet-DP2-Core-Gold",
            optimized=optimized,
            runtime_config=config,
        )

        logger.info(
            "========================================="
        )
        logger.info(
            "DP2 SILVER -> CORE GOLD"
        )
        logger.info(
            "Optimized mode: %s",
            optimized,
        )
        logger.info("Environment: %s", config.environment)
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
            storage=config.storage,
        )

        accounts_df = read_silver(
            spark,
            "accounts",
            storage=config.storage,
        )

        merchants_df = read_silver(
            spark,
            "merchants",
            storage=config.storage,
        )

        devices_df = read_silver(
            spark,
            "devices",
            storage=config.storage,
        )

        transactions_df = read_silver(
            spark,
            "transactions",
            storage=config.storage,
        )

        balance_snapshots_df = read_silver(
            spark,
            "balance_snapshots",
            storage=config.storage,
        )

        login_events_df = read_silver(
            spark,
            "login_events",
            storage=config.storage,
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

        persist_gold_table(
            "dim_user",
            dim_user_df,
        )

        persist_gold_table(
            "dim_account",
            dim_account_df,
        )

        persist_gold_table(
            "dim_merchant",
            dim_merchant_df,
        )

        persist_gold_table(
            "dim_device",
            dim_device_df,
        )

        persist_gold_table(
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

        persist_gold_table(
            table_name="fact_transactions",
            dataframe=fact_transactions_df,
            partition_columns=[
                "event_date"
            ],
        )

        persist_gold_table(
            table_name="fact_login_events",
            dataframe=fact_login_events_df,
            partition_columns=[
                "event_date"
            ],
        )

        persist_gold_table(
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

        persist_gold_table(
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

        persist_gold_table(
            "feat_user_90d",
            feat_user_90d_df,
        )

        logger.info(
            "========== BUILD ANALYTICS =========="
        )

        opt_merchant_performance_df = (
            build_opt_merchant_performance(
                fact_transactions_df,
                dim_merchant_df,
            )
        )

        persist_gold_table(
            "opt_merchant_performance",
            opt_merchant_performance_df,
        )

        elapsed = (
            time.perf_counter()
            - pipeline_started_at
        )

        logger.info("")
        logger.info(
            "DP2 Silver -> core Gold completed successfully."
        )
        logger.info(
            "Total core Gold runtime: %.2f seconds",
            elapsed,
        )

    except Exception:
        logger.exception(
            "DP2 Silver -> core Gold failed."
        )
        raise

    finally:
        if spark is not None:
            spark.stop()
            logger.info(
                "SparkSession stopped."
            )


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="DP2 Silver -> core Gold Pipeline"
    )

    parser.add_argument(
        "--environment",
        choices=["local", "dev", "gcp"],
        default="local",
        help="Runtime storage and Spark profile (default: local).",
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

    parser.add_argument("--silver-root")
    parser.add_argument("--gold-root")
    parser.add_argument("--dataset-version")
    parser.add_argument("--source-silver-run-id")
    parser.add_argument("--run-id")
    parser.add_argument("--project-id")
    parser.add_argument("--region")
    parser.add_argument("--data-bucket")
    parser.add_argument("--runtime-config-file", type=Path)
    parser.add_argument("--expected-table-count", type=int)

    args = parser.parse_args(argv)
    cloud_values = (
        args.silver_root,
        args.gold_root,
        args.dataset_version,
        args.source_silver_run_id,
        args.run_id,
        args.project_id,
        args.region,
        args.data_bucket,
        args.runtime_config_file,
        args.expected_table_count,
    )
    if args.environment == "gcp" and any(value is not None for value in cloud_values):
        if any(value is None for value in cloud_values):
            parser.error("all cloud core Gold lineage and run-root arguments are required")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        environ = os.environ
        if args.environment == "gcp" and args.project_id:
            environ = {
                **os.environ,
                "GCP_PROJECT_ID": args.project_id,
                "GCP_REGION": args.region,
                "EWALLET_DATA_BUCKET": args.data_bucket,
                "DATAHUB_GMS_URL": os.environ.get(
                    "DATAHUB_GMS_URL", "http://datahub-gms.invalid:8080"
                ),
            }
        if args.runtime_config_file is not None:
            config = load_runtime_config_file(
                args.runtime_config_file,
                args.environment,
                environ=environ,
            )
        else:
            config = load_runtime_config(args.environment, environ=environ)
        if args.silver_root and args.gold_root:
            config = apply_run_scoped_storage(
                config,
                silver_root=args.silver_root,
                gold_root=args.gold_root,
            )
        if (
            args.expected_table_count is not None
            and args.expected_table_count != len(CORE_GOLD_TABLES)
        ):
            raise ValueError(
                f"expected {args.expected_table_count} core Gold tables, "
                f"pipeline defines {len(CORE_GOLD_TABLES)}"
            )
        run_gold_pipeline(
            optimized=(
                args.mode == "optimized"
            ),
            runtime_config=config,
        )
        return 0

    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
