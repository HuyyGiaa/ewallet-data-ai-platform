"""Agent-integration (AGI) evidence probes through local port-forwards.

request  build one canonical typed A2A request for the Coordinator:
         fraud (g3-business-request/v1), drift (g3-drift-request/v1) or
         combined (g3-workflow-request/v1). Every business value is supplied by
         the operator; nothing is defaulted except the fraud feature projection.
mcp      call one MCP tool through the MCP Gateway with the official SDK and
         check an expectation: ``ok``, ``rejected`` (invalid or unknown business
         input) or ``unavailable`` (backend or data source missing) are in-band
         tool statuses. Two negative security expectations need proof that the
         intended policy caused the rejection:
           ``denied``           the server's hidden-tool error (JSON-RPC -32602
                                "Unknown tool") for a tool absent from the tested
                                principal's tools/list, while a control principal
                                (--control-token-env) lists it on the same endpoint;
           ``unauthenticated``  HTTP 401 at initialize, while the same credential is
                                valid on its own server (--tested-valid-endpoint) and a
                                control principal lists the tool.
         Any other failure (invalid arguments, backend, protocol, HTTP 5xx,
         transport, timeout) is FAIL. Tokens come from the environment and are
         never printed or written.
a2a-route  send one typed request to the A2A Gateway with an undeclared
         x-kagent-host; PASS only for HTTP 404 while a declared control answers
         200. The control is a declared x-kagent-host (--control-kagent-host) or,
         since the legacy Specialist Deployments are gone, a declared Sandbox
         route path (--control-path /api/a2a-sandboxes/ewallet-sandbox/drift-specialist/).

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
import re
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


# The MCP server's tools/call contract for a tool outside the caller's grants (or unknown): the
# same JSON-RPC error, so tools cannot be enumerated (ai_platform/llm_agent/tools/service.py).
HIDDEN_TOOL_ERROR = (-32602, "Unknown tool")
IN_BAND = frozenset({"ok", "rejected", "unavailable", "timeout", "error"})


def _http_outcome(status: int) -> str:
    return {401: "unauthenticated", 403: "forbidden"}.get(status, "server_error" if status >= 500 else "protocol_error")


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for sub in error.exceptions for leaf in _leaves(sub)]
    return [error]


async def observe(endpoint: str, token: str, tool: str, arguments: dict | None, timeout: float,
                  transport: object | None = None) -> dict:
    """Run initialize, tools/list and (unless ``arguments`` is None) tools/call; classify what happened.

    Outcomes: in-band tool statuses (ok, rejected, unavailable, timeout, error); ``listed`` when no
    call was made; ``hidden_tool`` for the exact HIDDEN_TOOL_ERROR; ``unauthenticated`` (HTTP 401) and
    ``forbidden`` (HTTP 403) from the server's authentication layer; ``protocol_error`` for any other
    JSON-RPC or HTTP 4xx error; ``server_error`` for HTTP 5xx; ``transport_error`` when no HTTP exchange
    completed; ``client_timeout`` when the probe deadline expired.
    """

    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.exceptions import MCPError

    statuses: list[int] = []

    async def on_response(response) -> None:
        statuses.append(response.status_code)

    report: dict = {"tool": tool, "tools": None, "stage": "connect", "http_statuses": statuses}

    def finish(outcome: str, **extra) -> dict:
        report.update(outcome=outcome, **extra)
        return report

    try:
        async with asyncio.timeout(timeout):
            async with httpx2.AsyncClient(headers={"X-Ewallet-MCP-Service-Token": token}, timeout=timeout,
                                          transport=transport, event_hooks={"response": [on_response]}) as client:
                async with streamable_http_client(endpoint, http_client=client) as (read, write):
                    async with ClientSession(read, write) as session:
                        report["stage"] = "initialize"
                        initialized = await session.initialize()
                        report["server"] = initialized.server_info.name
                        report["stage"] = "list"
                        report["tools"] = sorted(item.name for item in (await session.list_tools()).tools)
                        if arguments is None:
                            return finish("listed")
                        report["stage"] = "call"
                        started = time.monotonic()
                        try:
                            result = await session.call_tool(tool, arguments)
                        except MCPError as exc:
                            code, message = exc.error.code, exc.error.message
                            if statuses and statuses[-1] >= 400:   # the SDK wraps HTTP errors as MCPError
                                return finish(_http_outcome(statuses[-1]), http_status=statuses[-1],
                                              jsonrpc_error={"code": code, "message": message[:200]})
                            hidden = (code, message) == HIDDEN_TOOL_ERROR
                            return finish("hidden_tool" if hidden else "protocol_error",
                                          jsonrpc_error={"code": code, "message": message[:200]})
                        content = result.structured_content or {}
                        status = content.get("status")
                        if status not in IN_BAND or (status == "ok") == bool(result.is_error):
                            return finish("protocol_error", detail="tool result is not a shared ToolCallResponse")
                        error = (content.get("result") or {}).get("error") if status != "ok" else None
                        return finish(status, latency_ms=round((time.monotonic() - started) * 1000, 3),
                                      tool_error_code=(error or {}).get("code"), structured_content=content)
    except TimeoutError:
        return finish("client_timeout")
    except Exception as exc:  # noqa: BLE001 - classified below, never re-raised as success
        leaves = _leaves(exc)
        last = statuses[-1] if statuses else None
        if last is not None and last >= 400:
            return finish(_http_outcome(last), http_status=last)
        if any(isinstance(leaf, MCPError) for leaf in leaves):
            error = next(leaf for leaf in leaves if isinstance(leaf, MCPError)).error
            return finish("protocol_error", jsonrpc_error={"code": error.code, "message": error.message[:200]})
        return finish("transport_error", error_type=sorted({type(leaf).__name__ for leaf in leaves}))


async def call_tool(endpoint: str, token: str, tool: str, arguments: dict, timeout: float,
                    transport: object | None = None) -> dict:
    """Compatibility wrapper: one classified tool call."""

    return await observe(endpoint, token, tool, arguments, timeout, transport)


async def verdict(args: argparse.Namespace, token: str, arguments: dict, transport: object | None = None,
                  valid_transport: object | None = None) -> dict:
    """Observe the call and decide PASS only when the expected mechanism is proven responsible."""

    report = await observe(args.endpoint, token, args.tool, arguments, args.timeout, transport)
    report.update({"endpoint": args.endpoint, "expect": args.expect})
    reasons: list[str] = []
    if args.expect in IN_BAND:
        if report["outcome"] != args.expect:
            reasons.append(f"observed {report['outcome']}, expected {args.expect}")
    else:
        # A negative expectation needs a control principal that can see the tool on the same endpoint:
        # otherwise an unknown tool, a broken route or a down server would look like a denial.
        control_token = os.environ.get(args.control_token_env or "", "").strip()
        if not control_token:
            reasons.append("negative expectations need --control-token-env naming a principal granted the tool")
        else:
            control = await observe(args.endpoint, control_token, args.tool, None, args.timeout, transport)
            report["control"] = {key: control.get(key) for key in ("outcome", "tools", "http_statuses")}
            if control["outcome"] != "listed" or args.tool not in (control["tools"] or []):
                reasons.append("the control principal does not see the tool on this endpoint")
        if args.expect == "denied":
            if report["outcome"] != "hidden_tool":
                reasons.append(f"observed {report['outcome']}, expected the hidden-tool authorization error")
            elif args.tool in (report["tools"] or []):
                reasons.append("the tested principal lists the tool, so 'Unknown tool' is not an authorization denial")
        elif args.expect == "call-denied":
            # A list-only (discovery) subject: it authenticates and lists the tool, so the tool exists;
            # "Unknown tool" on tools/call can then only be the server's grant check.
            if report["outcome"] != "hidden_tool":
                reasons.append(f"observed {report['outcome']}, expected the hidden-tool authorization error")
            elif args.tool not in (report["tools"] or []):
                reasons.append("the tested principal does not list the tool, so the denial is not proven on a known tool")
        elif args.expect == "revoked":
            # A retired credential: HTTP 401 at initialize while an active principal works on the same endpoint
            # (the control above), so the 401 is the credential, not a down server or a broken route.
            if report["outcome"] != "unauthenticated" or report["stage"] != "initialize":
                reasons.append(f"observed {report['outcome']} at {report['stage']}, expected HTTP 401 at initialize")
        elif args.expect == "unauthenticated":
            if report["outcome"] != "unauthenticated" or report["stage"] != "initialize":
                reasons.append(f"observed {report['outcome']} at {report['stage']}, expected HTTP 401 at initialize")
            if not args.tested_valid_endpoint:
                reasons.append("unauthenticated needs --tested-valid-endpoint where the tested credential is valid")
            else:
                home = await observe(args.tested_valid_endpoint, token, args.tool, None, args.timeout, valid_transport)
                report["tested_credential_home"] = {"endpoint": args.tested_valid_endpoint, "outcome": home["outcome"]}
                if home["outcome"] != "listed":
                    reasons.append("the tested credential is not valid on its own server, so 401 proves nothing")
    report["verdict"] = "FAIL" if reasons else "PASS"
    report["reasons"] = reasons
    return report


def a2a_route(args: argparse.Namespace, opener=None) -> dict:
    """POST one typed A2A request with x-kagent-host; ``no-route`` needs 404 and a routed control host."""

    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    opener = opener or urlopen
    body = args.request.read_bytes()

    def send(host: str) -> dict:
        request = Request(args.endpoint, data=body, method="POST",
                          headers={"Content-Type": "application/json", "x-kagent-host": host})
        try:
            with opener(request, timeout=args.timeout) as response:
                return {"host": host, "http_status": response.status, "body": response.read(4096).decode(errors="replace")}
        except HTTPError as exc:
            return {"host": host, "http_status": exc.code, "body": exc.read(4096).decode(errors="replace")}
        except (URLError, TimeoutError, OSError) as exc:
            return {"host": host, "http_status": None, "error_type": type(exc).__name__}

    def send_path(path: str) -> dict:
        # A declared Sandbox route: kagent selects the session actor by the message contextId.
        payload = json.loads(body)
        message = (payload.get("params") or {}).get("message") or {}
        if message and not message.get("contextId"):
            message["contextId"] = message.get("messageId") or "a2a-route-control"
        request = Request(args.endpoint.rstrip("/") + path, data=json.dumps(payload).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
        try:
            with opener(request, timeout=args.timeout) as response:
                return {"path": path, "http_status": response.status, "body": response.read(4096).decode(errors="replace")}
        except HTTPError as exc:
            return {"path": path, "http_status": exc.code, "body": exc.read(4096).decode(errors="replace")}
        except (URLError, TimeoutError, OSError) as exc:
            return {"path": path, "http_status": None, "error_type": type(exc).__name__}

    control_path = getattr(args, "control_path", None)
    if control_path and not re.fullmatch(r"/api/a2a-sandboxes/[a-z0-9-]+/[a-z0-9-]+/", control_path):
        raise SystemExit("--control-path must be a declared /api/a2a-sandboxes/<namespace>/<agent>/ route")
    tested = send(args.kagent_host)
    control = send_path(control_path) if control_path else send(args.control_kagent_host)
    reasons = []
    if tested["http_status"] != 404:
        reasons.append(f"undeclared host returned {tested['http_status']}, expected 404 (no route)")
    if '"jsonrpc"' in tested.get("body", "") and '"result"' in tested.get("body", ""):
        reasons.append("undeclared host produced an agent result")
    if control["http_status"] != 200:
        reasons.append(f"control host returned {control['http_status']}: the gateway or its route is not working")
    return {"endpoint": args.endpoint, "tested": tested, "control": {k: v for k, v in control.items() if k != "body"},
            "expect": args.expect, "verdict": "FAIL" if reasons else "PASS", "reasons": reasons}


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
    mcp.add_argument("--expect", choices=["ok", "rejected", "unavailable", "denied", "call-denied", "unauthenticated",
                                          "revoked"], required=True)
    mcp.add_argument("--control-token-env", help="env var with the token of a principal granted --tool (negative expectations)")
    mcp.add_argument("--tested-valid-endpoint", type=_local,
                     help="endpoint where the tested credential is valid (required for --expect unauthenticated)")
    mcp.add_argument("--evidence", type=Path, required=True)
    mcp.add_argument("--timeout", type=float, default=30.0)
    route = commands.add_parser("a2a-route")
    route.add_argument("--endpoint", type=_local, required=True)
    route.add_argument("--kagent-host", required=True)
    control = route.add_mutually_exclusive_group()
    control.add_argument("--control-kagent-host", default="drift-specialist.ewallet-agents",
                         help="declared x-kagent-host control (legacy Deployment routes)")
    control.add_argument("--control-path", help="declared Sandbox route control, e.g. /api/a2a-sandboxes/ewallet-sandbox/drift-specialist/")
    route.add_argument("--request", type=Path, required=True)
    route.add_argument("--expect", choices=["no-route"], required=True)
    route.add_argument("--evidence", type=Path, required=True)
    route.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if getattr(args, "evidence", None) is not None and args.evidence.exists():
        parser.error(f"{args.evidence} exists; evidence files are create-only")

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

    if args.command == "a2a-route":
        report = a2a_route(args)
    else:
        token = os.environ.get("EWALLET_MCP_SERVICE_TOKEN", "").strip()
        if not token:
            parser.error("EWALLET_MCP_SERVICE_TOKEN is required and is never printed")
        arguments = json.loads(args.arguments_json)
        if not isinstance(arguments, dict):
            parser.error("--arguments-json must decode to an object")
        report = asyncio.run(verdict(args, token, arguments))
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "structured_content"}, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
