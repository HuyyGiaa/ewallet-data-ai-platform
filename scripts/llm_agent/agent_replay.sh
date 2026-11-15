#!/usr/bin/env bash
# Owner evidence replay for the EDAI-2 Agent Platform (docs/edai2/agents/OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md).
#
# A thin, phased wrapper around the tools the campaign validated on GKE; it adds banners, port-forwards,
# hop checks and one output directory per invocation. It never changes a Helm release, Secret, policy or
# production Deployment. Commands that create load or experiment resources say so below.
#
#   agent_replay.sh status                 [READ-ONLY]  releases, agents, MCP, sandbox fleet, KEDA, gateways
#   agent_replay.sh pf                     [LOCAL]      start the port-forwards the UIs and probes need
#   agent_replay.sh e2e <case>             [READ-ONLY data] one business request through the Coordinator, then its hops
#                                          case: fraud | drift | combined | nl-fraud | nl-drift
#   agent_replay.sh negative               [READ-ONLY]  wrong credential, ungranted tool, undeclared route (AGI-08)
#   agent_replay.sh registry               [READ-ONLY]  Agent Registry readback against the deployed catalog (AGI-13)
#   agent_replay.sh keda                   [LOAD]       KEDA 2 -> 3 -> 2: two bounded AGR-06 load runs + live timeline
#   agent_replay.sh workers                [SESSIONS]   shared WorkerPool: sequential sessions of both Sandbox agents
#   agent_replay.sh failover               [EXPERIMENT] isolated MCP failover (ewallet-mcp-exp), both arms, force failure
#   agent_replay.sh stop-pf                [LOCAL]      stop the port-forwards this script started
#
# Env: AGI_ENV (default from ~/.cache/ewallet-agi/current.env), REPLAY_DIR (default
#   ~/ewallet-evidence/agents/replay/<UTC timestamp>), UV (default uv on PATH or the fintech env).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
die() { echo "agent_replay: $*" >&2; exit 1; }
banner() { printf '\n==================== %s  %s ====================\n' "$(date -u +%H:%M:%SZ)" "$*"; }

[ -n "${AGI_ENV:-}" ] || { [ -f "$HOME/.cache/ewallet-agi/current.env" ] && source "$HOME/.cache/ewallet-agi/current.env"; }
[ -f "${AGI_ENV:-}" ] || die "AGI_ENV must name the campaign env file"
# shellcheck disable=SC1090
source "$AGI_ENV"
: "${CTX:?}" "${EV:?}" "${BASELINE:?}" "${CAMPAIGN:?}"
UV=${UV:-$(command -v uv || echo "$HOME/miniconda3/envs/fintech/bin/uv")}
[ -x "$UV" ] || die "uv not found; set UV"
export PATH="$(dirname "$UV"):$PATH" PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
AGENT_PY=("$UV" run --frozen --project "$ROOT/infra/docker/g3-byo-coordinator" python)
TOOL_PY=("$UV" run --locked --project "$ROOT/infra/docker/llm-agent-tooling" python)
REPLAY_DIR=${REPLAY_DIR:-$HOME/ewallet-evidence/agents/replay/$(date -u +%Y%m%dT%H%M%SZ)}
PF_PIDS="$HOME/.cache/ewallet-agi/replay-port-forwards.pids"
k() { kubectl --context "$CTX" "$@"; }
[ "$(kubectl config view -o jsonpath="{.contexts[?(@.name=='$CTX')].name}")" = "$CTX" ] || die "kube context $CTX missing"

