"""Mock of the vLLM OpenAI API for offline gateway tests (NOT a model).

POST /v1/chat/completions answers with a fixed feature_lookup tool call (or a
text reply once a tool result is present), non-streamed or as SSE chunks.
POST /_mode {"mode": "ok"|"slow"|"500"|"503", "delay": s} switches behaviour.
GET /_log returns every request seen (model, stream, tools, auth ok).
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import time

STATE = {"mode": "ok", "delay": 0.0, "log": []}
KEY = sys.argv[2]
NAME = sys.argv[3]


def completion(body):
    has_tool_result = any(m.get("role") == "tool" for m in body.get("messages", []))
    if has_tool_result:
        return {"role": "assistant", "content": "Fixture summary: velocity_5m=3 (test data)."}, "stop"
    call = {"id": "call_mock_1", "type": "function",
            "function": {"name": "feature_lookup", "arguments": json.dumps({"transaction_id": "tx-demo-001"})}}
    return {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200 if STATE["mode"] != "503" else 503, {})
        if self.path == "/_log":
            return self._json(200, STATE["log"])
        self._json(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))) or b"{}")
        if self.path == "/_mode":
            STATE.update(mode=body["mode"], delay=float(body.get("delay", 0)), log=[])
            return self._json(200, {"ok": True})
        STATE["log"].append({"backend": NAME, "model": body.get("model"), "stream": bool(body.get("stream")),
                             "tools": len(body.get("tools") or []),
                             "auth_ok": self.headers.get("authorization") == f"Bearer {KEY}"})
        mode = STATE["mode"]
        if mode == "503":
            return self._json(503, {"error": {"message": "mock unavailable"}})
        if mode == "500":
            return self._json(500, {"error": {"message": "mock engine error"}})
        if mode == "slow":
            time.sleep(STATE["delay"])
        message, finish = completion(body)
        usage = {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49}
        if not body.get("stream"):
            return self._json(200, {"id": "chatcmpl-mock", "object": "chat.completion", "model": body.get("model"),
                                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                    "usage": usage})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()

        def send(chunk):
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": body.get("model")}
        if "tool_calls" in message:
            call = message["tool_calls"][0]
            args = call["function"]["arguments"]
            send(dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                {"index": 0, "id": call["id"], "type": "function",
                 "function": {"name": "feature_lookup", "arguments": ""}}]}, "finish_reason": None}]))
            for part in (args[: len(args) // 2], args[len(args) // 2:]):
                send(dict(base, choices=[{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": part}}]}, "finish_reason": None}]))
        else:
            for word in message["content"].split(" "):
                send(dict(base, choices=[{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]))
        send(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": finish}]))
        send(dict(base, choices=[], usage=usage))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), Handler).serve_forever()
