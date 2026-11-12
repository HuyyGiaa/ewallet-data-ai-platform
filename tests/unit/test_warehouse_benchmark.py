"""Warehouse benchmark logic without a database (a fake psql records the SQL)."""

from __future__ import annotations

import json
import subprocess

import pytest

from data_platform.storage.warehouse import benchmark, dbeaver
from data_platform.storage.warehouse.postgres import Psql, plan_summary
from data_platform.storage.warehouse.schema import create_table_sql, postgres_type
from data_platform.storage.warehouse.source import file_inventory, select_partitions
from data_platform.storage.warehouse.workload import (
    SIGNATURE_SQL,
    Parameters,
    choose_parameters,
    index_design,
    query_sql,
)

PROFILE = {"rows": 3_816_574, "event_date_min": "2026-05-01", "event_date_max": "2026-05-31",
           "distinct_merchants": 55_623, "status_counts": {"failed": 132_152, "pending": 73_712, "success": 3_610_710}}
PARAMS = Parameters("2026-05-25", "2026-05-31", "00058dec-9436-4664-91e0-3e1d5829e22b", "success", 2)


class FakePsql(Psql):
    """Answers by the first matching SQL fragment; records every statement."""

    def __init__(self, answers: dict[str, str]):
        super().__init__(runner=self._run)
        self.answers = answers
        self.statements: list[str] = []

    def _run(self, args, input=None, **_):
        self.statements.append(input or "")
        for fragment, answer in self.answers.items():
            if fragment in (input or ""):
                return subprocess.CompletedProcess(args, 0, answer, "")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_sample_is_whole_recent_partitions():
    assert select_partitions(["2026-05-02", "2026-05-01", "2026-05-03", "2026-05-03"], 2) == ["2026-05-02", "2026-05-03"]
    with pytest.raises(ValueError):
        select_partitions(["2026-05-01"], 2)
    actions = {"path": ["b", "a", "c"], "size_bytes": [1, 2, 3], "num_records": [10, 20, 30],
               "partition.event_date": ["2026-05-02", "2026-05-02", "2026-05-01"]}
    files = file_inventory(actions, ["2026-05-02"])
    assert [item["path"] for item in files] == ["a", "b"] and sum(i["delta_num_records"] for i in files) == 30


def test_schema_mapping_is_explicit_and_keeps_only_the_primary_key():
    assert postgres_type("timestamp[us, tz=UTC]") == "timestamptz"
    with pytest.raises(ValueError):
        postgres_type("decimal128(10, 2)")
    ddl = create_table_sql([("transaction_id", "string"), ("amount", "double"), ("event_date", "date32[day]")])
    assert "transaction_id text NOT NULL" in ddl and "PRIMARY KEY (transaction_id)" in ddl
    assert "INDEX" not in ddl


def test_parameters_pick_the_median_merchant_in_the_last_week():
    rows = "\n".join(["a|1", "b|2", "c|2", "d|50"])
    psql = FakePsql({"GROUP BY merchant_id": rows})
    chosen = choose_parameters(psql, PROFILE)
    assert (chosen.window_start, chosen.window_end, chosen.merchant_id, chosen.expected_rows) == \
        ("2026-05-25", "2026-05-31", "b", 2)
    with pytest.raises(ValueError):
        Parameters("2026-05-25", "2026-05-31", "x'; DROP TABLE t; --", "success", 1)


def test_index_design_uses_partial_predicate_only_when_it_excludes_enough_rows():
    full = index_design(PROFILE, PARAMS)
    assert full["predicate"] is None and full["included_columns"] == ["status", "amount"]
    assert full["sql"] == ("CREATE INDEX idx_fact_transactions_merchant_date ON fact_transactions "
                           "(merchant_id, event_date) INCLUDE (status, amount);")
    skewed = dict(PROFILE, status_counts={"success": 1_000_000, "failed": 2_816_574})
    partial = index_design(skewed, PARAMS)
    assert partial["predicate"] == "WHERE status = 'success'" and partial["included_columns"] == ["amount"]


