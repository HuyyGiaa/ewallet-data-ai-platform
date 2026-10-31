#!/usr/bin/env bash
# OFFLINE R1 gateway test: the pinned agentgateway binary with the
# chart-rendered LLM config (test-only short timeouts) in front of two mock
# vLLM backends (scripts/llm_agent/r1_mock/mock_vllm.py), on an internal
# Docker network. No model, no cluster. Each scenario gets a fresh gateway so
# eviction state does not leak between scenarios. Removes only what it creates.
# usage: scripts/llm_agent/r1_mock/run.sh <evidence.json>
set -euo pipefail
root=$(cd "$(dirname "$0")/../../.." && pwd)
chart="$root/infra/helm/llm-agent-platform"
image=$(python3 -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['agentgateway']['image'])" "$chart/values.yaml")
python_image=python:3.12.12-slim-trixie
out=$1
tag="ewallet-r1-mock-$$"
work=$(mktemp -d)
backend_key=mock-backend-key
cleanup() {
  docker rm -f "$tag-gw" "$tag-primary" "$tag-fallback" >/dev/null 2>&1 || true
  docker network rm "$tag" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

# Test-only budgets (nested like production): client 30 > entry 6 > listener 5 > backend 4 >= idle 4.
helm template llm-agent "$chart" -f "$chart/values-local.yaml" \
  --set modelConfig.timeoutSeconds=30 --set agentgateway.requestTimeout=6s \
  --set agentgateway.llmRequestTimeout=5s --set agentgateway.backendRequestTimeout=4s \
  --set agentgateway.responseIdleTimeout=4s | python3 -c '
import sys, yaml
for doc in yaml.safe_load_all(sys.stdin):
    if doc and doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "agentgateway-llm-config":
        sys.stdout.write(doc["data"]["config.yaml"])' > "$work/llm.yaml"
cp "$root/scripts/llm_agent/r1_mock/"{mock_vllm.py,client.py} "$root/scripts/llm_agent/smoke_tool_call.py" "$work/"
chmod 755 "$work" && chmod 644 "$work"/*

docker network create --internal "$tag" >/dev/null
for backend in primary fallback; do
  docker run -d --name "$tag-$backend" --network "$tag" --memory 96m --cpus 0.25 \
    --network-alias "vllm-$backend.ewallet-agents.svc.cluster.local" \
    -v "$work:/w:ro" "$python_image" python -u /w/mock_vllm.py 8000 "$backend_key" "$backend" >/dev/null
done
echo "{" > "$out.tmp"
first=1
for scenario in ok_non_stream ok_stream primary_500_not_replayed primary_slow_beyond_backend_budget \
                primary_down_503 primary_down_then_failover all_backends_down; do
  docker run -d --name "$tag-gw" --network "$tag" --network-alias gw --memory 256m --cpus 0.5 \
    -e VLLM_API_KEY="$backend_key" -v "$work:/cfg:ro" "$image" -f /cfg/llm.yaml >/dev/null
  sleep 3
  result=$(docker run --rm --network "$tag" --memory 96m --cpus 0.25 -v "$work:/w:ro" "$python_image" \
    python /w/client.py "$scenario")
  [ $first = 1 ] || echo "," >> "$out.tmp"
  first=0
  printf '"%s": %s\n' "$scenario" "$result" >> "$out.tmp"
  docker rm -f "$tag-gw" >/dev/null
done
echo "}" >> "$out.tmp"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["_meta"]={"kind": "OFFLINE_MOCK_BACKEND", "gateway_image": sys.argv[2], "note": "mock vLLM, not a model; not runtime evidence"}; json.dump(d, open(sys.argv[3],"w"), indent=1, sort_keys=True)' "$out.tmp" "$image" "$out"
rm -f "$out.tmp"
python3 - "$out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for name, r in d.items():
    if name.startswith("_"):
        continue
    first = r["first_turn"]
    hits = {k: len(v) for k, v in r["backend_requests"].items()}
    print(f"{name:38s} verdict={r['verdict']:4s} status={first.get('status')} "
          f"backend={first.get('backend')} latency={first.get('latency_s')} hits={hits}")
PY
