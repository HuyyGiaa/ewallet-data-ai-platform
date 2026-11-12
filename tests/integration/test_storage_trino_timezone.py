"""The generated ST09 Trino predicates, executed by a real Trino under two session zones.

The benchmark window is ``[2026-05-25T00:00Z, 2026-06-01T00:00Z)``. ``event_date`` is the
UTC day of ``timestamp`` in the campaign tables, so the partition predicate, the
timestamp-range predicate and the formatted-day predicate must select the same rows in
any client session zone. The rows below sit on both window edges and on the edges of the
window shifted to UTC+7, which is what a zone-less literal produced from DBeaver.

The statements read only inline ``VALUES``: no table, catalog or cloud data is touched.
The test needs the schema-viz Trino container (``EDAI1_TRINO_CONTAINER``, default
``edai1-schema-viz-trino``) and is skipped without it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest

from ops.gcp.storage_benchmark_report import query_sql

CONTAINER = os.environ.get("EDAI1_TRINO_CONTAINER", "edai1-schema-viz-trino")
ZONES = ("UTC", "Asia/Ho_Chi_Minh")
TABLES = ("fact_transactions", "obt_transaction_enriched")
PARAMETERS = {"window_start": "2026-05-25", "window_end": "2026-05-31", "merchant_id": "m-0001"}

# (timestamp in UTC, event_date = its UTC day, inside the UTC window?)
BOUNDARY_ROWS = (
    ("2026-05-24 16:59:59.999", "2026-05-24", False),
    ("2026-05-24 17:00:00.000", "2026-05-24", False),  # start of the window shifted to UTC+7
    ("2026-05-24 18:00:00.000", "2026-05-24", False),
    ("2026-05-24 23:59:59.999", "2026-05-24", False),
    ("2026-05-25 00:00:00.000", "2026-05-25", True),   # window start (inclusive)
    ("2026-05-31 16:59:59.999", "2026-05-31", True),
    ("2026-05-31 17:00:00.000", "2026-05-31", True),   # end of the window shifted to UTC+7
    ("2026-05-31 23:59:59.999", "2026-05-31", True),
    ("2026-06-01 00:00:00.000", "2026-06-01", False),  # window end (exclusive)
)
EXPECTED = sum(inside for *_, inside in BOUNDARY_ROWS)


def _trino_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "inspect", "-f", "{{.State.Health.Status}}", CONTAINER],
                           capture_output=True, text=True, check=False)
    return probe.returncode == 0 and probe.stdout.strip() == "healthy"


def _count(sql: str, zone: str) -> int:
    completed = subprocess.run(
        ["docker", "exec", CONTAINER, "trino", "--user", "viewer", f"--timezone={zone}",
         "--output-format", "CSV", "--execute", sql],
        capture_output=True, text=True, check=False, timeout=120)
    if completed.returncode != 0:
        raise AssertionError(f"trino failed: {completed.stderr.strip()[:400]}")
    return int(completed.stdout.strip().strip('"'))


def _st09_where_clauses(table: str) -> dict[str, str]:
    plan = {"tables": [{"table": table, "uri": "gs://b/t", "delta_version": 0,
                        "workload_parameters": PARAMETERS}]}
    script = query_sql(plan)
    clauses = {}
    for side in ("pruned", "stats_only", "full_scan"):
        statement = script.split(f"-- ST09 partition pruning: {side}\n", 1)[1].splitlines()[0]
        match = re.search(rf"FROM delta\.storage_bench\.{table}_baseline WHERE (.*);$", statement)
        assert match, statement
        clauses[side] = match.group(1)
    return clauses


def _boundary_query(where: str) -> str:
    rows = ", ".join(f"(TIMESTAMP '{ts} UTC', DATE '{day}', 1.0E0)" for ts, day, _ in BOUNDARY_ROWS)
    return (f'WITH boundary("timestamp", event_date, amount) AS (VALUES {rows}) '
            f"SELECT count(*) FROM boundary WHERE {where}")


@unittest.skipUnless(_trino_available(), f"Trino container {CONTAINER} is not running")
class GeneratedSt09PredicatesInTrino(unittest.TestCase):
    def test_all_three_sides_select_the_utc_window_in_every_session_zone(self) -> None:
        for table in TABLES:
            for side, where in _st09_where_clauses(table).items():
                for zone in ZONES:
                    with self.subTest(table=table, side=side, zone=zone):
                        self.assertEqual(_count(_boundary_query(where), zone), EXPECTED)

    def test_the_former_zone_less_literal_shifts_with_the_session_zone(self) -> None:
        # The predicate the 2026-11-13 capture ran; kept here to prove the root cause.
        legacy = ("\"timestamp\" >= TIMESTAMP '2026-05-25 00:00:00' "
                  "AND \"timestamp\" < TIMESTAMP '2026-06-01 00:00:00'")
        self.assertEqual(_count(_boundary_query(legacy), "UTC"), EXPECTED)
        self.assertNotEqual(_count(_boundary_query(legacy), "Asia/Ho_Chi_Minh"), EXPECTED)


if __name__ == "__main__":
    unittest.main()
