"""Delta I/O boundary for the Gold transformation pipeline."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from data_platform.config import StorageConfig

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
) -> None:
    path = storage.table_uri("gold", table_name)
    logger.info("[%s] Writing Gold: %s", table_name, path)

    writer = (
        df.write
        .format("delta")
        .mode("overwrite")
        .option("mergeSchema", "true")
    )
    if partition_columns:
        writer = writer.partitionBy(*partition_columns)
    writer.save(path)
    logger.info("[%s] Gold write completed.", table_name)


def process_gold_table(
    table_name: str,
    dataframe: DataFrame,
    partition_columns: list[str] | None = None,
    *,
    storage: StorageConfig,
) -> None:
    started_at = time.perf_counter()
    write_gold(
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
