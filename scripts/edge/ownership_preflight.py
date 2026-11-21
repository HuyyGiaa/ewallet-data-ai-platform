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


EDGE_LB = f"{EDGE_NAMESPACE}/{EDGE_SERVICE}"


def balancer_problems(service: dict, allowed_lb: frozenset[str], public: bool = False) -> list[str]:
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
    if not public and (not ranges or any(r.endswith("/0") for r in ranges)):
        problems.append(f"approved LoadBalancer {where} has no source-range restriction")
    if sorted(p.get("port") for p in spec.get("ports", [])) != [80, 443]:
        problems.append(f"approved LoadBalancer {where} must expose exactly ports 80 and 443")
    if spec.get("externalTrafficPolicy") != "Local":
        problems.append(f"approved LoadBalancer {where} does not preserve the client address (externalTrafficPolicy Local)")
    return problems


def public_security_problems(objects: Iterable[dict]) -> list[str]:
    """What an Internet-open edge LoadBalancer depends on, read from the edge objects (a render
    or the live cluster): Basic Auth and the rate limit on the HTTPS listener, the agents host
    behind the DENY source restriction on that listener (or not routed), and the ACME route
    pointing at the responder only."""
    objects = [o for o in objects if o]
    by = lambda kind, name: next((o for o in objects if o["kind"] == kind and o["metadata"]["name"] == name), None)
    problems = []
    envoy = by("EnvoyFilter", "edge-security")
    filters = {p.get("patch", {}).get("value", {}).get("name") for p in (envoy or {}).get("spec", {}).get("configPatches", [])}
    for needed in ("envoy.filters.http.basic_auth", "envoy.filters.http.local_ratelimit"):
        if needed not in filters:
            problems.append(f"public mode needs {needed} in EnvoyFilter edge-security")
    agents = by("HTTPRoute", "kagent-ui")
    if agents:
        policy = by("AuthorizationPolicy", "edge-source-restriction")
        rules = (policy or {}).get("spec", {}).get("rules", [])
        ops = [op.get("operation", {}) for r in rules for op in r.get("to", [])]
        hosts = {h for op in ops for h in op.get("hosts", [])}
        if not policy or policy["spec"].get("action") != "DENY" or not set(agents["spec"]["hostnames"]) <= hosts \
                or not all(r.get("from") and r["from"][0]["source"].get("notIpBlocks") for r in rules):
            problems.append("public mode needs the agents host behind AuthorizationPolicy edge-source-restriction (or the route disabled)")
    acme = by("HTTPRoute", "acme-http01")
    if acme:
        refs = [b["name"] for rule in acme["spec"]["rules"] for b in rule.get("backendRefs", [])]
        paths = [m["path"]["value"] for rule in acme["spec"]["rules"] for m in rule.get("matches", [])]
        if refs != ["edge-acme-responder"] or paths != ["/.well-known/acme-challenge/"]:
            problems.append("HTTPRoute acme-http01 must send only /.well-known/acme-challenge/ to edge-acme-responder")
    return problems


def render_problems(documents: Iterable[dict], allowed_lb: frozenset[str] = frozenset(),
                    public: bool = False) -> list[str]:
    """Problems in rendered manifests. ``public`` allows the edge LoadBalancer to accept any
    source only if the same render proves the controls it then relies on."""
    documents = [doc for doc in documents if doc]
    problems = gateway_problems(d for d in documents if d["apiVersion"].startswith(GATEWAY_API) and d["kind"] == "Gateway")
    for d in documents:
        if d["kind"] == "Service":
            d = {**d, "metadata": {"namespace": EDGE_NAMESPACE, **d["metadata"]}} if d["metadata"]["name"] == EDGE_SERVICE else d
            problems += balancer_problems(d, allowed_lb, public)
    if public:
        problems += public_security_problems(documents)
    return problems


