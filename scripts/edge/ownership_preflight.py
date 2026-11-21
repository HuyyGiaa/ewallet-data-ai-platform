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
import ipaddress
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
HTTPS_PORT = 443
REPO_ROOT = Path(__file__).resolve().parents[2]
# Ranges a SNAT'd node or pod address (or a non-public client) could come from. An allowlist
# entry overlapping one of them would let traffic through whenever the client address is not
# preserved, so it is refused. Same list as templates/source-restriction.yaml.
NON_PUBLIC = [ipaddress.ip_network(n) for n in ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
                                                "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/3")]
MIN_PREFIX = 16


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


def cidr_problems(values: Iterable[str]) -> tuple[set[ipaddress.IPv4Network], list[str]]:
    """Parse CIDRs as real IPv4 networks. Refused: malformed, IPv6, host bits set (the meaning
    would change when normalised), wider than /16, or overlapping a non-public range."""
    networks, problems = set(), []
    for value in values:
        text = str(value).strip()
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            problems.append(f"{text!r} is not a valid CIDR")
            continue
        if network.version != 4 or "/" not in text:
            problems.append(f"{text!r} must be an IPv4 network with a prefix")
        elif str(network) != text:
            problems.append(f"{text!r} has host bits set (it would mean {network})")
        elif network.prefixlen < MIN_PREFIX:
            problems.append(f"{text!r} is wider than /{MIN_PREFIX}")
        elif any(network.overlaps(n) for n in NON_PUBLIC):
            problems.append(f"{text!r} overlaps a private, shared or reserved range")
        else:
            networks.add(network)
    return networks, problems


def selects(selector: dict | None, labels: dict) -> bool:
    """A non-empty label selector that the gateway pods' labels satisfy."""
    return bool(selector) and selector.items() <= labels.items()


def public_security_problems(objects: Iterable[dict], gateway_labels: dict | None, domain: str | None,
                             approved: set[ipaddress.IPv4Network] | None) -> list[str]:
    """What an Internet-open edge LoadBalancer depends on, read from the edge objects (a render
    or the live cluster) and checked against the owner's inputs (domain, approved CIDRs):

    - EnvoyFilter edge-security selects the gateway pods and puts Basic Auth and the rate
      limit on the HTTPS listener;
    - no application route is attached to the plain-HTTP listener (only the redirect and the
      ACME responder live there);
    - if the agents route exists: AuthorizationPolicy edge-source-restriction selects the
      gateway pods, is one DENY rule whose only condition is notIpBlocks = the approved CIDRs,
      for exactly agents.<domain> (with and without a port) on port 443, with nothing that
      narrows it;
    - the ACME route sends only the challenge prefix to the responder.
    """
    objects = [o for o in objects if o]
    by = lambda kind, name: next((o for o in objects if o["kind"] == kind and o["metadata"]["name"] == name
                                  and o["metadata"].get("namespace", EDGE_NAMESPACE) == EDGE_NAMESPACE), None)
    problems = []
    if not domain:
        problems.append("public mode needs --domain (the owner-approved domain)")
    if not gateway_labels:
        problems.append(f"Deployment {EDGE_NAMESPACE}/{EDGE_SERVICE} not found: cannot prove what the policies select")
        gateway_labels = {}
    envoy = by("EnvoyFilter", "edge-security")
    if not envoy or not selects(envoy["spec"].get("workloadSelector", {}).get("labels"), gateway_labels):
        problems.append("EnvoyFilter edge-security is missing or does not select the gateway pods")
    for needed in ("envoy.filters.http.basic_auth", "envoy.filters.http.local_ratelimit"):
        on_https = [p for p in (envoy or {}).get("spec", {}).get("configPatches", [])
                    if p.get("patch", {}).get("value", {}).get("name") == needed and p.get("applyTo") == "HTTP_FILTER"
                    and p.get("match", {}).get("context") == "GATEWAY"
                    and p.get("match", {}).get("listener", {}).get("portNumber") == HTTPS_PORT]
        if not on_https:
            problems.append(f"EnvoyFilter edge-security does not put {needed} on the HTTPS listener ({HTTPS_PORT})")
    for route in (o for o in objects if o["kind"] == "HTTPRoute"):
        name = route["metadata"]["name"]
        listeners = {ref.get("sectionName") for ref in route["spec"].get("parentRefs", [])}
        backends = [b["name"] for rule in route["spec"].get("rules", []) for b in rule.get("backendRefs", [])]
        if name not in ("https-redirect", "acme-http01") and listeners != {"https"}:
            problems.append(f"HTTPRoute {name} is not limited to the HTTPS listener (Basic Auth runs only there)")
        if name == "https-redirect" and backends:
            problems.append("HTTPRoute https-redirect must not have a backend")
    agents = by("HTTPRoute", "kagent-ui")
    if agents:
        problems += [f"agents restriction: {p}" for p in
                     restriction_problems(by("AuthorizationPolicy", "edge-source-restriction"), agents, gateway_labels,
                                          domain, approved)]
    acme = by("HTTPRoute", "acme-http01")
    if acme:
        refs = [b["name"] for rule in acme["spec"]["rules"] for b in rule.get("backendRefs", [])]
        paths = [m["path"]["value"] for rule in acme["spec"]["rules"] for m in rule.get("matches", [])]
        listeners = [ref.get("sectionName") for ref in acme["spec"].get("parentRefs", [])]
        if refs != ["edge-acme-responder"] or paths != ["/.well-known/acme-challenge/"] or listeners != ["http"]:
            problems.append("HTTPRoute acme-http01 must send only /.well-known/acme-challenge/ on the HTTP listener to edge-acme-responder")
    return problems


