"""Data profile, data-derived parameters, the benchmark query and the index design.

Nothing here is assumed from the historical synthetic benchmark: the window, the
merchant and the shape of the index are all derived from the loaded 5GB rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping

from . import TABLE
from .postgres import Psql

WINDOW_DAYS = 7
STATUS = "success"
INDEX_NAME = "idx_fact_transactions_merchant_date"
# A partial index (WHERE status = 'success') pays off only when it excludes a
# material share of rows; below this exclusion share a full covering index is used.
PARTIAL_INDEX_MIN_EXCLUDED_SHARE = 0.20
_SAFE_LITERAL = re.compile(r"^[A-Za-z0-9-]{1,64}$")


@dataclass(frozen=True)
class Parameters:
    """The benchmark predicates, all observed in the data."""

    window_start: str
    window_end: str
    merchant_id: str
    status: str
    expected_rows: int

    def __post_init__(self) -> None:
        date.fromisoformat(self.window_start)
        date.fromisoformat(self.window_end)
        if not _SAFE_LITERAL.fullmatch(self.merchant_id) or not _SAFE_LITERAL.fullmatch(self.status):
            raise ValueError("unexpected characters in a data-derived literal")


def database_profile(psql: Psql) -> dict[str, Any]:
    """Row count, date range, status shares and merchant cardinality in PostgreSQL."""

    rows, start, end, merchants = psql.scalar(
        f"SELECT count(*) || '|' || min(event_date) || '|' || max(event_date) || '|' || "
        f"count(DISTINCT merchant_id) FROM {TABLE};").split("|")
    statuses = dict(line.split("|") for line in psql.run(
        f"SELECT status, count(*) FROM {TABLE} GROUP BY status ORDER BY status;").splitlines())
    return {"rows": int(rows), "event_date_min": start, "event_date_max": end, "distinct_merchants": int(merchants),
            "status_counts": {key: int(value) for key, value in statuses.items()}}


def choose_parameters(psql: Psql, profile: Mapping[str, Any]) -> Parameters:
    """The last ``WINDOW_DAYS`` days and the merchant with the median success count in them."""

    end = date.fromisoformat(profile["event_date_max"])
    start = end - timedelta(days=WINDOW_DAYS - 1)
    counts = [line.split("|") for line in psql.run(
        f"SELECT merchant_id, count(*) FROM {TABLE} WHERE event_date BETWEEN DATE '{start}' AND DATE '{end}' "
        f"AND status = '{STATUS}' AND merchant_id IS NOT NULL GROUP BY merchant_id ORDER BY merchant_id;"
    ).splitlines()]
    if not counts:
        raise ValueError("the window holds no successful merchant transaction")
    ordered = sorted(int(value) for _, value in counts)
    target = ordered[(len(ordered) - 1) // 2]
    merchant, value = min(counts, key=lambda item: (abs(int(item[1]) - target), item[0]))
    return Parameters(str(start), str(end), merchant, STATUS, int(value))


def query_sql(parameters: Parameters) -> str:
    """WH-A query: one merchant's successful payments over the window."""

    return (
        "SELECT count(*) AS tx_count, round(sum(amount::numeric), 2) AS total_amount, "
        "round(avg(amount::numeric), 4) AS avg_amount "
        f"FROM {TABLE} "
        f"WHERE merchant_id = '{parameters.merchant_id}' "
        f"AND event_date BETWEEN DATE '{parameters.window_start}' AND DATE '{parameters.window_end}' "
        f"AND status = '{parameters.status}'"
    )


def index_design(profile: Mapping[str, Any], parameters: Parameters, *, table: str = TABLE,
                 name: str = INDEX_NAME) -> dict[str, Any]:
    """Index chosen from measured selectivity, with the reasoning recorded.

    ``merchant_id`` leads (equality, high cardinality), ``event_date`` follows (range),
    and ``status``/``amount`` are included so the query can be answered from the index.
    The partial predicate is used only if it excludes at least
    ``PARTIAL_INDEX_MIN_EXCLUDED_SHARE`` of the rows.
    """

    rows = int(profile["rows"])
    status_share = int(profile["status_counts"].get(parameters.status, 0)) / rows
    excluded = 1 - status_share
    partial = excluded >= PARTIAL_INDEX_MIN_EXCLUDED_SHARE
    include = "(amount)" if partial else "(status, amount)"
    predicate = f" WHERE status = '{parameters.status}'" if partial else ""
    sql = f"CREATE INDEX {name} ON {table} (merchant_id, event_date) INCLUDE {include}{predicate}"
    return {
        "name": name,
        "sql": sql + ";",
        "key_columns": ["merchant_id", "event_date"],
        "included_columns": ["amount"] if partial else ["status", "amount"],
        "predicate": predicate.strip() or None,
        "rationale": {
            "rows_per_merchant_average": round(rows / max(1, int(profile["distinct_merchants"])), 2),
            "query_rows_expected": parameters.expected_rows,
            "query_selectivity": round(parameters.expected_rows / rows, 8),
            f"{parameters.status}_share": round(status_share, 6),
            "partial_predicate_excluded_share": round(excluded, 6),
            "partial_index_used": partial,
            "rule": f"partial predicate only if it excludes >= {PARTIAL_INDEX_MIN_EXCLUDED_SHARE:.0%} of rows",
        },
    }


# amount is double precision: summing it in plan-dependent (parallel) order is not
# exact, so every aggregate converts each value to numeric first.
SIGNATURE_SQL = (
    f"SELECT count(*) || '|' || round(sum(amount::numeric), 2) || '|' || min(transaction_id) || '|' || "
    f"max(transaction_id) || '|' || count(DISTINCT merchant_id) || '|' || "
    f"md5(string_agg(transaction_id, ',' ORDER BY transaction_id)) FROM {{table}};"
)


def table_signature(psql: Psql, table: str = TABLE) -> str:
    """Order-independent content signature used for the WH-D no-mutation check."""

    return psql.scalar(SIGNATURE_SQL.format(table=table))
