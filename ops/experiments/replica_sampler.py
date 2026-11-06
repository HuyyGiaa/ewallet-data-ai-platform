"""Record ready replicas and pending pods of one Deployment until stopped.

Timestamps are epoch seconds, matching the load rows. A failed read is a row with
``collect_error``, and later analysis skips it without losing the gap.
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


def main(argv: Sequence[str] | None = None) -> int:
    """Append one sample per interval until the process is stopped."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    while True:
        row: dict[str, object] = {"ts": time.time()}
        try:
            row["ready"] = read_ready(args.context, args.namespace, args.deployment)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            row["collect_error"] = type(error).__name__
        with args.output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