# --- port-forwards ------------------------------------------------------------------------------------
PORT_FORWARDS=(
  "ewallet-agents svc/a2a-gateway 18083:8083"
  "ewallet-agents svc/mcp-gateway 18082:8080"
  "kagent svc/kagent-controller 18383:8083"
  "kagent svc/kagent-ui 18300:8080"
  "ewallet-agents svc/grafana 13000:3000"
  "ewallet-prompts svc/prompt-registry-agentregistry 18121:12121"
)
listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
start_pf() {
  mkdir -p "$(dirname "$PF_PIDS")"
  local spec ns target ports local_port
  for spec in "${PORT_FORWARDS[@]}"; do
    read -r ns target ports <<< "$spec"; local_port=${ports%%:*}
    if listening "$local_port"; then echo "port $local_port already listening ($ns $target)"; continue; fi
    if ! k -n "$ns" get "${target}" > /dev/null 2>&1; then echo "skip $ns $target (not present)"; continue; fi
    nohup kubectl --context "$CTX" -n "$ns" port-forward "$target" "$ports" > /dev/null 2>&1 &
    echo "$!" >> "$PF_PIDS"; echo "port-forward $ns $target -> 127.0.0.1:$local_port (pid $!)"
  done
  sleep 3
}
stop_pf() {
  [ -f "$PF_PIDS" ] || { echo "no port-forwards recorded"; return; }
  while read -r pid; do kill "$pid" 2>/dev/null && echo "stopped $pid"; done < "$PF_PIDS"
  rm -f "$PF_PIDS"
}
need_port() { listening "$1" || die "nothing listens on 127.0.0.1:$1; run: $0 pf"; }
token() { k -n ewallet-agents get secret "$1" -o jsonpath='{.data.token}' | base64 -d; }

# --- status -------------------------------------------------------------------------------------------
status() {
  banner "REPLAY-01 cluster and platform state (Kubernetes readiness only, not business E2E)"
  k get nodes -L ewallet.io/g3-llm
  helm --kube-context "$CTX" -n default history llm-agent --max 1
  helm --kube-context "$CTX" -n ewallet-mcp history llm-agent-mcp --max 1
  banner "Agents (kagent), Deployments, KEDA"
  k -n ewallet-agents get agents.kagent.dev,modelconfig
  k -n ewallet-agents get deploy fraud-coordinator feature-rag-specialist drift-specialist a2a-gateway mcp-gateway agentgateway
  k -n ewallet-agents get scaledobject,hpa
  banner "MCP servers (2 replicas, PDB, RollingUpdate)"
  k -n ewallet-mcp get deploy,pdb,pods -o wide
  k -n ewallet-mcp get deploy feature-rag-mcp drift-mcp \
    -o custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas,STRATEGY:.spec.strategy.type,MAXUNAVAILABLE:.spec.strategy.rollingUpdate.maxUnavailable
  banner "Sandbox fleet (SandboxAgents, shared WorkerPool, gVisor ActorTemplates)"
  k -n ewallet-sandbox get sandboxagent,workerpool,actortemplate
  k -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents -o wide
  banner "Restarts in ewallet-agents / ewallet-mcp (expect 0)"
  k -n ewallet-agents get pods --no-headers | awk '$4 != "0" {print "RESTARTED:", $0}'
  k -n ewallet-mcp get pods --no-headers | awk '$4 != "0" {print "RESTARTED:", $0}'
  echo "done"
}

# --- business E2E -------------------------------------------------------------------------------------
NL_FRAUD="Hãy đánh giá rủi ro gian lận cho giao dịch payment dc58ff4e-b5f7-4486-b1f8-4c31383741a9 (user 55561d92-d042-401c-b02a-b03e3ed280f7, account 2d48f147-f98c-4819-b183-f94ca049c175, device e7765b81-c9f9-4147-aa00-46f34de1d7da, merchant e3e4aced-6b0d-40af-be5c-eef9255204c9, amount 275465.53 VND, channel app, event_timestamp 2026-06-01T00:00:00Z): lấy feature của user/account, gọi mô hình dự đoán gian lận, giải thích mức rủi ro và trích dẫn chính sách liên quan."
NL_DRIFT="Phân tích drift của account_behavior__account_tx_count_1h so với baseline bl-r5gb-202604.account_tx_count_1h trong window 2026-05-01T00:00:00Z to 2026-05-31T00:00:00Z."
e2e() {
  local case=${1:?case: fraud|drift|combined|nl-fraud|nl-drift} id out
  need_port 18083
  id="replay-$case-$(date -u +%H%M%S)"; out="$REPLAY_DIR/e2e"; mkdir -p "$out"
  banner "E2E $case  request_id $id  (Coordinator -> A2A Gateway -> Sandbox Specialist -> MCP Gateway -> backend)"
  local since; since=$(date -u +%FT%TZ)
  case "$case" in
    fraud|drift|combined)
      local args=(--request-id "$id")
      [ "$case" != drift ] && args+=(--transaction "$EV/inputs/transaction.json" --policy-query "payment anomaly policy and step-up authentication requirements")
      [ "$case" != fraud ] && args+=(--feature account_behavior__account_tx_count_1h --baseline "$BASELINE" \
        --window-start 2026-05-01T00:00:00Z --window-end 2026-05-31T00:00:00Z)
      "${AGENT_PY[@]}" "$ROOT/scripts/llm_agent/agi_probe.py" request "$case" "${args[@]}" --out "$out/$id-request.json" > /dev/null
      "${AGENT_PY[@]}" "$ROOT/scripts/llm_agent/g3_a2a_smoke.py" --endpoint http://127.0.0.1:18083/api/a2a/ewallet-agents/fraud-coordinator/ \
        --request "$out/$id-request.json" --response "$out/$id-task.json" --timing "$out/$id-timing.json" > /dev/null ;;
    nl-fraud|nl-drift)
      local text=$NL_FRAUD; [ "$case" = nl-drift ] && text=$NL_DRIFT
      "${AGENT_PY[@]}" - "$out/$id-request.json" "$out/$id-task.json" "$out/$id-timing.json" "$id" "$text" <<'PY'
