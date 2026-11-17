#!/usr/bin/env bash
# Owner evidence replay for the EDAI-2 Agent Platform (docs/edai2/agents/OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md).
#
# A thin wrapper around the tools the campaigns validated on GKE. It collects observations (kubectl/helm JSON,
# A2A responses, probe evidence) into one directory per invocation; ops/llm_agent/replay_verdict.py decides
# PASS/FAIL and the exit code. It never changes a Helm release, Secret, policy, Registry entry or production
# Deployment, and it never runs the lf-agents cleanup.
#
#   agent_replay.sh status              [READ-ONLY]  releases, Coordinator, gateways, SandboxAgents, WorkerPool, MCP,
#                                                    KEDA, retired workloads absent
#   agent_replay.sh pf                  [LOCAL]      start the port-forwards the UIs and probes need
#   agent_replay.sh stop-pf             [LOCAL]      stop only the port-forwards this script started
#   agent_replay.sh registry            [READ-ONLY]  strict Agent Registry readback against the live values (AGI-13)
#   agent_replay.sh e2e <case>          [TRAFFIC]    one business request through the Coordinator, verdict, hops
#                                                    case: fraud | drift | combined | nl-fraud | nl-drift
#   agent_replay.sh negative            [READ-ONLY]  wrong credential, ungranted tool, undeclared route (AGI-08)
#   agent_replay.sh keda                [READ-ONLY]  both ScaledObjects, their HPAs and replicas, checked (exit 1 on a failed
#                                                    or malformed observation); Ready is not a scale-out result
#   agent_replay.sh autoscale           [LOAD]       PRIMARY autoscaling replay (B′ profile): 100 typed tasks, concurrency 8,
#                                                    at most 900 s, through scripts/experiments/agent_experiment.sh; samples
#                                                    both autoscalers (2->3->2), worker pods, Substrate actors per worker and
#                                                    kubectl top; needs REPLAY_AUTOSCALE_APPROVED=yes
#   agent_replay.sh keda demo           [LOAD]       HELPER ONLY, not part of the owner sequence: 6 typed requests + replica
#                                                    timeline; may not scale; needs REPLAY_LOAD_APPROVED=yes
#   agent_replay.sh workers             [SESSIONS]   Sandbox sessions (new + resume) of both agents, placement, latency;
#                                                    needs REPLAY_SESSIONS_APPROVED=yes
#   agent_replay.sh failover            [EXPERIMENT] isolated MCP failover in ewallet-mcp-exp (pod force-deleted there);
#                                                    needs REPLAY_EXPERIMENT_APPROVED=yes; never part of the default replay
#
# Env: AGI_ENV (default from ~/.cache/ewallet-agi/current.env; needs CTX, EV with inputs/transaction.json, BASELINE),
#   REPLAY_DIR (required; set once for the whole owner campaign, every command writes a subdirectory of it),
#   UV (default uv on PATH or the fintech env).
#   Test seams: KUBECTL, HELM, SS, PROC_ROOT, PF_SETTLE, HOPS_SETTLE, PF_PIDS, KEDA_SAMPLE, KEDA_POLL, KEDA_COOLDOWN, WORKERS_CONTEXTS,
#   AUTOSCALE_DRIVER, AUTOSCALE_SAMPLE, AUTOSCALE_POLL, AUTOSCALE_ACTOR_INTERVAL, AUTOSCALE_TOP_INTERVAL, AUTOSCALE_SCALE_IN_WAIT, AUTOSCALE_COOLDOWN.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
die() { echo "agent_replay: $*" >&2; exit 1; }
banner() { printf '\n==================== %s  %s ====================\n' "$(date -u +%H:%M:%SZ)" "$*"; }

[ -n "${AGI_ENV:-}" ] || { [ -f "$HOME/.cache/ewallet-agi/current.env" ] && source "$HOME/.cache/ewallet-agi/current.env"; }
[ -f "${AGI_ENV:-}" ] || die "AGI_ENV must name the campaign env file"
# shellcheck disable=SC1090
source "$AGI_ENV"
: "${CTX:?}" "${EV:?}" "${BASELINE:?}"
KUBECTL=${KUBECTL:-kubectl} HELM=${HELM:-helm} SS=${SS:-ss} PROC_ROOT=${PROC_ROOT:-/proc} PF_SETTLE=${PF_SETTLE:-10} HOPS_SETTLE=${HOPS_SETTLE:-5}
UV=${UV:-$(command -v uv || echo "$HOME/miniconda3/envs/fintech/bin/uv")}
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
AGENT_PY=("$UV" run --frozen --project "$ROOT/infra/docker/g3-byo-coordinator" python)
TOOL_PY=("$UV" run --locked --project "$ROOT/infra/docker/llm-agent-tooling" python)
VERDICT=(python3 -m ops.llm_agent.replay_verdict)   # stdlib only
# One REPLAY_DIR for the whole owner campaign: commands that write evidence refuse to invent a per-command directory.
case "${1:-}" in pf|stop-pf|__request-id|"") ;; *) [ -n "${REPLAY_DIR:-}" ] || die "set REPLAY_DIR once for the whole owner campaign (see the runbook, Phase 0)" ;; esac
REPLAY_DIR=${REPLAY_DIR:-}
PF_PIDS=${PF_PIDS:-$HOME/.cache/ewallet-agi/replay-port-forwards.pids}
TRANSACTION=${TRANSACTION:-$EV/inputs/transaction.json}
k() { "$KUBECTL" --context "$CTX" "$@"; }
verdict() { (cd "$ROOT" && "${VERDICT[@]}" "$@"); }
need_uv() { [ -x "$UV" ] || die "uv not found; set UV"; }
[ "$("$KUBECTL" config view -o jsonpath="{.contexts[?(@.name=='$CTX')].name}")" = "$CTX" ] || die "kube context $CTX missing"

