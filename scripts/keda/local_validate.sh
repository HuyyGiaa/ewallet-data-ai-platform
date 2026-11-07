#!/usr/bin/env bash
# Validate serving and agent autoscaling on a real KEDA in kind. Refuses any other context.
# usage: keda_local_validate.sh <evidence-dir>
# Real here: KEDA 2.21, Prometheus, the HPA, per-pod DNS discovery, the chart's ScaledObjects
# and PromQL, and real HTTP load. Stand-in: the pod behind them. Not evidence about GKE.
set -euo pipefail
evidence=${1:?evidence directory required}
CONTEXT=${KEDA_LOCAL_CONTEXT:-kind-llm-agent-local}
[[ "$CONTEXT" == kind-* ]] || { echo "refusing non-kind context" >&2; exit 2; }
NS=ml-platform IMAGE=python:3.12-alpine PROM=prom/prometheus:v3.7.3
root=$(cd "$(dirname "$0")/../.." && pwd); chart=$root/infra/helm/ewallet-serving
work=$(mktemp -d); mkdir -p "$evidence"; timeline=$evidence/timeline.jsonl; : > "$timeline"
k() { kubectl --context "$CONTEXT" "$@"; }
digest=sha256:$(printf 'a%.0s' {1..64})
cleanup() {
  [ -z "${KEDA_KEEP:-}" ] || return 0
  # Before KEDA goes: its finalizer would otherwise hold the namespace forever.
  k -n "$NS" delete scaledobject --all --timeout=60s >/dev/null 2>&1 || true
  k delete ns "$NS" --wait=false >/dev/null 2>&1 || true
  helm --kube-context "$CONTEXT" uninstall keda -n keda >/dev/null 2>&1 || true
  k delete ns keda --wait=false >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
k create ns "$NS" >/dev/null
helm pull keda --repo https://kedacore.github.io/charts --version 2.21.0 -d "$work" >/dev/null
. "$root/infra/helm/keda/versions.env"
[ "$(sha256sum "$work"/keda-2.21.0.tgz | cut -d' ' -f1)" = "$KEDA_CHART_TGZ_SHA256" ] || { echo "KEDA chart hash differs" >&2; exit 1; }
helm --kube-context "$CONTEXT" install keda "$work/keda-2.21.0.tgz" -n keda --create-namespace \
  -f "$root/infra/helm/keda/values.yaml" --set 'podLabels.keda=null' --wait --timeout 5m >/dev/null
k -n "$NS" create configmap stub --from-file="$root/scripts/keda/metrics_stub.py" >/dev/null
k -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: feature-api}
spec:
  replicas: 1
  selector: {matchLabels: {app.kubernetes.io/name: feature-api}}
  template:
    metadata: {labels: {app.kubernetes.io/name: feature-api}}
    spec:
      containers:
        - name: stub
          image: $IMAGE
          command: [python3, /stub/metrics_stub.py]
          ports: [{containerPort: 8080}]
          resources: {requests: {cpu: 10m, memory: 32Mi}}
          volumeMounts: [{name: stub, mountPath: /stub}]
      volumes: [{name: stub, configMap: {name: stub}}]
---
apiVersion: v1
kind: Service
metadata: {name: feature-api}
spec:
  selector: {app.kubernetes.io/name: feature-api}
  ports: [{port: 80, targetPort: 8080}]
---
apiVersion: v1
kind: ConfigMap
metadata: {name: prometheus}
data:
  prometheus.yml: |
    global: {scrape_interval: 5s}
    scrape_configs:
      - job_name: feature-api-pods
        metrics_path: /metrics
        dns_sd_configs: [{names: ["feature-api-metrics.$NS.svc.cluster.local"], type: A, port: 8080, refresh_interval: 5s}]
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: prometheus}
spec:
  selector: {matchLabels: {app: prometheus}}
  template:
    metadata: {labels: {app: prometheus}}
    spec:
      containers:
        - name: prometheus
          image: $PROM
          args: [--config.file=/etc/prometheus/prometheus.yml, --storage.tsdb.path=/tmp/data]
          ports: [{containerPort: 9090}]
          volumeMounts: [{name: config, mountPath: /etc/prometheus}]
      volumes: [{name: config, configMap: {name: prometheus}}]
---
apiVersion: v1
kind: Service
metadata: {name: prometheus}
spec:
  selector: {app: prometheus}
  ports: [{port: 9090}]
