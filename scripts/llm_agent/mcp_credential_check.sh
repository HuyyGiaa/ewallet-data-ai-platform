#!/usr/bin/env bash
# [READ-ONLY] MCP credential state on EVERY MCP server replica (legacy Specialist credential retirement).
#
# Each replica is reached through its own pod port-forward (not the Service, so no replica is skipped).
# Per replica, against its own server:
#   active    sandbox-<agent> (the live SandboxAgent credential) calls a granted tool      -> ok
#   discovery kagent-controller (list-only) sees the tool but cannot call it              -> call-denied
#   retired   each of the nine legacy Specialist credentials                              -> revoked (HTTP 401,
#             with the active credential listing the tool on the same replica as the control)
# The in-release ewallet-agents/drift-mcp has no non-retired subject, so its retired checks run without a
# control and are reported as observations (outcome only).
#
# usage: mcp_credential_check.sh <out-dir> <label>
# Env: AGI_ENV (campaign file: CTX, EV with inputs/transaction.json, BASELINE), KUBECONFIG, UV.
# Tokens are read into this process and reach agi_probe through its environment only; nothing prints them.
set -euo pipefail
out=${1:?out-dir} label=${2:?label}
die() { echo "mcp_credential_check: FAIL: $*" >&2; exit 1; }
[[ -f "${AGI_ENV:-}" ]] || die "AGI_ENV must name the campaign env file"
# shellcheck disable=SC1090
source "$AGI_ENV"
: "${CTX:?}" "${EV:?}" "${BASELINE:?}"
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
UV=${UV:-$(command -v uv || echo "$HOME/miniconda3/envs/fintech/bin/uv")}
PY=("$UV" run --frozen --project "$ROOT/infra/docker/g3-byo-coordinator" python "$ROOT/scripts/llm_agent/agi_probe.py")
k() { kubectl --context "$CTX" --request-timeout=60s "$@"; }
secret() { k -n "$1" get secret "$2" -o jsonpath='{.data.token}' | base64 -d; }
umask 077; mkdir -p "$out/$label"
summary="$out/$label/summary.tsv"; : > "$summary"

RETIRED_DRIFT=(mcp-token-drift-specialist-drift-mcp mcp-token-drift-specialist-drift-mcp-r1-release1
               mcp-token-drift-specialist-drift-mcp-sbx-20261115)
RETIRED_RAG=(mcp-token-drift-specialist-feature-rag-mcp mcp-token-drift-specialist-feature-rag-mcp-r1-release1
             mcp-token-drift-specialist-feature-rag-mcp-sbx-20261115
             mcp-token-feature-rag-specialist-feature-rag-mcp mcp-token-feature-rag-specialist-feature-rag-mcp-r1-release1
             mcp-token-feature-rag-specialist-feature-rag-mcp-sbx-20261115)
SANDBOX_DRIFT=mcp-token-sandbox-drift-drift-mcp-sbx-20261115
SANDBOX_RAG=mcp-token-sandbox-feature-rag-feature-rag-mcp-sbx-20261115
CONTROLLER_RELEASE=$(k -n ewallet-agents get remotemcpservers.kagent.dev drift-mcp -o jsonpath='{.spec.headersFrom[0].valueFrom.name}' \
  | sed -E 's/^mcp-token-kagent-controller-drift-mcp-//')
[[ "$CONTROLLER_RELEASE" =~ ^[a-z0-9-]+$ ]] || die "cannot read the kagent controller credential release"

window='{"start":"2026-05-01T00:00:00Z","end":"2026-05-31T00:00:00Z"}'
DRIFT_ARGS="{\"feature_name\":\"account_behavior__account_tx_count_1h\",\"baseline_id\":\"${BASELINE}\",\"current_window\":${window}}"
RAG_ARGS=$(python3 -c 'import json,sys;print(json.dumps({"transaction":json.load(open(sys.argv[1])),"feature_names":["account_behavior__account_tx_count_1h"]}))' "$EV/inputs/transaction.json")

