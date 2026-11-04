#!/usr/bin/env bash
# G2 matched routing benchmark on the shared GKE pool: B2 standard routing vs O2
# llm-d routing over the same two C1 vLLM replicas (docs/llm_agent/g2_runbook.md).
# usage: g2_experiment.sh <evidence-dir>
#
# Env: KUBE_CONTEXT (ewallet-dev), DIGESTS (pushed vllm-cpu-model digest json),
#   G2_APPROVED=yes (owner approval: 3 temporary nodes, labels, deploys, test
#   pods, failover, deletion of exactly those 3 nodes afterwards),
#   G2_DRY_RUN=yes (offline: stops after the local preflight, touches nothing).
#
# Order (G2 prompt §9): A two C1 replicas -> B C1 functional/security gates ->
# C B2 benchmark -> D O2 deploy + gates -> E O2 benchmark -> compare -> F cleanup.
# One bounded campaign: a failed step stops the run (no automatic retry) and
# cleanup always runs (success, failure, signal).
set -euo pipefail
evidence=${1:?usage: g2_experiment.sh <evidence-dir>}
PROJECT=ewallet-data-ai-platform-hg26 ZONE=asia-southeast1-b CLUSTER=ewallet-dev POOL=cpu-pool NS=ewallet-agents
LABEL=ewallet.io/g2-llm ADDED_NODES=3 ROUTER_RELEASE=llmd-router
root=$(cd "$(dirname "$0")/../.." && pwd)
chart="$root/infra/helm/llm-agent-platform"
# G2_OPERATOR replaces the G1 stage runner only in the offline simulation (tests/llm_agent/g2_sim).
op=${G2_OPERATOR:-$root/scripts/llm_agent/g1_operator.sh}
# shellcheck source=../../infra/helm/llm-d/versions.env
. "$root/infra/helm/llm-d/versions.env"
mkdir -p "$evidence/g2" "$evidence/ops" "$evidence/state"
exec 9>&1  # the caller's stdout: traps restore it (a step's redirection may be active)
evidence=$(cd "$evidence" && pwd)
LEVELS=${G2_LEVELS:-1,2,4,8}
GW_PORT=13200 EPP_PORT=19290
VALUES_COMMON="$chart/values-gke-cpu-g1.yaml $chart/g2/common.yaml $chart/g2/benchmark-gateway.yaml"
export G1_APPROVED_STAGES=istio,namespace,secret,netpol-verify,failover
k() { kubectl --context "${KUBE_CONTEXT:-}" --request-timeout=60s "$@"; }
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$evidence/g2.log"; }
die() { log "FAIL: $*"; exit 1; }
api_key() { k -n "$NS" get secret "$1" -o jsonpath='{.data.api-key}' | base64 -d; }
port_free() { ! ss -ltnH "( sport = :$1 )" 2>/dev/null | grep -q .; }
pool_size() {
  gcloud compute instance-groups managed list --project "$PROJECT" --filter="name~^gke-${CLUSTER}-${POOL}-" \
    --format='value(targetSize)' | awk '{s+=$1} END {print s+0}'
}

# ---- 0. Local preflight (no cluster change) ---------------------------------
log "g2 start git=$(git -C "$root" rev-parse HEAD) levels=$LEVELS"
if [ -n "$(git -C "$root" status --porcelain -- infra scripts ops)" ]; then
  # G2_ALLOW_DIRTY is for the offline simulation; a real run refuses below.
  [ "${G2_ALLOW_DIRTY:-}" = yes ] || die "uncommitted source changes: runtime evidence must map to one SHA"
  log "source_dirty=yes (G2_ALLOW_DIRTY)"
