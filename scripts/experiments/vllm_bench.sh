#!/usr/bin/env bash
# EXP-V1 (vLLM CPU thread binding) and EXP-V2 (Kubernetes Service versus llm-d routing).
# usage: vllm_bench.sh <exp-v1|exp-v2> <plan|session|down> <evidence-dir>
#
#   plan     Local only: renders both arms and prints the budget. Touches no cluster.
#   session  Needs EXP_APPROVED=yes. Creates pool bench-pool and namespace ewallet-bench,
#            runs both arms, compares them, and always removes what it created.
#   down     Removes the same two things after a lost session.
#
# Env: KUBE_CONTEXT (must be the ewallet-dev context), VLLM_IMAGE (digest reference),
#   EXP_RUNS (per arm, default 3), EXP_MAX_MINUTES (hard stop, default 240, max 300).
#
# Rules this script keeps:
#  - It only creates, changes and deletes bench-pool and ewallet-bench. The production
#    release revision and the cpu-pool node list are read before and after and compared.
#  - Arms alternate (A B, B A, A B) so slow drift does not favour one arm.
#  - vLLM and the gateway restart before every run: no shared cache, fresh token budget.
#  - A failed run stops the session. It is not retried and its evidence is kept.
set -euo pipefail
experiment=${1:-} action=${2:-} evidence=${3:-}
die() { echo "vllm_bench: $*" >&2; exit 1; }
case "$experiment" in exp-v1|exp-v2) ;; *) die "usage: vllm_bench.sh <exp-v1|exp-v2> <plan|session|down> <evidence-dir>";; esac
case "$action" in plan|session|down) ;; *) die "unknown action $action";; esac
[ -n "$evidence" ] || die "evidence directory required"
PROJECT=ewallet-data-ai-platform-hg26 ZONE=asia-southeast1-b CLUSTER=ewallet-dev
POOL=bench-pool NS=ewallet-bench PROD_POOL=cpu-pool
EXPECTED_CONTEXT="gke_${PROJECT}_${ZONE}_${CLUSTER}"
GATEWAY_IMAGE=ghcr.io/agentgateway/agentgateway:v1.7.0@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b
GW_PORT=13300 METRICS_PORT=13301 EPP_PORT=13302
root=$(cd "$(dirname "$0")/../.." && pwd)
# EXP_CLIENT_DIR replaces the benchmark clients only in the offline simulation.
client_dir=${EXP_CLIENT_DIR:-$root/scripts/llm_agent}
python=${EXP_PYTHON:-python3}
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
# shellcheck source=../../infra/helm/llm-d/versions.env
. "$root/infra/helm/llm-d/versions.env"
if [ "$experiment" = exp-v1 ]; then
  nodes=1 spec="$root/configs/experiments/exp-v1-vllm-cpu.json" levels=1,2,4 baseline_label=b0-prime optimized_label=c1-thread-bind
else
  nodes=2 spec="$root/configs/experiments/exp-v2-llmd-routing.json" levels=1,2,4,8 baseline_label=b2 optimized_label=o2
fi
runs=${EXP_RUNS:-3} max_minutes=${EXP_MAX_MINUTES:-240}
[[ "$runs" =~ ^[2-5]$ ]] || die "EXP_RUNS must be 2-5; one run cannot support a verdict"
[[ "$max_minutes" =~ ^[0-9]+$ ]] && [ "$max_minutes" -ge 1 ] && [ "$max_minutes" -le 300 ] || die "EXP_MAX_MINUTES must be 1-300"

render_arm() {  # <arm> <run-id> <cidr> <port>
  "$python" "$root/scripts/experiments/render_bench.py" "$experiment" "$1" --vllm-image "$VLLM_IMAGE" \
    --gateway-image "$GATEWAY_IMAGE" --run-id "$2" --api-server-cidr "$3" --api-server-port "$4"
}

