#!/usr/bin/env bash
# EXP-C5-SANDBOX: one run of the same image as a BYO Deployment or as a SandboxAgent.
# usage: sandbox_experiment.sh <baseline|optimized> <plan|check|run|down> <evidence-dir>
#
#   plan   Local only: renders the arm.
#   check  Read-only: reports whether the namespace, pool and Secrets are in place.
#   run    Needs EXP_APPROVED=yes. Creates this arm's agent in ewallet-sandbox, measures it,
#          optionally deletes the arm's own pod, and removes the agent.
#   down   Removes both arms' agents after a lost run. Leaves the namespace and pool.
#
# Env: KUBE_CONTEXT, AGENT_IMAGE (digest reference; the same in both arms), EXP_TASKS (5),
#   EXP_INIT_LIMIT (600 s), EXP_RECOVERY=yes to include the recovery measurement.
#
# Everything here happens in ewallet-sandbox, and only objects named below are touched.
# The namespace, pool and Secrets come from the approved package, not from this script.
set -euo pipefail
arm=${1:-} action=${2:-} evidence=${3:-}
die() { echo "sandbox_experiment: $*" >&2; exit 1; }
case "$arm" in baseline|optimized) ;; *) die "usage: sandbox_experiment.sh <baseline|optimized> <plan|check|run|down> <evidence-dir>";; esac
case "$action" in plan|check|run|down) ;; *) die "unknown action $action";; esac
[ -n "$evidence" ] || die "evidence directory required"
root=$(cd "$(dirname "$0")/../.." && pwd)
python=${EXP_PYTHON:-python3}
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
NS=ewallet-sandbox POOL=business-agents PORT=18383
EXPECTED_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
FIXTURE="$root/configs/experiments/fixtures/agent-load-request.json"
tasks=${EXP_TASKS:-5} init_limit=${EXP_INIT_LIMIT:-600}
[ "$tasks" -ge 1 ] && [ "$tasks" -le 20 ] || die "EXP_TASKS must be 1-20"
[ "$init_limit" -ge 30 ] && [ "$init_limit" -le 1800 ] || die "EXP_INIT_LIMIT must be 30-1800"
if [ "$arm" = baseline ]; then
  kind=agents.kagent.dev name=bench-feature-rag-byo pod_prefix=bench-feature-rag-byo
  selector=app.kubernetes.io/name=bench-feature-rag-byo
else
  kind=sandboxagents.kagent.dev name=bench-feature-rag-sandbox pod_prefix=business-agents
  selector=ate.dev/worker-pool=$POOL
fi
load_hash=$(cat "$FIXTURE" "$root/ops/experiments/sandbox_client.py" | sha256sum | cut -d' ' -f1)
render() { "$python" "$root/scripts/experiments/render_sandbox.py" "$arm" --image "${AGENT_IMAGE:-}"; }
if [ "$action" = plan ]; then
  render > /dev/null
  echo "arm=$arm kind=$kind name=$name tasks=$tasks init_limit=$init_limit load_profile_sha256=$load_hash"
  exit 0
fi
[ "${KUBE_CONTEXT:-}" = "$EXPECTED_CONTEXT" ] || die "KUBE_CONTEXT must be $EXPECTED_CONTEXT"
k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=60s -n "$NS" "$@"; }
# --request-timeout would end a long wait early, so waits go without it.
kw() { kubectl --context "$KUBE_CONTEXT" -n "$NS" "$@"; }

check() {
  local problems=0 mark
  mark=$(kubectl --context "$KUBE_CONTEXT" --request-timeout=60s get namespace "$NS" --ignore-not-found \
    -o 'jsonpath={.metadata.labels.ewallet\.io/experiment}') || die "cannot read namespaces"
  [ "$mark" = true ] || { echo "missing: namespace $NS with its experiment mark"; return 1; }
  [ "$(k get workerpool "$POOL" --ignore-not-found -o 'jsonpath={.status.replicas}')" -ge 1 ] 2>/dev/null \
    || { echo "missing: WorkerPool $POOL with a ready worker"; problems=1; }
  for secret in sandbox-llm-key sandbox-mcp-token; do
    [ -n "$(k get secret "$secret" --ignore-not-found -o name)" ] || { echo "missing: Secret $secret"; problems=1; }
  done
  return "$problems"
}
if [ "$action" = check ]; then check; echo "ready"; exit 0; fi

remove_arms() {
  local status=0
  kw delete agents.kagent.dev bench-feature-rag-byo --ignore-not-found --wait=true --timeout=3m || status=1
  # A SandboxAgent's finalizer removes its actors and template; the controller gives up after a timeout.
  kw delete sandboxagents.kagent.dev bench-feature-rag-sandbox --ignore-not-found --wait=true --timeout=5m || status=1
  return "$status"
}
if [ "$action" = down ]; then remove_arms; echo "arms removed"; exit 0; fi

