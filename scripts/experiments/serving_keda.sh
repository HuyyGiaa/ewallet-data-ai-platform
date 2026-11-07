#!/usr/bin/env bash
# Measure one arm of A-01..A-04: fixed replicas or KEDA, for one serving API (package P-A3).
# usage: serving_keda.sh <plan|run> <fixed|keda> <feature-api|fraud-api> <evidence-dir>
#
# This script measures; it does not switch arms. The owner applies the approved change first
# and the run stops unless the cluster matches the arm. Profiles run low, medium, high with a
# rest between them, and a failed run stops the session. Nothing else may load the API.
# Env: KUBE_CONTEXT, EXP_APPROVED=yes, EWALLET_LOAD_ENTITIES (verified), LOAD_HOST,
#      IMAGE_DIGEST, EWALLET_LOAD_MODEL_VERSION, EXP_REST_SECONDS (180), EXP_FIXED_REPLICAS (1).
set -euo pipefail
action=${1:-} arm=${2:-} api=${3:-} evidence=${4:-}
die() { echo "serving_keda: $*" >&2; exit 1; }
case "$api" in feature-api) target=feature ;; fraud-api) target=inference ;; *) die "api must be feature-api or fraud-api" ;; esac
case "$arm" in fixed|keda) ;; *) die "arm must be fixed or keda" ;; esac
[ -n "$evidence" ] || die "evidence directory required"
NS=ml-platform EXPECTED=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
root=$(cd "$(dirname "$0")/../.." && pwd); python=${EXP_PYTHON:-python3}; export PYTHONPATH=$root${PYTHONPATH:+:$PYTHONPATH}
rest=${EXP_REST_SECONDS:-180} fixed=${EXP_FIXED_REPLICAS:-1}
if [ "$action" = plan ]; then
  echo "arm=$arm api=$api target=$target profiles=low,medium,high rest_seconds=$rest fixed_replicas=$fixed"
  exit 0
fi
[ "$action" = run ] || die "action must be plan or run"
[ "${EXP_APPROVED:-}" = yes ] || die "EXP_APPROVED=yes is required"
[ "${KUBE_CONTEXT:-}" = "$EXPECTED" ] || die "KUBE_CONTEXT must be $EXPECTED"
k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=30s "$@"; }
owner=$(k -n "$NS" get scaledobject "$api" --ignore-not-found -o name)
if [ "$arm" = fixed ]; then
  [ -z "$owner" ] || die "arm fixed needs no ScaledObject on $api"
  [ "$(k -n "$NS" get deployment "$api" -o 'jsonpath={.spec.replicas}')" = "$fixed" ] || die "$api must run $fixed replicas"
else
  [ -n "$owner" ] || die "arm keda needs the ScaledObject"
  [ "$(k -n "$NS" get "$owner" -o 'jsonpath={.status.conditions[?(@.type=="Ready")].status}')" = True ] || die "ScaledObject is not Ready"
fi
out=$evidence/$arm/$api; mkdir -p "$out"; sampler=
stop_sampler() { [ -z "$sampler" ] || kill "$sampler" 2>/dev/null || true; sampler=; }
trap stop_sampler EXIT
for profile in low medium high; do
  "$python" -m ops.experiments.replica_sampler --context "$KUBE_CONTEXT" --namespace "$NS" --deployment "$api" \
    --output "$out/$profile-replicas.jsonl" & sampler=$!
  k -n "$NS" get deployment "$api" -o json | jq '{replicas:.spec.replicas,image:.spec.template.spec.containers[0].image,resources:.spec.template.spec.containers[0].resources}' > "$out/$profile-before.json"
  "$root/scripts/testing/run_real_load.sh" "$target" internal "$profile" "$out"
  k -n "$NS" get hpa,scaledobject -o yaml > "$out/$profile-autoscalers.yaml" 2>/dev/null || true
  sleep "$rest"
  stop_sampler
done
"$python" -m ops.experiments.serving_compare --arm-dir "$out" --target "$target" --out "$out/arm-summary.json"
