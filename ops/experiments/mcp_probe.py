"""Call a read-only MCP tool at a fixed rate through the production retry code.

Runs inside a Job in the isolated experiment namespace, from the Specialist's image, so
``with_failover``, ``is_recoverable`` and ``policy_from_env`` are the code production runs.
Each call prints one ``LOAD_ROW``.

A call counts as a success when the server answers it, even when the tool reports that its
backend is unavailable: the experiment measures whether a live replica was reached, and no
backend exists in that namespace. Authentication failures still count as failures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from typing import Awaitable, Callable, Mapping

import httpx

from ai_platform.llm_agent.mcp_failover import FailoverPolicy, is_recoverable, policy_from_env, with_failover

TOOL = "drift_detection"
TOKEN_HEADER = "X-Ewallet-MCP-Service-Token"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
# tools/list needs no backend; the retry decision still names the read-only tool it stands for.
PAYLOAD = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
Send = Callable[[], Awaitable[int]]


def http_sender(client: httpx.AsyncClient, url: str, token: str) -> Send:
    """Return a sender that raises on any HTTP failure, as the MCP client does."""

    async def send() -> int:
        response = await client.post(url, json=PAYLOAD, headers={**HEADERS, TOKEN_HEADER: token})
        response.raise_for_status()
        return response.status_code

    return send


async def one_call(send: Send, policy: FailoverPolicy, index: int) -> dict[str, object]:
    """Make one call and describe it; a failure is a row, not an exception."""

    retries = 0

    def counted(_attempt: int) -> None:
        nonlocal retries
        retries += 1

    started = time.time()
    row: dict[str, object] = {"id": f"call-{index:05d}", "started": started, "state": "failed", "error": None}
    try:
        await with_failover(send, tools=[TOOL], is_transient=is_recoverable, policy=policy, on_retry=counted)
        row["state"] = "completed"
    except Exception as error:  # noqa: BLE001 - every failure is the measurement
        row["error"] = type(error).__name__
    row["finished"] = time.time()
    row["e2e_ms"] = 1000 * (row["finished"] - started)
    row["retries"] = retries
    row["ok"] = row["state"] == "completed"
    return row


async def run(send: Send, policy: FailoverPolicy, *, duration: float, rate: float,
              emit: Callable[[str], None] = print) -> int:
    """Issue calls at a fixed rate until the duration ends; return the number made."""

    deadline = time.monotonic() + duration
    tasks: list[asyncio.Task[None]] = []

    async def call(index: int) -> None:
        emit("LOAD_ROW " + json.dumps(await one_call(send, policy, index), sort_keys=True))

    index = 0
    while time.monotonic() < deadline:
        index += 1
        tasks.append(asyncio.create_task(call(index)))
        await asyncio.sleep(1 / rate)
    await asyncio.gather(*tasks)
    return index


async def amain(url: str, token: str, environment: Mapping[str, str], duration: float, rate: float) -> int:
    """Run against a real server."""

    async with httpx.AsyncClient(timeout=5.0) as client:
        return await run(http_sender(client, url, token), policy_from_env(environment), duration=duration, rate=rate)


def main() -> int:
    """Parse arguments and run the probe."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--duration", type=float, default=120)
    parser.add_argument("--rate", type=float, default=2)
    args = parser.parse_args()
    made = asyncio.run(amain(args.url, os.environ["MCP_EXP_TOKEN"], os.environ, args.duration, args.rate))
    print(f"LOAD_DONE {made}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
