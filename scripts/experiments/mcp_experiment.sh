#!/usr/bin/env bash
# EXP-C5-MCP: one measured run of MCP failover, fully isolated from production.
# usage: mcp_experiment.sh <baseline|optimized> <plan|run|down> <evidence-dir>
#
#   plan  Local only: renders the arm and prints the load profile.
#   run   Needs EXP_APPROVED=yes. Creates namespace ewallet-mcp-exp, runs the probe, deletes
#         one pod of that namespace mid-run, then removes the namespace.
#   down  Removes the namespace after a lost run.
#
# Env: KUBE_CONTEXT, MCP_IMAGE and PROBE_IMAGE (digest references; the same in both arms),
#   EXP_DURATION (120 s), EXP_RATE (2 calls/s), EXP_FAILURE_AFTER (40 s).
#
# This script never deletes anything outside ewallet-mcp-exp. Production ewallet-mcp pods
# and their revisions are read before and after, and a difference fails the run.
set -euo pipefail
arm=${1:-} action=${2:-} evidence=${3:-}
die() { echo "mcp_experiment: $*" >&2; exit 1; }
case "$arm" in baseline|optimized) ;; *) die "usage: mcp_experiment.sh <baseline|optimized> <plan|run|down> <evidence-dir>";; esac
case "$action" in plan|run|down) ;; *) die "unknown action $action";; esac
[ -n "$evidence" ] || die "evidence directory required"
root=$(cd "$(dirname "$0")/../.." && pwd)
python=${EXP_PYTHON:-python3}
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
NS=ewallet-mcp-exp PROD_NS=ewallet-mcp
EXPECTED_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
duration=${EXP_DURATION:-120} rate=${EXP_RATE:-2} failure_after=${EXP_FAILURE_AFTER:-40}
[ "$duration" -ge 1 ] && [ "$duration" -le 600 ] || die "EXP_DURATION must be 1-600"
[ "$failure_after" -lt "$duration" ] || die "EXP_FAILURE_AFTER must fall inside the run"
load_hash=$("$python" "$root/scripts/experiments/render_mcp.py" "$arm" --duration "$duration" --rate "$rate" --print-hash)
render() {
  "$python" "$root/scripts/experiments/render_mcp.py" "$arm" --mcp-image "${MCP_IMAGE:-}" --probe-image "${PROBE_IMAGE:-}" \
    --release-sha "$(git -C "$root" rev-parse HEAD)" --duration "$duration" --rate "$rate"
}
if [ "$action" = plan ]; then
  render > /dev/null
  echo "arm=$arm duration=$duration rate=$rate failure_after=$failure_after load_profile_sha256=$load_hash"
  exit 0
fi
[ "${KUBE_CONTEXT:-}" = "$EXPECTED_CONTEXT" ] || die "KUBE_CONTEXT must be $EXPECTED_CONTEXT"
k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=60s -n "$NS" "$@"; }
# --request-timeout would end a long wait early, so waits go without it.
kw() { kubectl --context "$KUBE_CONTEXT" -n "$NS" "$@"; }
kc() { kubectl --context "$KUBE_CONTEXT" --request-timeout=60s "$@"; }

remove_namespace() {
  local found
  found=$(kc get namespace "$NS" --ignore-not-found -o 'jsonpath={.metadata.labels.ewallet\.io/experiment}') \
    || { echo "mcp_experiment: cannot read $NS" >&2; return 1; }
  [ -n "$found" ] || return 0
  [ "$found" = true ] || { echo "mcp_experiment: $NS is not an experiment namespace" >&2; return 1; }
  kubectl --context "$KUBE_CONTEXT" delete namespace "$NS" --wait=true --timeout=5m
}
if [ "$action" = down ]; then remove_namespace; echo "namespace removed"; exit 0; fi

[ "${EXP_APPROVED:-}" = yes ] || die "set EXP_APPROVED=yes after owner approval"
rendered=$(render) || die "render failed"
existing=$(kc get namespace "$NS" --ignore-not-found -o name) || die "cannot read namespaces"
[ -z "$existing" ] || die "$NS already exists; run down first"
production_state() {
  kc -n "$PROD_NS" get pods -o 'jsonpath={range .items[*]}{.metadata.uid}{" "}{.status.containerStatuses[0].restartCount}{"\n"}{end}' | sort
  kc -n "$PROD_NS" get deployments -o 'jsonpath={range .items[*]}{.metadata.name}{" "}{.metadata.generation}{"\n"}{end}' | sort
}
mkdir -p "$evidence/$arm"/{config,raw,metrics,logs,screenshots}
evidence=$(cd "$evidence" && pwd)
run=1
while [ -e "$evidence/$arm/raw/run-$(printf '%02d' "$run")" ]; do run=$((run + 1)); done
run=$(printf '%02d' "$run")
dir="$evidence/$arm/raw/run-$run" logs="$evidence/$arm/logs/run-$run"
config="$evidence/$arm/config/run-$run.yaml" replicas="$evidence/$arm/metrics/run-$run-replicas.jsonl"
mkdir -p "$dir" "$logs"
spec="$root/configs/experiments/exp-c5-mcp-failover.json"
if [ ! -f "$evidence/manifest.json" ]; then
  "$python" -m ops.experiments.cli preflight --spec "$spec" --dir "$evidence" --kube-context "$KUBE_CONTEXT" \
    --namespace "$NS" --setting "mcp_image=$MCP_IMAGE" --setting "probe_image=$PROBE_IMAGE" \
    --setting "duration=$duration" --setting "rate=$rate" --setting "failure_after=$failure_after"
