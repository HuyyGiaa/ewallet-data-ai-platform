"""Local A2A server for sandbox runs: answers on the port the driver forwards to."""
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        state = "failed" if os.environ.get("SIM_AGENT_FAILS") else "completed"
        assert self.headers["X-User-Id"], "the controller needs X-User-Id"
        assert body["params"]["message"]["contextId"], "a sandbox needs contextId"
        data = json.dumps({"result": {"id": "r", "status": {"state": state}}}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
