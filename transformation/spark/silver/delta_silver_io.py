from __future__ import annotations
import logging
from pyspark.sql import DataFrame


BRONZE_ROOT = "s3a://bronze-zone"
SILVER_ROOT = "s3a://silver-zone"

logger = logging.getLogger(__name__)


def read_bronze(spark, table_name: str,) -> DataFrame:
    path = f"{BRONZE_ROOT}/{table_name}"

    logger.info(
        "[%s] Reading Bronze: %s",
        table_name,
        path,
    )

    return (
        spark.read
        .format("delta")
        .load(path)
    )


def write_silver(df: DataFrame, table_name: str, partition_columns: list[str] | None = None,) -> None:
    path = f"{SILVER_ROOT}/{table_name}"

    logger.info(
        "[%s] Writing Silver: %s",
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
        "[%s] Silver write completed.",
        table_name,
    )