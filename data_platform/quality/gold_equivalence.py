"""Row-level equivalence of two Gold outputs with an explicit float allowlist.

The table fingerprint in ``ops.gcp.summarize_spark_output`` is exact: one different
bit fails it. Spark sums doubles in plan-dependent order and IEEE-754 addition is
not associative, so two correct runs can differ in the last bits of an aggregate.

Rounding before hashing cannot fix this. Two values a few units in the last place
apart can still fall on opposite sides of a rounding boundary. On the DP2 Gold pair
``%.12g`` leaves 8,726 ``avg_amount`` rows and 146 ``f_user_avg_transaction_amount_90d``
rows different, so such a fingerprint would still fail.

This module joins the two outputs on their business key and compares every column:

* a tolerance applies only to the columns named in :data:`GOLD_POLICIES`;
* every other column, floats included, must match bit for bit;
* null, NaN and infinity must match exactly, even in an allowlisted column;
* keys must be non-null, unique and identical on both sides.

It never changes either output.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, Sequence

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc


ROW_REPORT_SCHEMA = "dp2-gold-row-equivalence-v1"
FINAL_VERDICT_SCHEMA = "dp2-gold-final-equivalence-v1"
FINGERPRINT_DIFFERENCE = "semantic_content_fingerprint"
WORST_EXAMPLES = 5


@dataclass(frozen=True)
class FloatTolerance:
    """Accepted numeric difference for one floating-point aggregate.

    A pair matches when ``|a - b| <= max(abs_tol, rel_tol * max(|a|, |b|))``.
    """

    rel_tol: float
    abs_tol: float
    justification: str

    def __post_init__(self) -> None:
        if not 0 <= self.rel_tol < 1e-9 or self.abs_tol < 0:
            raise ValueError("a tolerance must stay far below any business precision")
        if not self.justification:
            raise ValueError("a tolerance needs a written justification")


@dataclass(frozen=True)
class Invariant:
    """A business rule checked on one output; ``check`` returns violating rows."""

    name: str
    columns: tuple[str, ...]
    check: Callable[[Mapping[str, np.ndarray]], np.ndarray]


@dataclass(frozen=True)
class TablePolicy:
    """Comparison rules for one Gold table."""

    keys: tuple[str, ...]
    volatile_columns: tuple[str, ...] = ()
    float_tolerances: Mapping[str, FloatTolerance] = field(default_factory=dict)
    invariants: tuple[Invariant, ...] = ()


_SUM_ORDER = (
    "Sum of doubles evaluated in plan-dependent order. Measured on the DP2 pair: at "
    "most 2.6e-15 relative. 1e-12 is about 400 times that and far below the 0.01 VND "
    "resolution of these amounts."
)
_AMOUNT_TOLERANCE = FloatTolerance(1e-12, 0.0, _SUM_ORDER)
_AVERAGE_CHECK_TOLERANCE = 1e-9


def _average_disagrees(total: np.ndarray, average: np.ndarray, count: np.ndarray) -> np.ndarray:
    expected = total / np.maximum(count, 1)
    return np.abs(expected - average) > _AVERAGE_CHECK_TOLERANCE * np.maximum(np.abs(expected), 1.0)


GOLD_POLICIES: dict[str, TablePolicy] = {
    "dim_user": TablePolicy(("user_id",)),
    "dim_account": TablePolicy(("account_id",)),
    "dim_merchant": TablePolicy(("merchant_id",)),
    "dim_device": TablePolicy(("device_id",)),
    "dim_date": TablePolicy(("date_key",)),
    "fact_transactions": TablePolicy(("transaction_id",)),
    "fact_login_events": TablePolicy(("login_id",)),
    "fact_balance_snapshot": TablePolicy(("account_id", "snapshot_date")),
    "obt_transaction_enriched": TablePolicy(("transaction_id",)),
    "feat_user_90d": TablePolicy(
        keys=("user_id",),
        volatile_columns=("created_timestamp", "created"),
        float_tolerances={"f_user_avg_transaction_amount_90d": _AMOUNT_TOLERANCE},
        invariants=(
            Invariant(
                "metrics_not_negative",
                ("f_user_total_transactions_90d", "f_user_avg_transaction_amount_90d",
                 "f_user_distinct_merchants_90d"),
                lambda c: (c["f_user_total_transactions_90d"] < 0)
                | (c["f_user_avg_transaction_amount_90d"] < 0)
                | (c["f_user_distinct_merchants_90d"] < 0),
            ),
            Invariant(
                "failed_rate_between_0_and_1",
                ("f_user_failed_transaction_rate_90d",),
                lambda c: (c["f_user_failed_transaction_rate_90d"] < 0)
                | (c["f_user_failed_transaction_rate_90d"] > 1),
            ),
            Invariant(
                "distinct_merchants_within_transactions",
                ("f_user_distinct_merchants_90d", "f_user_total_transactions_90d"),
                lambda c: c["f_user_distinct_merchants_90d"] > c["f_user_total_transactions_90d"],
            ),
        ),
    ),
    "opt_merchant_performance": TablePolicy(
        keys=("merchant_id",),
        float_tolerances={"total_amount": _AMOUNT_TOLERANCE, "avg_amount": _AMOUNT_TOLERANCE},
        invariants=(
            Invariant(
                "outcomes_within_transactions",
                ("success_count", "failed_count", "transaction_count"),
                lambda c: c["success_count"] + c["failed_count"] > c["transaction_count"],
            ),
            Invariant(
                "distinct_users_within_transactions",
                ("distinct_users", "transaction_count"),
                lambda c: c["distinct_users"] > c["transaction_count"],
            ),
            Invariant(
                "average_is_total_over_count",
                ("total_amount", "avg_amount", "transaction_count"),
                lambda c: _average_disagrees(c["total_amount"], c["avg_amount"], c["transaction_count"]),
            ),
        ),
    ),
}


class ColumnSource(Protocol):
    """Column-wise access, so a 46-million-row table is never fully in memory."""

    @property
    def schema(self) -> pa.Schema:
        """Return the table schema."""

    def read(self, columns: Sequence[str]) -> pa.Table:
        """Return ``columns`` in a stable row order."""


class ArrowSource:
    """In-memory source used by tests and small fixtures."""

    def __init__(self, table: pa.Table) -> None:
        self.table = table

    @property
    def schema(self) -> pa.Schema:
        """Return the table schema."""

        return self.table.schema

    def read(self, columns: Sequence[str]) -> pa.Table:
        """Return ``columns`` in stored order."""

        return self.table.select(list(columns))


class DeltaSource:
    """Read a Delta table from a local path or ``gs://`` URI without writing."""

    def __init__(self, uri: str) -> None:
        from deltalake import DeltaTable

        self.uri = uri
        self.delta = DeltaTable(uri)
        self.dataset = self.delta.to_pyarrow_dataset()

    @property
    def schema(self) -> pa.Schema:
        """Return the table schema."""

        return self.dataset.schema

    @property
    def version(self) -> int:
        """Return the Delta version being compared."""

        return int(self.delta.version())

    def read(self, columns: Sequence[str]) -> pa.Table:
        """Return ``columns``; a scan is ordered by file, so repeated reads align."""

        return self.dataset.to_table(columns=list(columns))


