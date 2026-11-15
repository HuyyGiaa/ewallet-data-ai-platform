"""Overlay generation for agent-platform packages that cannot be static values files.

``crossNamespace.flows`` is a list and Helm replaces lists, so a static overlay with one new
flow would delete every live flow (16 policies in the AGI profile). ``append-flow`` writes an
overlay that keeps the live list unchanged and appends exactly one reviewed flow.

    python -m ops.llm_agent.agi_packages append-flow sandbox-mcp-gateway --values <live values> --out <overlay>

``merge-values`` merges values files with Helm's rule (maps merge, everything else replaces),
for tools that take one values file, such as g3_mcp_credentials --values.

    python -m ops.llm_agent.agi_packages merge-values --out <merged> <live values> <overlay>...
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

# PKG-S1: sandbox workers (WorkerPool business-agents, namespace default ServiceAccount, the
# only identity Substrate 0.0.9 gives them) may reach the MCP Gateway on its port. ztunnel
# admits the exact SPIFFE principal; the MCP backend still applies the token's tool grants.
FLOWS: dict[str, dict[str, Any]] = {
    "sandbox-mcp-gateway": {
        "name": "sandbox-workers-to-mcp-gateway",
        "direction": "ingress",
        "local": "mcp-gateway",
        "port": 8080,
        "peer": {"namespace": "ewallet-sandbox", "podLabels": {"ate.dev/worker-pool": "business-agents"},
                 "ambient": True, "serviceAccount": "default"},
    },
    # PKG-T1: the same workers export OTLP to the shared collector, so the Sandbox Specialist's own
    # spans join the business trace (AGI-12). Port 4317 only; the collector's other ports stay closed.
    "sandbox-otel-collector": {
        "name": "sandbox-workers-to-otel-collector",
        "direction": "ingress",
        "local": "otel-collector",
        "port": 4317,
        "peer": {"namespace": "ewallet-sandbox", "podLabels": {"ate.dev/worker-pool": "business-agents"},
                 "ambient": True, "serviceAccount": "default"},
    },
}


def append_flow(values: Mapping[str, Any], flow_id: str) -> dict[str, Any]:
    flow = FLOWS[flow_id]
    live = list(((values.get("crossNamespace") or {}).get("flows")) or [])
    for existing in live:
        if existing.get("name") == flow["name"]:
            if existing == flow:
                raise ValueError(f"flow {flow['name']} is already live; nothing to change")
            raise ValueError(f"a different flow named {flow['name']} is live; refusing to replace it")
    return {"crossNamespace": {"flows": [copy.deepcopy(item) for item in live] + [copy.deepcopy(flow)]}}


def merge(base: Any, overlay: Any) -> Any:
    """Helm's coalescing: nested maps merge key by key; lists and scalars replace."""
    if isinstance(base, Mapping) and isinstance(overlay, Mapping):
        merged = {key: copy.deepcopy(value) for key, value in base.items()}
        for key, value in overlay.items():
            merged[key] = merge(merged[key], value) if key in merged else copy.deepcopy(value)
        return merged
    return copy.deepcopy(overlay)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("append-flow")
    add.add_argument("flow", choices=sorted(FLOWS))
    add.add_argument("--values", type=Path, required=True, help="live release values (helm get values -o yaml)")
    add.add_argument("--out", type=Path, required=True)
    combine = sub.add_parser("merge-values")
    combine.add_argument("--out", type=Path, required=True)
    combine.add_argument("files", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.command == "merge-values":
        merged: Any = {}
        for path in args.files:
            merged = merge(merged, yaml.safe_load(path.read_text()) or {})
        args.out.write_text(yaml.safe_dump(merged, sort_keys=False))
        print(f"{args.out}: merged {len(args.files)} files")
        return 0
    try:
        overlay = append_flow(yaml.safe_load(args.values.read_text()) or {}, args.flow)
    except ValueError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    args.out.write_text(yaml.safe_dump(overlay, sort_keys=False))
    print(f"{args.out}: {len(overlay['crossNamespace']['flows'])} flows (live + {FLOWS[args.flow]['name']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
