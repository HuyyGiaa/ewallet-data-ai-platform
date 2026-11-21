#!/usr/bin/env bash
# Validate the edge on the real Istio 1.30.7 gateway in kind. Refuses any other context.
# usage: local_validate.sh <evidence-dir> [foundation|full]
#   foundation (G2.1): Gateway API status, workload ownership, routing, TLS; no security filters.
#   full: also Basic Auth and rate limits (securityFiltersVerified=true), once G2.2 verifies them.
# Real: istiod, the official gateway chart, Envoy, the rendered Gateway API resources, TLS.
# Stand-in: one echo backend for every route. Not GKE evidence.
set -euo pipefail
evidence=${1:?evidence directory required} mode=${2:-foundation}
[[ "$mode" == foundation || "$mode" == full ]] || { echo "mode must be foundation or full" >&2; exit 2; }
CONTEXT=${EDGE_LOCAL_CONTEXT:-kind-llm-agent-local}
[[ "$CONTEXT" == kind-* ]] || { echo "refusing non-kind context" >&2; exit 2; }
kubectl --context "$CONTEXT" get node "${CONTEXT#kind-}-control-plane" >/dev/null || { echo "not the kind node of $CONTEXT" >&2; exit 2; }
NS=ewallet-ingress STUB=edge-stub DOMAIN=edge.local.test
root=$(cd "$(dirname "$0")/../.." && pwd); chart=$root/infra/helm/ewallet-edge
k() { kubectl --context "$CONTEXT" "$@"; }
# Cleanup deletes both namespaces, so never adopt ones this run did not create.
if k get ns "$NS" >/dev/null 2>&1 || k get ns "$STUB" >/dev/null 2>&1; then
  echo "namespace $NS or $STUB already exists; refusing to reuse it" >&2; exit 2
fi
work=$(mktemp -d); mkdir -p "$evidence"
cleanup() {
  [ -z "${EDGE_KEEP:-}" ] || return 0
  helm --kube-context "$CONTEXT" uninstall ewallet-edge-gateway -n "$NS" >/dev/null 2>&1 || true
  k delete ns "$NS" "$STUB" --wait=false >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
lb_before=$(k get svc -A -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}/{.metadata.name} {end}')
k create ns "$NS" >/dev/null; k create ns "$STUB" >/dev/null
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -subj "/CN=$DOMAIN" \
  -addext "subjectAltName=DNS:*.$DOMAIN" -keyout "$work/tls.key" -out "$work/tls.crt" 2>/dev/null
k -n "$NS" create secret tls edge-tls --cert="$work/tls.crt" --key="$work/tls.key" >/dev/null
password=$(openssl rand -hex 24); printf 'edge:%s\n' "$password" > "$work/credentials"
printf '%s' "$password" | "$root/scripts/edge/create_basic_auth.sh" edge "$NS" "$CONTEXT" >/dev/null
k -n "$STUB" create configmap stub --from-file="$root/scripts/edge/stub/echo_stub.py" >/dev/null
k -n "$STUB" apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: stub}
spec:
  selector: {matchLabels: {app: stub}}
  template:
    metadata: {labels: {app: stub}}
    spec:
      containers:
        - {name: stub, image: python:3.12-alpine, command: [python3, /stub/echo_stub.py], volumeMounts: [{name: stub, mountPath: /stub}]}
      volumes: [{name: stub, configMap: {name: stub}}]
---
apiVersion: v1
kind: Service
metadata: {name: stub}
spec:
  selector: {app: stub}
  ports: [{name: http, port: 8080}]
YAML
# NodePort on the kind node only: a port-forward drops under bursts and would fake failures.
helm --kube-context "$CONTEXT" install ewallet-edge-gateway istio/gateway --version 1.30.7 -n "$NS" \
  -f "$chart/values-gateway.yaml" --set service.type=NodePort --wait --timeout 5m >/dev/null
python3 - "$chart/values.yaml" "$STUB" "$mode" > "$work/local.yaml" <<'PY'
import sys, yaml
routes = yaml.safe_load(open(sys.argv[1]))["routes"]
out = {name: {"enabled": True, "backend": {"service": "stub", "namespace": sys.argv[2], "port": 8080}, "access": None}
       for name in routes}
