"""Minimal MCP Streamable HTTP stub (JSON responses) that records request headers.

Test fixture only: it stands in for feature-rag-mcp/drift-mcp so the policy
behaviour of the MCP Gateway can be observed without the real tool backends.
Every request is logged as one JSON line with the identity headers it carried.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys

TOOLS = [{"name": name, "description": f"stub {name}", "inputSchema": {"type": "object"}}
         for name in ("feature_lookup", "fraud_scoring", "knowledge_search", "drift_detection")]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, body=None):
        payload = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._reply(405)

    def do_DELETE(self):
        self._reply(405)

    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))) or b"{}")
        method = message.get("method")
        print(json.dumps({"method": method, "tool": (message.get("params") or {}).get("name"),
                          "x-ewallet-actor": self.headers.get("x-ewallet-actor"),
                          "x-ewallet-caller": self.headers.get("x-ewallet-caller"),
                          "authorization_present": "authorization" in self.headers}), flush=True)
        if "id" not in message:
            return self._reply(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "stub", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "stub-executed"}], "isError": False}
        else:
            result = {}
        self._reply(200, {"jsonrpc": "2.0", "id": message["id"], "result": result})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), Handler).serve_forever()
