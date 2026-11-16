#!/usr/bin/env bash
# B′: the shared Sandbox WorkerPool (ewallet-sandbox/business-agents) under KEDA.
# Packages, each its own owner approval (BPRIME_APPROVAL=<TOKEN>:<context>):
#   pool         BPRIME-POOL         patch the live WorkerPool: spec.scaleSelector (needs PKG-P3) and
#                                    spec.ateomImage (the drain build, ATEOM_IMAGE). Only these two
#                                    fields; replicas is never written (KEDA owns it).
#   scaledobject BPRIME-SO           create/replace infra/kubernetes/agent-sandbox/workerpool-scaledobject.yaml
# PKG-W (KEDA watch) is scripts/llm_agent/sandbox_watch.sh keda; PKG-P3 (CRD) is
# infra/kubernetes/substrate-scale/crd_patch.sh.
#
# usage: bprime.sh <action> <work-dir>
#   status                [READ-ONLY] ScaledObject, HPA, WorkerPool spec/status, /scale selector, worker pods
#   pool-diff             [READ-ONLY] current vs intended pool fields, server-side dry run of the patch
#   pool-apply            [MUTATES]   BPRIME-POOL; records the previous fields for pool-rollback
#   pool-rollback         [MUTATES]   BPRIME-POOL-ROLLBACK; restores the recorded fields
#   so-diff               [READ-ONLY] server-side dry run of the ScaledObject
#   so-apply              [MUTATES]   BPRIME-SO; refuses unless KEDA watches ewallet-sandbox and /scale has a selector
#   so-rollback           [MUTATES]   BPRIME-SO-ROLLBACK; deletes the ScaledObject (KEDA removes its HPA), then
#                                     returns the pool to the 2-worker baseline
#
# Env: KUBE_CONTEXT (required), KUBECONFIG, ATEOM_IMAGE (pool-*).
set -euo pipefail
action=${1:-} work=${2:-}
die() { echo "bprime: FAIL: $*" >&2; exit 1; }
[[ -n "$action" && -n "$work" ]] || die "usage: bprime.sh <action> <work-dir>"
[[ -n "${KUBE_CONTEXT:-}" ]] || die "KUBE_CONTEXT is required"
root=$(cd "$(dirname "$0")/../.." && pwd)
so_file=$root/infra/kubernetes/agent-sandbox/workerpool-scaledobject.yaml
ns=ewallet-sandbox pool=business-agents so=business-agents-workerpool
selector="ate.dev/worker-pool=$pool"
umask 077; mkdir -p "$work"
K() { kubectl --context "$KUBE_CONTEXT" --request-timeout=60s "$@"; }
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$work/bprime.log"; }
approval() { [[ "${BPRIME_APPROVAL:-}" == "$1:$KUBE_CONTEXT" ]] || die "set BPRIME_APPROVAL=$1:$KUBE_CONTEXT only under the owner's approval"; }
field() { K -n "$ns" get workerpool "$pool" -o "jsonpath={.spec.$1}"; }

