"""Expose ewallet-bench pod CPU and memory usage to Prometheus (stdlib only).

Runs as a sidecar of the benchmark Prometheus. It polls the Kubernetes resource metrics
API (``metrics.k8s.io``, the same metrics-server data as ``kubectl top``) for its own
namespace and serves the latest sample on ``/metrics``.

Why not cAdvisor: scraping the kubelet needs ``nodes/metrics``, a cluster-wide permission
the benchmark is not allowed to add. ``pods.metrics.k8s.io`` is namespaced, so a Role in
``ewallet-bench`` is enough.

These are metrics-server samples (usage averaged over its own window, typically 15-30 s),
not requested CPU. A failed poll keeps the previous values out of ``/metrics`` and raises
``bench_pod_usage_poll_errors_total``, so a gap shows as missing data, never as zero.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

SA_DIR = Path(os.environ.get("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount"))


def cpu_cores(value: str) -> float:
    """Convert a Kubernetes CPU quantity (n, u, m or cores) to cores."""

    for suffix, scale in (("n", 1e-9), ("u", 1e-6), ("m", 1e-3)):
        if value.endswith(suffix):
            return float(value[:-1]) * scale
    return float(value)


def memory_bytes(value: str) -> float:
    """Convert a Kubernetes memory quantity to bytes."""

    units = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "k": 1e3, "M": 1e6, "G": 1e9}
    for suffix, scale in units.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * scale
    return float(value)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(listing: dict[str, Any] | None, errors: int, last_success: float | None) -> str:
    """Render one Prometheus text exposition from a PodMetricsList."""

    lines = ["# HELP bench_pod_container_cpu_cores CPU usage from metrics.k8s.io (cores).",
             "# TYPE bench_pod_container_cpu_cores gauge",
             "# HELP bench_pod_container_memory_working_set_bytes Memory working set from metrics.k8s.io.",
             "# TYPE bench_pod_container_memory_working_set_bytes gauge",
             "# HELP bench_pod_usage_window_seconds metrics-server averaging window of the sample.",
             "# TYPE bench_pod_usage_window_seconds gauge"]
    for item in (listing or {}).get("items", []):
        pod = _escape(item["metadata"]["name"])
        window = item.get("window", "0s").rstrip("s") or "0"
        lines.append(f'bench_pod_usage_window_seconds{{pod="{pod}"}} {float(window)}')
        for container in item.get("containers", []):
            labels = f'pod="{pod}",container="{_escape(container["name"])}"'
            usage = container.get("usage", {})
            if "cpu" in usage:
                lines.append(f"bench_pod_container_cpu_cores{{{labels}}} {cpu_cores(usage['cpu'])}")
            if "memory" in usage:
                lines.append(f"bench_pod_container_memory_working_set_bytes{{{labels}}} {memory_bytes(usage['memory'])}")
    lines += ["# TYPE bench_pod_usage_poll_errors_total counter", f"bench_pod_usage_poll_errors_total {errors}"]
    if last_success is not None:
        lines += ["# TYPE bench_pod_usage_last_success_timestamp_seconds gauge",
                  f"bench_pod_usage_last_success_timestamp_seconds {last_success}"]
    return "\n".join(lines) + "\n"


class Poller:
    """Keeps the latest listing; only a successful poll replaces it."""

    def __init__(self, fetch: Callable[[], dict[str, Any]]) -> None:
        self.fetch = fetch
        self.listing: dict[str, Any] | None = None
        self.errors = 0
        self.last_success: float | None = None
        self.lock = threading.Lock()

    def poll(self) -> None:
        try:
            listing = self.fetch()
        except Exception:  # noqa: BLE001 - every failure is counted, none is fatal
            with self.lock:
                self.errors += 1
                self.listing = None
            return
        with self.lock:
            self.listing, self.last_success = listing, time.time()

    def text(self) -> str:
        with self.lock:
            return render(self.listing, self.errors, self.last_success)


def fetcher(namespace: str) -> Callable[[], dict[str, Any]]:
    """Return a function that lists PodMetrics in the namespace with a verified TLS session."""

    host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    context = ssl.create_default_context(cafile=str(SA_DIR / "ca.crt"))
    url = f"https://{host}:{port}/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods"

    def fetch() -> dict[str, Any]:
        token = (SA_DIR / "token").read_text(encoding="utf-8").strip()      # re-read: projected tokens rotate
        request = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        with urlopen(request, timeout=10, context=context) as response:  # noqa: S310 (in-cluster API)
            return json.load(response)

    return fetch


def main() -> int:
    """Poll every POLL_SECONDS and serve /metrics on PORT."""

    namespace = os.environ.get("NAMESPACE") or (SA_DIR / "namespace").read_text(encoding="utf-8").strip()
    poller = Poller(fetcher(namespace))
    interval = float(os.environ.get("POLL_SECONDS", "10"))

    def loop() -> None:
        while True:
            poller.poll()
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            body = poller.text().encode() if self.path == "/metrics" else b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    # Loopback only: Prometheus is in the same pod, nothing else needs this port.
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "9101"))), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
