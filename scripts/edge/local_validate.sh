#!/usr/bin/env bash
# Validate the edge on the real Istio 1.30.7 gateway in kind. Refuses any other context.
# usage: local_validate.sh <evidence-dir> [foundation|full]
#   foundation (G2.1): Gateway API status, workload ownership, routing, TLS; no security filters.
#   full (G2.2): also Basic Auth, rate limits, backend policies (default-deny namespace in
#   ambient mode, edge NetworkPolicy and AuthorizationPolicy, three intruder probes), a
#   gateway restart, and a broken users file (must fail closed).
# Real: istiod, ztunnel, the official gateway chart, Envoy, the rendered chart, TLS.
# Stand-in: one echo backend for every route. Not GKE evidence.
set -euo pipefail
evidence=${1:?evidence directory required} mode=${2:-foundation}
[[ "$mode" == foundation || "$mode" == full ]] || { echo "mode must be foundation or full" >&2; exit 2; }
CONTEXT=${EDGE_LOCAL_CONTEXT:-kind-llm-agent-local}
[[ "$CONTEXT" == kind-* ]] || { echo "refusing non-kind context" >&2; exit 2; }
kubectl --context "$CONTEXT" get node "${CONTEXT#kind-}-control-plane" >/dev/null || { echo "not the kind node of $CONTEXT" >&2; exit 2; }
NS=ewallet-ingress STUB=edge-stub INTRUDER=edge-intruder DOMAIN=edge.local.test
root=$(cd "$(dirname "$0")/../.." && pwd); chart=$root/infra/helm/ewallet-edge
k() { kubectl --context "$CONTEXT" "$@"; }
# Cleanup deletes these namespaces, so never adopt ones this run did not create.
for ns in "$NS" "$STUB" "$INTRUDER"; do
  if k get ns "$ns" >/dev/null 2>&1; then echo "namespace $ns already exists; refusing to reuse it" >&2; exit 2; fi
done
work=$(mktemp -d); mkdir -p "$evidence"
cleanup() {
  [ -z "${EDGE_KEEP:-}" ] || return 0
  helm --kube-context "$CONTEXT" uninstall ewallet-edge-gateway -n "$NS" >/dev/null 2>&1 || true
  k delete ns "$NS" "$STUB" "$INTRUDER" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
record() {  # name passed(true|false) observed
  python3 -c 'import json, sys; print(json.dumps({"name": sys.argv[1], "passed": sys.argv[2] == "true", "observed": sys.argv[3][:200]}))' \
    "$@" >> "$evidence/status.jsonl"
}
# A pod that tries the stub directly, bypassing the gateway. Prints REACHED, or BLOCKED and
# the underlying socket error (URLError hides whether it was a timeout or a DNS failure).
probe() {  # pod-name namespace labels
  k -n "$2" run "$1" --image=python:3.12-alpine --restart=Never --labels="$3" --command -- python3 -c "
import urllib.request
try:
    urllib.request.urlopen('http://stub.$STUB.svc.cluster.local:8080/', timeout=4); print('REACHED')
except Exception as error:
    print('BLOCKED', type(getattr(error, 'reason', error)).__name__)" >/dev/null
  k -n "$2" wait "pod/$1" --for=jsonpath='{.status.phase}'=Succeeded --timeout=90s >/dev/null 2>&1 || true
  k -n "$2" logs "$1" 2>/dev/null | tail -1; k -n "$2" delete pod "$1" --wait=false >/dev/null 2>&1 || true
}
# Prints "echo" when the stub answered 200 with its echo, else the HTTP status (000: no answer).
# Credentials come from a curl config file in the private work directory, never the command line.
authed_reply() {  # host path
  local code; code=$(curl -sS -m 10 -o "$work/body" -w '%{http_code}' -K "$work/curl-auth.conf" --cacert "$work/tls.crt" \
    --resolve "$1.$DOMAIN:$https:$node" "https://$1.$DOMAIN:$https$2" 2>/dev/null || true)
  if [ "$code" == 200 ] && python3 -c 'import json, sys; sys.exit(not isinstance(json.load(open(sys.argv[1])).get("headers"), dict))' "$work/body" 2>/dev/null
  then echo echo; else echo "${code:-000}"; fi
}
anon_status() { curl -sS -m 10 -o /dev/null -w '%{http_code}' --cacert "$work/tls.crt" --resolve "$1.$DOMAIN:$https:$node" "https://$1.$DOMAIN:$https$2" 2>/dev/null || true; }
lb_before=$(k get svc -A -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}/{.metadata.name} {end}')
k create ns "$NS" >/dev/null; k create ns "$STUB" >/dev/null
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -subj "/CN=$DOMAIN" \
  -addext "subjectAltName=DNS:*.$DOMAIN" -keyout "$work/tls.key" -out "$work/tls.crt" 2>/dev/null
k -n "$NS" create secret tls edge-tls --cert="$work/tls.crt" --key="$work/tls.key" >/dev/null
# A quote, a backslash, an apostrophe and spaces: every client must pass them through unchanged.
secret=$(openssl rand -hex 24); password="$secret q\"u'o\\te x"
printf 'edge:%s\n' "$password" > "$work/credentials"
python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from negative_tests import curl_user_config
print(curl_user_config(open(sys.argv[2]).read().rstrip("\r\n")), end="")' "$root/scripts/edge" "$work/credentials" > "$work/curl-auth.conf"
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
k -n "$STUB" rollout status deploy/stub --timeout=120s >/dev/null
if [ "$mode" == full ]; then
  # The backend namespace mirrors ewallet-agents: ambient, default-deny, ALLOW-only policies.
  k create ns "$INTRUDER" >/dev/null
  control=$(probe control "$INTRUDER" "app=probe")  # recorded once status.jsonl exists
  k label ns "$STUB" istio.io/dataplane-mode=ambient >/dev/null
  k -n "$STUB" apply -f - >/dev/null <<YAML
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: default-deny}
spec: {podSelector: {}, policyTypes: [Ingress]}
YAML
fi
# NodePort on the kind node only: a port-forward drops under bursts and would fake failures.
helm --kube-context "$CONTEXT" install ewallet-edge-gateway istio/gateway --version 1.30.7 -n "$NS" \
  -f "$chart/values-gateway.yaml" --set service.type=NodePort --wait --timeout 5m >/dev/null