fi
for port in $GW_PORT $EPP_PORT 13000 13001 18000 18001; do port_free "$port" || die "local port $port in use"; done
chart_dir=$(mktemp -d)  # the pulled chart is verified by digest, never committed as evidence
router_chart="$chart_dir/llm-d-router-standalone-$LLMD_STANDALONE_VERSION.tgz"
helm pull "$LLMD_STANDALONE_CHART" --version "$LLMD_STANDALONE_VERSION" -d "$chart_dir" >> "$evidence/g2.log" 2>&1
echo "$LLMD_STANDALONE_CHART_SHA256  $router_chart" | sha256sum --check --quiet || die "router chart digest mismatch"
placeholder=placeholder.invalid/x@sha256:$(printf '0%.0s' {1..64})
render() {  # <b2|o2> -> llm-agent render on stdout
  local extra=()
  [ "$1" = o2 ] && extra=(--set "llmd.standalone.apiServer.cidrs={192.0.2.1/32}")
  helm template llm-agent "$chart" -f "$chart/values-gke-cpu.yaml" $(printf -- '-f %s ' $VALUES_COMMON) \
    -f "$chart/g2/$1.yaml" --set "vllm.image=$placeholder" "${extra[@]}"
}
render b2 > "$evidence/state/render-b2.yaml" && render o2 > "$evidence/state/render-o2.yaml" || die "chart render"
helm template "$ROUTER_RELEASE" "$router_chart" -n "$NS" -f "$root/infra/helm/llm-d/standalone-values-g2.yaml" \
  > "$evidence/state/render-router.yaml" || die "router render"
grep -qE '^kind: Cluster(Role|RoleBinding)' "$evidence/state/render-router.yaml" && die "router render has cluster-scoped RBAC"
python3 "$root/scripts/llm_agent/g2_preflight.py" --b2 "$evidence/state/render-b2.yaml" \
  --o2 "$evidence/state/render-o2.yaml" --router "$evidence/state/render-router.yaml" --levels "$LEVELS" \
  --output "$evidence/state/preflight.json" || die "local preflight (see state/preflight.json)"
if [ "${G2_DRY_RUN:-}" = yes ]; then log "dry run: local preflight passed, nothing changed"; exit 0; fi

# ---- Cloud from here: approval, context, digests ----------------------------
[ "${G2_APPROVED:-}" = yes ] || die "set G2_APPROVED=yes after owner approval"
[ -z "${G2_ALLOW_DIRTY:-}" ] || [ -n "${G2_OPERATOR:-}" ] || die "G2_ALLOW_DIRTY is for the offline simulation only"
[ "${KUBE_CONTEXT:-}" = "gke_${PROJECT}_${ZONE}_${CLUSTER}" ] || die "KUBE_CONTEXT must be the ewallet-dev context"
: "${DIGESTS:?DIGESTS required}"
cp "$DIGESTS" "$evidence/image-digests.json"
digest_ref=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["images"]["vllm-cpu-model"]["digest_ref"])' "$DIGESTS")
guard_pid="" pfs=() child=""
# Long steps (deploys, gates, rollouts, the benchmark) run in their own process
# group and the script waits on them, so a signal interrupts the wait at once
# (bash defers traps until a foreground child ends) and cleanup stops the group.
run() {
  local rc=0
  setsid "$@" &
  child=$!
  wait "$child" || rc=$?
  child=""
  return "$rc"
}

