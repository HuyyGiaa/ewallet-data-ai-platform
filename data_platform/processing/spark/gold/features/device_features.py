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


def build_feat_device_behavior(
    transactions_df: DataFrame,
    devices_df: DataFrame,
) -> DataFrame:
    events = with_event_time(transactions_df)
    window_1h = historical_window("device_id", 60 * 60)
    window_24h = historical_window("device_id", 24 * 60 * 60)

    features = events.select(
        "device_id",
        "event_timestamp",
        historical_count(window_1h).alias("device_tx_count_1h"),
        historical_count(window_24h).alias("device_tx_count_24h"),
        historical_sum("amount", window_24h).alias("device_amount_sum_24h"),
        historical_failed_rate(window_24h).alias("device_failed_rate_24h"),
    )
    snapshots = shared_snapshots(features, "device_id")

    return (
        snapshots
        .join(devices_df.select("device_id", "first_seen_at"), "device_id", "left")
        .withColumn(
            "device_age_seconds",
            F.greatest(
                F.lit(0.0),
                F.col("event_timestamp").cast("double")
                - F.col("first_seen_at").cast("double"),
            ),
        )
        .select(
            "device_id",
            "event_timestamp",
            "device_age_seconds",
            "device_tx_count_1h",
            "device_tx_count_24h",
            "device_amount_sum_24h",
            "device_failed_rate_24h",
        )
    )
