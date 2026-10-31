#!/usr/bin/env bash
# G1 on GKE ewallet-dev, one stage per call (docs/llm_agent/gke_cpu_runtime.md).
# usage: g1_operator.sh <stage> <evidence-dir>
#
# Env: KUBE_CONTEXT (must be the ewallet-dev context), KUBECONFIG as usual.
# A stage that changes GKE or Artifact Registry runs only when its name is in
# G1_APPROVED_STAGES (comma-separated, the owner's approval). cleanup always
# runs. Every stage reads its result back and exits non-zero otherwise.
#
# read-only : status, preflight, netpol-verify, smoke-direct, smoke-gateway, evidence
# mutating  : netpol-enable, scale-up, istio, image-push, namespace, secret, deploy, failover
# always    : cleanup [--uninstall]
#
# Stage env: image-build/image-push: MODEL_DIR, AR_REPO, DIGESTS (json path);
# deploy/preflight: DIGESTS; scale-up: NODES (1-3, default 3).
set -euo pipefail
stage=${1:-} evidence=${2:-}
PROJECT=ewallet-data-ai-platform-hg26 ZONE=asia-southeast1-b CLUSTER=ewallet-dev POOL=cpu-pool NS=ewallet-agents
EXPECTED_CONTEXT="gke_${PROJECT}_${ZONE}_${CLUSTER}"
MUTATING=" netpol-enable scale-up istio image-push namespace secret deploy failover "
TEST_NS=g1-netpol-probe
# curlimages/curl 8.17.0 (multi-arch index digest), only used by netpol-verify.
CURL_IMAGE=docker.io/curlimages/curl@sha256:935d9100e9ba842cdb060de42472c7ca90cfe9a7c96e4dacb55e79e560b3ff40

die() { echo "g1_operator: $*" >&2; exit 1; }
[ -n "$stage" ] && [ -n "$evidence" ] || die "usage: g1_operator.sh <stage> <evidence-dir>"
[ "${KUBE_CONTEXT:-}" = "$EXPECTED_CONTEXT" ] || die "KUBE_CONTEXT must be $EXPECTED_CONTEXT"
if [[ "$MUTATING" == *" $stage "* ]] && [[ ",${G1_APPROVED_STAGES:-}," != *",$stage,"* ]]; then
  die "stage $stage changes the cluster or registry; add it to G1_APPROVED_STAGES after owner approval"
fi
root=$(cd "$(dirname "$0")/../.." && pwd)
chart="$root/infra/helm/llm-agent-platform"
mkdir -p "$evidence"
k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=60s "$@"; }
gc() { gcloud container clusters "$@" "$CLUSTER" --zone "$ZONE" --project "$PROJECT"; }
record() { printf '%s\n' "$*" | tee -a "$evidence/operator.log"; }
pool_size() {
  gcloud compute instance-groups managed list --project "$PROJECT" --filter="name~^gke-${CLUSTER}-${POOL}-" \
    --format='value(targetSize)' | awk '{s+=$1} END {print s+0}'
}
netpol_enabled() {
  gc describe --format='value(networkPolicy.enabled,addonsConfig.networkPolicyConfig.disabled)' \
    | awk '{exit !($1 == "True" && $2 != "True")}'
}
api_key() { k -n "$NS" get secret vllm-api-key -o jsonpath='{.data.api-key}' | base64 -d; }
port_forward() {  # <target> <local:remote>; echoes the pid
  k -n "$NS" port-forward "$1" "$2" >/dev/null 2>&1 &
  echo $!
}
record "$(date -u +%FT%TZ) stage=$stage git=$(git -C "$root" rev-parse HEAD)"

case "$stage" in
status)
  gc describe --format=json > "$evidence/cluster.json"
  echo "pool_size=$(pool_size)" | tee "$evidence/pool_size.txt"
  k get nodes -o wide > "$evidence/nodes.txt" || true
  ;;

netpol-enable)
  # Both updates recreate nodes; only allowed while the pool is empty.
  [ "$(pool_size)" = 0 ] || die "resize $POOL to 0 before enabling NetworkPolicy"
  gc update --update-addons=NetworkPolicy=ENABLED --quiet
  gc update --enable-network-policy --quiet
  netpol_enabled || die "readback: NetworkPolicy is not enabled"
  gc describe --format='json(networkPolicy,addonsConfig.networkPolicyConfig)' > "$evidence/networkpolicy.json"
  ;;

scale-up)
  nodes=${NODES:-3}
  [[ "$nodes" =~ ^[1-3]$ ]] || die "NODES must be 1-3 (G1 ceiling)"
  netpol_enabled || die "NetworkPolicy enforcement must be enabled before nodes exist"
  gcloud container clusters resize "$CLUSTER" --node-pool "$POOL" --num-nodes "$nodes" \
    --zone "$ZONE" --project "$PROJECT" --quiet
  k wait --for=condition=Ready nodes --all --timeout=15m
  [ "$(k get nodes --no-headers | grep -c ' Ready')" -ge "$nodes" ] || die "readback: fewer than $nodes Ready nodes"
  k -n kube-system rollout status ds/calico-node --timeout=10m
  k get nodes -o wide > "$evidence/nodes.txt"
  k get nodes -o json > "$evidence/nodes.json"
  ;;

