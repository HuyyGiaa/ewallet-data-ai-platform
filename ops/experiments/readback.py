"""Record what a run really ran on, read from the cluster rather than from the request.

The comparison's control fields come from here. If a pod ran a different image from the
one asked for, the recorded value differs and the comparison becomes invalid.

Inputs are saved `kubectl get ... -o json` files and the live gateway configuration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


def _one(values: Sequence[str]) -> str:
    """Join distinct values, so pods that disagree show up as a differing control."""

    return ",".join(sorted(set(values)))


def _flag(args: Sequence[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def _env(container: Mapping[str, Any], name: str) -> str:
    return next((item.get("value", "") for item in container.get("env", []) if item["name"] == name), "")


def controls_from(pods: Mapping[str, Any], nodes: Mapping[str, Any], gateway_config: str) -> dict[str, Any]:
    """Build the control values of one run.

    Args:
        pods: ``kubectl get pods -o json`` for the running vLLM pods.
        nodes: ``kubectl get nodes -o json`` for the benchmark pool.
        gateway_config: The live gateway configuration text.

    Returns:
        Control fields for ``controls.json``.
    """

    items = pods.get("items", [])
    containers = [item["spec"]["containers"][0] for item in items]
    statuses = [status for item in items for status in item.get("status", {}).get("containerStatuses", [])]
    budget = [line.split(":")[1].strip() for line in gateway_config.splitlines() if "maxTokens" in line]
    model_paths = [_flag(item.get("args", []), "--model") or "" for item in containers]
    return {
        "image_digest": _one([status.get("imageID", "").rsplit("@", 1)[-1] for status in statuses]),
        "model_revision": _one([path.rsplit("/", 1)[-1] for path in model_paths]),
        "cpu_request": _one([item["resources"]["requests"]["cpu"] for item in containers]),
        "memory_request": _one([item["resources"]["requests"]["memory"] for item in containers]),
        "node_names": _one([item["metadata"].get("name", "") for item in nodes.get("items", [])]),
        "node_type": _one([item["metadata"]["labels"].get("node.kubernetes.io/instance-type", "")
                           for item in nodes.get("items", [])]),
        "max_num_seqs": _one([_flag(item.get("args", []), "--max-num-seqs") or "" for item in containers]),
        "prefix_cache": _one(["on" if "--enable-prefix-caching" in item.get("args", []) else "off" for item in containers]),
        "thread_bind": _one([_env(item, "VLLM_CPU_OMP_THREADS_BIND") for item in containers]),
        "replicas": len(items),
        "gateway_token_budget": max(budget, key=int, default=""),
    }


def provenance_problems(controls: Mapping[str, Any], digest: str, revision: str) -> list[str]:
    """Return every way the running pods differ from the pinned image and model."""

    problems = []
    if controls.get("image_digest") != digest:
        problems.append(f"image is {controls.get('image_digest')}, expected {digest}")
    if controls.get("model_revision") != revision:
        problems.append(f"model revision is {controls.get('model_revision')}, expected {revision}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Write ``controls.json`` from saved kubectl output, or check an existing one."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pods", type=Path)
    parser.add_argument("--nodes", type=Path)
    parser.add_argument("--gateway-config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", type=Path, help="controls.json to verify against the pins")
    parser.add_argument("--expect-digest", default="")
    parser.add_argument("--expect-revision", default="")
    args = parser.parse_args(argv)
    if args.check:
        controls = json.loads(args.check.read_text(encoding="utf-8"))
        problems = provenance_problems(controls, args.expect_digest, args.expect_revision)
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    if not (args.pods and args.nodes and args.gateway_config and args.output):
        parser.error("--pods, --nodes, --gateway-config and --output are required")
    controls = controls_from(json.loads(args.pods.read_text(encoding="utf-8")),
                             json.loads(args.nodes.read_text(encoding="utf-8")),
                             args.gateway_config.read_text(encoding="utf-8"))
    args.output.write_text(json.dumps(controls, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