# --- request ids --------------------------------------------------------------------------------------------
# UTC second + PID + 32 random bits: jobs launched in the same second (keda demo) never share an id, so the
# Coordinator never answers idempotency_in_progress for a different request.
request_id() { printf 'replay-%s-%s-%s-%s' "$1" "$(date -u +%Y%m%dT%H%M%SZ)" "$$" "$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"; }

# --- port-forwards ------------------------------------------------------------------------------------------
PORT_FORWARDS=(
  "ewallet-agents svc/a2a-gateway 18083:8083"
  "ewallet-agents svc/mcp-gateway 18082:8080"
  "kagent svc/kagent-controller 18383:8083"
  "kagent svc/kagent-ui 18300:8080"
  "ewallet-agents svc/grafana 13000:3000"
  "ewallet-prompts svc/prompt-registry-agentregistry 18121:12121"
)
listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
listener_pid() { "$SS" -ltnpH "sport = :$1" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2; }
cmdline() { [ -r "$PROC_ROOT/$1/cmdline" ] && tr '\0' ' ' < "$PROC_ROOT/$1/cmdline"; }
is_forward() {   # is_forward <pid> <ns> <target> <ports>: a kubectl port-forward to exactly this target
  local line; line=$(cmdline "$1") || return 1
  [[ "$line" == *kubectl* && "$line" == *" port-forward "* && "$line" == *" $3 "* && "$line" == *" $4"* \
     && "$line" == *"$CTX"* && "$line" == *"-n $2 "* ]]
}
spec_for_port() { local spec; for spec in "${PORT_FORWARDS[@]}"; do [[ "$spec" == *" $1:"* ]] && echo "$spec"; done; return 0; }
forward_ok() {   # forward_ok <local port>: the listener is this script's kind of port-forward for that port
  local spec ns target ports pid; spec=$(spec_for_port "$1"); [ -n "$spec" ] || return 1
  read -r ns target ports <<< "$spec"; pid=$(listener_pid "$1")
  [ -n "$pid" ] && is_forward "$pid" "$ns" "$target" "$ports"
}
start_pf() {
  mkdir -p "$(dirname "$PF_PIDS")"
  local spec ns target ports port pid failed=0 waited
  for spec in "${PORT_FORWARDS[@]}"; do
    read -r ns target ports <<< "$spec"; port=${ports%%:*}
    if listening "$port"; then
      if forward_ok "$port"; then echo "OK      127.0.0.1:$port  existing port-forward $ns $target (pid $(listener_pid "$port"))"
      else
        pid=$(listener_pid "$port")
        echo "FAIL    127.0.0.1:$port  held by another process (pid ${pid:-unknown}, $(cat "$PROC_ROOT/${pid:-0}/comm" 2>/dev/null || echo '?')); not a port-forward to $ns $target. Free the port; nothing was killed"
        failed=1
      fi
      continue
    fi
    if ! k -n "$ns" get "$target" > /dev/null 2>&1; then echo "FAIL    127.0.0.1:$port  $ns $target does not exist"; failed=1; continue; fi
    nohup "$KUBECTL" --context "$CTX" -n "$ns" port-forward "$target" "$ports" > /dev/null 2>&1 &
    pid=$!
    waited=0
    while ! listening "$port" && kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$PF_SETTLE" ]; do sleep 1; waited=$((waited + 1)); done
    if listening "$port" && kill -0 "$pid" 2>/dev/null; then
      echo "$pid $port $ns $target $ports" >> "$PF_PIDS"; echo "OK      127.0.0.1:$port  port-forward $ns $target (pid $pid)"
    else
      kill "$pid" 2>/dev/null || true
      echo "FAIL    127.0.0.1:$port  port-forward $ns $target did not start"; failed=1
    fi
  done
  [ "$failed" = 0 ] || die "port-forwards incomplete (see FAIL lines)"
}
stop_pf() {
  [ -f "$PF_PIDS" ] || { echo "no port-forwards recorded"; return; }
  local pid port ns target ports
  while read -r pid port ns target ports; do
    [ -n "$pid" ] || continue
    if [ -n "${target:-}" ] && is_forward "$pid" "$ns" "$target" "$ports"; then
      kill "$pid" 2>/dev/null && echo "stopped $pid ($ns $target)"
    else
      echo "skipped $pid: not a port-forward this script started (PID reused or old record)"
    fi
  done < "$PF_PIDS"
  rm -f "$PF_PIDS"
}
need_port() { forward_ok "$1" || die "127.0.0.1:$1 is not this script's port-forward; run: $0 pf"; }
token() { k -n "${2:-ewallet-agents}" get secret "$1" -o jsonpath='{.data.token}' | base64 -d; }

