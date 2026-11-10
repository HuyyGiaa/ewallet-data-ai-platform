from __future__ import annotations

from datetime import datetime

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F

from data_platform.processing.spark.gold.materialization import require_utc


MICROSECONDS_PER_SECOND = 1_000_000
CREATED_COLUMN = "created"


def created_column(materialized_at: datetime | None = None) -> F.Column:
    """Return the ``created`` audit value: when the feature row was materialized.

    ``created`` is processing time, not event time. Pass one timezone-aware UTC
    instant per write scope so every row and table of a run shares it; without
    one, Spark's ``current_timestamp()`` (fixed per query) is used. A naive
    datetime is refused because PySpark would read it in the driver's local zone.
    """
    if materialized_at is None:
        return F.current_timestamp()
    return F.lit(require_utc(materialized_at)).cast("timestamp")


def with_created(df: DataFrame, materialized_at: datetime | None = None) -> DataFrame:
    """Append the ``created`` audit column; feature values are untouched."""
    return df.withColumn(CREATED_COLUMN, created_column(materialized_at))


def with_event_time(df: DataFrame) -> DataFrame:
    """Expose the canonical feature timestamp and a numeric window key."""
    return (
        df
        .withColumn("event_timestamp", F.col("timestamp"))
        .withColumn("_event_micros", F.unix_micros("event_timestamp"))
    )


def historical_window(entity_column: str, seconds: int):
    """Return an exclusive point-in-time window: [T-seconds, T)."""
    return (
        Window
        .partitionBy(entity_column)
        .orderBy(F.col("_event_micros"))
        .rangeBetween(-seconds * MICROSECONDS_PER_SECOND, -1)
    )


def complete_history_window(entity_column: str):
    """Return all strictly earlier events for the entity."""
    return (
        Window
        .partitionBy(entity_column)
        .orderBy(F.col("_event_micros"))
        .rangeBetween(Window.unboundedPreceding, -1)
    )


def historical_count(window) -> F.Column:
    return F.count(F.lit(1)).over(window).cast("long")


def historical_sum(column_name: str, window) -> F.Column:
    return F.coalesce(F.sum(column_name).over(window), F.lit(0.0))


def historical_failed_rate(window) -> F.Column:
    return F.avg(
        F.when(F.col("status") == "failed", F.lit(1.0)).otherwise(F.lit(0.0))
    ).over(window)


def shared_snapshots(df: DataFrame, entity_column: str) -> DataFrame:
    """Collapse identical same-entity/same-time feature rows to one snapshot."""
    return df.dropDuplicates([entity_column, "event_timestamp"])
