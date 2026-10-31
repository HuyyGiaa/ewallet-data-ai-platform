"""MCP client for the gateway policy smoke: spoofed headers, list and call."""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1]
SPOOF = {"x-ewallet-actor": "spoofed-admin", "x-ewallet-caller": "spoofed-specialist",
         "authorization": "Bearer forged"}


def post(path, message, session=None):
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream",
               "mcp-protocol-version": "2025-06-18", **SPOOF}
    if session:
        headers["mcp-session-id"] = session
    request = urllib.request.Request(BASE + path, json.dumps(message).encode(), headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, raw, sid = response.status, response.read().decode(), response.headers.get("mcp-session-id")
    except urllib.error.HTTPError as exc:
        status, raw, sid = exc.code, exc.read().decode(), None
    body = None
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
    try:
        body = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        body = raw[:300]
    return status, body, sid


results = {}
for path in sys.argv[2:]:
    status, body, sid = post(path, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}}})
    entry = {"initialize": status}
    if status == 200:
        post(path, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        status, body, _ = post(path, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
        tools = sorted(t["name"] for t in (body or {}).get("result", {}).get("tools", [])) if isinstance(body, dict) else body
        entry["tools_list"] = {"status": status, "tools": tools}
        calls = {}
        for tool in ("feature_lookup", "fraud_scoring"):
            status, body, _ = post(path, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                          "params": {"name": tool, "arguments": {}}}, sid)
            outcome = body.get("error") if isinstance(body, dict) and "error" in body else (
                body.get("result") if isinstance(body, dict) else body)
            calls[tool] = {"status": status, "outcome": outcome}
        entry["tools_call"] = calls
    else:
        entry["body"] = body
    results[path] = entry
print(json.dumps(results, sort_keys=True))