# --- status -------------------------------------------------------------------------------------------------
status() {
  local out="$REPLAY_DIR/status" ns
  mkdir -p "$out"
  "$HELM" --kube-context "$CTX" -n default history llm-agent --max 1 -o json > "$out/helm-llm-agent.json"
  "$HELM" --kube-context "$CTX" -n ewallet-mcp history llm-agent-mcp --max 1 -o json > "$out/helm-llm-agent-mcp.json"
  for ns in ewallet-agents ewallet-mcp; do k -n "$ns" get deployments -o json > "$out/deployments-$ns.json"; done
  k -n ewallet-mcp get pdb -o json > "$out/pdb-ewallet-mcp.json"
  k -n ewallet-agents get agents.kagent.dev -o json > "$out/agents.json"
  k -n ewallet-sandbox get sandboxagents.kagent.dev -o json > "$out/sandboxagents.json"
  k -n ewallet-sandbox get workerpools.ate.dev -o json > "$out/workerpools.json"
  for ns in ewallet-agents ewallet-sandbox; do
    k -n "$ns" get scaledobjects.keda.sh -o json > "$out/scaledobjects-$ns.json"
    k -n "$ns" get hpa -o json > "$out/hpa-$ns.json"
  done
  for ns in ewallet-agents ewallet-mcp ewallet-sandbox; do k -n "$ns" get pods -o json > "$out/pods-$ns.json"; done
  banner "REPLAY-01 platform state (read-only)"
  {   # display only (for the screenshot); the checks below read the JSON snapshot
    k get nodes -L ewallet.io/g3-llm
    "$HELM" --kube-context "$CTX" -n default history llm-agent --max 1
    "$HELM" --kube-context "$CTX" -n ewallet-mcp history llm-agent-mcp --max 1
    banner "Coordinator, gateways and the BYO Agent (ewallet-agents)"
    k -n ewallet-agents get deploy fraud-coordinator a2a-gateway mcp-gateway agentgateway
    k -n ewallet-agents get agents.kagent.dev
    banner "Sandbox fleet (SandboxAgents, shared WorkerPool, gVisor ActorTemplates)"
    k -n ewallet-sandbox get sandboxagents.kagent.dev,workerpools.ate.dev,actortemplates.ate.dev
    k -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents -o wide
    banner "MCP servers (ewallet-mcp: 2 replicas, PDB, RollingUpdate)"
    k -n ewallet-mcp get deploy feature-rag-mcp drift-mcp \
      -o custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,REPLICAS:.spec.replicas,STRATEGY:.spec.strategy.type,MAXUNAVAILABLE:.spec.strategy.rollingUpdate.maxUnavailable
    k -n ewallet-mcp get pdb
    banner "KEDA (Coordinator and shared WorkerPool)"
    k -n ewallet-agents get scaledobjects.keda.sh,hpa
    k -n ewallet-sandbox get scaledobjects.keda.sh,hpa
  } || true
  banner "Checks"
  verdict status --snapshot "$out" --out "$out/verdict.json"
}

# --- business E2E -------------------------------------------------------------------------------------------
NL_FRAUD="Hãy đánh giá rủi ro gian lận cho giao dịch payment dc58ff4e-b5f7-4486-b1f8-4c31383741a9 (user 55561d92-d042-401c-b02a-b03e3ed280f7, account 2d48f147-f98c-4819-b183-f94ca049c175, device e7765b81-c9f9-4147-aa00-46f34de1d7da, merchant e3e4aced-6b0d-40af-be5c-eef9255204c9, amount 275465.53 VND, channel app, event_timestamp 2026-06-01T00:00:00Z): lấy feature của user/account, gọi mô hình dự đoán gian lận, giải thích mức rủi ro và trích dẫn chính sách liên quan."
NL_DRIFT="Phân tích drift của account_behavior__account_tx_count_1h so với baseline bl-r5gb-202604.account_tx_count_1h trong window 2026-05-01T00:00:00Z to 2026-05-31T00:00:00Z."
e2e() {
  local case=${1:?case: fraud|drift|combined|nl-fraud|nl-drift} id out rc
  case "$case" in fraud|drift|combined|nl-fraud|nl-drift) ;; *) die "unknown e2e case $case" ;; esac
  need_uv; need_port 18083
  id=$(request_id "$case"); out="$REPLAY_DIR/e2e"; mkdir -p "$out"
  banner "E2E $case  request_id $id  (Coordinator -> A2A Gateway -> SandboxAgent -> MCP Gateway -> ewallet-mcp)"
  local since; since=$(date -u +%FT%TZ)
  case "$case" in
    fraud|drift|combined)
      local args=(--request-id "$id")
      [ "$case" != drift ] && args+=(--transaction "$TRANSACTION" --policy-query "payment anomaly policy and step-up authentication requirements")
      [ "$case" != fraud ] && args+=(--feature account_behavior__account_tx_count_1h --baseline "$BASELINE" \
        --window-start 2026-05-01T00:00:00Z --window-end 2026-05-31T00:00:00Z)
      "${AGENT_PY[@]}" "$ROOT/scripts/llm_agent/agi_probe.py" request "$case" "${args[@]}" --out "$out/$id-request.json" > /dev/null
      # The smoke's own exit code is advisory; the verdict below reads the saved response.
      "${AGENT_PY[@]}" "$ROOT/scripts/llm_agent/g3_a2a_smoke.py" --endpoint http://127.0.0.1:18083/api/a2a/ewallet-agents/fraud-coordinator/ \
        --request "$out/$id-request.json" --response "$out/$id-task.json" --timing "$out/$id-timing.json" > /dev/null || true ;;
    nl-fraud|nl-drift)
      local text=$NL_FRAUD; [ "$case" = nl-drift ] && text=$NL_DRIFT
      "${AGENT_PY[@]}" - "$out/$id-request.json" "$out/$id-task.json" "$out/$id-timing.json" "$id" "$text" <<'PY'
