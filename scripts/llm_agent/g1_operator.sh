#!/usr/bin/env bash
# G1 on GKE ewallet-dev (docs/llm_agent/gke_cpu_runtime.md).
# usage: g1_operator.sh <stage> <evidence-dir> [--uninstall]
#
# Env: KUBE_CONTEXT (must be the ewallet-dev context; its kubeconfig server must
# be one of the cluster's endpoints), KUBECONFIG as usual.
# G1_APPROVED_STAGES: comma-separated stages the owner approved; a stage that
#   changes GKE, Artifact Registry or cluster objects runs only when listed, and
#   an unknown name in the list is an error.
# Each mutating stage runs at most once per evidence dir (state/); a retry needs
#   G1_RERUN=<stage> (an explicit, separate decision).
# cleanup always runs (no approval) and reads back 0 nodes.
# session runs the node-bound stages in order inside a time box
#   (G1_SESSION_MINUTES, default 180, max 240) and scales to 0 on success,
#   failure, Ctrl-C, SIGTERM or SIGHUP. Host loss: see "Recovery" in the runbook.
#
# Stage env: image-build: MODEL_DIR, AR_REPO, DIGESTS; image-push, deploy,
# session: DIGESTS; scale-up: NODES (1-3, default 3).
set -euo pipefail
stage=${1:-} evidence=${2:-}
PROJECT=ewallet-data-ai-platform-hg26 ZONE=asia-southeast1-b CLUSTER=ewallet-dev POOL=cpu-pool NS=ewallet-agents
EXPECTED_CONTEXT="gke_${PROJECT}_${ZONE}_${CLUSTER}"
READ_ONLY="status preflight smoke-direct smoke-gateway benchmark evidence image-build"
MUTATING="netpol-enable image-push scale-up istio namespace secret deploy netpol-verify failover"
SESSION_STAGES="scale-up istio namespace secret deploy netpol-verify smoke-direct smoke-gateway failover benchmark evidence"
TEST_NS=g1-netpol-probe
# curlimages/curl 8.17.0 (multi-arch index digest), only used by netpol-verify.
CURL_IMAGE=docker.io/curlimages/curl@sha256:935d9100e9ba842cdb060de42472c7ca90cfe9a7c96e4dacb55e79e560b3ff40

