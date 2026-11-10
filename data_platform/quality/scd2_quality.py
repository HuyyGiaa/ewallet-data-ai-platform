"""Quality rules for Silver merchant_history and Gold dim_merchant_scd2.

The rules work on DataFrames, so the same checks run in unit tests, in the
local validators and on explicit cloud run roots. Each returns
``ValidationResult`` records; a failing rule never raises.
"""

from __future__ import annotations

from collections.abc import Sequence

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F

from data_platform.processing.spark.gold.scd2 import lookup_as_of
from data_platform.quality.results import ValidationResult


MERCHANT_ATTRIBUTES = ("merchant_name", "category")
SCD2_SCHEMA = {
    "merchant_sk": "string",
    "merchant_id": "string",
    "merchant_name": "string",
    "category": "string",
    "valid_from_ts": "timestamp",
    "valid_to_ts": "timestamp",
    "is_current": "boolean",
}
HISTORY_SCHEMA = {
    "snapshot_id": "string",
    "merchant_id": "string",
    "merchant_name": "string",
    "category": "string",
    "snapshot_ts": "timestamp",
    "history_source": "string",
}


def _count(df: DataFrame, rule: str, condition, expected: str) -> ValidationResult:
    invalid = df.filter(condition).count()
    return ValidationResult(rule, invalid == 0, invalid, expected)


def schema_result(df: DataFrame, table: str, schema: dict[str, str]) -> ValidationResult:
    actual = {field.name: field.dataType.simpleString() for field in df.schema.fields}
    problems = sorted(
        f"{name}:{actual.get(name, 'missing')}"
        for name, kind in schema.items()
        if actual.get(name) != kind
    )
    return ValidationResult(
        f"{table}.schema", not problems, ",".join(problems) or "ok",
        "columns " + ", ".join(f"{name} {kind}" for name, kind in schema.items()),
    )


def _duplicates(df: DataFrame, keys: Sequence[str]) -> int:
    return df.groupBy(*keys).count().filter(F.col("count") > 1).count()


def merchant_history_results(history: DataFrame, merchants: DataFrame) -> list[ValidationResult]:
    """Silver merchant_history: typed, keyed, synthetic, and consistent with merchants."""

    schema = schema_result(history, "merchant_history", HISTORY_SCHEMA)
    if not schema.passed:
        return [schema]
    table = "merchant_history"
    ordered = Window.partitionBy("merchant_id").orderBy(F.col("snapshot_ts").desc())
    latest = (
        history.withColumn("_rank", F.row_number().over(ordered))
        .filter(F.col("_rank") == 1)
        .select("merchant_id", *[F.col(c).alias(f"h_{c}") for c in MERCHANT_ATTRIBUTES])
    )
    joined = merchants.select("merchant_id", *MERCHANT_ATTRIBUTES).join(latest, "merchant_id", "full")
    mismatch = F.lit(False)
    for column in MERCHANT_ATTRIBUTES:
        mismatch = mismatch | ~F.col(column).eqNullSafe(F.col(f"h_{column}"))
    conflicts = _duplicates(history, ["merchant_id", "snapshot_ts"])
    return [
        schema,
        ValidationResult(f"{table}.non_empty", history.limit(1).count() == 1, "checked", "row_count > 0"),
        _count(history, f"{table}.required_columns_not_null",
               F.col("snapshot_id").isNull() | F.col("merchant_id").isNull()
               | F.col("snapshot_ts").isNull(), "0 null snapshot_id, merchant_id or snapshot_ts"),
        ValidationResult(f"{table}.unique_key(snapshot_id)", _duplicates(history, ["snapshot_id"]) == 0,
                         "checked", "snapshot_id is unique"),
        ValidationResult(f"{table}.unique_key(merchant_id,snapshot_ts)", conflicts == 0, conflicts,
                         "one state per merchant and snapshot time"),
        _count(history, f"{table}.synthetic_provenance", ~F.col("history_source").eqNullSafe("synthetic"),
               "every row has history_source = synthetic"),
        _count(joined, f"{table}.latest_snapshot_equals_merchants",
               mismatch,
               "every merchant has history and its latest snapshot equals merchants"),
    ]


