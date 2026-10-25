from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from data_platform.config import (
    RuntimeConfig,
    load_runtime_config,
    load_runtime_config_file,
)
from data_platform.config.spark_profiles import validate_git_sha
from data_platform.processing.spark.common.spark_session import (
    create_spark_session,
)
from data_platform.processing.spark.silver.cleaners import (
    clean_accounts,
    clean_balance_snapshots,
    clean_devices,
    clean_fraud_labels,
    clean_login_events,
    clean_merchants,
    clean_users,
)
from data_platform.processing.spark.silver.delta_silver_io import (
    read_bronze,
    write_silver,
)
from data_platform.processing.spark.silver.quality import (
    log_transaction_quality,
    measure_table_quality,
)
from data_platform.processing.spark.silver.transactions import (
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


def selected_pipelines(include_fraud_labels: bool = False):
    if include_fraud_labels:
        return PIPELINES + [("fraud_labels", clean_fraud_labels, None)]
    return list(PIPELINES)


def apply_run_scoped_storage(
    config: RuntimeConfig,
    *,
    bronze_root: str,
    silver_root: str,
) -> RuntimeConfig:
    """Bind a GCP Silver execution to immutable, run-scoped roots."""

    if config.environment != "gcp" or config.storage.scheme != "gs":
        raise ValueError("run-scoped Silver storage requires the gcp profile")
    if not bronze_root.startswith("gs://") or not silver_root.startswith("gs://"):
        raise ValueError("Bronze and Silver roots must use gs:// URIs")
    if "/lakehouse/runs/" not in bronze_root or not bronze_root.endswith("/bronze"):
        raise ValueError("Bronze root must be a run-scoped Bronze path")
    if "/lakehouse/runs/" not in silver_root or not silver_root.endswith("/silver"):
        raise ValueError("Silver root must be a run-scoped Silver path")

    storage = replace(
        config.storage,
        bronze_root=bronze_root,
        silver_root=silver_root,
    )
    return replace(config, storage=storage)


def run_silver_pipeline(
    optimized: bool = True,
    include_fraud_labels: bool = False,
    runtime_config: RuntimeConfig | None = None,
) -> None:
    spark = None
    config = runtime_config or load_runtime_config()

    try:
        spark = create_spark_session(
            app_name="EWallet-DP2-Silver",
            optimized=optimized,
            runtime_config=config,
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
            "Fraud labels enabled: %s",
            include_fraud_labels,
        )
        logger.info("Environment: %s", config.environment)
        logger.info(
            "========================================="
        )

        pipeline_started_at = (
            time.perf_counter()
        )

        for (table_name, cleaner, partition_columns,) in selected_pipelines(
            include_fraud_labels
        ):
            logger.info("")
            logger.info(
                "========== %s ==========",
                table_name,
            )

            bronze_df = read_bronze(
                spark,
                table_name,
                storage=config.storage,
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
                storage=config.storage,
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


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="DP2 Bronze -> Silver Pipeline"
    )

    parser.add_argument(
        "--environment",
        choices=["local", "dev", "gcp"],
        default="local",
        help="Runtime storage and Spark profile (default: local).",
    )

    parser.add_argument(
        "--include-fraud-labels",
        action="store_true",
        help="Require Bronze fraud_labels and write Silver fraud_labels.",
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

    parser.add_argument("--bronze-root")
    parser.add_argument("--silver-root")
    parser.add_argument("--dataset-version")
    parser.add_argument("--source-bronze-run-id")
    parser.add_argument("--run-id")
    parser.add_argument("--project-id")
    parser.add_argument("--region")
    parser.add_argument("--data-bucket")
    parser.add_argument("--runtime-config-file", type=Path)
    parser.add_argument("--expected-table-count", type=int)
    parser.add_argument("--benchmark-id")
    parser.add_argument("--spark-profile", choices=("baseline", "optimized"))
    parser.add_argument("--git-sha")

    args = parser.parse_args(argv)
    if len(
        [value for value in (args.benchmark_id, args.spark_profile, args.git_sha) if value]
    ) not in (0, 3):
        parser.error("benchmark-id, spark-profile and git-sha must be supplied together")
    if args.git_sha:
        validate_git_sha(args.git_sha)
    if args.spark_profile is not None and args.spark_profile != args.mode:
        parser.error("spark-profile must equal mode")
    cloud_values = (
        args.bronze_root,
        args.silver_root,
        args.dataset_version,
        args.source_bronze_run_id,
        args.run_id,
        args.project_id,
        args.region,
        args.data_bucket,
        args.runtime_config_file,
        args.expected_table_count,
    )
    if args.environment == "gcp" and any(value is not None for value in cloud_values):
        if any(value is None for value in cloud_values):
            parser.error("all cloud Silver lineage and run-root arguments are required")
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
                    "DATAHUB_GMS_URL",
                    "http://datahub-gms.invalid:8080",
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
        if args.bronze_root and args.silver_root:
            config = apply_run_scoped_storage(
                config,
                bronze_root=args.bronze_root,
                silver_root=args.silver_root,
            )
            actual_table_count = len(
                selected_pipelines(args.include_fraud_labels)
            )
            if args.expected_table_count != actual_table_count:
                raise ValueError(
                    "Silver table selection does not match expected table count: "
                    f"{actual_table_count} != {args.expected_table_count}"
                )
            logger.info("Dataset version: %s", args.dataset_version)
            logger.info("Source Bronze run: %s", args.source_bronze_run_id)
            logger.info("Silver run: %s", args.run_id)
            logger.info("Benchmark ID: %s", args.benchmark_id)
            logger.info("Spark profile: %s", args.spark_profile or args.mode)
            logger.info("Git SHA: %s", args.git_sha)
            logger.info("Bronze input root: %s", args.bronze_root)
            logger.info("Silver output root: %s", args.silver_root)
        run_silver_pipeline(
            optimized=(
                args.mode == "optimized"
            ),
            include_fraud_labels=args.include_fraud_labels,
            runtime_config=config,
        )
        return 0

    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
