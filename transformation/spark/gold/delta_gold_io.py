from __future__ import annotations

import logging
import time

from pyspark.sql import DataFrame


SILVER_ROOT = "s3a://silver-zone"
GOLD_ROOT = "s3a://gold-zone"

logger = logging.getLogger(__name__)


def read_silver(spark, table_name: str,) -> DataFrame:
    path = f"{SILVER_ROOT}/{table_name}"

    logger.info(
        "[%s] Reading Silver: %s",
        table_name,
        path,
    )

    return (
        spark.read
        .format("delta")
        .load(path)
    )


def write_gold(df: DataFrame, table_name: str, partition_columns: list[str] | None = None,) -> None:
    path = f"{GOLD_ROOT}/{table_name}"

    logger.info(
        "[%s] Writing Gold: %s",
        table_name,
        path,
    )

    writer = (
        df.write
        .format("delta")
        .mode("overwrite")
        .option("mergeSchema", "true")
    )

    if partition_columns:
        writer = writer.partitionBy(
            *partition_columns
        )

    writer.save(path)

    logger.info(
        "[%s] Gold write completed.",
        table_name,
    )


def process_gold_table(table_name: str, dataframe: DataFrame, partition_columns: list[str] | None = None,) -> None:
    started_at = time.perf_counter()

    write_gold(
        df=dataframe,
        table_name=table_name,
        partition_columns=partition_columns,
    )

    elapsed = (
        time.perf_counter()
        - started_at
    )

    logger.info(
        "[%s] Runtime: %.2f seconds",
        table_name,
        elapsed,
    )