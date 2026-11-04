"""Shared state of the offline G2 cluster simulation (fake kubectl/gcloud/helm, mock servers).

One JSON file ($G2_SIM_STATE) behind an fcntl lock: nodes, releases, pods, the
rendered agentgateway ConfigMap, per-pod vLLM counters and EPP decisions.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
from typing import Any, Iterator

MLOPS_NODES = ["sim-mlops-a", "sim-mlops-b"]


def path() -> Path:
    return Path(os.environ["G2_SIM_STATE"])


def initial() -> dict[str, Any]:
    return {"nodes": {n: {} for n in MLOPS_NODES}, "pool": 2, "releases": [], "pods": {}, "configmap": None,
            "routing": None, "counters": {}, "epp": [], "serial": 0, "calls": []}


@contextlib.contextmanager
def locked() -> Iterator[dict[str, Any]]:
    p = path()
    with open(str(p) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(p.read_text()) if p.exists() else initial()
        yield state
        p.write_text(json.dumps(state))


def read() -> dict[str, Any]:
    with locked() as state:
        return state


def fail_at(point: str) -> bool:
    return point in os.environ.get("G2_SIM_FAIL", "").split(",")


def new_pod(state: dict[str, Any], prefix: str, node: str, labels: dict[str, str], image: str) -> str:
    state["serial"] += 1
    name = f"{prefix}-{state['serial']:04d}"
    state["pods"][name] = {"node": node, "labels": labels, "image": image, "ip": f"10.9.0.{state['serial'] % 250}",
                           "started": _now()}
    return name


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
