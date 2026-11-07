"""Guard rails for the R2 Terraform change on the dev stack.

The dev state still records the GKE node pool with autoscaling 0-3, while the live pool
was resized by ``cloud_lab.py`` with autoscaling off, and the cluster's authorized
networks and network policy have drifted too. A plain ``terraform apply`` therefore
replaces the node pool. Even ``-target`` on a Workload Identity binding pulls the GKE
module in, because ``module.workload_identity`` declares ``depends_on = [module.gke]``.

Two commands keep the R2 change narrow:

- ``tfvars``: write the variables the state was applied with (GKE enabled, its
  authorized CIDRs, node-pool bounds) to a private file, printing only variable names,
  so ``terraform import`` resolves the Workload Identity addresses.
- ``guard``: read ``terraform show -json <plan>`` and fail unless the plan does exactly
  the approved creates and nothing else.

Usage::

    python3 -m ops.gcp.r2_terraform tfvars --state state.json --out r2.auto.tfvars
    terraform show -json r2.tfplan > r2.plan.json
    python3 -m ops.gcp.r2_terraform guard --plan r2.plan.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

APPROVED_CREATES = frozenset({
    'module.iam.google_service_account.workload["fraud_api"]',
    'module.iam.google_service_account.workload["feature_api"]',
    'module.iam.google_storage_bucket_iam_member.registry_reader["fraud_api"]',
    'module.iam.google_storage_bucket_iam_member.registry_reader["feature_api"]',
})
REGISTRY_CONDITION = "/objects/features/registries/"


def _resource(state: dict[str, Any], kind: str) -> dict[str, Any]:
    matches = [r for r in state.get("resources", []) if r.get("type") == kind and r.get("mode") == "managed"]
    if len(matches) != 1 or len(matches[0].get("instances", [])) != 1:
        raise ValueError(f"expected exactly one {kind} in the state")
    return matches[0]["instances"][0]["attributes"]


def tfvars(state: dict[str, Any]) -> str:
    """Return HCL variables that reproduce the applied GKE settings of the state."""

    cluster = _resource(state, "google_container_cluster")
    pool = _resource(state, "google_container_node_pool")
    cidrs = [
        block["cidr_block"]
        for config in cluster.get("master_authorized_networks_config") or []
        for block in config.get("cidr_blocks", [])
    ]
    scaling = (pool.get("autoscaling") or [{}])[0]
    return "\n".join([
        "enable_gke = true",
        "enable_jenkins_vm = false",
        "enable_in_cluster_cd = false",
        f"gke_deletion_protection = {str(bool(cluster.get('deletion_protection'))).lower()}",
        f"gke_master_authorized_cidrs = {json.dumps(cidrs)}",
        f"gke_min_nodes = {int(scaling.get('min_node_count', 0))}",
        f"gke_max_nodes = {int(scaling.get('max_node_count', 3))}",
        f'gke_machine_type = "{pool["node_config"][0]["machine_type"]}"',
        "",
    ])


def guard(plan: dict[str, Any]) -> list[str]:
    """Return every problem with ``plan``; an empty list means it is exactly the R2 change."""

    problems = []
    created = set()
    for change in plan.get("resource_changes", []):
        actions = change["change"]["actions"]
        address = change["address"]
        if actions in (["no-op"], ["read"]):
            continue
        if actions != ["create"] or address not in APPROVED_CREATES:
            problems.append(f"{'+'.join(actions)} {address} is outside the approved R2 change")
            continue
        created.add(address)
        after = change["change"].get("after") or {}
        if "registry_reader" in address:
            condition = (after.get("condition") or [{}])[0].get("expression", "")
            if after.get("role") != "roles/storage.objectViewer" or REGISTRY_CONDITION not in condition:
                problems.append(f"{address} is not a registry-only objectViewer")
    for missing in sorted(APPROVED_CREATES - created):
        problems.append(f"{missing} is missing from the plan")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Run ``tfvars`` or ``guard``."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("tfvars")
    make.add_argument("--state", type=Path, required=True)
    make.add_argument("--out", type=Path, required=True)
    check = sub.add_parser("guard")
    check.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "tfvars":
        text = tfvars(json.loads(args.state.read_text(encoding="utf-8")))
        descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        names = [line.split(" = ")[0] for line in text.splitlines() if line]
        print(f"wrote {args.out} with: {', '.join(names)}")
        return 0
    problems = guard(json.loads(args.plan.read_text(encoding="utf-8")))
    for problem in problems:
        print(f"STOP: {problem}", file=sys.stderr)
    if not problems:
        print(f"plan is exactly the approved R2 change: {len(APPROVED_CREATES)} creates")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
