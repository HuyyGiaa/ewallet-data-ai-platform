"""Stand-in pod for local KEDA validation: counts real HTTP requests as the real API does.

Exposes the same series name as ``tests/fixtures/serving_metric_names.txt``, so the chart's
PromQL runs unchanged. It proves the scaling chain, never a real API's capacity.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading

COUNTER = os.environ.get("METRIC", "feature_api_requests_total")
LATENCY = os.environ.get("LATENCY_METRIC", "feature_api_request_latency_seconds")
lock = threading.Lock()
count = 0


class Handler(BaseHTTPRequestHandler):
    """Serve /metrics and count every other request."""

    def log_message(self, *args: object) -> None:
        """Keep the pod log quiet."""

    def do_GET(self) -> None:
        """Answer one request."""
        global count
        if self.path.startswith("/metrics"):
            with lock:
                value = count
            body = (f'{COUNTER}{{outcome="success"}} {value}\n'
                    f'{LATENCY}_sum{{outcome="success"}} {value * 0.01}\n'
                    f'{LATENCY}_count{{outcome="success"}} {value}\n').encode()
        else:
            with lock:
                count += 1
            body = b"ok"
        self.send_response(200)
        # Prometheus 3 refuses a scrape without a content type, as prometheus_client sends.
        self.send_header("content-type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
