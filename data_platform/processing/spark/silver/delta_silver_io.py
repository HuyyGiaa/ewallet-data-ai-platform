"""Delta I/O boundary for the Silver transformation pipeline."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from data_platform.config import StorageConfig

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


logger = logging.getLogger(__name__)


def read_bronze(
    spark,
    table_name: str,
    *,
    storage: StorageConfig,
) -> DataFrame:
    path = storage.table_uri("bronze", table_name)
    logger.info("[%s] Reading Bronze: %s", table_name, path)
    return spark.read.format("delta").load(path)


def write_silver(
    df: DataFrame,
    table_name: str,
    partition_columns: list[str] | None = None,
    *,
    storage: StorageConfig,
) -> None:
    path = storage.table_uri("silver", table_name)
    logger.info("[%s] Writing Silver: %s", table_name, path)

    writer = (
        df.write
        .format("delta")
        .mode("overwrite")
        .option("mergeSchema", "true")
    )
    if partition_columns:
        writer = writer.partitionBy(*partition_columns)
    writer.save(path)
    logger.info("[%s] Silver write completed.", table_name)
