#!/usr/bin/env bash
# R1: move ownership of the Fraud API's spec.replicas from kubectl to KEDA, safely.
#
# The Fraud API has always been applied client-side (kubectl apply). Client-side
# apply is a three-way merge against the last-applied annotation: a field that is
# in the annotation but missing from the new manifest is DELETED, and a Deployment
# without spec.replicas defaults to 1. Applying the keda manifest directly would
# therefore drop the Fraud API to one replica. The safe order is:
#
#   1. set-last-applied with the keda manifest (annotation only; live replicas untouched);
#   2. prove with a server dry-run that applying the keda manifest keeps the count;
#   3. apply the keda manifest;
#   4. install the ScaledObject (P-A3, Helm release ewallet-serving), then verify.
#
# Usage: scripts/serving/fraud_api_keda_handover.sh <readback|plan|handover|verify|rollback> <evidence-dir> [--execute]
# readback, plan and verify are read-only. handover and rollback change the cluster and
# refuse to run without --execute. Template values (FRAUD_API_IMAGE, ONLINE_REGISTRY_URI,
# MODEL_VERSION, GIT_SHA) must be exported for plan, handover and rollback.
set -euo pipefail

NS="${FRAUD_API_NAMESPACE:-ml-platform}"
NAME=fraud-api
KUBECTL="${KUBECTL:-kubectl}"
PYTHON="${PYTHON:-python3}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ $# -ge 2 ] || usage
action="$1"; evidence="$2"; execute="${3:-}"
mkdir -p "$evidence"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$evidence/handover.log" >&2; }
die() { log "STOP: $*"; exit 1; }
need_execute() { [ "$execute" = "--execute" ] || die "$action changes the cluster; rerun with --execute after owner approval"; }

live_replicas() { "$KUBECTL" -n "$NS" get deployment "$NAME" -o jsonpath='{.spec.replicas}'; }

render() {  # <mode> [replicas] -> manifest path
  local mode="$1" out="$evidence/fraud-api-$1.yaml"
  (cd "$ROOT" && "$PYTHON" -m ops.serving.fraud_api_manifest --mode "$mode" ${2:+--replicas "$2"}) >"$out"
  echo "$out"
}

readback() {
  "$KUBECTL" -n "$NS" get deployment "$NAME" -o json >"$evidence/deployment-before.json"
  "$PYTHON" - "$evidence/deployment-before.json" >"$evidence/ownership.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
annotation = d["metadata"].get("annotations", {}).get("kubectl.kubernetes.io/last-applied-configuration")
last = json.loads(annotation)["spec"].get("replicas") if annotation else None
owners = [
    {"manager": f.get("manager"), "operation": f.get("operation")}
    for f in d["metadata"].get("managedFields", [])
    if "f:replicas" in (f.get("fieldsV1", {}).get("f:spec") or {})
]
print(json.dumps({
    "spec_replicas": d["spec"].get("replicas"),
    "ready_replicas": d.get("status", {}).get("readyReplicas"),
    "last_applied_present": annotation is not None,
    "last_applied_replicas": last,
    "replicas_managers": owners,
}, indent=2))
PY
  "$KUBECTL" -n "$NS" get hpa -o json \
    | "$PYTHON" -c 'import json,sys; print(json.dumps([h["metadata"]["name"] for h in json.load(sys.stdin)["items"] if h["spec"]["scaleTargetRef"]["name"]=="fraud-api"]))' \
    >"$evidence/hpas-targeting-fraud-api.json"
  "$KUBECTL" -n "$NS" get scaledobjects.keda.sh -o name >"$evidence/scaledobjects.txt" 2>/dev/null || echo "keda CRD absent" >"$evidence/scaledobjects.txt"
  log "readback: replicas=$(live_replicas) hpas=$(cat "$evidence/hpas-targeting-fraud-api.json")"
}

deployment_only() {  # <manifest> -> path of a file holding only its Deployment
  local out="${1%.yaml}.deployment.yaml"
  "$PYTHON" -c 'import sys; docs=open(sys.argv[1]).read().split("\n---\n"); sys.stdout.write(next(d for d in docs if "\nkind: Deployment" in "\n"+d))' "$1" >"$out"
  echo "$out"
}

dry_run_replicas() {  # <manifest> -> replicas the API server would store for the Deployment
  "$KUBECTL" apply --dry-run=server -f "$(deployment_only "$1")" -o jsonpath='{.spec.replicas}'
}

case "$action" in
  readback) readback ;;
  plan)
    readback
    keda="$(render keda)"
    "$KUBECTL" diff -f "$keda" >"$evidence/diff-keda.txt" 2>&1 || true
    before="$(live_replicas)"
    after="$(dry_run_replicas "$keda")"
    log "plan: a plain apply of the keda manifest would store replicas='${after:-<default 1>}' (live $before)"
    log "plan: handover step 1 (set-last-applied) must run first; see $evidence/diff-keda.txt"
    ;;
  handover)
    need_execute
    readback
    before="$(live_replicas)"
    [ -n "$before" ] || die "the live Deployment has no spec.replicas; read it back by hand first"
    [ "$(cat "$evidence/hpas-targeting-fraud-api.json")" = "[]" ] || die "an HPA already targets $NAME; resolve ownership first"
    keda="$(render keda)"
    "$KUBECTL" apply set-last-applied -f "$keda"
    [ "$(live_replicas)" = "$before" ] || die "replicas changed during set-last-applied"
    projected="$(dry_run_replicas "$keda")"
    if [ "$projected" != "$before" ]; then
      "$KUBECTL" apply set-last-applied -f "$(render fixed "$before")"
      die "server dry-run would store replicas='$projected' instead of $before; annotation restored"
    fi
    "$KUBECTL" apply -f "$keda"
    [ "$(live_replicas)" = "$before" ] || die "replicas changed on apply; run rollback"
    log "handover: kubectl no longer owns replicas ($before kept). Install the ScaledObject (P-A3), then run verify."
    ;;
  verify)
    readback
    so_ready="$("$KUBECTL" -n "$NS" get scaledobjects.keda.sh "$NAME" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')"
    [ "$so_ready" = "True" ] || die "ScaledObject $NAME is not Ready"
    hpas="$(cat "$evidence/hpas-targeting-fraud-api.json")"
    [ "$hpas" = '["keda-hpa-fraud-api"]' ] || die "expected only keda-hpa-fraud-api to target $NAME, found $hpas"
    "$PYTHON" -c 'import json,sys; o=json.load(open(sys.argv[1])); sys.exit(0 if o["last_applied_replicas"] is None else 1)' \
      "$evidence/ownership.json" || die "the last-applied annotation still carries replicas"
    log "verify: KEDA owns replicas; Deployment at $(live_replicas)"
    ;;
  rollback)
    need_execute
    current="$(live_replicas)"
    if "$KUBECTL" -n "$NS" get scaledobjects.keda.sh "$NAME" >/dev/null 2>&1; then
      die "delete the ScaledObject first (helm upgrade ewallet-serving with autoscaling.enabled=false), then rerun"
    fi
    "$KUBECTL" apply -f "$(render fixed "$current")"
    [ "$(live_replicas)" = "$current" ] || die "replicas changed on rollback apply"
    log "rollback: kubectl owns replicas again at $current"
    ;;
  *) usage ;;
esac