def restriction_problems(policy: dict | None, agents: dict, gateway_labels: dict, domain: str | None,
                         approved: set[ipaddress.IPv4Network] | None) -> list[str]:
    """Why the agents source restriction does not hold exactly as approved (empty: it does)."""
    if not policy:
        return ["AuthorizationPolicy edge-source-restriction is missing (disable the agents route instead)"]
    problems = []
    spec = policy.get("spec", {})
    if spec.get("action") != "DENY":
        problems.append("action must be DENY")
    if spec.get("targetRef") or spec.get("targetRefs") or not selects(spec.get("selector", {}).get("matchLabels"), gateway_labels):
        problems.append("it does not select the gateway pods")
    expected_hosts = {f"agents.{domain}", f"agents.{domain}:*"} if domain else set()
    if domain and agents["spec"].get("hostnames") != [f"agents.{domain}"]:
        problems.append(f"the agents route serves {agents['spec'].get('hostnames')}, not agents.{domain}")
    rules = spec.get("rules") or []
    if len(rules) != 1:
        return problems + [f"expected exactly one rule, found {len(rules)}"]
    rule = rules[0]
    if set(rule) != {"from", "to"}:
        problems.append(f"the rule may hold only from/to, found {sorted(rule)} (anything else narrows the DENY)")
    sources = rule.get("from") or []
    if len(sources) != 1 or set(sources[0].get("source", {})) != {"notIpBlocks"}:
        problems.append("the only source condition must be notIpBlocks (the TCP peer address)")
    operations = rule.get("to") or []
    if len(operations) != 1 or set(operations[0].get("operation", {})) != {"hosts", "ports"}:
        problems.append("the only operation conditions must be hosts and ports")
    operation = (operations[0] if operations else {}).get("operation", {})
    if expected_hosts and not expected_hosts <= set(operation.get("hosts", [])):
        problems.append(f"hosts {operation.get('hosts')} do not cover {sorted(expected_hosts)}")
    if operation.get("ports") != [str(HTTPS_PORT)]:
        problems.append(f"ports must be exactly [\"{HTTPS_PORT}\"] (the HTTPS listener), found {operation.get('ports')}")
    blocks = (sources[0].get("source", {}) if sources else {}).get("notIpBlocks") or []
    networks, cidr_errors = cidr_problems(blocks)
    problems += [f"notIpBlocks: {e}" for e in cidr_errors]
    if not blocks:
        problems.append("notIpBlocks is empty")
    if not approved:
        problems.append("no approved CIDRs were given (--approved-cidr / --approved-cidrs-file)")
    elif networks != approved and not cidr_errors:
        problems.append(f"notIpBlocks {sorted(map(str, networks))} differ from the approved {sorted(map(str, approved))}")
    return problems


def rbac_problems(config_dump: dict, approved: set[ipaddress.IPv4Network] | None, agents: bool) -> list[str]:
    """Runtime side of the restriction: the HTTPS listener's RBAC filter must hold a DENY policy
    from edge-source-restriction whose remote-address prefixes equal the approved CIDRs. This is
    configuration evidence only; source-IP behaviour still needs black-box tests."""
    def walk(node, found):
        if isinstance(node, dict):
            if "direct_remote_ip" in node:
                ip = node["direct_remote_ip"]
                found.add(ipaddress.ip_network(f"{ip['address_prefix']}/{ip.get('prefix_len', 32)}", strict=False))
            for value in node.values():
                walk(value, found)
        elif isinstance(node, list):
            for value in node:
                walk(value, found)
        return found
    problems, chain, rbac = [], [], []
    for c in config_dump.get("configs", []):
        for listener in c.get("dynamic_listeners", []):
            active = listener.get("active_state", {}).get("listener", {})
            if active.get("address", {}).get("socket_address", {}).get("port_value") != HTTPS_PORT:
                continue
            for fc in active.get("filter_chains", []):
                for f in fc.get("filters", []):
                    filters = f.get("typed_config", {}).get("http_filters", [])
                    chain = [h["name"] for h in filters] or chain
                    rbac += [h.get("typed_config", {}) for h in filters if h["name"] == "envoy.filters.http.rbac"]
    for needed in ("envoy.filters.http.local_ratelimit", "envoy.filters.http.rbac", "envoy.filters.http.basic_auth"):
        if needed not in chain:
            problems.append(f"{needed} is not active on the HTTPS listener")
    if agents:
        policies = {name: pol for r in rbac if r.get("rules", {}).get("action") == "DENY"
                    for name, pol in r["rules"].get("policies", {}).items() if "edge-source-restriction" in name}
        if not policies:
            problems.append("no active DENY RBAC policy from edge-source-restriction")
        elif approved is not None and walk(policies, set()) != approved:
            problems.append(f"active RBAC addresses {sorted(map(str, walk(policies, set())))} differ from the approved "
                            f"{sorted(map(str, approved))}")
    return problems


