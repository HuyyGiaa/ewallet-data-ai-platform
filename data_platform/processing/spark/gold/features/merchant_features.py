from __future__ import annotations

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from .temporal import (
    historical_count,
    historical_sum,
    historical_window,
    shared_snapshots,
    with_event_time,
)


def build_feat_merchant_behavior(transactions_df: DataFrame) -> DataFrame:
    events = with_event_time(
        transactions_df.filter(
            (F.col("type") == "payment") & F.col("merchant_id").isNotNull()
        )
    )
    window_10m = historical_window("merchant_id", 10 * 60)
    window_1h = historical_window("merchant_id", 60 * 60)
    window_24h = historical_window("merchant_id", 24 * 60 * 60)

    features = events.select(
        "merchant_id",
        "event_timestamp",
        historical_count(window_10m).alias("merchant_tx_count_10m"),
        historical_count(window_1h).alias("merchant_tx_count_1h"),
        historical_count(window_24h).alias("merchant_tx_count_24h"),
        F.size(F.collect_set("user_id").over(window_10m)).cast("long").alias(
            "merchant_unique_users_10m"
        ),
        F.size(F.collect_set("user_id").over(window_1h)).cast("long").alias(
            "merchant_unique_users_1h"
        ),
        historical_sum("amount", window_1h).alias("merchant_amount_sum_1h"),
        F.avg("amount").over(window_24h).alias("merchant_avg_amount_24h"),
    )
    return shared_snapshots(features, "merchant_id")
