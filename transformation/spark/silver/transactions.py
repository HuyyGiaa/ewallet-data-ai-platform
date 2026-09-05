from __future__ import annotations

import logging

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F


VALID_TRANSACTION_TYPES = (
    "deposit",
    "withdraw",
    "transfer",
    "payment",
)

VALID_TRANSACTION_STATUS = (
    "success",
    "failed",
    "pending",
)

VALID_CHANNELS = (
    "app",
    "web",
    "atm",
    "UNKNOWN",
)

logger = logging.getLogger(__name__)


def ensure_column(df: DataFrame, column_name: str, data_type: str, default_value=None,) -> DataFrame:
    """
    Ensure that a field exists, including cases where
    an older physical schema did not contain the column.
    """

    if column_name in df.columns:
        return df

    logger.warning(
        "Missing column '%s' -> adding it with default=%s",
        column_name,
        default_value,
    )

    return df.withColumn(
        column_name,
        F.lit(default_value).cast(data_type),
    )


def clean_transactions(df: DataFrame,) -> DataFrame:
    """
    Clean and normalize transactions.

    Grain after deduplication:
        1 row / transaction_id.
    """

    df = ensure_column(
        df=df,
        column_name="channel",
        data_type="string",
        default_value=None,
    )

    df = (
        df
        .withColumn(
            "timestamp",
            F.col("timestamp").cast("timestamp"),
        )
        .withColumn(
            "ingested_at",
            F.col("ingested_at").cast("timestamp"),
        )
        .withColumn(
            "amount",
            F.col("amount").cast("double"),
        )
        .withColumn(
            "old_balance",
            F.col("old_balance").cast("double"),
        )
        .withColumn(
            "new_balance",
            F.col("new_balance").cast("double"),
        )
    )

    # Historical schema versions legitimately contain no channel.
    # Silver exposes one stable schema to downstream consumers.
    df = df.withColumn(
        "channel",
        F.when(
            F.col("channel").isNull(),
            F.lit("UNKNOWN"),
        ).otherwise(
            F.lower(
                F.trim(
                    F.col("channel")
                )
            )
        ),
    )

    df = df.filter(
        F.col("transaction_id").isNotNull()
        & F.col("account_id").isNotNull()
        & F.col("user_id").isNotNull()
        & F.col("timestamp").isNotNull()
        & F.col("ingested_at").isNotNull()
        & (F.col("amount") > 0)
        & (F.col("old_balance") >= 0)
        & (F.col("new_balance") >= 0)
    )

    # Failed transactions are valid business events.
    # Only values outside the supported domain are rejected.
    df = df.filter(
        F.col("type").isin(
            *VALID_TRANSACTION_TYPES
        )
        & F.col("status").isin(
            *VALID_TRANSACTION_STATUS
        )
        & F.col("channel").isin(
            *VALID_CHANNELS
        )
        & (F.col("currency") == "VND")
    )

    # Synthetic duplicate records share transaction_id;
    # the latest ingestion represents the retained record.
    window_spec = (
        Window
        .partitionBy("transaction_id")
        .orderBy(
            F.col("ingested_at").desc()
        )
    )

    df = (
        df
        .withColumn(
            "_row_number",
            F.row_number().over(
                window_spec
            ),
        )
        .filter(
            F.col("_row_number") == 1
        )
        .drop("_row_number")
    )

    return df.withColumn(
        "event_date",
        F.to_date(
            F.col("timestamp")
        ),
    )