print(yaml.safe_dump({"enabled": True, "domain": "edge.local.test", "securityFiltersVerified": sys.argv[3] == "full",
                      "routes": out}))
PY
helm template edge "$chart" -n "$NS" -f "$work/local.yaml" > "$evidence/rendered.yaml"
python3 "$root/scripts/edge/ownership_preflight.py" render "$evidence/rendered.yaml" > "$evidence/preflight-render.txt"
k apply -f "$evidence/rendered.yaml" >/dev/null
k -n "$STUB" rollout status deploy/stub --timeout=120s >/dev/null
k -n "$NS" wait gateway/ewallet-edge --for=condition=Programmed --timeout=120s >/dev/null
sleep 8

# Gateway API status and workload ownership, read back from the API server.
{
  k get gatewayclass istio -o json
  k -n "$NS" get gateway,httproute,deploy,svc -o json
  k -n "$STUB" get referencegrant -o json
} > "$work/state.json"
lb_after=$(k get svc -A -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}/{.metadata.name} {end}')
python3 - "$work/state.json" "$lb_before" "$lb_after" > "$evidence/status.jsonl" <<'PY'
import json, sys
text, docs, at = open(sys.argv[1]).read(), [], 0
decoder = json.JSONDecoder()
while at < len(text):
    if text[at].isspace():
        at += 1; continue
    doc, at = decoder.raw_decode(text, at); docs.append(doc)
items = [docs[0]] + [i for d in docs[1:] for i in d["items"]]
by = lambda kind: [i for i in items if i["kind"] == kind]
cond = lambda conds, t: next((c["status"] for c in conds if c["type"] == t), "missing")
out = []
def check(name, ok, observed):
    out.append({"name": name, "passed": bool(ok), "observed": str(observed)[:200]})
gc = by("GatewayClass")[0]
check("T01 GatewayClass istio accepted by the Istio controller",
      gc["spec"]["controllerName"] == "istio.io/gateway-controller" and cond(gc["status"]["conditions"], "Accepted") == "True",
      gc["spec"]["controllerName"])
gw = by("Gateway")[0]
check("T02 Gateway accepted", cond(gw["status"]["conditions"], "Accepted") == "True", gw["status"]["conditions"][0]["message"])
listeners = {l["name"]: {c["type"]: c["status"] for c in l["conditions"]} for l in gw["status"].get("listeners", [])}
programmed = cond(gw["status"]["conditions"], "Programmed")
check("T03 Gateway programmed, both listeners programmed with resolved refs",
      programmed == "True" and set(listeners) == {"https", "http"}
      and all(v.get("Programmed") == "True" and v.get("ResolvedRefs") == "True" for v in listeners.values()),
      {"Programmed": programmed, "listeners": listeners})
deploys = [d["metadata"]["name"] for d in by("Deployment")]
services = {s["metadata"]["name"]: s["spec"]["type"] for s in by("Service")}
check("T04 one gateway Deployment and Service, both from the istio/gateway release",
      deploys == ["ewallet-edge-gateway"] and list(services) == ["ewallet-edge-gateway"]
      and gw["status"].get("addresses") == [{"type": "Hostname", "value": "ewallet-edge-gateway.ewallet-ingress.svc.cluster.local"}],
      {"deployments": deploys, "services": services, "addresses": gw["status"].get("addresses")})
check("T05 no LoadBalancer Service, before or after",
      "LoadBalancer" not in services.values() and sys.argv[2] == sys.argv[3], {"types": services, "lb_before": sys.argv[2], "lb_after": sys.argv[3]})
routes = {r["metadata"]["name"]: {c["type"]: c["status"] for p in r["status"].get("parents", []) for c in p["conditions"]} for r in by("HTTPRoute")}
check("T06 every HTTPRoute accepted", len(routes) == 5 and all(v.get("Accepted") == "True" for v in routes.values()), routes)
check("T07 every HTTPRoute backend resolved", all(v.get("ResolvedRefs") == "True" for v in routes.values()),
      {n: v.get("ResolvedRefs") for n, v in routes.items()})
check("T07 one ReferenceGrant per route, each to one Service",
      len(by("ReferenceGrant")) == 4 and all(len(g["spec"]["to"]) == 1 for g in by("ReferenceGrant")),
      [g["metadata"]["name"] for g in by("ReferenceGrant")])
