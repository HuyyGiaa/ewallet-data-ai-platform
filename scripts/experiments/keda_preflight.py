#!/usr/bin/env python3
"""Read-only checks before KEDA is installed or an agent moves to it.

Asks the cluster and changes nothing, apart from a short local port-forward to Prometheus.
Each check is READY, WAITING (expected before a later approved step) or BLOCKED. The exit
code is 1 only when something is BLOCKED.

    python scripts/experiments/keda_preflight.py --context "$KUBE_CONTEXT" --stage before-install
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import quote
from urllib.request import urlopen

AGENTS = ("drift-specialist", "feature-rag-specialist", "fraud-coordinator")
NAMESPACE = "ewallet-agents"
LLM_NODE_LABEL = "ewallet.io/g3-llm"
READY, WAITING, BLOCKED = "READY", "WAITING", "BLOCKED"
LOCAL_PORT = 19091


@dataclass(frozen=True)
class Check:
    """One preflight result."""

    name: str
    status: str
    detail: str


def cpu(value: str) -> float:
    """Convert a Kubernetes CPU quantity to cores."""

    return float(value[:-1]) / 1000 if value.endswith("m") else float(value)


def memory_mib(value: str) -> float:
    """Convert a Kubernetes memory quantity to MiB."""

    # Binary units first, so "Mi" is not read as "M".
    for suffix, factor in (("Ki", 1 / 1024), ("Mi", 1.0), ("Gi", 1024.0),
                           ("k", 1000 / 1048576), ("M", 1e6 / 1048576), ("G", 1e9 / 1048576)):
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * factor
    return float(value) / 1048576


def free_capacity(nodes: Mapping[str, Any], pods: Mapping[str, Any], label: str) -> dict[str, tuple[float, float]]:
    """Return allocatable minus requested CPU and MiB for each node carrying ``label``."""

    free = {}
    for node in nodes["items"]:
        if node["metadata"].get("labels", {}).get(label) != "true":
            continue
        name = node["metadata"]["name"]
        allocatable = node["status"]["allocatable"]
        used_cpu = used_memory = 0.0
        for pod in pods["items"]:
            if pod["spec"].get("nodeName") != name or pod["status"].get("phase") not in {"Running", "Pending"}:
                continue
            for container in pod["spec"]["containers"]:
                requests = container.get("resources", {}).get("requests", {})
                used_cpu += cpu(requests.get("cpu", "0"))
                used_memory += memory_mib(requests.get("memory", "0"))
        free[name] = (cpu(allocatable["cpu"]) - used_cpu, memory_mib(allocatable["memory"]) - used_memory)
    return free


def worst_case_fits(free: Mapping[str, tuple[float, float]], extra: Sequence[tuple[float, float]]) -> bool:
    """Place each extra replica, largest first, on the node with the most CPU left."""

    room = {name: list(value) for name, value in free.items()}
    for need_cpu, need_memory in sorted(extra, reverse=True):
        candidates = [name for name, (c, m) in room.items() if c >= need_cpu and m >= need_memory]
        if not candidates:
            return False
        best = max(candidates, key=lambda name: room[name][0])
        room[best][0] -= need_cpu
        room[best][1] -= need_memory
    return True


def capacity_check(nodes: Mapping[str, Any], pods: Mapping[str, Any], deployments: Mapping[str, Any], maximum: int) -> Check:
    """Check that every agent can reach ``maximum`` replicas at the same time."""

    extra: list[tuple[float, float]] = []
    for item in deployments["items"]:
        if item["metadata"]["name"] not in AGENTS:
            continue
        requests = item["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
        extra += [(cpu(requests["cpu"]), memory_mib(requests["memory"]))] * max(0, maximum - item["spec"]["replicas"])
    free = free_capacity(nodes, pods, LLM_NODE_LABEL)
    shown = ", ".join(f"{name[-4:]} {c:.2f} CPU {m / 1024:.1f} GiB" for name, (c, m) in sorted(free.items()))
    fits = worst_case_fits(free, extra)
    return Check("node capacity at maximum replicas", READY if fits else BLOCKED, f"{len(extra)} extra replicas; free: {shown}")


def hpa_check(hpas: Mapping[str, Any], stage: str) -> Check:
    """Check autoscaler ownership: one per agent, and none plain once KEDA owns it."""

    plain = sorted(item["metadata"]["name"] for item in hpas["items"] if item["metadata"]["name"] in AGENTS)
    ranges = {item["metadata"]["name"]: (item["spec"].get("minReplicas"), item["spec"]["maxReplicas"]) for item in hpas["items"]}
    bad = [name for name, bounds in ranges.items() if name in AGENTS and bounds != (2, 3)]
    if bad:
        return Check("autoscaler ownership", BLOCKED, f"unexpected range on {bad}")
    if stage == "before-scaledobject" and plain:
        return Check("autoscaler ownership", WAITING, f"delete {plain} immediately before the upgrade; the webhook refuses both")
    return Check("autoscaler ownership", READY, f"plain HPAs: {plain}")


def kubectl_json(context: str, *arguments: str) -> dict[str, Any]:
    """Read one object list as JSON."""

    output = subprocess.run(["kubectl", "--context", context, "--request-timeout=60s", *arguments, "-o", "json"],
                            capture_output=True, text=True, timeout=90, check=True).stdout
    return json.loads(output)


def object_exists(context: str, *arguments: str) -> bool:
    """Return whether an object or CRD exists; a failed read raises."""

    output = subprocess.run(["kubectl", "--context", context, "--request-timeout=60s", *arguments, "--ignore-not-found", "-o", "name"],
                            capture_output=True, text=True, timeout=90, check=True).stdout
    return bool(output.strip())


def prometheus_series(context: str, queries: Sequence[str]) -> dict[str, int]:
    """Count series per metric through a short port-forward to the Prometheus Service."""

    forward = subprocess.Popen(["kubectl", "--context", context, "-n", NAMESPACE, "port-forward", "svc/prometheus",
                                f"{LOCAL_PORT}:9090"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        counts = {}
        for query in queries:
            url = f"http://127.0.0.1:{LOCAL_PORT}/api/v1/series?match[]={quote(query)}"
            with urlopen(url, timeout=15) as response:
                counts[query] = len(json.loads(response.read())["data"])
        return counts
    finally:
        forward.terminate()
        forward.wait(timeout=10)


def run_checks(context: str, stage: str, maximum: int) -> list[Check]:
    """Run every check against the cluster."""

    pods = kubectl_json(context, "get", "pods", "-A")
    nodes = kubectl_json(context, "get", "nodes")
    deployments = kubectl_json(context, "-n", NAMESPACE, "get", "deployments")
    hpas = kubectl_json(context, "-n", NAMESPACE, "get", "hpa")
    pending = [item["metadata"]["name"] for item in pods["items"] if item["status"].get("phase") == "Pending"]
    series = prometheus_series(context, ["ewallet_agent_invocations_total", "ewallet_agent_inflight_tasks"])
    crd = object_exists(context, "get", "crd", "scaledobjects.keda.sh")
    access = [object_exists(context, "-n", NAMESPACE, "get", "authorizationpolicy", "allow-keda-operator-to-prometheus"),
              object_exists(context, "-n", NAMESPACE, "get", "networkpolicy", "allow-keda-operator-to-prometheus-ingress")]
    version = kubectl_json(context, "version")["serverVersion"]["gitVersion"]
    return [
        Check("kubernetes version", READY, f"{version}; KEDA 2.21 chart declares >=1.23"),
        Check("pending pods", BLOCKED if pending else READY, f"{len(pending)} pending"),
        capacity_check(nodes, pods, deployments, maximum),
        hpa_check(hpas, stage),
        Check("KEDA installed", READY if crd or stage == "before-install" else BLOCKED,
              "present" if crd else "absent, as expected before the install" if stage == "before-install" else "absent"),
        Check("Prometheus reachable with agent metrics", READY if series["ewallet_agent_invocations_total"] else BLOCKED,
              f"{series['ewallet_agent_invocations_total']} invocation series"),
        Check("in-flight gauge exported", READY if series["ewallet_agent_inflight_tasks"] else WAITING,
              f"{series['ewallet_agent_inflight_tasks']} series; needs the rebuilt agent images"),
        Check("Prometheus access rules", READY if all(access) else WAITING, f"{sum(access)}/2 present"),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Print the checks; exit 1 when one is BLOCKED."""

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--context", required=True)
    parser.add_argument("--stage", choices=("before-install", "before-scaledobject"), default="before-install")
    parser.add_argument("--max-replicas", type=int, default=3)
    args = parser.parse_args(argv)
    checks = run_checks(args.context, args.stage, args.max_replicas)
    for check in checks:
        print(f"{check.status:8} {check.name}: {check.detail}")
    return 1 if any(check.status == BLOCKED for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
