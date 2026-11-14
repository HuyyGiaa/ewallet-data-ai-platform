#!/usr/bin/env bash
# CP2: B' feasibility on a throw-away kind cluster. Reproduces the Substrate 0.0.9 WorkerPool
# /scale selector problem with KEDA 2.21, applies the reviewed CRD patch
# (infra/kubernetes/substrate-scale/), drives a real KEDA -> HPA -> WorkerPool -> atecontroller
# -> Deployment loop up and down, checks that atecontroller keeps the HPA's decision, then
# reverts the CRD. Everything happens in kind cluster `ewallet-bprime`, deleted on exit unless
# BPRIME_KEEP=1. No cloud resource is touched.
#
# BPRIME_REUSE_CONTEXT=kind-<name> runs in an existing local kind cluster instead (for a host
# whose inotify limits refuse another cluster). The script then refuses a cluster that already
# has KEDA or Substrate CRDs, and on exit uninstalls exactly what it installed: the keda and
# substrate-crds releases, the ate-system and bprime namespaces, and the CRDs of both charts.
#
# Limits, stated in the summary: only atecontroller runs (no ate-api, atelet, Valkey or gVisor),
# and worker pods use the pause image, so actors are not exercised here; the metric is a
# metrics-api endpoint the script controls, not the agents' gauge.
#
# usage: scripts/experiments/bprime_kind.sh <evidence-dir>
set -euo pipefail
shopt -s inherit_errexit
root=$(cd "$(dirname "$0")/../.." && pwd)
out=${1:?evidence directory}
[[ ! -e "$out/summary.json" ]] || { echo "$out already holds a run" >&2; exit 2; }
mkdir -p "$out"
cluster=ewallet-bprime ctx=${BPRIME_REUSE_CONTEXT:-kind-ewallet-bprime} ns=bprime pool=business-agents
# atecontroller 0.0.9 names the worker Deployment <pool>-deployment and labels only its pods.
worker=$pool-deployment
work=$(mktemp -d)
. "$root/infra/helm/keda/versions.env"
. "$root/infra/kubernetes/kagent-pilot/versions.env"
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$out/run.log"; }
k() { kubectl --context "$ctx" --request-timeout=60s "$@"; }
cleanup() {
  rm -rf "$work"
  [[ "${BPRIME_KEEP:-0}" == 1 ]] && return 0
  if [[ -n "${BPRIME_REUSE_CONTEXT:-}" ]]; then
    # Only what this run installed; each step is allowed to find nothing.
    kubectl --context "$ctx" delete namespace bprime ate-system --ignore-not-found --wait=true >/dev/null 2>&1 || echo "cleanup: namespaces"
    helm --kube-context "$ctx" uninstall keda -n keda --wait >/dev/null 2>&1 || echo "cleanup: keda release"
    kubectl --context "$ctx" delete namespace keda --ignore-not-found >/dev/null 2>&1 || echo "cleanup: keda namespace"
    helm --kube-context "$ctx" uninstall substrate-crds >/dev/null 2>&1 || echo "cleanup: substrate-crds release"
    kubectl --context "$ctx" get crd -o name | grep -E "\.(keda\.sh|eventing\.keda\.sh|ate\.dev)$" \
      | xargs -r kubectl --context "$ctx" delete >/dev/null 2>&1 || echo "cleanup: CRDs"
    echo "cleanup of $ctx done"
  else
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || echo "cleanup: kind delete failed"
  fi
}
trap cleanup EXIT
wait_for() {   # wait_for <seconds> <description> <command...>
  local limit=$1 what=$2; shift 2
  local deadline=$((SECONDS + limit))
  until "$@" >/dev/null 2>&1; do
    (( SECONDS < deadline )) || { log "TIMEOUT $what"; return 1; }
    sleep 5
  done
  log "OK $what"
}
sample() {   # one line of replicas.jsonl
  python3 - "$(k -n $ns get workerpool $pool -o json)" "$(k -n $ns get deploy --field-selector metadata.name=$worker -o json)" \
    "$(k -n $ns get hpa -o json)" "$1" <<'PY' >> "$out/replicas.jsonl"
import json, sys, time
wp, deps, hpas, phase = json.loads(sys.argv[1]), json.loads(sys.argv[2])["items"], json.loads(sys.argv[3])["items"], sys.argv[4]
dep = deps[0] if deps else {}
hpa = hpas[0] if hpas else {}
print(json.dumps({"t": time.time(), "phase": phase, "workerpool_spec": wp["spec"].get("replicas"),
  "workerpool_status": (wp.get("status") or {}).get("replicas"),
  "deployment_spec": dep.get("spec", {}).get("replicas"), "deployment_ready": dep.get("status", {}).get("readyReplicas"),
  "hpa_desired": hpa.get("status", {}).get("desiredReplicas"),
  "hpa_conditions": {c["type"]: [c["status"], c.get("reason")] for c in hpa.get("status", {}).get("conditions", [])}}))
PY
}
replicas_are() { [[ "$(k -n $ns get workerpool $pool -o jsonpath='{.spec.replicas}')" == "$1" && \
                   "$(k -n $ns get deploy --field-selector metadata.name=$worker -o jsonpath='{.items[0].status.readyReplicas}')" == "$1" ]]; }
