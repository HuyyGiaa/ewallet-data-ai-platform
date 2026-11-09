"""SCD Type 2 versions from persisted full snapshots, and as-of lookup.

Input: full snapshots, one state per ``(business_key, effective timestamp)``.
Output: one row per version with the half-open validity interval
``[valid_from_ts, valid_to_ts)``:

* consecutive snapshots with the same tracked attributes collapse into one
  version, so repeating an unchanged state never opens a new version;
* exact duplicate snapshots are collapsed before change detection;
* two different states for the same key and timestamp are a contract violation
  and raise ``Scd2ContractError``, as do missing keys or effective timestamps;
* ``valid_to_ts`` is the next version's ``valid_from_ts``; the latest version has
  ``valid_to_ts = NULL`` and ``is_current = true``.

The output is a pure function of the snapshot set, so rebuilding from the same
snapshots (or from snapshots replayed twice) gives identical rows.

Timestamps are Spark ``TIMESTAMP`` (instants). With the platform's
``spark.sql.session.timeZone = UTC`` they display as UTC.
"""

from __future__ import annotations

from collections.abc import Sequence

from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as F
from pyspark.sql.types import TimestampType


SCD2_COLUMNS = ("valid_from_ts", "valid_to_ts", "is_current")


class Scd2ContractError(ValueError):
    """The snapshot input violates the SCD2 input contract."""


def _require_input_contract(
    snapshots_df: DataFrame,
    business_key: str,
    attribute_columns: Sequence[str],
    effective_ts_column: str,
) -> None:
    if not attribute_columns:
        raise Scd2ContractError("SCD2 needs at least one tracked attribute column")
    required = [business_key, effective_ts_column, *attribute_columns]
    missing = sorted(set(required) - set(snapshots_df.columns))
    if missing:
        raise Scd2ContractError(f"SCD2 snapshot input is missing columns: {missing}")
    reserved = sorted(set(SCD2_COLUMNS) & set(required))
    if reserved:
        raise Scd2ContractError(f"SCD2 input must not use output column names: {reserved}")
    field_type = snapshots_df.schema[effective_ts_column].dataType
    if not isinstance(field_type, TimestampType):
        raise Scd2ContractError(
            f"{effective_ts_column} must be TIMESTAMP, got {field_type.simpleString()}"
        )


def _require_valid_rows(
    distinct_snapshots: DataFrame,
    business_key: str,
    effective_ts_column: str,
) -> None:
    """Reject null keys, null effective times and conflicting same-time states."""

    nulls = distinct_snapshots.agg(
        F.sum(F.col(business_key).isNull().cast("long")).alias("null_keys"),
        F.sum(F.col(effective_ts_column).isNull().cast("long")).alias("null_times"),
    ).first()
    if (nulls["null_keys"] or 0) or (nulls["null_times"] or 0):
        raise Scd2ContractError(
            f"SCD2 snapshots contain {nulls['null_keys'] or 0} null {business_key} and "
            f"{nulls['null_times'] or 0} null {effective_ts_column} values"
        )
    conflicts = (
        distinct_snapshots.groupBy(business_key, effective_ts_column)
        .count()
        .filter(F.col("count") > 1)
    )
    sample = conflicts.limit(5).collect()
    if sample:
        described = ", ".join(
            f"{row[business_key]}@{row[effective_ts_column]}" for row in sample
        )
        raise Scd2ContractError(
            f"conflicting snapshots share a {business_key} and {effective_ts_column}: "
            f"{described}"
        )


def build_scd2_from_snapshots(
    snapshots_df: DataFrame,
    *,
    business_key: str,
    attribute_columns: Sequence[str],
    effective_ts_column: str,
    surrogate_key_column: str | None = None,
) -> DataFrame:
    """Build SCD2 versions; see the module docstring for the contract."""

    attributes = tuple(attribute_columns)
    _require_input_contract(snapshots_df, business_key, attributes, effective_ts_column)
    distinct_snapshots = snapshots_df.select(
        business_key, effective_ts_column, *attributes
    ).distinct()
    _require_valid_rows(distinct_snapshots, business_key, effective_ts_column)

    ordered = Window.partitionBy(business_key).orderBy(F.col(effective_ts_column))
    unchanged = F.lit(True)
    for column in attributes:
        unchanged = unchanged & F.col(column).eqNullSafe(F.lag(column).over(ordered))
    first = F.row_number().over(ordered) == 1
    versions = (
        distinct_snapshots
        .withColumn("_opens_version", first | ~unchanged)
        .filter(F.col("_opens_version"))
        .withColumnRenamed(effective_ts_column, "valid_from_ts")
    )
    by_start = Window.partitionBy(business_key).orderBy(F.col("valid_from_ts"))
    versions = (
        versions
        .withColumn("valid_to_ts", F.lead("valid_from_ts").over(by_start))
        .withColumn("is_current", F.col("valid_to_ts").isNull())
    )
    columns = [business_key, *attributes, *SCD2_COLUMNS]
    if surrogate_key_column is not None:
        versions = versions.withColumn(
            surrogate_key_column,
            version_key(F.col(business_key), F.col("valid_from_ts")),
        )
        columns.insert(0, surrogate_key_column)
    return versions.select(*columns)


def version_key(business_key: Column, valid_from_ts: Column) -> Column:
    """Deterministic surrogate key of one version: SHA-256 of key and start micros."""

    return F.sha2(
        F.concat_ws("|", business_key.cast("string"), F.unix_micros(valid_from_ts).cast("string")),
        256,
    )


def as_of_condition(event_ts: Column, valid_from_ts: Column, valid_to_ts: Column) -> Column:
    """Half-open membership: ``valid_from_ts <= t < valid_to_ts`` (open end if NULL).

    At an exact boundary ``t == valid_to_ts`` of one version, ``t`` equals the
    next version's ``valid_from_ts`` and resolves to that new version.
    """

    return (valid_from_ts <= event_ts) & (valid_to_ts.isNull() | (event_ts < valid_to_ts))


def lookup_as_of(
    events_df: DataFrame,
    versions_df: DataFrame,
    *,
    business_key: str,
    event_ts_column: str,
    version_columns: Sequence[str],
) -> DataFrame:
    """Left-join each event to the version valid at its event time.

    Events before the first version, or with a key that has no history, keep
    NULL version columns. The returned version columns are prefixed ``asof_``.
    """

    versions = versions_df.select(
        F.col(business_key).alias("_asof_key"),
        F.col("valid_from_ts").alias("_asof_from"),
        F.col("valid_to_ts").alias("_asof_to"),
        *[F.col(column).alias(f"asof_{column}") for column in version_columns],
    )
    joined = events_df.join(
        versions,
        (F.col(business_key) == F.col("_asof_key"))
        & as_of_condition(F.col(event_ts_column), F.col("_asof_from"), F.col("_asof_to")),
        "left",
    )
    return joined.drop("_asof_key", "_asof_from", "_asof_to")
