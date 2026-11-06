"""Sample benchmark pod usage into the JSONL format the O1 and G2 clients read.

Runs beside a benchmark until its stop file appears. A failed sample is written as a row
with ``collect_error``, so a gap in the data stays visible.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


def _cpu_m(value: str) -> float:
    return float(value[:-1]) if value.endswith("m") else float(value) * 1000


def _mem_mib(value: str) -> float:
    if value.endswith("Mi"):
        return float(value[:-2])
    if value.endswith("Gi"):
        return float(value[:-2]) * 1024
    if value.endswith("Ki"):
        return float(value[:-2]) / 1024
    return float(value) / 1048576


def parse_top(text: str) -> dict[str, Any]:
    """Convert ``kubectl top pods --containers --no-headers`` output into one sample."""

    pods: dict[str, dict[str, float]] = {}
    every_pod: dict[str, dict[str, float]] = {}
    router: dict[str, dict[str, float]] = {}
    gateway_cpu = 0.0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        pod, container, cpu, memory = parts
        usage = {"cpu_m": _cpu_m(cpu), "mem_mib": _mem_mib(memory)}
        previous = every_pod.get(pod, {"cpu_m": 0.0, "mem_mib": 0.0})
        every_pod[pod] = {key: previous[key] + usage[key] for key in usage}
        if pod.startswith("vllm-primary"):
            pods[pod] = usage
        elif pod.startswith("agentgateway"):
            gateway_cpu += usage["cpu_m"]
        elif pod.startswith("llmd-router"):
            router[container] = usage
    sample: dict[str, Any] = {
        "pods": every_pod,
        "vllm_pods": pods,
        "vllm_cpu_m": sum(item["cpu_m"] for item in pods.values()),
        "vllm_mem_mib": sum(item["mem_mib"] for item in pods.values()),
        "gateway_cpu_m": gateway_cpu,
    }
    if router:
        sample["router"] = router
    return sample


def top_pods(context: str, namespace: str) -> str:
    """Read current usage through metrics-server."""

    command = ["kubectl", "--context", context, "--request-timeout=30s", "top", "pods", "-n", namespace,
               "--containers", "--no-headers"]
    return subprocess.run(command, capture_output=True, text=True, check=True, timeout=45).stdout


def sample_once(reader: Callable[[], str]) -> dict[str, Any]:
    """Take one sample; a failure becomes a row, never a silent gap."""

    stamp = {"ts": datetime.now(timezone.utc).isoformat()}
    try:
        return {**stamp, **parse_top(reader())}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {**stamp, "collect_error": type(error).__name__}


def main(argv: Sequence[str] | None = None) -> int:
    """Append samples to the output file until the stop file exists."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="ewallet-bench")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=15.0)
    args = parser.parse_args(argv)
    while not args.stop_file.exists():
        row = sample_once(lambda: top_pods(args.context, args.namespace))
        with args.output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