def test_query_and_signature_aggregate_exactly():
    sql = query_sql(PARAMS)
    assert "sum(amount::numeric)" in sql and "avg(amount::numeric)" in sql
    assert "merchant_id = '00058dec-9436-4664-91e0-3e1d5829e22b'" in sql
    assert "sum(amount::numeric)" in SIGNATURE_SQL and "string_agg(transaction_id" in SIGNATURE_SQL


def test_plan_summary_reads_access_path_and_buffers():
    explain = {"Planning Time": 0.1, "Execution Time": 0.05, "Plan": {
        "Node Type": "Aggregate", "Shared Hit Blocks": 4, "Plans": [
            {"Node Type": "Index Only Scan", "Index Name": "idx", "Heap Fetches": 0}]}}
    summary = plan_summary(explain)
    assert summary["access_paths"] == ["Index Only Scan:idx"] and summary["shared_hit_blocks"] == 4


def test_before_phase_refuses_an_existing_secondary_index():
    psql = FakePsql({"FROM pg_indexes": "1"})
    with pytest.raises(benchmark.BenchmarkError, match="secondary index"):
        benchmark.phase_before(psql)


def test_index_created_in_dbeaver_must_match_the_design():
    design = index_design(PROFILE, PARAMS)
    psql = FakePsql({"FROM pg_indexes WHERE indexname": "1",
                     "pg_get_indexdef": "CREATE INDEX idx_fact_transactions_merchant_date ON public.fact_transactions "
                                        "USING btree (merchant_id, event_date) INCLUDE (status, amount)",
                     "pg_relation_size": "1024"})
    recorded = benchmark.create_index(psql, design, execute=False)
    assert recorded["create_ms"] is None and recorded["created_by"].startswith("owner in DBeaver")
    wrong = FakePsql({"FROM pg_indexes WHERE indexname": "1",
                      "pg_get_indexdef": "CREATE INDEX idx_fact_transactions_merchant_date ON public.fact_transactions "
                                         "USING btree (merchant_id)", "pg_relation_size": "1"})
    with pytest.raises(benchmark.BenchmarkError, match="differs from the design"):
        benchmark.create_index(wrong, design, execute=False)
    absent = FakePsql({"FROM pg_indexes WHERE indexname": "0"})
    with pytest.raises(benchmark.BenchmarkError, match="does not exist"):
        benchmark.create_index(absent, design, execute=False)


def test_measured_runs_keep_every_sample():
    plan = json.dumps([{"Planning Time": 0.1, "Execution Time": 2.0, "Plan": {"Node Type": "Seq Scan",
                                                                              "Relation Name": "t"}}])
    psql = FakePsql({"EXPLAIN": plan})
    result = benchmark.measure_query(psql, "SELECT 1", warmups=1, repetitions=5)
    assert len(result["runs"]) == 5 and result["execution_ms"]["median"] == 2.0
    assert sum("EXPLAIN" in statement for statement in psql.statements) == 6


def test_interleaving_alternates_which_side_runs_first():
    plan = json.dumps([{"Planning Time": 0.1, "Execution Time": 1.0, "Plan": {"Node Type": "Seq Scan"}}])
    psql = FakePsql({"EXPLAIN": plan})
    benchmark.interleaved(psql, "SELECT 1", pairs=2)
    disabled = ["enable_indexscan = off" in statement for statement in psql.statements]
    assert disabled == [True, False, False, True]


def test_dbeaver_session_has_every_checkpoint_in_order():
    before = {"query": query_sql(PARAMS), "design": index_design(PROFILE, PARAMS), "parameters": PARAMS.__dict__}
    text = dbeaver.render(before)
    positions = [text.index(f"-- WH0{number}") for number in range(1, 7)]
    assert positions == sorted(positions)
    assert text.index("EXPLAIN (ANALYZE, BUFFERS)") < text.index("CREATE INDEX idx_fact_transactions_merchant_date")
    assert "127.0.0.1, port 55432" in text
