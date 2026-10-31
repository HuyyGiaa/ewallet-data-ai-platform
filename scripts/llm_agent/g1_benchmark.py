"""Bounded G1 CPU benchmark through a LOCAL port-forward (agentgateway or vLLM).

Streams N chat completions (fixed prompt, max_tokens bounded), measures TTFT,
inter-token latency, end-to-end latency and usage tokens per request, and
aggregates them with ai_platform.llm_agent.benchmark.summarize together with
resource samples taken by the operator (`kubectl top`). Never prints the key.

usage: EWALLET_VLLM_API_KEY=... python3 scripts/llm_agent/g1_benchmark.py \
    --endpoint http://127.0.0.1:13000 --model ewallet-fraud-llm --requests 8 \
    --concurrency 1 --resources resources.jsonl --output benchmark.json
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ai_platform.llm_agent.benchmark import RequestMeasurement, ResourceMeasurement, summarize  # noqa: E402

PROMPT_REVISION = "g1-bench-v1"
PROMPT = "In two sentences, explain why a sudden burst of small e-wallet top-ups can indicate fraud."
MAX_REQUESTS, MAX_TOKENS = 32, 128


def one_request(endpoint: str, key: str, model: str, index: int, timeout: float) -> RequestMeasurement:
    body = {"model": model, "stream": True, "stream_options": {"include_usage": True}, "temperature": 0,
            "max_tokens": MAX_TOKENS, "messages": [{"role": "user", "content": PROMPT}]}
    request = Request(endpoint.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    started = time.monotonic()
    token_times: list[float] = []
    usage, backend, error = None, "unknown", False
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 (local port-forward only)
            backend = response.headers.get("x-ewallet-backend") or "direct"
            for raw in response:
                line = raw.decode().strip()
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                chunk = json.loads(line[5:])
                usage = chunk.get("usage") or usage
                if any((c.get("delta") or {}).get("content") for c in chunk.get("choices") or []):
                    token_times.append(time.monotonic())
    except Exception:  # recorded as an error row, never as a success
        error = True
    ended = time.monotonic()
    if not usage or not token_times:
        error = True
    inputs = int((usage or {}).get("prompt_tokens", 0))
    outputs = int((usage or {}).get("completion_tokens", 0)) if not error else 0
    gaps = tuple((b - a) * 1000 for a, b in zip(token_times, token_times[1:]))
    return RequestMeasurement(
        request_id=f"g1-bench-{index}", backend=backend, model=model, prompt_revision=PROMPT_REVISION,
        e2e_ms=(ended - started) * 1000, ttft_ms=((token_times[0] if token_times else ended) - started) * 1000,
        inter_token_latencies_ms=gaps, input_tokens=inputs, output_tokens=outputs, total_tokens=inputs + outputs,
        error=error, fallback=backend == "fallback")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--requests", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=220.0)
    parser.add_argument("--resources", type=Path, required=True,
                        help="JSON lines of {cpu_millicores, memory_mib}, sampled while the run is going")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.endpoint.startswith("http://127.0.0.1:"):
        parser.error("local port-forward only: endpoint must be http://127.0.0.1:<port>")
    if not 1 <= args.requests <= MAX_REQUESTS or not 1 <= args.concurrency <= 4:
        parser.error(f"requests must be 1-{MAX_REQUESTS} and concurrency 1-4 (bounded CPU benchmark)")
    key = os.environ.get("EWALLET_VLLM_API_KEY") or parser.error("EWALLET_VLLM_API_KEY missing")
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        rows = list(pool.map(lambda i: one_request(args.endpoint, key, args.model, i, args.timeout),
                             range(args.requests)))
    wall = time.monotonic() - started
    # Read after the run, so samples appended meanwhile are included.
    resources = [ResourceMeasurement(**json.loads(line)) for line in args.resources.read_text().splitlines() if line]
    report = summarize(rows, resources, wall_seconds=wall)
    report.update(endpoint=args.endpoint, model=args.model, concurrency=args.concurrency, max_tokens=MAX_TOKENS)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("request_count", "success_count", "latency_ms", "ttft_ms",
                                              "throughput", "error_rate", "fallback_rate")}, indent=2))
    return 0 if report["success_count"] == report["request_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