die() { echo "g1_operator: $*" >&2; exit 1; }
contains() { [[ " $1 " == *" $2 "* ]]; }
[ -n "$stage" ] && [ -n "$evidence" ] || die "usage: g1_operator.sh <stage> <evidence-dir>"
contains "$READ_ONLY $MUTATING cleanup session" "$stage" || die "unknown stage $stage"
[ "${KUBE_CONTEXT:-}" = "$EXPECTED_CONTEXT" ] || die "KUBE_CONTEXT must be $EXPECTED_CONTEXT"
approvals=${G1_APPROVED_STAGES:-}
for approved in ${approvals//,/ }; do
  contains "$MUTATING" "$approved" || die "G1_APPROVED_STAGES has $approved, which is not a mutating stage"
done
approved() { [[ ",${G1_APPROVED_STAGES:-}," == *",$1,"* ]]; }
if contains "$MUTATING" "$stage" && ! approved "$stage"; then
  die "stage $stage changes the cluster or registry; add it to G1_APPROVED_STAGES after owner approval"
fi
root=$(cd "$(dirname "$0")/../.." && pwd)
chart="$root/infra/helm/llm-agent-platform"
mkdir -p "$evidence/state"
k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=60s "$@"; }
gc() { gcloud container clusters "$@" "$CLUSTER" --zone "$ZONE" --project "$PROJECT"; }
record() { printf '%s\n' "$*" | tee -a "$evidence/operator.log"; }
pool_size() {
  gcloud compute instance-groups managed list --project "$PROJECT" --filter="name~^gke-${CLUSTER}-${POOL}-" \
    --format='value(targetSize)' | awk '{s+=$1} END {print s+0}'
}
pool_instances() {
  gcloud compute instances list --project "$PROJECT" --filter="name~^gke-${CLUSTER}-${POOL}-" \
    --format='value(name)' | grep -c . || true
}
netpol_enabled() {
  gc describe --format='value(networkPolicy.enabled,addonsConfig.networkPolicyConfig.disabled)' \
    | awk '{exit !($1 == "True" && $2 != "True")}'
}
verify_cluster() {  # the context must point at this cluster, not just carry its name
  local server endpoints
  server=$(kubectl config view --minify --context "$KUBE_CONTEXT" -o jsonpath='{.clusters[0].cluster.server}')
  endpoints=$(gc describe --format='value(endpoint,controlPlaneEndpointsConfig.dnsEndpointConfig.endpoint)')
  server=${server#https://}
  contains "$(echo "$endpoints" | tr '\t;' '  ')" "${server%%:*}" || die "kubeconfig server $server is not $CLUSTER"
}
api_key() { k -n "$NS" get secret vllm-api-key -o jsonpath='{.data.api-key}' | base64 -d; }
port_forward() {  # <target> <local:remote>; echoes the pid
  k -n "$NS" port-forward "$1" "$2" >/dev/null 2>&1 &
  echo $!
}
cleanup() {  # idempotent; also the session's exit/failure/interrupt handler
  k delete namespace "$TEST_NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  if [ "${1:-}" = --uninstall ]; then
    helm uninstall llm-agent --kube-context "$KUBE_CONTEXT" --namespace default || true
  fi
  gcloud container clusters resize "$CLUSTER" --node-pool "$POOL" --num-nodes 0 \
    --zone "$ZONE" --project "$PROJECT" --quiet
  [ "$(pool_size)" = 0 ] && [ "$(pool_instances)" = 0 ] || die "readback: $POOL still has nodes"
  echo "pool_size=0 instances=0 $(date -u +%FT%TZ)" | tee -a "$evidence/cleanup.txt"
}

# cleanup must work even when the kubeconfig does not (it only needs gcloud).
contains "image-build image-push cleanup" "$stage" || verify_cluster
if contains "$MUTATING" "$stage"; then
  if [ -e "$evidence/state/$stage.attempted" ] && [[ ",${G1_RERUN:-}," != *",$stage,"* ]]; then
    die "stage $stage already ran in $evidence (state/); a retry needs G1_RERUN=$stage"
  fi
  touch "$evidence/state/$stage.attempted"
fi
record "$(date -u +%FT%TZ) stage=$stage git=$(git -C "$root" rev-parse HEAD)"

case "$stage" in
status)
  gc describe --format=json > "$evidence/cluster.json"
  echo "pool_size=$(pool_size) instances=$(pool_instances)" | tee "$evidence/pool_size.txt"
  ;;

netpol-enable)
  # Both updates recreate nodes; only allowed while the pool is empty.
  [ "$(pool_size)" = 0 ] || die "resize $POOL to 0 before enabling NetworkPolicy"
  gc update --update-addons=NetworkPolicy=ENABLED --quiet
  gc update --enable-network-policy --quiet
  netpol_enabled || die "readback: NetworkPolicy is not enabled"
  gc describe --format='json(networkPolicy,addonsConfig.networkPolicyConfig)' > "$evidence/networkpolicy.json"
  ;;

image-build)  # local only: uv contract, checksums, build; never pushes
  : "${MODEL_DIR:?MODEL_DIR required}" "${AR_REPO:?AR_REPO required}" "${DIGESTS:?DIGESTS required}"
  "$root/scripts/llm_agent/build_vllm_model_image.sh" "$MODEL_DIR" "$AR_REPO" "$(git -C "$root" rev-parse HEAD)" "$DIGESTS"
  ;;

image-push)  # Trivy vuln + secret gate on the final image first; a finding stops the push
  : "${DIGESTS:?DIGESTS required}"
  "$root/scripts/llm_agent/scan_and_push.sh" "$DIGESTS"
  cp "$DIGESTS" "$evidence/image-digests.json"
  ;;

scale-up)
  nodes=${NODES:-3}
  [[ "$nodes" =~ ^[1-3]$ ]] || die "NODES must be 1-3 (G1 ceiling)"
  netpol_enabled || die "NetworkPolicy enforcement must be enabled before nodes exist"
  gcloud container clusters resize "$CLUSTER" --node-pool "$POOL" --num-nodes "$nodes" \
    --zone "$ZONE" --project "$PROJECT" --quiet
  [ "$(pool_size)" = "$nodes" ] || die "readback: pool target size is not $nodes"
  k wait --for=condition=Ready nodes -l "cloud.google.com/gke-nodepool=$POOL" --timeout=15m
  ready=$(k get nodes -l "cloud.google.com/gke-nodepool=$POOL" --no-headers | grep -c ' Ready ' || true)
  [ "$ready" = "$nodes" ] || die "readback: $ready Ready nodes, expected exactly $nodes"
  k -n kube-system rollout status ds/calico-node --timeout=10m
  k get nodes -o wide > "$evidence/nodes.txt"
  k get nodes -o json > "$evidence/nodes.json"
  ;;

