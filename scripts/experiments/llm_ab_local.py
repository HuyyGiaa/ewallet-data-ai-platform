"""Validate LLM routing on the pinned agentgateway with chart-rendered configuration.

Renders the real chart, points the rendered backends at local OpenAI-compatible servers,
runs the digest-pinned gateway in Docker, sends traffic and compares the split seen in
``x-ewallet-backend`` with the configured one. With ``--real-backend name=url`` a real vLLM
replaces a stub. Local routing evidence only: stub latency measures nothing.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from ops.experiments.ab_stats import split_check

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/helm/llm-agent-platform"
GATEWAY_IMAGE = "ghcr.io/agentgateway/agentgateway:v1.7.0@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b"
GATEWAY_KEY = "local-client-key"
BACKEND_KEY = "local-backend-key"
IMAGE_VALUE = "asia-southeast1-docker.pkg.dev/p/r/vllm-gpu-model@sha256:" + "c" * 64


class StubBackend:
    """An OpenAI-compatible server that reports the model it was asked for."""

    def __init__(self) -> None:
        outer = self
        self.calls = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return None

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.calls += 1
                raw = json.dumps({
                    "id": f"stub-{outer.calls}", "object": "chat.completion", "created": 0, "model": body["model"],
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        """Stop answering, as a lost backend would."""
        self.server.shutdown()
        self.server.server_close()


def render_config(overlays: list[Path]) -> dict:
    """Render the chart and return the agentgateway configuration document."""
    args = ["helm", "template", "llm-agent", str(CHART), "-f", str(CHART / "values-gcp.yaml"),
            "--set", "agents.enabled=false", "--set", f"vllmCandidate.image={IMAGE_VALUE}",
            # Test-only: the production limiter would turn a burst into 429s here.
            "--set", "agentgateway.requestRate.capacity=100000",
            "--set", "agentgateway.requestRate.refill=100000",
            "--set", "agentgateway.tokenRate.capacity=100000000",
            "--set", "agentgateway.tokenRate.refill=100000000"]
    for overlay in overlays:
        args += ["-f", str(overlay)]
    documents = yaml.safe_load_all(subprocess.run(args, check=True, capture_output=True, text=True).stdout)
    config_map = next(d for d in documents if d and d["kind"] == "ConfigMap" and d["metadata"]["name"] == "agentgateway-llm-config")
    return yaml.safe_load(config_map["data"]["config.yaml"])


def localise(config: dict, urls: dict[str, str]) -> dict:
    """Point each Service at a local URL and keep everything else as rendered."""
    text = json.dumps(config)
    for service, url in urls.items():
        text = re.sub(rf"http://{service}\.[a-z0-9.-]+:8000/v1", f"{url}/v1", text)
    return json.loads(text)


def post(port: int, model: str) -> tuple[int, str, str]:
    """Send one chat request through the gateway; return status, backend and model."""
    body = {"model": model, "max_tokens": 16, "temperature": 0, "messages": [{"role": "user", "content": "hello"}]}
    request = Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {GATEWAY_KEY}"},
    )
    try:
        with urlopen(request, timeout=240) as response:
            return response.status, response.headers.get("x-ewallet-backend", ""), json.loads(response.read()).get("model", "")
    except HTTPError as error:
        return error.code, "", ""
    except (URLError, TimeoutError, OSError):
        return 0, "", ""


def run_gateway(config: dict, folder: Path) -> str:
    """Start the pinned gateway on the host network and wait for it."""
    path = folder / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    path.chmod(0o644)
    folder.chmod(0o755)
    name = f"llm-ab-local-{int(time.time())}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "--network", "host", "--cpus", "1", "-v", f"{path}:/config.yaml:ro",
         "-e", f"GATEWAY_CLIENT_KEY={GATEWAY_KEY}", "-e", f"VLLM_API_KEY={BACKEND_KEY}",
         GATEWAY_IMAGE, "-f", "/config.yaml"],
        check=True, capture_output=True,
    )
    return name


def main() -> int:
    """Run the split and outage scenarios and write one summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overlay", type=Path, action="append", required=True)
    parser.add_argument("--model", default="ewallet-llm-ab")
    parser.add_argument("--expect", action="append", required=True, help="backendLabel=weight")
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--real-backend", action="append", default=[], help="service=url")
    parser.add_argument("--then", help="shell command to run while the gateway is up")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    expected = {item.split("=")[0]: int(item.split("=")[1]) for item in args.expect}
    real = dict(item.split("=", 1) for item in args.real_backend)
    stubs = {service: StubBackend() for service in ("vllm-primary", "vllm-fallback", "vllm-candidate") if service not in real}
    urls = {**real, **{service: f"http://127.0.0.1:{stub.port}" for service, stub in stubs.items()}}
    config = localise(render_config(args.overlay), urls)
    config.get("config", {}).pop("tracing", None)
    routes = config["routes"]
    port = config["gateways"]["default"]["port"]
    with tempfile.TemporaryDirectory() as raw:
        name = run_gateway(config, Path(raw))
        try:
            for _ in range(60):
                if post(port, args.model)[0] == 200:
                    break
                time.sleep(0.5)
            results = [post(port, args.model) for _ in range(args.requests)]
            summary: dict[str, object] = {
                "scope": "LOCAL routing check on the pinned gateway image; rate limits raised for this test",
                "gateway_image": GATEWAY_IMAGE,
                "route_count": len(routes),
                "model": args.model,
                "requests": len(results),
                "failed": sum(1 for status, _, _ in results if status != 200),
                "by_backend": dict(Counter(backend for status, backend, _ in results if status == 200)),
                "served_models": dict(Counter(model for status, _, model in results if status == 200)),
            }
            summary["split_check"] = asdict(split_check(summary["by_backend"], expected))
            if "vllm-candidate" in stubs:
                stubs["vllm-candidate"].stop()
                outage = [post(port, args.model) for _ in range(60)]
                summary["candidate_outage"] = {
                    "requests": len(outage),
                    "by_status": dict(Counter(str(status) for status, _, _ in outage)),
                    "by_backend": dict(Counter(backend or "none" for _, backend, _ in outage)),
                }
            if args.then:
                environment = {**os.environ, "EWALLET_LLM_API_KEY": GATEWAY_KEY, "EWALLET_GATEWAY_KEY": GATEWAY_KEY,
                               "GATEWAY_URL": f"http://127.0.0.1:{port}"}
                summary["then_exit_code"] = subprocess.run(args.then, shell=True, env=environment).returncode
        finally:
            logs = subprocess.run(["docker", "logs", "--tail", "5", name], capture_output=True, text=True)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    if summary["failed"] or not summary["split_check"]["within_tolerance"]:
        print(logs.stdout[-400:], logs.stderr[-400:])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
