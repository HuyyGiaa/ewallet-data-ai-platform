"""Compare one stage of a Baseline and an Optimized campaign (read-only, Spark).

Run after both campaigns, once per stage. Both profiles read the same Source, so
their business content must agree. This is not a winner selection.

Policy, per table:
* same columns and types, row counts and logical keys;
* non-float columns equal (NULL-safe); float columns within ``REL_TOL``/``ABS_TOL``,
  the bound the repository uses for last-bit aggregate order differences;
* run-specific audit columns (``created``, ``created_timestamp``) are excluded here;
  their own contract is checked by the stage validators of each run.

Tables with a logical key are compared row by row. Bronze has no unique key, so it
is compared as an exact multiset.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from ops.gcp.summarize_spark_output import EXTENSION_GOLD_PRIMARY_KEYS, FEATURE_GRAINS, GOLD_PRIMARY_KEYS
from ops.gcp.validate_spark_output_equivalence import expected_tables


REL_TOL = 1e-12
ABS_TOL = 1e-9
AUDIT_COLUMNS = ("created", "created_timestamp")
# The Dataproc archive carries only Python files, so these mirror the Silver keys of
# schema_relationships.yml (a unit test pins them together).
SILVER_KEYS = {
    "users": ("user_id",),
    "accounts": ("account_id",),
    "merchants": ("merchant_id",),
    "devices": ("device_id",),
    "transactions": ("transaction_id",),
    "login_events": ("login_id",),
    "balance_snapshots": ("account_id", "snapshot_date"),
    "fraud_labels": ("transaction_id",),
    "merchant_history": ("merchant_id", "snapshot_ts"),
}


def stage_keys(stage: str) -> Mapping[str, tuple[str, ...]]:
    """Logical keys per table; an absent table is compared as a multiset."""

    if stage == "dp2-gold":
        return {**GOLD_PRIMARY_KEYS, **EXTENSION_GOLD_PRIMARY_KEYS}
    if stage == "dp3-features":
        return FEATURE_GRAINS
    if stage == "dp2-silver":
        return SILVER_KEYS
    return {}


def compare_table(baseline: Any, optimized: Any, table: str, keys: Sequence[str] | None) -> dict[str, Any]:
    """Return a factual difference report for one table; ``passed`` is the verdict."""

    from pyspark.sql import functions as F

    left = baseline.drop(*[c for c in AUDIT_COLUMNS if c in baseline.columns])
    right = optimized.drop(*[c for c in AUDIT_COLUMNS if c in optimized.columns])
    report: dict[str, Any] = {"table": table, "keys": list(keys or []), "differences": {}}
    if left.dtypes != right.dtypes:
        report["differences"]["schema"] = {"baseline": left.dtypes, "optimized": right.dtypes}
        report["passed"] = False
        return report
    rows = (left.count(), right.count())
    report["rows"] = {"baseline": rows[0], "optimized": rows[1]}
    if rows[0] != rows[1]:
        report["differences"]["row_count"] = rows[0] - rows[1]
    if not keys:
        only_left = left.exceptAll(right).limit(1).count()
        only_right = right.exceptAll(left).limit(1).count()
        if only_left or only_right:
            report["differences"]["multiset"] = {"baseline_only": only_left, "optimized_only": only_right}
        report["passed"] = not report["differences"]
        return report
    duplicate = {
        "baseline": left.groupBy(*keys).count().filter("count > 1").limit(1).count(),
        "optimized": right.groupBy(*keys).count().filter("count > 1").limit(1).count(),
    }
    if any(duplicate.values()):
        report["differences"]["duplicate_keys"] = duplicate
    a = left.select(*[F.col(c).alias(f"l__{c}") if c not in keys else F.col(c) for c in left.columns])
    b = right.select(*[F.col(c).alias(f"r__{c}") if c not in keys else F.col(c) for c in right.columns])
    joined = a.join(b, list(keys), "full_outer")
    types = dict(left.dtypes)
    aggregates = []
    for column in left.columns:
        if column in keys:
            continue
        l, r = F.col(f"l__{column}"), F.col(f"r__{column}")
        if types[column] in ("double", "float"):
            equal = l.eqNullSafe(r) | (
                l.isNotNull() & r.isNotNull()
                & (F.abs(l - r) <= F.greatest(F.lit(ABS_TOL), F.lit(REL_TOL) * F.greatest(F.abs(l), F.abs(r))))
            )
        else:
            equal = l.eqNullSafe(r)
        aggregates.append(F.sum((~equal).cast("long")).alias(column))
    # A key present on one side leaves every value NULL there; count it from the join.
    left_presence = a.select(*keys).withColumn("_l", F.lit(1))
    right_presence = b.select(*keys).withColumn("_r", F.lit(1))
    missing = left_presence.join(right_presence, list(keys), "full_outer").filter("_l IS NULL OR _r IS NULL").limit(1).count()
    if missing:
        report["differences"]["unmatched_keys"] = missing
    if aggregates:
        counts = joined.agg(*aggregates).first().asDict()
        columns = {name: int(value) for name, value in counts.items() if value}
        if columns:
            report["differences"]["columns"] = columns
    report["passed"] = not report["differences"]
    return report


def compare_stage(spark: Any, stage: str, dataset_version: str, baseline_root: str, optimized_root: str) -> dict[str, Any]:
    keys = stage_keys(stage)
    tables = []
    for table in sorted(expected_tables(stage, dataset_version)):
        baseline = spark.read.format("delta").load(f"{baseline_root.rstrip('/')}/{table}")
        optimized = spark.read.format("delta").load(f"{optimized_root.rstrip('/')}/{table}")
        tables.append(compare_table(baseline, optimized, table, keys.get(table)))
    return {
        "schema_version": "campaign-comparison-v1",
        "stage": stage,
        "dataset_version": dataset_version,
        "baseline_root": baseline_root,
        "optimized_root": optimized_root,
        "float_policy": {"rel_tol": REL_TOL, "abs_tol": ABS_TOL},
        "excluded_audit_columns": list(AUDIT_COLUMNS),
        "status": "PASS" if all(item["passed"] for item in tables) else "DIFFERENT",
        "tables": tables,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("dp1-bronze", "dp2-silver", "dp2-gold", "dp3-features"))
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--baseline-root", required=True)
    parser.add_argument("--optimized-root", required=True)
    parser.add_argument("--report-uri", required=True, help="Written once; refuses to overwrite.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.baseline_root.rstrip("/") == args.optimized_root.rstrip("/"):
        raise ValueError("baseline and optimized roots must differ")
    from pyspark.sql import SparkSession

    from data_platform.quality.reporting import write_report

    spark = (SparkSession.builder.appName(f"ewallet-campaign-compare-{args.stage}")
             .config("spark.sql.session.timeZone", "UTC").getOrCreate())
    try:
        report = compare_stage(spark, args.stage, args.dataset_version, args.baseline_root, args.optimized_root)
        write_report(spark, args.report_uri, report)
    finally:
        spark.stop()
    print(json.dumps({"status": report["status"], "report": args.report_uri}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