stop_forwards() {  # kubectl itself was backgrounded (never through k()): $! is its pid
  local pid
  for pid in "${pfs[@]}"; do kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null || true; done
  pfs=()
}
cleanup() {
  local status=$?
  set +e
  exec 1>&9 2>&9
  trap - EXIT INT TERM HUP
  log "cleanup (status $status)"
  stop_forwards
  [ -n "$child" ] && kill -TERM -- "-$child" 2>/dev/null
  [ -n "$guard_pid" ] && kill "$guard_pid" 2>/dev/null
  k -n "$NS" logs deploy/llmd-router-epp -c epp --tail=2000 > "$evidence/state/epp-final.log" 2>/dev/null
  helm uninstall "$ROUTER_RELEASE" --kube-context "$KUBE_CONTEXT" --namespace "$NS" --wait --timeout 5m \
    >> "$evidence/g2.log" 2>&1
  helm uninstall llm-agent --kube-context "$KUBE_CONTEXT" --namespace default --wait --timeout 10m \
    >> "$evidence/g2.log" 2>&1
  k delete namespace g1-netpol-probe --ignore-not-found --wait=false >/dev/null 2>&1
  mapfile -t g2_nodes < <(k get nodes -l "$LABEL=true" -o name 2>/dev/null | sed 's|node/||')
  if [ "${#g2_nodes[@]}" -gt 0 ]; then
    for node in "${g2_nodes[@]}"; do
      k drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout=5m >> "$evidence/g2.log" 2>&1
    done
    mig=$(gcloud container node-pools describe "$POOL" --cluster "$CLUSTER" --zone "$ZONE" --project "$PROJECT" \
      --format='value(instanceGroupUrls)' | tr ';' '\n' | head -1 | sed 's|.*/||')
    gcloud compute instance-groups managed delete-instances "$mig" --zone "$ZONE" --project "$PROJECT" \
      --instances="$(IFS=,; echo "${g2_nodes[*]}")" --quiet >> "$evidence/g2.log" 2>&1
  fi
  # Wait (<= 10 min) until the labelled Node objects are gone before reading back (O1 erratum E3).
  for _ in $(seq 1 60); do
    [ -z "$(k get nodes -l "$LABEL=true" -o name 2>/dev/null)" ] && break
    sleep 10
  done
  [ -z "$(k get nodes -l "$LABEL=true" -o name 2>/dev/null)" ] || { log "G2 nodes still present after 10 min"; status=1; }
  {
    echo "pool_size=$(pool_size)"
    echo "nodes_after=$(k get nodes -o name | sed 's|node/||' | sort | tr '\n' ' ')"
    echo "mlops_nodes_before=$(tr '\n' ' ' < "$evidence/state/mlops-nodes.txt" 2>/dev/null)"
    echo "llm_releases_after=$(helm list -A --kube-context "$KUBE_CONTEXT" -q | grep -E '^(llm-agent|llmd-router)$' | tr '\n' ' ')"
    echo "ewallet_agents_objects_after=$(k -n "$NS" get deploy,svc,cm,networkpolicy,authorizationpolicy -o name 2>/dev/null \
      | grep -vE 'configmap/(kube-root-ca.crt|istio-ca-root-cert|istio-ca-crl)' | tr '\n' ' ')"
  } | tee "$evidence/cleanup.txt"
  python3 "$root/ops/llm_agent/g2_guard.py" health --context "$KUBE_CONTEXT" \
    --output "$evidence/mlops-health-after.json" >> "$evidence/g2.log" 2>&1 || status=1
  log "cleanup done"
  exit "$status"
}
trap cleanup EXIT
trap 'exec 1>&9 2>&9; log "interrupted"; exit 130' INT TERM HUP

# ---- 1. MLOps healthy before anything changes; remember its nodes -----------
python3 "$root/ops/llm_agent/g2_guard.py" health --context "$KUBE_CONTEXT" \
  --output "$evidence/mlops-health-before.json" || die "MLOps not healthy before G2"
k get nodes -o name | sed 's|node/||' | sort > "$evidence/state/mlops-nodes.txt"
[ "$(wc -l < "$evidence/state/mlops-nodes.txt")" = 2 ] || die "expected exactly the 2 MLOps nodes before G2"
[ -z "$(helm list -A --kube-context "$KUBE_CONTEXT" -q | grep -E '^(llm-agent|llmd-router)$')" ] \
  || die "an LLM release already exists: another operator may own the cluster"

# ---- 2. Exactly three temporary nodes, labelled -----------------------------
target=$(( $(pool_size) + ADDED_NODES ))
[ "$target" = 5 ] || die "pool must grow from 2 to 5 nodes (target $target)"
run gcloud container clusters resize "$CLUSTER" --node-pool "$POOL" --num-nodes "$target" --zone "$ZONE" \
  --project "$PROJECT" --quiet >> "$evidence/g2.log" 2>&1 || die "scale-up"
run kubectl --context "$KUBE_CONTEXT" wait --for=condition=Ready nodes -l "cloud.google.com/gke-nodepool=$POOL" \
  --timeout=15m >> "$evidence/g2.log" 2>&1 || die "nodes not Ready"
mapfile -t new_nodes < <(k get nodes -o name | sed 's|node/||' | sort | comm -23 - "$evidence/state/mlops-nodes.txt")
[ "${#new_nodes[@]}" = "$ADDED_NODES" ] || die "expected $ADDED_NODES new nodes, got ${#new_nodes[@]}"
for node in "${new_nodes[@]}"; do k label node "$node" "$LABEL=true" --overwrite >/dev/null; done
[ "$(k get nodes -l "$LABEL=true" -o name | wc -l)" = "$ADDED_NODES" ] || die "label readback"
printf '%s\n' "${new_nodes[@]}" > "$evidence/state/g2-nodes.txt"
k get nodes -o wide > "$evidence/state/nodes-after-scale-up.txt"

