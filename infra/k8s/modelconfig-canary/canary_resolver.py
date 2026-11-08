"""Isolated canary for the BYO ModelConfig resolver (kagent-pilot namespace only).

Runs inside a kagent BYO Agent pod built from the deployed Coordinator image, with
ai_platform/llm_agent/model_config.py mounted from a ConfigMap (the deployed image predates
it). It performs exactly the startup step the new BYO agents perform, with the same pod
env, projected token, Role and NetworkPolicy the chart renders:

  ModelConfig CR -> GET with the pod ServiceAccount -> validation -> resolved config

On success it logs the resolution line and serves it on the kagent readiness path, so the
pod turns Ready only after a successful resolution. On failure it logs the error code and
exits 1 (fail closed: the pod never becomes Ready). It makes no LLM call: the canary
namespace has no route to agentgateway; the LLM leg is tested separately.

It also tries one TCP connection that the chart's egress policy must block.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
import os
import socket
import sys


def load_resolver():
    spec = importlib.util.spec_from_file_location("model_config", "/canary/model_config.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module      # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def denied_egress_probe() -> str:
    target = os.environ.get("CANARY_DENIED_EGRESS", "")
    if not target:
        return "not-configured"
    host, _, port = target.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            return "connected"          # the egress policy failed to block it
    except OSError as exc:
        return f"blocked ({type(exc).__name__})"


def main() -> int:
    mc = load_resolver()
    egress = denied_egress_probe()
    try:
        if not os.environ.get("EWALLET_LLM_API_KEY", "").strip():
            raise mc.ModelConfigError("llm_credential_unavailable", "EWALLET_LLM_API_KEY is not mounted")
        resolved = mc.resolve_from_env()
    except mc.ModelConfigError as exc:
        print(json.dumps({"event": "modelconfig.failed", "code": exc.code, "message": str(exc),
                          "denied_egress_probe": egress}), file=sys.stderr, flush=True)
        return 1
    mc.log_resolution(resolved)
    body = json.dumps({**resolved.audit(), "denied_egress_probe": egress}).encode()
    print(json.dumps({"event": "canary.ready", "denied_egress_probe": egress}), file=sys.stderr, flush=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path not in ("/.well-known/agent-card.json", "/resolved"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
