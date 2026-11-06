#!/usr/bin/env bash
# One measured run of EXP-C5-KEDA or EXP-C5-MCP against the live platform.
# usage: agent_experiment.sh <exp-c5-keda|exp-c5-mcp-failover> <baseline|optimized> <plan|run> <evidence-dir>
#
#   plan  Local only: prints the load profile. Touches no cluster.
#   run   Needs EXP_APPROVED=yes. EXP-C5-MCP also needs EXP_DISRUPT_APPROVED=yes, because
#         it deletes one production MCP pod.
#
# This script measures; it does not switch arms. The owner applies the approved Helm change
# first, and the run stops unless the cluster is in the state the arm requires. Load objects
# are removed on every exit. Run numbers only go up, so a run never replaces another.
#
# Env: KUBE_CONTEXT, EXP_CONCURRENCY (4), EXP_DURATION (300 s), EXP_MAX_REQUESTS (40),
#   EXP_FAILURE_AFTER (60 s), EXP_SCALE_IN_WAIT (420 s).
set -euo pipefail
experiment=${1:-} arm=${2:-} action=${3:-} evidence=${4:-}
die() { echo "agent_experiment: $*" >&2; exit 1; }
case "$experiment" in exp-c5-keda|exp-c5-mcp-failover) ;; *) die "unknown experiment $experiment";; esac
case "$arm" in baseline|optimized) ;; *) die "arm must be baseline or optimized";; esac
case "$action" in plan|run) ;; *) die "action must be plan or run";; esac
[ -n "$evidence" ] || die "evidence directory required"
root=$(cd "$(dirname "$0")/../.." && pwd)
python=${EXP_PYTHON:-python3}
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
NS=ewallet-agents TARGET=fraud-coordinator SPECIALIST=feature-rag-specialist MCP=feature-rag-mcp MCP_NS=ewallet-mcp
EXPECTED_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
concurrency=${EXP_CONCURRENCY:-4} duration=${EXP_DURATION:-300} max_requests=${EXP_MAX_REQUESTS:-40}
failure_after=${EXP_FAILURE_AFTER:-60} scale_in_wait=${EXP_SCALE_IN_WAIT:-420}
[ "$concurrency" -ge 1 ] && [ "$concurrency" -le 8 ] || die "EXP_CONCURRENCY must be 1-8"
[ "$duration" -ge 1 ] && [ "$duration" -le 900 ] || die "EXP_DURATION must be 1-900"
[ "$max_requests" -ge 1 ] && [ "$max_requests" -le 100 ] || die "EXP_MAX_REQUESTS must be 1-100"
profile=(--concurrency "$concurrency" --duration "$duration" --max-requests "$max_requests")
load_hash=$("$python" "$root/scripts/experiments/render_load.py" "${profile[@]}" --print-hash)
if [ "$action" = plan ]; then
  echo "experiment=$experiment arm=$arm concurrency=$concurrency duration=$duration max_requests=$max_requests load_profile_sha256=$load_hash"
  exit 0
fi
[ "${KUBE_CONTEXT:-}" = "$EXPECTED_CONTEXT" ] || die "KUBE_CONTEXT must be $EXPECTED_CONTEXT"
[ "${EXP_APPROVED:-}" = yes ] || die "set EXP_APPROVED=yes after owner approval"
if [ "$experiment" = exp-c5-mcp-failover ] && [ "${EXP_DISRUPT_APPROVED:-}" != yes ]; then
  die "this run deletes a production MCP pod; set EXP_DISRUPT_APPROVED=yes after owner approval"
fi
k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=60s -n "$NS" "$@"; }
# --request-timeout would end a long wait early, so waits go without it.
kw() { kubectl --context "$KUBE_CONTEXT" -n "$NS" "$@"; }
km() { kubectl --context "$KUBE_CONTEXT" --request-timeout=60s -n "$MCP_NS" "$@"; }
exists() { k get "$1" "$2" >/dev/null 2>&1; }

# The cluster must already be in this arm's state, or the measurement belongs to another arm.
if [ "$experiment" = exp-c5-keda ]; then
  scaled_object=no hpa=no
  if k get scaledobjects.keda.sh "$TARGET" >/dev/null 2>&1; then scaled_object=yes; fi
  if exists hpa "$TARGET"; then hpa=yes; fi
  # KEDA's own HPA is named keda-hpa-<target>, so an HPA under the plain name is independent.
  if [ "$arm" = baseline ]; then
    [ "$hpa" = yes ] && [ "$scaled_object" = no ] || die "baseline needs the HPA and no ScaledObject"
  else
    [ "$hpa" = no ] && [ "$scaled_object" = yes ] || die "optimized needs a ScaledObject and no independent HPA"
  fi
else
  # Both arms run one image; retry is switched by configuration, so only behaviour differs.
  want=1 attempts=1
  [ "$arm" = baseline ] || { want=2 attempts=3; }
  [ "$(km get deployment "$MCP" -o jsonpath='{.spec.replicas}')" = "$want" ] || die "$arm needs $want $MCP replicas"
  set_attempts=$(k get deployment "$SPECIALIST" -o \
    'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="EWALLET_MCP_MAX_ATTEMPTS")].value}')
  # Unset means the client default of 3.
  [ "${set_attempts:-3}" = "$attempts" ] || die "$arm needs EWALLET_MCP_MAX_ATTEMPTS=$attempts on $SPECIALIST"
fi

