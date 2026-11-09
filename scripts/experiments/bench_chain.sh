#!/usr/bin/env bash
# Unattended EXP-V1 -> EXP-V2 formal rehearsal chain (owner approval 2026-11-09).
# usage: bench_chain.sh <campaign-dir relative to the worktree> <exp-v1 systemd unit>
#
# Started as its own systemd user unit, so it does not depend on an editor or terminal.
#  1. Waits until the running EXP-V1 unit has ended; it never touches that unit.
#  2. Starts EXP-V2 only if every gate passes: runner exit 0; ops.experiments.chain_gate
#     (3 runs per arm, AB/BA/AB, every level, raw artifacts, valid comparison, cleanup
#     file, production files equal); live cloud reads (only cpu-pool, no namespace, no
#     bench VM or disk, Helm releases equal to EXP-V1's production-before, no armed bench
#     timer, no 130xx port-forward); quota for 2 more nodes; EXP-V2 plan fits; estimated
#     campaign spend including EXP-V2's worst case <= USD 5.
#  3. Runs EXP-V2 once. A marker directory created with mkdir (atomic) guards against a
#     second start, and so does the runner, which refuses an existing evidence directory.
#  4. Gates EXP-V2 the same way, and runs `down` only if leftovers are found.
# It never retries a session. Logs go to ~/.local/share/ewallet-bench and are copied into
# the campaign directory at the end. Evidence is committed locally; nothing is pushed.
set -u

campaign=$1 v1_unit=$2
root=$(cd "$(dirname "$0")/../.." && pwd)
state=$HOME/.local/share/ewallet-bench
log_file=$state/chain-$(basename "$campaign").log
marker=$state/chain-$(basename "$campaign").exp-v2-started
spent_before_usd=${CHAIN_SPENT_BEFORE_USD:-1.24}   # interrupted attempt 1 (~0.31) + EXP-V1 worst case (0.93)
project=ewallet-data-ai-platform-hg26 cluster=ewallet-dev zone=asia-southeast1-b
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/ewallet-n15-dns-config}
export KUBE_CONTEXT=${KUBE_CONTEXT:-gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev}
mkdir -p "$state"
exec >>"$log_file" 2>&1
cd "$root" || exit 90

log() { echo "$(date -u +%FT%TZ) $*"; }
blocked() { log "BLOCKED: $*"; finish 1; }
finish() {
  cp "$log_file" "$campaign/chain.log" 2>/dev/null || true
  cp "$state"/chain-gate-*.json "$campaign/" 2>/dev/null || true
  if [ -n "$(git status --porcelain -- "$campaign")" ]; then
    git add -- "$campaign" && git commit -q -m "docs(evidence): formal rehearsal chain record ($(basename "$campaign"))" -- "$campaign" \
      && log "evidence committed $(git rev-parse --short HEAD)"
  fi
  exit "$1"
}

live_clean() {  # <session dir>: the cloud holds nothing of the benchmark and production is as before
  local session=$1 pools ns vms disks
  pools=$(gcloud container node-pools list --cluster "$cluster" --zone "$zone" --project "$project" --format='value(name)') \
    || { log "node-pool read failed"; return 1; }
  [ "$pools" = cpu-pool ] || { log "node pools: $(echo $pools)"; return 1; }
  ns=$(kubectl --context "$KUBE_CONTEXT" get namespace ewallet-bench 2>&1)
  grep -q NotFound <<<"$ns" || { log "namespace check: $ns"; return 1; }
  vms=$(gcloud compute instances list --project "$project" --format='value(name)') || { log "VM read failed"; return 1; }
  disks=$(gcloud compute disks list --project "$project" --format='value(name)') || { log "disk read failed"; return 1; }
  if grep -q bench-pool <<<"$vms$disks"; then log "bench VM or disk left"; return 1; fi
  if systemctl --user list-timers --all 2>/dev/null | grep -q ewallet-bench-down; then log "bench timer armed"; return 1; fi
  local p
  for p in $(pgrep -x kubectl); do
    if tr '\0' ' ' </proc/"$p"/cmdline 2>/dev/null | grep -qE 'port-forward.* 13[0-9]{3}:'; then log "port-forward left: pid $p"; return 1; fi
  done
  local helm_now
  helm_now=$(helm list --kube-context "$KUBE_CONTEXT" --all-namespaces --output json) || { log "helm read failed"; return 1; }
  [ "$helm_now" = "$(head -1 "$session/production-before.txt")" ] || { log "Helm releases differ from production-before"; return 1; }
  log "live cleanup and production: OK ($session)"
}

