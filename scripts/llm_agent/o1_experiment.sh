#!/usr/bin/env bash
# O1 matched vLLM CPU experiments on the shared GKE pool (docs/llm_agent/o1_runbook.md).
# usage: o1_experiment.sh <evidence-dir>
#
# Env: KUBE_CONTEXT (ewallet-dev), DIGESTS (pushed vllm-cpu-model digest json),
#   O1_CONFIGS (default b0-prime,c1-thread-bind,c2-batched-tokens,c3-no-prefix-cache),
#   O1_APPROVED=yes (owner approval of this run: 2 temporary nodes, labels, deploys,
#   test pods, failover, deletion of exactly those 2 nodes afterwards).
#
# Protection of the shared pool:
#  - MLOps health snapshot before; the run stops unless it is healthy;
#  - the pool grows by exactly 2 nodes, labelled ewallet.io/o1-llm=true; all LLM
#    pods carry that nodeSelector (o1/placement.yaml), no tolerations/PriorityClass;
#  - capacity check: every O1 request (primary + fallback + gateways) fits the
#    labelled nodes' safe budget, else BLOCKED_INSUFFICIENT_SAFE_CAPACITY;
#  - ops/llm_agent/o1_guard.py samples every 15 s and writes STOP on a breach;
#    the benchmark stops immediately and the run ends;
#  - cleanup (always: success, failure, signal) uninstalls the release, drains and
#    deletes exactly the 2 labelled instances (never a resize that may pick MLOps
#    nodes), then reads back the pool size, the MLOps node names and their health.
set -euo pipefail
evidence=${1:?usage: o1_experiment.sh <evidence-dir>}
: "${KUBE_CONTEXT:?KUBE_CONTEXT required}" "${DIGESTS:?DIGESTS required}"
[ "${O1_APPROVED:-}" = yes ] || { echo "o1: set O1_APPROVED=yes after owner approval" >&2; exit 2; }
PROJECT=ewallet-data-ai-platform-hg26 ZONE=asia-southeast1-b CLUSTER=ewallet-dev POOL=cpu-pool NS=ewallet-agents
LABEL=ewallet.io/o1-llm
configs=${O1_CONFIGS:-b0-prime,c1-thread-bind,c2-batched-tokens,c3-no-prefix-cache}
root=$(cd "$(dirname "$0")/../.." && pwd)
chart="$root/infra/helm/llm-agent-platform"
op="$root/scripts/llm_agent/g1_operator.sh"
mkdir -p "$evidence/o1" "$evidence/ops" "$evidence/state"
evidence=$(cd "$evidence" && pwd)
export G1_APPROVED_STAGES=scale-up,istio,namespace,secret,netpol-verify,failover
k() { kubectl --context "$KUBE_CONTEXT" --request-timeout=60s "$@"; }
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$evidence/o1.log"; }
die() { log "FAIL: $*"; exit 1; }
api_key() { k -n "$NS" get secret "$1" -o jsonpath='{.data.api-key}' | base64 -d; }
pool_size() {
  gcloud compute instance-groups managed list --project "$PROJECT" --filter="name~^gke-${CLUSTER}-${POOL}-" \
    --format='value(targetSize)' | awk '{s+=$1} END {print s+0}'
}
guard_pid="" pfs=()
log "o1 start git=$(git -C "$root" rev-parse HEAD) configs=$configs"

