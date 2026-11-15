"""Typed MCP request loop through the MCP Gateway, for rolling-update and failover evidence (AGR-02/03).

Each iteration makes one real tools/call with the same classified MCP client as agi_probe
(initialize, tools/list, tools/call) and prints one ``LOAD_ROW`` JSON line: wall time, tool,
outcome (ok / rejected / unavailable / timeout / error / unauthenticated / forbidden /
protocol_error / server_error / transport_error / client_timeout), latency and HTTP statuses.
Read-only tools only. Tokens come from the environment, never from arguments:

    EWALLET_MCP_TOKEN_FEATURE_RAG  (feature_lookup on /feature-rag/mcp)
    EWALLET_MCP_TOKEN_DRIFT        (drift_detection on /drift/mcp)

    python scripts/llm_agent/mcp_typed_loop.py --gateway http://127.0.0.1:18082 \\
        --transaction inputs/transaction.json --baseline <id> --duration 300 --interval 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from typing import Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
FEATURES = ["account_behavior__account_tx_count_1h", "account_behavior__account_tx_count_24h",
            "user_behavior__user_avg_amount_30d", "merchant_behavior__merchant_tx_count_24h"]
Observe = Callable[[str, str, str, dict, float], Awaitable[dict]]


def calls(gateway: str, transaction: dict, baseline: str) -> list[tuple[str, str, str, dict]]:
    """(endpoint, token env, tool, arguments) for the two read-only typed calls."""
    base = gateway.rstrip("/")
    return [
        (f"{base}/feature-rag/mcp", "EWALLET_MCP_TOKEN_FEATURE_RAG", "feature_lookup",
         {"transaction": transaction, "feature_names": FEATURES}),
        (f"{base}/drift/mcp", "EWALLET_MCP_TOKEN_DRIFT", "drift_detection",
         {"feature_name": "account_behavior__account_tx_count_1h", "baseline_id": baseline,
          "current_window": {"start": "2026-05-01T00:00:00Z", "end": "2026-05-31T00:00:00Z"}}),
    ]


async def run(plan: list[tuple[str, str, str, dict]], observe: Observe, *, duration: float, interval: float,
              timeout: float, emit: Callable[[str], None], clock=time.time, sleep=asyncio.sleep) -> dict:
    tokens = {env: os.environ.get(env, "") for _, env, _, _ in plan}
    missing = sorted(env for env, value in tokens.items() if not value)
    if missing:
        raise SystemExit(f"missing token environment: {missing}")
    deadline = clock() + duration
    counts: dict[str, int] = {}
    while clock() < deadline:
        for endpoint, env, tool, arguments in plan:
            started = clock()
            report = await observe(endpoint, tokens[env], tool, arguments, timeout)
            row = {"t": round(started, 3), "tool": tool, "outcome": report.get("outcome"),
                   "latency_ms": report.get("latency_ms"), "e2e_ms": round((clock() - started) * 1000, 1),
                   "http_statuses": report.get("http_statuses"), "tool_error_code": report.get("tool_error_code")}
            counts[f"{tool}:{row['outcome']}"] = counts.get(f"{tool}:{row['outcome']}", 0) + 1
            emit("LOAD_ROW " + json.dumps(row, sort_keys=True))
        await sleep(interval)
    summary = {"counts": counts, "non_ok": sum(v for k, v in counts.items() if not k.endswith(":ok"))}
    emit("SUMMARY " + json.dumps(summary, sort_keys=True))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gateway", required=True, help="local port-forward to mcp-gateway, http://127.0.0.1:<port>")
    parser.add_argument("--transaction", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    if not args.gateway.startswith("http://127.0.0.1:"):
        parser.error("local port-forwards only")
    if not (1 <= args.duration <= 1800 and 0.2 <= args.interval <= 60):
        parser.error("bounded: duration 1-1800 s, interval 0.2-60 s")
    from agi_probe import observe  # the same classified MCP client as the AGI probes
    plan = calls(args.gateway, json.loads(Path(args.transaction).read_text()), args.baseline)
    summary = asyncio.run(run(plan, observe, duration=args.duration, interval=args.interval, timeout=args.timeout,
                              emit=lambda line: print(line, flush=True)))
    return 0 if summary["non_ok"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