if [ "$action" = plan ]; then
  : "${VLLM_IMAGE:?VLLM_IMAGE required}"
  mkdir -p "$evidence/plan"
  for arm in baseline optimized; do render_arm "$arm" plan 192.0.2.1/32 443 > "$evidence/plan/$arm.yaml"; done
  diff "$evidence/plan/baseline.yaml" "$evidence/plan/optimized.yaml" > "$evidence/plan/arm-difference.diff" || true
  echo "experiment=$experiment nodes=$nodes machine=e2-standard-4 runs_per_arm=$runs max_minutes=$max_minutes levels=$levels"
  echo "arm difference: $evidence/plan/arm-difference.diff"
  exit 0
fi

[ "${KUBE_CONTEXT:-}" = "$EXPECTED_CONTEXT" ] || die "KUBE_CONTEXT must be $EXPECTED_CONTEXT"
k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=60s "$@"; }
pools() { gcloud container node-pools list --cluster "$CLUSTER" --zone "$ZONE" --project "$PROJECT" --format='value(name)'; }
production_state() {
  helm list --kube-context "$KUBE_CONTEXT" --namespace default --filter '^llm-agent$' --output json
  k get nodes -l "cloud.google.com/gke-nodepool=$PROD_POOL" -o name | sort
}

remove_stack() {
  local status=0
  if k get namespace "$NS" -o 'jsonpath={.metadata.labels.ewallet\.io/experiment}' 2>/dev/null | grep -qx true; then
    helm uninstall llmd-router --kube-context "$KUBE_CONTEXT" --namespace "$NS" --wait --timeout 5m >/dev/null 2>&1 || true
    k delete namespace "$NS" --wait=true --timeout=5m || status=1
  fi
  if pools | grep -qx "$POOL"; then
    gcloud container node-pools delete "$POOL" --cluster "$CLUSTER" --zone "$ZONE" --project "$PROJECT" --quiet || status=1
  fi
  pools | grep -qx "$POOL" && { echo "vllm_bench: $POOL still exists" >&2; status=1; }
  if k get namespace "$NS" >/dev/null 2>&1; then echo "vllm_bench: $NS still exists" >&2; status=1; fi
  return "$status"
}

if [ "$action" = down ]; then remove_stack; echo "pool and namespace removed"; exit 0; fi

[ "${EXP_APPROVED:-}" = yes ] || die "set EXP_APPROVED=yes after owner approval"
: "${VLLM_IMAGE:?VLLM_IMAGE required}"
[ ! -e "$evidence" ] || die "$evidence exists; evidence is never overwritten"
if pools | grep -qx "$POOL"; then die "$POOL already exists"; fi
if k get namespace "$NS" >/dev/null 2>&1; then die "$NS already exists"; fi
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd)
for arm in baseline optimized; do mkdir -p "$evidence/$arm"/{config,raw,metrics,logs,screenshots}; done
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$evidence/session.log"; }
production_state > "$evidence/production-before.txt"
stop="$evidence/STOP" pids=() timer="" deviations=()

stop_children() {
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  pids=()
}

finish() {
  local status=$?
  set +e
  trap - EXIT INT TERM HUP
  touch "$stop"
  stop_children
  [ -n "$timer" ] && kill "$timer" 2>/dev/null
  [ "$status" = 0 ] || deviations+=("session ended with status $status")
  local args=()
  for item in "${deviations[@]}"; do args+=(--deviation "$item"); done
  if [ -f "$evidence/manifest.json" ]; then
    "$python" -m ops.experiments.cli finish --spec "$spec" --dir "$evidence" "${args[@]}"
    "$python" -m ops.experiments.cli compare --spec "$spec" --dir "$evidence" > "$evidence/compare.log" 2>&1 \
      || log "comparison failed; see compare.log"
  fi
  log "cleanup (status $status)"
  remove_stack >> "$evidence/session.log" 2>&1 || { log "CLEANUP FAILED: run down"; status=1; }
  production_state > "$evidence/production-after.txt"
  if ! cmp -s "$evidence/production-before.txt" "$evidence/production-after.txt"; then
    log "PRODUCTION STATE CHANGED: compare production-before.txt and production-after.txt"
    status=1
  fi
  log "session end status=$status"
  exit "$status"
}
trap finish EXIT
trap 'log "interrupted"; exit 130' INT TERM HUP

