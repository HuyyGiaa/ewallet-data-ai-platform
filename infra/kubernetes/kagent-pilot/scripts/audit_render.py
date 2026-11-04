#!/usr/bin/env python3
"""Inventory approval-sensitive objects in a rendered Substrate pilot.

Exit 2 is intentional when cluster-scoped or privileged resources are found:
the render is valid, but applying it requires a separate owner approval.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import yaml


CLUSTER_SCOPED_KINDS = {
    "APIService",
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "MutatingWebhookConfiguration",
    "Namespace",
    "PriorityClass",
    "RuntimeClass",
    "SandboxConfig",
    "StorageClass",
    "ValidatingAdmissionPolicy",
    "ValidatingAdmissionPolicyBinding",
    "ValidatingWebhookConfiguration",
}
WORKLOAD_KINDS = {"DaemonSet", "Deployment", "Job", "StatefulSet"}


def identity(document: dict, source: Path) -> dict[str, object]:
    metadata = document.get("metadata") or {}
    return {
        "apiVersion": document.get("apiVersion"),
        "kind": document.get("kind"),
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "source": source.name,
    }


def privilege_reasons(document: dict) -> list[str]:
    if document.get("kind") not in WORKLOAD_KINDS:
        return []
    pod = (
        ((document.get("spec") or {}).get("template") or {}).get("spec") or {}
    )
    reasons: list[str] = []
    if pod.get("hostNetwork"):
        reasons.append("hostNetwork")
    if pod.get("hostPID"):
        reasons.append("hostPID")
    for volume in pod.get("volumes") or []:
        if "hostPath" in volume:
            reasons.append(f"hostPath:{volume.get('name')}")
    for section in ("initContainers", "containers"):
        for container in pod.get(section) or []:
            security = container.get("securityContext") or {}
            if security.get("privileged"):
                reasons.append(f"privileged:{container.get('name')}")
            if security.get("allowPrivilegeEscalation"):
                reasons.append(f"allowPrivilegeEscalation:{container.get('name')}")
    return reasons


def audit(rendered_directory: Path) -> dict[str, object]:
    documents: list[tuple[Path, dict]] = []
    for path in sorted(rendered_directory.glob("*.yaml")):
        documents.extend(
            (path, document)
            for document in yaml.safe_load_all(path.read_text())
            if isinstance(document, dict)
        )

    cluster_scoped = [
        identity(document, path)
        for path, document in documents
        if document.get("kind") in CLUSTER_SCOPED_KINDS
    ]
    privileged = []
    for path, document in documents:
        reasons = privilege_reasons(document)
        if reasons:
            privileged.append({**identity(document, path), "reasons": reasons})

    return {
        "classification": "source_render_only",
        "documentCount": len(documents),
        "kindCounts": dict(
            sorted(Counter(document.get("kind") for _, document in documents).items())
        ),
        "clusterScoped": cluster_scoped,
        "privilegedWorkloads": privileged,
        "verdict": (
            "BLOCKED_PENDING_OWNER_APPROVAL"
            if cluster_scoped or privileged
            else "NAMESPACE_SCOPED_RENDER"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("rendered_directory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.rendered_directory)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload)
    print(payload, end="")
    return 2 if report["verdict"] == "BLOCKED_PENDING_OWNER_APPROVAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
