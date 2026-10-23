#!/usr/bin/env bash
set -euo pipefail

PILOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${PILOT_DIR}/versions.env"

RENDER_DIR="${1:-$(mktemp -d /tmp/kagent-pilot-render.XXXXXX)}"
CHART_DIR="${RENDER_DIR}/charts"
mkdir -p "${CHART_DIR}" "${RENDER_DIR}/rendered"

pull_chart() {
  local reference="$1"
  local version="$2"
  local expected_digest="$3"
  local log_file="$4"
  helm pull "${reference}" --version "${version}" --untar --untardir "${CHART_DIR}" \
    2>&1 | tee "${log_file}"
  grep -F "Digest: ${expected_digest}" "${log_file}" >/dev/null
}

pull_chart "${SUBSTRATE_CRDS_CHART}" "${SUBSTRATE_VERSION}" "${SUBSTRATE_CRDS_DIGEST}" "${RENDER_DIR}/substrate-crds.pull.log"
pull_chart "${SUBSTRATE_CHART}" "${SUBSTRATE_VERSION}" "${SUBSTRATE_CHART_DIGEST}" "${RENDER_DIR}/substrate.pull.log"
pull_chart "${KAGENT_CRDS_CHART}" "${KAGENT_VERSION}" "${KAGENT_CRDS_DIGEST}" "${RENDER_DIR}/kagent-crds.pull.log"
pull_chart "${KAGENT_CHART}" "${KAGENT_VERSION}" "${KAGENT_CHART_DIGEST}" "${RENDER_DIR}/kagent.pull.log"

helm template substrate-crds "${CHART_DIR}/substrate-crds" \
  --namespace ate-system >"${RENDER_DIR}/rendered/substrate-crds.yaml"
helm template substrate "${CHART_DIR}/substrate" \
  --namespace ate-system \
  -f "${PILOT_DIR}/values/substrate.yaml" \
  --post-renderer "${PILOT_DIR}/scripts/substrate_resource_post_renderer.py" \
  >"${RENDER_DIR}/rendered/substrate.yaml"
helm template kagent-crds "${CHART_DIR}/kagent-crds" \
  --namespace kagent >"${RENDER_DIR}/rendered/kagent-crds.yaml"
helm template kagent "${CHART_DIR}/kagent" \
  --namespace kagent \
  -f "${PILOT_DIR}/values/kagent.yaml" \
  >"${RENDER_DIR}/rendered/kagent.yaml"

python3 - "${RENDER_DIR}" "${PILOT_DIR}/manifests" <<'PY'
from pathlib import Path
import sys
import jsonschema
import yaml

root = Path(sys.argv[1])
schemas = {}
for path in sorted((root / "rendered").glob("*.yaml")):
    documents = list(yaml.safe_load_all(path.read_text()))
    if path.name.endswith("crds.yaml"):
        for crd in documents:
            if not crd or crd.get("kind") != "CustomResourceDefinition":
                continue
            for version in crd["spec"]["versions"]:
                if version["served"]:
                    key = (crd["spec"]["group"], version["name"], crd["spec"]["names"]["kind"])
                    schemas[key] = version["schema"]["openAPIV3Schema"]

manifest_root = Path(sys.argv[2]) if len(sys.argv) > 2 else None
if manifest_root:
    for path in sorted(manifest_root.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text()):
            if not document or "/" not in document["apiVersion"]:
                continue
            group, version = document["apiVersion"].split("/", 1)
            schema = schemas.get((group, version, document["kind"]))
            if schema:
                jsonschema.Draft202012Validator(schema).validate(document)
print(root)
PY
