"""Refuse an edge rollout that could create a second ingress workload or a LoadBalancer.

The external gateway is one Deployment and one ClusterIP Service owned by the istio/gateway
Helm release in ewallet-ingress. A Gateway API Gateway without ``spec.addresses`` makes Istio
provision its own Deployment and, by default, a LoadBalancer Service. The legacy Gateway in
the llm-agent-platform chart is such a Gateway, rendered by ``values-gcp.yaml``.

``render``: checks manifests (``helm template`` output) before anything is applied.
``live``: reads a cluster (get only) before and after a rollout.

Report only: the script never changes or deletes anything. A finding means "stop and
review", not "remove". Resources the owner has reviewed and approved, such as an existing
LoadBalancer or another team's ingress Gateway, are passed with ``--allow-loadbalancer``
or ``--allow-gateway`` (namespace/name) and are then not reported.
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


def gateway_problems(gateways: Iterable[dict], allowed: frozenset[str] = frozenset()) -> list[str]:
    """Every Gateway must be the edge Gateway, bound to the Helm-managed Service, unless the
    owner approved it (allowed lists namespace/name)."""
    problems = []
    for gateway in gateways:
        if gateway.get("spec", {}).get("gatewayClassName") != INGRESS_CLASS:
            continue
        where = f"{gateway['metadata'].get('namespace', '?')}/{gateway['metadata']['name']}"
        if where in allowed:
            continue
        if (gateway["metadata"].get("namespace"), gateway["metadata"]["name"]) != (EDGE_NAMESPACE, EDGE_GATEWAY):
            problems.append(f"unexpected ingress Gateway {where} (only {EDGE_NAMESPACE}/{EDGE_GATEWAY} may exist)")
        addresses = gateway.get("spec", {}).get("addresses") or []
        if addresses != [{"type": "Hostname", "value": EDGE_ADDRESS}]:
            problems.append(f"Gateway {where} is not bound to {EDGE_ADDRESS}: Istio would provision its own workload")
    return problems


def balancer_problems(service: dict, allowed_lb: frozenset[str]) -> list[str]:
    """A LoadBalancer is a problem unless the owner approved it by name. The approved edge
    Service must also keep the client address (externalTrafficPolicy Local) and filter it at
    the cloud firewall (loadBalancerSourceRanges, never 0.0.0.0/0); other approved ones are
    someone else's and are only reported if unapproved."""
    spec = service.get("spec", {})
    if spec.get("type") != "LoadBalancer":
        return []
    where = f"{service['metadata'].get('namespace', '?')}/{service['metadata']['name']}"
    if where not in allowed_lb:
        return [f"LoadBalancer Service {where}"]
    if where != f"{EDGE_NAMESPACE}/{EDGE_SERVICE}":
        return []
    problems = []
    ranges = spec.get("loadBalancerSourceRanges") or []
    if not ranges or any(r.endswith("/0") for r in ranges):
        problems.append(f"approved LoadBalancer {where} has no source-range restriction")
    if spec.get("externalTrafficPolicy") != "Local":
        problems.append(f"approved LoadBalancer {where} does not preserve the client address (externalTrafficPolicy Local)")
    return problems


def render_problems(documents: Iterable[dict], allowed_lb: frozenset[str] = frozenset()) -> list[str]:
    """Problems in rendered manifests."""
    documents = [doc for doc in documents if doc]
    problems = gateway_problems(d for d in documents if d["apiVersion"].startswith(GATEWAY_API) and d["kind"] == "Gateway")
    for d in documents:
        if d["kind"] == "Service":
            d = {**d, "metadata": {"namespace": EDGE_NAMESPACE, **d["metadata"]}} if d["metadata"]["name"] == EDGE_SERVICE else d
            problems += balancer_problems(d, allowed_lb)
    return problems


def live_problems(gateways: list[dict], deployments: list[dict], services: list[dict],
                  allowed_lb: frozenset[str] = frozenset(), allowed_gateways: frozenset[str] = frozenset()) -> list[str]:
    """Problems in a cluster's current state; allowed_* list owner-approved namespace/name."""
    problems = gateway_problems(gateways, allowed_gateways)
    labels = lambda d: d["metadata"].get("labels", {})
    managed = [d for d in deployments if labels(d).get("gateway.istio.io/managed") == "istio.io-gateway-controller"
               and f"{d['metadata']['namespace']}/{labels(d).get('gateway.networking.k8s.io/gateway-name')}" not in allowed_gateways]
    problems += [f"Istio-provisioned gateway Deployment {d['metadata']['namespace']}/{d['metadata']['name']}" for d in managed]
    for service in services:
        problems += balancer_problems(service, allowed_lb)
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
    render.add_argument("--allow-loadbalancer", action="append", default=[], metavar="NAMESPACE/NAME",
                        help="an owner-approved LoadBalancer Service (repeatable)")
    live = sub.add_parser("live", help="read a cluster (get only)")
    live.add_argument("--context", required=True)
    live.add_argument("--allow-loadbalancer", action="append", default=[], metavar="NAMESPACE/NAME",
                      help="an owner-approved LoadBalancer Service (repeatable)")
    live.add_argument("--allow-gateway", action="append", default=[], metavar="NAMESPACE/NAME",
                      help="an owner-approved ingress Gateway and the workload Istio made for it (repeatable)")
    args = parser.parse_args(argv)
    if args.mode == "render":
        documents = []
        for path in args.manifests:
            text = sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8")
            documents += list(yaml.safe_load_all(text))
        problems = render_problems(documents, frozenset(args.allow_loadbalancer))
    else:
        problems = live_problems(kubectl_items(args.context, "gateways.gateway.networking.k8s.io"),
                                 kubectl_items(args.context, "deployments"), kubectl_items(args.context, "services"),
                                 frozenset(args.allow_loadbalancer), frozenset(args.allow_gateway))
    for problem in problems:
        print(f"UNSAFE {problem}")
    if problems:
        print("Nothing was changed. Review each finding; approve existing resources with --allow-* instead of deleting them.")
    print("EDGE_OWNERSHIP_UNSAFE" if problems else "EDGE_OWNERSHIP_OK")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