# ---- 3. Mesh, namespace, API keys (G1 stages) --------------------------------
for stage in istio namespace secret; do run "$op" "$stage" "$evidence/ops" >> "$evidence/g2.log" 2>&1 || die "$stage"; done
for ds in istio-system/ztunnel istio-system/istio-cni-node kube-system/calico-node; do
  k -n "${ds%%/*}" rollout status "ds/${ds#*/}" --timeout=10m >> "$evidence/g2.log" 2>&1 || die "$ds not rolled out"
done

# ---- 4. Capacity: every G2 request on the labelled nodes within the safe budget
planned=$(cat "$evidence/state/render-o2.yaml" "$evidence/state/render-router.yaml" | python3 -c 'import sys, json, yaml
sys.path.insert(0, sys.argv[1])
from ops.llm_agent.gke_preflight import cpu, mem_gib
out = []
for d in yaml.safe_load_all(sys.stdin):
    if d and d["kind"] == "Deployment":
        req = [c["resources"].get("requests", {}) for c in d["spec"]["template"]["spec"]["containers"]]
        for _ in range(d["spec"].get("replicas", 1)):
            out.append({"name": d["metadata"]["name"], "cpu": sum(cpu(str(r.get("cpu", "0"))) for r in req),
                        "memory_gib": sum(mem_gib(str(r.get("memory", "0"))) for r in req)})
print(json.dumps(out))' "$root")
python3 "$root/ops/llm_agent/g2_guard.py" capacity --context "$KUBE_CONTEXT" --planned "$planned" \
  --output "$evidence/capacity.json" || die "BLOCKED_INSUFFICIENT_SAFE_CAPACITY (see capacity.json)"

# ---- 5. Resource guard for the whole campaign ------------------------------
python3 "$root/ops/llm_agent/g2_guard.py" guard --context "$KUBE_CONTEXT" --baseline "$evidence/mlops-health-before.json" \
  --stop-file "$evidence/state/STOP" --output "$evidence/state/resources.jsonl" --interval 15 \
  >> "$evidence/g2.log" 2>&1 &
guard_pid=$!

# API server endpoints for the EPP's egress policy (read back, never guessed).
apiserver=$( { k get svc kubernetes -n default -o jsonpath='{.spec.clusterIP}{"\n"}'
               k get endpointslices -n default -l kubernetes.io/service-name=kubernetes \
                 -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]}{"\n"}{end}'; } | sort -u | grep . | sed 's|$|/32|' | paste -sd, -)
apiserver_ports=$(k get endpointslices -n default -l kubernetes.io/service-name=kubernetes \
  -o jsonpath='{range .items[*].ports[*]}{.port}{"\n"}{end}' | sort -u | { cat; echo 443; } | sort -u | paste -sd, -)
[ -n "$apiserver" ] || die "API server readback"
echo "cidrs=$apiserver ports=$apiserver_ports" > "$evidence/state/apiserver.txt"

# Read-back values for O2 (a values file: deploy_atomic.sh takes values files only).
python3 - "$apiserver" "$apiserver_ports" > "$evidence/state/o2-apiserver.yaml" <<'PY'
import json, sys
cidrs, ports = sys.argv[1].split(","), [int(p) for p in sys.argv[2].split(",")]
print(json.dumps({"llmd": {"standalone": {"apiServer": {"cidrs": cidrs, "ports": ports}}}}))
PY

deploy() {  # <b2|o2>: helm upgrade of llm-agent (background + wait: signals interrupt it at once)
  local extra="" dir="$evidence/g2/$1/deploy"
  mkdir -p "$dir"
  [ "$1" = o2 ] && extra=" $evidence/state/o2-apiserver.yaml"
  HELM_TIMEOUT=30m EXTRA_VALUES="$VALUES_COMMON $chart/g2/$1.yaml$extra" KUBE_CONTEXT="$KUBE_CONTEXT" \
    run "$root/scripts/llm_agent/deploy_atomic.sh" gke-cpu "$DIGESTS" "$dir" >> "$evidence/g2.log" 2>&1 \
    || die "$1: deploy failed"
}

placement_check() {  # every LLM pod on a G2 node; two Ready primaries on distinct nodes
  k -n "$NS" get pods -o wide > "$evidence/g2/$1/pods.txt"
  if k -n "$NS" get pods -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' \
      | grep -vxF -f "$evidence/state/g2-nodes.txt" | grep -q .; then
    die "$1: an LLM pod landed outside the G2 nodes"
  fi
  [ "$(k -n "$NS" get pods -l app.kubernetes.io/instance=primary -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' \
       | sort -u | grep -c .)" = 2 ] || die "$1: the two primaries are not on two distinct nodes"
  for image in $(k -n "$NS" get pods -l app.kubernetes.io/name=vllm -o jsonpath='{range .items[*]}{.spec.containers[0].image}{"\n"}{end}'); do
    [ "$image" = "$digest_ref" ] || die "$1: vLLM image $image is not the pushed digest"
  done
}

fresh_serving() {  # <b2|o2> <failover-mark>: cold primaries and a fresh gateway, then read back
  # The failover gate replaces both primaries at its end; pods created after its
  # start have served nothing but their startup warm-up, so they count as fresh
  # and are not replaced again (one ~25 min sequential CPU warm-up saved per run).
  # Otherwise both are restarted. The gateway is always restarted (eviction state).
  local dir="$evidence/g2/$1/readback" mark=$2 stale
  mkdir -p "$dir"
  stale=$(k -n "$NS" get pods -l app.kubernetes.io/instance=primary \
    -o jsonpath='{range .items[*]}{.status.startTime}{"\n"}{end}' | awk -v m="$mark" '$1 < m' | grep -c . || true)
  if [ "$stale" != 0 ]; then
    mark=$(date -u +%FT%TZ)
    k -n "$NS" rollout restart deploy/vllm-primary >> "$evidence/g2.log"
  fi
  log "$1: primaries fresh since $mark (restarted: $([ "$stale" != 0 ] && echo yes || echo no))"
  k -n "$NS" rollout restart deploy/agentgateway >> "$evidence/g2.log"
  run kubectl --context "$KUBE_CONTEXT" -n "$NS" rollout status deploy/agentgateway --timeout=10m \
    >> "$evidence/g2.log" 2>&1 || die "$1: gateway restart"
  run kubectl --context "$KUBE_CONTEXT" -n "$NS" rollout status deploy/vllm-primary --timeout=40m \
    >> "$evidence/g2.log" 2>&1 || die "$1: vLLM restart"
  k -n "$NS" get deploy vllm-primary -o json > "$dir/deploy.json"
  k -n "$NS" get pods -l app.kubernetes.io/instance=primary -o json > "$dir/pods.json"
  k get nodes -o json > "$dir/nodes.json"
  k -n "$NS" get configmap agentgateway-llm-config -o json > "$dir/gateway-configmap.json"
  python3 "$root/scripts/llm_agent/g2_readback.py" "$dir" "$mark" > "$evidence/g2/$1/serving.json"
  python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); sys.exit(not (s["fresh_pods"] and s["distinct_nodes"]
    and len(s["pods"]) == 2 and len(s["image_ids"]) == 1))' "$evidence/g2/$1/serving.json" \
    || die "$1: serving readback (see g2/$1/serving.json)"
  cp "$dir/pods.json" "$evidence/g2/$1/pods.json"
}

