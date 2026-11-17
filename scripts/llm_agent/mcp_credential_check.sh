#!/usr/bin/env bash
# [READ-ONLY] MCP credential state on EVERY MCP server replica. Collects observations; the decision (and the exit
# code) comes from ops/llm_agent/mcp_credential_verdict.py, which fails on any failed, missing or malformed probe.
#
# Each replica is reached through its own pod port-forward (not the Service, so no replica is skipped). The forward
# must be held by the kubectl this script started (a port already held by another process is refused: a stale
# forward could answer for another pod), and is re-checked before every probe.
#
# Per Ready replica of the active ewallet-mcp servers (feature-rag-mcp, drift-mcp):
#   active    sandbox-<agent> (the live SandboxAgent credential): authenticated, authorized, business execution ok
#   discovery kagent-controller (list-only, from its RemoteMCPServer): authenticated, lists the tool, call denied
#   invalid   a random credential generated here (never a real one): HTTP 401 at initialize
#   (negative checks run with the active credential as the authorized control on the same replica)
# Replica counts are compared with the Deployment's spec.replicas, readyReplicas and MCP_MIN_REPLICAS.
#
# The retired in-release ewallet-agents/drift-mcp (LEGACY_SERVER):
#   absent   (default) Deployment, Service, PodDisruptionBudget, ServiceAccount and pods must all be gone
#   present  before its retirement: authentication/authorization only (active with execution not required, and
#            invalid); its known drift_baseline_unavailable is reported as backend_unavailable, never as success
#
# usage: mcp_credential_check.sh <out-dir> <label>
# Env: AGI_ENV (campaign file: CTX, EV with inputs/transaction.json, BASELINE), KUBECONFIG, UV,
#      LEGACY_SERVER=absent|present, MCP_MIN_REPLICAS (default 2),
#      SANDBOX_RAG_SECRET / SANDBOX_DRIFT_SECRET (ewallet-sandbox client Secrets; defaults are the live ones).
# Test seams: KUBECTL, SS, CURL, MCP_PROBE (replaces the agi_probe command), MCP_PYTHON (runs the verdict module).
# Tokens are read into this process and reach the probe through its environment only; nothing prints them.
set -euo pipefail
shopt -s inherit_errexit
out=${1:?out-dir} label=${2:?label}
die() { echo "mcp_credential_check: FAIL: $*" >&2; exit 1; }
[[ -f "${AGI_ENV:-}" ]] || die "AGI_ENV must name the campaign env file"
# shellcheck disable=SC1090
source "$AGI_ENV"
: "${CTX:?}" "${EV:?}" "${BASELINE:?}"
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
UV=${UV:-$(command -v uv || echo "$HOME/miniconda3/envs/fintech/bin/uv")}
KUBECTL=${KUBECTL:-kubectl} SS=${SS:-ss} CURL=${CURL:-curl}
LEGACY_SERVER=${LEGACY_SERVER:-absent} MCP_MIN_REPLICAS=${MCP_MIN_REPLICAS:-2}
SANDBOX_RAG_SECRET=${SANDBOX_RAG_SECRET:-mcp-token-sandbox-feature-rag-feature-rag-mcp-sbx-20261115}
SANDBOX_DRIFT_SECRET=${SANDBOX_DRIFT_SECRET:-mcp-token-sandbox-drift-drift-mcp-sbx-20261115}
[[ "$LEGACY_SERVER" == absent || "$LEGACY_SERVER" == present ]] || die "LEGACY_SERVER must be absent or present"
[[ "$MCP_MIN_REPLICAS" =~ ^[1-9][0-9]*$ ]] || die "MCP_MIN_REPLICAS must be a positive integer"
read -r -a PROBE <<< "${MCP_PROBE:-$UV run --frozen --project $ROOT/infra/docker/g3-byo-coordinator python $ROOT/scripts/llm_agent/agi_probe.py}"
read -r -a VERDICT_PY <<< "${MCP_PYTHON:-$UV run --frozen --project $ROOT/infra/docker/g3-byo-coordinator python}"
k() { "$KUBECTL" --context "$CTX" --request-timeout=60s "$@"; }
umask 077; mkdir -p "$out/$label"
dir="$out/$label" records="$out/$label/records.jsonl"
[[ ! -e "$records" ]] || die "$records exists; evidence is create-only"
: > "$records"

emit() {   # emit key=value ...: one JSON record; values that parse as JSON (numbers, lists, booleans) stay typed
  python3 -c 'import json, sys
record = {}
for pair in sys.argv[1:]:
    key, _, value = pair.partition("=")
    try: record[key] = json.loads(value)
    except ValueError: record[key] = value
print(json.dumps(record, sort_keys=True))' "$@" >> "$records"
}
secret() { k -n "$1" get secret "$2" -o jsonpath='{.data.token}' | base64 -d; }

