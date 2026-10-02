from __future__ import annotations

from pyspark.sql import DataFrame
from pyspark.sql import functions as F


def build_fact_transactions(transactions_df: DataFrame,) -> DataFrame:
    """
    Grain:
        1 row / transaction.
    """

    return (
        transactions_df
        .withColumn(
            "event_date",
            F.to_date(
                F.col("timestamp")
            ),
        )
        .withColumn(
            "date_key",
            F.date_format(
                F.col("event_date"),
                "yyyyMMdd",
            ).cast("int"),
        )
        .select(
            "transaction_id",
            "user_id",
            "account_id",
            "merchant_id",
            "device_id",
            "counterparty_account_id",
            "date_key",
            "type",
            "status",
            "channel",
            "currency",
            "amount",
            "old_balance",
            "new_balance",
            "timestamp",
            "ingested_at",
            "event_date",
        )
    )


def build_fact_login_events(login_events_df: DataFrame,) -> DataFrame:
    """
    Grain:
        1 row / login attempt.
    """

    return (
        login_events_df
        .withColumn(
            "event_date",
            F.to_date(
                F.col("login_ts")
            ),
        )
        .withColumn(
            "date_key",
            F.date_format(
                F.col("event_date"),
                "yyyyMMdd",
            ).cast("int"),
        )
        .select(
            "login_id",
            "user_id",
            "device_id",
            "date_key",
            "is_success",
            "login_ts",
            "event_date",
        )
    )


def build_fact_balance_snapshot(balance_snapshots_df: DataFrame,) -> DataFrame:
    """
    Grain:
        1 row / account / day.

    Logical key:
        (account_id, snapshot_date)
    """

    return (
        balance_snapshots_df
        .withColumn(
            "snapshot_date",
            F.to_date(
                F.col("snapshot_date")
            ),
        )
        .withColumn(
            "date_key",
            F.date_format(
                F.col("snapshot_date"),
                "yyyyMMdd",
            ).cast("int"),
        )
        .select(
            "account_id",
            "date_key",
            "snapshot_date",
            "closing_balance",
        )
    )