# probe <pod> <port> <case> <expect> <tool> <args> <tested-secret-ns> <tested-secret> [<control-ns> <control-secret>]
probe() {
  local pod=$1 port=$2 case=$3 expect=$4 tool=$5 args=$6 tns=$7 tname=$8 cns=${9:-} cname=${10:-}
  local file="$out/$label/$pod--$case--$tname.json" tested control="" rc
  tested=$(secret "$tns" "$tname") || die "cannot read $tns/$tname"
  [[ -n "$cname" ]] && { control=$(secret "$cns" "$cname") || die "cannot read $cns/$cname"; }
  local cargs=(); [[ -n "$control" ]] && cargs=(--control-token-env AGI_CONTROL_TOKEN)
  set +e
  EWALLET_MCP_SERVICE_TOKEN=$tested AGI_CONTROL_TOKEN=$control "${PY[@]}" mcp --endpoint "http://127.0.0.1:$port/mcp" \
    --tool "$tool" --arguments-json "$args" --expect "$expect" "${cargs[@]}" --evidence "$file" > /dev/null 2>&1
  rc=$?; set -e
  local verdict outcome
  read -r verdict outcome < <(python3 -c 'import json,sys
try: d=json.load(open(sys.argv[1])); print(d.get("verdict","?"), d.get("outcome","?"))
except Exception: print("NO_EVIDENCE", "?")' "$file")
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$pod" "$case" "$tname" "$expect" "$verdict" "$outcome" | tee -a "$summary"
  return 0
}

pf_pid=""
cleanup() {   # kubectl itself is the background process (exec), so this really closes the forward
  if [[ -n "$pf_pid" ]]; then kill "$pf_pid" 2>/dev/null || true; wait "$pf_pid" 2>/dev/null || true; fi
  pf_pid=""
}
trap cleanup EXIT
listener_pid() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2; }
forward() {   # forward <ns> <pod> <port>: exactly this pod on a port nobody else holds
  cleanup
  [[ -z "$(listener_pid "$3")" ]] || die "127.0.0.1:$3 is already in use; refusing to probe through a stale forward"
  ( exec kubectl --context "$CTX" --request-timeout=60s -n "$1" port-forward --address 127.0.0.1 "pod/$2" "$3:8080" \
      > /dev/null 2>&1 ) & pf_pid=$!
  for _ in $(seq 1 60); do
    if [[ "$(listener_pid "$3")" == "$pf_pid" ]] \
       && curl -s -o /dev/null --max-time 5 -w '%{http_code}' "http://127.0.0.1:$3/health/live" | grep -qE '^[1-5][0-9][0-9]$'; then
      return 0   # the listener is this kubectl and the pod answered HTTP (warm-up)
    fi
    sleep 0.5
  done
  die "port-forward to $1/$2 did not open"
}

port=18190
for pod in $(k -n ewallet-mcp get pods -l app.kubernetes.io/name=feature-rag-mcp -o name | sed 's|pod/||'); do
  port=$((port + 1)); forward ewallet-mcp "$pod" "$port"
  probe "$pod" "$port" active ok feature_lookup "$RAG_ARGS" ewallet-sandbox "$SANDBOX_RAG"
  probe "$pod" "$port" discovery call-denied feature_lookup "$RAG_ARGS" ewallet-agents \
    "mcp-token-kagent-controller-feature-rag-mcp-$CONTROLLER_RELEASE" ewallet-sandbox "$SANDBOX_RAG"
  for name in "${RETIRED_RAG[@]}"; do
    probe "$pod" "$port" retired revoked feature_lookup "$RAG_ARGS" ewallet-agents "$name" ewallet-sandbox "$SANDBOX_RAG"
  done
done
for pod in $(k -n ewallet-mcp get pods -l app.kubernetes.io/name=drift-mcp -o name | sed 's|pod/||'); do
  port=$((port + 1)); forward ewallet-mcp "$pod" "$port"
  probe "$pod" "$port" active ok drift_detection "$DRIFT_ARGS" ewallet-sandbox "$SANDBOX_DRIFT"
  probe "$pod" "$port" discovery call-denied drift_detection "$DRIFT_ARGS" ewallet-agents \
    "mcp-token-kagent-controller-drift-mcp-$CONTROLLER_RELEASE" ewallet-sandbox "$SANDBOX_DRIFT"
  for name in "${RETIRED_DRIFT[@]}"; do
    probe "$pod" "$port" retired revoked drift_detection "$DRIFT_ARGS" ewallet-agents "$name" ewallet-sandbox "$SANDBOX_DRIFT"
  done
done
for pod in $(k -n ewallet-agents get pods -l app.kubernetes.io/name=drift-mcp -o name | sed 's|pod/||'); do
  port=$((port + 1)); forward ewallet-agents "$pod" "$port"
  for name in "${RETIRED_DRIFT[@]}"; do   # no non-retired subject exists on this server: observation only
    probe "$pod" "$port" retired-observed revoked drift_detection "$DRIFT_ARGS" ewallet-agents "$name"
  done
done
cleanup
echo "summary: $summary"
