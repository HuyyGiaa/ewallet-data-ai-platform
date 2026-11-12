"""WH-A read, WH-B index, WH-C write and WH-D equivalence experiments.

* WH-A runs the query with no secondary index (``warmups`` then ``repetitions``
  EXPLAIN ANALYZE), then with the index. Because before/after on one table also
  differs in cache state, it adds an interleaved comparison on the same warm table:
  each pair runs once with index scans disabled for the planner and once enabled,
  alternating which side goes first.
* WH-B records the CREATE INDEX statement, its duration, definition, size and the
  access path the planner chooses.
* WH-C measures maintenance cost: two empty twin tables (primary key only, and
  primary key plus the index) receive the same deterministic batches of real rows
  in alternating order; execution time and WAL bytes are recorded per batch.
* WH-D compares table signatures before and after indexing and the query results of
  every arm; any difference fails the run.

The experiments run in three phases so the index can be created in DBeaver (WH03):
``phase_before`` (no secondary index), ``phase_index`` (create, or verify an index the
owner created) and ``phase_after``. Every measured run is also stored in the
``benchmark_runs`` table, which DBeaver queries for WH05 and WH06.

Verdicts use ``classify_runtime_change`` (a descriptive noise-band rule, not a
significance test), the same rule as the lakehouse benchmark.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any, Sequence

from data_platform.storage.cloud_optimization import classify_runtime_change

from . import REPORT_SCHEMA, TABLE
from .postgres import Psql, plan_summary
from .workload import choose_parameters, database_profile, index_design, query_sql, table_signature

DISABLE_INDEX = ("SET enable_indexscan = off", "SET enable_bitmapscan = off", "SET enable_indexonlyscan = off")
WRITE_BATCHES = 8
WRITE_BATCH_MODULUS = 50  # each batch is the rows with abs(hashtext(transaction_id)) % 50 = b


class BenchmarkError(RuntimeError):
    """A semantic check of the benchmark failed."""


def _samples(runs: Sequence[dict[str, Any]], key: str = "execution_ms") -> list[float]:
    return [float(run[key]) for run in runs]


def _stats(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(values)
    quartiles = statistics.quantiles(ordered, n=4) if len(ordered) >= 4 else [ordered[0], ordered[-1], ordered[-1]]
    return {"median": round(statistics.median(ordered), 4), "minimum": ordered[0], "maximum": ordered[-1],
            "q1": round(quartiles[0], 4), "q3": round(quartiles[2], 4), "n": len(ordered)}


def measure_query(psql: Psql, sql: str, *, warmups: int, repetitions: int,
                  settings: Sequence[str] = ()) -> dict[str, Any]:
    """Warm up, then EXPLAIN ANALYZE ``repetitions`` times; keep every run."""

    for _ in range(warmups):
        psql.explain(sql, settings=settings)
    runs, plans = [], []
    for _ in range(repetitions):
        explain = psql.explain(sql, settings=settings)
        runs.append(plan_summary(explain))
        plans.append(explain)
    return {"warmups": warmups, "runs": runs, "execution_ms": _stats(_samples(runs)),
            "representative_plan": plans[len(plans) // 2]}


def interleaved(psql: Psql, sql: str, *, pairs: int) -> dict[str, Any]:
    """Alternate index-disabled and index-enabled executions on the same warm table."""

    disabled, enabled = [], []
    for pair in range(pairs):
        order = (("disabled", DISABLE_INDEX), ("enabled", ())) if pair % 2 == 0 else \
                (("enabled", ()), ("disabled", DISABLE_INDEX))
        for side, settings in order:
            summary = plan_summary(psql.explain(sql, settings=settings))
            (disabled if side == "disabled" else enabled).append(summary)
    return {"pairs": pairs, "order": "alternating per pair", "index_disabled": disabled, "index_enabled": enabled,
            "comparison": classify_runtime_change(_samples(disabled), _samples(enabled))}


RUNS_DDL = """CREATE TABLE IF NOT EXISTS benchmark_runs (
    experiment text NOT NULL,
    side text NOT NULL,
    sequence integer NOT NULL,
    execution_ms double precision NOT NULL,
    planning_ms double precision,
    access_path text,
    shared_hit_blocks bigint,
    shared_read_blocks bigint,
    wal_bytes bigint,
    PRIMARY KEY (experiment, side, sequence)
);"""


def record_runs(psql: Psql, experiment: str, side: str, runs: Sequence[dict[str, Any]]) -> None:
    """Store measured runs in ``benchmark_runs`` (inserts only; never touches the fact table)."""

    psql.run(RUNS_DDL, tuples=False)
    rows = []
    for sequence, run_ in enumerate(runs):
        path = ";".join(run_.get("access_paths", [])).replace("'", "''")
        rows.append(f"('{experiment}', '{side}', {sequence}, {float(run_['execution_ms'])}, "
                    f"{run_.get('planning_ms') if run_.get('planning_ms') is not None else 'NULL'}, '{path}', "
                    f"{run_.get('shared_hit_blocks', 'NULL')}, {run_.get('shared_read_blocks', 'NULL')}, "
                    f"{run_.get('wal_bytes', 'NULL')})")
    if rows:
        psql.run("INSERT INTO benchmark_runs VALUES\n" + ",\n".join(rows) + ";", tuples=False)


def _normalized(definition: str) -> str:
    return " ".join(definition.replace("public.", "").replace(" USING btree", "").split()).rstrip(";").lower()


def create_index(psql: Psql, design: dict[str, Any], *, execute: bool = True) -> dict[str, Any]:
    """Create the index (or verify one created in DBeaver) and report it (WH-B).

    Raises:
        BenchmarkError: An index created outside the tool differs from the design.
    """

    name = design["name"]
    exists = psql.scalar(f"SELECT count(*) FROM pg_indexes WHERE indexname = '{name}';") == "1"
    if execute:
        if exists:
            raise BenchmarkError(f"{name} already exists; use --already-created to record it")
        milliseconds: float | None = round(psql.timed(design["sql"]), 3)
        created_by = "benchmark CLI (psql \\timing)"
    else:
        if not exists:
            raise BenchmarkError(f"{name} does not exist; create it first or omit --already-created")
        milliseconds, created_by = None, "owner in DBeaver (duration shown in DBeaver's statistics tab)"
    definition = psql.scalar(f"SELECT pg_get_indexdef('{name}'::regclass);")
    if _normalized(definition) != _normalized(design["sql"]):
        raise BenchmarkError(f"index definition differs from the design: {definition}")
    psql.run(f"VACUUM (ANALYZE) {TABLE};", tuples=False)
    return {
        **design,
        "created_by": created_by,
        "create_ms": milliseconds,
        "definition": definition,
        "size_bytes": int(psql.scalar(f"SELECT pg_relation_size('{name}');")),
        "table_size_bytes": int(psql.scalar(f"SELECT pg_relation_size('{TABLE}');")),
        "primary_key_size_bytes": int(psql.scalar(f"SELECT pg_relation_size('{TABLE}_pkey');")),
    }


def write_cost(psql: Psql, design: dict[str, Any]) -> dict[str, Any]:
    """WH-C: insert the same real-row batches into twin tables, alternating order."""

    twin_index = design["sql"].replace(f"INDEX {design['name']} ON {TABLE}", "INDEX ingest_with_index_idx ON ingest_with_index")
    psql.run(
        "DROP TABLE IF EXISTS ingest_without_index, ingest_with_index;\n"
        f"CREATE TABLE ingest_without_index (LIKE {TABLE} INCLUDING DEFAULTS INCLUDING CONSTRAINTS);\n"
        "ALTER TABLE ingest_without_index ADD PRIMARY KEY (transaction_id);\n"
        f"CREATE TABLE ingest_with_index (LIKE {TABLE} INCLUDING DEFAULTS INCLUDING CONSTRAINTS);\n"
        "ALTER TABLE ingest_with_index ADD PRIMARY KEY (transaction_id);\n"
        f"{twin_index}\nCHECKPOINT;", tuples=False)
    batches = []
    for batch in range(WRITE_BATCHES):
        source = (f"SELECT * FROM {TABLE} WHERE abs(hashtext(transaction_id)) % {WRITE_BATCH_MODULUS} = {batch}")
        order = ("ingest_without_index", "ingest_with_index") if batch % 2 == 0 else \
                ("ingest_with_index", "ingest_without_index")
        record: dict[str, Any] = {"batch": batch, "order": list(order)}
        for target in order:
            before = psql.scalar("SELECT pg_current_wal_lsn();")
            explain = psql.explain(f"INSERT INTO {target} {source}")
            after = psql.scalar("SELECT pg_current_wal_lsn();")
            wal = int(psql.scalar(f"SELECT pg_wal_lsn_diff('{after}', '{before}');"))
            record[target] = {"execution_ms": explain["Execution Time"], "wal_bytes": wal,
                              "rows": explain["Plan"].get("Plans", [{}])[0].get("Actual Rows")}
        batches.append(record)
    without = [item["ingest_without_index"]["execution_ms"] for item in batches]
    with_index = [item["ingest_with_index"]["execution_ms"] for item in batches]
    wal_without = sum(item["ingest_without_index"]["wal_bytes"] for item in batches)
    wal_with = sum(item["ingest_with_index"]["wal_bytes"] for item in batches)
    signatures = {name: table_signature(psql, name) for name in ("ingest_without_index", "ingest_with_index")}
    if signatures["ingest_without_index"] != signatures["ingest_with_index"]:
        raise BenchmarkError("the twin ingest tables differ after identical inserts")
    return {
        "batches": batches,
        "batch_rule": f"abs(hashtext(transaction_id)) % {WRITE_BATCH_MODULUS} = batch, batch 0..{WRITE_BATCHES - 1}",
        "rows_inserted_per_table": int(psql.scalar("SELECT count(*) FROM ingest_with_index;")),
        "execution_ms": {"without_index": _stats(without), "with_index": _stats(with_index)},
        "comparison": classify_runtime_change(without, with_index),
        "wal_bytes": {"without_index": wal_without, "with_index": wal_with,
                      "overhead_percent": round((wal_with - wal_without) / wal_without * 100, 2) if wal_without else None},
        "twin_signature": signatures["ingest_with_index"],
        "index_size_bytes": int(psql.scalar("SELECT pg_relation_size('ingest_with_index_idx');")),
    }


def phase_before(psql: Psql, *, warmups: int = 2, repetitions: int = 11) -> dict[str, Any]:
    """WH-A part 1: profile, parameters, integrity signature and the no-index runs."""

    existing = psql.scalar(f"SELECT count(*) FROM pg_indexes WHERE tablename = '{TABLE}' "
                           f"AND indexname <> '{TABLE}_pkey';")
    if existing != "0":
        raise BenchmarkError("the benchmark table already has a secondary index; reload a fresh volume")
    profile = database_profile(psql)
    parameters = choose_parameters(psql, profile)
    sql = query_sql(parameters)
    before = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "parameters": parameters.__dict__,
        "query": sql,
        "design": index_design(profile, parameters),
        "table_signature_before": table_signature(psql),
        "query_result_without_index": psql.run(sql + ";"),
        "without_index": measure_query(psql, sql, warmups=warmups, repetitions=repetitions),
    }
    record_runs(psql, "read", "without_index", before["without_index"]["runs"])
    return before


def phase_after(psql: Psql, before: dict[str, Any], index: dict[str, Any], *, warmups: int = 2,
                repetitions: int = 11, pairs: int = 10) -> dict[str, Any]:
    """WH-A part 2, WH-C and WH-D; returns the complete result document."""

    sql = before["query"]
    result_after = psql.run(sql + ";")
    optimized = measure_query(psql, sql, warmups=warmups, repetitions=repetitions)
    record_runs(psql, "read", "with_index", optimized["runs"])
    fairness = interleaved(psql, sql, pairs=pairs)
    record_runs(psql, "interleaved", "index_disabled", fairness["index_disabled"])
    record_runs(psql, "interleaved", "index_enabled", fairness["index_enabled"])
    signature_after = table_signature(psql)
    writes = write_cost(psql, before["design"])
    for side in ("ingest_without_index", "ingest_with_index"):
        record_runs(psql, "write", side, [item[side] for item in writes["batches"]])
    semantic = {
        "table_signature_before": before["table_signature_before"],
        "table_signature_after": signature_after,
        "query_result_without_index": before["query_result_without_index"],
        "query_result_with_index": result_after,
        "passed": (before["table_signature_before"] == signature_after
                   and before["query_result_without_index"] == result_after),
    }
    if not semantic["passed"]:
        raise BenchmarkError("indexing changed the table or the query result")
    return {
        "schema_version": REPORT_SCHEMA,
        "started_at": before["started_at"],
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "postgres_version": psql.scalar("SHOW server_version;"),
        "settings": {name: psql.scalar(f"SHOW {name};") for name in
                     ("shared_buffers", "work_mem", "max_parallel_workers_per_gather", "jit", "random_page_cost")},
        "profile": before["profile"],
        "parameters": before["parameters"],
        "query": sql,
        "wh_a_read": {
            "without_index": before["without_index"],
            "with_index": optimized,
            "before_after": classify_runtime_change(_samples(before["without_index"]["runs"]),
                                                    _samples(optimized["runs"])),
            "interleaved": fairness,
        },
        "wh_b_index": index,
        "wh_c_write": writes,
        "wh_d_semantics": semantic,
    }
