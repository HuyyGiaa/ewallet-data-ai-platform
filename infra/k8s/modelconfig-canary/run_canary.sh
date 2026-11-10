#!/usr/bin/env bash
# Isolated canary for the BYO ModelConfig resolver in namespace kagent-pilot (watched by
# kagent, so the copy of ewallet-global-llm gets a real Accepted status). Nothing in
# ewallet-agents is touched. Every object carries ewallet.io/canary=modelconfig-resolver.
#
#   run_canary.sh up <out-dir>        render, apply, wait, record the baseline resolution
#   run_canary.sh cases <out-dir>     ModelConfig changes, each followed by a BYO restart
#   run_canary.sh down                delete every canary object
#
# Needs: kubectl and helm on the GKE context, python3 with PyYAML, the production values
# of release llm-agent (helm get values) and the API server CIDRs.
set -euo pipefail

NS=kagent-pilot
SEL=ewallet.io/canary=modelconfig-resolver
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
HERE=$ROOT/infra/k8s/modelconfig-canary
MC=ewallet-global-llm

wait_ready() {   # <agent>
  local i
  for i in $(seq 1 60); do
    if [ "$(kubectl -n $NS get pods -l "app.kubernetes.io/name=$1" -o jsonpath='{.items[0].status.containerStatuses[0].ready}' 2>/dev/null)" = true ]; then
      return 0
    fi
    sleep 5
  done
  return 1
}

wait_accepted() {   # ModelConfig reconciled at its current generation
  local i gen obs acc
  for i in $(seq 1 30); do
    gen=$(kubectl -n $NS get modelconfig $MC -o jsonpath='{.metadata.generation}')
    obs=$(kubectl -n $NS get modelconfig $MC -o jsonpath='{.status.observedGeneration}')
    acc=$(kubectl -n $NS get modelconfig $MC -o jsonpath='{.status.conditions[?(@.type=="Accepted")].status}')
    [ "$gen" = "$obs" ] && [ "$acc" = True ] && return 0
    sleep 2
  done
  return 1
}

failed_logs() {   # <pod>: the crashed container's log; retried, the runtime may be restarting it
  local i log
  for i in $(seq 1 12); do
    log=$(kubectl -n $NS logs "$1" --previous 2>/dev/null || kubectl -n $NS logs "$1" 2>/dev/null || true)
    if grep -q modelconfig.failed <<<"$log"; then echo "$log"; return 0; fi
    sleep 5
  done
  echo "no modelconfig.failed line captured"
}