set_metric() { k -n $ns create configmap metric --from-literal=value.json="{\"value\": $1}" --dry-run=client -o yaml | k apply -f - >/dev/null; log "metric value=$1"; }
watch_until() {   # watch_until <seconds> <phase> <replicas>
  local deadline=$((SECONDS + $1))
  while (( SECONDS < deadline )); do
    sample "$2"
    replicas_are "$3" && { log "OK $2: WorkerPool and Deployment at $3"; return 0; }
    sleep 5
  done
  log "TIMEOUT $2 waiting for $3 replicas"; return 1
}

log "versions: kind $(kind version | awk '{print $2}'), KEDA $KEDA_VERSION, Substrate $SUBSTRATE_VERSION"
if [[ -n "${BPRIME_REUSE_CONTEXT:-}" ]]; then
  [[ "$ctx" == kind-* ]] || { log "BPRIME_REUSE_CONTEXT must be a kind context"; exit 2; }
  if k get crd -o name | grep -qE "\.(keda\.sh|ate\.dev)$"; then log "refusing: $ctx already has KEDA or Substrate CRDs"; trap - EXIT; exit 2; fi
  if k get namespace keda ate-system bprime -o name 2>/dev/null | grep -q .; then log "refusing: keda, ate-system or bprime exists"; trap - EXIT; exit 2; fi
  log "reusing existing cluster $ctx (nothing of KEDA or Substrate present)"
else
  kind create cluster --name "$cluster" --wait 120s > "$out/kind-create.log" 2>&1
fi
k version -o json > "$out/kubernetes-version.json"

# Pinned charts, digests checked.
helm pull "$SUBSTRATE_CRDS_CHART" --version "$SUBSTRATE_VERSION" --destination "$work" 2>&1 | tee "$work/crds-pull.txt" > /dev/null
grep -q "Digest: $SUBSTRATE_CRDS_DIGEST" "$work/crds-pull.txt" || { log "substrate-crds digest mismatch"; exit 1; }
helm pull "$SUBSTRATE_CHART" --version "$SUBSTRATE_VERSION" --destination "$work" 2>&1 | tee "$work/sub-pull.txt" > /dev/null
grep -q "Digest: $SUBSTRATE_CHART_DIGEST" "$work/sub-pull.txt" || { log "substrate digest mismatch"; exit 1; }
helm pull keda --repo "$KEDA_CHART_REPO" --version "$KEDA_VERSION" --destination "$work" > /dev/null
[[ "$(sha256sum "$work/keda-$KEDA_VERSION.tgz" | cut -d' ' -f1)" == "$KEDA_CHART_TGZ_SHA256" ]] || { log "KEDA digest mismatch"; exit 1; }
log "chart digests verified"
helm --kube-context "$ctx" install substrate-crds "$work/substrate-crds-$SUBSTRATE_VERSION.tgz" > /dev/null
k get crd workerpools.ate.dev -o json > "$out/crd-original.json"
python3 -c 'import json,sys,hashlib; print(hashlib.sha256(json.dumps(json.load(open(sys.argv[1]))["spec"],sort_keys=True).encode()).hexdigest())' \
  "$out/crd-original.json" > "$out/crd-original.spec.sha256"
