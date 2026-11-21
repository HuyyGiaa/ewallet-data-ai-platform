#!/usr/bin/env bash
# Local check of grafanaExternal.rootUrl (llm-agent-platform) against the pinned Grafana image:
# without GF_SERVER_ROOT_URL Grafana reports appUrl http://localhost:3000/; with it, the public
# URL, while redirects stay relative and static assets load. LOCAL_DOCKER only (127.0.0.1).
# usage: grafana_root_url_check.sh [root-url]   (default https://observe.edge.example.test/)
set -euo pipefail
url=${1:-https://observe.edge.example.test/}
image=$(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["observabilityStack"]["grafana"]["image"])' \
  "$(cd "$(dirname "$0")/../.." && pwd)/infra/helm/llm-agent-platform/values.yaml")
names=(edge-grafana-check-default edge-grafana-check-rooturl); trap 'docker rm -f "${names[@]}" >/dev/null 2>&1 || true' EXIT
docker run -d --rm --name "${names[0]}" -p 127.0.0.1:13001:3000 -e GF_PATHS_DATA=/tmp/grafana-data "$image" >/dev/null
docker run -d --rm --name "${names[1]}" -p 127.0.0.1:13002:3000 -e GF_PATHS_DATA=/tmp/grafana-data -e GF_SERVER_ROOT_URL="$url" "$image" >/dev/null
for port in 13001 13002; do
  for _ in $(seq 1 60); do curl -sf -o /dev/null "http://127.0.0.1:$port/api/health" && break; sleep 1; done
done
fail=0
check() {  # port expected-appUrl label
  local page location asset code
  page=$(curl -s "http://127.0.0.1:$1/login"); location=$(curl -s -o /dev/null -w '%{redirect_url}' "http://127.0.0.1:$1/")
  asset=$(grep -oE 'public/build/[^"]+\.js' <<< "$page" | head -1); code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$1/$asset")
  app=$(grep -o '"appUrl":"[^"]*"' <<< "$page" | head -1 | cut -d'"' -f4)
  ok=true; [ "$app" == "$2" ] && [ "$location" == "http://127.0.0.1:$1/login" ] && [ "$code" == 200 ] || { ok=false; fail=1; }
  printf '{"check": "%s", "passed": %s, "observed": "appUrl=%s redirect=%s asset=%s"}\n' "$3" "$ok" "$app" "$location" "$code"
}
check 13001 http://localhost:3000/ "default: appUrl is localhost (the G3-A1 finding)"
check 13002 "$url" "rootUrl set: appUrl is the public URL, redirects relative, assets load"
exit $fail
