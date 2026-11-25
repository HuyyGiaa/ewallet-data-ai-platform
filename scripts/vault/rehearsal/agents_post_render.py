#!/usr/bin/env python3
"""Helm post-renderer for the kind-only rehearsal release of infra/helm/llm-agent-platform (Vault V2, K6/K7).

Keeps the chart unchanged and makes its render safe for the temporary namespace: refuses any object
outside REHEARSAL_NAMESPACE (and any cluster-scoped object), refuses a rendered Secret (Helm must never
hold credential material), and labels every object ewallet.io/vault-rehearsal=<REHEARSAL_ID>.
"""

from __future__ import annotations

import os
import sys

import yaml

LABEL = "ewallet.io/vault-rehearsal"


def post_render(documents: list[dict], namespace: str, rehearsal_id: str) -> list[dict]:
    for doc in documents:
        meta = doc.get("metadata", {})
        if meta.get("namespace") != namespace:
            raise SystemExit(f"refusing {doc.get('kind')}/{meta.get('name')} outside {namespace} (namespace {meta.get('namespace')})")
        if doc.get("kind") == "Secret":
            raise SystemExit(f"refusing Secret/{meta.get('name')}: credentials are never rendered by Helm")
        meta.setdefault("labels", {})[LABEL] = rehearsal_id
    if {("Deployment", "agentgateway"), ("Deployment", "vllm-primary")} - {(d["kind"], d["metadata"]["name"]) for d in documents}:
        raise SystemExit("agentgateway and vllm-primary must both be rendered")
    return documents


def main() -> int:
    namespace, rehearsal_id = os.environ.get("REHEARSAL_NAMESPACE", ""), os.environ.get("REHEARSAL_ID", "")
    if not namespace or not rehearsal_id:
        print("REHEARSAL_NAMESPACE and REHEARSAL_ID are required", file=sys.stderr)
        return 2
    documents = [d for d in yaml.safe_load_all(sys.stdin) if d]
    yaml.safe_dump_all(post_render(documents, namespace, rehearsal_id), sys.stdout, sort_keys=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
