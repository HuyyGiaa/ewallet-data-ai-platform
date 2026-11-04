#!/usr/bin/env bash
# usage: m2_matrix.sh <phase> <outdir> <repo-root>   (kind context only)
set -uo pipefail
phase=$1; out=$2; root=$3; C=kind-llm-agent-local; NS=ewallet-prompts; PY=$root/infra/docker/llm-agent-tooling/.venv/bin/python
k(){ kubectl --context $C "$@"; }
REGPOD=$(k -n $NS get pod -l app.kubernetes.io/name=agentregistry -o jsonpath='{.items[0].metadata.name}')
DBPOD=$(k -n $NS get pod -l app.kubernetes.io/name=prompt-registry-db -o jsonpath='{.items[0].metadata.name}')
REGIP=$(k -n $NS get pod $REGPOD -o jsonpath='{.status.podIP}'); DBIP=$(k -n $NS get pod $DBPOD -o jsonpath='{.status.podIP}')
declare -A SRC=([open]="m2-probe-src m2-probe-open" [cicd]="ewallet-cicd m2-probe-cicd" [lateral]="$NS m2-probe-lateral" [db]="$NS $DBPOD" [registry]="$NS $REGPOD")
TGT=("reg-svc prompt-registry-agentregistry.$NS.svc.cluster.local 12121" "reg-pod $REGIP 8080" "mcp-svc prompt-registry-agentregistry.$NS.svc.cluster.local 31313" "db-svc prompt-registry-db.$NS.svc.cluster.local 5432" "db-pod $DBIP 5432" "kubeapi kubernetes.default.svc.cluster.local 443" "external 1.1.1.1 443")
{
echo "## phase=$phase $(date -u +%FT%TZ) policies: $(k -n $NS get netpol -o name | tr '\n' ' ')"
printf "%-9s %-9s %s\n" source target result
for s in open cicd lateral db registry; do
  set -- ${SRC[$s]}; ns=$1; pod=$2
  if k -n $ns exec $pod -- bash -c 'timeout 4 getent hosts kubernetes.default.svc.cluster.local >/dev/null' 2>/dev/null; then r=RESOLVES; else r=NO-DNS; fi
  printf "%-9s %-9s %s\n" $s dns $r
  for t in "${TGT[@]}"; do
    set -- $t; name=$1; host=$2; port=$3
    [ "$s" = registry ] && [ "${name%%-*}" = reg ] && continue
    [ "$s" = db ] && [ "${name%%-*}" = db ] && continue
    if [ "$name" != external ] && [ "$name" != reg-pod ] && [ "$name" != db-pod ] && [ $r = NO-DNS ]; then
      # resolve on the host side so the TCP edge is still measured
      host=$(k -n default get svc kubernetes >/dev/null 2>&1; case $name in reg-svc|mcp-svc) k -n $NS get svc prompt-registry-agentregistry -o jsonpath='{.spec.clusterIP}';; db-svc) k -n $NS get svc prompt-registry-db -o jsonpath='{.spec.clusterIP}';; kubeapi) k -n default get svc kubernetes -o jsonpath='{.spec.clusterIP}';; esac)
    fi
    if k -n $ns exec $pod -- bash -c "timeout 4 bash -c '</dev/tcp/$host/$port'" 2>/dev/null; then res=REACHABLE; else res=BLOCKED; fi
    printf "%-9s %-9s %s\n" $s $name $res
  done
done
echo "## functional (operator via port-forward, kind registry only)"
for p in $(pgrep kubectl); do tr "\0" " " </proc/$p/cmdline 2>/dev/null | grep -q "port-forward.*42121" && kill $p; done
(k -n $NS port-forward svc/prompt-registry-agentregistry 42121:12121 43131:31313 >/dev/null 2>&1 &); sleep 4
echo "FN1 REST health http=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:42121/v0/health)"
cd $root
echo "FN2 operator publish: $($PY -m ops.llm_agent.registry_catalog publish --all-prompt-versions --registry http://127.0.0.1:42121 --confirm-live-write http://127.0.0.1:42121 2>&1 | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["verdict"],len(d.get("readback",[])))' 2>&1)"
echo "FN2 gate post: $($PY -m ops.llm_agent.prompt_registry gate --phase post --registry http://127.0.0.1:42121 | grep -o '"verdict": "[A-Z]*"')"
echo "FN3 MCP: $(curl -s --max-time 8 -X POST http://127.0.0.1:43131/mcp -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"m2","version":"1"}}}' | grep -o '"serverInfo":{[^}]*}' | head -1)"
echo "ready: registry=$(k -n $NS get pod $REGPOD -o jsonpath='{.status.containerStatuses[0].ready}/restarts={.status.containerStatuses[0].restartCount}') db=$(k -n $NS get pod $DBPOD -o jsonpath='{.status.containerStatuses[0].ready}')"
for p in $(pgrep kubectl); do tr "\0" " " </proc/$p/cmdline 2>/dev/null | grep -q "port-forward.*42121" && kill $p; done
} 2>&1 | tee $out/matrix-$phase.txt