window='{"start":"2026-05-01T00:00:00Z","end":"2026-05-31T00:00:00Z"}'
DRIFT_ARGS="{\"feature_name\":\"account_behavior__account_tx_count_1h\",\"baseline_id\":\"${BASELINE}\",\"current_window\":${window}}"
RAG_ARGS=$(python3 -c 'import json,sys;print(json.dumps({"transaction":json.load(open(sys.argv[1])),"feature_names":["account_behavior__account_tx_count_1h"]}))' "$EV/inputs/transaction.json")

pf_pid="" pf_port=""
cleanup() {   # kubectl itself is the background process (exec), so this really closes the forward
  if [[ -n "$pf_pid" ]]; then kill "$pf_pid" 2>/dev/null || true; wait "$pf_pid" 2>/dev/null || true; fi
  pf_pid="" pf_port=""
}
trap cleanup EXIT
listener_pid() { "$SS" -ltnpH "sport = :$1" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2 || true; }
forward() {   # forward <ns> <pod> <port>: exactly this pod on a port nobody else holds; returns 1 with a reason
  cleanup
  if [[ -n "$(listener_pid "$3")" ]]; then
    forward_reason="127.0.0.1:$3 already held by another process (stale forward refused)"; return 1
  fi
  ( exec "$KUBECTL" --context "$CTX" --request-timeout=60s -n "$1" port-forward --address 127.0.0.1 "pod/$2" "$3:8080" \
      > /dev/null 2>&1 ) & pf_pid=$! pf_port=$3
  for _ in $(seq 1 60); do
    if [[ "$(listener_pid "$3")" == "$pf_pid" ]] \
       && "$CURL" -s -o /dev/null --max-time 5 -w '%{http_code}' "http://127.0.0.1:$3/health/live" | grep -qE '^[1-5][0-9][0-9]$'; then
      return 0   # the listener is this kubectl and the pod answered HTTP (warm-up)
    fi
    sleep 0.5
  done
  forward_reason="port-forward to $1/$2 did not open (or the listener is not this kubectl)"; return 1
}

# probe <ns> <server> <pod> <check> <expect> <tool> <args> <tested-env-var> [<control-env-var>]
probe() {
  local ns=$1 server=$2 pod=$3 check=$4 expect=$5 tool=$6 args=$7 tested=$8 control=${9:-} rc=0
  local file="$dir/$ns--$pod--$check.json" cargs=()
  if [[ -n "$control" ]]; then cargs=(--control-token-env AGI_CONTROL_TOKEN); fi
  if [[ "$(listener_pid "$pf_port")" != "$pf_pid" ]]; then   # the forward died or was replaced: no evidence
    emit type=fatal "reason=$pod: forward lost before the $check probe"; return 0
  fi
  EWALLET_MCP_SERVICE_TOKEN=${!tested} AGI_CONTROL_TOKEN=${control:+${!control}} "${PROBE[@]}" mcp \
    --endpoint "http://127.0.0.1:$pf_port/mcp" --tool "$tool" --arguments-json "$args" --expect "$expect" \
    "${cargs[@]}" --evidence "$file" > /dev/null 2>&1 || rc=$?
  emit type=probe "namespace=$ns" "server=$server" "pod=$pod" "check=$check" "expect=$expect" "evidence=$file" "probe_rc=$rc"
}