python3 - "$chart/values.yaml" "$STUB" "$mode" > "$work/local.yaml" <<'PY'
import sys, yaml
routes = yaml.safe_load(open(sys.argv[1]))["routes"]
out = {name: {"enabled": True, "backend": {"service": "stub", "namespace": sys.argv[2], "port": 8080}, "access": None}
       for name in routes}
if sys.argv[3] == "full":  # one set of edge policies covers the shared stub
    # null removes the chart's default label: Helm merges maps, it does not replace them.
    out["feature-api"]["access"] = {"namespace": sys.argv[2], "podLabels": {"app": "stub", "app.kubernetes.io/name": None}, "port": 8080,
                                    "ambient": True, "authorizationPolicy": True}
print(yaml.safe_dump({"enabled": True, "domain": "edge.local.test", "securityFiltersVerified": sys.argv[3] == "full",
                      "routes": out}))
PY
helm template edge "$chart" -n "$NS" -f "$work/local.yaml" > "$evidence/rendered.yaml"
python3 "$root/scripts/edge/ownership_preflight.py" render "$evidence/rendered.yaml" > "$evidence/preflight-render.txt"
k apply -f "$evidence/rendered.yaml" >/dev/null
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

if [ "$mode" == full ]; then
  record "network control: a pod reaches the stub before any policy" "$([ "$control" == REACHED ] && echo true || echo false)" "$control"
  for case in "other-namespace:$INTRUDER:app=probe" "gateway-namespace-unlabelled:$NS:app=probe" \
              "gateway-labels-wrong-identity:$NS:istio=ewallet-edge-gateway,app=probe"; do
    IFS=: read -r name ns labels <<< "$case"
    seen=$(probe "intruder-$name" "$ns" "$labels")
    # Only a refused, reset or timed-out connection is a policy block; a DNS error proves nothing.
    record "network: $name pod cannot reach the backend directly" \
      "$([[ "$seen" =~ ^BLOCKED\ (TimeoutError|ConnectionResetError|ConnectionRefusedError|RemoteDisconnected)$ ]] && echo true || echo false)" "$seen"
  done
fi

