"""Refuse an edge rollout that could create a second ingress workload or a LoadBalancer.

The external gateway is one Deployment and one ClusterIP Service owned by the istio/gateway
Helm release in ewallet-ingress. A Gateway API Gateway without ``spec.addresses`` makes Istio
provision its own Deployment and, by default, a LoadBalancer Service. The legacy Gateway in
the llm-agent-platform chart is such a Gateway, rendered by ``values-gcp.yaml``.

``render``: checks manifests (``helm template`` output) before anything is applied.
``live``: reads a cluster (get only) before and after a rollout.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Iterable

import yaml

GATEWAY_API = "gateway.networking.k8s.io/"
EDGE_NAMESPACE, EDGE_GATEWAY, EDGE_SERVICE = "ewallet-ingress", "ewallet-edge", "ewallet-edge-gateway"
EDGE_ADDRESS = f"{EDGE_SERVICE}.{EDGE_NAMESPACE}.svc.cluster.local"
# Only the ingress class provisions ingress workloads; ambient waypoints (istio-waypoint) and
# other controllers' Gateways are not ingress and are left alone.
INGRESS_CLASS = "istio"


def gateway_problems(gateways: Iterable[dict]) -> list[str]:
    """Every Gateway must be the edge Gateway, bound to the Helm-managed Service."""
    problems = []
    for gateway in gateways:
        if gateway.get("spec", {}).get("gatewayClassName") != INGRESS_CLASS:
            continue
        where = f"{gateway['metadata'].get('namespace', '?')}/{gateway['metadata']['name']}"
        if (gateway["metadata"].get("namespace"), gateway["metadata"]["name"]) != (EDGE_NAMESPACE, EDGE_GATEWAY):
            problems.append(f"unexpected ingress Gateway {where} (only {EDGE_NAMESPACE}/{EDGE_GATEWAY} may exist)")
        addresses = gateway.get("spec", {}).get("addresses") or []
        if addresses != [{"type": "Hostname", "value": EDGE_ADDRESS}]:
            problems.append(f"Gateway {where} is not bound to {EDGE_ADDRESS}: Istio would provision its own workload")
    return problems


def render_problems(documents: Iterable[dict]) -> list[str]:
    """Problems in rendered manifests."""
    documents = [doc for doc in documents if doc]
    problems = gateway_problems(d for d in documents if d["apiVersion"].startswith(GATEWAY_API) and d["kind"] == "Gateway")
    problems += [f"Service {d['metadata'].get('namespace', '?')}/{d['metadata']['name']} is a LoadBalancer"
                 for d in documents if d["kind"] == "Service" and d.get("spec", {}).get("type") == "LoadBalancer"]
    return problems


def live_problems(gateways: list[dict], deployments: list[dict], services: list[dict],
                  allowed_lb: frozenset[str] = frozenset()) -> list[str]:
    """Problems in a cluster's current state; allowed_lb lists owner-approved namespace/name."""
    problems = gateway_problems(gateways)
    managed = [d for d in deployments if d["metadata"].get("labels", {}).get("gateway.istio.io/managed") == "istio.io-gateway-controller"]
    problems += [f"Istio-provisioned gateway Deployment {d['metadata']['namespace']}/{d['metadata']['name']}" for d in managed]
    problems += [f"LoadBalancer Service {s['metadata']['namespace']}/{s['metadata']['name']}"
                 for s in services if s["spec"].get("type") == "LoadBalancer"
                 and f"{s['metadata']['namespace']}/{s['metadata']['name']}" not in allowed_lb]
    edge = [s for s in services if (s["metadata"]["namespace"], s["metadata"]["name"]) == (EDGE_NAMESPACE, EDGE_SERVICE)]
    if edge and edge[0]["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != "Helm":
        problems.append(f"Service {EDGE_NAMESPACE}/{EDGE_SERVICE} is not managed by Helm")
    return problems


def kubectl_items(context: str, *resource: str) -> list[dict]:
    """List a resource in every namespace; a missing API is an empty list."""
    done = subprocess.run(["kubectl", "--context", context, "get", *resource, "-A", "-o", "json"],
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        if "the server doesn't have a resource type" in done.stderr:
            return []
        raise SystemExit(f"kubectl get {' '.join(resource)} failed: {done.stderr.strip()}")
    return json.loads(done.stdout)["items"]


def main(argv: list[str] | None = None) -> int:
    """Print every problem and fail if there is one."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    render = sub.add_parser("render", help="check helm template output")
    render.add_argument("manifests", nargs="+", type=Path, help="rendered YAML files ('-' for stdin)")
    live = sub.add_parser("live", help="read a cluster (get only)")
    live.add_argument("--context", required=True)
    live.add_argument("--allow-loadbalancer", action="append", default=[], metavar="NAMESPACE/NAME",
                      help="an owner-approved LoadBalancer Service (repeatable)")
    args = parser.parse_args(argv)
    if args.mode == "render":
        documents = []
        for path in args.manifests:
            text = sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8")
            documents += list(yaml.safe_load_all(text))
        problems = render_problems(documents)
    else:
        problems = live_problems(kubectl_items(args.context, "gateways.gateway.networking.k8s.io"),
                                 kubectl_items(args.context, "deployments"), kubectl_items(args.context, "services"),
                                 frozenset(args.allow_loadbalancer))
    for problem in problems:
        print(f"UNSAFE {problem}")
    print("EDGE_OWNERSHIP_UNSAFE" if problems else "EDGE_OWNERSHIP_OK")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