def render_problems(documents: Iterable[dict], allowed_lb: frozenset[str] = frozenset(),
                    public: bool = False, domain: str | None = None,
                    approved: set[ipaddress.IPv4Network] | None = None) -> list[str]:
    """Problems in rendered manifests. ``public`` allows the edge LoadBalancer to accept any
    source only if the same render proves the controls it then relies on."""
    documents = [doc for doc in documents if doc]
    problems = gateway_problems(d for d in documents if d["apiVersion"].startswith(GATEWAY_API) and d["kind"] == "Gateway")
    for d in documents:
        if d["kind"] == "Service":
            d = {**d, "metadata": {"namespace": EDGE_NAMESPACE, **d["metadata"]}} if d["metadata"]["name"] == EDGE_SERVICE else d
            problems += balancer_problems(d, allowed_lb, public)
    if public:
        problems += public_security_problems(documents, gateway_pod_labels(documents), domain, approved)
    return problems


def gateway_pod_labels(objects: Iterable[dict]) -> dict | None:
    """Pod labels of the edge gateway Deployment (the istio/gateway release), if present."""
    for o in objects:
        if o and o["kind"] == "Deployment" and o["metadata"]["name"] == EDGE_SERVICE \
                and o["metadata"].get("namespace", EDGE_NAMESPACE) == EDGE_NAMESPACE:
            return o["spec"]["template"]["metadata"].get("labels", {})
    return None