fi
production_state > "$logs/production-before.txt"
printf '%s\n' "$rendered" > "$config"
sampler="" deviations=()
cleanup() {
  local status=$?
  set +e
  trap - EXIT INT TERM HUP
  [ -n "$sampler" ] && kill "$sampler" 2>/dev/null
  remove_namespace >> "$logs/cleanup.log" 2>&1 || { echo "mcp_experiment: CLEANUP FAILED, run down" >&2; status=1; }
  production_state > "$logs/production-after.txt"
  if ! cmp -s "$logs/production-before.txt" "$logs/production-after.txt"; then
    echo "mcp_experiment: PRODUCTION STATE CHANGED during this run" >&2
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

# The first document is the Namespace; the Secret must exist before the workloads.
awk '/^---$/ {exit} {print}' "$config" | kc apply -f - >> "$logs/apply.log"
token=$(openssl rand -hex 32)
tokens=$(printf '{"version":1,"credentials":[{"id":"mcp-exp","subject":"mcp-exp","token":"%s","status":"active"}]}' "$token")
k create secret generic mcp-exp-auth --from-file=token=/dev/stdin <<<"$token" >/dev/null
k patch secret mcp-exp-auth --type merge --patch-file=/dev/stdin >/dev/null <<<"{\"stringData\":{\"tokens\":$(printf '%s' "$tokens" | "$python" -c 'import json,sys;print(json.dumps(sys.stdin.read()))')}}"
unset token tokens
kc apply -f "$config" >> "$logs/apply.log"
kw rollout status deployment/mcp-exp-drift --timeout=5m >> "$logs/apply.log"
"$python" -m ops.experiments.replica_sampler --context "$KUBE_CONTEXT" --namespace "$NS" --deployment mcp-exp-drift \
  --output "$replicas" --interval 1 & sampler=$!
kw wait --for=condition=Ready pod -l app.kubernetes.io/name=mcp-exp-probe --timeout=120s >> "$logs/apply.log" 2>&1 \
  || echo "probe pod was not seen Ready" >> "$logs/apply.log"
sleep "$failure_after"
victim=$(k get pods -l app.kubernetes.io/name=mcp-exp-drift -o 'jsonpath={.items[0].metadata.name}')
[ -n "$victim" ] || die "no pod to delete"
failure_at=$(date +%s)
k delete pod "$victim" --wait=false >> "$logs/failure.log"
if ! kw wait --for=condition=complete job/mcp-exp-probe --timeout="$((duration + 120))s" >> "$logs/apply.log" 2>&1; then
  deviations+=(--deviation "$arm run $run: probe Job did not complete")
fi
k logs job/mcp-exp-probe > "$logs/probe.log"
kill "$sampler" 2>/dev/null || true
sampler=""
"$python" -m ops.experiments.agent_summary --log "$logs/probe.log" --replicas "$replicas" --workload-sha256 "$load_hash" \
  --failure-at "$failure_at" --output "$dir/summary.json"
pod_image() {
  k get "$1" -o 'jsonpath={.spec.template.spec.containers[0].image}'
}
"$python" - "$dir/controls.json" "$load_hash" "$(pod_image deployment/mcp-exp-drift)" "$(pod_image job/mcp-exp-probe)" <<'P'
import json, sys
path, load, server, probe = sys.argv[1:5]
controls = {"load_profile_sha256": load, "image_digest": server.rsplit("@", 1)[-1] + "," + probe.rsplit("@", 1)[-1],
            "failure_action": "delete-one-pod", "tool_set": "tools/list"}
with open(path, "w", encoding="utf-8") as stream:
    json.dump(controls, stream, indent=2, sort_keys=True)
P
"$python" -m ops.experiments.cli finish --spec "$spec" --dir "$evidence" "${deviations[@]}"
echo "run $run complete: $dir"