cleanup() {
  local status=$?
  set +e
  trap - EXIT INT TERM HUP
  log "cleanup (status $status)"
  for pid in "${pfs[@]}"; do kill "$pid" 2>/dev/null; done
  [ -n "${deploy_pid:-}" ] && pkill -TERM -P "$deploy_pid" 2>/dev/null && kill "$deploy_pid" 2>/dev/null
  [ -n "$guard_pid" ] && kill "$guard_pid" 2>/dev/null
  helm uninstall llm-agent --kube-context "$KUBE_CONTEXT" --namespace default --wait --timeout 10m \
    >> "$evidence/o1.log" 2>&1
  k delete namespace g1-netpol-probe --ignore-not-found --wait=false >/dev/null 2>&1
  mapfile -t o1_nodes < <(k get nodes -l "$LABEL=true" -o name 2>/dev/null | sed 's|node/||')
  if [ "${#o1_nodes[@]}" -gt 0 ]; then
    for node in "${o1_nodes[@]}"; do
      k drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout=5m >> "$evidence/o1.log" 2>&1
    done
    mig=$(gcloud container node-pools describe "$POOL" --cluster "$CLUSTER" --zone "$ZONE" --project "$PROJECT" \
      --format='value(instanceGroupUrls)' | tr ';' '\n' | head -1 | sed 's|.*/||')
    gcloud compute instance-groups managed delete-instances "$mig" --zone "$ZONE" --project "$PROJECT" \
      --instances="$(IFS=,; echo "${o1_nodes[*]}")" --quiet >> "$evidence/o1.log" 2>&1
  fi
  # Deleted instances keep their Node objects while the VMs stop: wait (<= 10 min)
  # until no labelled node is left before reading the pool back.
  for _ in $(seq 1 60); do
    [ -z "$(k get nodes -l "$LABEL=true" -o name 2>/dev/null)" ] && break
    sleep 10
  done
  [ -z "$(k get nodes -l "$LABEL=true" -o name 2>/dev/null)" ] || { log "O1 nodes still present after 10 min"; status=1; }
  {
    echo "pool_size=$(pool_size)"
    echo "nodes_after=$(k get nodes -o name | sed 's|node/||' | sort | tr '\n' ' ')"
    echo "mlops_nodes_before=$(cat "$evidence/state/mlops-nodes.txt" 2>/dev/null | tr '\n' ' ')"
  } | tee "$evidence/cleanup.txt"
  python3 "$root/ops/llm_agent/o1_guard.py" health --context "$KUBE_CONTEXT" \
    --output "$evidence/mlops-health-after.json" >> "$evidence/o1.log" 2>&1
  log "cleanup done"
  exit "$status"
}
trap cleanup EXIT
trap 'log "interrupted"; exit 130' INT TERM HUP

# 1. MLOps must be healthy before anything changes; remember its nodes.
python3 "$root/ops/llm_agent/o1_guard.py" health --context "$KUBE_CONTEXT" \
  --output "$evidence/mlops-health-before.json" || die "MLOps not healthy before O1 (see mlops-health-before.json)"
k get nodes -o name | sed 's|node/||' | sort > "$evidence/state/mlops-nodes.txt"
[ "$(wc -l < "$evidence/state/mlops-nodes.txt")" = 2 ] || die "expected the 2 MLOps nodes before O1"

# 2. Exactly two temporary nodes for O1, labelled; MLOps nodes are never labelled.
NODES=4 "$op" scale-up "$evidence/ops" >> "$evidence/o1.log" 2>&1 || die "scale-up"
mapfile -t new_nodes < <(k get nodes -o name | sed 's|node/||' | sort | comm -23 - "$evidence/state/mlops-nodes.txt")
[ "${#new_nodes[@]}" = 2 ] || die "expected 2 new nodes, got ${#new_nodes[@]}"
for node in "${new_nodes[@]}"; do k label node "$node" "$LABEL=true" --overwrite >/dev/null; done
[ "$(k get nodes -l "$LABEL=true" -o name | wc -l)" = 2 ] || die "label readback"
printf '%s\n' "${new_nodes[@]}" > "$evidence/state/o1-nodes.txt"

# 4. Mesh, namespace and the two API keys (G1 stages).
for stage in istio namespace secret; do "$op" "$stage" "$evidence/ops" >> "$evidence/o1.log" 2>&1 || die "$stage"; done
for ds in istio-system/ztunnel istio-system/istio-cni-node kube-system/calico-node; do
  k -n "${ds%%/*}" rollout status "ds/${ds#*/}" --timeout=10m >> "$evidence/o1.log" 2>&1 || die "$ds not rolled out"
done