import json, sys, time, urllib.request, uuid
req_path, task_path, timing_path, mid, text = sys.argv[1:6]
req = {"jsonrpc": "2.0", "id": mid, "method": "message/send", "params": {"message": {
    "role": "user", "messageId": mid, "contextId": str(uuid.uuid4()), "parts": [{"kind": "text", "text": text}]}}}
json.dump(req, open(req_path, "w"), ensure_ascii=False, indent=1)
start = time.time()
request = urllib.request.Request("http://127.0.0.1:18083/api/a2a/ewallet-agents/fraud-coordinator/",
                                 data=json.dumps(req).encode(), headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(request, timeout=650) as response:
    body = json.loads(response.read())
json.dump(body, open(task_path, "w"), ensure_ascii=False, indent=1)
state = body.get("result", {}).get("status", {}).get("state")
json.dump({"elapsed_seconds": round(time.time() - start, 3), "terminal_state": state}, open(timing_path, "w"))
PY
      ;;
    *) die "unknown e2e case $case" ;;
  esac
  python3 - "$out/$id-task.json" "$out/$id-timing.json" <<'PY'
import json, sys
body = json.load(open(sys.argv[1])); result = body.get("result", body); timing = json.load(open(sys.argv[2]))
print(f"state={result.get('status', {}).get('state')}  elapsed={timing.get('elapsed_seconds')} s")
for artifact in result.get("artifacts", []):
    for part in artifact.get("parts", []):
        text = part.get("text") or ""
        try:
            value = json.loads(text)
        except ValueError:
            print(text[:1500]); continue
        keys = ("status", "workflow", "contract_version", "fraud_score", "features", "unavailable")
        print(json.dumps({key: value.get(key) for key in keys if key in value}, ensure_ascii=False)[:1500])
        for name, section in (value.get("specialists") or {}).items():
            drift = (section.get("drift") or [{}])[0] if isinstance(section, dict) else {}
            print(f"  {name}: status={section.get('status')} fraud_probability="
                  f"{(section.get('fraud_score') or {}).get('fraud_probability')} psi={drift.get('psi')}")
message = (result.get("status", {}).get("message") or {}).get("parts") or []
if message:
    print("message:", message[0].get("text", "")[:400])
PY
  banner "HOPS for $case (logs since $since)"
  sleep 5
  echo "-- Coordinator (A2A Gateway sandbox route, LLM Gateway for NL)"
  local pod
  for pod in $(k -n ewallet-agents get pods -l ewallet.io/agent=fraud-coordinator -o name); do
    k -n ewallet-agents logs "$pod" --since-time="$since" 2>/dev/null \
      | grep -E "a2a-sandboxes|chat/completions|task_completed|intake_rejected|task_failed" | grep -v "/api/tasks" | cut -c1-220 || true
  done
  echo "-- A2A Gateway routes"
  k -n ewallet-agents logs deploy/a2a-gateway --since-time="$since" 2>/dev/null | grep -oE '"route":"default/sandbox-[a-z-]+"' | sort | uniq -c || true
  echo "-- MCP servers (actor_id = the Sandbox identity's credential subject)"
  for pod in $(k -n ewallet-mcp get pods -o name); do
    k -n ewallet-mcp logs "$pod" --since-time="$since" 2>/dev/null | grep tool_call_completed \
      | grep -oE '"actor_id": "[^"]+"|"tool_name": "[a-z_]+"|"status": "[a-z_]+"|"trace_id": "[0-9a-f]+"' | paste -sd' ' | cut -c1-400 || true
  done | grep -v '^$' || true
  echo "-- Deployment Specialists must show no task for this request (0 = no fallback)"
  for pod in $(k -n ewallet-agents get pods -l 'ewallet.io/agent in (feature-rag-specialist,drift-specialist)' -o name); do
    printf '%s %s\n' "$pod" "$(k -n ewallet-agents logs "$pod" --since-time="$since" 2>/dev/null | grep -c 'specialist.task_started' || true)"
  done
  echo "saved under $out/$id-*"
}

