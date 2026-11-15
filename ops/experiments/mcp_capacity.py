"""Scheduling preflight for the isolated MCP failover experiment (EXP-C5-MCP, AGR-02).

Reads the rendered experiment manifest for what must schedule: the MCP Deployment's replicas,
CPU and memory requests, required node affinity and hostname spread, plus the probe Job's
requests. Then it reads the cluster's nodes and pods and decides, before anything is created,
whether every pod can be placed. It mutates nothing.

The MCP pods must avoid nodes labelled ``ewallet.io/g3-llm`` (they are reserved for vLLM and
the agents). The optimized arm spreads its two replicas over hostnames, so it needs two
distinct eligible nodes with room for one replica each; the check refuses the single-node
placement that Kubernetes itself would allow.

    python -m ops.experiments.mcp_capacity --rendered <arm config> --context <ctx> [--out report.json]
Exit 0: SCHEDULABLE. Exit 3: CAPACITY_INSUFFICIENT (the report says why).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable, Mapping

import yaml

TERMINAL = {"Succeeded", "Failed"}


def cpu_m(value: str | int | float | None) -> float:
    if value in (None, ""):
        return 0.0
    text = str(value)
    return float(text[:-1]) if text.endswith("m") else float(text) * 1000


def mem_mi(value: str | int | float | None) -> float:
    if value in (None, ""):
        return 0.0
    text = str(value)
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024, "k": 1000 / 1048576, "M": 1e6 / 1048576, "G": 1e9 / 1048576}
    for suffix, factor in units.items():
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text) / 1048576


def pod_requests(spec: Mapping[str, Any]) -> tuple[float, float]:
    """Effective requests: max(sum of containers, largest init container)."""
    containers = spec.get("containers") or []
    cpu = sum(cpu_m(((c.get("resources") or {}).get("requests") or {}).get("cpu")) for c in containers)
    mem = sum(mem_mi(((c.get("resources") or {}).get("requests") or {}).get("memory")) for c in containers)
    for init in spec.get("initContainers") or []:
        requests = (init.get("resources") or {}).get("requests") or {}
        cpu, mem = max(cpu, cpu_m(requests.get("cpu"))), max(mem, mem_mi(requests.get("memory")))
    return cpu, mem


def requirements(rendered: str) -> dict[str, Any]:
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    server = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "mcp-exp-drift")
    probe = next(d for d in docs if d["kind"] == "Job" and d["metadata"]["name"] == "mcp-exp-probe")
    spec = server["spec"]["template"]["spec"]
    excluded = []
    terms = (((spec.get("affinity") or {}).get("nodeAffinity") or {}).get("requiredDuringSchedulingIgnoredDuringExecution") or {}).get("nodeSelectorTerms") or []
    for term in terms:
        for expression in term.get("matchExpressions") or []:
            if expression.get("operator") == "DoesNotExist":
                excluded.append(expression["key"])
    spread = any(c.get("topologyKey") == "kubernetes.io/hostname" and c.get("whenUnsatisfiable") == "DoNotSchedule"
                 for c in spec.get("topologySpreadConstraints") or [])
    cpu, mem = pod_requests(spec)
    probe_cpu, probe_mem = pod_requests(probe["spec"]["template"]["spec"])
    return {"replicas": int(server["spec"]["replicas"]), "cpu_m": cpu, "mem_mi": mem, "excluded_labels": sorted(excluded),
            "hostname_spread": spread, "probe_cpu_m": probe_cpu, "probe_mem_mi": probe_mem}


def free_capacity(nodes: Iterable[Mapping[str, Any]], pods: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    used: dict[str, list[float]] = {}
    for pod in pods:
        node = (pod.get("spec") or {}).get("nodeName")
        if not node or (pod.get("status") or {}).get("phase") in TERMINAL:
            continue
        cpu, mem = pod_requests(pod["spec"])
        totals = used.setdefault(node, [0.0, 0.0])
        totals[0] += cpu
        totals[1] += mem
    result = []
    for node in nodes:
        name = node["metadata"]["name"]
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in (node.get("status") or {}).get("conditions") or [])
        allocatable = (node.get("status") or {}).get("allocatable") or {}
        cpu, mem = used.get(name, [0.0, 0.0])
        result.append({"name": name, "labels": (node["metadata"].get("labels") or {}), "ready": ready,
                       "schedulable": not (node.get("spec") or {}).get("unschedulable", False) and not any(
                           t.get("effect") in ("NoSchedule", "NoExecute") for t in (node.get("spec") or {}).get("taints") or []),
                       "free_cpu_m": cpu_m(allocatable.get("cpu")) - cpu, "free_mem_mi": mem_mi(allocatable.get("memory")) - mem})
    return result


def decide(need: Mapping[str, Any], capacity: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [n for n in capacity if n["ready"] and n["schedulable"]]
    eligible = [n for n in usable if not any(label in n["labels"] for label in need["excluded_labels"])]
    fits_one = [n for n in eligible if n["free_cpu_m"] >= need["cpu_m"] and n["free_mem_mi"] >= need["mem_mi"]]
    reasons = []
    replicas = need["replicas"]
    if need["hostname_spread"] and replicas > 1:
        # The template spreads replicas over hostnames so that one pod loss leaves a replica on
        # another node. Kubernetes would let both land on a single eligible node (the spread only
        # counts eligible domains), which silently weakens the experiment, so require one
        # eligible node with room per replica.
        if len(fits_one) < replicas:
            reasons.append(f"{replicas} replicas need {replicas} distinct eligible nodes with {need['cpu_m']:.0f}m/"
                           f"{need['mem_mi']:.0f}Mi free each; {len(fits_one)} of {len(eligible)} eligible nodes have it")
    elif not eligible:
        reasons.append(f"no Ready node without {need['excluded_labels']}")
    elif not any(n["free_cpu_m"] >= need["cpu_m"] * replicas and n["free_mem_mi"] >= need["mem_mi"] * replicas for n in eligible):
        reasons.append(f"no eligible node has {need['cpu_m'] * replicas:.0f}m/{need['mem_mi'] * replicas:.0f}Mi free "
                       f"for {replicas} replica(s)")
    if not any(n["free_cpu_m"] >= need["probe_cpu_m"] and n["free_mem_mi"] >= need["probe_mem_mi"] for n in usable):
        reasons.append(f"no node has {need['probe_cpu_m']:.0f}m/{need['probe_mem_mi']:.0f}Mi free for the probe Job")
    return {"verdict": "SCHEDULABLE" if not reasons else "CAPACITY_INSUFFICIENT", "requirements": dict(need),
            "eligible_nodes": [{k: n[k] for k in ("name", "free_cpu_m", "free_mem_mi")} for n in eligible],
            "usable_nodes": len(usable), "reasons": reasons}


def _kubectl(context: str, *args: str) -> Any:
    out = subprocess.run(["kubectl", "--context", context, "--request-timeout=60s", *args, "-o", "json"],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)["items"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rendered", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    need = requirements(args.rendered.read_text())
    report = decide(need, free_capacity(_kubectl(args.context, "get", "nodes"), _kubectl(args.context, "get", "pods", "-A")))
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)
    return 0 if report["verdict"] == "SCHEDULABLE" else 3


if __name__ == "__main__":
    sys.exit(main())
