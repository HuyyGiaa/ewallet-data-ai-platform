"""Compare a Baseline and an Optimized campaign (read-only, distributed Spark).

Input is the two finished campaign manifests, so only two COMPLETE campaigns of the
same dataset and the same Source manifest can be compared. This is not a winner
selection.

Audit policy: the only columns left out of cross-run equality are the materialization
metadata declared per table in ``VOLATILE_AUDIT_COLUMNS``: ``created`` on the five
feature tables and the legacy ``created_timestamp`` on ``feat_user_90d``. They are
excluded by table and column, not by name pattern: ``event_timestamp``, SCD2
``valid_from_ts``/``valid_to_ts``/``is_current`` and every other timestamp are
compared. Each run's own audit metadata is checked by its validators (every
``created`` equals the clock in the Delta commit), and the manifests must show those
validations passed.

Value policy: non-float columns are compared exactly and NULL-safely. Double and
float columns use ``REL_TOL``/``ABS_TOL``, the bound used by the golden regression
for last-bit aggregation-order differences; a significant change fails.

Tables with a logical key are compared row by row. Bronze has no unique key, so it
is compared as an exact multiset. All work stays in Spark; only counts reach the
driver.

Verdict: SEMANTIC_EQUIVALENT, SEMANTIC_MISMATCH, or COMPARISON_INCOMPLETE when any
table or manifest could not be compared. A partial comparison is never equivalent.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from ops.gcp.summarize_spark_output import (
    EXTENSION_GOLD_PRIMARY_KEYS,
    FEATURE_GRAINS,
    GOLD_PRIMARY_KEYS,
    VOLATILE_AUDIT_COLUMNS,
)
from ops.gcp.validate_spark_output_equivalence import expected_tables


REL_TOL = 1e-12
ABS_TOL = 1e-9
EQUIVALENT = "SEMANTIC_EQUIVALENT"
MISMATCH = "SEMANTIC_MISMATCH"
INCOMPLETE = "COMPARISON_INCOMPLETE"
STAGES = (
    ("bronze", "dp1-bronze"),
    ("silver", "dp2-silver"),
    ("gold", "dp2-gold"),
    ("features", "dp3-features"),
)
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


def audit_columns(stage: str, table: str) -> tuple[str, ...]:
    return tuple(VOLATILE_AUDIT_COLUMNS.get((stage, table), ()))


def _float_equal(left: Any, right: Any) -> Any:
    from pyspark.sql import functions as F

    return left.eqNullSafe(right) | (
        left.isNotNull() & right.isNotNull()
        & (F.abs(left - right) <= F.greatest(F.lit(ABS_TOL), F.lit(REL_TOL) * F.greatest(F.abs(left), F.abs(right))))
    )


def compare_table(
    baseline: Any,
    optimized: Any,
    table: str,
    keys: Sequence[str] | None,
    audit: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a factual difference report for one table."""

    from pyspark.sql import functions as F

    report: dict[str, Any] = {"table": table, "keys": list(keys or []), "excluded_audit_columns": list(audit),
                              "differences": {}}
    missing_audit = sorted(c for c in audit if c not in baseline.columns or c not in optimized.columns)
    if missing_audit:
        report["incomplete"] = f"declared audit columns are missing: {missing_audit}"
        report["passed"] = False
        return report
    left = baseline.drop(*audit)
    right = optimized.drop(*audit)
    report["compared_columns"] = list(left.columns)
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
        equal = _float_equal(l, r) if types[column] in ("double", "float") else l.eqNullSafe(r)
        aggregates.append(F.sum((~equal).cast("long")).alias(column))
    left_presence = a.select(*keys).withColumn("_l", F.lit(1))
    right_presence = b.select(*keys).withColumn("_r", F.lit(1))
    unmatched = left_presence.join(right_presence, list(keys), "full_outer").filter("_l IS NULL OR _r IS NULL").limit(1).count()
    if unmatched:
        report["differences"]["unmatched_keys"] = unmatched
    if aggregates:
        counts = joined.agg(*aggregates).first().asDict()
        columns = {name: int(value) for name, value in counts.items() if value}
        if columns:
            report["differences"]["columns"] = columns
    report["passed"] = not report["differences"]
    return report


