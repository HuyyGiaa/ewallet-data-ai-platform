"""Extract description-free OpenAPI schemas of the pinned kagent/Substrate CRDs.

usage: extract_crd_schemas.py <kagent-crds chart dir> <output.json>

The chart is pulled by digest (infra/kubernetes/kagent-pilot/versions.env:
KAGENT_CRDS_DIGEST). The output lets tests validate rendered manifests
offline against the exact served schema (unknown fields, required, enums).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys

import yaml

KINDS = {"SandboxAgent", "RemoteMCPServer", "ModelConfig", "WorkerPool", "Agent"}
KEEP = {"type", "properties", "items", "required", "enum", "additionalProperties",
        "x-kubernetes-preserve-unknown-fields", "x-kubernetes-int-or-string", "anyOf", "format"}


def strip(node):
    """Keep validation keywords only; ``properties`` keeps every field name."""

    if isinstance(node, list):
        return [strip(v) for v in node]
    if not isinstance(node, dict):
        return node
    result = {}
    for key, value in node.items():
        if key == "properties":
            result[key] = {name: strip(child) for name, child in value.items()}
        elif key in KEEP:
            result[key] = strip(value)
    return result


def main() -> int:
    chart, output = Path(sys.argv[1]), Path(sys.argv[2])
    schemas = {}
    for path in sorted(chart.rglob("*.yaml")):
        text = re.sub(r"{{.*?}}", "", path.read_text(encoding="utf-8"))
        for doc in yaml.safe_load_all(text):
            if not doc or doc.get("kind") != "CustomResourceDefinition":
                continue
            kind = doc["spec"]["names"]["kind"]
            if kind not in KINDS:
                continue
            version = next(v for v in doc["spec"]["versions"] if v.get("storage"))
            schemas[kind] = {"apiVersion": f"{doc['spec']['group']}/{version['name']}",
                             "scale": "scale" in (version.get("subresources") or {}),
                             "schema": strip(version["schema"]["openAPIV3Schema"])}
    output.write_text(json.dumps(schemas, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print({kind: len(json.dumps(value)) for kind, value in schemas.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
