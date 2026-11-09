#!/usr/bin/env bash
# EXP-V1 (vLLM CPU thread binding) and EXP-V2 (Kubernetes Service versus llm-d routing).
# usage: vllm_bench.sh <exp-v1|exp-v2> <plan|session|drill|down> <evidence-dir>
#
#   plan     Local only: renders both arms, the observability and watchdog stacks, and the
#            worst-case cost plan. Touches no cluster.
#   session  Needs EXP_APPROVED=yes. Creates pool bench-pool and namespace ewallet-bench,
#            runs both arms, compares them, and always removes what it created.
#   drill    Needs EXP_APPROVED=yes. Proves the independent cleanup: one node, one idle pod,
#            a watchdog with a short deadline; waits until the autoscaler has removed the
#            node without any action from this machine, then removes pool and namespace.
#   down     Removes the same two things after a lost session.
#
# Env: KUBE_CONTEXT (must be the ewallet-dev context), VLLM_IMAGE (digest reference),
#   EXP_RUNS (per arm, default 3), EXP_MAX_MINUTES (hard stop, default 210, max 215),
#   EXP_PROFILE (formal | smoke: one run per arm, fewer levels, never a result),
#   EXP_CAPTURE_MODE (unattended | interactive: owner checkpoint after every run),
#   EXP_CHECKPOINT_MINUTES (longest wait at a checkpoint, default 20, max 45),
#   EXP_CHECKPOINT_RUNS (all | first: stop only after run 01 of each arm and at the comparison),
#   EXP_MAX_USD (worst-case ceiling, default 5, max 5), EXP_LAPTOP_TIMER (auto | off),
#   EXP_POLICY_PROBE (on | off).
#
# Rules this script keeps:
#  - It only creates, changes and deletes bench-pool and ewallet-bench, and it deletes them
#    only when they carry its ownership mark. Every Helm release and the cpu-pool nodes and
#    size are read before and after and compared.
#  - A read that fails is an error, never "absent", so cleanup cannot report a false success.
#  - Arms alternate (A B, B A, A B) so slow drift does not favour one arm.
#  - vLLM and the gateway restart before every run: no shared cache, fresh token budget.
#  - A failed run stops the session (a run that measured every level is kept, its failed
#    requests counted: ops/experiments/run_outcome.py). It is not retried; evidence is kept.
#  - The time limit bounds every wait, and it is wall-clock time, so a suspended laptop
#    that wakes up past the limit stops at once.
#  - Host loss: an in-cluster watchdog deletes the workloads at limit + 10 min and the
#    autoscaled pool (min 0, max = nodes) drains to zero nodes; a systemd user timer on this
#    machine runs `down` at limit + 20 min as a second path. Pool and namespace stay until
#    `down` runs, but bill no compute.
set -euo pipefail
experiment=${1:-} action=${2:-} evidence=${3:-}
die() { echo "vllm_bench: $*" >&2; exit 1; }
case "$experiment" in exp-v1|exp-v2) ;; *) die "usage: vllm_bench.sh <exp-v1|exp-v2> <plan|session|drill|down> <evidence-dir>";; esac
case "$action" in plan|session|drill|down) ;; *) die "unknown action $action";; esac
[ -n "$evidence" ] || die "evidence directory required"
PROJECT=ewallet-data-ai-platform-hg26 ZONE=asia-southeast1-b CLUSTER=ewallet-dev
POOL=bench-pool NS=ewallet-bench PROD_POOL=cpu-pool OWNER_LABEL=ewallet.io/bench=true
NODE_SA=ewallet-gke-nodes@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com
IMAGE_REPO=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/vllm-cpu-model
MODEL_REVISION=c1899de289a04d12100db370d81485cdf75e47ca
EXPECTED_CONTEXT="gke_${PROJECT}_${ZONE}_${CLUSTER}"
GATEWAY_IMAGE=ghcr.io/agentgateway/agentgateway:v1.7.0@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b
# Python 3 image already in the project registry; runs the exporter and the watchdog.
HELPER_IMAGE=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/g3-byo-coordinator@sha256:483dd943290c02fdd4d0d3e9e38b4182dae76a01581e6e5e191ca977b7498aff
GW_PORT=13300 METRICS_PORT=13301 EPP_PORT=13302 GRAFANA_PORT=13030 PROM_PORT=13090
WATCHDOG_GRACE_MINUTES=10 LAPTOP_TIMER_GRACE_MINUTES=20
root=$(cd "$(dirname "$0")/../.." && pwd)
self="$root/scripts/experiments/vllm_bench.sh"
# EXP_CLIENT_DIR replaces the benchmark clients only in the offline simulation.
client_dir=${EXP_CLIENT_DIR:-$root/scripts/llm_agent}
python=${EXP_PYTHON:-python3}
lock_file=${EXP_LOCK_FILE:-${XDG_RUNTIME_DIR:-/tmp}/ewallet-bench-cleanup.lock}
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
# shellcheck source=../../infra/helm/llm-d/versions.env
. "$root/infra/helm/llm-d/versions.env"
profile=${EXP_PROFILE:-formal}
case "$profile" in formal|smoke) ;; *) die "EXP_PROFILE must be formal or smoke";; esac
if [ "$experiment" = exp-v1 ]; then
  nodes=1 spec="$root/configs/experiments/exp-v1-vllm-cpu.json" levels=1,2,4 baseline_label=b0-prime optimized_label=c1-thread-bind
  [ "$profile" = formal ] || levels=1
  route_baseline="Service -> vLLM" route_optimized="Service -> vLLM"
