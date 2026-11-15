#!/usr/bin/env bash
# Feast cross-version check: the feature repo's locked Feast publishes, the MCP runtime's locked
# Feast reads, through the MCP server's own read code. Local only: a file registry and a
# throwaway Redis container on 127.0.0.1; nothing in any cluster is read or written.
# usage: feast_cross_version_check.sh <out-dir>
# Env: UV (default: uv on PATH), FEAST_COMPAT_PORT (default 16399).
set -euo pipefail
out=${1:?output directory required}
root=$(cd "$(dirname "$0")/../.." && pwd)
uv=${UV:-uv}
port=${FEAST_COMPAT_PORT:-16399}
redis_image=redis:7.4.2-alpine@sha256:02419de7eddf55aa5bcf49efb74e88fa8d931b4d77c07eff8a6b2144472b6952
name=feast-compat-$$
mkdir -p "$out"
out=$(cd "$out" && pwd)
cleanup() { docker rm -f "$name" > /dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --rm --name "$name" -p "127.0.0.1:$port:6379" "$redis_image" > /dev/null
for _ in $(seq 1 30); do docker exec "$name" redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 1; done
cd "$root"
export PYTHONPATH="$root"
"$uv" run --locked --project ai_platform/features/feast python scripts/llm_agent/feast_compat/writer.py "$out" "127.0.0.1:$port"
"$uv" run --locked --project infra/docker/llm-agent-tooling --extra feast python scripts/llm_agent/feast_compat/reader.py "$out" "127.0.0.1:$port"
