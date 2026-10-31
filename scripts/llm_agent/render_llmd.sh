#!/usr/bin/env bash
# Render the pinned llm-d router chart and extract the InferencePool v1 CRD
# contract into infra/helm/llm-d/rendered/ for offline review and tests.
# Reads only: helm pull (digest-checked) and the GAIE manifest (sha256-checked).
# Installs nothing. usage: scripts/llm_agent/render_llmd.sh
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
# shellcheck source=/dev/null
source "$root/infra/helm/llm-d/versions.env"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
out="$root/infra/helm/llm-d/rendered"
mkdir -p "$out"

pulled=$(helm pull "$LLMD_ROUTER_CHART" --version "$LLMD_ROUTER_VERSION" --destination "$work" 2>&1)
grep -q "Digest: $LLMD_ROUTER_CHART_DIGEST" <<<"$pulled" || { echo "router chart digest mismatch" >&2; exit 1; }
helm template "$LLMD_RELEASE" "$work/llm-d-router-gateway-$LLMD_ROUTER_VERSION.tgz" -n ewallet-agents \
  -f "$root/infra/helm/llm-d/router-values.yaml" > "$out/router-$LLMD_ROUTER_VERSION.yaml"

curl -fsSL -o "$work/gaie.yaml" "$GAIE_MANIFEST_URL"
echo "$GAIE_MANIFEST_SHA256  $work/gaie.yaml" | sha256sum --check --quiet
python3 - "$work/gaie.yaml" "$out/inferencepool-$GAIE_VERSION-contract.json" <<'PY'
import json, sys, yaml
for doc in yaml.safe_load_all(open(sys.argv[1])):
    if doc and doc.get("kind") == "CustomResourceDefinition" and doc["spec"]["names"]["kind"] == "InferencePool":
        version = next(v for v in doc["spec"]["versions"] if v["storage"])
        spec = version["schema"]["openAPIV3Schema"]["properties"]["spec"]
        epr = spec["properties"]["endpointPickerRef"]
        contract = {
            "group": doc["spec"]["group"], "version": version["name"],
            "spec_required": spec["required"], "spec_fields": sorted(spec["properties"]),
            "endpointPickerRef_required": epr["required"], "endpointPickerRef_fields": sorted(epr["properties"]),
            "failureMode_enum": epr["properties"]["failureMode"]["enum"],
            "failureMode_default": epr["properties"]["failureMode"]["default"],
            "targetPorts_maxItems": spec["properties"]["targetPorts"]["maxItems"],
        }
        json.dump(contract, open(sys.argv[2], "w"), indent=1, sort_keys=True)
        open(sys.argv[2], "a").write("\n")
PY
sha256sum "$out"/* | sed "s#$root/##"
