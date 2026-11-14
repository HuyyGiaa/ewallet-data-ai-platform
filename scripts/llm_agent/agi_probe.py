"""Agent-integration (AGI) evidence probes through local port-forwards.

request  build one canonical typed A2A request for the Coordinator:
         fraud (g3-business-request/v1), drift (g3-drift-request/v1) or
         combined (g3-workflow-request/v1). Every business value is supplied by
         the operator; nothing is defaulted except the fraud feature projection.
mcp      call one MCP tool through the MCP Gateway with the official SDK and
         check an expectation: ``ok`` (tool succeeded), ``denied`` (the
         principal may not call it), ``rejected`` (invalid or unknown business
         input) or ``unavailable`` (a backend or data source is missing).
         The service token comes from EWALLET_MCP_SERVICE_TOKEN and is never
         printed or written.

usage:
  python3 scripts/llm_agent/agi_probe.py request drift --request-id agi-...-drift \\
      --feature account_behavior__account_tx_count_1h --baseline BASELINE \\
      --window-start 2026-05-24T00:00:00Z --window-end 2026-05-31T23:00:00Z --out drift-request.json
  python3 scripts/llm_agent/agi_probe.py request fraud --request-id agi-...-fraud \\
      --transaction transaction.json [--policy-query TEXT] --out fraud-request.json
  python3 scripts/llm_agent/agi_probe.py request combined --request-id agi-...-wf --transaction transaction.json \\
      --feature ... --baseline ... --window-start ... --window-end ... --out combined-request.json
  EWALLET_MCP_SERVICE_TOKEN=... python3 scripts/llm_agent/agi_probe.py mcp \\
      --endpoint http://127.0.0.1:18082/drift/mcp --tool drift_detection --arguments-json '{...}' \\
      --expect ok --evidence out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_platform.llm_agent.business_request import (  # noqa: E402
    G3BusinessRequest,
    G3DriftRequest,
    G3WorkflowRequest,
    a2a_message_send,
    member_request_id,
)

DEFAULT_FEATURES = (
    "account_behavior__account_tx_count_1h",
    "account_behavior__account_tx_count_24h",
    "user_behavior__user_avg_amount_30d",
    "merchant_behavior__merchant_tx_count_24h",
)


def _local(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise argparse.ArgumentTypeError("endpoint must be http://127.0.0.1:<port>/... (a port-forward)")
    return value


def fraud_request(request_id: str, transaction: dict, policy_query: str | None) -> G3BusinessRequest:
    tools = ("feature_lookup", "fraud_scoring") + (("knowledge_search",) if policy_query else ())
    return G3BusinessRequest(request_id=request_id, target_agent="feature-rag-specialist",
                             capabilities=("fraud-evidence",), requested_tools=tools, transaction=transaction,
                             feature_names=DEFAULT_FEATURES, knowledge_query=policy_query)


def drift_request(request_id: str, args: argparse.Namespace) -> G3DriftRequest:
    return G3DriftRequest(request_id=request_id, feature_name=args.feature, baseline_id=args.baseline,
                          current_window={"start": args.window_start, "end": args.window_end})


def build(args: argparse.Namespace):
    transaction = json.loads(args.transaction.read_text(encoding="utf-8")) if args.transaction else None
    if args.kind == "fraud":
        return fraud_request(args.request_id, transaction, args.policy_query)
    if args.kind == "drift":
        return drift_request(args.request_id, args)
    return G3WorkflowRequest(request_id=args.request_id,
                             fraud=fraud_request(member_request_id(args.request_id, "fraud"), transaction,
                                                 args.policy_query),
                             drift=drift_request(member_request_id(args.request_id, "drift"), args))


async def call_tool(endpoint: str, token: str, tool: str, arguments: dict, timeout: float,
                    transport: object | None = None) -> dict:
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    try:
        from mcp.shared.exceptions import MCPError as McpError
    except ImportError:  # older SDK naming
        from mcp.shared.exceptions import McpError

    async with httpx2.AsyncClient(headers={"X-Ewallet-MCP-Service-Token": token}, timeout=timeout,
                                  transport=transport) as client:
        async with streamable_http_client(endpoint, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                tools = sorted(item.name for item in (await session.list_tools()).tools)
                started = time.monotonic()
                try:
                    result = await session.call_tool(tool, arguments)
                except McpError as exc:
                    return {"server": initialized.server_info.name, "tools": tools, "tool": tool,
                            "outcome": "denied", "jsonrpc_error": str(exc)[:200]}
                status = (result.structured_content or {}).get("status")
                outcome = "ok" if not result.is_error and status == "ok" else status or "error"
                return {"server": initialized.server_info.name, "tools": tools, "tool": tool,
                        "latency_ms": round((time.monotonic() - started) * 1000, 3),
                        "outcome": outcome, "structured_content": result.structured_content}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    request = commands.add_parser("request")
    request.add_argument("kind", choices=["fraud", "drift", "combined"])
    request.add_argument("--request-id", required=True)
    request.add_argument("--transaction", type=Path, help="reviewed FraudPredictionRequest JSON")
    request.add_argument("--policy-query")
    request.add_argument("--feature")
    request.add_argument("--baseline")
    request.add_argument("--window-start")
    request.add_argument("--window-end")
    request.add_argument("--out", type=Path, required=True)
    mcp = commands.add_parser("mcp")
    mcp.add_argument("--endpoint", type=_local, required=True)
    mcp.add_argument("--tool", required=True)
    mcp.add_argument("--arguments-json", required=True)
    mcp.add_argument("--expect", choices=["ok", "denied", "rejected", "unavailable"], required=True)
    mcp.add_argument("--evidence", type=Path, required=True)
    mcp.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    if args.command == "request":
        if args.kind in ("fraud", "combined") and args.transaction is None:
            parser.error("fraud and combined requests need --transaction")
        if args.kind in ("drift", "combined") and not all((args.feature, args.baseline, args.window_start, args.window_end)):
            parser.error("drift and combined requests need --feature, --baseline, --window-start and --window-end")
        try:
            typed = build(args)
        except ValueError as exc:
            parser.error(str(exc)[:500])
        if args.out.exists():
            parser.error(f"{args.out} exists; evidence files are create-only")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(a2a_message_send(typed), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(typed.to_wire_text())
        return 0

    token = os.environ.get("EWALLET_MCP_SERVICE_TOKEN", "").strip()
    if not token:
        parser.error("EWALLET_MCP_SERVICE_TOKEN is required and is never printed")
    arguments = json.loads(args.arguments_json)
    if not isinstance(arguments, dict):
        parser.error("--arguments-json must decode to an object")
    report = asyncio.run(call_tool(args.endpoint, token, args.tool, arguments, args.timeout))
    report.update({"endpoint": args.endpoint, "expect": args.expect,
                   "verdict": "PASS" if report["outcome"] == args.expect else "FAIL"})
    if args.evidence.exists():
        parser.error(f"{args.evidence} exists; evidence files are create-only")
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "structured_content"}, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
