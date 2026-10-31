from __future__ import annotations

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from .temporal import (
    historical_count,
    historical_failed_rate,
    historical_sum,
    historical_window,
    shared_snapshots,
    with_event_time,
)


def build_feat_user_behavior(transactions_df: DataFrame) -> DataFrame:
    events = with_event_time(transactions_df)
    window_5m = historical_window("user_id", 5 * 60)
    window_1h = historical_window("user_id", 60 * 60)
    window_24h = historical_window("user_id", 24 * 60 * 60)
    window_30d = historical_window("user_id", 30 * 24 * 60 * 60)

    features = events.select(
        "user_id",
        "event_timestamp",
        historical_count(window_5m).alias("user_tx_count_5m"),
        historical_count(window_1h).alias("user_tx_count_1h"),
        historical_count(window_24h).alias("user_tx_count_24h"),
        historical_sum("amount", window_1h).alias("user_amount_sum_1h"),
        F.count("amount").over(window_30d).cast("long").alias(
            "user_amount_observation_count_30d"
        ),
        F.avg("amount").over(window_30d).alias("user_avg_amount_30d"),
        F.stddev_samp("amount").over(window_30d).alias("user_std_amount_30d"),
        historical_failed_rate(window_24h).alias("user_failed_rate_24h"),
        F.size(F.collect_set("merchant_id").over(window_24h)).cast("long").alias(
            "user_distinct_merchants_24h"
        ),
    )
    return shared_snapshots(features, "user_id")