benchmark() {  # <b2|o2>
  local run=$1 pods start
  mkdir -p "$evidence/g2/$run"
  pods=$(k -n "$NS" get pods -l app.kubernetes.io/instance=primary -o jsonpath='{range .items[*]}{.metadata.name}{","}{end}')
  kubectl --context "$KUBE_CONTEXT" -n "$NS" port-forward svc/agentgateway "$GW_PORT:3000" \
    > "$evidence/g2/$run/pf-gateway.log" 2>&1 & pfs+=($!)
  local epp_args=()
  if [ "$run" = o2 ]; then
    kubectl --context "$KUBE_CONTEXT" -n "$NS" port-forward deploy/llmd-router-epp "$EPP_PORT:9090" \
      > "$evidence/g2/$run/pf-epp.log" 2>&1 & pfs+=($!)
    epp_args=(--epp-metrics "http://127.0.0.1:$EPP_PORT")
  fi
  for _ in $(seq 1 30); do
    curl -s -o /dev/null "http://127.0.0.1:$GW_PORT/" && { [ "$run" = b2 ] || curl -s -o /dev/null "http://127.0.0.1:$EPP_PORT/metrics"; } && break
    sleep 1
  done
  start=$(date -u +%FT%TZ)
  k -n "$NS" exec "${pods%%,*}" -c vllm -- cat /sys/fs/cgroup/cpu.stat > "$evidence/g2/$run/cpu-stat-a-before.txt" 2>&1 || true
  log "benchmark $run pods=$pods"
  EWALLET_GATEWAY_KEY=$(api_key agentgateway-client-key) run timeout 90m python3 "$root/scripts/llm_agent/g2_benchmark.py" \
    --config "$run" --gateway "http://127.0.0.1:$GW_PORT" --context "$KUBE_CONTEXT" --pods "${pods%,}" "${epp_args[@]}" \
    --levels "$LEVELS" --resources "$evidence/state/resources.jsonl" --stop-file "$evidence/state/STOP" \
    --output "$evidence/g2/$run" >> "$evidence/g2.log" 2>&1 || log "benchmark $run reported failures"
  stop_forwards
  for pod in ${pods//,/ }; do
    k -n "$NS" exec "$pod" -c vllm -- cat /sys/fs/cgroup/cpu.stat > "$evidence/g2/$run/cpu-stat-$pod.txt" 2>&1 || true
    k -n "$NS" logs "$pod" -c vllm --since-time="$start" 2>&1 | grep -viE 'authorization|bearer|api[-_]key' \
      | tail -400 > "$evidence/g2/$run/$pod.log" || true
  done
  # Evidence capture must never end the run: an empty log makes grep exit 1, which
  # under set -e killed the script as the last command of an && list.
  if [ "$run" = o2 ]; then
    k -n "$NS" logs deploy/llmd-router-epp -c epp --since-time="$start" > "$evidence/g2/o2/epp.log" 2>&1 || true
    { k -n "$NS" logs deploy/llmd-router-epp -c envoy-proxy --since-time="$start" 2>&1 \
        | grep -viE 'authorization|bearer' > "$evidence/g2/o2/envoy.log"; } || true
  fi
}

# ---- Gate A: two C1 replicas (B2 routing deployed) --------------------------
mkdir -p "$evidence/g2/b2"
log "gate A: deploy B2 (two C1 replicas)"
deploy b2
placement_check b2

# ---- Gate B: C1 functional/security verification (never run in O1) ---------
mkdir -p "$evidence/ops/b2"
for stage in netpol-verify smoke-direct smoke-gateway failover; do
  log "gate B: $stage"
  [ "$stage" = failover ] && failover_mark_b2=$(date -u +%FT%TZ)
  run "$op" "$stage" "$evidence/ops/b2" >> "$evidence/g2.log" 2>&1 || die "gate B $stage"
done
python3 -c 'import json,sys; sys.exit(json.load(open(sys.argv[1]))["first_turn"].get("backend") != "primary")' \
  "$evidence/ops/b2/smoke-gateway-non-stream.json" || die "gate B: gateway smoke not served by the primary route"

# ---- Gate C: B2 benchmark ---------------------------------------------------
[ -e "$evidence/state/STOP" ] && die "guard stop before B2"
fresh_serving b2 "$failover_mark_b2"
benchmark b2
# No O2 spend without a complete B2 control (metrics missing, rate limit, guard stop).
measured() { python3 -c 'import json,sys; sys.exit(json.load(open(sys.argv[1])).get("status") != "MEASURED")' \
  "$evidence/g2/$1/summary.json" 2>/dev/null; }
measured b2 || die "B2 not fully measured (see g2/b2/summary.json): no O2 run"

# ---- Gate D: O2 llm-d routing over the same two replicas -------------------
log "gate D: O2 policies and gateway route, then install $ROUTER_RELEASE"
mkdir -p "$evidence/g2/o2/deploy"
# The router's allow policies (kubelet probes, API server egress, agentgateway ->
# router, router -> vLLM) come with the llm-agent O2 release, so it goes first:
# in the default-deny namespace a router without them never becomes Ready
# (G2 attempt 1: probes on 9003 timed out, CrashLoopBackOff).
deploy o2
# No --atomic: on failure the pods stay until diagnostics are saved, then cleanup uninstalls.
if ! run helm upgrade --install "$ROUTER_RELEASE" "$router_chart" --kube-context "$KUBE_CONTEXT" --namespace "$NS" \
    -f "$root/infra/helm/llm-d/standalone-values-g2.yaml" --wait --timeout 10m \
    > "$evidence/g2/o2/deploy/router-helm.txt" 2>&1; then
  d="$evidence/g2/o2/router-diagnostics"; mkdir -p "$d"
  k -n "$NS" describe pods -l llm-d-router-standalone=llmd-router-epp > "$d/describe.txt" 2>&1 || true
  for c in epp envoy-proxy; do
    k -n "$NS" logs deploy/llmd-router-epp -c "$c" --tail=300 > "$d/$c.log" 2>&1 || true
    k -n "$NS" logs deploy/llmd-router-epp -c "$c" --previous --tail=300 > "$d/$c.previous.log" 2>&1 || true
  done
  k -n "$NS" get networkpolicy,authorizationpolicy -o name > "$d/policies.txt" 2>&1 || true
  die "router install (diagnostics in g2/o2/router-diagnostics)"
fi
placement_check o2
k -n "$NS" rollout restart deploy/agentgateway >> "$evidence/g2.log"
k -n "$NS" rollout status deploy/agentgateway --timeout=10m >> "$evidence/g2.log" 2>&1 || die "gateway restart"
# The EPP must see exactly the two primaries before any O2 claim: after the router
# install, and again before the benchmark (the failover gate replaces both pods).
# llm_d_epp_ready_endpoints counts the pods whose vLLM metrics the EPP scraped
# recently: discovery and the metrics data path in one gauge.
epp_ready() {  # <label>
  kubectl --context "$KUBE_CONTEXT" -n "$NS" port-forward deploy/llmd-router-epp "$EPP_PORT:9090" \
    > "$evidence/g2/o2/pf-epp-$1.log" 2>&1 & pfs+=($!)
  sleep 15  # port-forward up and at least one EPP metrics refresh
  curl -s "http://127.0.0.1:$EPP_PORT/metrics" > "$evidence/g2/o2/epp-metrics-$1.txt" || true
  stop_forwards
  python3 - "$evidence/g2/o2/epp-metrics-$1.txt" <<'PY'
import sys
ready = [float(l.rsplit(" ", 1)[1]) for l in open(sys.argv[1]) if l.startswith("llm_d_epp_ready_endpoints")]
print("epp ready_endpoints", ready)
sys.exit(not ready or sum(ready) != 2)
PY
}
epp_ready deploy || die "gate D: EPP does not report 2 ready endpoints"
mkdir -p "$evidence/ops/o2"
for stage in netpol-verify smoke-gateway failover; do
  log "gate D: $stage"
  [ "$stage" = failover ] && failover_mark_o2=$(date -u +%FT%TZ)
  run "$op" "$stage" "$evidence/ops/o2" >> "$evidence/g2.log" 2>&1 || die "gate D $stage"
done
python3 -c 'import json,sys; sys.exit(json.load(open(sys.argv[1]))["first_turn"].get("backend") != "llm-d")' \
  "$evidence/ops/o2/smoke-gateway-non-stream.json" || die "gate D: gateway smoke not served by llm-d"

# ---- Gate E: O2 benchmark, then the matched comparison ---------------------
[ -e "$evidence/state/STOP" ] && die "guard stop before O2"
fresh_serving o2 "$failover_mark_o2"
epp_ready benchmark || die "O2: EPP does not see the two restored primaries"
benchmark o2
python3 "$root/ops/llm_agent/g2_guard.py" health --context "$KUBE_CONTEXT" \
  --output "$evidence/mlops-health-during.json" >> "$evidence/g2.log" 2>&1 || log "MLOps health during G2 reported issues"
python3 "$root/scripts/llm_agent/g2_compare.py" "$evidence/g2" | tee "$evidence/g2/compare.txt"
measured o2 || die "O2 not fully measured (see g2/o2/summary.json); comparison recorded as is"
log "g2 complete"
