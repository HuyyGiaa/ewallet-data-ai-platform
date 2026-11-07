"""Read existing benchmark output into the shared run format.

The O1 and G2 clients stay as they are, and their ``summary.json`` is read here. A failed
request is ``requests`` minus ``succeeded``, so nothing is filtered on the way in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from ops.experiments.compare import Run


def _per_pod_counters(level: Mapping[str, Any]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for counters in level.get("distribution", {}).get("per_pod", {}).values():
        for name, value in counters.items():
            totals[name] = totals.get(name, 0.0) + value
    return totals


def derived_metrics(level: Mapping[str, Any]) -> dict[str, float | None]:
    """Give O1 (one pod) and G2 (per pod) resource and server fields one shape."""

    resources = level.get("resources") or {}
    server = level.get("server") or {}
    counters = _per_pod_counters(level)
    if "vllm_total_cpu_m" in resources:
        cpu_m = (resources["vllm_total_cpu_m"] or {}).get("mean")
        memory = sum(pod["mem_mib_peak"] for pod in resources.get("vllm_pods", {}).values()) or None
    else:
        cpu_m, memory = resources.get("cpu_millicores_mean"), resources.get("memory_mib_peak")
    queries, count = counters.get("vllm:prefix_cache_queries", 0.0), counters.get("vllm:request_queue_time_seconds_count", 0.0)
    return {
        "cpu_cores_mean": None if cpu_m is None else cpu_m / 1000,
        "memory_mib_peak": memory,
        "prefix_cache_hit_rate": server.get("prefix_cache_hit_rate")
        if server else (counters.get("vllm:prefix_cache_hits", 0.0) / queries if queries else None),
        "queue_time_mean_ms": server.get("queue_time_mean_ms")
        if server else (1000 * counters.get("vllm:request_queue_time_seconds_sum", 0.0) / count if count else None),
    }


def _level_summary(level: Mapping[str, Any]) -> dict[str, Any]:
    requests = int(level.get("requests", 0))
    return {**level, "requests": requests, "failed": requests - int(level.get("succeeded", 0)),
            "derived": derived_metrics(level)}


def run_from_summary(summary: Mapping[str, Any], controls: Mapping[str, Any]) -> Run:
    """Convert one client summary.

    A level that was not measured keeps its status and its request counts, so
    the comparison can refuse it instead of reading it as an empty success.
    """

    levels = {str(name): _level_summary(level) for name, level in summary.get("levels", {}).items()}
    merged = {"workload_sha256": summary.get("workload_sha256"), **controls}
    return Run(controls=merged, levels=levels)


def raw_row_counts(lines: Iterable[str]) -> dict[str, int]:
    """Count measured rows per level; level 0 is the unmeasured cold pass."""

    counts: dict[str, int] = {}
    for line in lines:
        if line.strip():
            level = str(json.loads(line).get("level", 0))
            if level != "0":
                counts[level] = counts.get(level, 0) + 1
    return counts


def check_raw_rows(run: Run, summary: Mapping[str, Any], path: Path) -> Run:
    """Refuse a measured level whose summary does not account for every raw request.

    Failed requests are written to the raw file as they happen. If a summary counts fewer
    rows than the raw file holds, something was dropped between them, so the level stops
    being comparable. Only benchmark client output has a raw file, marked by ``config``.
    """

    if "config" not in summary:
        return run
    counts = raw_row_counts(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else None
    levels = dict(run.levels)
    for name, level in run.levels.items():
        if level.get("status") != "MEASURED":
            continue
        if counts is None:
            levels[name] = {**level, "status": "RAW_FILE_MISSING"}
        elif counts.get(name, 0) != level["requests"]:
            levels[name] = {**level, "status": "RAW_ROW_MISMATCH"}
    return Run(controls=run.controls, levels=levels)


def load_arm(directory: Path) -> list[Run]:
    """Load every ``raw/run-*`` of one arm; each holds ``summary.json`` and ``controls.json``."""

    runs = []
    for path in sorted((directory / "raw").glob("run-*")):
        summary = path / "summary.json"
        if not summary.is_file():
            continue
        controls_path = path / "controls.json"
        controls = json.loads(controls_path.read_text(encoding="utf-8")) if controls_path.is_file() else {}
        parsed = json.loads(summary.read_text(encoding="utf-8"))
        runs.append(check_raw_rows(run_from_summary(parsed, controls), parsed, path / "raw_requests.jsonl"))
    return runs
