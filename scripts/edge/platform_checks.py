"""Read-only GKE checks around an edge campaign (G3-A1 / G3-B / G4), replayable from the repo.

Subcommands (all read-only: kubectl get, and exec of read-only GETs / pilot-agent dumps):
  backends   readiness, restarts and endpoints of the four backends (+ kagent-controller) and
             their callers (Jenkins -> Registry, Grafana -> Prometheus/Loki/Tempo); --out writes
             the snapshot used as a baseline
  compare    the same, judged against a baseline snapshot (no new restarts, still ready)
  watch      a Deployment's pod stays Ready with unchanged restarts for --seconds (the kagent-ui
             probe check after the edge NetworkPolicy lands)
  gateway    GatewayClass/Gateway/HTTPRoute/ReferenceGrant conditions
  envoy      the edge gateway's active config: filter order on 443, nothing on 80, route names,
             per-route buckets, no retries, TLS secret; --wait captures the FIRST config that has
             a 443 listener (staged 0 -> 1 rollout)
  sleep-feasibility  the PDBs a wake-full would make blockers, against the approved guarded record

Every check prints one JSON line {"check", "passed", "observed"}; the exit code is 1 if any failed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable

import yaml

ROOT = Path(__file__).resolve().parents[2]
NS = "ewallet-ingress"
GATEWAY = "ewallet-edge-gateway"
BACKENDS = [("ml-platform", "feature-api"), ("kagent", "kagent-ui"), ("kagent", "kagent-controller"),
            ("ewallet-prompts", "prompt-registry-agentregistry"), ("ewallet-agents", "grafana")]
ROUTES = ["feature-api", "kagent-ui", "agent-registry", "grafana"]
SECURITY = ("envoy.filters.http.local_ratelimit", "envoy.filters.http.basic_auth")


class Kube:
    """kubectl pinned to one kubeconfig and context."""

    def __init__(self, context: str, kubeconfig: str | None = None, runner: Callable[[list[str]], str] | None = None):
        self.base = ["kubectl", *(["--kubeconfig", kubeconfig] if kubeconfig else []), "--context", context]
        self.runner = runner or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, check=False).stdout)

    def json(self, *args: str) -> dict:
        return json.loads(self.runner([*self.base, *args, "-o", "json"]) or "{}")

    def exec(self, namespace: str, target: str, *command: str, container: str | None = None) -> str:
        return self.runner([*self.base, "-n", namespace, "exec", target, *(["-c", container] if container else []), "--", *command])


def result(check: str, passed: bool, observed: object) -> dict:
    return {"check": check, "passed": bool(passed), "observed": observed}


# --- backends -----------------------------------------------------------------------------------

def snapshot(kube: Kube) -> dict:
    """Readiness, restarts and ready endpoints per backend, plus the callers' answers."""
    out: dict = {"backends": {}, "callers": {}}
    for ns, name in BACKENDS:
        deploy = kube.json("-n", ns, "get", "deploy", name)
        selector = deploy.get("spec", {}).get("selector", {}).get("matchLabels", {})
        pods = kube.json("-n", ns, "get", "pods", "-l", ",".join(f"{k}={v}" for k, v in selector.items())).get("items", [])
        slices = kube.json("-n", ns, "get", "endpointslices", "-l", f"kubernetes.io/service-name={name}").get("items", [])
        out["backends"][f"{ns}/{name}"] = {
            "ready_replicas": deploy.get("status", {}).get("readyReplicas", 0) or 0,
            "restarts": sum(c.get("restartCount", 0) for p in pods for c in p.get("status", {}).get("containerStatuses", [])),
            "ready_endpoints": sum(1 for s in slices for e in s.get("endpoints") or [] if e.get("conditions", {}).get("ready")),
        }
    jenkins = next(iter(kube.json("-n", "ewallet-ci", "get", "pods", "-l", "app.kubernetes.io/name=jenkins").get("items", [])), None)
    if jenkins:
        line = kube.exec("ewallet-ci", jenkins["metadata"]["name"], "bash", "-c",
                         'exec 3<>/dev/tcp/prompt-registry-agentregistry.ewallet-prompts.svc/12121 && '
                         'printf "GET / HTTP/1.0\\r\\nHost: x\\r\\n\\r\\n" >&3 && head -1 <&3', container="jenkins")
        out["callers"]["jenkins->registry"] = line.strip()
    grafana = next(iter(kube.json("-n", "ewallet-agents", "get", "pods", "-l", "app.kubernetes.io/name=grafana").get("items", [])), None)
    for target in ("prometheus:9090/-/ready", "loki:3100/ready", "tempo:3200/ready"):
        answer = kube.exec("ewallet-agents", grafana["metadata"]["name"], "wget", "-q", "-O", "-", "-T", "10", f"http://{target}") if grafana else ""
        out["callers"][f"grafana->{target.split(':')[0]}"] = answer.strip().splitlines()[0] if answer.strip() else ""
    return out


