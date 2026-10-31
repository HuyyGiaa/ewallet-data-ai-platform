#!/usr/bin/env bash
# Validate every agentgateway config this chart renders with the pinned
# agentgateway binary (`--validate-only`: parses config and CEL, starts no
# proxy), and the OpenTelemetry Collector config with the pinned collector
# (`validate`). Runs the images with no network, 256 MiB and half a CPU.
# usage: scripts/llm_agent/validate_gateway_configs.sh [evidence.txt]
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
chart="$root/infra/helm/llm-agent-platform"
image=$(python3 -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['agentgateway']['image'])" "$chart/values.yaml")
collector=$(python3 -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['otelCollector']['image'])" "$chart/values.yaml")
out=${1:-/dev/stdout}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

render() {  # name configmap helm-args...
  local name=$1 configmap=$2; shift 2
  helm template llm-agent "$chart" -f "$chart/values-gcp.yaml" "$@" | python3 -c '
import sys, yaml
for doc in yaml.safe_load_all(sys.stdin):
    if doc and doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == sys.argv[1]:
        sys.stdout.write(doc["data"]["config.yaml"])' "$configmap" > "$work/$name.yaml"
}

render llm agentgateway-llm-config
render llm-llmd agentgateway-llm-config --set llmd.enabled=true
render mcp-deny-all mcp-gateway-config --set mcpGateway.enabled=true
render mcp-callers mcp-gateway-config --set mcpGateway.enabled=true \
  --set mcpGateway.callerServiceAccounts.drift-specialist=worker-drift \
  --set mcpGateway.callerServiceAccounts.feature-rag-specialist=worker-feature-rag
render a2a-deny-all a2a-gateway-config --set a2aGateway.enabled=true
render a2a-callers a2a-gateway-config --set a2aGateway.enabled=true \
  --set a2aGateway.callerServiceAccounts.fraud-coordinator=worker-coordinator
printf 'gateways:\n  default:\n    port: 8080\n    bogusField: 1\n' > "$work/negative-control.yaml"
mkdir -p "$work/otel"
for variant in debug traces; do
  extra=()
  [ "$variant" = traces ] && extra=(--set otelCollector.tracesEndpoint=jaeger-collector.observability:4317)
  helm template llm-agent "$chart" -f "$chart/values-gcp.yaml" --set otelCollector.enabled=true "${extra[@]}" | python3 -c '
import sys, yaml
for doc in yaml.safe_load_all(sys.stdin):
    if doc and doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "otel-collector-config":
        sys.stdout.write(doc["data"]["config.yaml"])' > "$work/otel/collector-$variant.yaml"
done

chmod 755 "$work" "$work/otel" && chmod 644 "$work"/*.yaml "$work"/otel/*.yaml
{
  echo "# agentgateway --validate-only, image $image"
  status=0
  for file in "$work"/*.yaml; do
    name=$(basename "$file" .yaml)
    result=$(docker run --rm --network none --memory 256m --cpus 0.5 -e VLLM_API_KEY=validate-only \
      -v "$work:/cfg:ro" "$image" --validate-only -f "/cfg/$name.yaml" 2>&1 | tail -1 || true)
    echo "$name: $result"
    if [ "$name" = negative-control ]; then
      [[ "$result" == *"unknown field"* ]] || status=1
    else
      [ "$result" = "Configuration is valid!" ] || status=1
    fi
  done
  echo "# otelcol-contrib validate, image $collector"
  for file in "$work"/otel/*.yaml; do
    name=$(basename "$file" .yaml)
    if docker run --rm --network none --memory 256m --cpus 0.5 -v "$work/otel:/conf:ro" "$collector" \
         validate --config="/conf/$name.yaml" > /dev/null 2>&1; then
      echo "$name: valid"
    else
      echo "$name: INVALID"; status=1
    fi
  done
  echo "overall: $([ $status = 0 ] && echo PASS || echo FAIL)"
} | tee "$out"
grep -q '^overall: PASS' "$out"