import json, sys, time, urllib.error, urllib.request, uuid
req_path, task_path, timing_path, mid, text = sys.argv[1:6]
req = {"jsonrpc": "2.0", "id": mid, "method": "message/send", "params": {"message": {
    "role": "user", "messageId": mid, "contextId": str(uuid.uuid4()), "parts": [{"kind": "text", "text": text}]}}}
json.dump(req, open(req_path, "w"), ensure_ascii=False, indent=1)
start, status, body = time.time(), None, None
request = urllib.request.Request("http://127.0.0.1:18083/api/a2a/ewallet-agents/fraud-coordinator/",
                                 data=json.dumps(req).encode(), headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(request, timeout=650) as response:
        status, body = response.status, json.loads(response.read())
except urllib.error.HTTPError as exc:   # recorded, then judged by the verdict
    status, body = exc.code, {"error": {"http_status": exc.code}}
except (OSError, ValueError) as exc:
    body = {"error": {"transport": type(exc).__name__}}
json.dump(body, open(task_path, "w"), ensure_ascii=False, indent=1)
state = ((body.get("result") or {}).get("status") or {}).get("state") if isinstance(body, dict) else None
json.dump({"http_status": status, "elapsed_seconds": round(time.time() - start, 3), "terminal_state": state}, open(timing_path, "w"))
PY
      ;;
  esac
  rc=0
  verdict e2e --case "$case" --task "$out/$id-task.json" --timing "$out/$id-timing.json" --request-id "$id" \
    --out "$out/$id-verdict.json" || rc=$?
  python3 - "$out/$id-task.json" <<'PY' || true
import json, sys
try:
    result = json.load(open(sys.argv[1])).get("result") or {}
except (OSError, ValueError):
    sys.exit(0)
message = (result.get("status", {}).get("message") or {}).get("parts") or []
for artifact in result.get("artifacts", []):
    for part in artifact.get("parts", []):
        text = part.get("text") or ""
        if not text.lstrip().startswith("{"):
            print("answer:", text[:600].replace("\n", " "))
if message:
    print("message:", message[0].get("text", "")[:300])
PY
  banner "HOPS for $case (logs since $since)"
  sleep "$HOPS_SETTLE"
  echo "-- Coordinator (A2A Gateway sandbox route; LLM Gateway for NL)"
  local pod
  for pod in $(k -n ewallet-agents get pods -l ewallet.io/agent=fraud-coordinator -o name); do
    k -n ewallet-agents logs "$pod" --since-time="$since" 2>/dev/null \
      | grep -E "a2a-sandboxes|chat/completions|task_completed|intake_rejected|task_failed" | grep -v "/api/tasks" | cut -c1-220 || true
  done
  echo "-- A2A Gateway routes"
  k -n ewallet-agents logs deploy/a2a-gateway --since-time="$since" 2>/dev/null | grep -oE '"route":"default/sandbox-[a-z-]+"' | sort | uniq -c || true
  echo "-- MCP servers in ewallet-mcp (actor_id = the SandboxAgent credential subject)"
  for pod in $(k -n ewallet-mcp get pods -o name); do
    k -n ewallet-mcp logs "$pod" --since-time="$since" 2>/dev/null | grep tool_call_completed \
      | grep -oE '"actor_id": "[^"]+"|"tool_name": "[a-z_]+"|"status": "[a-z_]+"|"trace_id": "[0-9a-f]+"' | paste -sd' ' | cut -c1-400 || true
  done | grep -v '^$' || true
  echo "-- retired Specialist Deployments (expected 0; Specialists run only as SandboxAgents): $(k -n ewallet-agents get deployment feature-rag-specialist drift-specialist --ignore-not-found -o name | wc -l)"
  echo "saved under $out/$id-*"
  return "$rc"
}