def judge(current: dict, baseline: dict | None = None) -> list[dict]:
    """Every backend ready with a ready endpoint (where it has a Service), callers answering, and
    against a baseline: no backend restarted since."""
    results = []
    for key, now in current["backends"].items():
        before = (baseline or {}).get("backends", {}).get(key)
        ok = now["ready_replicas"] >= 1 and (now["ready_endpoints"] >= 1 or key.endswith("kagent-controller"))
        if before is not None:
            ok = ok and now["restarts"] <= before["restarts"]
        results.append(result(f"backend {key}", ok, {**now, **({"baseline_restarts": before["restarts"]} if before else {})}))
    callers = current["callers"]
    results.append(result("jenkins -> registry", callers.get("jenkins->registry", "").startswith("HTTP/1.0 200"),
                          callers.get("jenkins->registry")))
    for name in ("prometheus", "loki", "tempo"):
        answer = callers.get(f"grafana->{name}", "")
        results.append(result(f"grafana -> {name}", "ready" in answer.lower(), answer))
    return results


def watch(kube: Kube, namespace: str, deployment: str, seconds: int, interval: int, sleep=time.sleep,
          now: Callable[[], str] = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())) -> list[dict]:
    """The Deployment's pods stay Ready, restarts do not change, and no Unhealthy/BackOff/Killing
    event for it appears after the start, for ``seconds``."""
    started = now()
    selector = kube.json("-n", namespace, "get", "deploy", deployment)["spec"]["selector"]["matchLabels"]
    label = ",".join(f"{k}={v}" for k, v in selector.items())
    samples = []
    for _ in range(max(1, seconds // interval)):
        pods = kube.json("-n", namespace, "get", "pods", "-l", label).get("items", [])
        samples.append([(p["metadata"]["name"], all(c.get("ready") for c in p["status"].get("containerStatuses", [])),
                         sum(c.get("restartCount", 0) for c in p["status"].get("containerStatuses", []))) for p in pods])
        sleep(interval)
    events = [e for e in kube.json("-n", namespace, "get", "events").get("items", [])
              if e.get("reason") in ("Unhealthy", "BackOff", "Killing")
              and e.get("involvedObject", {}).get("name", "").startswith(deployment)
              and (e.get("lastTimestamp") or e.get("eventTime") or "") >= started]
    restarts = lambda sample: [r for *_, r in sample]
    steady = all(sample and all(ready for _, ready, _ in sample) and restarts(sample) == restarts(samples[0]) for sample in samples)
    return [result(f"{namespace}/{deployment} stays Ready for {seconds}s", steady and not events,
                   {"samples": len(samples), "first": samples[0], "last": samples[-1], "probe_events": len(events)})]


# --- gateway and envoy --------------------------------------------------------------------------

def conditions(obj: dict) -> dict:
    return {c["type"]: c["status"] for c in obj.get("status", {}).get("conditions", [])}


def gateway_checks(gatewayclass: dict, gateway: dict, routes: list[dict], grants: list[dict], expect_programmed: bool) -> list[dict]:
    listeners = {l["name"]: {c["type"]: c["status"] for c in l.get("conditions", [])} for l in gateway.get("status", {}).get("listeners", [])}
    route_state = {r["metadata"]["name"]: {c["type"]: c["status"] for p in r.get("status", {}).get("parents", []) for c in p["conditions"]}
                   for r in routes}
    return [
        result("GatewayClass istio Accepted", conditions(gatewayclass).get("Accepted") == "True", conditions(gatewayclass)),
        result("Gateway Accepted", conditions(gateway).get("Accepted") == "True", conditions(gateway)),
        result(f"Gateway Programmed is {expect_programmed}", (conditions(gateway).get("Programmed") == "True") == expect_programmed,
               conditions(gateway)),
        result("listeners https and http accepted with resolved refs", {"https", "http"} <= set(listeners) and all(
            v.get("Accepted") == "True" and v.get("ResolvedRefs") == "True" for v in listeners.values()), listeners),
        result("every HTTPRoute Accepted with ResolvedRefs", bool(route_state) and all(
            v.get("Accepted") == "True" and v.get("ResolvedRefs") == "True" for v in route_state.values()), route_state),
        result("each ReferenceGrant: HTTPRoute@ewallet-ingress -> one Service", bool(grants) and all(
            [f.get("namespace") for f in g["spec"]["from"]] == [NS] and len(g["spec"]["to"]) == 1 and g["spec"]["to"][0].get("kind") == "Service"
            for g in grants), [f"{g['metadata']['namespace']}/{g['metadata']['name']}" for g in grants]),
    ]


def envoy_checks(dump: dict, limits: dict[str, int], restricted: bool = False) -> list[dict]:
    """The edge gateway's active config, as G3-A1 verified it."""
    chains, routes, secrets = {}, {}, []
    for c in dump.get("configs", []):
        for l in c.get("dynamic_listeners", []):
            active = l.get("active_state", {}).get("listener", {})
            port = active.get("address", {}).get("socket_address", {}).get("port_value")
            for fc in active.get("filter_chains", []):
                for f in fc.get("filters", []):
                    names = [h["name"] for h in f.get("typed_config", {}).get("http_filters", [])]
                    chains[port] = names or chains.get(port, [])
        for r in c.get("dynamic_route_configs", []):
            for vh in r.get("route_config", {}).get("virtual_hosts", []):
                for x in vh.get("routes", []):
                    routes[x.get("name")] = x
        secrets += [s.get("name") for s in c.get("dynamic_active_secrets", [])]
    https, http = chains.get(443, []), chains.get(80, [])
    rl, ba, router = (*SECURITY, "envoy.filters.http.router")
    buckets = {n: routes[n].get("typed_per_filter_config", {}).get(rl, {}).get("token_bucket", {}).get("max_tokens")
               for n in ROUTES if n in routes}
    out = [
        result("443: rate limit, then Basic Auth, then router", rl in https and ba in https and router in https
               and https.index(rl) < https.index(ba) < https.index(router), https),
        result("80: no security filter", bool(http) and rl not in http and ba not in http, http),
        result("routes named after the HTTPRoute rules", set(ROUTES) <= set(routes), sorted(n for n in routes if n)),
        result("per-route token buckets equal values.yaml", buckets == {n: limits[n] for n in buckets} and len(buckets) == len(ROUTES), buckets),
        result("no route retries", all("retry_policy" not in routes[n].get("route", {}) for n in ROUTES if n in routes),
               [n for n in ROUTES if "retry_policy" in routes.get(n, {}).get("route", {})]),
        result("TLS secret edge-tls loaded", f"kubernetes-gateway://{NS}/edge-tls" in secrets, secrets),
    ]
    if restricted:
        out.append(result("443: RBAC (source restriction) active", "envoy.filters.http.rbac" in https, https))
    return out


def gateway_dump(kube: Kube, wait: int = 0, sleep=time.sleep) -> tuple[str, dict]:
    """The first config of the gateway pod that has a 443 listener (``wait`` seconds at most)."""
    for _ in range(max(1, wait)):
        pods = kube.json("-n", NS, "get", "pods", "-l", f"istio={GATEWAY}").get("items", [])
        if pods:
            name = pods[0]["metadata"]["name"]
            text = kube.exec(NS, name, "pilot-agent", "request", "GET", "config_dump")
            if '"port_value": 443' in text:
                return name, json.loads(text)
        sleep(1)
    return "", {}


def route_limits() -> dict[str, int]:
    values = yaml.safe_load((ROOT / "infra/helm/ewallet-edge/values.yaml").read_text())
    return {n: r["rateLimit"]["maxTokens"] for n, r in values["routes"].items()}


# --- sleep feasibility --------------------------------------------------------------------------

def predict_blockers(pdbs: list[dict], deployments: list[dict], recorded: dict[str, int], approved: set[str]) -> dict:
    """PDBs that block a drain to zero once wake-full restores the recorded replicas."""
    sys.path.insert(0, str(ROOT / "ops/gcp"))
    import cloud_lab  # noqa: E402 (the accepted Cloud Lab gate logic, reused as is)
    blockers = []
    for pdb in pdbs:
        ns = pdb["metadata"]["namespace"]
        labels = pdb["spec"].get("selector", {}).get("matchLabels") or {}
        targets = [f"{ns}/{d['metadata']['name']}" for d in deployments if d["metadata"]["namespace"] == ns
                   and labels.items() <= (d["spec"]["template"]["metadata"].get("labels") or {}).items()]
        replicas = sum(recorded.get(t, 0) for t in targets)
        if ns not in cloud_lab.SYSTEM_NAMESPACES and cloud_lab.evaluate_pdbs([{**pdb, "status": {"expectedPods": replicas}}]):
            blockers.append(f"{ns}/{pdb['metadata']['name']}")
    return {"predicted_blockers": sorted(blockers), "not_covered": sorted(set(blockers) - approved)}


# --- CLI ----------------------------------------------------------------------------------------

def emit(results: list[dict], out: Path | None) -> int:
    for r in results:
        print(json.dumps(r, default=str))
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(r, default=str) + "\n" for r in results), encoding="utf-8")
    return 0 if all(r["passed"] for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--context", required=True)
    parser.add_argument("--kubeconfig")
    sub = parser.add_subparsers(dest="command", required=True)
    backends = sub.add_parser("backends")
    backends.add_argument("--snapshot-out", type=Path, help="write the snapshot (baseline for compare)")
    compare = sub.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    w = sub.add_parser("watch")
    w.add_argument("--namespace", default="kagent")
    w.add_argument("--deployment", default="kagent-ui")
    w.add_argument("--seconds", type=int, default=180)
    w.add_argument("--interval", type=int, default=15)
    g = sub.add_parser("gateway")
    g.add_argument("--expect-programmed", choices=["true", "false"], default="true")
    e = sub.add_parser("envoy")
    e.add_argument("--wait", type=int, default=0, help="seconds to wait for the first 443 listener")
    e.add_argument("--restricted", action="store_true", help="also require the RBAC filter (source restriction)")
    s = sub.add_parser("sleep-feasibility")
    s.add_argument("--manifest", type=Path, required=True, help="recovery manifest directory (workloads.json)")
    s.add_argument("--approved-record", type=Path, default=ROOT / "docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z")
    s.add_argument("--also-approve", action="append", default=[], metavar="NS/NAME", help="owner-approved extra PDBs")
    for p in (backends, compare, w, g, e, s):
        p.add_argument("--out", type=Path, help="also write the results as JSON lines")
    args = parser.parse_args(argv)
    kube = Kube(args.context, args.kubeconfig)
    if args.command == "backends":
        snap = snapshot(kube)
        if args.snapshot_out:
            args.snapshot_out.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")
        return emit(judge(snap), args.out)
    if args.command == "compare":
        return emit(judge(snapshot(kube), json.loads(args.baseline.read_text())), args.out)
    if args.command == "watch":
        return emit(watch(kube, args.namespace, args.deployment, args.seconds, args.interval), args.out)
    if args.command == "gateway":
        gw = kube.json("-n", NS, "get", "gateways.gateway.networking.k8s.io", "ewallet-edge")
        results = gateway_checks(kube.json("get", "gatewayclass", "istio"), gw,
                                 kube.json("-n", NS, "get", "httproutes.gateway.networking.k8s.io").get("items", []),
                                 kube.json("get", "referencegrants.gateway.networking.k8s.io", "-A").get("items", []),
                                 args.expect_programmed == "true")
        return emit(results, args.out)
    if args.command == "envoy":
        pod, dump = gateway_dump(kube, args.wait)
        results = [result("a gateway pod with a 443 listener", bool(pod), pod or "none")]
        return emit(results + (envoy_checks(dump, route_limits(), args.restricted) if pod else []), args.out)
    sys.path.insert(0, str(ROOT / "ops/gcp"))
    import cloud_lab  # noqa: E402
    recorded = {f"{w['namespace']}/{w['name']}": int(w.get("replicas") or 0)
                for w in json.loads((args.manifest / "workloads.json").read_text()) if w["kind"] == "Deployment"}
    approved = cloud_lab.approved_guarded_pdbs(args.approved_record) | set(args.also_approve)
    prediction = predict_blockers(kube.json("get", "pdb", "-A").get("items", []), kube.json("get", "deploy", "-A").get("items", []),
                                  recorded, approved)
    return emit([result("every predicted PDB blocker is covered by an approval", not prediction["not_covered"], prediction)], args.out)


if __name__ == "__main__":
    raise SystemExit(main())
