"""Run a reproducible baseline/indexed PostgreSQL warehouse benchmark.

The runner creates one exact-name, tmpfs-backed Docker container and always
removes it.  It never connects to the project MLflow database or any cloud
database.  Query plans are captured as JSON and semantic signatures are
checked before an improvement is reported.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


QUERY = """
SELECT count(*) AS tx_count, round(sum(amount), 2) AS total_amount,
       round(avg(amount), 2) AS avg_amount
FROM fact_transactions
WHERE event_date BETWEEN DATE '2026-06-01' AND DATE '2026-06-30'
  AND merchant_id = 42
  AND status = 'completed'
""".strip()


@dataclass
class Runner:
    container: str

    def command(self, args: list[str], *, input_text: str | None = None) -> str:
        completed = subprocess.run(
            args,
            input=input_text,
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"command failed ({completed.returncode}): {' '.join(args)}\n"
                f"stderr: {completed.stderr.strip()}\n"
                f"stdout: {completed.stdout.strip()}"
            )
        return completed.stdout.strip()

    def psql(
        self,
        sql: str,
        *,
        tuples: bool = True,
        database: str = "warehouse",
    ) -> str:
        args = ["docker", "exec", "-i", self.container, "psql", "-X", "-v", "ON_ERROR_STOP=1"]
        if tuples:
            args += ["-A", "-t"]
        args += ["-U", "postgres", "-d", database]
        return self.command(args, input_text=sql)

    def explain(self, sql: str) -> dict[str, object]:
        value = json.loads(self.psql(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql};"))
        return value[0]


def _metric(explain: dict[str, object]) -> dict[str, object]:
    plan = explain["Plan"]
    return {
        "planning_ms": explain["Planning Time"],
        "execution_ms": explain["Execution Time"],
        "root_node": plan["Node Type"],
        "shared_hit_blocks": plan.get("Shared Hit Blocks", 0),
        "shared_read_blocks": plan.get("Shared Read Blocks", 0),
    }


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 4)


def run_benchmark(
    output: Path,
    *,
    rows: int = 2_000_000,
    repeats: int = 7,
    image: str = "postgres:16",
    container: str = "edai1-warehouse-benchmark-20261029",
) -> dict[str, object]:
    if rows < 100_000 or repeats < 3:
        raise ValueError("rows must be >= 100000 and repeats must be >= 3")
    runner = Runner(container)
    started = datetime.now(timezone.utc).isoformat()
    image_id = runner.command(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    runner.command([
        "docker", "run", "--rm", "--detach", "--name", container,
        "--tmpfs", "/var/lib/postgresql/data:rw,noexec,nosuid,size=6g",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", image,
    ])
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["docker", "exec", container, "pg_isready", "-U", "postgres"],
                text=True,
                capture_output=True,
            )
            if probe.returncode == 0:
                # The official image briefly starts a bootstrap server before
                # replacing it with the final server.  Avoid that race.
                time.sleep(1.0)
                probe = subprocess.run(
                    ["docker", "exec", container, "pg_isready", "-U", "postgres"],
                    text=True,
                    capture_output=True,
                )
                if probe.returncode != 0:
                    continue
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("isolated PostgreSQL did not become ready")

        runner.psql("CREATE DATABASE warehouse;", tuples=False, database="postgres")
        runner.psql(f"""
            CREATE TABLE fact_transactions (
                transaction_id bigint PRIMARY KEY,
                event_date date NOT NULL,
                merchant_id integer NOT NULL,
                account_id integer NOT NULL,
                status text NOT NULL,
                amount numeric(14,2) NOT NULL
            );
            INSERT INTO fact_transactions
            SELECT value,
                   DATE '2026-01-01' + ((value::bigint * 17) % 365)::integer,
                   CASE WHEN value % 20 = 0 THEN 42 ELSE (value % 10000)::integer END,
                   (value % 250000)::integer,
                   CASE WHEN value % 11 = 0 THEN 'failed' ELSE 'completed' END,
                   ((value::bigint * 7919) % 500000)::numeric / 100
            FROM generate_series(1, {rows}) value;
            ANALYZE fact_transactions;
        """, tuples=False)

        signature_before = runner.psql(
            "SELECT count(*) || '|' || round(sum(amount),2) || '|' || "
            "min(transaction_id) || '|' || max(transaction_id) FROM fact_transactions;"
        )
        expected = runner.psql(QUERY + ";")
        runner.explain(QUERY)  # warmup
        baseline_plan = runner.explain(QUERY)
        baseline = [_metric(runner.explain(QUERY)) for _ in range(repeats)]

        runner.psql("""
            CREATE INDEX idx_fact_transactions_merchant_date_completed
            ON fact_transactions (merchant_id, event_date)
            INCLUDE (amount)
            WHERE status = 'completed';
            ANALYZE fact_transactions;
        """, tuples=False)
        actual = runner.psql(QUERY + ";")
        signature_after = runner.psql(
            "SELECT count(*) || '|' || round(sum(amount),2) || '|' || "
            "min(transaction_id) || '|' || max(transaction_id) FROM fact_transactions;"
        )
        runner.explain(QUERY)  # warmup
        optimized_plan = runner.explain(QUERY)
        optimized = [_metric(runner.explain(QUERY)) for _ in range(repeats)]

        runner.psql("""
            CREATE UNLOGGED TABLE ingest_baseline (LIKE fact_transactions INCLUDING DEFAULTS);
            CREATE UNLOGGED TABLE ingest_indexed (LIKE fact_transactions INCLUDING DEFAULTS);
            CREATE INDEX idx_ingest_indexed_merchant_date_completed
            ON ingest_indexed (merchant_id, event_date) INCLUDE (amount)
            WHERE status = 'completed';
        """, tuples=False)
        insert_select = """
            SELECT value, DATE '2026-01-01' + (value % 365)::integer,
                   (value % 10000)::integer, (value % 250000)::integer,
                   CASE WHEN value % 11 = 0 THEN 'failed' ELSE 'completed' END,
                   ((value::bigint * 7919) % 500000)::numeric / 100
            FROM generate_series(1, 100000) value
        """.strip()
        write_baseline = _metric(runner.explain(f"INSERT INTO ingest_baseline {insert_select}"))
        write_indexed = _metric(runner.explain(f"INSERT INTO ingest_indexed {insert_select}"))

        baseline_median = _median([float(item["execution_ms"]) for item in baseline])
        optimized_median = _median([float(item["execution_ms"]) for item in optimized])
        semantic_equal = expected == actual and signature_before == signature_after
        payload = {
            "scope": "isolated local PostgreSQL container; no cloud or MLflow database access",
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "container": container,
            "image": image,
            "image_id": image_id,
            "dataset": {"rows": rows, "generator": "deterministic generate_series"},
            "query": QUERY,
            "repeats": repeats,
            "warmups_per_variant": 1,
            "semantic_equivalence": {
                "passed": semantic_equal,
                "query_result_before": expected,
                "query_result_after": actual,
                "table_signature_before": signature_before,
                "table_signature_after": signature_after,
            },
            "baseline": {
                "runs": baseline,
                "median_execution_ms": baseline_median,
                "representative_explain_analyze_buffers": baseline_plan,
            },
            "optimized": {
                "change": "partial covering index on (merchant_id,event_date) for completed rows",
                "runs": optimized,
                "median_execution_ms": optimized_median,
                "representative_explain_analyze_buffers": optimized_plan,
                "index_definition": runner.psql(
                    "SELECT pg_get_indexdef("
                    "'idx_fact_transactions_merchant_date_completed'::regclass);"
                ),
                "index_size": runner.psql(
                    "SELECT pg_size_pretty(pg_relation_size(" 
                    "'idx_fact_transactions_merchant_date_completed'));"
                ),
            },
            "read_improvement_percent": round(
                (baseline_median - optimized_median) / baseline_median * 100, 2
            ),
            "write_tradeoff": {
                "rows": 100000,
                "without_secondary_index": write_baseline,
                "with_secondary_index": write_indexed,
                "overhead_percent": round(
                    (float(write_indexed["execution_ms"]) - float(write_baseline["execution_ms"]))
                    / float(write_baseline["execution_ms"]) * 100,
                    2,
                ),
            },
        }
        if not semantic_equal:
            raise RuntimeError("semantic equivalence failed")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container],
            text=True,
            capture_output=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=2_000_000)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--image", default="postgres:16")
    parser.add_argument("--container", default="edai1-warehouse-benchmark-20261029")
    args = parser.parse_args(argv)
    payload = run_benchmark(
        args.output,
        rows=args.rows,
        repeats=args.repeats,
        image=args.image,
        container=args.container,
    )
    print(json.dumps({
        "semantic_equivalence": payload["semantic_equivalence"]["passed"],
        "baseline_median_ms": payload["baseline"]["median_execution_ms"],
        "optimized_median_ms": payload["optimized"]["median_execution_ms"],
        "read_improvement_percent": payload["read_improvement_percent"],
        "write_overhead_percent": payload["write_tradeoff"]["overhead_percent"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