istio)
  KUBE_CONTEXT="$KUBE_CONTEXT" "$root/scripts/llm_agent/istio_ambient_gke.sh" apply "$evidence/istio"
  ;;

image-build)  # local only: builds, never pushes
  : "${MODEL_DIR:?MODEL_DIR required}" "${AR_REPO:?AR_REPO required}" "${DIGESTS:?DIGESTS required}"
  "$root/scripts/llm_agent/build_vllm_model_image.sh" "$MODEL_DIR" "$AR_REPO" "$(git -C "$root" rev-parse HEAD)" "$DIGESTS"
  ;;

image-push)  # Trivy HIGH/CRITICAL gate first; a finding stops the push
  : "${DIGESTS:?DIGESTS required}"
  "$root/scripts/llm_agent/scan_and_push.sh" "$DIGESTS"
  cp "$DIGESTS" "$evidence/image-digests.json"
  ;;

namespace)
  # The chart's own Namespace (ambient + Pod Security labels); the release then
  # runs with namespace.create=false (values-gke-cpu-g1.yaml).
  helm template llm-agent "$chart" -f "$chart/values-gke-cpu.yaml" --show-only templates/namespace.yaml \
    --set vllm.image=placeholder.invalid/x@sha256:0000000000000000000000000000000000000000000000000000000000000000 \
    > "$evidence/namespace.yaml"
  k apply -f "$evidence/namespace.yaml"
  [ "$(k get ns "$NS" -o jsonpath='{.metadata.labels.istio\.io/dataplane-mode}')" = ambient ] \
    || die "readback: $NS is not enrolled in ambient"
  ;;

secret)
  # A fresh token, never printed, never on a command line; an existing Secret is kept.
  if k -n "$NS" get secret vllm-api-key >/dev/null 2>&1; then die "Secret vllm-api-key exists; not overwriting"; fi
  token_file=$(mktemp)
  trap 'rm -f "$token_file"' EXIT
  chmod 600 "$token_file"
  openssl rand -hex 32 | tr -d '\n' > "$token_file"
  k -n "$NS" create secret generic vllm-api-key --from-file=api-key="$token_file" >/dev/null
  [ "$(api_key | wc -c)" -eq 64 ] || die "readback: Secret has no 64-character api-key"
  echo "secret/vllm-api-key created (value not recorded)" | tee "$evidence/secret.txt"
  ;;

preflight)
  : "${DIGESTS:?DIGESTS required}"
  image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["images"]["vllm-cpu-model"]["digest_ref"] or "")' "$DIGESTS")
  [ -n "$image" ] || die "no pushed vllm-cpu-model digest in $DIGESTS"
  (cd "$root" && python3 -m ops.llm_agent.gke_preflight --stage g1 --context "$KUBE_CONTEXT" \
     --set "vllm.image=$image" --json-output "$evidence/preflight_g1.json")
  ;;

deploy)
  : "${DIGESTS:?DIGESTS required}"
  "$0" preflight "$evidence"  # GO required
  EXTRA_VALUES="$chart/values-gke-cpu-g1.yaml" KUBE_CONTEXT="$KUBE_CONTEXT" \
    "$root/scripts/llm_agent/deploy_atomic.sh" gke-cpu "$DIGESTS" "$evidence/deploy"
  k -n "$NS" get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[0].imageID}{"\n"}{end}' \
    > "$evidence/image-ids.txt"
  ;;

