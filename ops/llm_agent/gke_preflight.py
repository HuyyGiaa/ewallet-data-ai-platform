"""Read-only GO/NO-GO before one GKE CPU stage on cluster ewallet-dev.

Stages (docs/llm_agent/gke_cpu_runtime.md):
  g0 Istio ambient   g1 vLLM + agentgateway + ModelConfig   g2 + llm-d
  g3 + Substrate/kagent agents, MCP and A2A gateways

Collects, without mutating anything: the cluster's endpoints and NetworkPolicy
enforcement (gcloud describe), the kubeconfig server of the given context,
Ready nodes and allocatable CPU/memory, requests of every non-terminal pod
outside ewallet-agents (the release replaces those), the stage's planned
requests (helm template of values-gke-cpu.yaml plus the stage --set flags),
required CRDs, the vllm-api-key Secret by name only, and Helm releases.

GO needs: the exact allow-listed context and a server that is one of the
cluster's endpoints; NetworkPolicy enforced (or an explicit, recorded owner
waiver); Ready nodes; existing + planned requests <= 85 % of allocatable;
every CRD of the stage; the Secret from g1 on.

usage: python3 -m ops.llm_agent.gke_preflight --stage g1 --context <ctx> [--kubeconfig f] \
           [--set key=value ...] [--waive-unenforced-networkpolicy] [--json-output f.json]
exit 0 = GO, 1 = NO-GO.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
PROJECT, ZONE, CLUSTER = "ewallet-data-ai-platform-hg26", "asia-southeast1-b", "ewallet-dev"
EXPECTED_CONTEXT = f"gke_{PROJECT}_{ZONE}_{CLUSTER}"
NAMESPACE = "ewallet-agents"
HEADROOM = 0.85
# Istio ambient after g0, from the rendered 1.30.7 charts with values-gke.yaml.
ISTIOD = (0.1, 0.25)
ISTIO_PER_NODE = (0.2 + 0.1, 0.5 + 0.1)  # ztunnel + istio-cni
STAGES: dict[str, dict[str, Any]] = {
    "g0": {"sets": None, "crds": []},
    "g1": {"sets": [], "crds": ["authorizationpolicies.security.istio.io", "modelconfigs.kagent.dev"]},
    "g2": {"sets": ["llmd.enabled=true"],
           "crds": ["inferencepools.inference.networking.k8s.io", "httproutes.gateway.networking.k8s.io"]},
    "g3": {"sets": ["llmd.enabled=true", "agents.enabled=true", "mcpGateway.enabled=true",
                    "a2aGateway.enabled=true"],
           "crds": ["sandboxagents.kagent.dev", "remotemcpservers.kagent.dev", "workerpools.ate.dev"]},
}
ORDER = list(STAGES)


def cpu(value: str) -> float:
    return float(value[:-1]) / 1000 if value.endswith("m") else float(value)


def mem_gib(value: str) -> float:
    number, unit = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", value).groups()
    scale = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 1e3, "M": 1e6, "G": 1e9, "": 1}[unit]
    return float(number) * scale / 2**30


def requests_of(containers: list[dict[str, Any]]) -> tuple[float, float]:
    total = [0.0, 0.0]
    for container in containers:
        req = (container.get("resources") or {}).get("requests") or {}
        total[0] += cpu(str(req.get("cpu", "0")))
        total[1] += mem_gib(str(req.get("memory", "0")))
    return total[0], total[1]


def planned_requests(rendered: str) -> tuple[float, float]:
    """Sum replicas x container requests of rendered Deployments/StatefulSets."""

    total = [0.0, 0.0]
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") not in ("Deployment", "StatefulSet"):
            continue
        replicas = doc["spec"].get("replicas", 1)
        c, m = requests_of(doc["spec"]["template"]["spec"]["containers"])
        total[0] += replicas * c
        total[1] += replicas * m
    return total[0], total[1]


def stage_crds(stage: str) -> list[str]:
    return [crd for name in ORDER[: ORDER.index(stage) + 1] for crd in STAGES[name]["crds"]]


def evaluate(facts: dict[str, Any], stage: str, *, waive_networkpolicy: bool = False) -> list[str]:
    reasons = []
    if facts["context"] != EXPECTED_CONTEXT:
        reasons.append(f"context {facts['context']!r} is not {EXPECTED_CONTEXT}")
    if facts["server_host"] not in facts["cluster_endpoints"]:
        reasons.append(f"kubeconfig server {facts['server_host']} is not an endpoint of {CLUSTER}")
    if not facts["networkpolicy_enforced"] and not waive_networkpolicy:
        reasons.append("NetworkPolicy is not enforced on the cluster (enable it, or record an owner waiver)")
    if facts["ready_nodes"] == 0:
        reasons.append("no Ready nodes")
    alloc_cpu, alloc_mem = facts["allocatable"]
    need_cpu = facts["existing_requests"][0] + facts["planned_requests"][0]
    need_mem = facts["existing_requests"][1] + facts["planned_requests"][1]
    if need_cpu > HEADROOM * alloc_cpu:
        reasons.append(f"CPU requests {need_cpu:.2f} > {HEADROOM:.0%} of allocatable {alloc_cpu:.2f}")
    if need_mem > HEADROOM * alloc_mem:
        reasons.append(f"memory requests {need_mem:.2f} GiB > {HEADROOM:.0%} of allocatable {alloc_mem:.2f} GiB")
    missing = [crd for crd in stage_crds(stage) if crd not in facts["crds"]]
    if missing:
        reasons.append(f"missing CRDs {missing}")
    if stage != "g0" and not facts["api_key_secret"]:
        reasons.append(f"Secret {NAMESPACE}/vllm-api-key missing")
    return reasons


def _run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=120).stdout


def collect(stage: str, context: str, extra_sets: list[str]) -> dict[str, Any]:
    cluster = json.loads(_run("gcloud", "container", "clusters", "describe", CLUSTER, "--zone", ZONE,
                              "--project", PROJECT, "--format=json"))
    endpoints = {cluster.get("endpoint"), cluster.get("privateClusterConfig", {}).get("privateEndpoint"),
                 cluster.get("controlPlaneEndpointsConfig", {}).get("dnsEndpointConfig", {}).get("endpoint")}
    enforced = bool((cluster.get("networkPolicy") or {}).get("enabled")) or \
        (cluster.get("networkConfig") or {}).get("datapathProvider") == "ADVANCED_DATAPATH"
    server = _run("kubectl", "config", "view", "--minify", "--context", context,
                  "-o", "jsonpath={.clusters[0].cluster.server}")
    kubectl = ("kubectl", "--context", context, "--request-timeout=60s")
    nodes = json.loads(_run(*kubectl, "get", "nodes", "-o", "json"))["items"]
    ready = [n for n in nodes if any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"])]
    allocatable = (sum(cpu(n["status"]["allocatable"]["cpu"]) for n in ready),
                   sum(mem_gib(n["status"]["allocatable"]["memory"]) for n in ready))
    existing = [0.0, 0.0]
    istio_running = False
    for pod in json.loads(_run(*kubectl, "get", "pods", "-A", "-o", "json"))["items"]:
        istio_running |= pod["metadata"]["namespace"] == "istio-system"
        if pod["metadata"]["namespace"] == NAMESPACE or pod["status"].get("phase") in ("Succeeded", "Failed"):
            continue
        c, m = requests_of(pod["spec"]["containers"])
        existing[0] += c
        existing[1] += m
    planned = [0.0, 0.0]
    if not istio_running:
        planned = [ISTIOD[0] + ISTIO_PER_NODE[0] * len(ready), ISTIOD[1] + ISTIO_PER_NODE[1] * len(ready)]
    if STAGES[stage]["sets"] is not None:
        args = ["helm", "template", "llm-agent", str(CHART), "-f", str(CHART / "values-gke-cpu.yaml")]
        for item in STAGES[stage]["sets"] + extra_sets:
            args += ["--set", item]
        c, m = planned_requests(_run(*args))
        planned = [planned[0] + c, planned[1] + m]
    crds = set(_run(*kubectl, "get", "crd", "-o", "name").replace("customresourcedefinition.apiextensions.k8s.io/", "").split())
    secrets = _run(*kubectl, "-n", NAMESPACE, "get", "secret", "--ignore-not-found", "-o", "name").split()
    releases = json.loads(_run("helm", "list", "-A", "--kube-context", context, "-o", "json") or "[]")
    return {"context": context, "server_host": urlparse(server).hostname or server,
            "cluster_endpoints": sorted(e for e in endpoints if e), "networkpolicy_enforced": enforced,
            "ready_nodes": len(ready), "nodes": [n["metadata"]["name"] for n in ready],
            "allocatable": [round(v, 2) for v in allocatable], "existing_requests": [round(v, 2) for v in existing],
            "planned_requests": [round(v, 2) for v in planned], "crds": sorted(crds),
            "api_key_secret": "secret/vllm-api-key" in secrets,
            "helm_releases": [{k: r.get(k) for k in ("name", "namespace", "status", "chart")} for r in releases]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=ORDER, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--kubeconfig")
    # e.g. agents.snapshotsLocation=gs://<approved bucket>/<prefix>/ for g3.
    parser.add_argument("--set", dest="sets", action="append", default=[])
    parser.add_argument("--waive-unenforced-networkpolicy", action="store_true")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    if args.kubeconfig:
        os.environ["KUBECONFIG"] = args.kubeconfig
    facts = collect(args.stage, args.context, args.sets)
    reasons = evaluate(facts, args.stage, waive_networkpolicy=args.waive_unenforced_networkpolicy)
    facts.pop("crds")
    result = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "stage": args.stage,
              "networkpolicy_waiver": args.waive_unenforced_networkpolicy, **facts,
              "required_crds": stage_crds(args.stage), "verdict": "GO" if not reasons else "NO-GO",
              "reasons": reasons}
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if not reasons else 1


if __name__ == "__main__":
    sys.exit(main())