def _schema_fields(schema: pa.Schema) -> dict[str, str]:
    return {item.name: str(item.type) for item in schema}


def _key_order(source: ColumnSource, keys: Sequence[str]) -> tuple[pa.Table, pa.Array]:
    table = source.read(keys)
    order = pc.sort_indices(table, sort_keys=[(key, "ascending") for key in keys])
    return table.take(order), order


def _duplicate_keys(sorted_keys: pa.Table) -> int:
    if sorted_keys.num_rows < 2:
        return 0
    same = None
    for name in sorted_keys.column_names:
        column = sorted_keys[name].combine_chunks()
        equal = pc.equal(column.slice(1), column.slice(0, len(column) - 1)).fill_null(False)
        same = equal if same is None else pc.and_(same, equal)
    return int(pc.sum(same.cast(pa.int64())).as_py() or 0)


def compare_keys(baseline: pa.Table, optimized: pa.Table) -> dict[str, object]:
    """Report null, duplicate and differing keys on sorted key tables."""

    nulls = {
        side: sum(table[name].null_count for name in table.column_names)
        for side, table in (("baseline", baseline), ("optimized", optimized))
    }
    identical = baseline.num_rows == optimized.num_rows and all(
        baseline[name].combine_chunks().equals(optimized[name].combine_chunks())
        for name in baseline.column_names
    )
    return {
        "null_keys": nulls,
        "duplicate_keys": {"baseline": _duplicate_keys(baseline), "optimized": _duplicate_keys(optimized)},
        "keys_identical": identical,
    }


