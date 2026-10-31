#!/usr/bin/env python3
"""Add bounded-pilot resources where Substrate 0.0.9 exposes no values knob."""

from __future__ import annotations

import sys

import yaml


DEFAULT_RESOURCES = {
    "requests": {"cpu": "25m", "memory": "64Mi"},
    "limits": {"cpu": "250m", "memory": "256Mi"},
}

LARGER_CONTAINERS = {
    "ate-api-server": {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    },
    "atelet": {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    },
    "rustfs": {
        "requests": {"cpu": "50m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    },
    "valkey": {
        "requests": {"cpu": "25m", "memory": "64Mi"},
        "limits": {"cpu": "250m", "memory": "256Mi"},
    },
}


def pod_spec(document: dict) -> dict | None:
    kind = document.get("kind")
    spec = document.get("spec", {})
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        return spec.get("template", {}).get("spec")
    return None


def main() -> int:
    documents = [doc for doc in yaml.safe_load_all(sys.stdin) if doc]
    for document in documents:
        spec = pod_spec(document)
        if spec is None:
            continue
        for section in ("initContainers", "containers"):
            for container in spec.get(section, []):
                container.setdefault(
                    "resources", LARGER_CONTAINERS.get(container.get("name"), DEFAULT_RESOURCES)
                )
    yaml.safe_dump_all(documents, sys.stdout, sort_keys=False, explicit_start=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
