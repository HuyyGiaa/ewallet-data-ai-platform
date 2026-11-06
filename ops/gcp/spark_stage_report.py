"""Report Spark stage metrics from completed event logs, offline.

Reads a downloaded event log, so it needs no History Server and changes nothing in
the cloud. For each stage it reports duration, shuffle, spill and a skew indicator;
for the application it reports executor utilization and SQL plan identities. The
``compare`` command reports two applications side by side, and refuses unless the
caller names the same input for both, because a comparison over different datasets
is not a controlled result.

    python -m ops.gcp.spark_stage_report report --event-log app.zstd --output a.json
    python -m ops.gcp.spark_stage_report compare --baseline a.json --optimized b.json \
        --input-identity <run-id-of-the-shared-input> --output pair.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence


REPORT_SCHEMA = "spark-stage-report-v1"
COMPARISON_SCHEMA = "spark-stage-comparison-v1"
TOP_STAGES = 10
# A stage is flagged when its slowest task takes this many times the median task,
# and the task is long enough for the ratio to matter.
SKEW_RATIO = 3.0
SKEW_MIN_TASK_MS = 10_000


def read_events(path: Path) -> Iterator[Mapping[str, object]]:
    """Yield events from a plain or zstd-compressed Spark event log."""

    if path.suffix == ".zstd":
        process = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=False)
        if process.returncode != 0:
            raise ValueError(f"cannot decompress {path}: {process.stderr[:200]!r}")
        lines: Iterable[bytes | str] = process.stdout.splitlines()
    else:
        lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if line.strip():
            yield json.loads(line)


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


class _Stage:
    """Accumulates one stage attempt's task metrics."""

    def __init__(self) -> None:
        self.durations: list[int] = []
        self.failed = 0
        self.input_bytes = self.shuffle_read = self.shuffle_write = 0
        self.memory_spill = self.disk_spill = 0

    def add(self, event: Mapping[str, object]) -> None:
        """Add one ``SparkListenerTaskEnd`` event."""

        info = event.get("Task Info") or {}
        metrics = event.get("Task Metrics") or {}
        if info.get("Failed"):
            self.failed += 1
        self.durations.append(int(info.get("Finish Time", 0)) - int(info.get("Launch Time", 0)))
        read = metrics.get("Shuffle Read Metrics") or {}
        self.shuffle_read += int(read.get("Remote Bytes Read", 0)) + int(read.get("Local Bytes Read", 0))
        self.shuffle_write += int((metrics.get("Shuffle Write Metrics") or {}).get("Shuffle Bytes Written", 0))
        self.input_bytes += int((metrics.get("Input Metrics") or {}).get("Bytes Read", 0))
        self.memory_spill += int(metrics.get("Memory Bytes Spilled", 0))
        self.disk_spill += int(metrics.get("Disk Bytes Spilled", 0))


def _stage_summary(stage_id: int, name: str, duration_ms: int, stage: _Stage) -> dict[str, object]:
    median = statistics.median(stage.durations) if stage.durations else 0
    longest = max(stage.durations, default=0)
    ratio = round(longest / median, 2) if median else None
    return {
        "stage_id": stage_id,
        "name": name[:120],
        "duration_seconds": round(duration_ms / 1000, 3),
        "tasks": len(stage.durations),
        "failed_tasks": stage.failed,
        "input_bytes": stage.input_bytes,
        "shuffle_read_bytes": stage.shuffle_read,
        "shuffle_write_bytes": stage.shuffle_write,
        "memory_spill_bytes": stage.memory_spill,
        "disk_spill_bytes": stage.disk_spill,
        "task_ms_p50": median,
        "task_ms_p95": _percentile(stage.durations, 0.95) if stage.durations else 0,
        "task_ms_max": longest,
        "skew_ratio_max_over_median": ratio,
        "skew_flag": bool(ratio and ratio >= SKEW_RATIO and longest >= SKEW_MIN_TASK_MS),
    }