negative() {
  need_port 18082; need_port 18083
  banner "AGI-08 negatives (each must observe its expected denial, with a passing control)"
  local out="$REPLAY_DIR/negative" mcp=http://127.0.0.1:18082 window drift_args fraud_args rag drift drift_on_rag
  mkdir -p "$out"
  window='{"start":"2026-05-01T00:00:00Z","end":"2026-05-31T00:00:00Z"}'
  drift_args="{\"feature_name\":\"account_behavior__account_tx_count_1h\",\"baseline_id\":\"${BASELINE}\",\"current_window\":${window}}"
  fraud_args="{\"transaction\":$(cat "$EV/inputs/transaction.json")}"
  rag=$(token mcp-token-feature-rag-specialist-feature-rag-mcp); drift=$(token mcp-token-drift-specialist-drift-mcp)
  drift_on_rag=$(token mcp-token-drift-specialist-feature-rag-mcp)
  probe() { local label=$1; shift; if "$@" > /dev/null 2>&1; then echo "PASS $label"; else echo "FAIL $label"; fi; }
  # Tokens reach the probe through its environment, never argv (as agi_campaign.sh negative does).
  EWALLET_MCP_SERVICE_TOKEN=$rag AGI_CONTROL_TOKEN=$drift probe "feature-rag credential refused by drift-mcp (401)" \
    "${AGENT_PY[@]}" "$ROOT/scripts/llm_agent/agi_probe.py" mcp --endpoint "$mcp/drift/mcp" --tool drift_detection \
    --arguments-json "$drift_args" --expect unauthenticated --control-token-env AGI_CONTROL_TOKEN \
    --tested-valid-endpoint "$mcp/feature-rag/mcp" --evidence "$out/wrong-server-credential.json"
  EWALLET_MCP_SERVICE_TOKEN=$drift_on_rag AGI_CONTROL_TOKEN=$rag probe "drift principal does not see fraud_scoring (ungranted tool)" \
    "${AGENT_PY[@]}" "$ROOT/scripts/llm_agent/agi_probe.py" mcp --endpoint "$mcp/feature-rag/mcp" --tool fraud_scoring \
    --arguments-json "$fraud_args" --expect denied --control-token-env AGI_CONTROL_TOKEN --evidence "$out/ungranted-tool.json"
  "${AGENT_PY[@]}" "$ROOT/scripts/llm_agent/agi_probe.py" request drift --request-id "replay-a2a-neg-$(date -u +%H%M%S)" \
    --feature account_behavior__account_tx_count_1h --baseline "$BASELINE" \
    --window-start 2026-05-01T00:00:00Z --window-end 2026-05-31T00:00:00Z --out "$out/a2a-request.json" > /dev/null
  probe "undeclared A2A host is not routed (404, control routed)" \
    "${AGENT_PY[@]}" "$ROOT/scripts/llm_agent/agi_probe.py" a2a-route --endpoint http://127.0.0.1:18083/ \
    --kagent-host payments-agent.ewallet-agents --control-kagent-host drift-specialist.ewallet-agents \
    --request "$out/a2a-request.json" --expect no-route --evidence "$out/a2a-unrouted.json"
  unset rag drift drift_on_rag
  echo "evidence: $out/*.json"
}

registry() {
  need_port 18121
  banner "AGI-13 Agent Registry readback (GET only)"
  mkdir -p "$REPLAY_DIR/registry"
  (cd "$ROOT" && "${TOOL_PY[@]}" -m ops.llm_agent.registry_catalog readback --registry http://127.0.0.1:18121 \
     --values-overlay infra/helm/llm-agent-platform/g3/g3.yaml --values-overlay infra/helm/llm-agent-platform/agi/agents.yaml \
     --evidence "$REPLAY_DIR/registry/readback.json") > /dev/null || true
  python3 - "$REPLAY_DIR/registry/readback.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
print("verdict:", report["verdict"], "(FAIL = the registry does not hold exactly what is deployed)")
for e in report["entries"]:
    state = "matches" if e["matches"] else ("NOT REGISTERED" if not e["registered"] else "; ".join(e["differences"][:3]))
    print(f"  {e['kind']:9} {e['name']:32} {e['tag']:7} {state}")
PY
}