gate_session() {  # <experiment> <levels>
  local e=$1 levels=$2 session=$campaign/$1
  grep -q '^runner exit 0 ' "$campaign/$e.terminal.log" || { log "$e: runner exit is not 0: $(grep '^runner exit' "$campaign/$e.terminal.log")"; return 1; }
  python3 -m ops.experiments.chain_gate --session "$session" --levels "$levels" --runs 3 \
    --output "$state/chain-gate-$e.json" || return 1
  live_clean "$session"
}

log "chain start: campaign $campaign, waiting for $v1_unit"
while systemctl --user is-active --quiet "$v1_unit"; do sleep 60; done
log "$v1_unit is no longer active ($(systemctl --user show "$v1_unit" -p Result --value 2>/dev/null))"
for _ in $(seq 180); do   # the launcher writes its exit line right after the unit's main process ends
  grep -q '^runner exit ' "$campaign/exp-v1.terminal.log" 2>/dev/null && break
  sleep 10
done

if ! gate_session exp-v1 1,2,4; then
  live_clean "$campaign/exp-v1" || {
    log "leftovers found: running down once"
    scripts/experiments/vllm_bench.sh exp-v1 down "$state/down-exp-v1-$(date +%s)" || log "down failed: check by hand"
  }
  blocked "EXP-V1 did not pass the gate; EXP-V2 not started"
fi

# Capacity, time and budget for EXP-V2.
quota=$(gcloud compute project-info describe --project "$project" --format=json | python3 -c '
import json, sys
q = {x["metric"]: x for x in json.load(sys.stdin)["quotas"]}
c = q["CPUS_ALL_REGIONS"]
print("ok" if c["usage"] + 8 <= c["limit"] else "short", c["usage"], c["limit"])') || blocked "quota read failed"
[ "${quota%% *}" = ok ] || blocked "CPU quota: $quota"
plan_dir=$state/plan-exp-v2-$(date +%s)
EXP_MAX_MINUTES=215 VLLM_IMAGE=${VLLM_IMAGE:?} scripts/experiments/vllm_bench.sh exp-v2 plan "$plan_dir" >"$plan_dir.log" 2>&1 \
  || blocked "exp-v2 plan failed"
budget=$(python3 - "$plan_dir/plan/cost-plan.json" "$spent_before_usd" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1]))
total = float(sys.argv[2]) + plan["worst_case_usd"]
print(("ok" if plan["fits"] and not plan["problems"] and total <= 5.0 else "over"), plan["worst_case_usd"], round(total, 2))
PY
) || blocked "cost plan unreadable"
[ "${budget%% *}" = ok ] || blocked "budget: $budget"
log "EXP-V2 preflight OK: quota $quota; worst case and campaign total $budget"

# Idempotency, then a clean tree with the EXP-V1 evidence committed.
[ ! -e "$marker" ] || blocked "EXP-V2 was already started once ($marker)"
[ ! -e "$campaign/exp-v2" ] || blocked "$campaign/exp-v2 already exists"
outside=$(git status --porcelain --untracked-files=all | grep -v " $campaign/" | grep -v " ${campaign%/}$" || true)
[ -z "$outside" ] || blocked "changes outside the campaign directory: $outside"
git add -- "$campaign" && git commit -q -m "docs(evidence): EXP-V1 formal rehearsal (unattended, 3 runs per arm)" -- "$campaign" \
  && log "EXP-V1 evidence committed $(git rev-parse --short HEAD)"
[ -z "$(git status --porcelain --untracked-files=all)" ] || blocked "tree not clean after commit"

mkdir "$marker" 2>/dev/null || blocked "EXP-V2 was already started once ($marker)"
log "starting EXP-V2"
"$state/run-formal.sh" exp-v2; v2_status=$?
log "EXP-V2 runner exit $v2_status"
if gate_session exp-v2 1,2,4,8; then
  log "EXP-V2 passed the gate"
  finish 0
fi
live_clean "$campaign/exp-v2" || {
  log "leftovers found: running down once"
  scripts/experiments/vllm_bench.sh exp-v2 down "$state/down-exp-v2-$(date +%s)" || log "down failed: check by hand"
}
blocked "EXP-V2 did not pass the gate"
