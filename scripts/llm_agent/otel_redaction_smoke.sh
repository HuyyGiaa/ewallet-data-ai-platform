#!/usr/bin/env bash
# Collector redaction smoke: the chart-rendered collector config (debug
# exporter switched to detailed so attributes are printed) receives one OTLP
# span carrying synthetic PII, and the printed span is checked for leaks.
# Internal Docker network, memory-capped; removes only what it creates.
# usage: scripts/llm_agent/otel_redaction_smoke.sh <evidence.json>
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
chart="$root/infra/helm/llm-agent-platform"
image=$(python3 -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['otelCollector']['image'])" "$chart/values.yaml")
out=$1
tag="ewallet-otel-smoke-$$"
work=$(mktemp -d)
cleanup() {
  docker rm -f "$tag-col" >/dev/null 2>&1 || true
  docker network rm "$tag" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

helm template llm-agent "$chart" -f "$chart/values-gcp.yaml" --set otelCollector.enabled=true | python3 -c '
import sys, yaml
for doc in yaml.safe_load_all(sys.stdin):
    if doc and doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "otel-collector-config":
        config = yaml.safe_load(doc["data"]["config.yaml"])
config["exporters"]["debug"]["verbosity"] = "detailed"
yaml.safe_dump(config, open(sys.argv[1], "w"))' "$work/config.yaml"
cat > "$work/send.py" <<'PY'
import json, time, urllib.request
now = time.time_ns()
attrs = {"http.request.header.authorization": "Bearer synthetic-secret-token",
         "user.id": "customer-0001", "note": "card 4111 1111 1111 1111 mail jane.doe@example.com",
         "gen_ai.prompt": "synthetic prompt text", "ewallet.tool": "feature_lookup"}
span = {"traceId": "5b8efff798038103d269b633813fc60c", "spanId": "eee19b7ec3c1b174", "name": "redaction-smoke",
        "kind": 1, "startTimeUnixNano": str(now), "endTimeUnixNano": str(now + 1000),
        "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]}
body = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "smoke"}}]},
                           "scopeSpans": [{"spans": [span]}]}]}
for _ in range(30):
    try:
        req = urllib.request.Request("http://collector:4318/v1/traces", json.dumps(body).encode(),
                                     {"content-type": "application/json"})
        print(urllib.request.urlopen(req, timeout=5).status)
        break
    except Exception:
        time.sleep(1)
PY
chmod 755 "$work" && chmod 644 "$work"/*
docker network create --internal "$tag" >/dev/null
docker run -d --name "$tag-col" --network "$tag" --network-alias collector --memory 256m --cpus 0.5 \
  -v "$work:/conf:ro" "$image" --config=/conf/config.yaml >/dev/null
status=$(docker run --rm --network "$tag" --memory 128m --cpus 0.25 -v "$work:/w:ro" python:3.12.12-slim-trixie \
  python /w/send.py)
# batch processor flushes every 5 s
sleep 8
docker logs "$tag-col" > "$work/collector.log" 2>&1
python3 - "$work/collector.log" "$out" "$status" "$image" <<'PY'
import json, sys
log = open(sys.argv[1]).read()
secrets = {"bearer_token": "synthetic-secret-token", "card_number": "4111 1111 1111 1111",
           "email": "jane.doe@example.com", "raw_user_id": "customer-0001", "prompt_text": "synthetic prompt text"}
report = {
    "otlp_http_status": sys.argv[3], "collector_image": sys.argv[4],
    "span_exported": "redaction-smoke" in log,
    "non_sensitive_attribute_kept": "feature_lookup" in log,
    "leaked": sorted(name for name, value in secrets.items() if value in log),
    "redaction_markers": [m for m in ("****", "redaction.masked") if m in log],
}
report["verdict"] = "PASS" if report["span_exported"] and report["non_sensitive_attribute_kept"] and not report["leaked"] else "FAIL"
json.dump(report, open(sys.argv[2], "w"), indent=1, sort_keys=True)
print(json.dumps(report))
sys.exit(0 if report["verdict"] == "PASS" else 1)
PY
