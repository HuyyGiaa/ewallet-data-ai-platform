from __future__ import annotations

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from .temporal import (
    MICROSECONDS_PER_SECOND,
    complete_history_window,
    historical_count,
    historical_failed_rate,
    historical_sum,
    historical_window,
    shared_snapshots,
    with_event_time,
)


def build_feat_account_behavior(transactions_df: DataFrame) -> DataFrame:
    events = with_event_time(transactions_df)
    window_5m = historical_window("account_id", 5 * 60)
    window_1h = historical_window("account_id", 60 * 60)
    window_24h = historical_window("account_id", 24 * 60 * 60)
    window_30d = historical_window("account_id", 30 * 24 * 60 * 60)
    previous_window = complete_history_window("account_id")
    previous_micros = F.max("_event_micros").over(previous_window)

    features = events.select(
        "account_id",
        "event_timestamp",
        historical_count(window_5m).alias("account_tx_count_5m"),
        historical_count(window_1h).alias("account_tx_count_1h"),
        historical_count(window_24h).alias("account_tx_count_24h"),
        historical_sum("amount", window_1h).alias("account_amount_sum_1h"),
        historical_sum("amount", window_24h).alias("account_amount_sum_24h"),
        F.count("amount").over(window_30d).cast("long").alias(
            "account_amount_observation_count_30d"
        ),
        F.avg("amount").over(window_30d).alias("account_avg_amount_30d"),
        F.stddev_samp("amount").over(window_30d).alias("account_std_amount_30d"),
        historical_failed_rate(window_24h).alias("account_failed_rate_24h"),
        (
            (F.col("_event_micros") - previous_micros)
            / F.lit(MICROSECONDS_PER_SECOND)
        ).cast("double").alias("account_seconds_since_last_tx"),
    )
    return shared_snapshots(features, "account_id")
