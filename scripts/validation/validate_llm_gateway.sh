#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHART_DIR="${ROOT_DIR}/infra/helm/llm-agent-platform"
KAGENT_DIR="${ROOT_DIR}/infra/kubernetes/kagent-pilot"
VALIDATION_DIR="$(mktemp -d /tmp/llm-agent-validation.XXXXXX)"
trap 'rm -rf "${VALIDATION_DIR}"' EXIT

helm lint "${CHART_DIR}" -f "${CHART_DIR}/values-gcp.yaml"
helm template llm-agent "${CHART_DIR}" \
  -f "${CHART_DIR}/values-gcp.yaml" >"${VALIDATION_DIR}/platform.yaml"

"${KAGENT_DIR}/scripts/render.sh" "${VALIDATION_DIR}/kagent"

python3 - "${VALIDATION_DIR}" "${KAGENT_DIR}/manifests/model-config.yaml" <<'PY'
from pathlib import Path
import sys

import jsonschema
import yaml

root = Path(sys.argv[1])
pilot_model_path = Path(sys.argv[2])
crds = []
for path in (root / "kagent" / "rendered").glob("*crds.yaml"):
    crds.extend(
        document
        for document in yaml.safe_load_all(path.read_text())
        if document and document.get("kind") == "CustomResourceDefinition"
    )
model_crd = next(item for item in crds if item["spec"]["names"]["kind"] == "ModelConfig")
version = next(item for item in model_crd["spec"]["versions"] if item["name"] == "v1alpha2")
validator = jsonschema.Draft202012Validator(version["schema"]["openAPIV3Schema"])

platform_docs = [item for item in yaml.safe_load_all((root / "platform.yaml").read_text()) if item]
models = [item for item in platform_docs if item.get("kind") == "ModelConfig"]
models.extend(
    item
    for item in yaml.safe_load_all(pilot_model_path.read_text())
    if item and item.get("kind") == "ModelConfig"
)
for model in models:
    validator.validate(model)
    spec = model["spec"]
    if "apiKeySecret" not in spec or "apiKeySecretRef" in spec or "stream" in spec:
        raise ValueError(f"{model['metadata']['name']}: incompatible kagent 0.10.3 ModelConfig")

config_map = next(
    item
    for item in platform_docs
    if item.get("kind") == "ConfigMap"
    and item["metadata"]["name"] == "agentgateway-llm-config"
)
(root / "agentgateway.yaml").write_text(config_map["data"]["config.yaml"])
print("MODELCONFIG_CRD_VALID")
PY

docker run --rm \
  -e VLLM_API_KEY=validation-placeholder \
  -v "${VALIDATION_DIR}/agentgateway.yaml:/config.yaml:ro" \
  "ghcr.io/agentgateway/agentgateway:v1.7.0@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b" \
  -f /config.yaml --validate-only

python3 -m unittest -v tests.llm_agent.test_phase_b tests.unit.test_kagent_bounded_pilot