istio)
  KUBE_CONTEXT="$KUBE_CONTEXT" "$root/scripts/llm_agent/istio_ambient_gke.sh" apply "$evidence/istio"
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
  # A fresh token, never printed, never on a command line; an existing Secret is kept as is.
  if k -n "$NS" get secret vllm-api-key >/dev/null 2>&1; then
    echo "secret/vllm-api-key exists: kept, not overwritten" | tee "$evidence/secret.txt"
  else
    token_file=$(mktemp)
    trap 'rm -f "$token_file"' EXIT
    chmod 600 "$token_file"
    openssl rand -hex 32 | tr -d '\n' > "$token_file"
    k -n "$NS" create secret generic vllm-api-key --from-file=api-key="$token_file" >/dev/null
    echo "secret/vllm-api-key created (value not recorded)" | tee "$evidence/secret.txt"
  fi
  [ "$(api_key | wc -c)" -eq 64 ] || die "readback: Secret has no 64-character api-key"
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
  # Throwaway unprivileged pods (a cluster mutation, hence approved); every row must match.
  k create namespace "$TEST_NS" --dry-run=client -o yaml | k apply -f - >/dev/null
  trap 'k delete namespace "$TEST_NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true' EXIT
  results="$evidence/netpol-verify.tsv"
  : > "$results"
  probe() {  # <name> <namespace> <labels> <expect allow|deny> <curl args...>
    local name=$1 ns=$2 labels=$3 expect=$4; shift 4
    local rc=0
    k -n "$ns" run "np-$name" --image="$CURL_IMAGE" --restart=Never --labels="$labels" --rm -i --quiet \
      --pod-running-timeout=3m \
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
  if grep -q 'ready=false' "$evidence/vllm-ready.txt" || [ ! -s "$evidence/vllm-ready.txt" ]; then
    printf 'kubelet-probes\tallow\tdeny\n' | tee -a "$results"
  fi
  k get networkpolicy -A -o yaml > "$evidence/networkpolicies.yaml"
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
  # PASS only if a Ready fallback exists, the primary is really gone and the
  # fallback pod itself counted the request (vLLM metric), besides the header.
  [ "$(k -n "$NS" get deploy vllm-fallback -o jsonpath='{.status.readyReplicas}')" -ge 1 ] 2>/dev/null \
    || die "no Ready vllm-fallback: failover cannot be tested"
  fallback_requests() {
    local pf
    pf=$(port_forward svc/vllm-fallback 18001:8000)
    sleep 3
    curl -s http://127.0.0.1:18001/metrics | awk '/^vllm:request_success_total/ {s+=$2} END {print s+0}'
    kill "$pf" 2>/dev/null || true
  }
  before=$(fallback_requests)
  k -n "$NS" scale deploy/vllm-primary --replicas=0
  trap 'k -n "$NS" scale deploy/vllm-primary --replicas=1 >/dev/null && k -n "$NS" rollout status deploy/vllm-primary --timeout=20m >/dev/null' EXIT
  k -n "$NS" wait --for=delete pod -l app.kubernetes.io/name=vllm,app.kubernetes.io/instance=primary --timeout=5m
  [ "$(k -n "$NS" get pods -l app.kubernetes.io/instance=primary --no-headers 2>/dev/null | grep -c . || true)" = 0 ] \
    || die "primary pods still present"
  G1_APPROVED_STAGES="" "$0" smoke-gateway "$evidence/failover"
  after=$(fallback_requests)
  python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); sys.exit(r["first_turn"].get("backend") != "fallback")' \
    "$evidence/failover/smoke-gateway-non-stream.json" || die "response did not come from the fallback route"
  [ "$after" -gt "$before" ] 2>/dev/null || die "fallback pod counted no request ($before -> $after)"
  echo "fallback vllm:request_success_total $before -> $after" | tee "$evidence/failover/fallback-metric.txt"
  ;;