# --- AGI-08 negatives ---------------------------------------------------------------------------------------
negative() {
  need_uv; need_port 18082; need_port 18083
  banner "AGI-08 negatives (each must observe its expected denial, with a passing control)"
  local out="$REPLAY_DIR/negative" mcp=http://127.0.0.1:18082 window drift_args rag drift controller controller_secret
  local codes=() rc
  mkdir -p "$out"
  window='{"start":"2026-05-01T00:00:00Z","end":"2026-05-31T00:00:00Z"}'
  drift_args="{\"feature_name\":\"account_behavior__account_tx_count_1h\",\"baseline_id\":\"${BASELINE}\",\"current_window\":${window}}"
  # Live principals only: the SandboxAgent subjects and the kagent controller's list-only credential, read from its
  # RemoteMCPServer. The guard refuses the run if any of these names is a retired credential.
  controller_secret=$(k -n ewallet-agents get remotemcpservers.kagent.dev drift-mcp -o jsonpath='{.spec.headersFrom[0].valueFrom.name}')
  printf '%s\n' mcp-token-sandbox-feature-rag-feature-rag-mcp-sbx-20261115 mcp-token-sandbox-drift-drift-mcp-sbx-20261115 \
    "$controller_secret" | (cd "$ROOT" && python3 -m ops.llm_agent.retired_credentials check - --label "replay negative credentials") \
    || die "a negative would use a retired credential"
  rag=$(token mcp-token-sandbox-feature-rag-feature-rag-mcp-sbx-20261115 ewallet-sandbox)
  drift=$(token mcp-token-sandbox-drift-drift-mcp-sbx-20261115 ewallet-sandbox)
  controller=$(token "$controller_secret")
  # Tokens reach the probe through its environment, never argv or output (as agi_campaign.sh negative does).
  rc=0; EWALLET_MCP_SERVICE_TOKEN=$rag AGI_CONTROL_TOKEN=$drift "${AGENT_PY[@]}" "$ROOT/scripts/llm_agent/agi_probe.py" mcp \
    --endpoint "$mcp/drift/mcp" --tool drift_detection --arguments-json "$drift_args" --expect unauthenticated \
    --control-token-env AGI_CONTROL_TOKEN --tested-valid-endpoint "$mcp/feature-rag/mcp" \
    --evidence "$out/wrong-server-credential.json" > "$out/wrong-server-credential.log" 2>&1 || rc=$?
  codes+=(--exit-code "wrong-server-credential.json=$rc")
  rc=0; EWALLET_MCP_SERVICE_TOKEN=$controller AGI_CONTROL_TOKEN=$drift "${AGENT_PY[@]}" "$ROOT/scripts/llm_agent/agi_probe.py" mcp \
    --endpoint "$mcp/drift/mcp" --tool drift_detection --arguments-json "$drift_args" --expect call-denied \
    --control-token-env AGI_CONTROL_TOKEN --evidence "$out/ungranted-tool.json" > "$out/ungranted-tool.log" 2>&1 || rc=$?
  codes+=(--exit-code "ungranted-tool.json=$rc")
  unset rag drift controller
  "${AGENT_PY[@]}" "$ROOT/scripts/llm_agent/agi_probe.py" request drift --request-id "$(request_id a2a-neg)" \
    --feature account_behavior__account_tx_count_1h --baseline "$BASELINE" \
    --window-start 2026-05-01T00:00:00Z --window-end 2026-05-31T00:00:00Z --out "$out/a2a-request.json" > /dev/null
  rc=0; "${AGENT_PY[@]}" "$ROOT/scripts/llm_agent/agi_probe.py" a2a-route --endpoint http://127.0.0.1:18083/ \
    --kagent-host payments-agent.ewallet-agents --control-path /api/a2a-sandboxes/ewallet-sandbox/drift-specialist/ \
    --request "$out/a2a-request.json" --expect no-route --evidence "$out/a2a-unrouted.json" > "$out/a2a-unrouted.log" 2>&1 || rc=$?
  codes+=(--exit-code "a2a-unrouted.json=$rc")
  verdict negative --dir "$out" "${codes[@]}" --out "$out/verdict.json"
}

# --- Agent Registry -----------------------------------------------------------------------------------------
registry() {
  need_uv; need_port 18121
  banner "AGI-13 Agent Registry strict readback (GET only; nothing is published)"
  local out="$REPLAY_DIR/registry" sandbox_image rc=0
  mkdir -p "$out"
  # The catalog is rendered from the live Helm values and the live SandboxAgent fleet image, not from files alone.
  "$HELM" --kube-context "$CTX" -n default get values llm-agent -o yaml > "$out/llm-agent-live-values.yaml"
  sandbox_image=$(k -n ewallet-sandbox get sandboxagents.kagent.dev feature-rag-specialist -o json \
    | grep -oE '[a-z0-9.-]+/[a-z0-9./-]+@sha256:[0-9a-f]{64}' | head -1)
  [ -n "$sandbox_image" ] || die "cannot read the SandboxAgent fleet image"
  echo "SandboxAgent fleet image: ${sandbox_image##*/}"
  (cd "$ROOT" && "${TOOL_PY[@]}" -m ops.llm_agent.registry_catalog readback --registry http://127.0.0.1:18121 \
     --values-overlay infra/helm/llm-agent-platform/g3/g3.yaml --values-overlay infra/helm/llm-agent-platform/agi/agents.yaml \
     --values-overlay "$out/llm-agent-live-values.yaml" --sandbox-image "$sandbox_image" \
     --evidence "$out/readback.json") > "$out/readback.log" 2>&1 || rc=$?
  verdict registry --readback "$out/readback.json" --exit-code "$rc" --out "$out/verdict.json"
}

