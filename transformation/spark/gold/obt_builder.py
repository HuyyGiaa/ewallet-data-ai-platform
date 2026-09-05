from __future__ import annotations

from pyspark.sql import DataFrame
from pyspark.sql import functions as F


def build_obt_transaction_enriched(
    fact_transactions_df: DataFrame,
    dim_user_df: DataFrame,
    dim_account_df: DataFrame,
    dim_device_df: DataFrame,
    dim_merchant_df: DataFrame,
    dim_date_df: DataFrame,
) -> DataFrame:
    """
    Grain:
        1 row / transaction.

    LEFT JOINs preserve every transaction.
    """

    user_attrs = (
        dim_user_df
        .select(
            "user_id",
            "kyc_verified",
            F.col("created_at").alias(
                "user_created_at"
            ),
        )
    )

    account_attrs = (
        dim_account_df
        .select(
            "account_id",
            "account_type",
            F.col("currency").alias(
                "account_currency"
            ),
            F.col("created_at").alias(
                "account_created_at"
            ),
        )
    )

    device_attrs = (
        dim_device_df
        .select(
            "device_id",
            "device_type",
            "os",
            "first_seen_at",
        )
    )

    merchant_attrs = (
        dim_merchant_df
        .select(
            "merchant_id",
            "merchant_name",
            F.col("category").alias(
                "merchant_category"
            ),
        )
    )

    date_attrs = (
        dim_date_df
        .select(
            "date_key",
            "day_of_week",
            "month",
            "quarter",
            "year",
            "is_weekend",
        )
    )

    return (
        fact_transactions_df
        .join(
            user_attrs,
            on="user_id",
            how="left",
        )
        .join(
            account_attrs,
            on="account_id",
            how="left",
        )
        .join(
            device_attrs,
            on="device_id",
            how="left",
        )
        .join(
            merchant_attrs,
            on="merchant_id",
            how="left",
        )
        .join(
            date_attrs,
            on="date_key",
            how="left",
        )
        .select(
            "transaction_id",
            "user_id",
            "kyc_verified",
            "user_created_at",
            "account_id",
            "account_type",
            "account_currency",
            "account_created_at",
            "device_id",
            "device_type",
            "os",
            "first_seen_at",
            "merchant_id",
            "merchant_name",
            "merchant_category",
            "date_key",
            "day_of_week",
            "month",
            "quarter",
            "year",
            "is_weekend",
            "type",
            "status",
            "channel",
            "currency",
            "amount",
            "old_balance",
            "new_balance",
            "counterparty_account_id",
            "timestamp",
            "ingested_at",
            "event_date",
        )
    )