benchmark)
  pf=$(port_forward svc/agentgateway 13000:3000)
  trap 'kill "$pf" 2>/dev/null || true' EXIT
  sleep 3
  samples="$evidence/benchmark-resources.jsonl"
  sample() {  # sum of vLLM pod usage, one JSON line {cpu_millicores, memory_mib}
    k -n "$NS" top pods -l app.kubernetes.io/name=vllm --no-headers \
      | awk '{gsub("m","",$2); gsub("Mi","",$3); c+=$2; m+=$3} END {printf "{\"cpu_millicores\": %d, \"memory_mib\": %d}\n", c, m}'
  }
  sample > "$samples"
  EWALLET_VLLM_API_KEY=$(api_key) timeout 45m python3 "$root/scripts/llm_agent/g1_benchmark.py" \
    --endpoint http://127.0.0.1:13000 --model ewallet-fraud-llm --requests "${BENCH_REQUESTS:-8}" \
    --concurrency "${BENCH_CONCURRENCY:-1}" --resources "$samples" --output "$evidence/benchmark.json" &
  bench=$!
  while kill -0 "$bench" 2>/dev/null; do sleep 20; sample >> "$samples" || true; done
  wait "$bench"
  ;;

evidence)
  k get nodes -o wide > "$evidence/nodes.txt"
  k -n "$NS" get deploy,pods,svc,networkpolicy,authorizationpolicy -o wide > "$evidence/workloads.txt"
  k -n "$NS" get pods -o json > "$evidence/pods.json"
  k top nodes > "$evidence/top-nodes.txt" 2>&1 || true
  k -n "$NS" top pods > "$evidence/top-pods.txt" 2>&1 || true
  helm history llm-agent --kube-context "$KUBE_CONTEXT" --namespace default -o json > "$evidence/helm-history.json"
  k -n "$NS" logs deploy/vllm-primary --tail=400 | grep -viE 'authorization|bearer|api[-_]key' > "$evidence/vllm-primary.log" || true
  echo "pool_size=$(pool_size) instances=$(pool_instances)" > "$evidence/pool_size.txt"
  ;;

cleanup)
  cleanup "${3:-}"
  ;;

session)
  minutes=${G1_SESSION_MINUTES:-180}
  [[ "$minutes" =~ ^[0-9]+$ ]] && [ "$minutes" -ge 30 ] && [ "$minutes" -le 240 ] \
    || die "G1_SESSION_MINUTES must be 30-240"
  for needed in $SESSION_STAGES; do
    contains "$MUTATING" "$needed" && ! approved "$needed" && die "session needs approval for $needed"
  done
  netpol_enabled || die "run netpol-enable (approved) before the session"
  : "${DIGESTS:?DIGESTS required}"
  # From here on every exit path scales the pool to 0.
  # Each stage runs in its own process group (setsid) and the session waits on
  # it, so a signal or the time box interrupts the wait at once (bash would
  # defer the trap until a foreground child ends) and the trap stops the whole
  # group: no orphaned kubectl/gcloud/helm keeps changing the cluster.
  child=""
  finish() {  # errexit off: a failed kill must never skip the scale-down
    status=$?
    set +e
    trap - EXIT INT TERM HUP
    [ -n "$child" ] && kill -TERM -- "-$child" 2>/dev/null
    kill -TERM "${watchdog:-}" 2>/dev/null
    record "session ending (status $status): cleanup"
    (set -e; cleanup) || status=1
    exit "$status"
  }
  trap finish EXIT
  trap 'record "session interrupted"; exit 130' INT TERM HUP
  session_pid=$$
  (  # its own sleep is stopped with it, and it never holds the session's stdout
    trap 'kill "$nap" 2>/dev/null; exit 0' TERM
    sleep "$((minutes * 60))" & nap=$!
    wait "$nap" && { echo "$(date -u +%FT%TZ) session time box of $minutes min reached" >> "$evidence/operator.log"
                     kill -TERM "$session_pid"; }
  ) </dev/null >/dev/null 2>&1 &
  watchdog=$!
  for next in $SESSION_STAGES; do
    setsid "$0" "$next" "$evidence" &
    child=$!
    wait "$child" || die "stage $next failed; scaling down"
    child=""
  done
  record "session completed"
  ;;
esac
record "$(date -u +%FT%TZ) stage=$stage ok"
[ -e "$evidence/state/$stage.attempted" ] && touch "$evidence/state/$stage.done"
true
