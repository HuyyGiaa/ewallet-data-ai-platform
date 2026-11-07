"""Record ready replicas of one Deployment, and optionally its HPA, until stopped.

Timestamps are epoch seconds, matching the load rows. A failed read is a row with
``collect_error``, and later analysis skips it without losing the gap. With ``--hpa`` each
row also holds the HPA's current and desired replicas and the metric values it acted on.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Sequence


def read_ready(context: str, namespace: str, deployment: str) -> int:
    """Return the ready replica count."""

    output = subprocess.run(
        ["kubectl", "--context", context, "--request-timeout=20s", "-n", namespace, "get", "deployment", deployment,
         "-o", "jsonpath={.status.readyReplicas}"], capture_output=True, text=True, check=True, timeout=30).stdout
    return int(output or 0)


def read_hpa(context: str, namespace: str, name: str) -> dict[str, object]:
    """Return the HPA's replica decision and the metric values it saw."""

    output = subprocess.run(
        ["kubectl", "--context", context, "--request-timeout=20s", "-n", namespace, "get", "hpa", name, "-o", "json"],
        capture_output=True, text=True, check=True, timeout=30).stdout
    status = json.loads(output).get("status", {})
    metrics = {}
    for metric in status.get("currentMetrics") or []:
        external = metric.get("external") or {}
        current = external.get("current") or {}
        name_ = (external.get("metric") or {}).get("name", "metric")
        metrics[name_] = current.get("averageValue") or current.get("value")
    return {"current": status.get("currentReplicas"), "desired": status.get("desiredReplicas"), "metrics": metrics}


def main(argv: Sequence[str] | None = None) -> int:
    """Append one sample per interval until the process is stopped."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--hpa", help="also record this HPA's decision")
    args = parser.parse_args(argv)
    while True:
        row: dict[str, object] = {"ts": time.time()}
        try:
            row["ready"] = read_ready(args.context, args.namespace, args.deployment)
            if args.hpa:
                row["hpa"] = read_hpa(args.context, args.namespace, args.hpa)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            row["collect_error"] = type(error).__name__
        with args.output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