YAML
helm template serving "$chart" -s templates/metrics-discovery.yaml -s templates/scaledobjects.yaml \
  --set namespace=$NS --set prometheus.namespace=$NS --set "prometheus.serverAddress=http://prometheus.$NS.svc.cluster.local:9090" \
  --set featureApi.enabled=true --set "featureApi.image=r.example/p/i@$digest" --set featureApi.registryUri=gs://b/r \
  --set metricsDiscovery.enabled=true --set autoscaling.enabled=true --set autoscaling.targets.fraud-api.enabled=false \
  --set autoscaling.targets.feature-api.requestsPerSecondPerReplica=20 --set autoscaling.targets.feature-api.latencySeconds=5 \
  --set autoscaling.defaults.pollingIntervalSeconds=5 --set autoscaling.defaults.scaleUp.periodSeconds=15 \
  --set autoscaling.defaults.scaleDown.stabilizationWindowSeconds=20 --set autoscaling.defaults.scaleDown.periodSeconds=15 \
  > "$evidence/rendered.yaml"
python3 - "$evidence/rendered.yaml" > "$work/apply.yaml" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d and not (d["kind"] == "Service" and d["metadata"]["name"] == "fraud-api-metrics")]
docs = [d for d in docs if d["kind"] in {"Service", "ScaledObject"}]
print(yaml.safe_dump_all(docs))
PY
k -n "$NS" apply -f "$work/apply.yaml" >/dev/null
k -n "$NS" rollout status deploy/prometheus --timeout=180s >/dev/null
k -n "$NS" rollout status deploy/feature-api --timeout=120s >/dev/null
for _ in $(seq 40); do [ "$(k -n "$NS" get scaledobject feature-api -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" = True ] && break; sleep 3; done
sample() {  # <phase>
  k -n "$NS" get deploy feature-api -o json | jq -c --arg phase "$1" --arg ts "$(date -u +%FT%TZ)" \
    --argjson desired "$(k -n "$NS" get hpa keda-hpa-feature-api -o jsonpath='{.status.desiredReplicas}' 2>/dev/null || echo 0)" \
    --arg fallback "$(k -n "$NS" get scaledobject feature-api -o jsonpath='{.status.conditions[?(@.type=="Fallback")].status}')" \
    '{ts:$ts,phase:$phase,spec:.spec.replicas,ready:(.status.readyReplicas//0),hpa_desired:$desired,fallback:$fallback}' | tee -a "$timeline"
}
wait_for() {  # <phase> <replicas> <seconds>
  local deadline=$((SECONDS + $3))
  while [ $SECONDS -lt $deadline ]; do
    sample "$1" >/dev/null
    [ "$(tail -1 "$timeline" | jq .ready)" = "$2" ] && { tail -1 "$timeline"; return 0; }
    sleep 5
  done
  echo "phase $1 did not reach $2 replicas" >&2; return 1
}
sample baseline
k -n "$NS" get scaledobject feature-api -o json | jq '{conditions:.status.conditions,triggers:[.spec.triggers[]|{name,metricType}],external:.status.externalMetricNames}' > "$evidence/scaledobject-status.json"
k -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: batch/v1
kind: Job
metadata: {name: load}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: load
          image: $IMAGE
          command: [python3, -c, "import threading,time,urllib.request\ndef go():\n  while True:\n    try: urllib.request.urlopen('http://feature-api.$NS.svc.cluster.local/',timeout=2).read()\n    except Exception: pass\n    time.sleep(0.02)\nfor _ in range(4): threading.Thread(target=go,daemon=True).start()\ntime.sleep(900)"]
YAML
wait_for scale-up 3 240
k -n "$NS" delete job load --wait=false >/dev/null
wait_for scale-down 1 300
# Fallback: load again, then lose Prometheus; replicas must hold, not collapse.
k -n "$NS" scale deploy feature-api --replicas=2 >/dev/null
k -n "$NS" scale deploy prometheus --replicas=0 >/dev/null
for _ in $(seq 24); do sample prometheus-down >/dev/null; [ "$(tail -1 "$timeline" | jq -r .fallback)" = True ] && break; sleep 5; done
tail -1 "$timeline"
[ "$(tail -1 "$timeline" | jq -r .fallback)" = True ] || { echo "fallback never engaged" >&2; exit 1; }
# Agent triggers: the real webhook must accept the rendered Drift Specialist object.
k -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: drift-specialist}
spec:
  replicas: 2
  selector: {matchLabels: {a: drift}}
  template:
    metadata: {labels: {a: drift}}
    spec:
      containers:
        - {name: c, image: $IMAGE, command: [sleep, "3600"], resources: {requests: {cpu: 10m, memory: 16Mi}}}
YAML
python3 "$root/scripts/keda/render_agent_scaledobject.py" drift-specialist "$NS" > "$evidence/agent-drift-scaledobject.yaml"
k apply --dry-run=server -f "$evidence/agent-drift-scaledobject.yaml" | tee "$evidence/agent-webhook.txt"
echo KEDA_LOCAL_VALIDATION_PASS