inventory() {   # inventory <ns> <server> <tool>: prints the Ready pod names; records spec/ready/pods
  local ns=$1 server=$2 tool=$3 deploy pods
  if ! deploy=$(k -n "$ns" get deploy "$server" -o json 2>/dev/null); then
    emit type=replicas "namespace=$ns" "server=$server" "tool=$tool" spec=null ready=null 'pods=[]'; return 0
  fi
  pods=$(k -n "$ns" get pods -l "app.kubernetes.io/name=$server" -o json | python3 -c 'import json, sys
items = json.load(sys.stdin)["items"]
ready = [p["metadata"]["name"] for p in items if not p["metadata"].get("deletionTimestamp")
         and any(c.get("type") == "Ready" and c.get("status") == "True" for c in p.get("status", {}).get("conditions", []))]
print(json.dumps(sorted(ready)))') || { emit type=fatal "reason=cannot list $ns/$server pods"; return 0; }
  local spec ready
  read -r spec ready < <(python3 -c 'import json, sys; d = json.load(sys.stdin)
print(d["spec"].get("replicas", 1), d.get("status", {}).get("readyReplicas", 0))' <<< "$deploy")
  emit type=replicas "namespace=$ns" "server=$server" "tool=$tool" "spec=$spec" "ready=$ready" "pods=$pods"
  python3 -c 'import json, sys; print("\n".join(json.loads(sys.argv[1])))' "$pods"
}

absent() {   # absent <ns> <name>: every exclusively owned kind of the retired server must be gone
  local ns=$1 name=$2 kind names
  for kind in deployment service poddisruptionbudget serviceaccount; do
    if names=$(k -n "$ns" get "$kind" "$name" --ignore-not-found -o name); then
      emit type=absent "namespace=$ns" "server=$name" "kind=$kind" "present=$([[ -z "$names" ]] && echo false || echo true)" "names=$names"
    else
      emit type=fatal "reason=cannot read $kind $ns/$name (an API error is not absence)"
    fi
  done
  if names=$(k -n "$ns" get pods -l "app.kubernetes.io/name=$name" -o name); then
    emit type=absent "namespace=$ns" "server=$name" kind=pods "present=$([[ -z "$names" ]] && echo false || echo true)" "names=${names//$'\n'/,}"
  else
    emit type=fatal "reason=cannot list $ns/$name pods"
  fi
}

load_token() {   # load_token <var> <ns> <secret>: fatal record (not a crash) when it cannot be read
  local value
  if value=$(secret "$2" "$3") && [[ -n "$value" ]]; then printf -v "$1" '%s' "$value"; export "${1?}"; return 0; fi
  emit type=fatal "reason=cannot read the credential Secret $2/$3"; return 1
}

INVALID_TOKEN=$(python3 -c 'import secrets; print("invalid-" + secrets.token_urlsafe(32))'); export INVALID_TOKEN
port=18190
next_port() { port=$((port + 1)); }

check_active() {   # check_active <server> <tool> <args> <sandbox-secret>
  local server=$1 tool=$2 args=$3 sandbox=$4 controller pod
  load_token ACTIVE_TOKEN ewallet-sandbox "$sandbox" || ACTIVE_TOKEN=""
  controller=$(k -n ewallet-agents get remotemcpservers.kagent.dev "$server" -o jsonpath='{.spec.headersFrom[0].valueFrom.name}' 2>/dev/null || true)
  [[ "$controller" =~ ^mcp-token-kagent-controller-$server-[a-z0-9-]+$ ]] || { emit type=fatal "reason=cannot read the kagent controller credential of $server"; controller=""; }
  CONTROLLER_TOKEN=""
  if [[ -n "$controller" ]]; then load_token CONTROLLER_TOKEN ewallet-agents "$controller" || CONTROLLER_TOKEN=""; fi
  while read -r pod; do
    [[ -n "$pod" ]] || continue
    next_port
    if ! forward ewallet-mcp "$pod" "$port"; then emit type=forward namespace=ewallet-mcp "server=$server" "pod=$pod" ok=false "reason=$forward_reason"; continue; fi
    emit type=forward namespace=ewallet-mcp "server=$server" "pod=$pod" ok=true "port=$port"
    if [[ -n "$ACTIVE_TOKEN" ]]; then   # without the active credential no check can run (missing -> FAIL)
      probe ewallet-mcp "$server" "$pod" active ok "$tool" "$args" ACTIVE_TOKEN
      if [[ -n "$CONTROLLER_TOKEN" ]]; then
        probe ewallet-mcp "$server" "$pod" discovery call-denied "$tool" "$args" CONTROLLER_TOKEN ACTIVE_TOKEN
      fi
      probe ewallet-mcp "$server" "$pod" invalid revoked "$tool" "$args" INVALID_TOKEN ACTIVE_TOKEN
    fi
  done < <(inventory ewallet-mcp "$server" "$tool")
  cleanup
}

check_legacy_present() {   # the in-release ewallet-agents/drift-mcp before retirement: credential layers only
  local pod
  load_token ACTIVE_TOKEN ewallet-sandbox "$SANDBOX_DRIFT_SECRET" || ACTIVE_TOKEN=""
  while read -r pod; do
    [[ -n "$pod" ]] || continue
    next_port
    if ! forward ewallet-agents "$pod" "$port"; then emit type=forward namespace=ewallet-agents server=drift-mcp "pod=$pod" ok=false "reason=$forward_reason"; continue; fi
    emit type=forward namespace=ewallet-agents server=drift-mcp "pod=$pod" ok=true "port=$port"
    if [[ -n "$ACTIVE_TOKEN" ]]; then
      probe ewallet-agents drift-mcp "$pod" active ok drift_detection "$DRIFT_ARGS" ACTIVE_TOKEN
      probe ewallet-agents drift-mcp "$pod" invalid revoked drift_detection "$DRIFT_ARGS" INVALID_TOKEN ACTIVE_TOKEN
    fi
  done < <(inventory ewallet-agents drift-mcp drift_detection)
  cleanup
}

check_active feature-rag-mcp feature_lookup "$RAG_ARGS" "$SANDBOX_RAG_SECRET"
check_active drift-mcp drift_detection "$DRIFT_ARGS" "$SANDBOX_DRIFT_SECRET"
if [[ "$LEGACY_SERVER" == absent ]]; then absent ewallet-agents drift-mcp; legacy_mode=absent
else check_legacy_present; legacy_mode=legacy; fi

rc=0
(cd "$ROOT" && "${VERDICT_PY[@]}" -m ops.llm_agent.mcp_credential_verdict --records "$records" --out "$dir/verdict.json" \
   --min-replicas "$MCP_MIN_REPLICAS" --server ewallet-mcp/feature-rag-mcp=active --server ewallet-mcp/drift-mcp=active \
   --server "ewallet-agents/drift-mcp=$legacy_mode") > "$dir/summary.tsv" || rc=$?
cat "$dir/summary.tsv"
[[ -s "$dir/verdict.json" ]] || rc=1
echo "verdict: $dir/verdict.json (exit $rc)"
exit "$rc"
