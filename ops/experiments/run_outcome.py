"""Decide whether a benchmark client's run is a measurement or a failed run.

The O1 and G2 clients exit 1 whenever any request failed, even when every concurrency
level was measured. The comparison keeps failed requests in the counts (an arm that fails
more cannot be IMPROVED), so such a run is data: stopping the session on it would throw
away a valid observation. Historical example: O1 B0' (``VLLM_CPU_OMP_THREADS_BIND=auto``)
lost one level-1 request to the gateway's 160 s backend timeout.

A run is kept only when all of these hold:

* the client exited 0 or 1 (anything else is a crash or a refused argument);
* ``summary.json`` exists and has every requested level;
* every level is ``MEASURED`` (a 429 from the gateway or a resource-guard stop is not);
* every level has at least one successful request.

usage: python -m ops.experiments.run_outcome --summary <raw/run-NN/summary.json> --levels 1,2,4 --exit-code 1
Prints one line and exits 0 (keep) or 1 (failed run).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


def outcome(summary: Mapping[str, Any] | None, levels: Sequence[str], exit_code: int) -> tuple[bool, str]:
    """``(keep, reason)`` for one client run."""

    if exit_code not in (0, 1):
        return False, f"client exit {exit_code}"
    if summary is None:
        return False, f"client exit {exit_code} without summary.json"
    if summary.get("status", "MEASURED") != "MEASURED":
        return False, f"client status {summary.get('status')}"
    found = summary.get("levels") or {}
    missing = [level for level in levels if level not in found]
    if missing:
        return False, f"levels {','.join(missing)} missing from summary.json"
    failed = 0
    total = 0
    for level in levels:
        data = found[level]
        if data.get("status") != "MEASURED":
            return False, f"level {level} {data.get('status')}"
        requests = int(data.get("requests") or 0)
        succeeded = int(data.get("succeeded") or 0)
        if succeeded == 0:
            return False, f"level {level}: no successful request"
        failed += requests - succeeded
        total += requests
    if exit_code == 0 and failed == 0:
        return True, "measured"
    return True, f"measured; {failed} of {total} requests failed and stay in the comparison"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--levels", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        summary = json.loads(args.summary.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        summary = None
    keep, reason = outcome(summary, [item.strip() for item in args.levels.split(",") if item.strip()], args.exit_code)
    print(reason)
    return 0 if keep else 1


if __name__ == "__main__":
    raise SystemExit(main())