def merchant_scd2_results(
    scd2: DataFrame,
    dim_merchant: DataFrame,
    *,
    transactions: DataFrame | None = None,
) -> list[ValidationResult]:
    """Gold dim_merchant_scd2 interval, current-row and coverage rules.

    ``transactions`` (fact_transactions or Silver transactions) adds the as-of
    rule: every transaction with a merchant resolves to exactly one version.
    """

    table = "dim_merchant_scd2"
    schema = schema_result(scd2, table, SCD2_SCHEMA)
    if not schema.passed:
        return [schema]
    ordered = Window.partitionBy("merchant_id").orderBy(F.col("valid_from_ts"))
    sequenced = (
        scd2
        .withColumn("_next_from", F.lead("valid_from_ts").over(ordered))
        .withColumn("_previous_to", F.lag("valid_to_ts").over(ordered))
    )
    redundant = F.lit(True)
    for column in MERCHANT_ATTRIBUTES:
        redundant = redundant & F.col(column).eqNullSafe(F.lag(column).over(ordered))
    redundant = redundant & F.lag("merchant_id").over(ordered).isNotNull()
    current = scd2.filter(F.col("is_current"))
    current_counts = scd2.groupBy("merchant_id").agg(
        F.sum(F.col("is_current").cast("int")).alias("current_versions")
    )
    coverage = dim_merchant.select("merchant_id", *MERCHANT_ATTRIBUTES).join(
        current.select("merchant_id", *[F.col(c).alias(f"v_{c}") for c in MERCHANT_ATTRIBUTES]),
        "merchant_id",
        "full",
    )
    attribute_mismatch = F.lit(False)
    for column in MERCHANT_ATTRIBUTES:
        attribute_mismatch = attribute_mismatch | ~F.col(column).eqNullSafe(F.col(f"v_{column}"))
    unknown = scd2.select("merchant_id").distinct().join(
        dim_merchant.select("merchant_id"), "merchant_id", "left_anti"
    ).count()
    duplicate_starts = _duplicates(scd2, ["merchant_id", "valid_from_ts"])
    duplicate_keys = _duplicates(scd2, ["merchant_sk"])
    results = [
        schema,
        ValidationResult(f"{table}.non_empty", scd2.limit(1).count() == 1, "checked", "row_count > 0"),
        _count(scd2, f"{table}.required_columns_not_null",
               F.col("merchant_sk").isNull() | F.col("merchant_id").isNull()
               | F.col("valid_from_ts").isNull() | F.col("is_current").isNull(),
               "0 null merchant_sk, merchant_id, valid_from_ts or is_current"),
        ValidationResult(f"{table}.unique_key(merchant_id,valid_from_ts)", duplicate_starts == 0,
                         duplicate_starts, "one version per merchant and start"),
        ValidationResult(f"{table}.unique_key(merchant_sk)", duplicate_keys == 0, duplicate_keys,
                         "merchant_sk is unique"),
        _count(scd2, f"{table}.valid_to_after_valid_from",
               F.col("valid_to_ts").isNotNull() & (F.col("valid_to_ts") <= F.col("valid_from_ts")),
               "valid_to_ts > valid_from_ts when set"),
        _count(scd2, f"{table}.current_flag_matches_open_end",
               ~F.col("is_current").eqNullSafe(F.col("valid_to_ts").isNull()),
               "is_current = (valid_to_ts IS NULL)"),
        _count(current_counts, f"{table}.one_current_version_per_merchant",
               F.col("current_versions") != 1, "exactly one current version per merchant"),
        _count(sequenced, f"{table}.contiguous_non_overlapping_intervals",
               (F.col("_next_from").isNotNull() & ~F.col("valid_to_ts").eqNullSafe(F.col("_next_from")))
               | (F.col("_next_from").isNull() & F.col("valid_to_ts").isNotNull()),
               "each valid_to_ts equals the next version's valid_from_ts; the last is open"),
        _count(sequenced.withColumn("_redundant", redundant), f"{table}.no_redundant_versions",
               F.col("_redundant"), "consecutive versions differ in a tracked attribute"),
        ValidationResult(f"{table}.merchants_exist_in_dim_merchant", unknown == 0, unknown,
                         "every SCD2 merchant exists in dim_merchant"),
        _count(coverage, f"{table}.current_version_equals_dim_merchant",
               attribute_mismatch,
               "every dim_merchant row has a current version with the same attributes"),
    ]
    if transactions is not None:
        results.append(as_of_resolution_result(scd2, transactions))
    return results


def as_of_resolution_result(scd2: DataFrame, transactions: DataFrame) -> ValidationResult:
    """Every transaction with a merchant resolves to exactly one version as of its time."""

    events = transactions.filter(F.col("merchant_id").isNotNull()).select(
        F.col("transaction_id").alias("_event_id"),
        "merchant_id",
        F.col("timestamp").alias("_event_ts"),
    )
    resolved = lookup_as_of(
        events,
        F.broadcast(scd2),
        business_key="merchant_id",
        event_ts_column="_event_ts",
        version_columns=("merchant_sk",),
    )
    summary = resolved.groupBy("_event_id").agg(
        F.count("asof_merchant_sk").alias("matches")
    )
    bad = summary.filter(F.col("matches") != 1).count()
    return ValidationResult(
        "dim_merchant_scd2.transactions_resolve_to_one_version", bad == 0, bad,
        "every transaction with a merchant matches exactly one version in [valid_from_ts, valid_to_ts)",
    )
