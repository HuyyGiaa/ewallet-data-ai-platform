"""G2 shared-GKE protection: capacity budget, MLOps health, live per-pod resource guard.

Reuses the O1 decision functions unchanged (ops/llm_agent/o1_guard.py: capacity,
health, regressions, evaluate_guard and its thresholds); only the node label
(ewallet.io/g2-llm) and the per-pod sampling differ, because G2 runs two active
vLLM primaries and the llm-d router:

capacity  every G2 request (2 primaries, fallback, gateways, router) on the
          G2-labelled nodes within the safe budget, else
          BLOCKED_INSUFFICIENT_SAFE_CAPACITY;
health    MLOps snapshot (as O1);
guard     every --interval s: usage of each vLLM pod, the gateways and each
          router container, node usage, MLOps health against the baseline,
          OOM/eviction events; JSONL plus the stop file on the first breach.

Read-only towards the cluster; every kubectl call carries the explicit --context.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ops.llm_agent import o1_guard  # noqa: E402

G2_LABEL = "ewallet.io/g2-llm"
LLM_NS = o1_guard.LLM_NS


def as_budget_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nodes as o1_guard.capacity() sees them: G2-labelled nodes are the usable ones."""

    out = []
    for node in nodes:
        node = copy.deepcopy(node)
        labels = node["metadata"].setdefault("labels", {})
        labels.pop(o1_guard.O1_LABEL, None)
        if labels.get(G2_LABEL) == "true":
            labels[o1_guard.O1_LABEL] = "true"
        out.append(node)
    return out


def capacity(nodes: list[dict[str, Any]], pods: list[dict[str, Any]], planned: list[dict[str, float]]) -> dict[str, Any]:
    result = o1_guard.capacity(as_budget_nodes(nodes), pods, planned)
    result["g2_nodes"] = result.pop("o1_nodes")
    for row in result["nodes"]:
        row["g2"] = row.pop("o1")
    return result


def pod_usage(top_rows: list[list[str]]) -> dict[str, Any]:
    """`kubectl top pods --containers` rows -> per-pod and per-router-container usage."""

    pods: dict[str, dict[str, int]] = {}
    for pod, container, cpu_m, mem_mib in (r[:4] for r in top_rows if len(r) >= 4):
        usage = pods.setdefault(pod, {"cpu_m": 0, "mem_mib": 0, "containers": {}})
        c, m = int(cpu_m.rstrip("m")), int(mem_mib.rstrip("Mi"))
        usage["cpu_m"] += c
        usage["mem_mib"] += m
        usage["containers"][container] = {"cpu_m": c, "mem_mib": m}
    primaries = {k: v for k, v in pods.items() if k.startswith("vllm-primary")}
    gateway = [v for k, v in pods.items() if k.startswith("agentgateway")]
    router = [v for k, v in pods.items() if k.startswith("llmd-router")]
    return {
        "vllm_pods": {k: {"cpu_m": v["cpu_m"], "mem_mib": v["mem_mib"]} for k, v in sorted(primaries.items())},
        "fallback": next(({"cpu_m": v["cpu_m"], "mem_mib": v["mem_mib"]} for k, v in pods.items()
                          if k.startswith("vllm-fallback")), None),
        # o1_guard.evaluate_guard compares vllm_mem_mib with a per-pod limit: the largest pod.
        "vllm_cpu_m": sum(v["cpu_m"] for v in primaries.values()),
        "vllm_mem_mib": max((v["mem_mib"] for v in primaries.values()), default=0),
        "gateway_cpu_m": sum(v["cpu_m"] for v in gateway), "gateway_mem_mib": sum(v["mem_mib"] for v in gateway),
        "router": router[0]["containers"] if router else None,
    }


def collect_sample(context: str, baseline: dict[str, Any], since: str) -> dict[str, Any]:
    labels = {n["metadata"]["name"]: (n["metadata"].get("labels") or {}).get(G2_LABEL) == "true"
              for n in o1_guard._json(context, "get", "nodes")}
    nodes = [{"name": r[0], "o1": labels.get(r[0], False), "cpu_m": int(r[1].rstrip("m")),
              "cpu_pct": float(r[2].rstrip("%")), "mem_mib": int(r[3].rstrip("Mi")), "mem_pct": float(r[4].rstrip("%"))}
             for r in o1_guard._top(context, "nodes")]
    sample = pod_usage(o1_guard._top(context, "pods", "-n", LLM_NS, "--containers"))
    mlops_cpu = mlops_mem = 0
    for ns in o1_guard.MLOPS_NAMESPACES:
        try:
            for r in o1_guard._top(context, "pods", "-n", ns):
                mlops_cpu += int(r[1].rstrip("m"))
                mlops_mem += int(r[2].rstrip("Mi"))
        except subprocess.CalledProcessError:
            pass
    events = []
    for line in o1_guard._kubectl(context, "get", "events", "-A", "--field-selector", "type=Warning",
                                  "-o", "jsonpath={range .items[*]}{.lastTimestamp} {.reason} "
                                        "{.involvedObject.namespace}/{.involvedObject.name}{'\\n'}{end}").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] >= since and parts[1] in ("OOMKilling", "OOMKilled", "Evicted", "Preempted"):
            events.append(line)
    current = o1_guard.collect_health(context)
    # "o1" is the usable-node flag o1_guard.evaluate_guard reads (here: the G2 nodes).
    return {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "nodes": nodes, **sample,
            "mlops_cpu_m": mlops_cpu, "mlops_mem_mib": mlops_mem, "mlops_healthy": current["healthy"],
            "mlops_regressions": o1_guard.regressions(baseline, current), "oom_or_eviction_events": events}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["capacity", "health", "guard"])
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--planned", help="JSON list of {name, cpu, memory_gib} requests (capacity)")
    parser.add_argument("--baseline", type=Path, help="health snapshot (guard)")
    parser.add_argument("--stop-file", type=Path, help="guard")
    parser.add_argument("--interval", type=float, default=15.0)
    args = parser.parse_args(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.mode == "capacity":
        result = capacity(o1_guard._json(args.context, "get", "nodes"),
                          o1_guard._json(args.context, "get", "pods", "-A"), json.loads(args.planned))
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({k: result[k] for k in ("verdict", "g2_nodes", "placement", "unplaced")}, indent=1))
        return 0 if result["verdict"] == "FITS_SAFE_BUDGET" else 1
    if args.mode == "health":
        return o1_guard.main(["health", "--context", args.context, "--output", str(args.output)])
    baseline = json.loads(args.baseline.read_text())
    since = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    while True:
        try:
            sample = collect_sample(args.context, baseline, since)
        except (subprocess.SubprocessError, ValueError, IndexError) as exc:
            sample = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "collect_error": f"{type(exc).__name__}: {exc}"[:200]}
        reasons = o1_guard.evaluate_guard(sample) if "collect_error" not in sample else []
        sample["guard_reasons"] = reasons
        with args.output.open("a") as handle:
            handle.write(json.dumps(sample) + "\n")
        if reasons and not args.stop_file.exists():
            args.stop_file.write_text(json.dumps({"ts": sample["ts"], "reasons": reasons}) + "\n")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