print("".join(json.dumps(o) + "\n" for o in out), end="")
PY

# One ingress workload, Helm-owned, and no LoadBalancer anywhere in the cluster.
python3 "$root/scripts/edge/ownership_preflight.py" live --context "$CONTEXT" > "$evidence/preflight-live.txt" || true
ok=false; grep -qx EDGE_OWNERSHIP_OK "$evidence/preflight-live.txt" && ok=true
printf '{"name": "ownership preflight (live) finds one Helm-owned ingress workload", "passed": %s, "observed": "%s"}\n' \
  "$ok" "$(tr '\n' ' ' < "$evidence/preflight-live.txt")" >> "$evidence/status.jsonl"

# What Envoy was actually given: route names (the EnvoyFilter matches them) and retries.
pod=$(k -n "$NS" get pod -l istio=ewallet-edge-gateway -o name | head -1)
k -n "$NS" exec "$pod" -- pilot-agent request GET config_dump > "$work/config_dump.json"
python3 - "$work/config_dump.json" "$mode" >> "$evidence/status.jsonl" <<'PY'
import json, sys
dump = json.load(open(sys.argv[1]))["configs"]
routes = {}
for c in dump:
    if "RoutesConfigDump" in c["@type"]:
        for r in c.get("dynamic_route_configs", []):
            for vh in r["route_config"].get("virtual_hosts", []):
                for x in vh["routes"]:
                    routes[x.get("name")] = x
out = []
def check(name, ok, observed):
    out.append({"name": name, "passed": bool(ok), "observed": str(observed)[:200]})
expected = {"feature-api", "kagent-ui", "agent-registry", "grafana", "https-redirect"}
check("Envoy routes are named after the HTTPRoute rules", expected <= set(routes), sorted(routes))
service_routes = [routes[n] for n in expected - {"https-redirect"} if n in routes]
check("no Envoy route retries a request", service_routes and all("retry_policy" not in r.get("route", {}) for r in service_routes),
      [r.get("route", {}).get("retry_policy") for r in service_routes])
print("".join(json.dumps(o) + "\n" for o in out), end="")
PY

node=$(k get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
https=$(k -n "$NS" get svc ewallet-edge-gateway -o jsonpath='{.spec.ports[?(@.port==443)].nodePort}')
http=$(k -n "$NS" get svc ewallet-edge-gateway -o jsonpath='{.spec.ports[?(@.port==80)].nodePort}')
# Istio sets no TLS floor on Gateway API listeners, so test Envoy's default by handshake.
# SECLEVEL=0 lets this client offer TLS 1.1; a refusal then comes from the gateway.
cipher() {
  local line; line=$(echo | timeout 10 openssl s_client -connect "$node:$https" -servername "features.$DOMAIN" "$@" 2>/dev/null \
    | grep -m1 'Cipher is' || true)
  echo "${line:-no handshake}"
}
tls11=$(cipher -tls1_1 -cipher 'DEFAULT@SECLEVEL=0') tls12=$(cipher -tls1_2)
ok=false; [[ "$tls11" == *"(NONE)"* && "$tls12" == *TLSv1.2* ]] && ok=true
printf '{"name": "HTTPS listener refuses TLS 1.1 and accepts TLS 1.2", "passed": %s, "observed": "tls1.1: %s; tls1.2: %s"}\n' \
  "$ok" "$tls11" "$tls12" >> "$evidence/status.jsonl"
flags=(--stub); [ "$mode" == full ] || flags+=(--foundation)
status_failed=$(grep -c '"passed": false' "$evidence/status.jsonl" || true)
python3 "$root/scripts/edge/negative_tests.py" --address "$node" --https-port "$https" --http-port "$http" --domain "$DOMAIN" \
  --ca-file "$work/tls.crt" --credentials-file "$work/credentials" "${flags[@]}" --out "$evidence/results.jsonl"
if grep -rq "$password" "$evidence"; then echo "password reached the evidence directory" >&2; exit 1; fi
[ "$status_failed" == 0 ] || { echo "$status_failed status checks failed (status.jsonl)" >&2; exit 1; }
echo "EDGE_LOCAL_VALIDATION_PASS ($mode)"
