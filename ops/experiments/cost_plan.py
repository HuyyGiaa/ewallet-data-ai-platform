"""Worst-case cost and time plan for one ewallet-bench session.

The session refuses to start when the worst case exceeds the owner's ceilings. Worst case
means every node stays up for the whole time limit, plus teardown, plus the watchdog's
grace period and the autoscaler's scale-down delay in case the operator's machine is lost.

Prices are list prices for asia-southeast1 read on 2026-11-09 (on-demand e2-standard-4
about USD 0.1653/h; pd-balanced about USD 0.11 per GB-month; an in-use external IPv4
about USD 0.005/h). They are an estimate, not a billing readback, and a 25 % margin is
added for price drift and rounding.

usage: python -m ops.experiments.cost_plan --nodes 2 --max-minutes 210 [--max-usd 5] [--max-window-minutes 240]
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Sequence

NODE_USD_PER_HOUR = 0.1653         # e2-standard-4, asia-southeast1, on-demand
DISK_GB = 50
DISK_USD_PER_GB_MONTH = 0.11       # pd-balanced, asia-southeast1
IP_USD_PER_HOUR = 0.005            # in-use external IPv4 per node
HOURS_PER_MONTH = 730
MARGIN = 1.25
SETUP_MINUTES = 10                 # pool creation before the session clock starts
TEARDOWN_MINUTES = 15              # namespace and pool deletion after it stops
WATCHDOG_GRACE_MINUTES = 10        # watchdog deadline = session limit + this
SCALE_DOWN_MINUTES = 15            # autoscaler: 10 min unneeded time plus node drain


def node_hour_usd() -> float:
    """Cost of one benchmark node for one hour, compute plus disk plus IP."""

    return NODE_USD_PER_HOUR + DISK_GB * DISK_USD_PER_GB_MONTH / HOURS_PER_MONTH + IP_USD_PER_HOUR


def plan(nodes: int, max_minutes: int, *, max_usd: float, max_window_minutes: int) -> dict[str, Any]:
    """Return the plan and whether it fits both ceilings."""

    window = SETUP_MINUTES + max_minutes + TEARDOWN_MINUTES
    host_loss = SETUP_MINUTES + max_minutes + WATCHDOG_GRACE_MINUTES + SCALE_DOWN_MINUTES
    billable = max(window, host_loss)
    usd = nodes * billable / 60 * node_hour_usd() * MARGIN
    problems = []
    if nodes > 2:
        problems.append(f"{nodes} nodes exceeds the 2-node ceiling")
    if window > max_window_minutes:
        problems.append(f"session window {window} min exceeds {max_window_minutes} min")
    if usd > max_usd:
        problems.append(f"worst-case USD {usd:.2f} exceeds USD {max_usd:.2f}")
    return {"nodes": nodes, "machine_type": "e2-standard-4", "max_minutes": max_minutes,
            "session_window_minutes": window, "host_loss_billable_minutes": host_loss,
            "node_hour_usd": round(node_hour_usd(), 4), "margin": MARGIN,
            "worst_case_usd": round(usd, 2), "ceiling_usd": max_usd, "ceiling_window_minutes": max_window_minutes,
            "fits": not problems, "problems": problems,
            "price_source": "asia-southeast1 list prices read 2026-11-09; estimate, not a billing readback"}


def main(argv: Sequence[str] | None = None) -> int:
    """Print the plan as JSON; exit 1 when it does not fit."""

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--nodes", type=int, required=True)
    parser.add_argument("--max-minutes", type=int, required=True)
    parser.add_argument("--max-usd", type=float, default=5.0)
    parser.add_argument("--max-window-minutes", type=int, default=240)
    args = parser.parse_args(argv)
    result = plan(args.nodes, args.max_minutes, max_usd=args.max_usd, max_window_minutes=args.max_window_minutes)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["fits"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