restart_and_capture() {   # <agent> <out-file> <expect: ready|fail>
  local agent=$1 out=$2 expect=$3 old pod i
  old=$(kubectl -n $NS get pods -l "app.kubernetes.io/name=$agent" -o jsonpath='{.items[0].metadata.name}')
  kubectl -n $NS delete pod "$old" --wait=true >/dev/null
  for i in $(seq 1 60); do
    pod=$(kubectl -n $NS get pods -l "app.kubernetes.io/name=$agent" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [ -n "$pod" ] && [ "$pod" != "$old" ]; then
      if [ "$expect" = ready ] && wait_ready "$agent"; then break; fi
      if [ "$expect" = fail ] && [ "$(kubectl -n $NS get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')" -ge 1 ]; then break; fi
    fi
    sleep 5
  done
  {
    echo "# pod $pod (expect $expect) $(date -u +%FT%TZ)"
    kubectl -n $NS get pod "$pod" -o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount,STATE:.status.containerStatuses[0].lastState.terminated.reason
    if [ "$expect" = fail ]; then failed_logs "$pod"; else kubectl -n $NS logs "$pod"; fi
  } > "$out" 2>&1
  cat "$out"
}

cmd_up() {
  local out=$1; mkdir -p "$out"
  : "${LLM_AGENT_VALUES:?path to helm get values llm-agent -o yaml}"
  : "${API_SERVER_CIDRS:?e.g. 34.118.224.1/32,10.20.0.8/32}"
  local tmp; tmp=$(mktemp -d)
  printf 'modelConfig:\n  byoResolver:\n    enabled: true\n    apiServer:\n      cidrs: [%s]\n' "$API_SERVER_CIDRS" > "$tmp/on.yaml"
  helm template llm-agent "$ROOT/infra/helm/llm-agent-platform" -n default -f "$LLM_AGENT_VALUES" -f "$tmp/on.yaml" \
    --set namespace.name=$NS > "$tmp/render.yaml"
  local denied; denied="$(kubectl -n $NS get svc mock-openai -o jsonpath='{.spec.clusterIP}'):8080"
  kubectl -n ewallet-agents get modelconfig $MC -o json > "$out/00-production-modelconfig.json"
  python3 "$HERE/render_canary.py" "$tmp/render.yaml" "$out/canary-manifest.yaml" --denied-egress "$denied" \
    --live-modelconfig "$out/00-production-modelconfig.json"
  # Placeholder client keys: random, never the gateway key; the canary makes no LLM call.
  for s in agentgateway-client-key mc-canary-other-key; do
    kubectl -n $NS create secret generic $s --from-literal "api-key=$(openssl rand -hex 24)" \
      --dry-run=client -o yaml | kubectl label --local -f - $SEL -o yaml | kubectl apply -f - >/dev/null
  done
  kubectl -n $NS create configmap mc-canary-code \
    --from-file=model_config.py="$ROOT/ai_platform/llm_agent/model_config.py" \
    --from-file=canary_resolver.py="$HERE/canary_resolver.py" --dry-run=client -o yaml \
    | kubectl label --local -f - $SEL -o yaml | kubectl apply -f - >/dev/null
  kubectl apply -f "$out/canary-manifest.yaml"
  wait_accepted
  kubectl -n $NS get modelconfig $MC -o yaml > "$out/01-modelconfig.yaml"
  wait_ready mc-canary-coordinator; wait_ready mc-canary-feature-rag
  sleep 20   # let the unbound agent crash at least once
  for a in mc-canary-coordinator mc-canary-feature-rag; do
    kubectl -n $NS logs deploy/$a > "$out/02-resolved-$a.log" 2>&1
  done
  failed_logs "$(kubectl -n $NS get pods -l app.kubernetes.io/name=mc-canary-unbound -o jsonpath='{.items[0].metadata.name}')" \
    > "$out/03-unbound-forbidden.log"
  kubectl -n $NS get agents.kagent.dev,deploy,pods -l $SEL -o wide > "$out/04-canary-objects.txt" 2>&1 || true
  kubectl -n $NS get pods -l $SEL -o wide >> "$out/04-canary-objects.txt"
  kubectl -n $NS get deploy mc-canary-coordinator -o yaml > "$out/05-kagent-rendered-deployment.yaml"
  {
    for sa in fraud-coordinator feature-rag-specialist mc-canary-unbound; do
      as="--as=system:serviceaccount:$NS:$sa"
      for check in "get modelconfigs.kagent.dev/$MC" "get modelconfigs.kagent.dev/pilot-mock-openai" \
                   "list modelconfigs.kagent.dev" "watch modelconfigs.kagent.dev" "update modelconfigs.kagent.dev/$MC" \
                   "get secrets/agentgateway-client-key" "list secrets" "get agents.kagent.dev/mc-canary-coordinator"; do
        printf '%-24s %-48s %s\n' "$sa" "$check" "$(kubectl auth can-i $check -n $NS $as 2>/dev/null || true)"
      done
    done
  } > "$out/06-rbac-can-i.txt"
  cat "$out/06-rbac-can-i.txt"
  rm -rf "$tmp"
}

cmd_cases() {
  local out=$1; mkdir -p "$out"
  local agent=mc-canary-coordinator
  # A. Model change: the running pod keeps the startup value until it restarts.
  kubectl -n $NS patch modelconfig $MC --type merge -p '{"spec":{"model":"ewallet-fraud-llm-canary-v2"}}'
  wait_accepted
  kubectl -n $NS exec deploy/$agent -- python -c \
    "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/resolved').read().decode())" \
    > "$out/10-before-restart.json"
  restart_and_capture $agent "$out/11-model-change-after-restart.log" ready
  # B. Unauthorized endpoint (the CPU vLLM Service, bypassing agentgateway): fail closed.
  kubectl -n $NS patch modelconfig $MC --type merge \
    -p '{"spec":{"openAI":{"baseUrl":"http://vllm-primary.ewallet-agents.svc.cluster.local:8000/v1"}}}'
  wait_accepted
  restart_and_capture $agent "$out/12-unauthorized-endpoint.log" fail
  # C. Secret binding moved to another Secret: the pod's key is stale, fail closed.
  kubectl -n $NS patch modelconfig $MC --type merge \
    -p '{"spec":{"apiKeySecret":"mc-canary-other-key","openAI":{"baseUrl":"http://agentgateway.ewallet-agents.svc.cluster.local:3000/v1"}}}'
  wait_accepted
  restart_and_capture $agent "$out/13-secret-mismatch.log" fail
  # D. ModelConfig deleted: fail closed.
  kubectl -n $NS get modelconfig $MC -o yaml > "$out/14-modelconfig-before-delete.yaml"
  kubectl -n $NS delete modelconfig $MC
  restart_and_capture $agent "$out/15-modelconfig-missing.log" fail
  # E. Restore the original spec: resolves again after restart.
  kubectl -n $NS apply -f <(python3 - "$out/../canary-manifest.yaml" <<'EOF'
import sys, yaml
print(yaml.safe_dump(next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d["kind"] == "ModelConfig")))
EOF
)
  wait_accepted
  restart_and_capture $agent "$out/16-restored.log" ready
}

cmd_down() {
  kubectl -n $NS delete agents.kagent.dev -l $SEL --wait=true
  kubectl -n $NS delete modelconfig,networkpolicy,rolebinding,role,configmap,secret,serviceaccount -l $SEL
  kubectl -n $NS get all,modelconfig,networkpolicy,role,rolebinding,configmap,secret,serviceaccount -l $SEL 2>&1
}

case "${1:-}" in
  up) cmd_up "${2:?out-dir}" ;;
  cases) cmd_cases "${2:?out-dir}" ;;
  down) cmd_down ;;
  *) echo "usage: $0 up|cases <out-dir> | down" >&2; exit 2 ;;
esac
