"""Owner-side views of the storage benchmark: Trino SQL and the evidence summary.

``trino-sql``  renders, from ``plan.json``, the statements that register the three
               benchmark arms of each table in the read-only schema-viz Trino (schema
               ``delta.storage_bench``, local metastore only) and the read-only queries
               behind screenshots ST01, ST02, ST04, ST05, ST07 and ST09.
``summary``    validates a local copy of the job's ``evidence/`` folder against the plan
               and prints the rewrite, verdict and pruning tables (ST05, ST07, ST08).

Neither subcommand contacts GCP or Dataproc.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from ops.gcp.storage_benchmark_job import _query_sql

SCHEMA = "storage_bench"
ARMS = ("baseline", "compacted", "optimized")


def _relation(table: str, arm: str) -> str:
    return f"delta.{SCHEMA}.{table}_{arm}"


def registration_sql(plan: Mapping[str, Any]) -> str:
    """Register every arm of every planned table (writes only the local metastore)."""

    lines = ["-- Run as user `registrar` in the schema-viz Trino. Local metastore only; nothing is written to GCS.",
             f"CREATE SCHEMA IF NOT EXISTS delta.{SCHEMA};"]
    for table in plan["tables"]:
        for arm in ARMS:
            lines.append(f"CALL delta.system.register_table(schema_name => '{SCHEMA}', "
                         f"table_name => '{table['table']}_{arm}', table_location => '{table[f'{arm}_uri']}');")
    return "\n".join(lines) + "\n"


_ZONELESS_TIMESTAMP = re.compile(r"TIMESTAMP '(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})'")


def _trino(sql: str) -> str:
    """The job's Spark SQL in Trino syntax: Trino formats with ``format_datetime`` (Joda
    patterns), and ``timestamp`` is quoted because it is also a type keyword.

    The job runs with ``spark.sql.session.timeZone=UTC``. Trino reads the Delta column as
    ``timestamp(3) with time zone`` and interprets a zone-less literal in the client's
    session zone (DBeaver sends the desktop zone, e.g. +07:00), which would shift the
    window. Every literal and the formatted day are therefore pinned to UTC.
    """

    sql = sql.replace("date_format(timestamp, 'yyyy-MM-dd')",
                      "format_datetime(\"timestamp\" AT TIME ZONE 'UTC', 'yyyy-MM-dd')") \
             .replace("timestamp >= TIMESTAMP", "\"timestamp\" >= TIMESTAMP") \
             .replace("AND timestamp < TIMESTAMP", "AND \"timestamp\" < TIMESTAMP")
    return _ZONELESS_TIMESTAMP.sub(r"TIMESTAMP '\1 UTC'", sql)


def query_sql(plan: Mapping[str, Any]) -> str:
    """Read-only queries for the ST screenshots, one block per table."""

    lines = ["-- Run as user `viewer` in the schema-viz Trino. Read-only.",
             "-- Every timestamp literal and formatted day is pinned to UTC, so the results do not",
             "-- depend on the client session time zone (DBeaver sends the desktop zone)."]
    for table in plan["tables"]:
        name = str(table["table"])
        params = table["workload_parameters"]
        lines += ["", f"-- ===== {name} (source {table['uri']} @ Delta v{table['delta_version']}) =====",
                  f"-- ST01 identity: the clones' history shows the CLONE of the pinned source version"]
        lines += [f'SELECT version, timestamp, operation, operation_parameters FROM delta.{SCHEMA}."{name}_{arm}$history";'
                  for arm in ARMS]
        for screenshot, arm in (("ST02", "baseline"), ("ST04", "compacted"), ("ST04", "optimized")):
            lines += [f"-- {screenshot} file layout of the {arm} arm",
                      f'SELECT count(DISTINCT "$path") AS files, round(avg("$file_size") / 1048576.0, 2) AS avg_file_mib, '
                      f'min("$file_size") AS min_file_bytes, max("$file_size") AS max_file_bytes, '
                      f'count(DISTINCT event_date) AS partitions FROM {_relation(name, arm)};']
        lines += ["-- ST04 file locations: the baseline clone references the source run's files, "
                  "the Z-order arm's files are new objects under the benchmark root",
                  " UNION ALL ".join(f"SELECT '{arm}' AS arm, MIN(\"$path\") AS first_file, MAX(\"$path\") AS last_file "
                                     f"FROM {_relation(name, arm)}" for arm in ("baseline", "optimized")) + ";"]
        for screenshot, query in (("ST05/ST07", {"class": "date_entity_lookup"}),
                                  ("ST05/ST07", {"class": "date_range"})):
            for arm in ARMS:
                sql = _trino(_query_sql(table, query, relation="baseline_table")).replace(
                    "FROM baseline_table", f"FROM {_relation(name, arm)}")
                lines += [f"-- {screenshot} {query['class']} on {arm} (merchant {params['merchant_id']})",
                          f"EXPLAIN ANALYZE {sql};"]
        for side, cls in (("pruned", "date_range"), ("stats_only", "date_range_stats_only"),
                          ("full_scan", "date_range_full_scan")):
            sql = _trino(_query_sql(table, {"class": cls}, relation="baseline_table")).replace(
                "FROM baseline_table", f"FROM {_relation(name, 'baseline')}")
            # The business result first (equal on all three sides), then the plan statistics.
            lines += [f"-- ST09 result: {side}", f"{sql};",
                      f"-- ST09 partition pruning: {side}", f"EXPLAIN ANALYZE {sql};"]
    return "\n".join(lines) + "\n"


def summarize(plan: Mapping[str, Any], evidence_dir: Path) -> str:
    """Validate the downloaded evidence against the plan and render it as text.

    Raises:
        ValueError: The evidence belongs to another benchmark or did not pass.
    """

    comparison = json.loads((evidence_dir / "comparison.json").read_text(encoding="utf-8"))
    for key in ("benchmark_id", "dataset_version", "git_sha", "source_spark_git_sha"):
        if comparison.get(key) != plan.get(key):
            raise ValueError(f"evidence {key} {comparison.get(key)!r} differs from the plan {plan.get(key)!r}")
    if comparison.get("status") != "PASS":
        raise ValueError("the benchmark job did not complete with PASS")
    lines = [f"storage benchmark {plan['benchmark_id']}  spark={comparison['spark_version']}  "
             f"tool={plan['git_sha'][:12]}  source_spark={plan['source_spark_git_sha'][:12]}"]
    for table in comparison["tables"]:
        lines += ["", f"== {table['table']}  source v{table['source_delta_version']}  "
                      f"equivalence={all(item['equivalent'] for item in table['equivalence'].values())}"]
        for arm, rewrite in table["rewrite"].items():
            outcome = rewrite["outcome"]
            lines.append(f"  rewrite {arm:10} removed={outcome['files_removed']:>4} added={outcome['files_added']:>4} "
                         f"bytes_removed={outcome['bytes_removed']:,} no_op={outcome['no_op']} "
                         f"seconds={rewrite['seconds']:.1f}")
        for arm, items in table["comparisons"].items():
            lines.append(f"  {arm}: {table['verdicts'][arm]}")
            for item in items:
                lines.append(f"    {item['query']:26} {item['before_median_seconds']:.3f}s -> "
                             f"{item['after_median_seconds']:.3f}s ({item['runtime_change_percent']:+.1f}%) "
                             f"{item['verdict']}")
        for side, value in table["partition_pruning"]["sides"].items():
            scans = value["scan_metrics"]
            files = sum(scan.get("files_read") or 0 for scan in scans.get("scans", [])) if scans.get("available") else "n/a"
            lines.append(f"  pruning {side:10} median={value['runtime_seconds']:.3f}s files_read={files}")
        lines.append(f"  recommendation: {table['recommendation']}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    trino = commands.add_parser("trino-sql")
    trino.add_argument("--plan", type=Path, required=True)
    trino.add_argument("--output-dir", type=Path, required=True)
    summary = commands.add_parser("summary")
    summary.add_argument("--plan", type=Path, required=True)
    summary.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if args.command == "trino-sql":
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "storage_register.sql").write_text(registration_sql(plan), encoding="utf-8")
        (args.output_dir / "storage_queries.sql").write_text(query_sql(plan), encoding="utf-8")
        print(f"wrote {args.output_dir / 'storage_register.sql'} and {args.output_dir / 'storage_queries.sql'}")
    else:
        print(summarize(plan, args.evidence_dir), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