# --- KEDA 2 -> 3 -> 2 ---------------------------------------------------------------------------------
timeline() {   # timeline <file>: one line every 5 s until killed
  while true; do
    printf '%s ' "$(date -u +%H:%M:%SZ)"
    k -n ewallet-agents get hpa fraud-coordinator feature-rag-specialist --no-headers \
      | awk '{printf "%s target=%s replicas=%s | ", $1, $3, $(NF-1)}'
    k -n ewallet-agents get pods -l 'ewallet.io/agent in (fraud-coordinator,feature-rag-specialist)' --no-headers \
      | awk '{n[$2]++} END {for (s in n) printf "%s pods %s; ", n[s], s}'
    echo; sleep 5
  done >> "$1"
}
keda() {
  local out="$REPLAY_DIR/keda"; mkdir -p "$out"
  banner "BASELINE: expect 2/2 replicas, metric 0/2 (screenshot AGENT_08_KEDA_BASELINE)"
  k -n ewallet-agents get scaledobject,hpa
  k -n ewallet-agents get deploy fraud-coordinator feature-rag-specialist
  timeline "$out/timeline.txt" & local tl=$!
  trap 'kill $tl 2>/dev/null' EXIT
  echo "live timeline: tail -f $out/timeline.txt   (or: watch -n2 kubectl --context $CTX -n ewallet-agents get hpa,pods)"
  export KUBE_CONTEXT=$CTX EXP_APPROVED=yes EXP_CONCURRENCY=8 EXP_MAX_REQUESTS=100 EXP_DURATION=300
  banner "LOAD 1: 100 typed tasks, concurrency 8 (~45 s). SCALE_OUT expected within ~40 s: desired 3, then a new pod Ready (screenshot AGENT_09_KEDA_SCALEOUT now)"
  EXP_SCALE_IN_WAIT=0 bash "$ROOT/scripts/experiments/agent_experiment.sh" exp-c5-keda optimized run "$out/agr06" | tail -1
  banner "LOAD 2 on 3 replicas: the scale-out pods must complete real tasks"
  local since; since=$(date -u +%FT%TZ)
  EXP_SCALE_IN_WAIT=420 bash "$ROOT/scripts/experiments/agent_experiment.sh" exp-c5-keda optimized run "$out/agr06" | tail -1 &
  local load2=$!
  sleep 120
  banner "Tasks per pod since LOAD 2 (the newest pod must have > 0) (screenshot AGENT_09b_KEDA_NEW_POD_WORK)"
  per_pod "$since" | tee "$out/per-pod.txt"
  banner "SCALE_IN: ~5 min after load ends replicas return to 2 (screenshot AGENT_10_KEDA_SCALEIN when 2/2)"
  wait "$load2" || true
  kill "$tl" 2>/dev/null; trap - EXIT
  k -n ewallet-agents get hpa; k -n ewallet-agents get events --field-selector reason=SuccessfulRescale \
    -o custom-columns=TIME:.lastTimestamp,OBJECT:.involvedObject.name,MESSAGE:.message | tail -6 | tee "$out/rescale-events.txt"
  for run in "$out"/agr06/optimized/raw/run-*; do
    python3 -c "import json,sys;d=json.load(open(sys.argv[1]))['levels']['load'];print(sys.argv[1].rsplit('/',1)[-1], {k:d.get(k) for k in ('requests','succeeded','availability_percent','peak_replicas','e2e_ms')})" "$run/summary.json"
  done
  echo "timeline: $out/timeline.txt"
}
per_pod() {
  local since=$1 agent pod
  for agent in fraud-coordinator feature-rag-specialist; do
    for pod in $(k -n ewallet-agents get pods -l "ewallet.io/agent=$agent" -o name); do
      printf '%s created=%s tasks_completed=%s\n' "${pod#pod/}" \
        "$(k -n ewallet-agents get "$pod" -o jsonpath='{.metadata.creationTimestamp}')" \
        "$(k -n ewallet-agents logs "$pod" --since-time="$since" 2>/dev/null | grep -cE 'task_completed' || true)"
    done
  done
}

