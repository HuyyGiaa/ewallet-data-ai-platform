from __future__ import annotations

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from data_platform.processing.spark.gold.scd2 import build_scd2_from_snapshots


MERCHANT_SCD2_ATTRIBUTES = ("merchant_name", "category")


def build_dim_user(users_df: DataFrame,) -> DataFrame:
    """
    Grain:
        1 row / user.
    """
    return users_df.select(
        "user_id",
        "full_name",
        "email",
        "phone",
        "kyc_verified",
        "created_at",
    )


def build_dim_account(accounts_df: DataFrame,) -> DataFrame:
    """
    Grain:
        1 row / account.
    """
    return accounts_df.select(
        "account_id",
        "user_id",
        "account_type",
        "currency",
        "created_at",
    )


def build_dim_merchant(merchants_df: DataFrame,) -> DataFrame:
    """
    Grain:
        1 row / merchant.
    """
    return merchants_df.select(
        "merchant_id",
        "merchant_name",
        "category",
    )


def build_dim_merchant_scd2(merchant_history_df: DataFrame) -> DataFrame:
    """
    Grain:
        1 row / merchant version, keyed by (merchant_id, valid_from_ts).

    Built from the persisted Silver ``merchant_history`` snapshots, never from
    the current-state ``merchants`` table. ``dim_merchant`` stays one row per
    merchant, so existing fact, OBT, feature and analytics joins are unchanged.
    ``merchant_sk`` is a deterministic surrogate key for one version.
    """
    return build_scd2_from_snapshots(
        merchant_history_df,
        business_key="merchant_id",
        attribute_columns=MERCHANT_SCD2_ATTRIBUTES,
        effective_ts_column="snapshot_ts",
        surrogate_key_column="merchant_sk",
    )


def build_dim_device(devices_df: DataFrame,) -> DataFrame:
    """
    Grain:
        1 row / device.
    """
    return devices_df.select(
        "device_id",
        "user_id",
        "device_type",
        "os",
        "first_seen_at",
    )


def build_dim_date(transactions_df: DataFrame, login_events_df: DataFrame, balance_snapshots_df: DataFrame,) -> DataFrame:
    """
    Grain:
        1 row / calendar date.
    """

    transaction_dates = (
        transactions_df
        .select(
            F.to_date(
                F.col("timestamp")
            ).alias("calendar_date")
        )
    )

    login_dates = (
        login_events_df
        .select(
            F.to_date(
                F.col("login_ts")
            ).alias("calendar_date")
        )
    )

    snapshot_dates = (
        balance_snapshots_df
        .select(
            F.to_date(
                F.col("snapshot_date")
            ).alias("calendar_date")
        )
    )

    all_dates = (
        transaction_dates
        .unionByName(login_dates)
        .unionByName(snapshot_dates)
        .filter(
            F.col("calendar_date").isNotNull()
        )
        .distinct()
    )

    return (
        all_dates
        .withColumn(
            "date_key",
            F.date_format(
                F.col("calendar_date"),
                "yyyyMMdd",
            ).cast("int"),
        )
        .withColumn(
            "day",
            F.dayofmonth(
                F.col("calendar_date")
            ),
        )
        .withColumn(
            "month",
            F.month(
                F.col("calendar_date")
            ),
        )
        .withColumn(
            "quarter",
            F.quarter(
                F.col("calendar_date")
            ),
        )
        .withColumn(
            "year",
            F.year(
                F.col("calendar_date")
            ),
        )
        .withColumn(
            "day_of_week",
            F.date_format(
                F.col("calendar_date"),
                "EEEE",
            ),
        )
        .withColumn(
            "is_weekend",
            F.dayofweek(
                F.col("calendar_date")
            ).isin(1, 7),
        )
        .select(
            "date_key",
            "calendar_date",
            "day",
            "month",
            "quarter",
            "year",
            "day_of_week",
            "is_weekend",
        )
    )
