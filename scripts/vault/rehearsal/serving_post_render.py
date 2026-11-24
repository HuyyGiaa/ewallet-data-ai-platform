#!/usr/bin/env python3
"""Helm post-renderer for the rehearsal release of infra/helm/ewallet-serving (Campaign A+).

Keeps the chart unchanged and makes its render safe for the temporary namespace:
- drops the additive NetworkPolicy the chart always places in `feature-store` (the
  production release owns it; the rehearsal Redis lives in the rehearsal namespace);
- refuses any other object outside REHEARSAL_NAMESPACE, and any cluster-scoped object;
- mounts the synthetic Feast registry (ConfigMap `feast-registry`) at /registry, where
  `featureApi.registryUri` points;
- labels every object ewallet.io/vault-rehearsal=<REHEARSAL_ID>.

Helm passes the rendered manifests on stdin and applies what this prints. It also stores
the post-rendered manifest, so `helm rollback` re-applies it without running this again.
"""

from __future__ import annotations

import os
import sys

import yaml

LABEL = "ewallet.io/vault-rehearsal"
DROPPED = ("NetworkPolicy", "feature-store", "redis-clients-feature-api")


def post_render(documents: list[dict], namespace: str, rehearsal_id: str) -> list[dict]:
    out = []
    for doc in documents:
        meta = doc.get("metadata", {})
        key = (doc.get("kind"), meta.get("namespace"), meta.get("name"))
        if key == DROPPED:
            continue
        if meta.get("namespace") != namespace:
            raise SystemExit(f"refusing {key[0]}/{key[2]} outside {namespace} (namespace {key[1]})")
        meta.setdefault("labels", {})[LABEL] = rehearsal_id
        if doc["kind"] == "Deployment" and meta["name"] == "feature-api":
            pod = doc["spec"]["template"]["spec"]
            pod.setdefault("volumes", []).append({"name": "registry", "configMap": {"name": "feast-registry"}})
            pod["containers"][0].setdefault("volumeMounts", []).append(
                {"name": "registry", "mountPath": "/registry", "readOnly": True})
        out.append(doc)
    if not any(d["kind"] == "Deployment" for d in out):
        raise SystemExit("no Feature API Deployment rendered: is featureApi.enabled set?")
    return out


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