# The trap ends the sleep too; killing only the subshell would leave it running.
(
  trap 'kill "$nap" 2>/dev/null; exit 0' TERM
  sleep "$((max_minutes * 60))" & nap=$!
  wait "$nap"
  touch "$stop"
) </dev/null >/dev/null 2>&1 &
timer=$!
log "start $experiment source=$(git -C "$root" rev-parse HEAD) runs=$runs"
router_chart=""
if [ "$experiment" = exp-v2 ]; then
  helm pull "$LLMD_STANDALONE_CHART" --version "$LLMD_STANDALONE_VERSION" -d "$evidence" >> "$evidence/session.log" 2>&1
  router_chart="$evidence/llm-d-router-standalone-$LLMD_STANDALONE_VERSION.tgz"
  echo "$LLMD_STANDALONE_CHART_SHA256  $router_chart" | sha256sum --check --quiet || die "router chart digest mismatch"
fi
log "creating $POOL with $nodes node(s)"
gcloud container node-pools create "$POOL" --cluster "$CLUSTER" --zone "$ZONE" --project "$PROJECT" \
  --machine-type e2-standard-4 --num-nodes "$nodes" --disk-type pd-balanced --disk-size 50 \
  --node-labels ewallet.io/bench=true --node-taints ewallet.io/bench=true:NoSchedule \
  --no-enable-autoupgrade --quiet >> "$evidence/session.log" 2>&1
api_ip=$(k get endpoints kubernetes -n default -o 'jsonpath={.subsets[0].addresses[0].ip}')
api_port=$(k get endpoints kubernetes -n default -o 'jsonpath={.subsets[0].ports[0].port}')
[ -n "$api_ip" ] && [ -n "$api_port" ] || die "cannot read the API server endpoint"
render_arm baseline setup "$api_ip/32" "$api_port" > "$evidence/setup.yaml"
# The Namespace is the first document; Secrets must exist before the Deployments start.
awk '/^---$/ {exit} {print}' "$evidence/setup.yaml" | k apply -f -
vllm_key=$(openssl rand -hex 32) client_key=$(openssl rand -hex 32)
k -n "$NS" create secret generic vllm-api-key --from-file=api-key=/dev/stdin <<<"$vllm_key" >/dev/null
k -n "$NS" create secret generic agentgateway-client-key --from-file=api-key=/dev/stdin <<<"$client_key" >/dev/null
unset vllm_key
"$python" -m ops.experiments.cli preflight --spec "$spec" --dir "$evidence" --kube-context "$KUBE_CONTEXT" \
  --namespace "$NS" --setting "vllm_image=$VLLM_IMAGE" --setting "gateway_image=$GATEWAY_IMAGE" \
  --setting "levels=$levels" --setting "runs_per_arm=$runs" --setting "nodes=$nodes" \
  --setting "machine_type=e2-standard-4" --setting "max_minutes=$max_minutes"