else
  nodes=2 spec="$root/configs/experiments/exp-v2-llmd-routing.json" levels=1,2,4,8 baseline_label=b2 optimized_label=o2
  [ "$profile" = formal ] || levels=1,4
  route_baseline="Kubernetes Service" route_optimized="llm-d EPP"
fi
max_minutes=${EXP_MAX_MINUTES:-210} capture_mode=${EXP_CAPTURE_MODE:-unattended}
checkpoint_minutes=${EXP_CHECKPOINT_MINUTES:-20} max_usd=${EXP_MAX_USD:-5} checkpoint_runs=${EXP_CHECKPOINT_RUNS:-all}
if [ "$profile" = smoke ]; then runs=${EXP_RUNS:-1}; else runs=${EXP_RUNS:-3}; fi
if [ "$profile" = smoke ]; then
  [ "$runs" = 1 ] || die "EXP_PROFILE=smoke runs exactly one run per arm"
else
  [[ "$runs" =~ ^[2-5]$ ]] || die "EXP_RUNS must be 2-5; one run cannot support a verdict"
fi
[[ "$max_minutes" =~ ^[0-9]+$ ]] && [ "$max_minutes" -ge 1 ] && [ "$max_minutes" -le 215 ] \
  || die "EXP_MAX_MINUTES must be 1-215 (10 min setup + limit + 15 min teardown <= 240)"
case "$capture_mode" in unattended|interactive) ;; *) die "EXP_CAPTURE_MODE must be unattended or interactive";; esac
case "$checkpoint_runs" in all|first) ;; *) die "EXP_CHECKPOINT_RUNS must be all or first";; esac
[[ "$checkpoint_minutes" =~ ^[0-9]+$ ]] && [ "$checkpoint_minutes" -ge 1 ] && [ "$checkpoint_minutes" -le 45 ] \
  || die "EXP_CHECKPOINT_MINUTES must be 1-45"
"$python" -c "import sys; v=float(sys.argv[1]); sys.exit(0 if 0 < v <= 5 else 1)" "$max_usd" 2>/dev/null \
  || die "EXP_MAX_USD must be above 0 and at most 5"

render_arm() {  # <arm> <run-id> <cidr> <port> [stack]
  "$python" "$root/scripts/experiments/render_bench.py" "$experiment" "$1" --vllm-image "$VLLM_IMAGE" \
    --gateway-image "$GATEWAY_IMAGE" --helper-image "$HELPER_IMAGE" --run-id "$2" --api-server-cidr "$3" \
    --api-server-port "$4" --stack "${5:-serving}" --dns-ips "${dns_ips:-169.254.20.10}"
}
render_watchdog() {  # <deadline-utc> <cidr> <port>
  "$python" "$root/scripts/experiments/render_bench.py" "$experiment" baseline --stack watchdog \
    --helper-image "$HELPER_IMAGE" --deadline-utc "$1" --api-server-cidr "$2" --api-server-port "$3"
}
render_observability() {  # <cidr> <port>
  "$python" "$root/scripts/experiments/render_bench.py" "$experiment" baseline --stack observability \
    --helper-image "$HELPER_IMAGE" --api-server-cidr "$1" --api-server-port "$2"
}
cost_plan() {  # <minutes> [nodes]
  "$python" -m ops.experiments.cost_plan --nodes "${2:-$nodes}" --max-minutes "$1" --max-usd "$max_usd"
}

if [ "$action" = plan ]; then
  : "${VLLM_IMAGE:?VLLM_IMAGE required}"
  mkdir -p "$evidence/plan"
  for arm in baseline optimized; do render_arm "$arm" plan 192.0.2.1/32 443 > "$evidence/plan/$arm.yaml"; done
  diff "$evidence/plan/baseline.yaml" "$evidence/plan/optimized.yaml" > "$evidence/plan/arm-difference.diff" || true
  render_observability 192.0.2.1/32 443 > "$evidence/plan/observability.yaml"
  render_watchdog 2000-01-01T00:00:00Z 192.0.2.1/32 443 > "$evidence/plan/watchdog.yaml"
  status=0
  cost_plan "$max_minutes" > "$evidence/plan/cost-plan.json" || status=1
  echo "experiment=$experiment profile=$profile nodes=$nodes machine=e2-standard-4 runs_per_arm=$runs max_minutes=$max_minutes levels=$levels capture=$capture_mode"
  echo "arm difference: $evidence/plan/arm-difference.diff"
  cat "$evidence/plan/cost-plan.json"
  [ "$status" = 0 ] || die "the worst case does not fit the ceilings; see cost-plan.json"
  exit 0
fi

