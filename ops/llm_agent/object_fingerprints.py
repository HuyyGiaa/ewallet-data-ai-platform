"""[READ-ONLY] Fingerprints of the objects a cleanup must keep, and an exact before/after comparison.

``snapshot`` hashes the declarative content of every object of the listed kinds in the listed namespaces
(spec, data, rules, selected metadata), dropping what the cluster itself changes: status, resourceVersion,
generation, managedFields, timestamps, uids, the Deployment revision annotation and Deployment/StatefulSet
``spec.replicas`` (owned by KEDA/HPA and cloud_lab, not configuration). Helm release records (Secrets of type
``helm.sh/release.v1``) are skipped: an upgrade adds one by design. Secret data is hashed in memory and only
the digest is written; the snapshot belongs in private evidence.

``compare`` diffs two snapshots and requires the differences to equal the reviewed matrix exactly:
every --expect-removed must be gone, every --expect-changed changed, and nothing else added, removed or changed.

usage: python -m ops.llm_agent.object_fingerprints snapshot --context CTX --out before.json
       python -m ops.llm_agent.object_fingerprints compare before.json after.json --report report.json
           [--expect-removed Kind/ns/name ...] [--expect-changed Kind/ns/name ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

NAMESPACES = ("ewallet-mcp", "ewallet-agents", "ewallet-sandbox", "kagent", "feature-store", "fraud-serving")
KINDS = ("deployments", "statefulsets", "services", "configmaps", "secrets", "serviceaccounts", "networkpolicies",
         "poddisruptionbudgets", "authorizationpolicies.security.istio.io", "scaledobjects.keda.sh",
         "remotemcpservers.kagent.dev", "agents.kagent.dev", "sandboxagents.kagent.dev", "workerpools")
VOLATILE_META = ("resourceVersion", "generation", "managedFields", "creationTimestamp", "uid", "selfLink")
VOLATILE_ANNOTATIONS = ("deployment.kubernetes.io/revision", "kubectl.kubernetes.io/last-applied-configuration",
                        "control-plane.alpha.kubernetes.io/leader")

Runner = Callable[[list[str]], str]


def run_command(argv: list[str]) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=120).stdout


def normalize(obj: dict[str, Any]) -> dict[str, Any]:
    meta = {k: v for k, v in obj.get("metadata", {}).items() if k not in VOLATILE_META}
    meta["annotations"] = {k: v for k, v in (meta.get("annotations") or {}).items() if k not in VOLATILE_ANNOTATIONS}
    body = {k: v for k, v in obj.items() if k not in ("metadata", "status")}
    if obj.get("kind") in ("Deployment", "StatefulSet") and isinstance(body.get("spec"), dict):
        body["spec"] = {k: v for k, v in body["spec"].items() if k != "replicas"}
    if obj.get("kind") == "Secret":   # values never leave memory: only their digest is kept
        body["data"] = hashlib.sha256(json.dumps(body.get("data") or {}, sort_keys=True).encode()).hexdigest()
        body.pop("stringData", None)
    return {"metadata": meta, **body}


def fingerprint(obj: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(normalize(obj), sort_keys=True, default=str).encode()).hexdigest()


def snapshot(context: str, run: Runner = run_command, namespaces=NAMESPACES, kinds=KINDS) -> dict[str, Any]:
    objects: dict[str, str] = {}
    skipped: list[str] = []
    for namespace in namespaces:
        for kind in kinds:
            try:
                items = json.loads(run(["kubectl", "--context", context, "--request-timeout=60s", "-n", namespace,
                                        "get", kind, "-o", "json"]))["items"]
            except subprocess.CalledProcessError:
                skipped.append(f"{namespace}/{kind}")   # the kind is not served (recorded, never silent)
                continue
            for obj in items:
                if obj.get("type") == "helm.sh/release.v1":   # Helm's own revision records, not configuration
                    continue
                objects[f"{obj.get('kind')}/{namespace}/{obj['metadata']['name']}"] = fingerprint(obj)
    return {"context": context, "objects": objects, "unserved": skipped}


def compare(before: dict[str, Any], after: dict[str, Any], expect_removed: list[str], expect_changed: list[str]) -> dict[str, Any]:
    b, a = before["objects"], after["objects"]
    removed = sorted(set(b) - set(a))
    added = sorted(set(a) - set(b))
    changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    problems = []
    problems += [f"unexpected removal {k}" for k in removed if k not in expect_removed]
    problems += [f"expected removal did not happen {k}" for k in expect_removed if k not in removed]
    problems += [f"unexpected change {k}" for k in changed if k not in expect_changed]
    problems += [f"expected change did not happen {k}" for k in expect_changed if k not in changed]
    problems += [f"unexpected addition {k}" for k in added]
    if before.get("unserved") != after.get("unserved"):
        problems.append(f"served kinds differ: {before.get('unserved')} vs {after.get('unserved')}")
    kept = len(set(a) & set(b)) - len(changed)
    return {"verdict": "FAIL" if problems else "PASS", "problems": problems, "removed": removed, "added": added,
            "changed": changed, "unchanged_objects": kept, "objects_before": len(b), "objects_after": len(a)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot")
    snap.add_argument("--context", required=True)
    snap.add_argument("--out", type=Path, required=True)
    cmp_ = commands.add_parser("compare")
    cmp_.add_argument("before", type=Path)
    cmp_.add_argument("after", type=Path)
    cmp_.add_argument("--report", type=Path, required=True)
    cmp_.add_argument("--expect-removed", nargs="*", default=[])
    cmp_.add_argument("--expect-changed", nargs="*", default=[])
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        data = snapshot(args.context)
        args.out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"objects={len(data['objects'])} unserved={data['unserved']}")
        return 0
    report = compare(json.loads(args.before.read_text()), json.loads(args.after.read_text()),
                     args.expect_removed, args.expect_changed)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("verdict", "removed", "changed", "added", "unchanged_objects", "problems")}, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