mkdir -p "$evidence/$arm"/{config,raw,metrics,logs,screenshots}
evidence=$(cd "$evidence" && pwd)
run=1
while [ -e "$evidence/$arm/raw/run-$(printf '%02d' "$run")" ]; do run=$((run + 1)); done
run=$(printf '%02d' "$run")
dir="$evidence/$arm/raw/run-$run" config="$evidence/$arm/config/run-$run.json" logs="$evidence/$arm/logs/run-$run"
replicas="$evidence/$arm/metrics/run-$run-replicas.jsonl"
mkdir -p "$dir" "$logs"
sampler="" failure_at="" deviations=()
spec="$root/configs/experiments/$experiment.json"
if [ ! -f "$evidence/manifest.json" ]; then
  "$python" -m ops.experiments.cli preflight --spec "$spec" --dir "$evidence" --kube-context "$KUBE_CONTEXT" \
    --namespace "$NS" --setting "load_profile_sha256=$load_hash" --setting "concurrency=$concurrency" \
    --setting "duration=$duration" --setting "max_requests=$max_requests"
fi
cleanup() {
  local status=$?
  set +e
  trap - EXIT INT TERM HUP
  [ -n "$sampler" ] && kill "$sampler" 2>/dev/null
  kw delete job,configmap,serviceaccount,networkpolicy,authorizationpolicy -l ewallet.io/experiment-load=true \
    --ignore-not-found --wait=true --timeout=120s >> "$logs/cleanup.log" 2>&1 || status=1
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

image=$(k get deployment "$TARGET" -o jsonpath='{.spec.template.spec.containers[0].image}')
"$python" "$root/scripts/experiments/render_load.py" --image "$image" --run-id "$experiment-$arm-$run-$(date -u +%s)" \
  "${profile[@]}" > "$config"
"$python" -m ops.experiments.replica_sampler --context "$KUBE_CONTEXT" --namespace "$NS" --deployment "$TARGET" \
  --output "$replicas" & sampler=$!
k apply -f "$config" >> "$logs/apply.log"
# A short Job can finish before it is seen Ready, so only the Job's own result below decides.
kw wait --for=condition=Ready pod -l ewallet.io/experiment-load=true --timeout=180s >> "$logs/apply.log" 2>&1 \
  || echo "load pod was not seen Ready" >> "$logs/apply.log"
if [ "$experiment" = exp-c5-mcp-failover ]; then
  sleep "$failure_after"
  victim=$(km get pods -l "app.kubernetes.io/name=$MCP" -o 'jsonpath={.items[0].metadata.name}')
  [ -n "$victim" ] || die "no $MCP pod to delete"
  failure_at=$(date +%s)
  km delete pod "$victim" --wait=false >> "$logs/failure.log"
fi
if ! kw wait --for=condition=complete job/agent-load --timeout="$((duration + 600))s" >> "$logs/apply.log" 2>&1; then
  echo "job did not complete" >> "$logs/apply.log"
  deviations+=(--deviation "$arm run $run: load Job did not complete")
fi
k logs job/agent-load > "$logs/load.log"
if [ "$experiment" = exp-c5-keda ]; then sleep "$scale_in_wait"; fi
kill "$sampler" 2>/dev/null || true
sampler=""
"$python" -m ops.experiments.agent_summary --log "$logs/load.log" --replicas "$replicas" --workload-sha256 "$load_hash" \
  ${failure_at:+--failure-at "$failure_at"} --output "$dir/summary.json"
# Controls are read back from the cluster. Replicas are EXP-C5-MCP's variable, so a control
# here is the agent's scaling range, not the MCP replica count.
# KEDA owns keda-hpa-<target> once installed, whichever experiment is running.
hpa_name=$TARGET
if exists hpa "keda-hpa-$TARGET"; then hpa_name=keda-hpa-$TARGET; fi
min_replicas=$(k get hpa "$hpa_name" -o 'jsonpath={.spec.minReplicas}')
max_replicas=$(k get hpa "$hpa_name" -o 'jsonpath={.spec.maxReplicas}')
node_names=$(k get pods -l "ewallet.io/agent=$TARGET" -o 'jsonpath={.items[*].spec.nodeName}')
# shellcheck disable=SC2086  # one argument per node name
node_type=$(kubectl --context "$KUBE_CONTEXT" get nodes $node_names \
  -o 'jsonpath={.items[*].metadata.labels.node\.kubernetes\.io/instance-type}' | tr ' ' '\n' | sort -u | paste -sd,)
specialist_image=$(k get deployment "$SPECIALIST" -o jsonpath='{.spec.template.spec.containers[0].image}')
"$python" - "$dir/controls.json" "$load_hash" "${image##*@},${specialist_image##*@}" "$min_replicas" "$max_replicas" "$node_type" \
  "${failure_at:+delete-one-pod}" <<'P'
import json, sys
path, load, digest, low, high, node_type, failure = sys.argv[1:8]
controls = {"load_profile_sha256": load, "image_digest": digest, "min_replicas": low, "max_replicas": high,
            "node_type": node_type, "failure_action": failure or "none", "tool_set": "fixture:agent-load-request"}
with open(path, "w", encoding="utf-8") as stream:
    json.dump(controls, stream, indent=2, sort_keys=True)
P
"$python" -m ops.experiments.cli finish --spec "$spec" --dir "$evidence" "${deviations[@]}"
echo "run $run complete: $dir"
