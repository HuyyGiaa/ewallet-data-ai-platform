from __future__ import annotations

from pyspark.sql import DataFrame
from pyspark.sql import functions as F


def clean_users(df: DataFrame,) -> DataFrame:
    return (
        df
        .filter(
            F.col("user_id").isNotNull()
        )
        .dropDuplicates(
            ["user_id"]
        )
        .withColumn(
            "created_at",
            F.col("created_at").cast("timestamp"),
        )
    )


def clean_accounts(df: DataFrame,) -> DataFrame:
    return (
        df
        .filter(
            F.col("account_id").isNotNull()
            & F.col("user_id").isNotNull()
        )
        .dropDuplicates(
            ["account_id"]
        )
        .withColumn(
            "created_at",
            F.col("created_at").cast("timestamp"),
        )
    )


def clean_merchants(df: DataFrame,) -> DataFrame:
    return (
        df
        .filter(
            F.col("merchant_id").isNotNull()
        )
        .dropDuplicates(
            ["merchant_id"]
        )
    )


def clean_devices(df: DataFrame,) -> DataFrame:
    return (
        df
        .filter(
            F.col("device_id").isNotNull()
            & F.col("user_id").isNotNull()
        )
        .dropDuplicates(
            ["device_id"]
        )
        .withColumn(
            "first_seen_at",
            F.col("first_seen_at").cast("timestamp"),
        )
    )


def clean_balance_snapshots(df: DataFrame,) -> DataFrame:
    return (
        df
        .withColumn(
            "snapshot_date",
            F.col("snapshot_date").cast("date"),
        )
        .withColumn(
            "closing_balance",
            F.col("closing_balance").cast("double"),
        )
        .filter(
            F.col("account_id").isNotNull()
            & F.col("snapshot_date").isNotNull()
            & (F.col("closing_balance") >= 0)
        )
        .dropDuplicates(
            [
                "account_id",
                "snapshot_date",
            ]
        )
    )


def clean_login_events(df: DataFrame,) -> DataFrame:
    return (
        df
        .withColumn(
            "login_ts",
            F.col("login_ts").cast("timestamp"),
        )
        .filter(
            F.col("login_id").isNotNull()
            & F.col("user_id").isNotNull()
            & F.col("device_id").isNotNull()
            & F.col("login_ts").isNotNull()
        )
        .dropDuplicates(
            ["login_id"]
        )
        .withColumn(
            "login_date",
            F.to_date(
                F.col("login_ts")
            ),
        )
    )