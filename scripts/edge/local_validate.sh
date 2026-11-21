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
  docker rm -f g3b1-pebble g3b1-challtestsrv >/dev/null 2>&1 || true
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
authed_reply() {  # host path [curl args]
  local code; code=$(curl -sS -m 10 -o "$work/body" -w '%{http_code}' -K "$work/curl-auth.conf" --cacert "$work/tls.crt" \
    --resolve "$1.$DOMAIN:$https:$node" "https://$1.$DOMAIN:$https$2" "${@:3}" 2>/dev/null || true)
  if [ "$code" == 200 ] && python3 -c 'import json, sys; sys.exit(not isinstance(json.load(open(sys.argv[1])).get("headers"), dict))' "$work/body" 2>/dev/null
  then echo echo; else echo "${code:-000}"; fi
}
anon_status() { curl -sS -m 10 -o /dev/null -w '%{http_code}' --cacert "$work/tls.crt" --resolve "$1.$DOMAIN:$https:$node" "https://$1.$DOMAIN:$https$2" "${@:3}" 2>/dev/null || true; }
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
        - name: stub
          image: python:3.12-alpine
          command: [python3, /stub/echo_stub.py]
          volumeMounts: [{name: stub, mountPath: /stub}]
          # Like kagent-ui: an HTTP probe the edge NetworkPolicy must admit (probePort).
          readinessProbe: {httpGet: {path: /, port: 8080}, periodSeconds: 5}
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
                                    "ambient": True, "authorizationPolicy": True, "probePort": 8080}
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
  # kind does not run Calico, so this shows only that the probe rule is accepted and keeps the
  # pod Ready; whether Calico would drop the probe without it is checked on GKE (G3-A1).
  probe_rule=$(k -n "$STUB" get networkpolicy allow-edge-to-feature-api -o jsonpath='{.spec.ingress[1].from[0].ipBlock.cidr}:{.spec.ingress[1].ports[*].port}')
  stub_ready=$(k -n "$STUB" get pod -l app=stub -o jsonpath='{.items[0].status.containerStatuses[0].ready}')
  record "edge policy admits the ambient probe source on the probe port only; stub stays Ready" \
    "$([ "$probe_rule" == 169.254.7.127/32:8080 ] && [ "$stub_ready" == true ] && echo true || echo false)" "rule=$probe_rule ready=$stub_ready"
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
negative() {  # output file [extra flags]
  python3 "$root/scripts/edge/negative_tests.py" --address "$node" --https-port "$https" --http-port "$http" --domain "$DOMAIN" \
    --ca-file "$work/tls.crt" --credentials-file "$work/credentials" "${flags[@]}" "${@:2}" --out "$1"
}
negative "$evidence/results.jsonl" || failed_http=1
if [ "$mode" == full ]; then
  # The configuration must survive a gateway restart.
  k -n "$NS" rollout restart deploy/ewallet-edge-gateway >/dev/null
  k -n "$NS" rollout status deploy/ewallet-edge-gateway --timeout=180s >/dev/null; sleep 10
  envoy_checks "after restart: "
  # The G3-B/G4 method: one multiplexed HTTP/2 connection for the rate-limit burst.
  negative "$evidence/results-after-restart.jsonl" --rate-limit-mode http2 || failed_http=1
  # G3-B0 source restriction (kagent): KA-01..10. kind's NodePort SNATs the client, so this
  # shows the policy logic against the address Envoy actually sees, not GKE source-IP
  # preservation (that is a G3-B live gate).
  peer=$(curl -sS -m 10 -K "$work/curl-auth.conf" --cacert "$work/tls.crt" --resolve "features.$DOMAIN:$https:$node" \
    -X POST -H 'content-type: application/json' -d '{}' "https://features.$DOMAIN:$https/v1/features/lookup" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["headers"].get("x-forwarded-for", "").split(",")[-1].strip())')
  restrict() {  # allowed CIDR JSON list
    helm template edge "$chart" -n "$NS" -f "$work/local.yaml" --set sourceRestriction.enabled=true --set sourceRestriction.allowPrivateCidrs=true \
      --set-json "sourceRestriction.allowedCidrs=$1" -s templates/source-restriction.yaml | k apply -f - >/dev/null; sleep 10
  }
  ka() { record "$1" "$([ "$2" == "$3" ] && echo true || echo false)" "expected $3, observed $2${4:+; $4}"; }
  restrict '["192.0.2.0/24"]'
  ka "KA-01 restricted, source outside the allowlist: valid credentials are refused" "$(authed_reply agents /)" 403 "peer seen by Envoy=$peer"
  a=$(anon_status agents /); record "KA-01 restricted, source outside the allowlist: no credentials are refused" "$([[ "$a" == 403 || "$a" == 401 ]] && echo true || echo false)" "observed $a"
  ka "KA-04 forged X-Forwarded-For with an allowed address does not bypass" "$(authed_reply agents / -H 'x-forwarded-for: 192.0.2.10' -H 'x-real-ip: 192.0.2.10' -H 'x-envoy-external-address: 192.0.2.10')" 403
  ka "KA-06 controller API path through the UI is refused" "$(authed_reply agents /api/agents)" 403
  for h in "agents.$DOMAIN" "AGENTS.${DOMAIN^^}:$https" "agents.$DOMAIN.:$https"; do
    o=$(authed_reply agents / -H "Host: $h"); record "KA-06 Host variant '$h' does not bypass" "$([ "$o" != echo ] && echo true || echo false)" "observed $o"
  done
  for host in features registry observe; do
    path=/; extra=(); [ $host == features ] && { path=/v1/features/lookup; extra=(-X POST -H 'content-type: application/json' -d '{}'); }
    ka "KA-10 restriction on: $host still reaches its backend" "$(authed_reply $host $path "${extra[@]}")" echo
  done
  restrict "[\"$peer/32\"]"
  ka "KA-02 allowed source with valid credentials reaches the backend" "$(authed_reply agents /)" echo "allowlist=$peer/32"
  ka "KA-03 allowed source without credentials gets 401" "$(anon_status agents /)" 401
  ka "KA-05 allowed source with a wrong password gets 401" "$(anon_status agents / -u edge:definitely-wrong)" 401
  ka "KA-06 allowed source: API path still needs credentials" "$(anon_status agents /api/agents)" 401
  k -n "$NS" delete authorizationpolicy edge-source-restriction >/dev/null; sleep 10
  ka "KA-08 restriction off: G3-A1 behaviour (valid credentials reach the backend)" "$(authed_reply agents /)" echo
  if [ -n "${EDGE_ACME_TEST:-}" ]; then
    # G3-B1 HTTP-01 through the real gateway: Pebble (Let's Encrypt's test CA) validates over
    # the kind node's HTTP NodePort; certbot runs the repository hook. LOCAL_KIND, not GKE.
    plain() { curl -s -m 10 -o "$work/plain" -w '%{http_code}' --resolve "$1.$DOMAIN:$http:$node" "http://$1.$DOMAIN:$http$2" "${@:3}" || true; }
    helm template edge "$chart" -n "$NS" -f "$work/local.yaml" --set acmeHttp01.enabled=true -s templates/acme-http01.yaml | k apply -f - >/dev/null
    k -n "$NS" rollout status deploy/edge-acme-responder --timeout=120s >/dev/null; sleep 8
    ka "ACME-01 challenge path, unknown token: 404 from the responder (not a redirect)" "$(plain features /.well-known/acme-challenge/unknownToken123)" 404
    ka "ACME-02 other port-80 paths still redirect" "$(plain features /v1/features/lookup -X POST)" 301
    ka "ACME-02 port-80 root still redirects" "$(plain registry /)" 301
    ka "ACME-03 a look-alike prefix is not the challenge path" "$(plain features /.well-known/acme-challenge-x)" 301
    o=$(plain features '/.well-known/acme-challenge/../../v1/features/lookup'); record "ACME-03 dot segments do not escape the challenge path" "$([ "$o" != 200 ] && echo true || echo false)" "observed $o"
    o=$(plain features '/.well-known/acme-challenge/a%2F..%2F..%2Fetc%2Fpasswd'); record "ACME-03 encoded traversal is refused" "$([ "$o" == 404 ] || [ "$o" == 400 ] && echo true || echo false)" "observed $o"
    ka "ACME-04 the challenge path on 443 still needs credentials" "$(anon_status features /.well-known/acme-challenge/unknownToken123)" 401
    o=$(plain unrouted /.well-known/acme-challenge/unknownToken123); record "ACME-05 an unrouted host gets no challenge answer" "$([ "$o" != 200 ] && echo true || echo false)" "observed $o"
    printf 'synthetic-key-authorization' | k -n "$NS" exec -i deploy/edge-acme-responder -- sh -c 'cat > /challenges/tok_ABC-123'
    ka "ACME-06 a written token is served on port 80" "$(plain features /.well-known/acme-challenge/tok_ABC-123)" 200 "body=$(cat "$work/plain")"
    restrict '["192.0.2.0/24"]'
    ka "ACME-07 agents restriction on: the agents challenge is still reachable on port 80" "$(plain agents /.well-known/acme-challenge/tok_ABC-123)" 200
    ka "ACME-07 agents restriction on: HTTPS to agents is still refused" "$(authed_reply agents /)" 403
    k -n "$NS" delete authorizationpolicy edge-source-restriction >/dev/null
    k -n "$NS" exec deploy/edge-acme-responder -- rm -f /challenges/tok_ABC-123
    # Pebble and its DNS answer every name with the kind node, and validate on the HTTP NodePort.
    printf '{"pebble":{"listenAddress":"0.0.0.0:14000","managementListenAddress":"0.0.0.0:15000","certificate":"test/certs/localhost/cert.pem","privateKey":"test/certs/localhost/key.pem","httpPort":%s,"tlsPort":5001,"ocspResponderURL":"","externalAccountBindingRequired":false}}' "$http" > "$work/pebble.json"
    docker run -d --rm --name g3b1-challtestsrv --network kind ghcr.io/letsencrypt/pebble-challtestsrv:2.7.0 \
      -defaultIPv4 "$node" -defaultIPv6 "" -http01 "" -https01 "" -tlsalpn01 "" -doh "" >/dev/null
    dns=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' g3b1-challtestsrv)
    docker run -d --rm --name g3b1-pebble --network kind -p 127.0.0.1:14000:14000 -p 127.0.0.1:15000:15000 \
      -e PEBBLE_VA_NOSLEEP=1 -e PEBBLE_WFE_NONCEREJECT=0 -v "$work/pebble.json:/pebble.json:ro" \
      ghcr.io/letsencrypt/pebble:2.7.0 -config /pebble.json -dnsserver "$dns:8053" -strict=false >/dev/null
    sleep 3; docker cp g3b1-pebble:/test/certs/pebble.minica.pem "$work/minica.pem" >/dev/null
    certbot() {  # cert-name domains...
      local name=$1; shift; local d=(); for x in "$@"; do d+=(-d "$x"); done
      REQUESTS_CA_BUNDLE="$work/minica.pem" EDGE_ACME_CONTEXT="$CONTEXT" EDGE_ACME_HTTP_PORT="$http" EDGE_ACME_RESOLVE_ADDR="$node" \
        uvx --from certbot==5.10.0 certbot certonly --non-interactive --agree-tos --register-unsafely-without-email \
        --server https://localhost:14000/dir --config-dir "$work/le/config" --work-dir "$work/le/work" --logs-dir "$work/le/logs" \
        --manual --preferred-challenges http --manual-auth-hook "$root/scripts/edge/acme_http01_hook.sh auth" \
        --manual-cleanup-hook "$root/scripts/edge/acme_http01_hook.sh cleanup" --key-type ecdsa --cert-name "$name" "${d[@]}" >"$work/certbot-$name.log" 2>&1
    }
    certbot edge "features.$DOMAIN" "agents.$DOMAIN" "registry.$DOMAIN" "observe.$DOMAIN" && rc=0 || rc=$?
    live="$work/le/config/live/edge"
    sans=$(openssl x509 -in "$live/fullchain.pem" -noout -ext subjectAltName 2>/dev/null | tail -1 | tr -d ' ' || true)
    record "ACME-08 HTTP-01 issuance through the gateway (Pebble) for the four hostnames" \
      "$([ $rc == 0 ] && [[ "$sans" == *"features.$DOMAIN"* && "$sans" == *"agents.$DOMAIN"* && "$sans" == *"registry.$DOMAIN"* && "$sans" == *"observe.$DOMAIN"* ]] && echo true || echo false)" \
      "certbot exit=$rc; SAN=$sans; issuer=$(openssl x509 -in "$live/fullchain.pem" -noout -issuer 2>/dev/null || true)"
    ka "ACME-09 no challenge file is left after issuance" "$(k -n "$NS" exec deploy/edge-acme-responder -- ls -A /challenges | wc -l | tr -d ' ')" 0
    curl -s --cacert "$work/minica.pem" https://localhost:15000/roots/0 > "$work/pebble-root.pem" || true
    before=$(k -n "$NS" get secret edge-tls -o jsonpath='{.data.tls\.crt}' | sha256sum | cut -c1-16)
    k -n "$NS" create secret tls edge-tls --cert="$live/fullchain.pem" --key="$live/privkey.pem" --dry-run=client -o yaml | k apply -f - >/dev/null; sleep 12
    v=$(echo | openssl s_client -connect "$node:$https" -servername "registry.$DOMAIN" -CAfile "$work/pebble-root.pem" -verify_return_error 2>/dev/null | grep -m1 'Verify return code' || true)
    record "ACME-10 imported certificate is served and verifies against the issuing CA" "$([[ "$v" == *"0 (ok)"* ]] && echo true || echo false)" "$v"
    ka "ACME-10 Basic Auth still applies with the new certificate" "$(curl -s -m 10 -o /dev/null -w '%{http_code}' --cacert "$work/pebble-root.pem" --resolve "features.$DOMAIN:$https:$node" -X POST "https://features.$DOMAIN:$https/v1/features/lookup")" 401
    issued=$(k -n "$NS" get secret edge-tls -o jsonpath='{.data.tls\.crt}' | sha256sum | cut -c1-16)
    certbot fail "features.$DOMAIN" "notrouted.$DOMAIN" && rc=0 || rc=$?
    after=$(k -n "$NS" get secret edge-tls -o jsonpath='{.data.tls\.crt}' | sha256sum | cut -c1-16)
    left=$(k -n "$NS" exec deploy/edge-acme-responder -- ls -A /challenges | wc -l | tr -d ' ')
    record "ACME-11 a hostname the gateway does not route fails issuance, cleans up, and leaves edge-tls unchanged" \
      "$([ $rc != 0 ] && [ "$issued" == "$after" ] && [ "$left" == 0 ] && echo true || echo false)" "certbot exit=$rc; edge-tls unchanged=$([ "$issued" == "$after" ] && echo yes || echo no); challenge files left=$left"
    # Rollback: responder off, port 80 back to redirect only, original certificate back.
    helm template edge "$chart" -n "$NS" -f "$work/local.yaml" --set acmeHttp01.enabled=true -s templates/acme-http01.yaml | k delete -f - >/dev/null; sleep 8
    ka "ACME-12 after cleanup the challenge path redirects again" "$(plain features /.well-known/acme-challenge/unknownToken123)" 301
    ka "ACME-12 after cleanup no responder object remains" "$(k -n "$NS" get deploy,svc,cm,httproute -o name | grep -c acme || true)" 0
    k -n "$NS" create secret tls edge-tls --cert="$work/tls.crt" --key="$work/tls.key" --dry-run=client -o yaml | k apply -f - >/dev/null; sleep 12
    ka "ACME-12 original certificate restored" "$(k -n "$NS" get secret edge-tls -o jsonpath='{.data.tls\.crt}' | sha256sum | cut -c1-16)" "$before"
    cp "$work/certbot-edge.log" "$evidence/certbot-issue.log" 2>/dev/null; cp "$work/certbot-fail.log" "$evidence/certbot-fail.log" 2>/dev/null
    docker rm -f g3b1-pebble g3b1-challtestsrv >/dev/null 2>&1 || true
  fi
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
