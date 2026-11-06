#!/usr/bin/env bash
# Owner-run ML A/B on GKE (package P8). Every action but `plan` needs EXP_APPROVED=yes.
# usage: ml_ab.sh plan
#        ml_ab.sh deploy                  candidate InferenceService, then the router at 100/0
#        ml_ab.sh split <split-90-10|split-50-50|rollback-100-0>
#        ml_ab.sh traffic <entities.jsonl> <split-name> <evidence-dir>   needs a port-forward
#        ml_ab.sh remove                  router off, candidate deleted
# Env: KUBE_CONTEXT (must be the ewallet-dev context), CANDIDATE_IMAGE (digest),
#      CANDIDATE_MODEL_VERSION (default 2), FRAUD_API_URL (default http://127.0.0.1:18080).
set -euo pipefail
action=${1:-plan}
root=$(cd "$(dirname "$0")/../.." && pwd)
EXPECTED=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
configs=$root/configs/experiments/ml-ab serving=$root/infra/kubernetes/fraud-serving
die() { echo "ml_ab: $*" >&2; exit 1; }
k() { kubectl --context "$KUBE_CONTEXT" "$@"; }
export CANDIDATE_MODEL_VERSION=${CANDIDATE_MODEL_VERSION:-2}

render_candidate() {
  [[ "${CANDIDATE_IMAGE:-}" == *@sha256:* ]] || die "CANDIDATE_IMAGE must be pinned by digest"
  envsubst '${CANDIDATE_IMAGE} ${CANDIDATE_MODEL_VERSION}' < "$serving/inferenceservice-candidate.yaml.tmpl"
}
apply_split() {
  [ -f "$configs/$1.json" ] || die "unknown split $1"
  k -n ml-platform create configmap fraud-api-ab --from-file=experiment.json="$configs/$1.json" \
    --dry-run=client -o yaml | k apply -f -
  k -n ml-platform rollout restart deployment/fraud-api
  k -n ml-platform rollout status deployment/fraud-api --timeout=300s
}
if [ "$action" = plan ]; then
  echo "ml_ab plan: no cluster access"
  ls "$configs"
  CANDIDATE_IMAGE=${CANDIDATE_IMAGE:-example.invalid/fraud@sha256:0000} render_candidate
  exit 0
fi
[ "${EXP_APPROVED:-}" = yes ] || die "EXP_APPROVED=yes is required"
[ "${KUBE_CONTEXT:-}" = "$EXPECTED" ] || die "KUBE_CONTEXT must be $EXPECTED"
case "$action" in
  deploy)
    render_candidate | k apply -f -
    k apply -f "$serving/network-policy-candidate.yaml"
    k -n fraud-serving wait --for=condition=Ready inferenceservice/fraud-detector-v2 --timeout=600s
    k -n ml-platform create configmap fraud-api-ab \
      --from-file=experiment.json="$configs/rollback-100-0.json" --dry-run=client -o yaml | k apply -f -
    k -n ml-platform patch deployment fraud-api --type=strategic --patch-file "$serving/fraud-api-ab.patch.yaml"
    k -n ml-platform rollout status deployment/fraud-api --timeout=300s
    ;;
  split) apply_split "${2:?split name required}" ;;
  traffic)
    entities=${2:?entities file required} name=${3:?split name required} evidence=${4:?evidence dir required}
    PYTHONPATH=$root python3 -m ops.experiments.ml_ab_traffic \
      --url "${FRAUD_API_URL:-http://127.0.0.1:18080}/v1/fraud/predict" \
      --entities "$entities" --config "$configs/$name.json" --out "$evidence/$name"
    ;;
  remove)
    k -n ml-platform rollout undo deployment/fraud-api
    k -n ml-platform rollout status deployment/fraud-api --timeout=300s
    k -n ml-platform delete configmap fraud-api-ab --ignore-not-found
    k -n fraud-serving delete inferenceservice fraud-detector-v2 --ignore-not-found
    k delete -f "$serving/network-policy-candidate.yaml" --ignore-not-found
    ;;
  *) die "unknown action $action" ;;
esac