helm --kube-context "$ctx" install keda "$work/keda-$KEDA_VERSION.tgz" -n keda --create-namespace --wait --timeout 5m > /dev/null
log "KEDA installed"

# atecontroller only (WorkerPool reconciler); ate-api is absent, so its CA is a local self-signed one.
k create namespace ate-system > /dev/null
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=api.ate-system.svc -keyout "$work/ca.key" -out "$work/ca.crt" 2>/dev/null
k -n ate-system create configmap ateapi-ca --from-file=ca.crt="$work/ca.crt" > /dev/null
helm template substrate "$work/substrate-$SUBSTRATE_VERSION.tgz" -n ate-system | python3 -c '
import sys, yaml
keep = {("ServiceAccount", "ate-controller"), ("ClusterRole", "ate-controller"), ("ClusterRoleBinding", "ate-controller"), ("Deployment", "ate-controller")}
print(yaml.safe_dump_all([d for d in yaml.safe_load_all(sys.stdin) if d and (d["kind"], d["metadata"]["name"]) in keep]))' > "$work/atecontroller.yaml"
k apply -f "$work/atecontroller.yaml" > /dev/null
wait_for 240 "atecontroller available" k -n ate-system rollout status deploy/ate-controller --timeout=5s

# Controlled metric endpoint and the WorkerPool (pause image: no actor runtime on kind).
k create namespace $ns > /dev/null
set_metric 0
cat <<YAML | k apply -f - > /dev/null
apiVersion: apps/v1
kind: Deployment
metadata: {name: metric, namespace: $ns}
spec:
  replicas: 1
  selector: {matchLabels: {app: metric}}
  template:
    metadata: {labels: {app: metric}}
    spec:
      containers:
        - name: http
          image: python:3.12-alpine
          command: [python, -m, http.server, "8080", --directory, /data]
          volumeMounts: [{name: data, mountPath: /data}]
      volumes: [{name: data, configMap: {name: metric}}]
---
apiVersion: v1
kind: Service
metadata: {name: metric, namespace: $ns}
spec: {selector: {app: metric}, ports: [{port: 8080}]}
---
apiVersion: ate.dev/v1alpha1
kind: WorkerPool
metadata: {name: $pool, namespace: $ns, labels: {kagent.dev/worker-pool: $pool}}
spec:
  replicas: 1
  ateomImage: registry.k8s.io/pause:3.10
  sandboxClass: gvisor
  template: {resources: {requests: {cpu: 10m, memory: 16Mi}}}
YAML
wait_for 240 "metric endpoint" k -n $ns rollout status deploy/metric --timeout=5s
wait_for 240 "worker Deployment created by atecontroller" replicas_are 1
k -n $ns get deploy --field-selector metadata.name=$worker -o yaml > "$out/worker-deployment.yaml"
scaled_object() {
cat <<YAML | k apply -f - > /dev/null
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: {name: $pool, namespace: $ns}
spec:
  scaleTargetRef: {apiVersion: ate.dev/v1alpha1, kind: WorkerPool, name: $pool}
  minReplicaCount: 1
  maxReplicaCount: 3
  pollingInterval: 5
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleDown: {stabilizationWindowSeconds: 30, policies: [{type: Pods, value: 1, periodSeconds: 15}]}
  triggers:
    - type: metrics-api
      metricType: AverageValue
      metadata: {url: "http://metric.$ns.svc:8080/value.json", valueLocation: value, targetValue: "1"}
YAML
}

# Phase 1: unpatched CRD reproduces the problem.
scaled_object
sleep 45
set_metric 3
sleep 60
sample unpatched
k -n $ns get --raw "/apis/ate.dev/v1alpha1/namespaces/$ns/workerpools/$pool/scale" > "$out/scale-unpatched.json"
k -n $ns describe hpa > "$out/hpa-unpatched.txt"
unpatched_replicas=$(k -n $ns get workerpool $pool -o jsonpath='{.spec.replicas}')
selector_error=$(grep -c -i "selector is required\|InvalidSelector\|SelectorRequired" "$out/hpa-unpatched.txt" || true)
log "unpatched: WorkerPool replicas=$unpatched_replicas, selector errors in HPA=$selector_error"
set_metric 0

