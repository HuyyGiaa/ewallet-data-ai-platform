"""[READ-ONLY] Token-free inventory of the MCP server auth tables and every reference to them.

Collected through kubectl and helm (never written with values): each ``mcp-auth-*`` Secret in the MCP namespaces
with its immutability and credential ids/subjects/statuses (tokens are dropped in memory); the table each MCP
server Deployment reads; every live object that names a table (workloads, scaled-up ReplicaSets, kagent and
agent-sandbox resources); zero-replica ReplicaSets (rollout history); the current Helm release manifests; and
every historical Helm revision. ops/llm_agent/auth_table_matrix.py turns it into the KEEP/DELETE/UNKNOWN matrix.

usage: python -m ops.llm_agent.auth_table_inventory --context CTX --out inventory.json
           [--release llm-agent=default --release llm-agent-mcp=ewallet-mcp] [--skip-history]
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Iterable

NAMESPACES = ("ewallet-mcp", "ewallet-agents")
SERVERS = ("drift-mcp", "feature-rag-mcp")
WORKLOAD_KINDS = ("deployments", "statefulsets", "daemonsets", "replicasets", "jobs", "cronjobs", "pods")
EXTRA_GROUP_HINTS = ("kagent", "sandbox", "substrate")
_TABLE = re.compile(r"(?<![A-Za-z0-9-])mcp-auth-[a-z0-9-]+(?![A-Za-z0-9-])")

Runner = Callable[[list[str]], str]


def run_command(argv: list[str]) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=120).stdout


def table_entry(secret: dict[str, Any]) -> dict[str, Any]:
    """One auth table without its tokens."""

    meta = secret["metadata"]
    entry: dict[str, Any] = {"namespace": meta["namespace"], "name": meta["name"], "immutable": secret.get("immutable") is True,
                             "annotations": {k: v for k, v in (meta.get("annotations") or {}).items()
                                             if not k.startswith("kubectl.kubernetes.io/")},
                             "owner_references": [f"{o.get('kind')}/{o.get('name')}" for o in meta.get("ownerReferences") or []]}
    try:
        raw = json.loads(base64.b64decode((secret.get("data") or {})["auth-config.json"]))
        entry["credentials"] = [{"id": c.get("id"), "subject": c.get("subject"), "status": c.get("status")}
                                for c in raw.get("credentials") or []]
    except (KeyError, ValueError, TypeError):
        entry["credentials"] = None
    return entry


def server_table(deployment: dict[str, Any]) -> str | None:
    for container in deployment["spec"]["template"]["spec"].get("containers", []):
        for env in container.get("env") or []:
            if env.get("name") == "EWALLET_MCP_SERVICE_TOKENS":
                return ((env.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
    return None


def table_names(text: str) -> set[str]:
    return set(_TABLE.findall(text))


def object_references(objects: Iterable[dict[str, Any]], known: set[str]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(live, history) references keyed by '<namespace>/<table>' (Secret references are namespace-local)."""

    live: dict[str, list[str]] = {}
    history: dict[str, list[str]] = {}
    for obj in objects:
        meta = obj.get("metadata", {})
        namespace = meta.get("namespace", "")
        names = table_names(json.dumps(obj.get("spec", {})))
        if not names:
            continue
        label = f"{obj.get('kind')}/{namespace}/{meta.get('name')}"
        zero_rs = obj.get("kind") == "ReplicaSet" and (obj.get("spec") or {}).get("replicas", 1) == 0
        for name in names:
            key = f"{namespace}/{name}"
            if key not in known:
                key = f"{namespace}/{name} (no such table)"
            (history if zero_rs else live).setdefault(key, []).append(label)
    return live, history


def helm_references(text: str, label: str, known: set[str]) -> dict[str, list[str]]:
    """A Helm manifest names tables across namespaces; a name blocks every table that carries it."""

    out: dict[str, list[str]] = {}
    for name in table_names(text):
        for key in sorted(k for k in known if k.endswith(f"/{name}")) or [f"*/{name} (no such table)"]:
            out.setdefault(key, []).append(label)
    return out


def collect(context: str, releases: list[tuple[str, str]], run: Runner = run_command, history: bool = True) -> dict[str, Any]:
    kubectl = ["kubectl", "--context", context, "--request-timeout=60s"]
    helm = ["helm", "--kube-context", context]
    tables = []
    for namespace in NAMESPACES:
        secrets = json.loads(run([*kubectl, "-n", namespace, "get", "secrets", "-o", "json"]))["items"]
        tables += [table_entry(s) for s in secrets if s["metadata"]["name"].startswith("mcp-auth-")]
    known = {f"{t['namespace']}/{t['name']}" for t in tables}
    servers = {}
    for namespace in NAMESPACES:
        for server in SERVERS:
            text = run([*kubectl, "-n", namespace, "get", "deployments", "--field-selector", f"metadata.name={server}", "-o", "json"])
            items = json.loads(text)["items"]
            servers[f"{namespace}/{server}"] = {"present": bool(items), "table": server_table(items[0]) if items else None}
    kinds = list(WORKLOAD_KINDS)
    for resource in run([*kubectl, "api-resources", "--verbs=list", "-o", "name"]).split():
        if any(hint in resource for hint in EXTRA_GROUP_HINTS) and resource not in kinds:
            kinds.append(resource)
    objects: list[dict[str, Any]] = []
    for kind in kinds:
        objects += json.loads(run([*kubectl, "get", kind, "-A", "-o", "json"]))["items"]
    live, rollout_history = object_references(objects, known)
    helm_current: dict[str, list[str]] = {}
    helm_history: dict[str, list[str]] = {}
    revisions: dict[str, list[int]] = {}
    for release, namespace in releases:
        for key, labels in helm_references(run([*helm, "-n", namespace, "get", "manifest", release]), release, known).items():
            helm_current.setdefault(key, []).extend(labels)
        if not history:
            continue
        revs = sorted(r["revision"] for r in json.loads(run([*helm, "-n", namespace, "history", release, "--max", "1000", "-o", "json"])))
        revisions[release] = revs
        for revision in revs:
            text = run([*helm, "-n", namespace, "get", "manifest", release, "--revision", str(revision)])
            for key, labels in helm_references(text, f"{release}@{revision}", known).items():
                helm_history.setdefault(key, []).extend(labels)
    return {"context": context, "tables": tables, "servers": servers, "scanned_kinds": kinds,
            "objects_scanned": len(objects), "helm_revisions": revisions,
            "references": {"live": live, "history": rollout_history, "helm_current": helm_current,
                           "helm_history": helm_history}}


def _release(value: str) -> tuple[str, str]:
    name, _, namespace = value.partition("=")
    if not name or not namespace:
        raise argparse.ArgumentTypeError("--release must be <name>=<namespace>")
    return name, namespace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--context", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--release", type=_release, action="append",
                        default=None, help="Helm release whose current and historical manifests are scanned")
    parser.add_argument("--skip-history", action="store_true", help="scan only the current Helm manifests")
    args = parser.parse_args(argv)
    releases = args.release or [("llm-agent", "default"), ("llm-agent-mcp", "ewallet-mcp")]
    inventory = collect(args.context, releases, history=not args.skip_history)
    args.out.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"tables={len(inventory['tables'])} objects_scanned={inventory['objects_scanned']} "
          f"kinds={len(inventory['scanned_kinds'])} out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