[ "${KUBE_CONTEXT:-}" = "$EXPECTED_CONTEXT" ] || die "KUBE_CONTEXT must be $EXPECTED_CONTEXT"
k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=60s "$@"; }
# --request-timeout would end a long wait early, so waits go without it.
kw() { kubectl --context "$KUBE_CONTEXT" "$@"; }
gc() { gcloud "$@" --project "$PROJECT"; }
pools() { gc container node-pools list --cluster "$CLUSTER" --zone "$ZONE" --format='value(name)'; }
pool_exists() {  # 0 present, 1 absent, 2 unknown
  local names
  names=$(pools) || return 2
  grep -qx "$POOL" <<<"$names"
}
pool_is_ours() {
  gc container node-pools describe "$POOL" --cluster "$CLUSTER" --zone "$ZONE" \
    --format='value(config.labels)' | tr ';' '\n' | grep -qx "$OWNER_LABEL"
}
namespace_mark() {  # prints the ownership mark; empty when the namespace is absent
  k get namespace "$NS" --ignore-not-found -o 'jsonpath={.metadata.name}{"="}{.metadata.labels.ewallet\.io/experiment}'
}
bench_vms() {  # names of VMs and disks that belong to bench-pool; fails when unreadable
  gc compute instances list --filter="name~^gke-${CLUSTER}-${POOL}-" --format='value(name)'
  gc compute disks list --filter="name~^gke-${CLUSTER}-${POOL}-" --format='value(name)'
}
production_state() {
  helm list --kube-context "$KUBE_CONTEXT" --all-namespaces --output json
  k get nodes -l "cloud.google.com/gke-nodepool=$PROD_POOL" -o name | sort
  gc container node-pools describe "$PROD_POOL" --cluster "$CLUSTER" --zone "$ZONE" \
    --format='value(initialNodeCount,autoscaling.enabled)'
  gc compute instance-groups managed list --filter="name~^gke-${CLUSTER}-${PROD_POOL}-" --format='value(targetSize)'
}

remove_stack() {
  local status=0 mark pool_status=0 left
  # One cleanup at a time: the session, the laptop timer and a manual `down` can overlap.
  exec 9>"$lock_file"
  flock -w 900 9 || { echo "vllm_bench: cleanup lock busy" >&2; return 1; }
  if ! mark=$(namespace_mark); then
    echo "vllm_bench: cannot read $NS" >&2
    status=1
  elif [ "$mark" = "$NS=true" ]; then
    helm uninstall llmd-router --kube-context "$KUBE_CONTEXT" --namespace "$NS" --wait --timeout 5m >/dev/null 2>&1 || true
    kw delete namespace "$NS" --wait=true --timeout=5m || status=1
  elif [ -n "$mark" ]; then
    echo "vllm_bench: $NS has no experiment mark; leaving it" >&2
    status=1
  fi
  pool_exists || pool_status=$?
  if [ "$pool_status" = 0 ]; then
    if pool_is_ours; then
      gc container node-pools delete "$POOL" --cluster "$CLUSTER" --zone "$ZONE" --quiet || status=1
    else
      echo "vllm_bench: $POOL has no $OWNER_LABEL mark; leaving it" >&2
      status=1
    fi
  fi
  pool_status=0
  pool_exists || pool_status=$?
  [ "$pool_status" = 1 ] || { echo "vllm_bench: $POOL is not confirmed gone" >&2; status=1; }
  mark=""
  mark=$(namespace_mark) || status=1
  [ -z "$mark" ] || { echo "vllm_bench: $NS is not confirmed gone" >&2; status=1; }
  if left=$(bench_vms); then
    [ -z "$left" ] || { echo "vllm_bench: bench VMs or disks remain: $(tr '\n' ' ' <<<"$left")" >&2; status=1; }
  else
    echo "vllm_bench: cannot list bench VMs and disks" >&2
    status=1
  fi
  flock -u 9
  return "$status"
}

if [ "$action" = down ]; then remove_stack; echo "pool and namespace removed; no bench VMs or disks remain"; exit 0; fi

[ "${EXP_APPROVED:-}" = yes ] || die "set EXP_APPROVED=yes after owner approval"
: "${VLLM_IMAGE:?VLLM_IMAGE required}"
[ ! -e "$evidence" ] || die "$evidence exists; evidence is never overwritten"
[[ "$VLLM_IMAGE" == "$IMAGE_REPO"@sha256:* ]] || die "VLLM_IMAGE must be a digest in $IMAGE_REPO"
if [ "$action" = drill ]; then nodes=1 max_minutes=${EXP_DRILL_MAX_MINUTES:-40}; fi
plan_text=$(cost_plan "$max_minutes") || die "the worst case does not fit the ceilings: $plan_text"
for image in "$VLLM_IMAGE" "$HELPER_IMAGE"; do
  gc artifacts docker images describe "$image" --format='value(image_summary.digest)' >/dev/null \
    || die "cannot confirm $image in Artifact Registry"
done
for port in "$GW_PORT" "$METRICS_PORT" "$EPP_PORT" "$GRAFANA_PORT" "$PROM_PORT"; do
  # A leftover forward on one of these would send the benchmark to the wrong target.
  ! ss -ltnH "( sport = :$port )" 2>/dev/null | grep -q . || die "local port $port is in use"
done
pool_status=0
pool_exists || pool_status=$?
[ "$pool_status" = 1 ] || die "$POOL must be absent (status $pool_status)"
mark=$(namespace_mark) || die "cannot read namespaces"
[ -z "$mark" ] || die "$NS already exists"
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd)
mkdir -p "$evidence/safety"
if [ "$action" = session ]; then
  for arm in baseline optimized; do mkdir -p "$evidence/$arm"/{config,raw,metrics,logs,screenshots}; done
fi
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$evidence/session.log"; }
printf '%s\n' "$plan_text" > "$evidence/safety/cost-plan.json"
production_state > "$evidence/production-before.txt"
stop="$evidence/STOP" pids=() obs_pids=() timer="" deviations=() timer_unit="" status_file="$evidence/safety/cleanup.txt"
started_epoch=$(date +%s)
deadline=$((started_epoch + max_minutes * 60))
watchdog_deadline=$(date -u -d "@$((deadline + WATCHDOG_GRACE_MINUTES * 60))" +%FT%TZ)