# 4b. Capacity, once the DaemonSets (Calico, ztunnel, istio-cni) run on the new nodes:
#     all O1 requests on the labelled nodes within the safe budget.
planned=$(helm template x "$chart" -f "$chart/values-gke-cpu.yaml" -f "$chart/values-gke-cpu-g1.yaml" \
  -f "$chart/o1/placement.yaml" -f "$chart/o1/benchmark-gateway.yaml" --set vllm.image=placeholder.invalid/x@sha256:$(printf '0%.0s' {1..64}) \
  | python3 -c 'import sys, json, yaml
sys.path.insert(0, sys.argv[1])
from ops.llm_agent.gke_preflight import cpu, mem_gib
out = []
for d in yaml.safe_load_all(sys.stdin):
    if d and d["kind"] == "Deployment":
        for _ in range(d["spec"].get("replicas", 1)):
            r = d["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
            out.append({"name": d["metadata"]["name"], "cpu": cpu(r["cpu"]), "memory_gib": mem_gib(r["memory"])})
print(json.dumps(out))' "$root")
python3 "$root/ops/llm_agent/o1_guard.py" capacity --context "$KUBE_CONTEXT" --planned "$planned" \
  --output "$evidence/capacity.json" || die "BLOCKED_INSUFFICIENT_SAFE_CAPACITY (see capacity.json)"

# 5. Resource guard for the whole experiment.
python3 "$root/ops/llm_agent/o1_guard.py" guard --context "$KUBE_CONTEXT" --baseline "$evidence/mlops-health-before.json" \
  --stop-file "$evidence/state/STOP" --output "$evidence/state/resources.jsonl" --interval 15 \
  >> "$evidence/o1.log" 2>&1 &
guard_pid=$!

deploy() {  # <config> [evidence label, default the config]
  local digest_ref label=${2:-$1}
  digest_ref=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["images"]["vllm-cpu-model"]["digest_ref"])' "$DIGESTS")
  # In the background + wait, so a signal interrupts the up-to-30-minute helm wait
  # at once instead of after it (bash defers traps until a foreground child ends).
  HELM_TIMEOUT=30m EXTRA_VALUES="$chart/values-gke-cpu-g1.yaml $chart/o1/placement.yaml $chart/o1/benchmark-gateway.yaml $chart/o1/$1.yaml" \
    KUBE_CONTEXT="$KUBE_CONTEXT" "$root/scripts/llm_agent/deploy_atomic.sh" gke-cpu "$DIGESTS" "$evidence/o1/$label/deploy" \
    >> "$evidence/o1.log" 2>&1 &
  deploy_pid=$!
  wait "$deploy_pid" || die "$label: deploy failed"
  k -n "$NS" get pods -o wide > "$evidence/o1/$label/pods.txt"
  # Placement readback: every LLM pod on an O1 node.
  if k -n "$NS" get pods -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' \
      | grep -vxF -f "$evidence/state/o1-nodes.txt" | grep -q .; then
    die "$1: an LLM pod landed outside the O1 nodes"
  fi
  [ "$(k -n "$NS" get pods -l app.kubernetes.io/instance=primary -o jsonpath='{.items[0].spec.containers[0].image}')" \
    = "$digest_ref" ] || die "$1: primary image is not the pushed digest"
}

throttling() {  # <file>  cgroup v2 cpu.stat of the primary vLLM container
  k -n "$NS" exec deploy/vllm-primary -- cat /sys/fs/cgroup/cpu.stat > "$1" 2>&1 || true
}

restarts() { k -n "$NS" get pods -l app.kubernetes.io/name=vllm -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' | awk '{s+=$1} END {print s+0}'; }

# 6. One configuration at a time, same resources, same workload.
IFS=, read -ra config_list <<< "$configs"
done_configs=()
for config in "${config_list[@]}"; do
  [ -f "$chart/o1/$config.yaml" ] || die "unknown config $config"
  [ -e "$evidence/state/STOP" ] && { log "guard stop: skipping $config"; break; }
  mkdir -p "$evidence/o1/$config"
  log "deploy $config"
  deploy "$config"
  pfs=()
  # kubectl itself goes to the background (with the k() wrapper $! would be a
  # subshell, kill would miss kubectl, and the stale forward would keep the port
  # pointing at the previous config's deleted pods: O1 run 1, C1).
  gw_port=$((13100 + ${#done_configs[@]})) metrics_port=$((18100 + ${#done_configs[@]}))
  kubectl --context "$KUBE_CONTEXT" -n "$NS" port-forward svc/agentgateway "$gw_port:3000" \
    > "$evidence/o1/$config/pf-gateway.log" 2>&1 & pfs+=($!)
  kubectl --context "$KUBE_CONTEXT" -n "$NS" port-forward svc/vllm-primary "$metrics_port:8000" \
    > "$evidence/o1/$config/pf-metrics.log" 2>&1 & pfs+=($!)
  for _ in $(seq 1 20); do
    curl -s -o /dev/null "http://127.0.0.1:$metrics_port/health" && curl -s -o /dev/null "http://127.0.0.1:$gw_port/" && break
    sleep 1
  done
  window_start=$(date -u +%FT%TZ)
  before_restarts=$(restarts)
  throttling "$evidence/o1/$config/cpu-stat-before.txt"
  log "benchmark $config"
  EWALLET_GATEWAY_KEY=$(api_key agentgateway-client-key) timeout 60m python3 "$root/scripts/llm_agent/o1_benchmark.py" \
    --config "$config" --gateway "http://127.0.0.1:$gw_port" --metrics "http://127.0.0.1:$metrics_port" \
    --resources "$evidence/state/resources.jsonl" --stop-file "$evidence/state/STOP" \
    --output "$evidence/o1/$config" >> "$evidence/o1.log" 2>&1 || log "benchmark $config reported failures"
  throttling "$evidence/o1/$config/cpu-stat-after.txt"
  after_restarts=$(restarts)
  for pid in "${pfs[@]}"; do kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null || true; done  # wait returns 143 for a killed forward
  pfs=()
  done_configs+=("$config")
  python3 - "$evidence" "$config" "$window_start" "$before_restarts" "$after_restarts" <<'PY'
import json, sys
from pathlib import Path
evidence, config, start, before, after = sys.argv[1:]
rows = []
for line in (Path(evidence) / "state/resources.jsonl").read_text().splitlines():
    try:
        row = json.loads(line)
    except ValueError:  # the guard may be writing this line right now
        continue
    if row.get("ts", "") >= start.replace("Z", "+00:00"):
        rows.append(row)
stop = Path(evidence) / "state/STOP"
def stat(name):
    p = Path(evidence) / "o1" / config / name
    return dict(l.split() for l in p.read_text().splitlines() if len(l.split()) == 2) if p.exists() else {}
b, a = stat("cpu-stat-before.txt"), stat("cpu-stat-after.txt")
window = {
    "start": start, "samples": len(rows),
    "mlops_regressions": sorted({r for row in rows for r in row.get("mlops_regressions", [])}),
    "guard_stop": json.loads(stop.read_text()) if stop.exists() else None,
    "oom_or_eviction_events": sorted({e for row in rows for e in row.get("oom_or_eviction_events", [])}),
    "vllm_restarts": int(after) - int(before),
    "throttling": {k: int(a[k]) - int(b[k]) for k in ("nr_periods", "nr_throttled", "throttled_usec", "usage_usec")
                   if k in a and k in b},
    "mlops_node_cpu_pct_peak": max((n["cpu_pct"] for row in rows for n in row.get("nodes", []) if not n["o1"]), default=None),
    "mlops_node_mem_pct_peak": max((n["mem_pct"] for row in rows for n in row.get("nodes", []) if not n["o1"]), default=None),
    "mlops_cpu_m_peak": max((row.get("mlops_cpu_m", 0) for row in rows), default=None),
    "gateway_cpu_m_peak": max((row.get("gateway_cpu_m", 0) for row in rows), default=None),
}
(Path(evidence) / "o1" / config / "window.json").write_text(json.dumps(window, indent=2) + "\n")
print(json.dumps({k: window[k] for k in ("samples", "mlops_regressions", "guard_stop", "vllm_restarts", "throttling")}))
PY
done

# 7. Matched comparison and selection.
python3 "$root/scripts/llm_agent/o1_compare.py" "$evidence/o1" --configs "$configs" | tee "$evidence/o1/compare.txt"
selected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["selected"])' "$evidence/o1/comparison.json")
final=$selected; [ "$final" = NO_GAIN ] && final=b0-prime
log "selected=$selected (functional/security gates run on $final)"

# 8. G1 guarantees on the configuration that would be kept.
# Redeploying keeps its own evidence (final-<config>), never overwriting the benchmark deploy.
if [ "$final" != "${config_list[-1]}" ]; then mkdir -p "$evidence/o1/final-$final"; deploy "$final" "final-$final"; fi
for stage in netpol-verify smoke-direct smoke-gateway failover; do
  "$op" "$stage" "$evidence/ops" >> "$evidence/o1.log" 2>&1 || die "$stage on $final"
done
python3 "$root/ops/llm_agent/o1_guard.py" health --context "$KUBE_CONTEXT" \
  --output "$evidence/mlops-health-during.json" >> "$evidence/o1.log" 2>&1 || log "MLOps health check during O1 reported issues"
echo "$selected" > "$evidence/selected.txt"
log "o1 complete"
