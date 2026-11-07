"""Build one EXP-C5-SANDBOX run summary from client rows and resource samples.

``successful_mcp_calls`` is derived, not observed: the Specialist completes a task only when
every requested tool returned valid evidence, so each completed task counts its tools.
Anything not measured stays ``null``, such as recovery when no failure was injected.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ops.experiments.agent_summary import load_rows, percentile

WORKER_CPU_LIMIT_M = 1500.0


def tools_per_task(fixture: str) -> int:
    """Count the tools the load fixture requests."""

    text = json.loads(fixture)["params"]["message"]["parts"][0]["text"]
    return len(json.loads(text.splitlines()[1].split("=", 1)[1])["requested_tools"])


def _latency(rows: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    values = [row["e2e_ms"] for row in rows if row["ok"]]
    return {"p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}


def resource_usage(samples: Sequence[Mapping[str, Any]], arm_prefix: str) -> dict[str, float | None]:
    """Average CPU and peak memory of the arm's pods, and worker load against its limit."""

    cpu, memory, worker = [], [], []
    for sample in samples:
        arm = [item for name, item in sample.get("pods", {}).items() if name.startswith(arm_prefix)]
        pool = [item for name, item in sample.get("pods", {}).items() if name.startswith("business-agents")]
        if arm or pool:
            cpu.append(sum(item["cpu_m"] for item in arm + pool))
            memory.append(sum(item["mem_mib"] for item in arm + pool))
        if pool:
            worker.append(sum(item["cpu_m"] for item in pool))
    return {
        "cpu_cores_mean": sum(cpu) / len(cpu) / 1000 if cpu else None,
        "memory_mib_peak": max(memory) if memory else None,
        "worker_cpu_limit_percent": 100 * (sum(worker) / len(worker)) / WORKER_CPU_LIMIT_M if worker else None,
    }


def summarize(rows: Sequence[Mapping[str, Any]], samples: Sequence[Mapping[str, Any]], *, applied_at: float, ready_at: float | None,
              failure_at: float | None, tools: int, workload_sha256: str, arm_prefix: str) -> dict[str, Any]:
    """Build ``summary.json``; every attempt counts toward requests and failures."""

    if not rows:
        return {"workload_sha256": workload_sha256, "levels": {"load": {"status": "NEVER_ANSWERED", "requests": 0, "succeeded": 0}}}

    def phase(name: str) -> list[Mapping[str, Any]]:
        return [row for row in rows if row.get("phase") == name]

    first = [row for row in phase("first") if row["ok"]]
    recovered = [row for row in phase("recovery") if row["ok"]]
    succeeded = sum(1 for row in rows if row["ok"])
    level = {
        "status": "MEASURED" if first else "NEVER_ANSWERED",
        "requests": len(rows), "succeeded": succeeded,
        "initialization_seconds": None if ready_at is None else ready_at - applied_at,
        "first_success_seconds": first[0]["finished"] - applied_at if first else None,
        "e2e_ms": _latency(phase("new")), "resume_e2e_ms": _latency(phase("resume")),
        "successful_mcp_calls": succeeded * tools,
        "recovery_seconds": recovered[0]["finished"] - failure_at if recovered and failure_at else None,
        **resource_usage(samples, arm_prefix),
    }
    return {"workload_sha256": workload_sha256, "levels": {"load": level}}


def main() -> int:
    """Write ``summary.json``."""

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("--log", "--samples", "--fixture", "--output"):
        parser.add_argument(name, type=Path, required=True)
    parser.add_argument("--applied-at", type=float, required=True)
    parser.add_argument("--ready-at", type=float)
    parser.add_argument("--failure-at", type=float)
    parser.add_argument("--workload-sha256", required=True)
    parser.add_argument("--arm-prefix", required=True)
    args = parser.parse_args()
    samples = [json.loads(line) for line in args.samples.read_text(encoding="utf-8").splitlines() if line.strip()] \
        if args.samples.is_file() else []
    summary = summarize(load_rows(args.log.read_text(encoding="utf-8")), samples, applied_at=args.applied_at,
                        ready_at=args.ready_at, failure_at=args.failure_at, workload_sha256=args.workload_sha256,
                        tools=tools_per_task(args.fixture.read_text(encoding="utf-8")), arm_prefix=args.arm_prefix)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