stop_children() {
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  pids=()
}

forward() {  # <log> <local-port> <target> <remote-port>: a port-forward that reconnects until STOP
  (
    set +e
    child=""
    trap 'kill "$child" 2>/dev/null; exit 0' TERM
    while [ ! -e "$stop" ]; do
      kubectl --context "$KUBE_CONTEXT" -n "$NS" port-forward "$3" "$2:$4" >> "$1" 2>&1 </dev/null &
      child=$!
      wait "$child"
      sleep 2
    done
  ) </dev/null >/dev/null 2>&1 &
  obs_pids+=("$!")
}

checkpoint() {  # <banner-file>: 0 continue, 1 no answer in time, 2 owner aborted
  [ "$capture_mode" = interactive ] || return 0
  local wait_s=$((checkpoint_minutes * 60)) left answer until remaining
  # Offline simulation only: seconds instead of minutes, so the timeout path can be tested.
  [ -z "${EXP_CHECKPOINT_TEST_SECONDS:-}" ] || wait_s=$EXP_CHECKPOINT_TEST_SECONDS
  left=$((deadline - $(date +%s)))
  [ "$left" -lt "$wait_s" ] && wait_s=$left
  [ "$wait_s" -gt 0 ] || { log "checkpoint skipped: time limit reached"; return 1; }
  tee -a "$evidence/session.log" < "$1" >&2
  until=$(($(date +%s) + wait_s))
  while :; do
    remaining=$((until - $(date +%s)))
    [ "$remaining" -gt 0 ] || { log "checkpoint: no answer within ${checkpoint_minutes} min"; return 1; }
    if read -r -t "$remaining" answer; then
      case "$answer" in
        CONTINUE) log "checkpoint: owner typed CONTINUE"; return 0;;
        ABORT) log "checkpoint: owner typed ABORT"; return 2;;
        *) echo "type CONTINUE or ABORT" >&2;;
      esac
    else
      log "checkpoint: no answer (timeout or end of input)"
      return 1
    fi
  done
}

