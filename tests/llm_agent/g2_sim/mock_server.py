"""Mock agentgateway (OpenAI chat completions) or EPP /metrics, started by the fake
`kubectl port-forward` in the foreground, like kubectl itself (so kill stops it).

usage: mock_server.py gateway|epp <port>
gateway: every request is served by one of the two primary pods (round robin in
B2, least-served in O2), counted in the shared state, answered with the
x-ewallet-backend header of the routing in force; tool requests get valid
tool_calls. epp: llm_d_epp_* metrics with 2 ready endpoints.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import simstate  # noqa: E402

MODE = sys.argv[1]


def pick() -> tuple[str, str, str]:
    with simstate.locked() as state:
        pods = sorted(p for p, v in state["pods"].items() if v["labels"].get("app.kubernetes.io/instance") == "primary")
        routing = state["routing"] or "b2"
        backend = "llm-d" if routing == "o2" else "primary"
        if simstate.fail_at(f"backend-{routing}"):
            backend = "fallback"
        served = {p: state["counters"].get(p, {}).get("vllm:request_success", 0) for p in pods}
        pod = (min(pods, key=lambda p: (served[p], p)) if routing == "o2"
               else pods[state["serial"] % len(pods)])
        state["serial"] += 1
        if routing == "o2":
            state["epp"].append({"body": "Request handled", "endpoint": f"{state['pods'][pod]['ip']}:8000"})
        return pod, backend, routing


def count(pod: str, prompt: int, completion: int) -> None:
    with simstate.locked() as state:
        c = state["counters"].setdefault(pod, {})
        for key, value in (("vllm:request_success", 1), ("vllm:prompt_tokens", prompt),
                           ("vllm:generation_tokens", completion), ("vllm:prefix_cache_queries", prompt),
                           ("vllm:prefix_cache_hits", prompt // 2)):
            c[key] = c.get(key, 0) + value


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def do_GET(self):
        if MODE == "epp" and self.path == "/metrics":
            body = ("# TYPE llm_d_epp_ready_endpoints gauge\n"
                    'llm_d_epp_ready_endpoints{name="sim"} 2\n'
                    f"llm_d_epp_request_total {len(simstate.read()['epp'])}\n").encode()
        else:
            body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != f"Bearer {os.environ.get('G2_SIM_KEY', 'sim-client-key')}":
            self.send_response(401)
            self.end_headers()
            return
        if simstate.fail_at("rate-limit"):
            self.send_response(429)
            self.end_headers()
            return
        pod, backend, _ = pick()
        prompt = len(json.dumps(body["messages"])) // 4
        completion = min(body.get("max_tokens", 16), 8)
        time.sleep(float(os.environ.get("G2_SIM_LATENCY", "0.02")))
        tools = bool(body.get("tools"))
        call = {"index": 0, "id": "call-1", "type": "function",
                "function": {"name": "feature_lookup", "arguments": json.dumps({"transaction_id": "tx-1"})}}
        usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
        count(pod, prompt, completion)
        self.send_response(200)
        self.send_header("x-ewallet-backend", backend)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = ([{"choices": [{"delta": {"tool_calls": [call]}}]}] if tools else
                      [{"choices": [{"delta": {"content": f"t{i} "}}]} for i in range(completion)])
            for chunk in chunks + [{"choices": [], "usage": usage}]:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            return
        message = {"role": "assistant", "content": "ok"}
        if tools:
            message["tool_calls"] = [{k: v for k, v in call.items() if k != "index"}]
        payload = json.dumps({"choices": [{"message": message}], "usage": usage}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), Handler).serve_forever()