# --- shared workers -----------------------------------------------------------------------------------
workers() {
  need_port 18383
  local out="$REPLAY_DIR/workers"; mkdir -p "$out"
  banner "SHARED WORKERS: sequential sessions of both Sandbox agents on the 2-worker pool (~6 min)"
  "${TOOL_PY[@]}" -m ops.experiments.substrate_fleet record --url http://127.0.0.1:18383 --namespace ewallet-sandbox \
    --out "$out/status.jsonl" --interval 1 --duration 1200 > "$out/record.log" 2>&1 & local rec=$!
  local agent
  for agent in feature-rag-specialist drift-specialist feature-rag-specialist; do
    "${TOOL_PY[@]}" -m ops.experiments.sandbox_sessions --url http://127.0.0.1:18383 --run-id "replay-$agent-$RANDOM" \
      --agents "$agent" --contexts 4 --messages 1 >> "$out/sessions.log" 2>&1
    echo "$agent: $(grep -c '"ok": true' "$out/sessions.log") ok session turns so far"
  done
  sleep 5; kill "$rec" 2>/dev/null || true
  k -n ewallet-sandbox get actortemplate -o 'jsonpath={range .items[*]}{.status.goldenActorID}{"\n"}{end}' > "$out/golden-ids.txt"
  banner "Actor -> worker assignment, golden snapshot actors excluded (screenshot AGENT_11_SHARED_WORKERS)"
  python3 - "$out/status.jsonl" "$out/golden-ids.txt" <<'PY' | tee "$out/sharing.txt"
import collections, json, sys
golden = {line.strip() for line in open(sys.argv[2]) if line.strip()}
seen = collections.defaultdict(lambda: collections.defaultdict(set))
for line in open(sys.argv[1]):
    for agent, actors in json.loads(line).get("actors", {}).items():
        for actor in actors:
            if actor["actor"] not in golden and actor.get("pod"):
                seen[actor["pod"]][agent].add(actor["actor"])
for pod, agents in sorted(seen.items()):
    print(pod, {agent: len(ids) for agent, ids in agents.items()})
shared = len(seen) >= 2 and all(len(agents) == 2 for agents in seen.values())
print("verdict:", "SHARED_WORKERS_OBSERVED" if shared else "NOT_OBSERVED (placement varies: run again)")
PY
}

failover() {
  local out="$REPLAY_DIR/failover"; mkdir -p "$out"
  export KUBE_CONTEXT=$CTX MCP_IMAGE PROBE_IMAGE=$COORDINATOR_IMAGE EXP_APPROVED=yes EXP_FAILURE_MODE=force EXP_RATE=10
  EXP_PYTHON=$("${TOOL_PY[@]}" -c 'import sys; print(sys.executable)'); export EXP_PYTHON
  local arm
  for arm in baseline optimized; do
    banner "FAILOVER $arm: ewallet-mcp-exp only; one pod force-deleted at 40 s (watch: kubectl --context $CTX -n ewallet-mcp-exp get pods -w)"
    bash "$ROOT/scripts/experiments/mcp_experiment.sh" "$arm" run "$out" | tail -1
    local run; run=$(ls -d "$out/$arm/raw"/run-* | sort | tail -1)
    python3 - "$run/summary.json" "$out/$arm/logs/$(basename "$run")/probe.log" <<'PY'
import json, sys
load = json.load(open(sys.argv[1]))["levels"]["load"]
rows = [json.loads(line.split(" ", 1)[1]) for line in open(sys.argv[2]) if line.startswith("LOAD_ROW")]
print({key: load.get(key) for key in ("requests", "succeeded", "availability_percent", "recovery_seconds")},
      "client_retries:", sum(row.get("retries", 0) for row in rows))
PY
    cmp -s "$out/$arm/logs/$(basename "$run")/production-before.txt" "$out/$arm/logs/$(basename "$run")/production-after.txt" \
      && echo "production ewallet-mcp unchanged" || echo "WARNING: production ewallet-mcp differs"
  done
}

case "${1:-}" in
  status) status ;;
  pf) start_pf ;;
  stop-pf) stop_pf ;;
  e2e) shift; e2e "$@" ;;
  negative) negative ;;
  registry) registry ;;
  keda) keda ;;
  workers) workers ;;
  failover) failover ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