# --- KEDA: Coordinator and shared WorkerPool ----------------------------------------------------------------
replicas_sample() {
  printf '%s coord=%s pool=%s\n' "$(date -u +%H:%M:%SZ)" \
    "$(k -n ewallet-agents get deploy fraud-coordinator -o jsonpath='{.spec.replicas}')" \
    "$(k -n ewallet-sandbox get workerpools.ate.dev business-agents -o jsonpath='{.spec.replicas}')"
}
keda_status() {   # keda_status [dir]: read-only; every required observation is saved and judged
  local out=${1:-$REPLAY_DIR/keda-status} name rc
  mkdir -p "$out"; : > "$out/queries.tsv"
  query() {   # query <name> <namespace> <kubectl get args...>: JSON to <name>.json, exit code to queries.tsv
    name=$1; shift; local ns=$1; shift; rc=0
    k -n "$ns" get "$@" -o json > "$out/$name.json" 2> "$out/$name.err" || rc=$?
    printf '%s\t%s\n' "$name" "$rc" >> "$out/queries.tsv"
  }
  query scaledobject-coordinator ewallet-agents scaledobjects.keda.sh fraud-coordinator
  query scaledobject-workerpool ewallet-sandbox scaledobjects.keda.sh business-agents-workerpool
  query hpa-ewallet-agents ewallet-agents hpa
  query hpa-ewallet-sandbox ewallet-sandbox hpa
  query deploy-coordinator ewallet-agents deploy fraud-coordinator
  query workerpool ewallet-sandbox workerpools.ate.dev business-agents
  banner "KEDA (read-only): Coordinator and shared WorkerPool"
  {   # display only; the checks below read the saved JSON
    k -n ewallet-agents get scaledobjects.keda.sh,hpa
    k -n ewallet-sandbox get scaledobjects.keda.sh,hpa
    echo "-- recent rescale events"
    k -n ewallet-agents get events --field-selector reason=SuccessfulRescale \
      -o custom-columns=TIME:.lastTimestamp,OBJECT:.involvedObject.name,MESSAGE:.message | tail -4
    k -n ewallet-sandbox get events --field-selector reason=SuccessfulRescale \
      -o custom-columns=TIME:.lastTimestamp,OBJECT:.involvedObject.name,MESSAGE:.message | tail -4
  } 2>&1 || echo "(display commands failed; see the checks)"
  verdict keda-status --dir "$out" --out "$out/verdict.json"
}
worker_pods_sample() {   # name created ready-transition for each worker pod, one line per sample
  printf '%s ' "$(date -u +%H:%M:%SZ)"
  k -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents \
    -o jsonpath='{range .items[*]}{.metadata.name}={.metadata.creationTimestamp}={.status.conditions[?(@.type=="Ready")].lastTransitionTime}={.status.conditions[?(@.type=="Ready")].status} {end}'
  echo
}
keda_demo() {
  [ "${REPLAY_LOAD_APPROVED:-}" = yes ] || die "keda demo sends 6 business requests; set REPLAY_LOAD_APPROVED=yes to run it"
  need_uv; need_port 18083
  local out="$REPLAY_DIR/keda" tl i case pids=() verdicts=() waited=0 limit=${SCALE_IN_WAIT:-600} after=0 rc=0
  local sample=${KEDA_SAMPLE:-10} poll=${KEDA_POLL:-15} cooldown=${KEDA_COOLDOWN:-300}
  mkdir -p "$out"
  keda_status "$out/before" > "$out/before.txt" 2>&1 || { cat "$out/before.txt"; die "KEDA is not healthy before the demo (see $out/before); no load was sent"; }
  cat "$out/before.txt"
  ( while true; do replicas_sample >> "$out/timeline.txt"; worker_pods_sample >> "$out/worker-pods.txt"; sleep "$sample"; done ) \
    > /dev/null 2>&1 & tl=$!
  SAMPLER_PIDS=("$tl")
  trap 'kill "${SAMPLER_PIDS[@]}" 2>/dev/null' EXIT
  banner "LOAD: 6 typed requests (fraud, drift, combined x2), launched 1.2 s apart (the lc-20261117T002220Z profile)"
  echo "watch: tail -f $out/timeline.txt"
  i=0
  for case in fraud drift combined fraud drift combined; do
    i=$((i + 1))
    ( REPLAY_DIR="$out/job$i" "$0" e2e "$case" > "$out/job$i-$case.txt" 2>&1 ) & pids+=($!)
    sleep 1.2
  done
  for i in "${pids[@]}"; do wait "$i" || true; done
  banner "Requests done; waiting up to ${limit} s for both autoscalers to return to 2 (cooldown ${cooldown} s)"
  while [ "$waited" -lt "$limit" ]; do
    replicas_sample | grep -q ' coord=2 pool=2$' && [ "$waited" -ge "$cooldown" ] && break
    sleep "$poll"; waited=$((waited + poll))
  done
  sleep "$sample"; kill "$tl" 2>/dev/null || true; trap - EXIT
  for i in "$out"/job*/e2e/*-verdict.json; do [ -f "$i" ] && verdicts+=(--verdict "$i"); done
  keda_status "$out/after" > "$out/after.txt" 2>&1 || after=$?
  cat "$out/after.txt"
  verdict keda --timeline "$out/timeline.txt" --worker-pods "$out/worker-pods.txt" "${verdicts[@]}" --out "$out/verdict.json" || rc=$?
  [ "$after" = 0 ] || { echo "FAIL: KEDA not healthy after the demo (see $out/after)"; rc=1; }
  return "$rc"
}
keda() { case "${1:-}" in "") keda_status ;; demo) keda_demo ;; *) die "keda takes no argument or 'demo'" ;; esac; }

# --- primary autoscaling replay (B′ profile) ------------------------------------------------------------------
top_sample() {   # <ts> <namespace> <pod> <cpu> <memory>, one line per pod; a failed read is an ERROR line
  local ts ns; ts=$(date -u +%FT%TZ)
  for ns in ewallet-agents ewallet-sandbox; do
    k -n "$ns" top pods --no-headers 2>/dev/null | awk -v ts="$ts" -v ns="$ns" '{print ts, ns, $1, $2, $3}' \
      || echo "$ts $ns ERROR top-failed"
  done
}
autoscale() {
  [ "${REPLAY_AUTOSCALE_APPROVED:-}" = yes ] \
    || die "autoscale sends 100 typed business tasks (concurrency 8, <= 900 s); set REPLAY_AUTOSCALE_APPROVED=yes after owner approval"
  need_uv; need_port 18383
  local out="$REPLAY_DIR/autoscale" waited=0 after=0 rc=0 load_rc=0 run
  local sample=${AUTOSCALE_SAMPLE:-5} actor_interval=${AUTOSCALE_ACTOR_INTERVAL:-10} top_interval=${AUTOSCALE_TOP_INTERVAL:-30}
  local limit=${AUTOSCALE_SCALE_IN_WAIT:-900} cooldown=${AUTOSCALE_COOLDOWN:-300} poll=${AUTOSCALE_POLL:-15}
  local driver=${AUTOSCALE_DRIVER:-$ROOT/scripts/experiments/agent_experiment.sh}
  [ ! -e "$out" ] || die "$out already exists; one primary autoscaling run per REPLAY_DIR (nothing was sent)"
  mkdir -p "$out"
  keda_status "$out/before" > "$out/before.txt" 2>&1 || { cat "$out/before.txt"; die "KEDA is not healthy before the run (see $out/before); no load was sent"; }
  cat "$out/before.txt"
  replicas_sample > "$out/baseline-replicas.txt"; cat "$out/baseline-replicas.txt"
  grep -q ' coord=2 pool=2$' "$out/baseline-replicas.txt" \
    || die "both autoscalers must start at 2 (see $out/baseline-replicas.txt); no load was sent"
  # Sampler PIDs are global so the EXIT trap still sees them if the script aborts.
  SAMPLER_PIDS=()
  trap 'kill "${SAMPLER_PIDS[@]}" 2>/dev/null' EXIT
  ( while true; do replicas_sample >> "$out/timeline.txt"; worker_pods_sample >> "$out/worker-pods.txt"; sleep "$sample"; done ) \
    > /dev/null 2>&1 & SAMPLER_PIDS+=($!)
  ( while true; do top_sample >> "$out/top.txt"; sleep "$top_interval"; done ) > /dev/null 2>&1 & SAMPLER_PIDS+=($!)
  # Actor placement from the kagent Substrate status API (the B′ gotcha: each poll makes ateapi log a full ListActors,
  # so the interval stays coarse).
  "${TOOL_PY[@]}" -m ops.experiments.substrate_fleet record --url http://127.0.0.1:18383 --namespace ewallet-sandbox \
    --out "$out/actors.jsonl" --interval "$actor_interval" --duration 3600 > "$out/actors.log" 2>&1 & SAMPLER_PIDS+=($!)
  banner "LOAD: 100 typed tasks, concurrency 8, at most 900 s (B′ profile) -> fraud-coordinator -> SandboxAgent on the shared WorkerPool"
  echo "watch: tail -f $out/timeline.txt   (Grafana: Agent Platform dashboard, KEDA panels)"
  date -u +%FT%TZ > "$out/load-start.txt"
  EXP_PYTHON=$("${TOOL_PY[@]}" -c 'import sys; print(sys.executable)') KUBE_CONTEXT=$CTX EXP_APPROVED=yes \
    EXP_CONCURRENCY=8 EXP_MAX_REQUESTS=100 EXP_DURATION=900 EXP_SCALE_IN_WAIT=0 \
    bash "$driver" exp-c5-keda optimized run "$out/load" > "$out/load.log" 2>&1 || load_rc=$?
  date -u +%FT%TZ > "$out/load-end.txt"
  tail -3 "$out/load.log"
  banner "Load done (rc $load_rc); waiting up to ${limit} s for both autoscalers to return to 2 (at least ${cooldown} s)"
  while [ "$waited" -lt "$limit" ]; do
    replicas_sample | grep -q ' coord=2 pool=2$' && [ "$waited" -ge "$cooldown" ] && break
    sleep "$poll"; waited=$((waited + poll))
  done
  sleep "$sample"; kill "${SAMPLER_PIDS[@]}" 2>/dev/null || true; trap - EXIT
  k -n ewallet-sandbox get actortemplates.ate.dev -o 'jsonpath={range .items[*]}{.status.goldenActorID}{"\n"}{end}' > "$out/golden-ids.txt" || true
  keda_status "$out/after" > "$out/after.txt" 2>&1 || after=$?
  cat "$out/after.txt"
  run=$( (ls -d "$out/load/optimized/raw"/run-* 2>/dev/null || true) | sort | tail -1)
  banner "Verdict: both autoscalers, the added worker's actors, workload results, latency, resources"
  verdict autoscale --timeline "$out/timeline.txt" --worker-pods "$out/worker-pods.txt" --actors "$out/actors.jsonl" \
    --golden "$out/golden-ids.txt" --top "$out/top.txt" --summary "${run:-$out/load/missing}/summary.json" \
    --load-exit-code "$load_rc" --out "$out/verdict.json" || rc=$?
  [ "$after" = 0 ] || { echo "FAIL: KEDA not healthy after the run (see $out/after)"; rc=1; }
  return "$rc"
}

# --- shared workers -----------------------------------------------------------------------------------------
workers() {
  [ "${REPLAY_SESSIONS_APPROVED:-}" = yes ] || die "workers creates Sandbox session actors; set REPLAY_SESSIONS_APPROVED=yes to run it"
  need_uv; need_port 18383
  local out="$REPLAY_DIR/workers" agent rec
  mkdir -p "$out"
  banner "SHARED WORKERS: sequential sessions (new, then resume) of both SandboxAgents on the shared WorkerPool (~12 min)"
  k -n ewallet-sandbox get sandboxagents.kagent.dev,workerpools.ate.dev | tee "$out/fleet.txt"
  "${TOOL_PY[@]}" -m ops.experiments.substrate_fleet record --url http://127.0.0.1:18383 --namespace ewallet-sandbox \
    --out "$out/status.jsonl" --interval 1 --duration 1200 > "$out/record.log" 2>&1 & rec=$!
  for agent in feature-rag-specialist drift-specialist feature-rag-specialist; do
    "${TOOL_PY[@]}" -m ops.experiments.sandbox_sessions --url http://127.0.0.1:18383 --run-id "$(request_id "$agent")" \
      --agents "$agent" --contexts "${WORKERS_CONTEXTS:-4}" --messages 2 >> "$out/sessions.log" 2>&1 || true
    echo "$agent: $(grep -c '"ok": true' "$out/sessions.log" || true) ok session turns so far (new + resume per context)"
  done
  sleep 5; kill "$rec" 2>/dev/null || true
  k -n ewallet-sandbox get actortemplates.ate.dev -o 'jsonpath={range .items[*]}{.status.goldenActorID}{"\n"}{end}' > "$out/golden-ids.txt"
  replicas_sample | tee "$out/replicas.txt"
  banner "Session outcomes and actor -> worker placement (golden snapshot actors excluded)"
  verdict workers --status-rows "$out/status.jsonl" --golden "$out/golden-ids.txt" --sessions "$out/sessions.log" \
    --out "$out/verdict.json"
}

# --- optional experiment: isolated MCP failover ---------------------------------------------------------------
failover() {
  [ "${REPLAY_EXPERIMENT_APPROVED:-}" = yes ] || die "failover creates ewallet-mcp-exp and force-deletes a pod there; set REPLAY_EXPERIMENT_APPROVED=yes (separate owner approval)"
  : "${MCP_IMAGE:?MCP_IMAGE (campaign env)}" "${COORDINATOR_IMAGE:?COORDINATOR_IMAGE (campaign env)}"
  need_uv
  local out="$REPLAY_DIR/failover"; mkdir -p "$out"
  export KUBE_CONTEXT=$CTX MCP_IMAGE PROBE_IMAGE=$COORDINATOR_IMAGE EXP_APPROVED=yes EXP_FAILURE_MODE=force EXP_RATE=10
  EXP_PYTHON=$("${TOOL_PY[@]}" -c 'import sys; print(sys.executable)'); export EXP_PYTHON
  local arm failed=0
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
    if cmp -s "$out/$arm/logs/$(basename "$run")/production-before.txt" "$out/$arm/logs/$(basename "$run")/production-after.txt"; then
      echo "production ewallet-mcp unchanged"
    else echo "FAIL: production ewallet-mcp differs"; failed=1; fi
  done
  return "$failed"
}

case "${1:-}" in
  status) status ;;
  pf) start_pf ;;
  stop-pf) stop_pf ;;
  e2e) shift; e2e "$@" ;;
  negative) negative ;;
  registry) registry ;;
  keda) shift; keda "$@" ;;
  autoscale) autoscale ;;
  workers) workers ;;
  failover) failover ;;
  __request-id) request_id "${2:?case}"; echo ;;   # test seam
  *) sed -n '2,34p' "$0"; exit 2 ;;
esac
