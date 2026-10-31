"""Host resource guard before a local test package (standard library only).

Samples CPU utilisation over an interval (/proc/stat), one-minute normalized
load, MemAvailable, swap activity (/proc/vmstat pswpin/pswpout deltas) and
memory PSI, and checks Airflow: a scheduler process exists, port 8084
accepts connections and /api/v2/monitor/health reports healthy components.
Process existence alone never counts as healthy.

SAFE needs: MemAvailable >= 10 GiB, CPU < 65 %, no swap-in/out during the
sample, memory PSI avg10 < 5, Airflow healthy. Otherwise UNSAFE (defer the
test package). These are conservative guards, not a guarantee.

usage: python3 -m ops.llm_agent.host_resource_guard [--interval 5] [--label name] [--json-output f.json]
exit 0 = SAFE, 1 = UNSAFE.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any
import urllib.request

MIN_MEM_GIB = 10.0
MAX_CPU_PCT = 65.0
MAX_PSI_AVG10 = 5.0
AIRFLOW = ("127.0.0.1", 8084)
HEALTH_URL = "http://127.0.0.1:8084/api/v2/monitor/health"


def _cpu_times() -> tuple[int, int]:
    fields = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


def _vmstat() -> dict[str, int]:
    return {k: int(v) for k, v in (line.split() for line in Path("/proc/vmstat").read_text().splitlines())
            if k in ("pswpin", "pswpout")}


def _airflow() -> dict[str, Any]:
    scheduler = any("airflow scheduler" in _cmdline(pid) for pid in os.listdir("/proc") if pid.isdigit())
    try:
        with socket.create_connection(AIRFLOW, timeout=2):
            port_open = True
    except OSError:
        port_open = False
    health, healthy = None, False
    if port_open:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=5) as response:  # noqa: S310 (fixed local URL)
                health = json.load(response)
            healthy = all((health.get(part) or {}).get("status") == "healthy"
                          for part in ("metadatabase", "scheduler", "dag_processor"))
        except Exception as exc:  # e.g. 401 if the endpoint ever requires auth
            health = {"error": type(exc).__name__}
    return {"scheduler_process": scheduler, "port_8084_open": port_open, "healthy": healthy,
            "components": {k: (v or {}).get("status") for k, v in (health or {}).items() if isinstance(v, dict)}
            or health}


def _cmdline(pid: str) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def sample(interval: float) -> dict[str, Any]:
    total0, idle0 = _cpu_times()
    swap0 = _vmstat()
    time.sleep(interval)
    total1, idle1 = _cpu_times()
    swap1 = _vmstat()
    cpu = 100.0 * (1 - (idle1 - idle0) / max(total1 - total0, 1))
    meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    mem_gib = int(meminfo["MemAvailable"].split()[0]) / 1024 ** 2
    psi = float(Path("/proc/pressure/memory").read_text().split()[1].split("=")[1])
    swap = {k: swap1[k] - swap0[k] for k in swap0}
    load1 = float(Path("/proc/loadavg").read_text().split()[0])
    airflow = _airflow()
    reasons = []
    if mem_gib < MIN_MEM_GIB:
        reasons.append(f"MemAvailable {mem_gib:.1f} GiB < {MIN_MEM_GIB}")
    if cpu >= MAX_CPU_PCT:
        reasons.append(f"CPU {cpu:.0f}% >= {MAX_CPU_PCT:.0f}%")
    if any(swap.values()):
        reasons.append(f"swap activity {swap}")
    if psi >= MAX_PSI_AVG10:
        reasons.append(f"memory PSI avg10 {psi}")
    if not airflow["healthy"]:
        reasons.append("Airflow 8084 not healthy")
    return {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "interval_s": interval,
            "cpu_pct": round(cpu, 1), "load1_normalized": round(load1 / (os.cpu_count() or 1), 3),
            "mem_available_gib": round(mem_gib, 2), "swap_pages_delta": swap, "psi_memory_avg10": psi,
            "airflow": airflow, "verdict": "SAFE" if not reasons else "UNSAFE", "reasons": reasons}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--label", default="")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    result = dict(sample(args.interval), label=args.label)
    text = json.dumps(result, sort_keys=True)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.json_output.with_suffix(".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        tmp.replace(args.json_output)
    print(text)
    return 0 if result["verdict"] == "SAFE" else 1


if __name__ == "__main__":
    sys.exit(main())
