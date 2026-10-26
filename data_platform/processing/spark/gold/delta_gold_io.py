"""Delta I/O boundary for the Gold transformation pipeline."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from data_platform.config import StorageConfig
from data_platform.processing.spark.common.write_layout import (
    apply_write_layout,
    resolve_write_layout,
)

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


logger = logging.getLogger(__name__)


def read_silver(
    spark,
    table_name: str,
    *,
    storage: StorageConfig,
) -> DataFrame:
    path = storage.table_uri("silver", table_name)
    logger.info("[%s] Reading Silver: %s", table_name, path)
    return spark.read.format("delta").load(path)


def write_gold(
    df: DataFrame,
    table_name: str,
    partition_columns: list[str] | None = None,
    *,
    storage: StorageConfig,
) -> dict[str, object]:
    path = storage.table_uri("gold", table_name)
    logger.info("[%s] Writing Gold: %s", table_name, path)

    stage = "dp3-features" if table_name in {
        "feat_user_behavior",
        "feat_account_behavior",
        "feat_device_behavior",
        "feat_merchant_behavior",
    } else "dp2-gold"
    layout = resolve_write_layout(
        stage=stage,
        table_name=table_name,
    )
    columns = tuple(partition_columns or ())
    planned = apply_write_layout(
        df, layout=layout, partition_columns=columns
    )
    layout_evidence = layout.evidence(columns)
    logger.info("[%s] Write layout: %s", table_name, layout_evidence)
    writer = (
        planned.write
        .format("delta")
        .mode("errorifexists" if storage.scheme == "gs" else "overwrite")
        .option("mergeSchema", "true")
        .option("maxRecordsPerFile", str(layout.max_records_per_file))
    )
    if partition_columns:
        writer = writer.partitionBy(*partition_columns)
    writer.save(path)
    logger.info("[%s] Gold write completed.", table_name)
    return layout_evidence


def process_gold_table(
    table_name: str,
    dataframe: DataFrame,
    partition_columns: list[str] | None = None,
    *,
    storage: StorageConfig,
) -> dict[str, object]:
    started_at = time.perf_counter()
    evidence = write_gold(
        df=dataframe,
        table_name=table_name,
        partition_columns=partition_columns,
        storage=storage,
    )
    logger.info(
        "[%s] Runtime: %.2f seconds",
        table_name,
        time.perf_counter() - started_at,
    )
    return evidence