disarm_laptop_timer() {
  [ -n "$timer_unit" ] || return 0
  systemctl --user stop "$timer_unit.timer" "$timer_unit.service" >/dev/null 2>&1 || true
  log "laptop timer $timer_unit disarmed"
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
    if [ "$status" = 0 ] && [ "$capture_mode" = interactive ] && [ -f "$evidence/comparison/comparison.md" ]; then
      {
        printf '%s\n%s — COMPARISON CHECKPOINT\n%s\n\n' "$(printf '=%.0s' {1..64})" "${experiment^^}" "$(printf '=%.0s' {1..64})"
        cat "$evidence/comparison/comparison.md"
        printf '\nCapture: comparison above (%s), manifest.json, and the Grafana dashboard over the whole session:\n' \
          "$evidence/comparison/comparison.md"
        printf '  http://127.0.0.1:%s/d/edai2-llm-cpu-bench/?orgId=1&from=%s000&to=now\n' "$GRAFANA_PORT" "$started_epoch"
        printf '  screenshots -> %s/comparison/screenshots/<name>.png\n' "$evidence"
        printf 'Type CONTINUE to clean up now (or wait up to %s min).\n' "$checkpoint_minutes"
      } > "$evidence/safety/checkpoint-final.txt"
      mkdir -p "$evidence/comparison/screenshots"
      deadline=$(($(date +%s) + checkpoint_minutes * 60))
      checkpoint "$evidence/safety/checkpoint-final.txt" || true
    fi
  fi
  for pid in "${obs_pids[@]}"; do kill "$pid" 2>/dev/null; done
  k -n "$NS" get jobs,cronjobs -o wide > "$evidence/safety/watchdog-jobs.txt" 2>&1
  k -n "$NS" logs -l app.kubernetes.io/name=bench-watchdog --tail=50 --prefix > "$evidence/safety/watchdog.log" 2>&1
  log "cleanup (status $status)"
  if remove_stack >> "$evidence/session.log" 2>&1; then
    echo "pool and namespace confirmed absent; no bench VMs or disks" > "$status_file"
  else
    log "CLEANUP FAILED: run down"
    echo "CLEANUP FAILED: see session.log, then run down" > "$status_file"
    status=1
  fi
  disarm_laptop_timer
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

# Wall-clock deadline: a suspended laptop that resumes past it stops at once.
(
  trap 'exit 0' TERM
  while [ "$(date +%s)" -lt "$deadline" ]; do sleep 5; done
  touch "$stop"
) </dev/null >/dev/null 2>&1 &
timer=$!
log "start $experiment $action profile=$profile source=$(git -C "$root" rev-parse HEAD) runs=$runs capture=$capture_mode"
log "time limit $(date -u -d "@$deadline" +%FT%TZ); watchdog deadline $watchdog_deadline"
router_chart=""
if [ "$action" = session ] && [ "$experiment" = exp-v2 ]; then
  helm pull "$LLMD_STANDALONE_CHART" --version "$LLMD_STANDALONE_VERSION" -d "$evidence" >> "$evidence/session.log" 2>&1
  router_chart="$evidence/llm-d-router-standalone-$LLMD_STANDALONE_VERSION.tgz"
  echo "$LLMD_STANDALONE_CHART_SHA256  $router_chart" | sha256sum --check --quiet || die "router chart digest mismatch"
fi
log "creating $POOL with $nodes node(s), autoscaling 0-$nodes"
# Same node identity and hardening as cpu-pool, which is what lets nodes pull from the
# registry. The cluster is on a release channel, so auto-upgrade cannot be turned off; a
# node replaced mid-session changes node_names and makes the comparison invalid.
# Autoscaling (min 0) is the host-loss path: once the watchdog has deleted the workloads,
# the empty nodes are removed. max = nodes caps the pool at the planned size.
gc container node-pools create "$POOL" --cluster "$CLUSTER" --zone "$ZONE" \
  --machine-type e2-standard-4 --num-nodes "$nodes" --enable-autoscaling --min-nodes 0 --max-nodes "$nodes" \
  --disk-type pd-balanced --disk-size 50 \
  --image-type COS_CONTAINERD --service-account "$NODE_SA" \
  --scopes https://www.googleapis.com/auth/cloud-platform --workload-metadata GKE_METADATA \
  --shielded-secure-boot --shielded-integrity-monitoring \
  --node-labels "$OWNER_LABEL" --node-taints ewallet.io/bench=true:NoSchedule \
  --quiet >> "$evidence/session.log" 2>&1
api_ip=$(k get endpoints kubernetes -n default -o 'jsonpath={.subsets[0].addresses[0].ip}')
api_port=$(k get endpoints kubernetes -n default -o 'jsonpath={.subsets[0].ports[0].port}')
api_svc=$(k get service kubernetes -n default -o 'jsonpath={.spec.clusterIP}')
[ -n "$api_ip" ] && [ -n "$api_port" ] && [ -n "$api_svc" ] || die "cannot read the API server endpoint"
# Both the endpoint and the Service address, as in the global ModelConfig canary.
api_cidrs="$api_ip/32,$api_svc/32"
# NodeLocal DNSCache listens on 169.254.20.10 and on the kube-dns ClusterIP.
dns_svc=$(k get service kube-dns -n kube-system -o 'jsonpath={.spec.clusterIP}')
[ -n "$dns_svc" ] || die "cannot read the kube-dns Service address"
dns_ips="169.254.20.10,$dns_svc"
if [ "$action" = drill ]; then
  render_arm baseline drill "$api_cidrs" "$api_port" drill > "$evidence/safety/drill-stack.yaml"
  awk '/^---$/ {exit} {print}' "$evidence/safety/drill-stack.yaml" | k apply -f -
else
  render_arm baseline setup "$api_cidrs" "$api_port" > "$evidence/setup.yaml"
  # The Namespace is the first document; Secrets must exist before the Deployments start.
  awk '/^---$/ {exit} {print}' "$evidence/setup.yaml" | k apply -f -
fi

# The watchdog goes in before any workload, so every billable pod is covered by it.
if [ "$action" = drill ]; then
  watchdog_deadline=$(date -u -d "@$(($(date +%s) + ${EXP_DRILL_DEADLINE_MINUTES:-4} * 60))" +%FT%TZ)
fi
render_watchdog "$watchdog_deadline" "$api_cidrs" "$api_port" > "$evidence/safety/watchdog.yaml"
k apply -f "$evidence/safety/watchdog.yaml" >> "$evidence/session.log"
sa="system:serviceaccount:$NS:bench-watchdog"
{
  for check in "delete deployments -n $NS" "delete deployments -n ewallet-agents" "delete deployments -n default" \
               "delete namespaces" "delete nodes" "get secrets -n $NS" "delete cronjobs/bench-watchdog -n $NS" \
               "delete cronjobs/other -n $NS"; do
    # shellcheck disable=SC2086
    echo "$check: $(k auth can-i $check --as "$sa" 2>/dev/null || true)"
  done
} > "$evidence/safety/watchdog-rbac.txt"
grep -qx "delete deployments -n $NS: yes" "$evidence/safety/watchdog-rbac.txt" \
  || die "the watchdog cannot delete workloads in $NS; see safety/watchdog-rbac.txt"
if grep -E -- "-n (ewallet-agents|default): yes|^delete (namespaces|nodes): yes|get secrets.*: yes|cronjobs/other.*: yes" \
     "$evidence/safety/watchdog-rbac.txt" >/dev/null; then
  die "the watchdog has more access than its own namespace; see safety/watchdog-rbac.txt"
fi
log "watchdog armed for $watchdog_deadline (in-cluster, RBAC limited to $NS)"

if [ "${EXP_LAPTOP_TIMER:-auto}" = auto ] && [ "$action" = session ] && command -v systemd-run >/dev/null; then
  timer_unit="ewallet-bench-down-$(date -u +%Y%m%dT%H%M%S)"
  fire=$(date -u -d "@$((deadline + LAPTOP_TIMER_GRACE_MINUTES * 60))" '+%F %T UTC')
  if systemd-run --user --unit "$timer_unit" --on-calendar "$fire" --timer-property=AccuracySec=30s \
       --setenv=KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}" --setenv=KUBE_CONTEXT="$KUBE_CONTEXT" \
       --setenv=PATH="$PATH" --setenv=EXP_LOCK_FILE="$lock_file" \
       "$self" "$experiment" down "$evidence/safety/laptop-timer" >> "$evidence/session.log" 2>&1; then
    log "laptop timer $timer_unit runs down at $fire (journalctl --user -u $timer_unit)"
  else
    log "laptop timer could not be armed; the in-cluster watchdog remains"
    timer_unit=""
  fi
fi

