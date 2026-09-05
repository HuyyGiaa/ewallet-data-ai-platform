from __future__ import annotations

from pyspark.sql import DataFrame
from pyspark.sql import functions as F


def build_opt_merchant_performance(fact_transactions_df: DataFrame, dim_merchant_df: DataFrame,) -> DataFrame:
    """
    Grain:
        1 row / merchant.
    """

    merchant_metrics = (
        fact_transactions_df
        .filter(
            F.col("merchant_id").isNotNull()
        )
        .groupBy(
            "merchant_id"
        )
        .agg(
            F.count(
                F.lit(1)
            ).alias(
                "transaction_count"
            ),
            F.sum(
                "amount"
            ).alias(
                "total_amount"
            ),
            F.avg(
                "amount"
            ).alias(
                "avg_amount"
            ),
            F.sum(
                F.when(
                    F.col("status") == "success",
                    1,
                ).otherwise(0)
            ).alias(
                "success_count"
            ),
            F.sum(
                F.when(
                    F.col("status") == "failed",
                    1,
                ).otherwise(0)
            ).alias(
                "failed_count"
            ),
            F.countDistinct(
                "user_id"
            ).alias(
                "distinct_users"
            ),
        )
    )

    return (
        dim_merchant_df
        .join(
            merchant_metrics,
            on="merchant_id",
            how="left",
        )
        .fillna(
            {
                "transaction_count": 0,
                "total_amount": 0.0,
                "avg_amount": 0.0,
                "success_count": 0,
                "failed_count": 0,
                "distinct_users": 0,
            }
        )
        .select(
            "merchant_id",
            "merchant_name",
            "category",
            "transaction_count",
            "total_amount",
            "avg_amount",
            "success_count",
            "failed_count",
            "distinct_users",
        )
    )