netpol-verify)
  # Allow/deny matrix with throwaway unprivileged pods; every row must match.
  k create namespace "$TEST_NS" --dry-run=client -o yaml | k apply -f - >/dev/null
  results="$evidence/netpol-verify.tsv"
  : > "$results"
  probe() {  # <name> <namespace> <labels> <expect allow|deny> <curl args...>
    local name=$1 ns=$2 labels=$3 expect=$4; shift 4
    local rc=0
    k -n "$ns" run "np-$name" --image="$CURL_IMAGE" --restart=Never --labels="$labels" --rm -i --quiet \
      --overrides='{"spec":{"automountServiceAccountToken":false,"securityContext":{"runAsNonRoot":true,"seccompProfile":{"type":"RuntimeDefault"}}}}' \
      --command -- curl -sS -o /dev/null --max-time 8 "$@" >/dev/null 2>&1 || rc=$?
    local got=deny
    [ "$rc" -eq 0 ] && got=allow
    # curl exit 6 = name not resolved: DNS itself is broken, never a valid deny.
    [ "$rc" -eq 6 ] && got=dns-failure
    printf '%s\t%s\t%s\t%s\n' "$name" "$expect" "$got" "$*" | tee -a "$results"
  }
  vllm="http://vllm-primary.$NS.svc.cluster.local:8000/health"
  gw="http://agentgateway.$NS.svc.cluster.local:4000/"
  probe internet-egress "$NS" g1-test=unlabelled deny https://huggingface.co
  probe unlabelled-to-vllm "$NS" g1-test=unlabelled deny "$vllm"
  probe unlabelled-to-gateway "$NS" g1-test=unlabelled deny "$gw"
  probe other-ns-to-vllm "$TEST_NS" g1-test=other-ns deny "$vllm"
  probe gateway-label-wrong-sa "$NS" app.kubernetes.io/name=agentgateway deny "$vllm"  # NetworkPolicy allows; Istio denies the SA
  if k -n "$NS" exec deploy/vllm-primary -- python3 -c \
      'import urllib.request; urllib.request.urlopen("https://huggingface.co", timeout=8)' >/dev/null 2>&1; then
    printf 'vllm-internet\tdeny\tallow\n' | tee -a "$results"
  else
    printf 'vllm-internet\tdeny\tdeny\n' | tee -a "$results"
  fi
  k -n "$NS" get pods -l app.kubernetes.io/name=vllm \
    -o jsonpath='{range .items[*]}{.metadata.name}{" ready="}{.status.containerStatuses[0].ready}{"\n"}{end}' \
    | tee "$evidence/vllm-ready.txt"
  grep -q 'ready=false' "$evidence/vllm-ready.txt" && printf 'kubelet-probes\tallow\tdeny\n' | tee -a "$results"
  k get networkpolicy -A -o yaml > "$evidence/networkpolicies.yaml"
  k delete namespace "$TEST_NS" --wait=false >/dev/null
  awk -F'\t' '$2 != $3 {bad=1} END {exit bad}' "$results" || die "allow/deny matrix mismatch (see $results)"
  ;;

smoke-direct | smoke-gateway)
  if [ "$stage" = smoke-direct ]; then target=svc/vllm-primary ports=18000:8000 model=ewallet-fraud-llm-v1
  else target=svc/agentgateway ports=13000:3000 model=ewallet-fraud-llm; fi
  pf=$(port_forward "$target" "$ports")
  trap 'kill "$pf" 2>/dev/null || true' EXIT
  sleep 3
  endpoint="http://127.0.0.1:${ports%%:*}"
  EWALLET_VLLM_API_KEY=$(api_key) python3 "$root/scripts/llm_agent/smoke_tool_call.py" --endpoint "$endpoint" \
    --model "$model" --evidence "$evidence/$stage-non-stream.json"
  EWALLET_VLLM_API_KEY=$(api_key) python3 "$root/scripts/llm_agent/smoke_tool_call.py" --endpoint "$endpoint" \
    --model "$model" --stream --evidence "$evidence/$stage-stream.json"
  code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer wrong-key' "$endpoint/v1/models")
  echo "wrong-key status $code" | tee "$evidence/$stage-wrong-key.txt"
  [ "$code" = 401 ] || die "wrong key was not rejected with 401"
  ;;

failover)
  k -n "$NS" scale deploy/vllm-primary --replicas=0
  trap 'k -n "$NS" scale deploy/vllm-primary --replicas=1 >/dev/null' EXIT
  k -n "$NS" wait --for=delete pod -l app.kubernetes.io/name=vllm,app.kubernetes.io/instance=primary --timeout=5m || true
  "$0" smoke-gateway "$evidence/failover"
  python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); sys.exit(r["first_turn"].get("backend") != "fallback")' \
    "$evidence/failover/smoke-gateway-non-stream.json" || die "request was not served by the fallback"
  ;;

evidence)
  k get nodes -o wide > "$evidence/nodes.txt"
  k -n "$NS" get deploy,pods,svc,networkpolicy,authorizationpolicy -o wide > "$evidence/workloads.txt"
  k -n "$NS" get pods -o json > "$evidence/pods.json"
  k top nodes > "$evidence/top-nodes.txt" 2>&1 || true
  k -n "$NS" top pods > "$evidence/top-pods.txt" 2>&1 || true
  helm history llm-agent --kube-context "$KUBE_CONTEXT" --namespace default -o json > "$evidence/helm-history.json"
  k -n "$NS" logs deploy/vllm-primary --tail=400 | grep -viE 'authorization|bearer|api[-_]key' > "$evidence/vllm-primary.log" || true
  echo "pool_size=$(pool_size)" > "$evidence/pool_size.txt"
  ;;

cleanup)
  # Operator-controlled scale-down; also runs after any failed stage.
  k delete namespace "$TEST_NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  if [ "${3:-}" = --uninstall ]; then
    helm uninstall llm-agent --kube-context "$KUBE_CONTEXT" --namespace default || true
  fi
  gcloud container clusters resize "$CLUSTER" --node-pool "$POOL" --num-nodes 0 \
    --zone "$ZONE" --project "$PROJECT" --quiet
  [ "$(pool_size)" = 0 ] || die "readback: $POOL is not at 0 nodes"
  echo "pool_size=0 $(date -u +%FT%TZ)" | tee "$evidence/cleanup.txt"
  ;;

*)
  die "unknown stage $stage"
  ;;
esac
record "$(date -u +%FT%TZ) stage=$stage ok"