if [ "$action" = drill ]; then
  # Everything below happens in the cluster; this machine only watches.
  k apply -f "$evidence/safety/drill-stack.yaml" >> "$evidence/session.log"
  kw -n "$NS" rollout status deployment/drill-idle --timeout=10m >> "$evidence/session.log"
  log "drill: idle pod running; waiting for the watchdog (deadline $watchdog_deadline) and the autoscaler"
  timeline="$evidence/safety/drill-timeline.tsv"
  printf 'utc\tdeployments\tcronjob\tbench_nodes\n' > "$timeline"
  result=FAIL
  while [ ! -e "$stop" ]; do
    deployments=$(k -n "$NS" get deployments -o name 2>/dev/null | wc -l || echo "?")
    cron=$(k -n "$NS" get cronjob bench-watchdog -o name --ignore-not-found 2>/dev/null | wc -l || echo "?")
    bench_nodes=$(k get nodes -l "$OWNER_LABEL" -o name 2>/dev/null | wc -l || echo "?")
    printf '%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$deployments" "$cron" "$bench_nodes" >> "$timeline"
    if [ "$deployments" = 0 ] && [ "$cron" = 0 ] && [ "$bench_nodes" = 0 ]; then result=PASS; break; fi
    sleep "${EXP_DRILL_POLL_SECONDS:-30}"
  done
  k -n "$NS" logs -l app.kubernetes.io/name=bench-watchdog --tail=20 --prefix > "$evidence/safety/watchdog.log" 2>&1 || true
  gc compute instance-groups managed list --filter="name~^gke-${CLUSTER}-${POOL}-" \
    --format='value(name,targetSize)' > "$evidence/safety/drill-mig.txt" 2>&1 || true
  log "drill result: $result (timeline: safety/drill-timeline.tsv)"
  echo "$result" > "$evidence/safety/drill-result.txt"
  [ "$result" = PASS ] || die "watchdog drill did not reach zero nodes in time"
  exit 0
fi

vllm_key=$(openssl rand -hex 32) client_key=$(openssl rand -hex 32)
# printf, not <<<: a here-string adds a newline that would become part of the key, so
# "Bearer <key>\n" headers fail and the client key never matches.
printf '%s' "$vllm_key" | k -n "$NS" create secret generic vllm-api-key --from-file=api-key=/dev/stdin >/dev/null
printf '%s' "$client_key" | k -n "$NS" create secret generic agentgateway-client-key --from-file=api-key=/dev/stdin >/dev/null
openssl rand -hex 24 | tr -d '\n' | k -n "$NS" create secret generic bench-grafana-admin --from-file=password=/dev/stdin >/dev/null
unset vllm_key
"$python" -m ops.experiments.cli preflight --spec "$spec" --dir "$evidence" --kube-context "$KUBE_CONTEXT" \
  --namespace "$NS" --setting "vllm_image=$VLLM_IMAGE" --setting "gateway_image=$GATEWAY_IMAGE" \
  --setting "levels=$levels" --setting "runs_per_arm=$runs" --setting "nodes=$nodes" \
  --setting "machine_type=e2-standard-4" --setting "max_minutes=$max_minutes" --setting "profile=$profile" \
  --setting "capture_mode=$capture_mode" --setting "helper_image=$HELPER_IMAGE" \
  --setting "watchdog_deadline=$watchdog_deadline"
[ "$profile" = formal ] || deviations+=("SMOKE profile: $runs run per arm, levels $levels; a smoke test, not a benchmark result")

mkdir -p "$evidence/observability"/{dashboard,metric-readback,queries} "$evidence/gateway-policy"
render_observability "$api_cidrs" "$api_port" > "$evidence/observability/stack.yaml"
"$python" -m ops.experiments.bench_dashboard > "$evidence/observability/dashboard/llm-cpu-benchmark.json"
"$python" -c "import json; from ops.experiments.bench_dashboard import queries; print(json.dumps(queries(), indent=1))" \
  > "$evidence/observability/queries/panel-queries.json"
k apply -f "$evidence/observability/stack.yaml" >> "$evidence/session.log"
kw -n "$NS" rollout status deployment/bench-prometheus --timeout=10m >> "$evidence/session.log"
kw -n "$NS" rollout status deployment/bench-grafana --timeout=10m >> "$evidence/session.log"
forward "$evidence/observability/pf-grafana.log" "$GRAFANA_PORT" svc/bench-grafana 3000
forward "$evidence/observability/pf-prometheus.log" "$PROM_PORT" svc/bench-prometheus 9090
log "observability ready: Grafana http://127.0.0.1:$GRAFANA_PORT  Prometheus http://127.0.0.1:$PROM_PORT"

probe_counters() {  # <dir> <before|after>: vLLM and EPP counters around the policy probe
  local dir=$1 tag=$2 pod
  mkdir -p "$dir"
  # /metrics needs no key; read on the pod's loopback so no NetworkPolicy is involved.
  for pod in $(k -n "$NS" get pods -l app.kubernetes.io/name=vllm -o 'jsonpath={.items[*].metadata.name}' 2>/dev/null); do
    k -n "$NS" exec "$pod" -c vllm -- python3 -c \
      'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=20).read().decode())' \
      2>/dev/null | grep -E '^vllm:(admission_rejections|request_success)_total' > "$dir/vllm-$pod-$tag.prom" || true
  done
  if [ "$arm" = optimized ] && [ "$experiment" = exp-v2 ]; then
    "$python" -c 'import sys, urllib.request; print(urllib.request.urlopen(sys.argv[1], timeout=10).read().decode())' \
      "http://127.0.0.1:$EPP_PORT/metrics" 2>/dev/null </dev/null \
      | grep -E '^llm_d_epp_request_(error_)?total' > "$dir/epp-$tag.prom" || true
  fi
}

