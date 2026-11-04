#!/usr/bin/env bash
set -euo pipefail
root=$1; E=$2; PY=$3
source "$root/infra/helm/agentregistry/versions.env"
tag="ewallet-probe-guard-$$"; port=12131; R="http://127.0.0.1:$port"
password=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')
cleanup() { docker rm -f "$tag-server" "$tag-db" >/dev/null 2>&1 || true; docker network rm "$tag" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker network create "$tag" >/dev/null
docker run -d --name "$tag-db" --network "$tag" --network-alias agentregistry-postgresql --memory 256m --cpus 0.5 -e POSTGRES_DB=agentregistry -e POSTGRES_USER=agentregistry -e POSTGRES_PASSWORD="$password" postgres:18 >/dev/null
for _ in $(seq 1 60); do docker exec "$tag-db" pg_isready -U agentregistry -d agentregistry >/dev/null 2>&1 && break; sleep 1; done
docker run -d --name "$tag-server" --network "$tag" --memory 384m --cpus 0.5 -p "127.0.0.1:$port:8080" -e AGENT_REGISTRY_SERVER_ADDRESS=:8080 -e AGENT_REGISTRY_MCP_PORT=31313 -e AGENT_REGISTRY_AGENT_GATEWAY_PORT=21212 -e AGENT_REGISTRY_ENABLE_REGISTRY_VALIDATION=false -e AGENT_REGISTRY_DATABASE_URL="postgres://agentregistry:$password@agentregistry-postgresql:5432/agentregistry?sslmode=disable" "$AGENTREGISTRY_SERVER_IMAGE" >/dev/null
for _ in $(seq 1 90); do curl -fsS "$R/v0/health" >/dev/null 2>&1 && break; sleep 1; done
cd "$root"
for _ in $(seq 1 60); do [ "$(curl -s -o /dev/null -w "%{http_code}" $R/v0/prompts/drift-specialist-system/v2)" = 404 ] && break; sleep 1; done
echo "## 1 gate pre on empty registry (all NOT_PUBLISHED tolerated)"; $PY -m ops.llm_agent.prompt_registry gate --phase pre --registry $R | grep verdict
echo "## 2 gate post on empty registry"; $PY -m ops.llm_agent.prompt_registry gate --phase post --registry $R | grep verdict || true
echo "## 3 live publish without confirmation"; $PY -m ops.llm_agent.registry_catalog publish --all-prompt-versions --registry $R 2>&1 | tail -1 || true
echo "## 4 guarded publish (pre gate, publish, post gate)"; $PY -m ops.llm_agent.registry_catalog publish --all-prompt-versions --registry $R --confirm-live-write $R --evidence "$E/publish.json" >/dev/null; python3 -c "import json;d=json.load(open('$E/publish.json'));print('verdict',d['verdict'],'readback',len(d['readback']),'pre',all(v.startswith('NOT_PUBLISHED') for v in d['gate']['pre'].values()),'post_match',sum(v=='MATCH' for v in d['gate']['post'].values()))"
echo "## 5 default probe (dry-run only)"; $PY scripts/llm_agent/registry_security_probe.py --registry $R --evidence "$E/probe.json" >/dev/null; python3 -c "import json;d=json.load(open('$E/probe.json'));print('verdict',d['verdict'],'writes',d['writes'],'dry_status',d['dry_run_write']['status'],'unchanged',d['dry_run_write']['unchanged'])"
echo "## 6 live write to a pinned prompt is refused before any request"; $PY scripts/llm_agent/registry_security_probe.py --registry $R --allow-live-write --write-target drift-specialist-system/v2 2>&1 | tail -1 || true
echo "## 7 live write without --write-target"; $PY scripts/llm_agent/registry_security_probe.py --registry $R --allow-live-write 2>&1 | tail -1 || true
echo "## 8 explicit guarded live write to a throwaway target via the API"
PYTHONPATH=scripts/llm_agent $PY - "$R" <<'PYEOF'
import sys
from registry_security_probe import Probe, ProbeSafetyError
p = Probe(sys.argv[1], allow_live_write=True, write_target="security-probe-guard/t1")
doc = lambda tag: {"apiVersion":"ar.dev/v1alpha1","kind":"Prompt","metadata":{"name":"security-probe-guard","tag":tag},"spec":{"description":"guard test","content":"throwaway"}}
print("declared target live write HTTP", p.write(doc("t1"), live=True)[0])
try: p.write(doc("t2"), live=True); print("UNEXPECTED")
except ProbeSafetyError as e: print("other target refused:", e)
print("readback", p.get("/v0/prompts/security-probe-guard/t1")[0])
PYEOF
echo "## 9 post gate unaffected by the throwaway object"; $PY -m ops.llm_agent.prompt_registry gate --phase post --registry $R | grep verdict
echo "## 10 tamper (local throwaway registry only), then gates"
$PY - "$R" <<'PYEOF'
import json,sys,urllib.request,yaml
R=sys.argv[1]; d=json.load(urllib.request.urlopen(R+"/v0/prompts/drift-specialist-system/v2"))
doc={"apiVersion":"ar.dev/v1alpha1","kind":"Prompt","metadata":{"name":"drift-specialist-system","tag":"v2","labels":d["metadata"].get("labels",{})},"spec":{"description":d["spec"]["description"],"content":d["spec"]["content"]+"\nprobe\n"}}
urllib.request.urlopen(urllib.request.Request(R+"/v0/apply?dryRun=false",data=yaml.safe_dump(doc).encode(),headers={"content-type":"application/yaml"},method="POST"))
PYEOF
$PY -m ops.llm_agent.prompt_registry gate --phase post --registry $R | grep -E 'verdict|drift-specialist/v2' || true
echo "configmap promotion:"; $PY -m ops.llm_agent.prompt_registry configmap --agent drift-specialist --version v1 --registry $R 2>&1 >/dev/null | cut -c1-90 || true
echo "normal publish:"; $PY -m ops.llm_agent.registry_catalog publish --all-prompt-versions --registry $R --confirm-live-write $R 2>&1 | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['verdict'],d.get('reason'))" || true
echo "restore publish:"; $PY -m ops.llm_agent.registry_catalog publish --all-prompt-versions --restore-from-git --registry $R --confirm-live-write $R 2>&1 | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['verdict'],'post_match',sum(v=='MATCH' for v in d['gate']['post'].values()))"
