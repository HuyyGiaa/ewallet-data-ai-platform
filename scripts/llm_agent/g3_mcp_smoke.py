"""Use the official MCP SDK against the G3 MCP Gateway via a local port-forward.

The caller supplies a tool and JSON arguments.  Authentication is read from
``EWALLET_MCP_SERVICE_TOKEN`` and is never printed or written.  Evidence holds
only protocol identity, discovered tool names, latency and the tool result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _local_endpoint(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise argparse.ArgumentTypeError("endpoint must be a local MCP Gateway route")
    if parsed.port is None:
        raise argparse.ArgumentTypeError("endpoint must include an explicit port")
    return value


async def _run(endpoint: str, token: str, tool: str, arguments: dict[str, object], timeout: float) -> dict[str, object]:
    headers = {"X-Ewallet-MCP-Service-Token": token}
    async with httpx2.AsyncClient(headers=headers, timeout=timeout) as client:
        async with streamable_http_client(endpoint, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                discovered = await session.list_tools()
                started = time.monotonic()
                result = await session.call_tool(tool, arguments)
                elapsed_ms = round((time.monotonic() - started) * 1000, 3)
    return {
        "transport": "official-mcp-sdk-streamable-http",
        "protocol_version": str(initialized.protocol_version),
        "server": initialized.server_info.name,
        "tools": sorted(item.name for item in discovered.tools),
        "tool": tool,
        "latency_ms": elapsed_ms,
        "is_error": result.is_error,
        "structured_content": result.structured_content,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", type=_local_endpoint, default="http://127.0.0.1:18082/feature-rag/mcp")
    parser.add_argument("--tool", required=True)
    parser.add_argument("--arguments-json", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    token = os.environ.get("EWALLET_MCP_SERVICE_TOKEN")
    if not token:
        parser.error("EWALLET_MCP_SERVICE_TOKEN is required and must not be logged")
    arguments = json.loads(args.arguments_json)
    if not isinstance(arguments, dict):
        parser.error("--arguments-json must decode to an object")
    report = asyncio.run(_run(args.endpoint, token, args.tool, arguments, args.timeout))
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "structured_content"}, indent=2))
    return 1 if report["is_error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
