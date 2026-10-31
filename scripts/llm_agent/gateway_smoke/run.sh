#!/usr/bin/env bash
# Local policy smoke for the MCP Gateway: the chart-rendered agentgateway
# config in front of a header-recording MCP stub, on an internal Docker
# network (no egress, no host ports). Removes only the containers and network
# it creates. Not a substitute for the in-cluster mesh test: Docker gives no
# Istio identity, so source.identity is absent here by construction.
# usage: scripts/llm_agent/gateway_smoke/run.sh <evidence.json>
set -euo pipefail
root=$(cd "$(dirname "$0")/../../.." && pwd)
chart="$root/infra/helm/llm-agent-platform"
image=$(python3 -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['agentgateway']['image'])" "$chart/values.yaml")
python_image=python:3.12.12-slim-trixie
out=$1
tag="ewallet-mcpgw-smoke-$$"
work=$(mktemp -d)
cleanup() {
  docker rm -f "$tag-stub" "$tag-gw" >/dev/null 2>&1 || true
  docker network rm "$tag" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

render() {  # name helm-args...
  local name=$1; shift
  helm template llm-agent "$chart" -f "$chart/values-gcp.yaml" --set mcpGateway.enabled=true "$@" | python3 -c '
import sys, yaml
for doc in yaml.safe_load_all(sys.stdin):
    if doc and doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "mcp-gateway-config":
        sys.stdout.write(doc["data"]["config.yaml"])' > "$work/$name.yaml"
}
render deny-all
render callers --set mcpGateway.callerServiceAccounts.drift-specialist=worker-drift \
  --set mcpGateway.callerServiceAccounts.feature-rag-specialist=worker-feature-rag
# TEST ONLY: identity-free rule, to observe routing and header handling on the
# allow path. Never a production configuration.
python3 - "$work/callers.yaml" "$work/test-only-identity-free.yaml" <<'PY'
import sys, yaml
config = yaml.safe_load(open(sys.argv[1]))
for route in config["routes"]:
    route["policies"]["mcpAuthorization"]["rules"] = ['mcp.tool.name == "feature_lookup"']
yaml.safe_dump(config, open(sys.argv[2], "w"), sort_keys=False)
PY
cp "$root/scripts/llm_agent/gateway_smoke/"{stub_mcp_server.py,client.py} "$work/"
chmod 755 "$work" && chmod 644 "$work"/*

docker network create --internal "$tag" >/dev/null
docker run -d --name "$tag-stub" --network "$tag" --memory 128m --cpus 0.25 \
  --network-alias feature-rag-mcp.ewallet-agents.svc.cluster.local \
  --network-alias drift-mcp.ewallet-agents.svc.cluster.local \
  -v "$work:/w:ro" "$python_image" python -u /w/stub_mcp_server.py 8080 >/dev/null

echo "{" > "$out.tmp"
first=1
for variant in deny-all callers test-only-identity-free; do
  docker run -d --name "$tag-gw" --network "$tag" --network-alias mcp-gateway --memory 256m --cpus 0.5 \
    -v "$work:/cfg:ro" "$image" -f "/cfg/$variant.yaml" >/dev/null
  for _ in $(seq 1 30); do
    docker logs "$tag-gw" 2>&1 | grep -q -i 'listen\|started\|serving' && break
    sleep 1
  done
  sleep 2
  since=$(date -u +%FT%T.%NZ)
  client=$(docker run --rm --network "$tag" --memory 128m --cpus 0.25 -v "$work:/w:ro" "$python_image" \
    python -u /w/client.py http://mcp-gateway:8080 /feature-rag/mcp /drift/mcp)
  stub=$(docker logs --since "$since" "$tag-stub" 2>&1 | python3 -c 'import sys,json; print(json.dumps([json.loads(l) for l in sys.stdin if l.startswith("{")]))')
  gwlog=$(docker logs "$tag-gw" 2>&1 | grep -c '"mcp' || true)
  [ $first = 1 ] || echo "," >> "$out.tmp"
  first=0
  printf '"%s": {"client": %s, "backend_saw": %s, "gateway_mcp_log_lines": %s}\n' "$variant" "$client" "$stub" "$gwlog" >> "$out.tmp"
  docker rm -f "$tag-gw" >/dev/null
done
echo "}" >> "$out.tmp"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["_meta"]={"image": sys.argv[2], "network": "docker --internal", "identity": "no Istio identity in Docker: source.identity absent"}; json.dump(d, open(sys.argv[3],"w"), indent=1, sort_keys=True)' "$out.tmp" "$image" "$out"
rm -f "$out.tmp"
cat "$out"
