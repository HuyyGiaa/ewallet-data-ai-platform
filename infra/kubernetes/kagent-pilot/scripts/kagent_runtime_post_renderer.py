#!/usr/bin/env python3
"""Keep the bounded Substrate pilot isolated from production Agent secrets.

The upstream kagent chart creates ate-api env-source RBAC in every watched
namespace when Substrate is enabled.  The pilot needs this access only in the
dedicated ``kagent-pilot`` namespace.  The live kagent PostgreSQL deployment also
has an approved scheduling selector that was added by the original release
post-renderer and is not represented by the upstream chart template.

This renderer therefore makes exactly two narrowly scoped transformations and
fails closed when the expected resources are absent or unexpectedly shaped.
"""

from __future__ import annotations

import sys

import yaml


PILOT_NAMESPACE = "kagent-pilot"
ENV_SOURCE_RBAC_NAME = "kagent-ate-api-env-sources"
POSTGRES_NAME = "kagent-postgresql"
WORKER_POOL_NAME = "kagent-pilot"
G3_NODE_SELECTOR = {"ewallet.io/g3-llm": "true"}


def _identity(document: dict) -> tuple[str, str, str]:
    metadata = document.get("metadata") or {}
    return (
        str(document.get("kind", "")),
        str(metadata.get("namespace", "")),
        str(metadata.get("name", "")),
    )


def transform(documents: list[dict]) -> list[dict]:
    output: list[dict] = []
    retained_env_source_kinds: set[str] = set()
    removed_env_source_identities: set[tuple[str, str, str]] = set()
    postgres_seen = False
    worker_pool_seen = False

    for document in documents:
        kind, namespace, name = _identity(document)
        if name == ENV_SOURCE_RBAC_NAME and kind in {"Role", "RoleBinding"}:
            if namespace == PILOT_NAMESPACE:
                retained_env_source_kinds.add(kind)
                output.append(document)
            else:
                removed_env_source_identities.add((kind, namespace, name))
            continue

        if kind == "WorkerPool" and name == WORKER_POOL_NAME:
            if namespace != "kagent":
                raise ValueError(
                    f"expected chart-rendered WorkerPool in kagent, got {namespace!r}"
                )
            document.setdefault("metadata", {})["namespace"] = PILOT_NAMESPACE
            worker_pool_seen = True

        if kind == "Deployment" and namespace == "kagent" and name == POSTGRES_NAME:
            pod_spec = document.setdefault("spec", {}).setdefault("template", {}).setdefault(
                "spec", {}
            )
            existing = pod_spec.get("nodeSelector") or {}
            conflicting = {
                key: value
                for key, value in G3_NODE_SELECTOR.items()
                if key in existing and str(existing[key]) != value
            }
            if conflicting:
                raise ValueError(
                    f"refusing to replace conflicting PostgreSQL nodeSelector: {conflicting}"
                )
            pod_spec["nodeSelector"] = {**existing, **G3_NODE_SELECTOR}
            postgres_seen = True

        output.append(document)

    if retained_env_source_kinds != {"Role", "RoleBinding"}:
        raise ValueError(
            "expected exactly the pilot env-source Role and RoleBinding in namespace kagent-pilot"
        )
    expected_removed = {
        ("Role", "ewallet-agents", ENV_SOURCE_RBAC_NAME),
        ("RoleBinding", "ewallet-agents", ENV_SOURCE_RBAC_NAME),
        ("Role", "kagent", ENV_SOURCE_RBAC_NAME),
        ("RoleBinding", "kagent", ENV_SOURCE_RBAC_NAME),
    }
    if removed_env_source_identities != expected_removed:
        raise ValueError(
            "unexpected cross-namespace env-source RBAC set: "
            f"{sorted(removed_env_source_identities)}"
        )
    if not postgres_seen:
        raise ValueError("expected kagent PostgreSQL Deployment was not rendered")
    if not worker_pool_seen:
        raise ValueError("expected kagent pilot WorkerPool was not rendered")
    return output


def main() -> int:
    try:
        documents = [document for document in yaml.safe_load_all(sys.stdin.read()) if document]
        rendered = transform(documents)
        yaml.safe_dump_all(rendered, sys.stdout, sort_keys=False, explicit_start=True)
    except Exception as exc:  # noqa: BLE001 - fail-closed CLI boundary
        print(f"kagent pilot post-render failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