def live_problems(gateways: list[dict], deployments: list[dict], services: list[dict],
                  allowed_lb: frozenset[str] = frozenset(), allowed_gateways: frozenset[str] = frozenset(),
                  public_objects: list[dict] | None = None) -> list[str]:
    """Problems in a cluster's current state; allowed_* list owner-approved namespace/name."""
    problems = gateway_problems(gateways, allowed_gateways)
    labels = lambda d: d["metadata"].get("labels", {})
    managed = [d for d in deployments if labels(d).get("gateway.istio.io/managed") == "istio.io-gateway-controller"
               and f"{d['metadata']['namespace']}/{labels(d).get('gateway.networking.k8s.io/gateway-name')}" not in allowed_gateways]
    problems += [f"Istio-provisioned gateway Deployment {d['metadata']['namespace']}/{d['metadata']['name']}" for d in managed]
    for service in services:
        problems += balancer_problems(service, allowed_lb, public_objects is not None)
    if public_objects is not None:
        problems += public_security_problems(public_objects)
    edge = [s for s in services if (s["metadata"]["namespace"], s["metadata"]["name"]) == (EDGE_NAMESPACE, EDGE_SERVICE)]
    if edge and edge[0]["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != "Helm":
        problems.append(f"Service {EDGE_NAMESPACE}/{EDGE_SERVICE} is not managed by Helm")
    return problems


def envoy_problems(context: str) -> list[str]:
    """The active config of every edge gateway pod: rate limit, RBAC (the source restriction)
    and Basic Auth on the HTTPS listener. Read with pilot-agent (exec, read-only)."""
    pods = [p["metadata"]["name"] for p in kubectl_items(context, "pods", "-l", f"istio={EDGE_SERVICE}")
            if p["metadata"]["namespace"] == EDGE_NAMESPACE]
    if not pods:
        return ["no edge gateway pod to read the active configuration from"]
    problems = []
    for pod in pods:
        done = subprocess.run(["kubectl", "--context", context, "-n", EDGE_NAMESPACE, "exec", pod, "--",
                               "pilot-agent", "request", "GET", "config_dump"], capture_output=True, text=True, check=False)
        chain = []
        for c in json.loads(done.stdout or "{}").get("configs", []):
            for listener in c.get("dynamic_listeners", []):
                active = listener.get("active_state", {}).get("listener", {})
                if active.get("address", {}).get("socket_address", {}).get("port_value") != 443:
                    continue
                for fc in active.get("filter_chains", []):
                    for f in fc.get("filters", []):
                        chain = [h["name"] for h in f.get("typed_config", {}).get("http_filters", [])] or chain
        for needed in ("envoy.filters.http.local_ratelimit", "envoy.filters.http.rbac", "envoy.filters.http.basic_auth"):
            if needed not in chain:
                problems.append(f"gateway pod {pod}: {needed} is not active on the HTTPS listener")
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
    for sub_parser in (render, live):
        sub_parser.add_argument("--allow-public-loadbalancer", action="store_true",
                                help=f"owner-approved public mode: {EDGE_LB} may accept any source, if the edge "
                                     "security controls are proven present (and, live, active in Envoy)")
    live.add_argument("--allow-gateway", action="append", default=[], metavar="NAMESPACE/NAME",
                      help="an owner-approved ingress Gateway and the workload Istio made for it (repeatable)")
    args = parser.parse_args(argv)
    if args.mode == "render":
        documents = []
        for path in args.manifests:
            text = sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8")
            documents += list(yaml.safe_load_all(text))
        allowed = frozenset(args.allow_loadbalancer) | ({EDGE_LB} if args.allow_public_loadbalancer else frozenset())
        problems = render_problems(documents, allowed, args.allow_public_loadbalancer)
    else:
        public_objects = None
        allowed = frozenset(args.allow_loadbalancer)
        if args.allow_public_loadbalancer:
            allowed |= {EDGE_LB}
            public_objects = [o for kind in ("envoyfilters.networking.istio.io", "authorizationpolicies.security.istio.io",
                                             "httproutes.gateway.networking.k8s.io")
                              for o in kubectl_items(args.context, kind) if o["metadata"]["namespace"] == EDGE_NAMESPACE]
        problems = live_problems(kubectl_items(args.context, "gateways.gateway.networking.k8s.io"),
                                 kubectl_items(args.context, "deployments"), kubectl_items(args.context, "services"),
                                 allowed, frozenset(args.allow_gateway), public_objects)
        if args.allow_public_loadbalancer:
            problems += envoy_problems(args.context)
    for problem in problems:
        print(f"UNSAFE {problem}")
    if problems:
        print("Nothing was changed. Review each finding; approve existing resources with --allow-* instead of deleting them.")
    print("EDGE_OWNERSHIP_UNSAFE" if problems else "EDGE_OWNERSHIP_OK")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