# Phase 2: reviewed patch, selector, real scale-out and scale-in.
"$root/infra/kubernetes/substrate-scale/crd_patch.sh" backup "$ctx" "$out/crd-backup" > /dev/null
"$root/infra/kubernetes/substrate-scale/crd_patch.sh" dry-run "$ctx" > "$out/crd-dry-run.txt"
CRD_PATCH_APPROVED=yes "$root/infra/kubernetes/substrate-scale/crd_patch.sh" apply "$ctx" | tee -a "$out/run.log"
k -n $ns patch workerpool $pool --type=merge -p "{\"spec\":{\"scaleSelector\":\"ate.dev/worker-pool=$pool\"}}" > /dev/null
"$root/infra/kubernetes/substrate-scale/crd_patch.sh" verify "$ctx" $pool $ns | tee -a "$out/run.log"
k -n $ns get --raw "/apis/ate.dev/v1alpha1/namespaces/$ns/workerpools/$pool/scale" > "$out/scale-patched.json"
set_metric 3
watch_until 300 scale-out 3
k -n $ns describe hpa > "$out/hpa-scaled-out.txt"
k -n $ns get hpa -o yaml > "$out/hpa.yaml"
k -n $ns get scaledobject $pool -o yaml > "$out/scaledobject.yaml"
# atecontroller must keep the HPA's decision, including after a forced reconcile.
k -n $ns annotate workerpool $pool ewallet.io/reconcile-probe="$(date +%s)" --overwrite > /dev/null
sleep 30
sample after-reconcile
replicas_are 3 && controller_kept=true || controller_kept=false
log "atecontroller kept 3 replicas after a forced reconcile: $controller_kept"
set_metric 0
watch_until 300 scale-in 1

# Phase 3: rollback to the original CRD.
k -n $ns delete scaledobject $pool --wait=true > /dev/null
k -n $ns patch workerpool $pool --type=json -p '[{"op":"remove","path":"/spec/scaleSelector"}]' > /dev/null
CRD_PATCH_APPROVED=yes "$root/infra/kubernetes/substrate-scale/crd_patch.sh" revert "$ctx" "$out/crd-backup" | tee -a "$out/run.log"
k -n $ns patch workerpool $pool --type=merge -p '{"spec":{"replicas":2}}' > /dev/null
wait_for 120 "WorkerPool still reconciles after the revert" replicas_are 2
hpa_left=$(k -n $ns get hpa -o name | wc -l)

python3 - "$out" "$unpatched_replicas" "$selector_error" "$controller_kept" "$hpa_left" <<'PY'
import json, sys
out, unpatched, errors, kept, hpa_left = sys.argv[1:]
rows = [json.loads(line) for line in open(f"{out}/replicas.jsonl")]
peak = max((row["workerpool_spec"] or 0) for row in rows if row["phase"] == "scale-out")
summary = {
  "problem_reproduced": unpatched == "1" and int(errors) > 0,
  "patched_selector": json.load(open(f"{out}/scale-patched.json"))["status"].get("selector"),
  "scale_out_to": peak,
  "controller_kept_hpa_decision": kept == "true",
  "scale_in_to": rows[-1]["workerpool_spec"],
  "rollback_hpa_remaining": int(hpa_left),
  "limits": ["only atecontroller runs: no ate-api, atelet, Valkey or gVisor; actors not exercised",
             "worker pods use the pause image", "metric: script-controlled metrics-api endpoint"],
}
summary["verdict"] = "CONTROL_LOOP_VALIDATED" if (summary["problem_reproduced"] and summary["patched_selector"]
  and peak == 3 and summary["controller_kept_hpa_decision"] and summary["scale_in_to"] == 1
  and summary["rollback_hpa_remaining"] == 0) else "NOT_VALIDATED"
json.dump(summary, open(f"{out}/summary.json", "w"), indent=1)
print(json.dumps(summary, indent=1))
PY