pool_patch() {
  [[ "${ATEOM_IMAGE:-}" =~ ^[a-z0-9.-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+/substrate-ateom-gvisor(:[A-Za-z0-9._+-]+)?@sha256:[0-9a-f]{64}$ ]] \
    || die "ATEOM_IMAGE must be a digest-pinned substrate-ateom-gvisor reference"
  printf '{"spec":{"scaleSelector":"%s","ateomImage":"%s"}}' "$selector" "$ATEOM_IMAGE"
}

scale_selector() {
  K get --raw "/apis/ate.dev/v1alpha1/namespaces/$ns/workerpools/$pool/scale" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"].get("selector",""))'
}

keda_watches() {
  K -n keda get deployment keda-operator -o json | python3 -c '
import json, sys
d = json.load(sys.stdin)
env = {e["name"]: e.get("value", "") for c in d["spec"]["template"]["spec"]["containers"] for e in c.get("env", [])}
sys.exit(0 if "ewallet-sandbox" in env.get("WATCH_NAMESPACE", "").split(",") else 1)'
}

case "$action" in
  status)
    { K -n "$ns" get scaledobject,hpa 2>&1 || true
      echo "workerpool spec.replicas=$(field replicas) status.replicas=$(K -n "$ns" get workerpool "$pool" -o 'jsonpath={.status.replicas}') scaleSelector=$(field scaleSelector)"
      echo "scale selector=$(scale_selector 2>/dev/null || echo none)"
      K -n "$ns" get pods -l "$selector" -o wide --no-headers; } | tee "$work/status-$(date -u +%H%M%S).txt" ;;
  pool-diff)
    echo "current: scaleSelector='$(field scaleSelector)' ateomImage=$(field ateomImage) replicas=$(field replicas)"
    echo "intended patch: $(pool_patch)"
    K -n "$ns" patch workerpool "$pool" --type merge -p "$(pool_patch)" --dry-run=server -o 'jsonpath={.spec.scaleSelector} {.spec.ateomImage} replicas={.spec.replicas}{"\n"}' ;;
  pool-apply)
    approval BPRIME-POOL
    patch=$(pool_patch)
    [[ -s "$work/pool-before.json" ]] || K -n "$ns" get workerpool "$pool" -o json \
      | python3 -c 'import json,sys; s=json.load(sys.stdin)["spec"]; print(json.dumps({"scaleSelector": s.get("scaleSelector"), "ateomImage": s["ateomImage"]}))' > "$work/pool-before.json"
    K -n "$ns" patch workerpool "$pool" --type merge -p "$patch" --dry-run=server > /dev/null
    K -n "$ns" patch workerpool "$pool" --type merge -p "$patch"
    [[ "$(field ateomImage)" == "$ATEOM_IMAGE" && "$(field scaleSelector)" == "$selector" ]] || die "pool fields not applied"
    K -n "$ns" rollout status "deployment/$pool-deployment" --timeout=900s
    log "pool-apply PASS: scaleSelector=$selector ateomImage=$ATEOM_IMAGE (replicas untouched: $(field replicas))" ;;
  pool-rollback)
    approval BPRIME-POOL-ROLLBACK
    [[ -s "$work/pool-before.json" ]] || die "no recorded pool fields in $work"
    patch=$(python3 -c 'import json,sys; b=json.load(open(sys.argv[1])); print(json.dumps({"spec": {"scaleSelector": b["scaleSelector"], "ateomImage": b["ateomImage"]}}))' "$work/pool-before.json")
    K -n "$ns" patch workerpool "$pool" --type merge -p "$patch"
    K -n "$ns" rollout status "deployment/$pool-deployment" --timeout=900s
    log "pool-rollback PASS: $(cat "$work/pool-before.json")" ;;
  so-diff)
    K apply --server-side --field-manager=ewallet-bprime -f "$so_file" --dry-run=server -o yaml > "$work/so-dry-run.yaml"
    echo "server dry run PASS ($(grep -c '' "$work/so-dry-run.yaml") lines): $(python3 -c 'import sys,yaml; d=yaml.safe_load(open(sys.argv[1])); s=d["spec"]; print(s["scaleTargetRef"], s["minReplicaCount"], s["maxReplicaCount"], s["fallback"], s["triggers"][0]["metadata"]["query"])' "$work/so-dry-run.yaml")" ;;
  so-apply)
    approval BPRIME-SO
    keda_watches || die "KEDA does not watch ewallet-sandbox (PKG-W first)"
    [[ "$(scale_selector)" == "$selector" ]] || die "WorkerPool /scale has no selector (PKG-P3 and pool-apply first)"
    K apply --server-side --field-manager=ewallet-bprime -f "$so_file" --dry-run=server > /dev/null
    K apply --server-side --field-manager=ewallet-bprime -f "$so_file"
    for _ in $(seq 1 40); do
      ready=$(K -n "$ns" get scaledobject "$so" -o 'jsonpath={.status.conditions[?(@.type=="Ready")].status}')
      [[ "$ready" == True ]] && K -n "$ns" get hpa "$so" > /dev/null 2>&1 && break
      sleep 3
    done
    [[ "$ready" == True ]] || die "ScaledObject not Ready: $(K -n "$ns" get scaledobject "$so" -o 'jsonpath={.status.conditions}')"
    log "so-apply PASS: ScaledObject Ready, HPA $(K -n "$ns" get hpa "$so" -o 'jsonpath={.spec.minReplicas}-{.spec.maxReplicas} current={.status.currentReplicas}')" ;;
  so-rollback)
    approval BPRIME-SO-ROLLBACK
    K -n "$ns" delete scaledobject "$so" --ignore-not-found
    for _ in $(seq 1 30); do K -n "$ns" get hpa "$so" > /dev/null 2>&1 || break; sleep 2; done
    K -n "$ns" patch workerpool "$pool" --type merge -p '{"spec":{"replicas":2}}'
    log "so-rollback PASS: ScaledObject and HPA removed; pool back to 2 workers" ;;
  *) sed -n '2,24p' "$0"; exit 2 ;;
esac
