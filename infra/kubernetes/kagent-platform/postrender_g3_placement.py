#!/usr/bin/env python3
"""Helm post-renderer for the G3 kagent release: workloads stay on G3 nodes.

The kagent 0.10.3 chart has nodeSelector values for the controller and the UI
but none for the bundled PostgreSQL Deployment. This post-renderer adds
``ewallet.io/g3-llm: "true"`` to every Deployment/StatefulSet/DaemonSet pod
template that has no nodeSelector, and changes nothing else, so no kagent pod
can land on an MLOps node. Reads the rendered manifests on stdin, writes them
to stdout (Helm post-renderer contract).

usage: helm install ... --post-renderer infra/kubernetes/kagent-platform/postrender_g3_placement.py
"""

from __future__ import annotations

import sys

import yaml

SELECTOR = {"ewallet.io/g3-llm": "true"}
WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet"}


def place(documents: list) -> list:
    for doc in documents:
        if isinstance(doc, dict) and doc.get("kind") in WORKLOADS:
            pod = doc["spec"]["template"]["spec"]
            if not pod.get("nodeSelector"):
                pod["nodeSelector"] = dict(SELECTOR)
    return documents


def main() -> int:
    documents = [d for d in yaml.safe_load_all(sys.stdin) if d is not None]
    yaml.safe_dump_all(place(documents), sys.stdout, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