def live_problems(gateways: list[dict], deployments: list[dict], services: list[dict],
                  allowed_lb: frozenset[str] = frozenset(), allowed_gateways: frozenset[str] = frozenset(),
                  public_objects: list[dict] | None = None, domain: str | None = None,
                  approved: set[ipaddress.IPv4Network] | None = None) -> list[str]:
    """Problems in a cluster's current state; allowed_* list owner-approved namespace/name."""
    problems = gateway_problems(gateways, allowed_gateways)
    labels = lambda d: d["metadata"].get("labels", {})
    managed = [d for d in deployments if labels(d).get("gateway.istio.io/managed") == "istio.io-gateway-controller"
               and f"{d['metadata']['namespace']}/{labels(d).get('gateway.networking.k8s.io/gateway-name')}" not in allowed_gateways]
    problems += [f"Istio-provisioned gateway Deployment {d['metadata']['namespace']}/{d['metadata']['name']}" for d in managed]
    for service in services:
        problems += balancer_problems(service, allowed_lb, public_objects is not None)
    if public_objects is not None:
        problems += public_security_problems(public_objects, gateway_pod_labels(deployments), domain, approved)
    edge = [s for s in services if (s["metadata"]["namespace"], s["metadata"]["name"]) == (EDGE_NAMESPACE, EDGE_SERVICE)]
    if edge and edge[0]["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != "Helm":
        problems.append(f"Service {EDGE_NAMESPACE}/{EDGE_SERVICE} is not managed by Helm")
    return problems


def envoy_problems(context: str, approved: set[ipaddress.IPv4Network] | None, agents: bool,
                   kubeconfig: str | None = None) -> list[str]:
    """The active config of every edge gateway pod (pilot-agent config_dump, exec, read-only)."""
    pods = [p["metadata"]["name"] for p in kubectl_items(context, "pods", "-l", f"istio={EDGE_SERVICE}", kubeconfig=kubeconfig)
            if p["metadata"]["namespace"] == EDGE_NAMESPACE]
    if not pods:
        return ["no edge gateway pod to read the active configuration from"]
    problems = []
    for pod in pods:
        done = subprocess.run(["kubectl", *(["--kubeconfig", kubeconfig] if kubeconfig else []), "--context", context,
                               "-n", EDGE_NAMESPACE, "exec", pod, "--", "pilot-agent", "request", "GET", "config_dump"],
                              capture_output=True, text=True, check=False)
        problems += [f"gateway pod {pod}: {p}" for p in rbac_problems(json.loads(done.stdout or "{}"), approved, agents)]
    return problems


def kubectl_items(context: str, *resource: str, kubeconfig: str | None = None) -> list[dict]:
    """List a resource in every namespace; a missing API is an empty list."""
    done = subprocess.run(["kubectl", *(["--kubeconfig", kubeconfig] if kubeconfig else []), "--context", context,
                           "get", *resource, "-A", "-o", "json"],
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        if "the server doesn't have a resource type" in done.stderr:
            return []
        raise SystemExit(f"kubectl get {' '.join(resource)} failed: {done.stderr.strip()}")
    return json.loads(done.stdout)["items"]


def approved_cidrs(args: argparse.Namespace) -> tuple[set[ipaddress.IPv4Network] | None, list[str]]:
    """The owner-approved client CIDRs, from --approved-cidr and/or a file kept OUTSIDE the
    repository (one CIDR per line, '#' comments), so a real address is never committed."""
    values, problems = list(args.approved_cidr), []
    if args.approved_cidrs_file:
        path = Path(args.approved_cidrs_file).resolve()
        if path == REPO_ROOT or REPO_ROOT in path.parents:
            return None, [f"--approved-cidrs-file {path} is inside the repository; keep owner addresses outside it"]
        values += [line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines()]
        values = [v for v in values if v]
    if not values:
        return None, []
    networks, errors = cidr_problems(values)
    return (networks if not errors else None), [f"approved CIDR {e}" for e in errors]


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
    live.add_argument("--kubeconfig", help="kubeconfig file (default: kubectl's)")
    live.add_argument("--allow-loadbalancer", action="append", default=[], metavar="NAMESPACE/NAME",
                      help="an owner-approved LoadBalancer Service (repeatable)")
    for sub_parser in (render, live):
        sub_parser.add_argument("--allow-public-loadbalancer", action="store_true",
                                help=f"owner-approved public mode: {EDGE_LB} may accept any source, if the edge "
                                     "security controls are proven present (and, live, active in Envoy)")
        sub_parser.add_argument("--domain", help="public mode: the owner-approved domain (agents.<domain> etc.)")
        sub_parser.add_argument("--approved-cidr", action="append", default=[], metavar="CIDR",
                                help="public mode: an owner-approved client CIDR for the agents host (repeatable)")
        sub_parser.add_argument("--approved-cidrs-file", metavar="PATH",
                                help="public mode: file of approved CIDRs, outside the repository")
    live.add_argument("--allow-gateway", action="append", default=[], metavar="NAMESPACE/NAME",
                      help="an owner-approved ingress Gateway and the workload Istio made for it (repeatable)")
    args = parser.parse_args(argv)
    approved, problems = approved_cidrs(args)
    if args.mode == "render" and not problems:
        documents = []
        for path in args.manifests:
            text = sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8")
            documents += list(yaml.safe_load_all(text))
        allowed = frozenset(args.allow_loadbalancer) | ({EDGE_LB} if args.allow_public_loadbalancer else frozenset())
        problems = render_problems(documents, allowed, args.allow_public_loadbalancer, args.domain, approved)
    elif not problems:
        kc = args.kubeconfig
        public_objects = None
        allowed = frozenset(args.allow_loadbalancer)
        if args.allow_public_loadbalancer:
            allowed |= {EDGE_LB}
            public_objects = [o for kind in ("envoyfilters.networking.istio.io", "authorizationpolicies.security.istio.io",
                                             "httproutes.gateway.networking.k8s.io")
                              for o in kubectl_items(args.context, kind, kubeconfig=kc) if o["metadata"]["namespace"] == EDGE_NAMESPACE]
        problems = live_problems(kubectl_items(args.context, "gateways.gateway.networking.k8s.io", kubeconfig=kc),
                                 kubectl_items(args.context, "deployments", kubeconfig=kc),
                                 kubectl_items(args.context, "services", kubeconfig=kc),
                                 allowed, frozenset(args.allow_gateway), public_objects, args.domain, approved)
        if args.allow_public_loadbalancer:
            agents = any(o["kind"] == "HTTPRoute" and o["metadata"]["name"] == "kagent-ui" for o in public_objects)
            problems += envoy_problems(args.context, approved, agents, kc)
    for problem in problems:
        print(f"UNSAFE {problem}")
    if problems:
        print("Nothing was changed. Review each finding; approve existing resources with --allow-* instead of deleting them.")
    print("EDGE_OWNERSHIP_UNSAFE" if problems else "EDGE_OWNERSHIP_OK")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
