"""Delta I/O boundary for the Silver transformation pipeline."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from data_platform.config import StorageConfig
from data_platform.processing.spark.common.write_layout import (
    apply_write_layout,
    resolve_write_layout,
)

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
) -> dict[str, object]:
    path = storage.table_uri("silver", table_name)
    logger.info("[%s] Writing Silver: %s", table_name, path)

    layout = resolve_write_layout(stage="dp2-silver", table_name=table_name)
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
    logger.info("[%s] Silver write completed.", table_name)
    return layout_evidence