[ "${EXP_APPROVED:-}" = yes ] || die "set EXP_APPROVED=yes after owner approval"
rendered=$(render) || die "render failed"
check || die "the isolated namespace is not ready; see the runbook"
[ -z "$(k get "$kind" "$name" --ignore-not-found -o name)" ] || die "$name already exists; run down first"
! ss -ltnH "( sport = :$PORT )" 2>/dev/null | grep -q . || die "local port $PORT is in use"
mkdir -p "$evidence/$arm"/{config,raw,metrics,logs,screenshots}
evidence=$(cd "$evidence" && pwd)
run=1
while [ -e "$evidence/$arm/raw/run-$(printf '%02d' "$run")" ]; do run=$((run + 1)); done
run=$(printf '%02d' "$run")
dir="$evidence/$arm/raw/run-$run" logs="$evidence/$arm/logs/run-$run"
config="$evidence/$arm/config/run-$run.yaml" samples="$evidence/$arm/metrics/run-$run-resources.jsonl"
mkdir -p "$dir" "$logs"
spec="$root/configs/experiments/exp-c5-sandbox.json"
if [ ! -f "$evidence/manifest.json" ]; then
  "$python" -m ops.experiments.cli preflight --spec "$spec" --dir "$evidence" --kube-context "$KUBE_CONTEXT" --namespace "$NS" \
    --setting "agent_image=$AGENT_IMAGE" --setting "tasks=$tasks" --setting "init_limit=$init_limit"
fi
printf '%s\n' "$rendered" > "$config"
pids=() deviations=()
cleanup() {
  local status=$?
  set +e
  trap - EXIT INT TERM HUP
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null; done
  k get "$kind" "$name" -o yaml 2>/dev/null | grep -viE 'token|api[-_]key|password' > "$logs/final-object.yaml"
  remove_arms >> "$logs/cleanup.log" 2>&1 || { echo "sandbox_experiment: CLEANUP FAILED, run down" >&2; status=1; }
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

touch "$logs/client.log"
"$python" -m ops.experiments.resource_sampler --context "$KUBE_CONTEXT" --namespace "$NS" --output "$samples" \
  --stop-file "$logs/STOP" & pids+=("$!")
applied_at=$(date +%s)
kubectl --context "$KUBE_CONTEXT" apply -f "$config" >> "$logs/apply.log"
ready_at=""
if kw wait --for=condition=Ready "$kind/$name" --timeout="${init_limit}s" >> "$logs/apply.log" 2>&1; then
  ready_at=$(date +%s)
else
  deviations+=(--deviation "$arm run $run: $name was not Ready within ${init_limit}s")
  k describe "$kind" "$name" > "$logs/not-ready.txt" 2>&1 || true
fi
failure_at=""
if [ -n "$ready_at" ]; then
  kubectl --context "$KUBE_CONTEXT" -n kagent port-forward svc/kagent-controller "$PORT:8083" >> "$logs/pf.log" 2>&1 & pids+=("$!")
  sleep "${EXP_FORWARD_WAIT:-3}"
  url="http://127.0.0.1:$PORT/api/a2a/$NS/$name/"
  client=("$python" -m ops.experiments.sandbox_client --url "$url" --fixture "$FIXTURE" --run-id "$arm-$run-$applied_at"
    --limit "$init_limit")
  "${client[@]}" --tasks "$tasks" >> "$logs/client.log" 2>"$logs/client.err" \
    || deviations+=(--deviation "$arm run $run: agent never answered")
  if [ "${EXP_RECOVERY:-}" = yes ]; then
    victim=$(k get pods -l "$selector" -o 'jsonpath={.items[0].metadata.name}')
    [ -n "$victim" ] || die "no $NS pod to delete for recovery"
    failure_at=$(date +%s)
    k delete pod "$victim" --wait=false >> "$logs/failure.log"
    "${client[@]}" --after-failure >> "$logs/client.log" 2>>"$logs/client.err" \
      || deviations+=(--deviation "$arm run $run: no recovery within the limit")
  fi
fi
touch "$logs/STOP"
"$python" -m ops.experiments.sandbox_summary --log "$logs/client.log" --samples "$samples" --fixture "$FIXTURE" \
  --applied-at "$applied_at" ${ready_at:+--ready-at "$ready_at"} ${failure_at:+--failure-at "$failure_at"} \
  --workload-sha256 "$load_hash" --arm-prefix "$pod_prefix" --output "$dir/summary.json"
"$python" - "$dir/controls.json" "$load_hash" "${AGENT_IMAGE##*@}" "$NS" <<'P'
import json, sys
path, load, digest, namespace = sys.argv[1:5]
controls = {"load_profile_sha256": load, "image_digest": digest, "tool_set": "fixture:agent-load-request", "namespace": namespace}
with open(path, "w", encoding="utf-8") as stream:
    json.dump(controls, stream, indent=2, sort_keys=True)
P
"$python" -m ops.experiments.cli finish --spec "$spec" --dir "$evidence" "${deviations[@]}"
echo "run $run complete: $dir"
