#!/usr/bin/env python3
"""Bound resources and pin images where Substrate 0.0.9 has no values knob."""

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

# The chart artifact is digest-pinned, but several v0.0.9 templates still emit
# mutable image tags. These are the independently read-back multi-architecture
# manifest digests used by the approved pilot. Fail closed on an unknown tag;
# silently deploying a newly resolved image would defeat the review boundary.
PINNED_IMAGES = {
    "ghcr.io/kagent-dev/substrate/ateapi:v0.0.9": "sha256:3c50661b4e7d9e697ada97a97b2a8df563057a2cab5b9e7b0b09060ff80d9e85",
    "ghcr.io/kagent-dev/substrate/atecontroller:v0.0.9": "sha256:dc29e79987fe4177b92c8b30a4fa4f36f3cf1a80937b03a7a8cd3411f98693e5",
    "ghcr.io/kagent-dev/substrate/atelet:v0.0.9": "sha256:ec3e842a1a0a060192710c34be83354d0b45e8d0e40342384d3b836413c3c8e1",
    "ghcr.io/kagent-dev/substrate/atenet:v0.0.9": "sha256:15892218c70e736bca06bd9f1134425897f8b9c265428f932de5d6d779e9452a",
    "valkey/valkey:8.0": "sha256:04b9002ae24bbe66687fb025052ed9bcebb54f8da3937338eb718d98d362887f",
    "cr.agentgateway.dev/agentgateway:v1.3.0-alpha.1": "sha256:95e8d849c44ba31399daf504367ecfcfe2d3d3e59ffd96b1098234353afc3ef7",
    "coredns/coredns:1.11.1": "sha256:1eeb4c7316bacb1d4c8ead65571cd92dd21e27359f0d4917f1a5822a73b75db1",
    "busybox:1.36": "sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662",
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
                image = container.get("image", "")
                if "@sha256:" not in image:
                    digest = PINNED_IMAGES.get(image)
                    if digest is None:
                        raise ValueError(f"unreviewed mutable Substrate image: {image}")
                    container["image"] = f"{image}@{digest}"
                container.setdefault(
                    "resources", LARGER_CONTAINERS.get(container.get("name"), DEFAULT_RESOURCES)
                )
    yaml.safe_dump_all(documents, sys.stdout, sort_keys=False, explicit_start=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
