#!/usr/bin/env bash
# usage: staged_rollout_check.sh <out-dir>   (kind only; EDGE_LOCAL_CONTEXT, default kind-llm-agent-local)
# Local kind check of the G3-A1 fail-closed order: gateway workload at 0 replicas, edge chart
# applied, then 1 replica. The first Envoy must start with Basic Auth and rate limit on 443.
set -euo pipefail
out=${1:?out dir}; C=${EDGE_LOCAL_CONTEXT:-kind-llm-agent-local}; NS=ewallet-ingress; STUB=edge-stub; D=edge.local.test
root=$(cd "$(dirname "$0")/../.." && pwd); chart=$root/infra/helm/ewallet-edge
[[ "$C" == kind-* ]] || { echo "refusing non-kind context $C" >&2; exit 2; }
k() { kubectl --context "$C" "$@"; }
for ns in $NS $STUB; do k get ns $ns >/dev/null 2>&1 && { echo "namespace $ns exists" >&2; exit 2; }; done
work=$(mktemp -d); trap 'helm --kube-context $C uninstall ewallet-edge-gateway -n $NS >/dev/null 2>&1 || true; k delete ns $NS $STUB --wait=false >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
k create ns $NS >/dev/null; k create ns $STUB >/dev/null
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -subj "/CN=$D" -addext "subjectAltName=DNS:*.$D" -keyout $work/k -out $work/c 2>/dev/null
k -n $NS create secret tls edge-tls --cert=$work/c --key=$work/k >/dev/null
openssl rand -hex 24 | tr -d '\n' | "$root/scripts/edge/create_basic_auth.sh" edge $NS $C >/dev/null
k -n $STUB create deployment stub --image=python:3.12-alpine -- python3 -m http.server 8080 >/dev/null
k -n $STUB expose deployment stub --port 8080 >/dev/null; k -n $STUB rollout status deploy/stub --timeout=120s >/dev/null
helm --kube-context $C install ewallet-edge-gateway istio/gateway --version 1.30.7 -n $NS -f $chart/values-gateway.yaml --set replicaCount=0 --wait --timeout 3m >/dev/null
echo "step1 gateway release at 0 replicas: pods=$(k -n $NS get pods --no-headers 2>/dev/null | wc -l) svc=$(k -n $NS get svc ewallet-edge-gateway -o jsonpath='{.spec.type}')"
python3 - "$chart/values.yaml" > $work/v.yaml <<'PY'
import sys, yaml
routes = yaml.safe_load(open(sys.argv[1]))["routes"]
print(yaml.safe_dump({"enabled": True, "domain": "edge.local.test", "securityFiltersVerified": True,
  "routes": {n: {"enabled": True, "backend": {"service": "stub", "namespace": "edge-stub", "port": 8080}, "access": None} for n in routes}}))
PY
helm template ewallet-edge $chart -n $NS -f $work/v.yaml > $work/r.yaml; k apply -f $work/r.yaml >/dev/null; sleep 20
echo "step2 edge applied with 0 gateway pods: Gateway conditions $(k -n $NS get gateway ewallet-edge -o jsonpath='{range .status.conditions[*]}{.type}={.status}({.reason}) {end}')"
echo "        HTTPRoutes Accepted/ResolvedRefs: $(k -n $NS get httproute -o jsonpath='{range .items[*]}{.metadata.name}:{.status.parents[0].conditions[?(@.type=="Accepted")].status}/{.status.parents[0].conditions[?(@.type=="ResolvedRefs")].status} {end}')"
helm --kube-context $C upgrade ewallet-edge-gateway istio/gateway --version 1.30.7 -n $NS -f $chart/values-gateway.yaml >/dev/null
# Poll from the moment the container starts: the very first config that has a 443 listener.
pod=""; for i in $(seq 1 60); do pod=$(k -n $NS get pod -l istio=ewallet-edge-gateway -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true); [ -n "$pod" ] && k -n $NS exec $pod -- pilot-agent request GET config_dump >$work/d.json 2>/dev/null && grep -q '"port_value": 443' $work/d.json && break; sleep 1; done
python3 - $work/d.json <<'PY'
import json, sys
chains = {}
for c in json.load(open(sys.argv[1]))["configs"]:
    for l in c.get("dynamic_listeners", []):
        li = l.get("active_state", {}).get("listener", {}); port = li.get("address", {}).get("socket_address", {}).get("port_value")
        for fc in li.get("filter_chains", []):
            for f in fc.get("filters", []):
                if f["name"] == "envoy.filters.network.http_connection_manager":
                    chains[port] = [h["name"].rsplit(".", 1)[-1] for h in f["typed_config"].get("http_filters", [])]
c443 = chains.get(443, [])
ok = "local_ratelimit" in c443 and "basic_auth" in c443 and c443.index("local_ratelimit") < c443.index("basic_auth") < c443.index("router")
print(f"step3 first config of the first gateway pod: 443 chain {c443}; 80 chain {chains.get(80)}")
print("STAGED_ROLLOUT_FIRST_CONFIG_SECURED" if ok else "STAGED_ROLLOUT_UNSECURED")
PY
k -n $NS rollout status deploy/ewallet-edge-gateway --timeout=120s >/dev/null
k -n $NS port-forward svc/ewallet-edge-gateway 18443:443 >/dev/null 2>&1 & pf=$!; sleep 3
code=$(curl -s -o /dev/null -w '%{http_code}' --cacert $work/c --resolve features.$D:18443:127.0.0.1 -X POST https://features.$D:18443/v1/features/lookup || true)
kill $pf 2>/dev/null || true
echo "step4 anonymous request through port-forward with the verified pod: HTTP $code (expect 401)"
