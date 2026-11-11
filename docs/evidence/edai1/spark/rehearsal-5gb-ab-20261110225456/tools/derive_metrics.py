"""Derive the rehearsal benchmark's supplementary metrics from downloaded inputs (offline).

Inputs, all read-only copies:
  --event-logs DIR      the 16 ``app-*.zstd`` Spark event logs (gs://<bucket>/spark-events/)
  --batches DIR         ``gcloud dataproc batches describe --format=json`` of the 16 batches
  --manifests DIR       ``<profile>/campaign_manifest.json`` for both profiles
  --airflow-db FILE     the local Airflow SQLite metadata (opened read-only)

Writes ``derived_metrics.json``, ``dataproc_usage.json`` and ``airflow_dagruns.json``. The headline
application metrics come from ``ops/gcp/spark_stage_report.py``; this script adds what that
tool does not report: AQE plan facts, cache re-reads, shuffle stages coalesced below the
available cores, executor start latency, Dataproc usage and Airflow durations.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

PROFILES = ("baseline", "optimized")
STAGES = ("bronze", "silver", "gold", "features")
LARGE_SHUFFLE_BYTES = 256 * 2**20
# 6 initial executors x 4 cores; the profile allows up to 7 executors (28 cores).
EXECUTOR_CORES_AT_START = 24


def events(path: Path):
    output = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=True).stdout
    for line in output.splitlines():
        if line.strip():
            yield json.loads(line)


def analyse(path: Path) -> dict[str, object]:
    stage_wall: dict[int, int] = {}
    tasks: dict[int, Counter] = defaultdict(Counter)
    plans: dict[int, str] = {}
    aqe_updates = 0
    jobs = 0
    started = first_executor = None
    for event in events(path):
        kind = event["Event"]
        if kind == "SparkListenerApplicationStart":
            started = event["Timestamp"]
        elif kind == "SparkListenerExecutorAdded" and first_executor is None:
            first_executor = event["Timestamp"]
        elif kind == "SparkListenerJobStart":
            jobs += 1
        elif kind == "SparkListenerStageCompleted":
            info = event["Stage Info"]
            stage_wall[info["Stage ID"]] = info.get("Completion Time", 0) - info.get("Submission Time", 0)
        elif kind == "SparkListenerTaskEnd":
            metrics = event.get("Task Metrics") or {}
            read = metrics.get("Shuffle Read Metrics") or {}
            counter = tasks[event["Stage ID"]]
            counter["tasks"] += 1
            counter["input"] += (metrics.get("Input Metrics") or {}).get("Bytes Read", 0)
            counter["shuffle_read"] += read.get("Remote Bytes Read", 0) + read.get("Local Bytes Read", 0)
            counter["run_ms"] += metrics.get("Executor Run Time", 0)
            counter["gc_ms"] += metrics.get("JVM GC Time", 0)
        elif kind.endswith("SparkListenerSQLExecutionStart"):
            plans[event["executionId"]] = event.get("physicalPlanDescription", "")
        elif kind.endswith("SparkListenerSQLAdaptiveExecutionUpdate"):
            aqe_updates += 1
            plans[event["executionId"]] = event.get("physicalPlanDescription", "")
    final_plans = "\n".join(plans.values())
    total_input = sum(c["input"] for c in tasks.values())
    large = {s: c for s, c in tasks.items() if c["shuffle_read"] > LARGE_SHUFFLE_BYTES}
    under = {s: c for s, c in large.items() if c["tasks"] < EXECUTOR_CORES_AT_START}
    return {
        "first_executor_after_start_seconds": round((first_executor - started) / 1000, 3) if first_executor else None,
        "spark_jobs": jobs,
        "sql_executions": len(plans),
        "aqe_plan_updates": aqe_updates,
        "final_plans": {
            "AQEShuffleRead": final_plans.count("AQEShuffleRead"),
            "AQEShuffleRead_coalesced": len(re.findall(r"AQEShuffleRead\s+coalesced", final_plans)),
            "BroadcastHashJoin": final_plans.count("BroadcastHashJoin"),
            "SortMergeJoin": final_plans.count("SortMergeJoin"),
            "TableCacheQueryStage": final_plans.count("TableCacheQueryStage"),
        },
        "executor_run_seconds": round(sum(c["run_ms"] for c in tasks.values()) / 1000, 1),
        "jvm_gc_seconds": round(sum(c["gc_ms"] for c in tasks.values()) / 1000, 1),
        "input_bytes_total": total_input,
        "large_shuffle_stages": {
            "threshold_bytes": LARGE_SHUFFLE_BYTES,
            "count": len(large),
            "wall_seconds": round(sum(stage_wall.get(s, 0) for s in large) / 1000, 1),
            "below_initial_cores": len(under),
            "below_initial_cores_wall_seconds": round(sum(stage_wall.get(s, 0) for s in under) / 1000, 1),
            "below_initial_cores_task_counts": sorted(c["tasks"] for c in under.values()),
        },
    }


def dataproc_usage(batches: Path) -> list[dict[str, object]]:
    rows = []
    for path in sorted(batches.glob("*.json")):
        batch = json.loads(path.read_text(encoding="utf-8"))
        usage = (batch.get("runtimeInfo") or {}).get("approximateUsage") or {}
        properties = (batch.get("runtimeConfig") or {}).get("properties") or {}
        rows.append({
            "batch_id": path.stem,
            "state": batch.get("state"),
            "create_time": batch.get("createTime"),
            "state_time": batch.get("stateTime"),
            "runtime_version": (batch.get("runtimeConfig") or {}).get("version"),
            "milli_dcu_seconds": int(usage["milliDcuSeconds"]) if "milliDcuSeconds" in usage else None,
            "dcu_hours": round(int(usage["milliDcuSeconds"]) / 1000 / 3600, 3) if "milliDcuSeconds" in usage else None,
            "shuffle_storage_gb_seconds": int(usage["shuffleStorageGbSeconds"]) if "shuffleStorageGbSeconds" in usage else None,
            "sql_properties": {k.removeprefix("spark:"): v for k, v in sorted(properties.items())
                               if k.startswith("spark:spark.sql.adaptive") or k in (
                                   "spark:spark.sql.shuffle.partitions", "spark:spark.dynamicAllocation.profile")},
        })
    return rows


def airflow_dagruns(database: Path, benchmark: str) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    seconds = "round((julianday(end_date) - julianday(start_date)) * 86400, 1)"
    runs = [dict(zip(("dag_id", "run_id", "state", "start_date", "end_date", "duration_seconds"), row))
            for row in connection.execute(
                f"select dag_id, run_id, state, start_date, end_date, {seconds} from dag_run "
                "where run_id like ? order by start_date", (f"{benchmark}-%",))]
    tasks = [dict(zip(("run_id", "task_id", "state", "try_number", "duration_seconds"), row))
             for row in connection.execute(
                 f"select run_id, task_id, state, try_number, {seconds} from task_instance "
                 "where run_id like ? order by run_id, start_date", (f"{benchmark}-%",))]
    return {"benchmark_id": benchmark, "dag_runs": runs, "task_instances": tasks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--event-logs", type=Path, required=True)
    parser.add_argument("--batches", type=Path, required=True)
    parser.add_argument("--manifests", type=Path, required=True)
    parser.add_argument("--airflow-db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    derived: dict[str, object] = {"schema_version": "rehearsal-derived-metrics-v1", "applications": {}}
    benchmark = None
    for profile in PROFILES:
        manifest = json.loads((args.manifests / profile / "campaign_manifest.json").read_text(encoding="utf-8"))
        benchmark = manifest["benchmark_id"]
        for stage in STAGES:
            for kind, meta in (("transform", manifest["stages"][stage]["execution_metadata"]),
                               ("validate", manifest["stages"][stage]["validation"]["execution_metadata"])):
                application = meta["spark_application_id"]
                derived["applications"][f"{profile}-{stage}-{kind}"] = {
                    "batch_id": meta["dataproc_batch_id"], "application_id": application,
                    **analyse(args.event_logs / f"{application}.zstd"),
                }
    derived["benchmark_id"] = benchmark
    write = lambda name, value: (args.output_dir / name).write_text(  # noqa: E731
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write("derived_metrics.json", derived)
    write("dataproc_usage.json", dataproc_usage(args.batches))
    write("airflow_dagruns.json", airflow_dagruns(args.airflow_db, benchmark))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