def compare_stage(spark: Any, stage: str, dataset_version: str, baseline_root: str, optimized_root: str) -> dict[str, Any]:
    """Compare every expected table of one stage; an unreadable table is incomplete."""

    keys = stage_keys(stage)
    tables = []
    for table in sorted(expected_tables(stage, dataset_version)):
        try:
            baseline = spark.read.format("delta").load(f"{baseline_root.rstrip('/')}/{table}")
            optimized = spark.read.format("delta").load(f"{optimized_root.rstrip('/')}/{table}")
        except Exception as error:  # a missing table must not read as equivalence
            tables.append({"table": table, "passed": False, "incomplete": f"unreadable: {type(error).__name__}"})
            continue
        tables.append(compare_table(baseline, optimized, table, keys.get(table), audit_columns(stage, table)))
    return {"stage": stage, "baseline_root": baseline_root, "optimized_root": optimized_root,
            "verdict": verdict_of(tables), "tables": tables}


def verdict_of(tables: Sequence[Mapping[str, Any]]) -> str:
    if any("incomplete" in item for item in tables):
        return INCOMPLETE
    return EQUIVALENT if all(item["passed"] for item in tables) else MISMATCH


def manifest_problems(baseline: Mapping[str, Any], optimized: Mapping[str, Any]) -> list[str]:
    """Two manifests are comparable only as two complete campaigns on one Source."""

    problems = []
    for name, manifest, profile in (("baseline", baseline, "baseline"), ("optimized", optimized, "optimized")):
        if manifest.get("status") != "COMPLETE":
            problems.append(f"{name} campaign is not COMPLETE")
        if manifest.get("spark_profile") != profile:
            problems.append(f"{name} manifest has profile {manifest.get('spark_profile')!r}")
        for stage, _ in STAGES:
            validation = ((manifest.get("stages") or {}).get(stage) or {}).get("validation") or {}
            if validation.get("status") != "PASS":
                problems.append(f"{name} {stage} validation did not pass")
    for field in ("dataset_version", "benchmark_id"):
        if baseline.get(field) != optimized.get(field):
            problems.append(f"{field} differs between campaigns")
    if (baseline.get("source") or {}).get("manifest_sha256") != (optimized.get("source") or {}).get("manifest_sha256"):
        problems.append("campaigns read different Source manifests")
    return problems


def compare_campaigns(spark: Any, baseline: Mapping[str, Any], optimized: Mapping[str, Any]) -> dict[str, Any]:
    """Compare all four stages of two campaign manifests."""

    problems = manifest_problems(baseline, optimized)
    report: dict[str, Any] = {
        "schema_version": "campaign-comparison-v2",
        "dataset_version": baseline.get("dataset_version"),
        "benchmark_id": baseline.get("benchmark_id"),
        "source_manifest_sha256": (baseline.get("source") or {}).get("manifest_sha256"),
        "float_policy": {"rel_tol": REL_TOL, "abs_tol": ABS_TOL},
        "baseline_run_ids": {stage: ((baseline.get("stages") or {}).get(stage) or {}).get("run_id") for stage, _ in STAGES},
        "optimized_run_ids": {stage: ((optimized.get("stages") or {}).get(stage) or {}).get("run_id") for stage, _ in STAGES},
        "problems": problems,
        "stages": [],
    }
    if problems:
        report["verdict"] = INCOMPLETE
        return report
    for stage, spark_stage in STAGES:
        report["stages"].append(compare_stage(
            spark, spark_stage, str(baseline["dataset_version"]),
            baseline["stages"][stage]["output_root"], optimized["stages"][stage]["output_root"],
        ))
    verdicts = {item["verdict"] for item in report["stages"]}
    report["verdict"] = INCOMPLETE if INCOMPLETE in verdicts else MISMATCH if MISMATCH in verdicts else EQUIVALENT
    return report


def _read_manifest(spark: Any, uri: str) -> Mapping[str, Any]:
    text = spark.read.text(uri, wholetext=True).first()[0]
    value = json.loads(text)
    if not isinstance(value, Mapping):
        raise ValueError(f"campaign manifest is not an object: {uri}")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline-manifest-uri", required=True)
    parser.add_argument("--optimized-manifest-uri", required=True)
    parser.add_argument("--report-uri", required=True, help="Written once; refuses to overwrite.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    from pyspark.sql import SparkSession

    from data_platform.quality.reporting import write_report

    spark = SparkSession.builder.appName("ewallet-campaign-compare").config("spark.sql.session.timeZone", "UTC").getOrCreate()
    try:
        report = compare_campaigns(spark, _read_manifest(spark, args.baseline_manifest_uri),
                                   _read_manifest(spark, args.optimized_manifest_uri))
        write_report(spark, args.report_uri, report)
    finally:
        spark.stop()
    print(json.dumps({"verdict": report["verdict"], "report": args.report_uri}))
    return 0 if report["verdict"] == EQUIVALENT else 1


if __name__ == "__main__":
    sys.exit(main())
