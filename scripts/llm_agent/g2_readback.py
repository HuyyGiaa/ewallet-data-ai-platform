"""G2 serving readback for the fairness gate (g2_compare.py): what actually ran.

From live kubectl JSON (saved by g2_experiment.sh): the vllm-primary Deployment,
its pods, the nodes and the agentgateway ConfigMap. Records the primary
container's image (and each pod's resolved imageID), args, env (Secret refs by
name only, never values), resources, replica count, Ready pods, node class,
the gateway rate limits, and whether every pod started after the restart mark.

usage: python3 scripts/llm_agent/g2_readback.py <dir-with-kubectl-json> <restart-mark-utc> > serving.json
  (<dir> holds deploy.json, pods.json, nodes.json, gateway-configmap.json)
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import yaml


def readback(deploy: dict[str, Any], pods: list[dict[str, Any]], nodes: list[dict[str, Any]],
             gateway_cm: dict[str, Any], restart_mark: str) -> dict[str, Any]:
    container = next(c for c in deploy["spec"]["template"]["spec"]["containers"] if c["name"] == "vllm")
    env = {}
    for e in container.get("env", []):
        ref = (e.get("valueFrom") or {}).get("secretKeyRef")
        env[e["name"]] = f"secret:{ref['name']}/{ref['key']}" if ref else e.get("value")
    node_types = {n["metadata"]["name"]: n["metadata"]["labels"].get("node.kubernetes.io/instance-type") for n in nodes}
    running = [p for p in pods if p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")]
    pod_rows = []
    for p in running:
        status = next((s for s in p["status"].get("containerStatuses", []) if s["name"] == "vllm"), {})
        pod_rows.append({"name": p["metadata"]["name"], "node": p["spec"].get("nodeName"), "ip": p["status"].get("podIP"),
                         "ready": bool(status.get("ready")), "restarts": status.get("restartCount", 0),
                         "image_id": status.get("imageID"), "started": p["status"].get("startTime")})
    llm = yaml.safe_load(gateway_cm["data"]["config.yaml"])["llm"]
    limits = {r["type"]: {k: r[k] for k in ("maxTokens", "tokensPerFill", "fillInterval")}
              for r in llm["policies"]["localRateLimit"]}
    return {
        "image": container["image"], "args": container["args"], "env": env, "resources": container["resources"],
        "replicas": deploy["spec"]["replicas"], "node_selector": deploy["spec"]["template"]["spec"].get("nodeSelector"),
        "pods": sorted(pod_rows, key=lambda r: r["name"]),
        "image_ids": sorted({r["image_id"] for r in pod_rows}),
        "distinct_nodes": len({r["node"] for r in pod_rows}) == len(pod_rows),
        "node_class": sorted({node_types.get(r["node"]) or "unknown" for r in pod_rows}),
        "token_rate": limits.get("tokens"), "request_rate": limits.get("requests"),
        "fresh_pods": bool(pod_rows) and all(r["ready"] and (r["started"] or "") >= restart_mark for r in pod_rows),
        "restart_mark": restart_mark,
    }


def main(argv: list[str]) -> int:
    d = Path(argv[1])
    load = lambda name: json.loads((d / name).read_text())  # noqa: E731
    result = readback(load("deploy.json"), load("pods.json")["items"], load("nodes.json")["items"],
                      load("gateway-configmap.json"), argv[2])
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
