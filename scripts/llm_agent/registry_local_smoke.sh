#!/usr/bin/env bash
# Local agentregistry v0.4.0 catalog smoke: Postgres + registry server in
# Docker (memory-capped, API bound to 127.0.0.1 only, throwaway password that
# is never written to disk), publish the E-Wallet catalog through /v0/apply,
# read every entry back, then remove only the containers and network it made.
# usage: scripts/llm_agent/registry_local_smoke.sh <evidence.json>
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
# shellcheck source=/dev/null
source "$root/infra/helm/agentregistry/versions.env"
out=$1
tag="ewallet-registry-smoke-$$"
port=${REGISTRY_PORT:-12121}
password=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')
cleanup() {
  docker rm -f "$tag-server" "$tag-db" >/dev/null 2>&1 || true
  docker network rm "$tag" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$tag" >/dev/null
docker run -d --name "$tag-db" --network "$tag" --network-alias agentregistry-postgresql \
  --memory 256m --cpus 0.5 -e POSTGRES_DB=agentregistry -e POSTGRES_USER=agentregistry \
  -e POSTGRES_PASSWORD="$password" postgres:18 >/dev/null
for _ in $(seq 1 60); do
  docker exec "$tag-db" pg_isready -U agentregistry -d agentregistry >/dev/null 2>&1 && break
  sleep 1
done
docker run -d --name "$tag-server" --network "$tag" --memory 384m --cpus 0.5 \
  -p "127.0.0.1:$port:8080" \
  -e AGENT_REGISTRY_SERVER_ADDRESS=:8080 -e AGENT_REGISTRY_MCP_PORT=31313 \
  -e AGENT_REGISTRY_AGENT_GATEWAY_PORT=21212 -e AGENT_REGISTRY_ENABLE_REGISTRY_VALIDATION=false \
  -e AGENT_REGISTRY_DATABASE_URL="postgres://agentregistry:$password@agentregistry-postgresql:5432/agentregistry?sslmode=disable" \
  "$AGENTREGISTRY_SERVER_IMAGE" >/dev/null
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$port/v0/health" >/dev/null 2>&1 && break
  sleep 1
done
version=$(curl -fsS "http://127.0.0.1:$port/v0/version" || echo '{}')
cd "$root"
set +e
python3 -m ops.llm_agent.registry_catalog publish --registry "http://127.0.0.1:$port" --evidence "$out.publish"
status=$?
set -e
agents=$(curl -fsS "http://127.0.0.1:$port/v0/agents" || echo '{}')
python3 - "$out.publish" "$out" "$version" "$agents" "$(docker image inspect "$AGENTREGISTRY_SERVER_IMAGE" --format '{{index .RepoDigests 0}}')" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
report["registry_version"] = json.loads(sys.argv[3] or "{}")
listing = json.loads(sys.argv[4] or "{}")
report["agents_listing"] = sorted((i["metadata"]["name"], i["metadata"].get("tag")) for i in listing.get("items") or [])
report["server_image"] = sys.argv[5]
report["environment"] = "local Docker, registry v0.4.0 + postgres:18, catalog only (no kagent runtime)"
json.dump(report, open(sys.argv[2], "w"), indent=1, sort_keys=True)
PY
rm -f "$out.publish"
docker logs "$tag-server" 2>&1 | tail -5 | sed -e "s/$password/<redacted>/g" >&2
exit $status
