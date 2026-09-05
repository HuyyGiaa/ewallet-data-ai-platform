from __future__ import annotations

import logging
from datetime import timedelta

from pyspark.sql import DataFrame
from pyspark.sql import functions as F


logger = logging.getLogger(__name__)


def build_feat_user_90d(fact_transactions_df: DataFrame, dim_user_df: DataFrame,) -> DataFrame:
    """
    Build user features over the latest 90-day window.

    Grain:
        1 row / user.
    """

    reference_timestamp = (
        fact_transactions_df
        .agg(
            F.max(
                "timestamp"
            ).alias(
                "reference_timestamp"
            )
        )
        .first()[
            "reference_timestamp"
        ]
    )

    if reference_timestamp is None:
        raise RuntimeError(
            "Cannot build feat_user_90d: "
            "fact_transactions is empty."
        )

    cutoff_timestamp = (
        reference_timestamp
        - timedelta(days=90)
    )

    logger.info(
        "[feat_user_90d] Feature window: %s -> %s",
        cutoff_timestamp,
        reference_timestamp,
    )

    transactions_90d = (
        fact_transactions_df
        .filter(
            (
                F.col("timestamp")
                >= F.lit(cutoff_timestamp)
            )
            &
            (
                F.col("timestamp")
                <= F.lit(reference_timestamp)
            )
        )
    )

    user_features = (
        transactions_90d
        .groupBy(
            "user_id"
        )
        .agg(
            F.count(
                F.lit(1)
            ).alias(
                "f_user_total_transactions_90d"
            ),
            F.avg(
                "amount"
            ).alias(
                "f_user_avg_transaction_amount_90d"
            ),
            (
                F.sum(
                    F.when(
                        F.col("status") == "failed",
                        1,
                    ).otherwise(0)
                )
                /
                F.count(
                    F.lit(1)
                )
            ).alias(
                "f_user_failed_transaction_rate_90d"
            ),
            F.countDistinct(
                "merchant_id"
            ).alias(
                "f_user_distinct_merchants_90d"
            ),
        )
    )

    return (
        dim_user_df
        .select(
            "user_id"
        )
        .join(
            user_features,
            on="user_id",
            how="left",
        )
        .fillna(
            {
                "f_user_total_transactions_90d": 0,
                "f_user_avg_transaction_amount_90d": 0.0,
                "f_user_failed_transaction_rate_90d": 0.0,
                "f_user_distinct_merchants_90d": 0,
            }
        )
        .withColumn(
            "event_timestamp",
            F.lit(
                reference_timestamp
            ).cast("timestamp"),
        )
        .withColumn(
            "created_timestamp",
            F.current_timestamp(),
        )
    )