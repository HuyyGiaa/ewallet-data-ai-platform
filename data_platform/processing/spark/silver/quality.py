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


def measure_table_quality(
    table_name: str, bronze_df: DataFrame, silver_df: DataFrame,
) -> TableQualityMetrics:
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


def _transaction_summary(dataframe: DataFrame) -> dict[str, int]:
    row = dataframe.agg(
        F.count("*").alias("rows"),
        F.countDistinct("transaction_id").alias("unique_transactions"),
        F.max(F.col("transaction_id").isNull().cast("int")).alias(
            "has_null_transaction_id"
        ),
        F.sum(F.when(F.col("channel").isNull(), 1).otherwise(0)).alias(
            "channel_nulls"
        ),
    ).first()
    return {
        "rows": int(row["rows"]),
        # Spark countDistinct excludes NULL, while the former
        # select(...).distinct().count() treated NULL as one distinct value.
        "unique_transactions": int(row["unique_transactions"])
        + int(row["has_null_transaction_id"] or 0),
        "channel_nulls": int(row["channel_nulls"] or 0),
    }


def validate_transaction_summaries(
    bronze: dict[str, int], silver: dict[str, int]
) -> TableQualityMetrics:
    silver_duplicate = silver["rows"] - silver["unique_transactions"]
    if silver_duplicate != 0:
        raise RuntimeError(
            "DQ failed: Silver transactions still contain duplicates."
        )
    if silver["channel_nulls"] != 0:
        raise RuntimeError(
            "DQ failed: Silver transactions still contain NULL channel."
        )
    if silver["rows"] == 0:
        raise RuntimeError("DQ failed: Silver transactions is empty.")
    return TableQualityMetrics(
        table_name="transactions",
        bronze_rows=bronze["rows"],
        silver_rows=silver["rows"],
    )


def log_transaction_quality(
    bronze_df: DataFrame, silver_df: DataFrame,
) -> TableQualityMetrics:
    """Evaluate each lineage once while preserving every transaction gate."""

    bronze = _transaction_summary(bronze_df)
    silver = _transaction_summary(silver_df)
    bronze_rows = bronze["rows"]
    bronze_unique_transactions = bronze["unique_transactions"]
    duplicate_rows = bronze_rows - bronze_unique_transactions
    bronze_channel_null = bronze["channel_nulls"]
    silver_channel_null = silver["channel_nulls"]
    silver_duplicate = silver["rows"] - silver["unique_transactions"]

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

    metrics = validate_transaction_summaries(bronze, silver)

    logger.info(
        "Transaction DQ validation: PASSED"
    )
    return metrics
