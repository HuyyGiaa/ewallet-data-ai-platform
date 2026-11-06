"""Exercise a released workload from inside the CD namespace.

Runs in a Job built from the same image digest as the workload. Uses only the standard
library, so one file serves every image, and prints one ``CD_SMOKE_SUMMARY`` line. This
script only reports: ``scripts/ci/bounded_cd.py`` compares the summary with the contract
and decides the verdict. It exits non-zero only when a check could not run.

Each mode proves real behaviour rather than a process start:

* ``mcp``: an unauthenticated tool request is refused, an authenticated MCP
  ``tools/list`` returns the expected tool, and the service reports the release SHA.
* ``agent``: both Agent Cards are served and a malformed A2A task is rejected rather
  than executed.
* ``streaming``: the released image runs Block A's event semantics on a deterministic
  scenario. It proves the image and its semantics; it does not start a Flink cluster.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Mapping


TIMEOUT_SECONDS = 20
TOKEN_HEADER = "X-Ewallet-MCP-Service-Token"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}


def http(url: str, *, body: Any = None, headers: Mapping[str, str] | None = None) -> tuple[int, str]:
    """Send one request and return its status and text, including error responses."""

    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


def check_mcp(base: str, token: str, expected_tool: str) -> dict[str, object]:
    """Check authentication and tool discovery on a running MCP server."""

    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    ready, _ = http(f"{base}/health/ready")
    anonymous, _ = http(f"{base}/mcp", body=payload, headers=MCP_HEADERS)
    status, text = http(f"{base}/mcp", body=payload, headers={**MCP_HEADERS, TOKEN_HEADER: token})
    tools: list[str] = []
    if status == 200:
        tools = sorted(item["name"] for item in json.loads(text)["result"]["tools"])
    return {
        "ready_status": ready,
        "anonymous_status": anonymous,
        "tools_list_status": status,
        "tool_found": expected_tool in tools,
    }


def check_agent(base: str, expected_name: str) -> dict[str, object]:
    """Check an Agent Card and that a malformed task does not complete."""

    status, text = http(f"{base}/.well-known/agent-card.json")
    card = json.loads(text) if status == 200 else {}
    payload = {
        "jsonrpc": "2.0", "id": "cd-smoke", "method": "message/send",
        "params": {"message": {"role": "user", "messageId": "cd-smoke-message",
                               "parts": [{"kind": "text", "text": "CALL_AGENT=not-a-request"}]}},
    }
    rpc_status, rpc_text = http(f"{base}/", body=payload, headers={"Content-Type": "application/json"})
    state = None
    if rpc_status == 200:
        state = (json.loads(rpc_text).get("result") or {}).get("status", {}).get("state")
    return {
        "card_status": status,
        "card_name_matches": card.get("name") == expected_name,
        "malformed_task_rejected": state in {"rejected", "failed"},
    }


def check_streaming() -> dict[str, object]:
    """Run the deterministic Block A processor shipped in the image."""

    from datetime import datetime, timezone

    from data_platform.streaming.config.settings import BASELINE
    from data_platform.streaming.flink.job import build_job_plan
    from data_platform.streaming.flink.processor import BlockAProcessor
    from data_platform.streaming.generator.scenarios import DeterministicScenarioGenerator

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    generator = DeterministicScenarioGenerator(20261106)
    processor = BlockAProcessor(BASELINE)
    first = generator.normal(1, start)
    processor.process_group([first])
    repeated = processor.process_group([generator.duplicate(first, BASELINE.dedup_ttl / 2)])
    second = processor.process_group([generator.normal(2, start.replace(minute=1))])
    return {
        "job_plan_operators": len(build_job_plan("baseline").keyed_operators),
        "duplicates_removed": len(repeated.duplicate_transaction_ids),
        "history_written": len(second.historical_records),
        "online_updates_positive": bool(second.online_updates),
    }


def main() -> int:
    """Run one mode, print its summary and return an exit code."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("mcp", "agent", "streaming"), required=True)
    parser.add_argument("--target", action="append", default=[], help="name=url for each agent or the MCP url")
    parser.add_argument("--tool", default="")
    parser.add_argument("--release-sha", required=True)
    args = parser.parse_args()
    summary: dict[str, object] = {"mode": args.mode, "release_sha": args.release_sha}
    try:
        if args.mode == "mcp":
            summary.update(check_mcp(args.target[0], os.environ["CD_SMOKE_TOKEN"], args.tool))
        elif args.mode == "agent":
            for item in args.target:
                name, url = item.split("=", 1)
                summary[name] = check_agent(url, name)
        else:
            summary.update(check_streaming())
    except Exception as error:  # Reported in the summary; the verifier then fails the release.
        summary["error"] = type(error).__name__
        print("CD_SMOKE_SUMMARY " + json.dumps(summary, sort_keys=True))
        return 1
    print("CD_SMOKE_SUMMARY " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
