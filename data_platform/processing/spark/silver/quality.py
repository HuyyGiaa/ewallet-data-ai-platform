from __future__ import annotations

import logging
from dataclasses import dataclass

from pyspark.sql import DataFrame
from pyspark.sql import functions as F


logger = logging.getLogger(__name__)


@dataclass
class TableQualityMetrics:
    table_name: str
    bronze_rows: int
    silver_rows: int

    @property
    def removed_rows(self) -> int:
        return (
            self.bronze_rows
            - self.silver_rows
        )


def measure_table_quality(table_name: str, bronze_df: DataFrame, silver_df: DataFrame,) -> TableQualityMetrics:
    bronze_rows = bronze_df.count()
    silver_rows = silver_df.count()

    metrics = TableQualityMetrics(
        table_name=table_name,
        bronze_rows=bronze_rows,
        silver_rows=silver_rows,
    )

    logger.info(
        "[%s] Bronze=%s | Silver=%s | Removed=%s",
        table_name,
        f"{metrics.bronze_rows:,}",
        f"{metrics.silver_rows:,}",
        f"{metrics.removed_rows:,}",
    )

    return metrics


def log_transaction_quality(bronze_df: DataFrame, silver_df: DataFrame,) -> None:
    bronze_rows = bronze_df.count()

    bronze_unique_transactions = (
        bronze_df
        .select("transaction_id")
        .distinct()
        .count()
    )

    duplicate_rows = (
        bronze_rows
        - bronze_unique_transactions
    )

    bronze_channel_null = (
        bronze_df
        .filter(
            F.col("channel").isNull()
        )
        .count()
    )

    silver_channel_null = (
        silver_df
        .filter(
            F.col("channel").isNull()
        )
        .count()
    )

    silver_duplicate = (
        silver_df
        .groupBy("transaction_id")
        .count()
        .filter(
            F.col("count") > 1
        )
        .count()
    )

    logger.info(
        "========== TRANSACTION QUALITY =========="
    )
    logger.info(
        "Bronze rows              : %s",
        f"{bronze_rows:,}",
    )
    logger.info(
        "Bronze unique tx         : %s",
        f"{bronze_unique_transactions:,}",
    )
    logger.info(
        "Bronze duplicate rows    : %s",
        f"{duplicate_rows:,}",
    )
    logger.info(
        "Bronze channel NULL      : %s",
        f"{bronze_channel_null:,}",
    )
    logger.info(
        "Silver channel NULL      : %s",
        f"{silver_channel_null:,}",
    )
    logger.info(
        "Silver duplicated tx IDs : %s",
        f"{silver_duplicate:,}",
    )

    if silver_duplicate != 0:
        raise RuntimeError(
            "DQ failed: Silver transactions still contain duplicates."
        )

    if silver_channel_null != 0:
        raise RuntimeError(
            "DQ failed: Silver transactions still contain NULL channel."
        )

    if silver_df.count() == 0:
        raise RuntimeError(
            "DQ failed: Silver transactions is empty."
        )

    logger.info(
        "Transaction DQ validation: PASSED"
    )