def build_report(events: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Fold events into an application report.

    Raises:
        ValueError: The log has no application start or end.
    """

    stages: dict[int, _Stage] = defaultdict(_Stage)
    completed: dict[int, tuple[str, int]] = {}
    executors: dict[str, dict[str, int]] = {}
    plans: set[str] = set()
    application: dict[str, object] = {}
    started = ended = None
    busy_ms = 0
    for event in events:
        kind = event.get("Event")
        if kind == "SparkListenerApplicationStart":
            started = int(event["Timestamp"])
            application = {"application_id": event.get("App ID"), "application_name": event.get("App Name")}
        elif kind == "SparkListenerApplicationEnd":
            ended = int(event["Timestamp"])
        elif kind == "SparkListenerTaskEnd":
            stages[int(event["Stage ID"])].add(event)
            busy_ms += int((event.get("Task Metrics") or {}).get("Executor Run Time", 0))
        elif kind == "SparkListenerStageCompleted":
            info = event["Stage Info"]
            duration = int(info.get("Completion Time", 0)) - int(info.get("Submission Time", 0))
            completed[int(info["Stage ID"])] = (str(info.get("Stage Name", "")), duration)
        elif kind == "SparkListenerExecutorAdded":
            executors[str(event["Executor ID"])] = {
                "added": int(event["Timestamp"]),
                "cores": int(event["Executor Info"]["Total Cores"]),
            }
        elif kind == "SparkListenerExecutorRemoved" and str(event["Executor ID"]) in executors:
            executors[str(event["Executor ID"])]["removed"] = int(event["Timestamp"])
        elif kind == "org.apache.spark.sql.execution.ui.SparkListenerSQLExecutionStart":
            plan = str(event.get("physicalPlanDescription", ""))
            plans.add(hashlib.sha256(plan.encode("utf-8")).hexdigest()[:16])
    if started is None or ended is None:
        raise ValueError("event log has no complete application")
    core_ms = sum(
        item["cores"] * (item.get("removed", ended) - item["added"]) for item in executors.values()
    )
    summaries = [
        _stage_summary(stage_id, *completed.get(stage_id, ("", 0)), stage)
        for stage_id, stage in stages.items()
    ]
    summaries.sort(key=lambda item: -item["duration_seconds"])

    def total(key: str) -> int:
        return sum(item[key] for item in summaries)

    return {
        "schema_version": REPORT_SCHEMA,
        **application,
        "runtime_seconds": round((ended - started) / 1000, 3),
        "stage_count": len(summaries),
        "task_count": total("tasks"),
        "failed_task_count": total("failed_tasks"),
        "input_bytes": total("input_bytes"),
        "shuffle_read_bytes": total("shuffle_read_bytes"),
        "shuffle_write_bytes": total("shuffle_write_bytes"),
        "memory_spill_bytes": total("memory_spill_bytes"),
        "disk_spill_bytes": total("disk_spill_bytes"),
        "executors": len(executors),
        "executor_core_seconds": round(core_ms / 1000, 1),
        "executor_utilization": round(busy_ms / core_ms, 4) if core_ms else None,
        "skewed_stages": sum(item["skew_flag"] for item in summaries),
        "sql_plan_sha256_prefixes": sorted(plans),
        "slowest_stages": summaries[:TOP_STAGES],
    }


COMPARED_METRICS = (
    "runtime_seconds", "task_count", "failed_task_count", "input_bytes", "shuffle_read_bytes",
    "shuffle_write_bytes", "memory_spill_bytes", "disk_spill_bytes", "executor_core_seconds",
    "executor_utilization", "skewed_stages",
)


def compare_reports(
    baseline: Mapping[str, object], optimized: Mapping[str, object], input_identity: str
) -> dict[str, object]:
    """Compare two reports over one named input.

    A slower optimized run stays a valid result: this reports the change and never
    picks a winner.

    Raises:
        ValueError: The input is unnamed, or both reports are one application.
    """

    if not input_identity.strip():
        raise ValueError("a controlled comparison must name its shared input")
    if baseline.get("application_id") == optimized.get("application_id"):
        raise ValueError("baseline and optimized are the same application")
    rows = {}
    for metric in COMPARED_METRICS:
        left, right = baseline.get(metric), optimized.get(metric)
        change = None
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) and left:
            change = round(100 * (right - left) / left, 2)
        rows[metric] = {"baseline": left, "optimized": right, "change_percent": change}
    return {
        "schema_version": COMPARISON_SCHEMA,
        "input_identity": input_identity,
        "baseline_application_id": baseline.get("application_id"),
        "optimized_application_id": optimized.get("application_id"),
        "metrics": rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``report`` or ``compare`` command."""

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report")
    report.add_argument("--event-log", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--optimized", type=Path, required=True)
    compare.add_argument("--input-identity", required=True)
    compare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "report":
        result = build_report(read_events(args.event_log))
    else:
        result = compare_reports(
            json.loads(args.baseline.read_text(encoding="utf-8")),
            json.loads(args.optimized.read_text(encoding="utf-8")),
            args.input_identity,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
