"""Quality rules for feature-table timestamps and point-in-time correctness.

``event_timestamp`` is business time: the instant the features describe.
``created`` is processing time: when the row was materialized. The pipelines
write one UTC instant per run into ``created``, so the rules check:

* both columns exist as ``TIMESTAMP`` and are never NULL;
* ``created`` holds one value per table (one materialization boundary), and the
  four DP3 tables share it;
* ``created`` is not a copy of ``event_timestamp``;
* ``feat_user_90d.created`` equals its legacy ``created_timestamp``.

No rule requires ``created >= event_timestamp``: a replay or backfill of future
dated synthetic events can legitimately materialize earlier than the events.

The PIT rule recomputes one window count for a deterministic sample of entities
directly from Silver transactions with strict ``[T - window, T)`` bounds and
compares it with the persisted value. Including the current transaction, a
same-timestamp peer or a future event changes the count and fails the rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F

from data_platform.quality.results import ValidationResult


MICROS_PER_SECOND = 1_000_000


@dataclass(frozen=True)
class PitProbe:
    """One persisted window count to recompute from transactions."""

    table: str
    entity_column: str
    count_column: str
    window_seconds: int
    payments_only: bool = False


# One window per table. The 1-hour counts cover every historical table and a
# boundary error in any of them shifts the count by at least one.
PIT_PROBES = (
    PitProbe("feat_user_behavior", "user_id", "user_tx_count_1h", 3600),
    PitProbe("feat_account_behavior", "account_id", "account_tx_count_1h", 3600),
    PitProbe("feat_device_behavior", "device_id", "device_tx_count_1h", 3600),
    PitProbe("feat_merchant_behavior", "merchant_id", "merchant_tx_count_1h", 3600, True),
)


def _type_of(df: DataFrame, column: str) -> str:
    if column not in df.columns:
        return "missing"
    return df.schema[column].dataType.simpleString()


def feature_timestamp_results(
    df: DataFrame,
    table: str,
    *,
    legacy_created_column: str | None = None,
) -> list[ValidationResult]:
    """event_timestamp/created rules for one persisted feature table."""

    types = {column: _type_of(df, column) for column in ("event_timestamp", "created")}
    typed = ValidationResult(
        f"{table}.timestamp_columns_typed",
        all(kind == "timestamp" for kind in types.values()),
        ",".join(f"{name}:{kind}" for name, kind in types.items()),
        "event_timestamp timestamp, created timestamp",
    )
    if not typed.passed:
        return [typed]
    summary = df.agg(
        F.count("*").alias("rows"),
        F.sum(F.col("event_timestamp").isNull().cast("long")).alias("null_event"),
        F.sum(F.col("created").isNull().cast("long")).alias("null_created"),
        F.countDistinct("created").alias("created_values"),
        F.sum((F.col("created") == F.col("event_timestamp")).cast("long")).alias("copied"),
        F.min("created").alias("created_at"),
        F.max("event_timestamp").alias("max_event"),
    ).first()
    nulls = int(summary["null_event"] or 0) + int(summary["null_created"] or 0)
    results = [
        typed,
        ValidationResult(f"{table}.timestamp_columns_not_null", nulls == 0, nulls,
                         "0 null event_timestamp or created"),
        ValidationResult(f"{table}.created_single_materialization",
                         int(summary["created_values"]) == 1 and int(summary["rows"]) > 0,
                         int(summary["created_values"]), "1 distinct created value per table"),
        ValidationResult(f"{table}.created_not_copied_from_event_timestamp",
                         int(summary["copied"] or 0) == 0, int(summary["copied"] or 0),
                         "0 rows where created = event_timestamp"),
    ]
    if legacy_created_column is not None:
        if legacy_created_column not in df.columns:
            results.append(ValidationResult(
                f"{table}.created_matches_{legacy_created_column}", False, "missing",
                f"{legacy_created_column} kept for existing readers",
            ))
        else:
            differ = df.filter(~F.col("created").eqNullSafe(F.col(legacy_created_column))).count()
            results.append(ValidationResult(
                f"{table}.created_matches_{legacy_created_column}", differ == 0, differ,
                f"created equals the legacy {legacy_created_column}",
            ))
    return results


def shared_created_result(tables: Mapping[str, DataFrame], scope: str) -> ValidationResult:
    """All tables written by one run carry the same materialization instant."""

    values = set()
    for frame in tables.values():
        values.update(row["created"] for row in frame.select("created").distinct().limit(2).collect())
    return ValidationResult(
        f"{scope}.created_shared_by_run", len(values) == 1, len(values),
        "one created value across " + ", ".join(sorted(tables)),
    )


def _micros(column: str) -> Column:
    return F.unix_micros(F.col(column))


def pit_sample_result(
    feature_df: DataFrame,
    transactions: DataFrame,
    probe: PitProbe,
    *,
    sample_modulus: int = 1000,
) -> ValidationResult:
    """Recompute ``probe.count_column`` for sampled entities with strict PIT bounds.

    Entities are sampled by ``pmod(xxhash64(entity), sample_modulus) = 0`` so the
    sample is deterministic. Rows are joined on the entity and the hour bucket to
    keep the recomputation bounded at 100 GB scale.
    """

    if sample_modulus <= 0:
        raise ValueError("sample_modulus must be positive")
    entity = probe.entity_column
    bucket = probe.window_seconds * MICROS_PER_SECOND
    sampled = F.pmod(F.xxhash64(F.col(entity)), F.lit(sample_modulus)) == 0
    events = transactions.filter(F.col(entity).isNotNull())
    if probe.payments_only:
        events = events.filter(F.col("type") == "payment")
    events = (
        events.filter(sampled)
        .select(F.col(entity).alias("_entity"), _micros("timestamp").alias("_event_micros"))
        .withColumn("_bucket", F.floor(F.col("_event_micros") / F.lit(bucket)))
    )
    features = (
        feature_df.filter(F.col(entity).isNotNull() & sampled)
        .select(
            F.col(entity).alias("_entity"),
            "event_timestamp",
            _micros("event_timestamp").alias("_t"),
            F.col(probe.count_column).alias("_persisted"),
        )
        .withColumn("_start", F.col("_t") - F.lit(bucket))
    )
    # [T - window, T) spans at most two buckets: floor(start) and floor(start)+1.
    keyed = features.withColumn(
        "_bucket",
        F.explode(F.array(F.floor(F.col("_start") / F.lit(bucket)),
                          F.floor(F.col("_start") / F.lit(bucket)) + 1)),
    )
    counted = (
        keyed.join(events, ["_entity", "_bucket"], "left")
        .groupBy("_entity", "event_timestamp", "_persisted")
        .agg(F.sum(
            ((F.col("_event_micros") >= F.col("_start")) & (F.col("_event_micros") < F.col("_t")))
            .cast("long")
        ).alias("_recomputed"))
    )
    mismatches = counted.filter(
        F.coalesce(F.col("_recomputed"), F.lit(0)) != F.col("_persisted")
    ).count()
    sampled_rows = features.count()
    return ValidationResult(
        f"{probe.table}.pit_{probe.count_column}_matches_recomputation",
        mismatches == 0 and sampled_rows > 0,
        f"mismatches={mismatches},sampled_rows={sampled_rows}",
        f"recomputed [T-{probe.window_seconds}s, T) count equals the persisted value on a "
        f"1/{sample_modulus} entity sample",
    )


def historical_feature_timestamp_results(
    features: Mapping[str, DataFrame],
    transactions: DataFrame,
    *,
    sample_modulus: int = 1000,
    probes: Sequence[PitProbe] = PIT_PROBES,
) -> list[ValidationResult]:
    """All Phase B timestamp and PIT rules for the four DP3 tables."""

    results: list[ValidationResult] = []
    for table, frame in features.items():
        results.extend(feature_timestamp_results(frame, table))
    if all("created" in frame.columns for frame in features.values()):
        results.append(shared_created_result(features, "dp3_features"))
    for probe in probes:
        results.append(pit_sample_result(
            features[probe.table], transactions, probe, sample_modulus=sample_modulus
        ))
    return results
