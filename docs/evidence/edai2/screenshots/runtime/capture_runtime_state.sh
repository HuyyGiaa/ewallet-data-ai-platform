#!/usr/bin/env bash
# N13-DOC: read-only runtime state capture (no mutation). Each section records the exact
# command and output; render_terminal.py turns them into labelled PNGs.
# The only requests sent: KServe V2 metadata/ready GETs and 2 Fraud API calls
# (1 known-good prediction, 1 invalid payload -> 422), plus health GETs.
set -u
OUT=$(cd "$(dirname "$0")" && pwd)/raw; mkdir -p "$OUT"
run(){ name=$1; shift; { echo "\$ $*"; eval "$@" 2>&1; } > "$OUT/$name.txt"; }
CTX=$(kubectl config current-context); HREV=$(helm -n default history llm-agent --max 1 | tail -1 | awk '{print $1}')
echo "context=$CTX helm_llm_agent_revision=$HREV captured_utc=$(date -u +%FT%TZ)" > "$OUT/_meta.txt"
run 01_agents_replicas_hpa_pdb "kubectl -n ewallet-agents get deploy fraud-coordinator feature-rag-specialist drift-specialist -o wide | cut -c1-150; echo; kubectl -n ewallet-agents get hpa; echo; kubectl -n ewallet-agents get pdb; echo; kubectl -n ewallet-agents get pods -l 'kagent in (fraud-coordinator,feature-rag-specialist,drift-specialist)' -o wide | cut -c1-140"
run 02_kagent_agent_crs "kubectl -n ewallet-agents get agents.kagent.dev -o custom-columns=NAME:.metadata.name,TYPE:.spec.type,READY:.status.conditions[-1].status; echo; kubectl -n ewallet-agents get agents.kagent.dev fraud-coordinator -o jsonpath='{.spec.byo.deployment.image}{\"\\n\"}'"
run 03_sandbox_substrate "kubectl get crd | grep -E 'sandboxagents|workerpools|actortemplates|sandboxconfigs'; echo; kubectl -n kagent-pilot get sandboxagents.kagent.dev,workerpools.ate.dev 2>&1; echo; kubectl get sandboxconfigs.ate.dev -A 2>&1"
run 04_mcp_rollingupdate "kubectl -n ewallet-mcp get deploy -o custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,STRATEGY:.spec.strategy.type,MAX_UNAVAILABLE:.spec.strategy.rollingUpdate.maxUnavailable,MAX_SURGE:.spec.strategy.rollingUpdate.maxSurge,IMAGE:.spec.template.spec.containers[0].image | cut -c1-170; echo; helm list -A | grep -E 'NAME|llm-agent'"
run 05_kserve_isvc "kubectl -n fraud-serving get inferenceservice fraud-detector; echo; kubectl -n fraud-serving get pods -l serving.kserve.io/inferenceservice=fraud-detector; echo; kubectl -n fraud-serving get inferenceservice fraud-detector -o jsonpath='{.spec.predictor.containers[0].image}{\"\\n\"}'"
(kubectl -n fraud-serving port-forward svc/fraud-detector-predictor 18181:80 >/dev/null 2>&1 &); (kubectl -n ml-platform port-forward svc/fraud-api 18100:80 >/dev/null 2>&1 &); sleep 5
run 06_kserve_v2_ready_metadata "curl -s -o /dev/null -w 'GET /v2/models/fraud-detector/ready -> HTTP %{http_code}\n' http://127.0.0.1:18181/v2/models/fraud-detector/ready; curl -s http://127.0.0.1:18181/v2/models/fraud-detector | python3 -c 'import json,sys;d=json.load(sys.stdin);print(\"name:\",d[\"name\"],\"| versions:\",d.get(\"versions\"),\"| platform:\",d.get(\"platform\"));print(\"inputs:\",len(d[\"inputs\"]),\"features e.g.\",[i[\"name\"] for i in d[\"inputs\"][:4]]);print(\"outputs:\",[o[\"name\"] for o in d[\"outputs\"]])'"
CID="n13-doc-$(date -u +%Y%m%dT%H%M%SZ)"
run 07_fraud_api_predict_via_kserve "python3 - '$CID' <<'PY'
import json,sys,urllib.request
case=json.load(open('docs/evidence/cloud/mlops-campaign-20261031/fraud_api_e2e_smoke.json'))[0]
r=urllib.request.Request('http://127.0.0.1:18100/v1/fraud/predict',data=json.dumps(case['request']).encode(),headers={'content-type':'application/json','x-correlation-id':sys.argv[1]},method='POST')
resp=urllib.request.urlopen(r,timeout=30); body=json.load(resp)
print('POST /v1/fraud/predict -> HTTP',resp.status); print(json.dumps(body,indent=1))
print('expected fraud_probability (recorded 2026-10-31):',case['response']['fraud_probability'],'| equal:',body['fraud_probability']==case['response']['fraud_probability'])
PY
sleep 3; echo; echo '\$ kubectl -n fraud-serving logs deploy/fraud-detector-predictor --since=2m | grep $CID'; kubectl -n fraud-serving logs deploy/fraud-detector-predictor --since=2m | grep '$CID' | cut -c1-200 | head -3"
run 08_fraud_api_pydantic_and_health "for p in /health/live /health/ready; do curl -s -w '  <- GET %{url_effective} HTTP %{http_code}\n' http://127.0.0.1:18100\$p; done; echo; curl -s -X POST http://127.0.0.1:18100/v1/fraud/predict -H 'content-type: application/json' -d '{\"transaction_id\":\"x\",\"amount\":-5,\"currency\":\"USD\"}' -w '\nPOST invalid payload -> HTTP %{http_code}\n' | python3 -c 'import sys;t=sys.stdin.read();print(t[:900])'"
for p in $(pgrep -x kubectl); do tr '\0' ' ' </proc/$p/cmdline | grep -q -E '18181:80|18100:80' && kill $p; done
echo done