def _numeric_arrays(left: pa.ChunkedArray, right: pa.ChunkedArray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    def split(column: pa.ChunkedArray) -> tuple[np.ndarray, np.ndarray]:
        combined = column.combine_chunks()
        nulls = np.asarray(pc.is_null(combined), dtype=bool)
        values = np.asarray(pc.fill_null(combined, 0.0).cast(pa.float64()), dtype=np.float64)
        return values, nulls

    left_values, left_nulls = split(left)
    right_values, right_nulls = split(right)
    return left_values, right_values, left_nulls, right_nulls


def _bits_differ(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    both_nan = np.isnan(left) & np.isnan(right)
    return (left.view(np.int64) != right.view(np.int64)) & ~both_nan


def _ulp_distance(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """Distance in representable doubles, for finite values of equal sign."""

    return np.abs(left.view(np.int64) - right.view(np.int64))


def compare_float_column(
    left: pa.ChunkedArray,
    right: pa.ChunkedArray,
    tolerance: FloatTolerance | None,
    keys: pa.Table,
) -> dict[str, object]:
    """Compare a float column, exactly unless a tolerance is allowlisted.

    Args:
        left: Baseline values, aligned by key.
        right: Optimized values, aligned by key.
        tolerance: Allowlisted tolerance, or ``None`` for bitwise comparison.
        keys: Sorted keys, for worst-case examples.

    Returns:
        Row counts, the distribution of differences and worst examples.
    """

    a, b, a_null, b_null = _numeric_arrays(left, right)
    null_mismatch = a_null != b_null
    valid = ~(a_null | b_null)
    special = (~np.isfinite(a) | ~np.isfinite(b)) & valid
    special_mismatch = special & _bits_differ(a, b)
    differ = valid & _bits_differ(a, b)
    finite = differ & ~special
    result: dict[str, object] = {
        "comparison": "tolerance" if tolerance else "exact",
        "null_mismatch_rows": int(null_mismatch.sum()),
        "non_finite_mismatch_rows": int(special_mismatch.sum()),
        "exact_mismatch_rows": int(differ.sum()),
        "rows": int(len(a)),
    }
    over = np.zeros(len(a), dtype=bool)
    if finite.any():
        x, y = a[finite], b[finite]
        absolute = np.abs(x - y)
        largest = np.maximum(np.abs(x), np.abs(y))
        with np.errstate(divide="ignore", invalid="ignore"):
            relative = np.where(largest > 0, absolute / largest, 0.0)
        ulps = _ulp_distance(x, y)
        result["difference"] = {
            "max_abs": float(absolute.max()),
            "max_rel": float(relative.max()),
            "rel_p50": float(np.percentile(relative, 50)),
            "rel_p99": float(np.percentile(relative, 99)),
            "ulp_p50": float(np.percentile(ulps, 50)),
            "ulp_p99": float(np.percentile(ulps, 99)),
            "ulp_max": int(ulps.max()),
            "mean_signed_rel": float(np.mean(np.where(largest > 0, (x - y) / largest, 0.0))),
        }
        if tolerance:
            allowed = np.maximum(tolerance.abs_tol, tolerance.rel_tol * largest)
            over[finite] = absolute > allowed
        else:
            over = finite
        worst = np.argsort(-relative)[:WORST_EXAMPLES]
        positions = np.flatnonzero(finite)[worst]
        result["worst_examples"] = [
            {
                "key": {name: keys[name][int(row)].as_py() for name in keys.column_names},
                "baseline": float(a[row]),
                "optimized": float(b[row]),
                "abs_diff": float(abs(a[row] - b[row])),
            }
            for row in positions
        ]
    if tolerance:
        result["tolerance"] = {
            "rel_tol": tolerance.rel_tol,
            "abs_tol": tolerance.abs_tol,
            "justification": tolerance.justification,
        }
    result["over_tolerance_rows"] = int(over.sum())
    result["passed"] = not (null_mismatch.any() or special_mismatch.any() or over.any())
    return result


def compare_exact_column(left: pa.ChunkedArray, right: pa.ChunkedArray) -> dict[str, object]:
    """Compare a non-float column; null must meet null."""

    a, b = left.combine_chunks(), right.combine_chunks()
    null_mismatch = pc.xor(pc.is_null(a), pc.is_null(b))
    value_mismatch = pc.fill_null(pc.not_equal(a, b), False)
    rows = int(pc.sum(pc.or_(null_mismatch, value_mismatch).cast(pa.int64())).as_py() or 0)
    return {"comparison": "exact", "mismatch_rows": rows, "rows": len(a), "passed": rows == 0}


def _invariant_arrays(source_columns: pa.Table) -> dict[str, np.ndarray]:
    return {
        name: np.asarray(pc.fill_null(source_columns[name].combine_chunks(), 0))
        for name in source_columns.column_names
    }


def check_invariants(source: ColumnSource, policy: TablePolicy) -> dict[str, int]:
    """Return the number of rows violating each business invariant."""

    return {
        rule.name: int(rule.check(_invariant_arrays(source.read(rule.columns))).sum())
        for rule in policy.invariants
    }


def compare_table(
    name: str, baseline: ColumnSource, optimized: ColumnSource, policy: TablePolicy
) -> dict[str, object]:
    """Compare one table and report every check.

    Args:
        name: Table name, for the report.
        baseline: Baseline output.
        optimized: Optimized output.
        policy: Keys, volatile columns, allowlisted tolerances and invariants.

    Returns:
        The table report; ``passed`` is true only when every check passed.

    Raises:
        ValueError: The policy allowlists a column that is not a floating-point
            column, which would make a tolerance apply to the wrong kind of data.
    """

    left_fields, right_fields = _schema_fields(baseline.schema), _schema_fields(optimized.schema)
    report: dict[str, object] = {"table": name, "keys": list(policy.keys)}
    report["schema_identical"] = left_fields == right_fields
    if not report["schema_identical"]:
        report.update(
            passed=False,
            schema_differences=sorted(set(left_fields.items()) ^ set(right_fields.items())),
        )
        return report
    for column in policy.float_tolerances:
        if left_fields.get(column) not in {"double", "float"}:
            raise ValueError(f"{name}.{column} is allowlisted but is not a float column")
    left_keys, left_order = _key_order(baseline, policy.keys)
    right_keys, right_order = _key_order(optimized, policy.keys)
    keys = compare_keys(left_keys, right_keys)
    report.update(row_counts={"baseline": left_keys.num_rows, "optimized": right_keys.num_rows}, keys_report=keys)
    keys_clean = (
        keys["keys_identical"]
        and not any(keys["null_keys"].values())
        and not any(keys["duplicate_keys"].values())
    )
    if not keys_clean:
        report["passed"] = False
        return report
    columns: dict[str, object] = {}
    for column, data_type in left_fields.items():
        if column in policy.keys:
            continue
        left = baseline.read([column])[column].take(left_order)
        right = optimized.read([column])[column].take(right_order)
        if column in policy.volatile_columns:
            columns[column] = {
                "comparison": "volatile_null_check_only",
                "null_rows": {"baseline": left.null_count, "optimized": right.null_count},
                "passed": left.null_count == 0 and right.null_count == 0,
            }
        elif data_type in {"double", "float"}:
            columns[column] = compare_float_column(left, right, policy.float_tolerances.get(column), left_keys)
        else:
            columns[column] = compare_exact_column(left, right)
    invariants = {"baseline": check_invariants(baseline, policy), "optimized": check_invariants(optimized, policy)}
    report.update(columns=columns, invariant_violations=invariants)
    report["passed"] = all(item["passed"] for item in columns.values()) and not any(
        count for side in invariants.values() for count in side.values()
    )
    return report


def digest(value: Mapping[str, object], field_name: str) -> str:
    """Return the SHA-256 of a report, excluding its own digest field."""

    payload = {key: item for key, item in value.items() if key != field_name}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def row_report(tables: Sequence[Mapping[str, object]], identity: Mapping[str, object]) -> dict[str, object]:
    """Assemble the machine-readable row-level report."""

    report: dict[str, object] = {
        "schema_version": ROW_REPORT_SCHEMA,
        "status": "PASS" if tables and all(item["passed"] for item in tables) else "FAIL",
        "identity": dict(identity),
        "tables": list(tables),
    }
    report["report_digest"] = digest(report, "report_digest")
    return report


def final_verdict(exact: Mapping[str, object], report: Mapping[str, object]) -> dict[str, object]:
    """Combine the exact gate with the row report without rewriting either.

    Tolerance equivalence is granted only when the exact gate's sole differences are
    content fingerprints of tables that the row report compared and passed. Any
    other difference, such as a schema, row count or invariant, stays a failure.

    Args:
        exact: The ``dp2-gold-equivalence.json`` document.
        report: Output of :func:`row_report`.

    Returns:
        A verdict that keeps ``exact_equivalent`` visible beside the new result.
    """

    if report.get("report_digest") != digest(report, "report_digest"):
        raise ValueError("row report digest mismatch")
    differences = [str(item) for item in exact.get("differences", [])]
    passed_tables = {str(item["table"]) for item in report["tables"] if item["passed"]}
    unresolved = [
        item
        for item in differences
        if not (
            item.endswith(f".{FINGERPRINT_DIFFERENCE}")
            and item.split(".", 1)[0] in passed_tables
        )
    ]
    if exact.get("output_equivalent") is True:
        status = "EXACT_EQUIVALENT"
    elif report["status"] == "PASS" and not unresolved and differences:
        status = "TOLERANCE_EQUIVALENT"
    else:
        status = "NOT_EQUIVALENT"
    verdict: dict[str, object] = {
        "schema_version": FINAL_VERDICT_SCHEMA,
        "status": status,
        "exact_equivalent": exact.get("output_equivalent") is True,
        "exact_differences": differences,
        "unresolved_differences": unresolved,
        "exact_artifact_digest": exact.get("equivalence_artifact_digest"),
        "row_report_digest": report["report_digest"],
        "adoption": "requires an explicit owner decision; this artifact adopts nothing",
    }
    verdict["verdict_digest"] = digest(verdict, "verdict_digest")
    return verdict
