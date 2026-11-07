#!/usr/bin/env bash
# Validate the edge on the real Istio 1.30.7 gateway in kind. Refuses any other context.
# usage: local_validate.sh <evidence-dir>
# Real: istiod, the official gateway chart, Envoy, the rendered Gateway, VirtualServices and
# EnvoyFilter, TLS, Basic Auth and rate limits. Stand-in: one echo backend. Not GKE evidence.
set -euo pipefail
evidence=${1:?evidence directory required}
CONTEXT=${EDGE_LOCAL_CONTEXT:-kind-llm-agent-local}
[[ "$CONTEXT" == kind-* ]] || { echo "refusing non-kind context" >&2; exit 2; }
NS=ewallet-edge STUB=edge-stub DOMAIN=edge.local.test
root=$(cd "$(dirname "$0")/../.." && pwd); chart=$root/infra/helm/ewallet-edge
work=$(mktemp -d); mkdir -p "$evidence"
k() { kubectl --context "$CONTEXT" "$@"; }
cleanup() {
  [ -z "${EDGE_KEEP:-}" ] || return 0
  helm --kube-context "$CONTEXT" uninstall ewallet-edge-gateway -n "$NS" >/dev/null 2>&1 || true
  k delete ns "$NS" "$STUB" --wait=false >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
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
helm --kube-context "$CONTEXT" install ewallet-edge-gateway istio/gateway --version 1.30.7 -n "$NS" \
  -f "$chart/values-gateway.yaml" --set service.type=NodePort --wait --timeout 5m >/dev/null
python3 - "$chart/values.yaml" "$STUB" > "$work/local.yaml" <<'PY'
import sys, yaml
routes = yaml.safe_load(open(sys.argv[1]))["routes"]
out = {name: {"enabled": True, "destination": {"host": f"stub.{sys.argv[2]}.svc.cluster.local", "port": 8080}, "access": None}
       for name in routes}
print(yaml.safe_dump({"enabled": True, "domain": "edge.local.test", "routes": out}))
PY
helm template edge "$chart" -f "$work/local.yaml" > "$evidence/rendered.yaml"
k apply -f "$evidence/rendered.yaml" >/dev/null
k -n "$STUB" rollout status deploy/stub --timeout=120s >/dev/null
sleep 8
# NodePort on the kind node: a port-forward drops under bursts and would fake failures.
node=$(k get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
https=$(k -n "$NS" get svc ewallet-edge-gateway -o jsonpath='{.spec.ports[?(@.port==443)].nodePort}')
http=$(k -n "$NS" get svc ewallet-edge-gateway -o jsonpath='{.spec.ports[?(@.port==80)].nodePort}')
python3 "$root/scripts/edge/negative_tests.py" --address "$node" --https-port "$https" --http-port "$http" --domain "$DOMAIN" \
  --ca-file "$work/tls.crt" --credentials-file "$work/credentials" --stub --out "$evidence/results.jsonl"
if grep -rq "$password" "$evidence"; then echo "password reached the evidence directory" >&2; exit 1; fi
echo EDGE_LOCAL_VALIDATION_PASS