run_arm() {  # <arm> <run-number>
  local arm=$1 run=$2 label route dir config metrics logs resources
  if [ "$arm" = baseline ]; then label=$baseline_label route=$route_baseline; else label=$optimized_label route=$route_optimized; fi
  dir="$evidence/$arm/raw/run-$run" config="$evidence/$arm/config/run-$run.yaml"
  metrics="$evidence/$arm/metrics/run-$run" logs="$evidence/$arm/logs/run-$run"
  mkdir -p "$dir" "$metrics" "$logs"
  resources="$metrics/resources.jsonl"
  log "run $run $arm ($label, route: $route)"
  render_arm "$arm" "$arm-$run" "$api_cidrs" "$api_port" > "$config"
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
  kw -n "$NS" rollout status deployment/agentgateway --timeout=5m >> "$logs/apply.log"
  # The startup probe can take up to 30 minutes, but never past the session's time limit.
  local left=$((deadline - $(date +%s)))
  [ "$left" -gt 0 ] || die "time limit reached before vLLM started"
  [ "$left" -lt 1800 ] || left=1800
  kw -n "$NS" rollout status deployment/vllm-primary --timeout="${left}s" >> "$logs/apply.log"
  k -n "$NS" get pods -l app.kubernetes.io/name=vllm --field-selector=status.phase=Running -o json > "$logs/pods.json"
  k get nodes -l ewallet.io/bench=true -o json > "$logs/nodes.json"
  k -n "$NS" get configmap agentgateway-llm-config -o 'jsonpath={.data.config\.yaml}' > "$logs/gateway-config.yaml"
  "$python" -m ops.experiments.readback --pods "$logs/pods.json" --nodes "$logs/nodes.json" \
    --gateway-config "$logs/gateway-config.yaml" --output "$dir/controls.json"
  # Provenance is checked on what is running, before any request is sent.
  "$python" -m ops.experiments.readback --check "$dir/controls.json" --expect-digest "${VLLM_IMAGE##*@}" \
    --expect-revision "$MODEL_REVISION" || die "run $run $arm is not running the pinned image and model"
  log "run $run $arm ready: $("$python" -c 'import json,sys; c=json.load(open(sys.argv[1])); print("thread_bind=%s replicas=%s nodes=%s digest=%s" % (c["thread_bind"], c["replicas"], c["node_names"], c["image_digest"][:19]))' "$dir/controls.json")"
  kubectl --context "$KUBE_CONTEXT" -n "$NS" port-forward svc/agentgateway "$GW_PORT:4000" >> "$logs/pf-gateway.log" 2>&1 </dev/null &
  pids+=("$!")
  kubectl --context "$KUBE_CONTEXT" -n "$NS" port-forward deployment/vllm-primary "$METRICS_PORT:8000" >> "$logs/pf-metrics.log" 2>&1 </dev/null &
  pids+=("$!")
  local client_args=(--config "$label" --gateway "http://127.0.0.1:$GW_PORT" --levels "$levels" --timeout 220
    --resources "$resources" --stop-file "$stop" --output "$dir")
  local client=o1_benchmark.py epp_args=()
  if [ "$experiment" = exp-v1 ]; then
    client_args+=(--metrics "http://127.0.0.1:$METRICS_PORT" --allocated-cpu 2)
  else
    client=g2_benchmark.py
    local names
    names=$(k -n "$NS" get pods -l app.kubernetes.io/name=vllm -o 'jsonpath={.items[*].metadata.name}' | tr ' ' ,)
    client_args+=(--context "$KUBE_CONTEXT" --namespace "$NS" --pods "$names" --allocated-cpu 4)
    if [ "$arm" = optimized ]; then
      kubectl --context "$KUBE_CONTEXT" -n "$NS" port-forward svc/llmd-router-epp "$EPP_PORT:9090" >> "$logs/pf-epp.log" 2>&1 </dev/null &
      pids+=("$!")
      # The G2 client appends /metrics itself; the policy probe takes the full URL.
      client_args+=(--epp-metrics "http://127.0.0.1:$EPP_PORT")
      epp_args=(--epp-metrics "http://127.0.0.1:$EPP_PORT/metrics")
    fi
  fi
  local sampler_stop="$metrics/SAMPLER_STOP"
  "$python" -m ops.experiments.resource_sampler --context "$KUBE_CONTEXT" --namespace "$NS" --output "$resources" \
    --stop-file "$sampler_stop" </dev/null & pids+=("$!")
  sleep "${EXP_FORWARD_WAIT:-5}"
  local status=0 window_start window_end
  window_start=$(date -u +%FT%TZ)
  echo "$window_start" > "$metrics/window-start.txt"
  log "run $run $arm measuring (levels $levels); live view in another terminal: python -m ops.experiments.progress $evidence"
  EWALLET_GATEWAY_KEY=$client_key "$python" "$client_dir/$client" "${client_args[@]}" > "$logs/client.log" 2>&1 </dev/null || status=$?
  window_end=$(date -u +%FT%TZ)
  echo "$window_end" > "$metrics/window-end.txt"
  touch "$sampler_stop"
  k -n "$NS" logs deployment/agentgateway --tail=2000 2>/dev/null | grep -viE 'authorization|bearer|api[-_]key' \
    > "$logs/gateway.log" || true
  if [ "$status" != 0 ]; then
    # The clients exit 1 when any request failed. A run that measured every level is data
    # (the comparison counts its failures); anything else stops the session.
    local outcome
    if outcome=$("$python" -m ops.experiments.run_outcome --summary "$dir/summary.json" --levels "$levels" \
         --exit-code "$status" 2>&1 </dev/null); then
      log "run $run $arm: $outcome"
      deviations+=("run $run $arm: $outcome")
    else
      stop_children
      deviations+=("run $run $arm client exit $status: $outcome")
      die "run $run $arm failed ($outcome); see $logs/client.log"
    fi
  fi
  # Supporting evidence, after the measured window so it cannot disturb it.
  { k -n "$NS" get pods -o wide; k get nodes -l ewallet.io/bench=true -o wide; } > "$logs/inventory.txt" 2>&1 || true
  sleep "${EXP_SCRAPE_SETTLE:-12}"     # two scrape intervals, so the window's last samples are stored
  "$python" -m ops.experiments.prom_readback --prometheus "http://127.0.0.1:$PROM_PORT" \
    --grafana "http://127.0.0.1:$GRAFANA_PORT" --start "$window_start" --end "$window_end" --run-id "$arm-$run" \
    --output "$metrics/prometheus-readback.json" >> "$logs/readback.log" 2>&1 </dev/null || log "readback failed; see $logs/readback.log"
  cp "$metrics/prometheus-readback.json" "$evidence/observability/metric-readback/$arm-run-$run.json" 2>/dev/null || true
  local policy=""
  if [ "${EXP_POLICY_PROBE:-on}" = on ] && [ "$run" = 01 ]; then
    policy="$evidence/gateway-policy/$arm/results.json"
    mkdir -p "$evidence/gateway-policy/$arm"
    local expect=primary
    [ "$route" = "llm-d EPP" ] && expect=llm-d
    k -n "$NS" get configmap agentgateway-llm-config -o 'jsonpath={.data.config\.yaml}' \
      | grep -vE 'key:|apiKey' > "$evidence/gateway-policy/$arm/gateway-policy-config.yaml" || true
    local snaps="$evidence/gateway-policy/$arm/counters"
    probe_counters "$snaps" before
    if EWALLET_GATEWAY_KEY=$client_key EWALLET_BACKEND_KEY=$(k -n "$NS" get secret vllm-api-key -o 'jsonpath={.data.api-key}' | base64 -d | tr -d '\n') \
         "$python" -m ops.experiments.gateway_policy --gateway "http://127.0.0.1:$GW_PORT" --expect-backend "$expect" \
         "${epp_args[@]}" --output "$policy" > "$evidence/gateway-policy/$arm/probe.log" 2>&1 </dev/null; then
      log "gateway policy probe ($arm): PASS"
    else
      log "gateway policy probe ($arm): FAIL or incomplete; see gateway-policy/$arm/probe.log"
    fi
    probe_counters "$snaps" after
    log "gateway policy probe ($arm): $("$python" -m ops.experiments.probe_attribution --results "$policy" \
      --snapshots "$snaps" --output "$evidence/gateway-policy/$arm/attribution.json" 2>&1 </dev/null | tail -1)"
    k -n "$NS" logs deployment/agentgateway --since=3m 2>/dev/null | grep -viE 'authorization|bearer|api[-_]key' \
      > "$evidence/gateway-policy/$arm/gateway.log" || true
    if [ "$arm" = optimized ] && [ "$experiment" = exp-v2 ]; then
      # EPP decisions (v=3) and the Envoy sidecar, for the same three minutes.
      k -n "$NS" logs deployment/llmd-router-epp -c epp --since=3m 2>/dev/null | grep -viE 'authorization|bearer|api[-_]key' \
        | tail -n 4000 > "$evidence/gateway-policy/$arm/epp.log" || true
      k -n "$NS" logs deployment/llmd-router-epp -c envoy-proxy --since=3m 2>/dev/null \
        | grep -viE 'authorization|bearer|api[-_]key' | tail -n 2000 > "$evidence/gateway-policy/$arm/envoy.log" || true
    fi
  fi
  "$python" -m ops.experiments.checkpoint --title "${experiment^^} / ${arm^^} run $run" --experiment "${experiment^^}" \
    --arm "$arm" --label "$label" --route "$route" --run-id "$arm-$run" --kube-context "$KUBE_CONTEXT" \
    --grafana "http://127.0.0.1:$GRAFANA_PORT" --prometheus "http://127.0.0.1:$PROM_PORT" \
    --window-start "$window_start" --window-end "$window_end" --wait-minutes "$checkpoint_minutes" \
    --run-dir "$dir" --summary "$dir/summary.json" --controls "$dir/controls.json" \
    --readback "$metrics/prometheus-readback.json" ${policy:+--policy "$policy"} \
    --screenshots "$evidence/$arm/screenshots" > "$logs/checkpoint.txt"
  local answer=0
  # Every vLLM start costs about 16 min, so a formal interactive session can stop only at
  # the first run of each arm (EXP_CHECKPOINT_RUNS=first) to stay inside the time limit.
  if [ "$checkpoint_runs" = all ] || [ "$run" = 01 ]; then
    checkpoint "$logs/checkpoint.txt" || answer=$?
  fi
  stop_children
  case "$answer" in
    0) ;;
    2) deviations+=("owner aborted at the checkpoint after run $run $arm"); die "aborted by the owner";;
    *) deviations+=("no owner answer at the checkpoint after run $run $arm"); die "checkpoint timed out";;
  esac
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
