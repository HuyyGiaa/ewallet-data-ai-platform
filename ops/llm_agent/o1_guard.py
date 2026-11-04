"""O1 shared-GKE protection: capacity budget, MLOps health, live resource guard.

capacity  per-node budget = allocatable - existing requests - headroom, for the
          nodes O1 may use (ewallet.io/o1-llm=true); BLOCKED_INSUFFICIENT_SAFE_CAPACITY
          when the O1 requests (all replicas, primary and fallback) do not fit.
health    MLOps snapshot: pods Ready / restarts per protected namespace,
          InferenceService readiness, node pressure conditions.
guard     every --interval s: vLLM/gateway/node usage, MLOps health against a
          baseline snapshot, OOM/eviction events; appends JSONL and writes the
          stop file (with the reason) on the first breach of THRESHOLDS.

The decision functions are pure (tested offline); only collect_* call kubectl,
always with the explicit --context. Read-only towards the cluster.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ops.llm_agent.gke_preflight import cpu, mem_gib  # noqa: E402

O1_LABEL = "ewallet.io/o1-llm"
LLM_NS = "ewallet-agents"
MLOPS_NAMESPACES = ("kubeflow", "ml-platform", "feature-store", "kserve", "fraud-serving", "cert-manager")
# Per-node headroom kept free on top of existing requests (bursts, DaemonSets).
HEADROOM = {"cpu": 0.3, "memory_gib": 0.75}
# Decided before any test (O1 prompt 3.7); a breach stops the benchmark.
THRESHOLDS = {
    "mlops_node_cpu_pct": 85.0,      # nodes without the O1 label
    "mlops_node_mem_pct": 90.0,
    "o1_node_mem_pct": 92.0,         # nodes with the O1 label
    "vllm_mem_gib": 9.5,             # 95 % of the 10 Gi limit
}


def _requests(containers: list[dict[str, Any]], kind: str = "requests") -> tuple[float, float]:
    c = m = 0.0
    for container in containers:
        r = (container.get("resources") or {}).get(kind) or {}
        c += cpu(str(r.get("cpu", "0")))
        m += mem_gib(str(r.get("memory", "0")))
    return c, m


def capacity(nodes: list[dict[str, Any]], pods: list[dict[str, Any]], planned: list[dict[str, float]],
             *, headroom: dict[str, float] = HEADROOM) -> dict[str, Any]:
    """Fit each planned pod (cpu, memory_gib requests) on an O1-labelled node."""

    rows = []
    for node in nodes:
        name = node["metadata"]["name"]
        alloc = node["status"]["allocatable"]
        used_cpu = used_mem = 0.0
        for pod in pods:
            if pod["spec"].get("nodeName") != name or pod["status"].get("phase") in ("Succeeded", "Failed"):
                continue
            if pod["metadata"]["namespace"] == LLM_NS:
                continue  # O1's own pods are re-planned below
            c, m = _requests(pod["spec"]["containers"])
            used_cpu += c
            used_mem += m
        rows.append({"node": name, "o1": (node["metadata"].get("labels") or {}).get(O1_LABEL) == "true",
                     "allocatable_cpu": cpu(alloc["cpu"]), "allocatable_mem_gib": mem_gib(alloc["memory"]),
                     "requested_cpu": round(used_cpu, 3), "requested_mem_gib": round(used_mem, 3)})
    for row in rows:
        row["safe_cpu"] = round(row["allocatable_cpu"] - row["requested_cpu"] - headroom["cpu"], 3)
        row["safe_mem_gib"] = round(row["allocatable_mem_gib"] - row["requested_mem_gib"] - headroom["memory_gib"], 3)
    free = {r["node"]: [r["safe_cpu"], r["safe_mem_gib"]] for r in rows if r["o1"]}
    placement, unplaced = [], []
    for pod in sorted(planned, key=lambda p: -p["cpu"]):     # largest first
        target = next((n for n, (c, m) in sorted(free.items(), key=lambda kv: -kv[1][0])
                       if c >= pod["cpu"] and m >= pod["memory_gib"]), None)
        if target is None:
            unplaced.append(pod)
            continue
        free[target][0] -= pod["cpu"]
        free[target][1] -= pod["memory_gib"]
        placement.append({**pod, "node": target})
    verdict = "FITS_SAFE_BUDGET" if not unplaced and free else "BLOCKED_INSUFFICIENT_SAFE_CAPACITY"
    return {"headroom": headroom, "nodes": rows, "planned": planned, "placement": placement,
            "unplaced": unplaced, "o1_nodes": sorted(free), "verdict": verdict}


def health(pods: list[dict[str, Any]], isvcs: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"pods": {}, "inferenceservices": {}, "node_pressure": {}}
    for pod in pods:
        ns = pod["metadata"]["namespace"]
        if ns not in MLOPS_NAMESPACES or pod["status"].get("phase") == "Succeeded":
            continue
        statuses = pod["status"].get("containerStatuses") or []
        out["pods"][f"{ns}/{pod['metadata']['name']}"] = {
            "phase": pod["status"].get("phase"), "ready": bool(statuses) and all(s.get("ready") for s in statuses),
            "restarts": sum(s.get("restartCount", 0) for s in statuses), "node": pod["spec"].get("nodeName")}
    for isvc in isvcs:
        conditions = {c["type"]: c["status"] for c in isvc.get("status", {}).get("conditions", [])}
        out["inferenceservices"][f"{isvc['metadata']['namespace']}/{isvc['metadata']['name']}"] = \
            conditions.get("Ready") == "True"
    for node in nodes:
        pressure = [c["type"] for c in node["status"]["conditions"] if c["type"].endswith("Pressure")
                    and c["status"] == "True"]
        out["node_pressure"][node["metadata"]["name"]] = pressure
    out["healthy"] = (all(p["ready"] for p in out["pods"].values()) and all(out["inferenceservices"].values())
                      and not any(out["node_pressure"].values()))
    return out


def regressions(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """New MLOps problems relative to the baseline (pre-existing ones are not O1's)."""

    found = []
    for name, before in baseline["pods"].items():
        now = current["pods"].get(name)
        if now is None:
            found.append(f"{name} disappeared")
            continue
        if before["ready"] and not now["ready"]:
            found.append(f"{name} not Ready")
        if now["restarts"] > before["restarts"]:
            found.append(f"{name} restarted ({before['restarts']} -> {now['restarts']})")
        if now["node"] != before["node"]:
            found.append(f"{name} moved {before['node']} -> {now['node']}")
    for name, ready in baseline["inferenceservices"].items():
        if ready and not current["inferenceservices"].get(name, False):
            found.append(f"InferenceService {name} not Ready")
    for node, pressure in current["node_pressure"].items():
        if pressure:
            found.append(f"node {node} pressure {pressure}")
    return found


def evaluate_guard(sample: dict[str, Any], thresholds: dict[str, float] = THRESHOLDS) -> list[str]:
    reasons = list(sample.get("mlops_regressions", []))
    for node in sample.get("nodes", []):
        if node["o1"]:
            if node["mem_pct"] > thresholds["o1_node_mem_pct"]:
                reasons.append(f"O1 node {node['name']} memory {node['mem_pct']}%")
        else:
            if node["cpu_pct"] > thresholds["mlops_node_cpu_pct"]:
                reasons.append(f"MLOps node {node['name']} CPU {node['cpu_pct']}%")
            if node["mem_pct"] > thresholds["mlops_node_mem_pct"]:
                reasons.append(f"MLOps node {node['name']} memory {node['mem_pct']}%")
    if sample.get("vllm_mem_mib", 0) / 1024 > thresholds["vllm_mem_gib"]:
        reasons.append(f"vLLM memory {sample['vllm_mem_mib']} MiB")
    reasons += [f"event {e}" for e in sample.get("oom_or_eviction_events", [])]
    return reasons


# --- collection (kubectl, explicit context) -------------------------------------------------

def _kubectl(context: str, *args: str) -> str:
    return subprocess.run(["kubectl", "--context", context, "--request-timeout=30s", *args],
                          capture_output=True, text=True, check=True, timeout=90).stdout


def _json(context: str, *args: str) -> list[dict[str, Any]]:
    return json.loads(_kubectl(context, *args, "-o", "json"))["items"]


def collect_health(context: str) -> dict[str, Any]:
    try:
        isvcs = _json(context, "get", "inferenceservices.serving.kserve.io", "-A")
    except subprocess.CalledProcessError:
        isvcs = []
    return health(_json(context, "get", "pods", "-A"), isvcs, _json(context, "get", "nodes"))


def _top(context: str, *args: str) -> list[list[str]]:
    return [line.split() for line in _kubectl(context, "top", *args, "--no-headers").splitlines() if line.strip()]


def collect_sample(context: str, baseline: dict[str, Any], since: str) -> dict[str, Any]:
    labels = {n["metadata"]["name"]: (n["metadata"].get("labels") or {}).get(O1_LABEL) == "true"
              for n in _json(context, "get", "nodes")}
    nodes = [{"name": r[0], "o1": labels.get(r[0], False), "cpu_m": int(r[1].rstrip("m")),
              "cpu_pct": float(r[2].rstrip("%")), "mem_mib": int(r[3].rstrip("Mi")), "mem_pct": float(r[4].rstrip("%"))}
             for r in _top(context, "nodes")]
    pods = {r[0]: (int(r[1].rstrip("m")), int(r[2].rstrip("Mi"))) for r in _top(context, "pods", "-n", LLM_NS)}
    vllm = [v for k, v in pods.items() if k.startswith("vllm-primary")]
    gateway = [v for k, v in pods.items() if k.startswith("agentgateway")]
    mlops = {}
    for ns in MLOPS_NAMESPACES:
        try:
            for r in _top(context, "pods", "-n", ns):
                mlops[f"{ns}/{r[0]}"] = (int(r[1].rstrip("m")), int(r[2].rstrip("Mi")))
        except subprocess.CalledProcessError:
            pass
    events = []
    for line in _kubectl(context, "get", "events", "-A", "--field-selector", "type=Warning",
                         "-o", "jsonpath={range .items[*]}{.lastTimestamp} {.reason} "
                               "{.involvedObject.namespace}/{.involvedObject.name}{'\\n'}{end}").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] >= since and parts[1] in ("OOMKilling", "OOMKilled", "Evicted", "Preempted"):
            events.append(line)
    current = collect_health(context)
    return {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "nodes": nodes,
            "vllm_cpu_m": vllm[0][0] if vllm else 0, "vllm_mem_mib": vllm[0][1] if vllm else 0,
            "gateway_cpu_m": sum(v[0] for v in gateway), "gateway_mem_mib": sum(v[1] for v in gateway),
            "mlops_cpu_m": sum(v[0] for v in mlops.values()), "mlops_mem_mib": sum(v[1] for v in mlops.values()),
            "mlops_healthy": current["healthy"], "mlops_regressions": regressions(baseline, current),
            "oom_or_eviction_events": events}


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
        result = capacity(_json(args.context, "get", "nodes"), _json(args.context, "get", "pods", "-A"),
                          json.loads(args.planned))
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({k: result[k] for k in ("verdict", "o1_nodes", "placement", "unplaced")}, indent=1))
        return 0 if result["verdict"] == "FITS_SAFE_BUDGET" else 1
    if args.mode == "health":
        result = collect_health(args.context)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"healthy": result["healthy"], "pods": len(result["pods"]),
                          "inferenceservices": result["inferenceservices"]}))
        return 0 if result["healthy"] else 1
    baseline = json.loads(args.baseline.read_text())
    since = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    while True:
        try:
            sample = collect_sample(args.context, baseline, since)
        except (subprocess.SubprocessError, ValueError, IndexError) as exc:
            sample = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "collect_error": f"{type(exc).__name__}: {exc}"[:200]}
        reasons = evaluate_guard(sample) if "collect_error" not in sample else []
        sample["guard_reasons"] = reasons
        with args.output.open("a") as handle:
            handle.write(json.dumps(sample) + "\n")
        if reasons and not args.stop_file.exists():
            args.stop_file.write_text(json.dumps({"ts": sample["ts"], "reasons": reasons}) + "\n")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
