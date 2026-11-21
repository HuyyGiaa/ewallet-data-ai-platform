"""Local stand-in backend: reports received headers and streams server-sent events.

A POST may ask for a status (x-stub-status), a delay (x-stub-sleep, seconds) or a dropped
connection (x-stub-close). With x-stub-key, every arrival is counted under that key, so a
test can see how many attempts the gateway made for one client request.
"""

from __future__ import annotations

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import json
import time

calls: Counter = Counter()


class Handler(BaseHTTPRequestHandler):
    """Answer every path the gateway forwards."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        """Keep the pod log quiet."""

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Serve the call counter, the stream, or an echo."""
        if self.path == "/ws" and self.headers.get("upgrade", "").lower() == "websocket":
            # Accept the WebSocket handshake, then close: enough to show the upgrade passes.
            key = self.headers.get("sec-websocket-key", "")
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            self.send_response(101)
            self.send_header("upgrade", "websocket")
            self.send_header("connection", "Upgrade")
            self.send_header("sec-websocket-accept", accept)
            self.end_headers()
            self.close_connection = True
        elif self.path == "/calls":
            self._json(200, {key: value for key, value in calls.items()})
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            for index in range(4):
                chunk = f"data: {index}\n\n".encode()
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
                time.sleep(0.7)
            self.wfile.write(b"0\r\n\r\n")
        else:
            self._json(200, {"headers": {key.lower(): value for key, value in self.headers.items()}})

    def do_POST(self) -> None:
        """Echo headers, or behave the way a test asked for."""
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        if self.headers.get("x-stub-key"):
            calls[self.headers["x-stub-key"]] += 1
        if self.headers.get("x-stub-close"):
            self.close_connection = True
            self.connection.close()
            return
        time.sleep(float(self.headers.get("x-stub-sleep") or 0))
        status = int(self.headers.get("x-stub-status") or 200)
        if status != 200:
            calls[str(status)] += 1
        self._json(status, {"headers": {key.lower(): value for key, value in self.headers.items()}})


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