# What Envoy was actually given: route names, retries, and where the security filters sit.
envoy_checks() {  # label
  local pod; pod=$(k -n "$NS" get pod -l istio=ewallet-edge-gateway -o name | head -1)
  k -n "$NS" exec "$pod" -- pilot-agent request GET config_dump > "$work/config_dump.json"
  python3 - "$work/config_dump.json" "$mode" "$1" "$work/local.yaml" "$chart/values.yaml" >> "$evidence/status.jsonl" <<'PY'
import json, sys, yaml
dump = json.load(open(sys.argv[1]))["configs"]
mode, label = sys.argv[2], sys.argv[3]
limits = {n: r["rateLimit"]["maxTokens"] for n, r in yaml.safe_load(open(sys.argv[5]))["routes"].items()}
routes, chains = {}, {}
for c in dump:
    if "RoutesConfigDump" in c["@type"]:
        for r in c.get("dynamic_route_configs", []):
            for vh in r["route_config"].get("virtual_hosts", []):
                for x in vh["routes"]:
                    routes[x.get("name")] = x
    if "ListenersConfigDump" in c["@type"]:
        for l in c.get("dynamic_listeners", []):
            listener = l.get("active_state", {}).get("listener", {})
            port = listener.get("address", {}).get("socket_address", {}).get("port_value")
            for fc in listener.get("filter_chains", []):
                for f in fc.get("filters", []):
                    if f["name"] == "envoy.filters.network.http_connection_manager":
                        chains.setdefault(port, [h["name"] for h in f["typed_config"].get("http_filters", [])])
out = []
def check(name, ok, observed):
    out.append({"name": name, "passed": bool(ok), "observed": str(observed)[:200]})
expected = {"feature-api", "kagent-ui", "agent-registry", "grafana", "https-redirect"}
check(f"{label}Envoy routes are named after the HTTPRoute rules", expected <= set(routes), sorted(routes))
service_routes = [routes[n] for n in expected - {"https-redirect"} if n in routes]
check(f"{label}no Envoy route retries a request", service_routes and all("retry_policy" not in r.get("route", {}) for r in service_routes),
      [r.get("route", {}).get("retry_policy") for r in service_routes])
https, http = chains.get(443, []), chains.get(80, [])
rl, ba, router = "envoy.filters.http.local_ratelimit", "envoy.filters.http.basic_auth", "envoy.filters.http.router"
if mode == "full":
    check(f"{label}HTTPS chain runs rate limit, then Basic Auth, then the router",
          rl in https and ba in https and https.index(rl) < https.index(ba) < https.index(router), https)
    per_route = {n: routes[n].get("typed_per_filter_config", {}).get(rl, {}).get("token_bucket", {}).get("max_tokens")
                 for n in expected - {"https-redirect"} if n in routes}
    check(f"{label}each route carries its own token bucket", per_route == {n: limits[n] for n in per_route} and len(per_route) == 4, per_route)
else:
    check(f"{label}no security filter is attached before G2.2", rl not in https and ba not in https, https)
check(f"{label}the HTTP redirect listener has no security filter", http and rl not in http and ba not in http, http)
print("".join(json.dumps(o) + "\n" for o in out), end="")
PY
}
envoy_checks ""

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
negative() {  # output file
  python3 "$root/scripts/edge/negative_tests.py" --address "$node" --https-port "$https" --http-port "$http" --domain "$DOMAIN" \
    --ca-file "$work/tls.crt" --credentials-file "$work/credentials" "${flags[@]}" --out "$1"
}
negative "$evidence/results.jsonl" || failed_http=1
if [ "$mode" == full ]; then
  # The configuration must survive a gateway restart.
  k -n "$NS" rollout restart deploy/ewallet-edge-gateway >/dev/null
  k -n "$NS" rollout status deploy/ewallet-edge-gateway --timeout=180s >/dev/null; sleep 10
  envoy_checks "after restart: "
  negative "$evidence/results-after-restart.jsonl" || failed_http=1
  # Fail closed: point Basic Auth at a missing users file. A running proxy must keep its
  # last good listener; a restarted one must serve nothing rather than serve unauthenticated.
  k -n "$NS" patch envoyfilter edge-security --type=json \
    -p '[{"op":"replace","path":"/spec/configPatches/1/patch/value/typed_config/users/filename","value":"/etc/edge-auth/missing"}]' >/dev/null
  sleep 15
  # Envoy must reject (NACK) the new listener and keep serving the old one unchanged: Basic
  # Auth still active on 443, anonymous exactly 401, and the real password still reaches the
  # stub (a broken users file would refuse it). Envoy redacts the users data source in its
  # config dump, so the file name itself cannot be read back.
  good=$(k -n "$NS" get pod -l istio=ewallet-edge-gateway -o name | head -1)
  basic_auth=$(k -n "$NS" exec "$good" -- pilot-agent request GET config_dump | python3 -c '
import json, sys
for c in json.load(sys.stdin)["configs"]:
    for l in c.get("dynamic_listeners", []):
        listener = l.get("active_state", {}).get("listener", {})
        if listener.get("address", {}).get("socket_address", {}).get("port_value") != 443:
            continue
        for fc in listener.get("filter_chains", []):
            for f in fc.get("filters", []):
                for h in f.get("typed_config", {}).get("http_filters", []):
                    if h["name"] == "envoy.filters.http.basic_auth":
                        print("active"); sys.exit()
print("missing")')
  rejected=$(k -n "$NS" logs "$good" 2>/dev/null | grep -c 'Listener rejected' || true)
  a1=$(anon_status features /v1/features/lookup) v1=$(authed_reply registry /)
  ok=false; [ "$a1" == 401 ] && [ "$v1" == echo ] && [ "$basic_auth" == active ] && [ "$rejected" -gt 0 ] && ok=true
  record "broken users file, running proxy: listener update rejected, last good auth kept" "$ok" \
    "anonymous=$a1 authenticated=$v1 basic_auth on 443=$basic_auth rejected-listener log lines=$rejected"
  # A new pod gets the broken config from the start: Envoy rejects the listener, the pod never
  # becomes Ready, and the rolling update keeps the old pod with its last good config.
  k -n "$NS" rollout restart deploy/ewallet-edge-gateway >/dev/null
  k -n "$NS" rollout status deploy/ewallet-edge-gateway --timeout=60s >/dev/null 2>&1 || true; sleep 10
  bad=$(k -n "$NS" get pods -l istio=ewallet-edge-gateway -o jsonpath='{range .items[?(@.status.containerStatuses[0].ready==false)]}{.metadata.name} {end}')
  ready=$(k -n "$NS" get pods -l istio=ewallet-edge-gateway -o jsonpath='{range .items[?(@.status.containerStatuses[0].ready==true)]}{.metadata.name} {end}')
  bad=${bad% } ready=${ready% } listeners="" bad_rejected=0
  if [ -n "$bad" ] && [[ "$bad" != *" "* ]]; then
    listeners=$(k -n "$NS" exec "$bad" -- pilot-agent request GET listeners 2>/dev/null | tr '\n' ' ' || true)
    bad_rejected=$(k -n "$NS" logs "$bad" 2>/dev/null | grep -c 'Listener rejected' || true)
  fi
  a2=$(anon_status features /v1/features/lookup) v2=$(authed_reply registry /)
  ok=false
  [ -n "$bad" ] && [[ "$bad" != *" "* ]] && [ -n "$ready" ] && [[ "$ready" != *" "* ]] && [ -n "$listeners" ] \
    && [[ "$listeners" != *0.0.0.0_443* ]] && [ "$bad_rejected" -gt 0 ] && [ "$a2" == 401 ] && [ "$v2" == echo ] && ok=true
  record "broken users file, restarted proxy: the new pod rejects its listener, serves no HTTPS and never becomes Ready" "$ok" \
    "via Service anonymous=$a2 authenticated=$v2; ready pods=${ready:-none}; not-ready pod=${bad:-none} rejected-listener log lines=$bad_rejected; its listeners: ${listeners:-none}"
fi
if grep -rqF -e "$password" -e "$secret" "$evidence"; then echo "password reached the evidence directory" >&2; exit 1; fi
status_failed=$(grep -c '"passed": false' "$evidence/status.jsonl" || true)
[ "$status_failed" == 0 ] || { echo "$status_failed status checks failed (status.jsonl)" >&2; exit 1; }
[ -z "${failed_http:-}" ] || { echo "HTTP checks failed (results*.jsonl)" >&2; exit 1; }
echo "EDGE_LOCAL_VALIDATION_PASS ($mode)"
