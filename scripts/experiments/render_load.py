#!/usr/bin/env python3
"""Render the load Job and its additive access rules as JSON manifests.

The Job calls the coordinator Service inside ``ewallet-agents``. Each access object adds
one flow for the ``agent-load`` identity. Everything carries one label, so cleanup can
remove exactly these objects. ``--print-hash`` gives the load profile hash used as a control.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOAD_SCRIPT = ROOT / "ops/experiments/agent_load.py"
FIXTURE = ROOT / "configs/experiments/fixtures/agent-load-request.json"
NAMESPACE = "ewallet-agents"
LABELS = {"ewallet.io/experiment-load": "true"}
TARGET = {"ewallet.io/agent": "fraud-coordinator"}
PORT = 8080


def profile_hash(concurrency: int, duration: int, max_requests: int) -> str:
    """Hash everything that defines the offered load."""

    digest = hashlib.sha256()
    digest.update(FIXTURE.read_bytes())
    digest.update(LOAD_SCRIPT.read_bytes())
    digest.update(f"{concurrency}/{duration}/{max_requests}".encode())
    return digest.hexdigest()


def _meta(name: str) -> dict[str, Any]:
    return {"name": name, "namespace": NAMESPACE, "labels": LABELS}


def manifests(image: str, run_id: str, concurrency: int, duration: int, max_requests: int) -> list[dict[str, Any]]:
    """Build every object of one load run."""

    tcp = [{"protocol": "TCP", "port": PORT}, {"protocol": "TCP", "port": 15008}]
    pod = {
        "serviceAccountName": "agent-load", "automountServiceAccountToken": False, "restartPolicy": "Never",
        "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "load", "image": image, "imagePullPolicy": "IfNotPresent", "command": ["python3", "/load/agent_load.py"],
            "args": ["--url", f"http://fraud-coordinator.{NAMESPACE}.svc.cluster.local:{PORT}/",
                     "--fixture", "/load/request.json", "--run-id", run_id, "--concurrency", str(concurrency),
                     "--duration", str(duration), "--max-requests", str(max_requests)],
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "200m", "memory": "128Mi"}},
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [{"name": "load", "mountPath": "/load", "readOnly": True}],
        }],
        "volumes": [{"name": "load", "configMap": {"name": "agent-load"}}],
    }
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": _meta("agent-load"), "automountServiceAccountToken": False},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": _meta("agent-load"),
         "data": {"agent_load.py": LOAD_SCRIPT.read_text(encoding="utf-8"), "request.json": FIXTURE.read_text(encoding="utf-8")}},
        {"apiVersion": "security.istio.io/v1", "kind": "AuthorizationPolicy", "metadata": _meta("allow-agent-load-to-fraud-coordinator"),
         "spec": {"action": "ALLOW", "selector": {"matchLabels": TARGET},
                  "rules": [{"from": [{"source": {"principals": [f"cluster.local/ns/{NAMESPACE}/sa/agent-load"]}}],
                             "to": [{"operation": {"ports": [str(PORT)]}}]}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": _meta("allow-agent-load-to-fraud-coordinator-ingress"),
         "spec": {"podSelector": {"matchLabels": TARGET}, "policyTypes": ["Ingress"],
                  "ingress": [{"from": [{"podSelector": {"matchLabels": LABELS}}], "ports": tcp}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": _meta("allow-agent-load-egress"),
         "spec": {"podSelector": {"matchLabels": LABELS}, "policyTypes": ["Egress"],
                  "egress": [{"to": [{"podSelector": {"matchLabels": TARGET}}], "ports": tcp}]}},
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": _meta("agent-load"),
         "spec": {"backoffLimit": 0, "activeDeadlineSeconds": duration + 600,
                  "template": {"metadata": {"labels": LABELS}, "spec": pod}}},
    ]


def main(argv: list[str] | None = None) -> int:
    """Print the manifests or the load profile hash."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument("--print-hash", action="store_true")
    args = parser.parse_args(argv)
    if args.print_hash:
        print(profile_hash(args.concurrency, args.duration, args.max_requests))
        return 0
    if not re.search(r"@sha256:[0-9a-f]{64}$", args.image) or not args.run_id:
        parser.error("--image must be pinned by digest and --run-id is required")
    sys.stdout.write(json.dumps({"apiVersion": "v1", "kind": "List",
                                 "items": manifests(args.image, args.run_id, args.concurrency, args.duration, args.max_requests)},
                                indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
