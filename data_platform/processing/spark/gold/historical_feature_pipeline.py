"""DP3 historical feature Gold pipeline with immutable cloud run roots."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import replace
from functools import partial
from pathlib import Path

from data_platform.config import (
    RuntimeConfig,
    load_runtime_config,
    load_runtime_config_file,
)
from data_platform.processing.spark.common.spark_session import create_spark_session
from data_platform.processing.spark.gold.delta_gold_io import (
    process_gold_table,
    read_silver,
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


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


HISTORICAL_FEATURE_TABLES = (
    "feat_user_behavior",
    "feat_account_behavior",
    "feat_device_behavior",
    "feat_merchant_behavior",
)


def apply_run_scoped_storage(
    config: RuntimeConfig,
    *,
    silver_root: str,
    feature_gold_root: str,
) -> RuntimeConfig:
    """Bind DP3 to immutable Silver input and feature Gold output roots."""

    if config.environment != "gcp" or config.storage.scheme != "gs":
        raise ValueError("run-scoped historical features require the gcp profile")
    if not silver_root.startswith("gs://") or not feature_gold_root.startswith("gs://"):
        raise ValueError("Silver and feature Gold roots must use gs:// URIs")
    if "/lakehouse/runs/" not in silver_root or not silver_root.endswith("/silver"):
        raise ValueError("Silver root must be a run-scoped Silver path")
    if (
        "/lakehouse/runs/" not in feature_gold_root
        or not feature_gold_root.endswith("/gold")
    ):
        raise ValueError("Feature Gold root must be a run-scoped Gold path")
    return replace(
        config,
        storage=replace(
            config.storage,
            silver_root=silver_root,
            gold_root=feature_gold_root,
        ),
    )


def feature_definitions(transactions_df, devices_df):
    return (
        (
            "feat_user_behavior",
            build_feat_user_behavior(transactions_df),
            (
                "user_id", "event_timestamp", "user_tx_count_5m",
                "user_tx_count_1h", "user_tx_count_24h",
                "user_amount_sum_1h", "user_amount_observation_count_30d",
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


def run_historical_feature_pipeline(
    optimized: bool = True,
    runtime_config: RuntimeConfig | None = None,
) -> None:
    spark = None
    config = runtime_config or load_runtime_config()
    persist = partial(process_gold_table, storage=config.storage)
    try:
        spark = create_spark_session(
            app_name="EWallet-DP3-Historical-Features",
            optimized=optimized,
            runtime_config=config,
        )
        started_at = time.perf_counter()
        transactions_df = read_silver(
            spark, "transactions", storage=config.storage
        )
        devices_df = read_silver(spark, "devices", storage=config.storage)
        for table_name, feature_df, required_columns in feature_definitions(
            transactions_df, devices_df
        ):
            validate_feature_columns(feature_df, table_name, required_columns)
            persist(table_name, feature_df)
        logger.info(
            "DP3 historical features completed in %.2f seconds",
            time.perf_counter() - started_at,
        )
    except Exception:
        logger.exception("DP3 historical features failed")
        raise
    finally:
        if spark is not None:
            spark.stop()
            logger.info("SparkSession stopped.")


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment", choices=["local", "dev", "gcp"], default="local"
    )
    parser.add_argument(
        "--mode", choices=["baseline", "optimized"], default="optimized"
    )
    parser.add_argument("--silver-root")
    parser.add_argument("--source-core-gold-root")
    parser.add_argument("--feature-gold-root")
    parser.add_argument("--dataset-version")
    parser.add_argument("--source-silver-run-id")
    parser.add_argument("--source-core-gold-run-id")
    parser.add_argument("--run-id")
    parser.add_argument("--project-id")
    parser.add_argument("--region")
    parser.add_argument("--data-bucket")
    parser.add_argument("--runtime-config-file", type=Path)
    parser.add_argument("--expected-table-count", type=int)
    args = parser.parse_args(argv)
    cloud_values = (
        args.silver_root,
        args.source_core_gold_root,
        args.feature_gold_root,
        args.dataset_version,
        args.source_silver_run_id,
        args.source_core_gold_run_id,
        args.run_id,
        args.project_id,
        args.region,
        args.data_bucket,
        args.runtime_config_file,
        args.expected_table_count,
    )
    if args.environment == "gcp" and any(value is not None for value in cloud_values):
        if any(value is None for value in cloud_values):
            parser.error("all cloud DP3 lineage and run-root arguments are required")
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
                args.runtime_config_file, args.environment, environ=environ
            )
        else:
            config = load_runtime_config(args.environment, environ=environ)
        if args.silver_root and args.feature_gold_root:
            config = apply_run_scoped_storage(
                config,
                silver_root=args.silver_root,
                feature_gold_root=args.feature_gold_root,
            )
        if (
            args.expected_table_count is not None
            and args.expected_table_count != len(HISTORICAL_FEATURE_TABLES)
        ):
            raise ValueError(
                f"expected {args.expected_table_count} feature tables, "
                f"pipeline defines {len(HISTORICAL_FEATURE_TABLES)}"
            )
        run_historical_feature_pipeline(
            optimized=args.mode == "optimized", runtime_config=config
        )
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