run_arm() {  # <arm> <run-number>
  local arm=$1 run=$2 label dir config metrics logs resources
  [ "$arm" = baseline ] && label=$baseline_label || label=$optimized_label
  dir="$evidence/$arm/raw/run-$run" config="$evidence/$arm/config/run-$run.yaml"
  metrics="$evidence/$arm/metrics/run-$run" logs="$evidence/$arm/logs/run-$run"
  mkdir -p "$dir" "$metrics" "$logs"
  resources="$metrics/resources.jsonl"
  log "run $run $arm ($label)"
  render_arm "$arm" "$arm-$run" "$api_ip/32" "$api_port" > "$config"
  if [ "$experiment" = exp-v2 ]; then
    if [ "$arm" = optimized ]; then
      helm upgrade --install llmd-router "$router_chart" --kube-context "$KUBE_CONTEXT" --namespace "$NS" \
        -f "$root/infra/helm/llm-d/standalone-values-g2.yaml" -f "$root/infra/kubernetes/experiments/llmd-bench-values.yaml" \
        --wait --timeout 10m >> "$logs/router.log" 2>&1
    else
      helm uninstall llmd-router --kube-context "$KUBE_CONTEXT" --namespace "$NS" --wait --timeout 5m \
        >> "$logs/router.log" 2>&1 || true
    fi
  fi
  k apply -f "$config" >> "$logs/apply.log"
  k -n "$NS" rollout status deployment/agentgateway --timeout=5m >> "$logs/apply.log"
  kubectl --context "$KUBE_CONTEXT" -n "$NS" rollout status deployment/vllm-primary --timeout=30m >> "$logs/apply.log"
  k -n "$NS" get pods -l app.kubernetes.io/name=vllm --field-selector=status.phase=Running -o json > "$logs/pods.json"
  k get nodes -l ewallet.io/bench=true -o json > "$logs/nodes.json"
  k -n "$NS" get configmap agentgateway-llm-config -o 'jsonpath={.data.config\.yaml}' > "$logs/gateway-config.yaml"
  "$python" -m ops.experiments.readback --pods "$logs/pods.json" --nodes "$logs/nodes.json" \
    --gateway-config "$logs/gateway-config.yaml" --output "$dir/controls.json"
  kubectl --context "$KUBE_CONTEXT" -n "$NS" port-forward svc/agentgateway "$GW_PORT:4000" >> "$logs/pf-gateway.log" 2>&1 &
  pids+=("$!")
  kubectl --context "$KUBE_CONTEXT" -n "$NS" port-forward deployment/vllm-primary "$METRICS_PORT:8000" >> "$logs/pf-metrics.log" 2>&1 &
  pids+=("$!")
  local client_args=(--config "$label" --gateway "http://127.0.0.1:$GW_PORT" --levels "$levels" --timeout 220
    --resources "$resources" --stop-file "$stop" --output "$dir")
  local client=o1_benchmark.py
  if [ "$experiment" = exp-v1 ]; then
    client_args+=(--metrics "http://127.0.0.1:$METRICS_PORT" --allocated-cpu 2)
  else
    client=g2_benchmark.py
    local names
    names=$(k -n "$NS" get pods -l app.kubernetes.io/name=vllm -o 'jsonpath={.items[*].metadata.name}' | tr ' ' ,)
    client_args+=(--context "$KUBE_CONTEXT" --namespace "$NS" --pods "$names" --allocated-cpu 4)
    if [ "$arm" = optimized ]; then
      kubectl --context "$KUBE_CONTEXT" -n "$NS" port-forward svc/llmd-router-epp "$EPP_PORT:9090" >> "$logs/pf-epp.log" 2>&1 &
      pids+=("$!")
      client_args+=(--epp-metrics "http://127.0.0.1:$EPP_PORT/metrics")
    fi
  fi
  local sampler_stop="$metrics/SAMPLER_STOP"
  "$python" -m ops.experiments.resource_sampler --context "$KUBE_CONTEXT" --namespace "$NS" --output "$resources" \
    --stop-file "$sampler_stop" & pids+=("$!")
  sleep "${EXP_FORWARD_WAIT:-5}"
  local status=0
  date -u +%FT%TZ > "$metrics/window-start.txt"
  EWALLET_GATEWAY_KEY=$client_key "$python" "$client_dir/$client" "${client_args[@]}" > "$logs/client.log" 2>&1 || status=$?
  date -u +%FT%TZ > "$metrics/window-end.txt"
  touch "$sampler_stop"
  k -n "$NS" logs deployment/agentgateway --tail=2000 2>/dev/null | grep -viE 'authorization|bearer|api[-_]key' \
    > "$logs/gateway.log" || true
  stop_children
  [ "$status" = 0 ] || { deviations+=("run $run $arm client exit $status"); die "run $run $arm failed; see $logs/client.log"; }
}

for run_number in $(seq 1 "$runs"); do
  run=$(printf '%02d' "$run_number")
  if [ $((run_number % 2)) = 1 ]; then order="baseline optimized"; else order="optimized baseline"; fi
  for arm in $order; do
    if [ -e "$stop" ]; then deviations+=("time limit reached before run $run $arm"); die "time limit reached"; fi
    run_arm "$arm" "$run"
  done
done
log "all runs complete"
