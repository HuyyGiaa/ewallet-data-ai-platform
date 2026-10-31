"""Run a controlled OpenAI-compatible streaming benchmark and preserve raw rows."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

from ai_platform.llm_agent.benchmark import (
    RequestMeasurement,
    ResourceMeasurement,
    summarize,
)


def parse_cpu(value: str) -> float:
    if value.endswith("m"):
        return float(value[:-1])
    return float(value) * 1000.0


def parse_memory(value: str) -> float:
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024}
    for suffix, multiplier in units.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * multiplier
    raise ValueError(f"unsupported kubectl memory unit: {value}")


def parse_sse(lines: Iterable[bytes], *, started: float) -> tuple[float, tuple[float, ...], dict]:
    first_content: float | None = None
    previous_content: float | None = None
    intervals: list[float] = []
    usage: dict = {}
    for raw in lines:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        payload = json.loads(data)
        if payload.get("usage"):
            usage = payload["usage"]
        choices = payload.get("choices") or []
        content = choices[0].get("delta", {}).get("content") if choices else None
        if not content:
            continue
        now = time.perf_counter()
        if first_content is None:
            first_content = now
        if previous_content is not None:
            intervals.append((now - previous_content) * 1000.0)
        previous_content = now
    if first_content is None:
        raise ValueError("stream returned no content token")
    return (first_content - started) * 1000.0, tuple(intervals), usage


def run_request(
    *, endpoint: str, api_key: str, model: str, prompt_revision: str,
    prompt: str, max_tokens: int, timeout_seconds: float,
) -> RequestMeasurement:
    request_id = str(uuid.uuid4())
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ).encode("utf-8")
    request = Request(
        endpoint.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            backend = response.headers.get("x-ewallet-backend", "unknown")
            ttft_ms, chunk_intervals, usage = parse_sse(response, started=started)
        e2e_ms = (time.perf_counter() - started) * 1000.0
        input_tokens = int(usage.get("prompt_tokens", -1))
        output_tokens = int(usage.get("completion_tokens", -1))
        total_tokens = int(usage.get("total_tokens", -1))
        if min(input_tokens, output_tokens, total_tokens) < 0:
            raise ValueError("stream did not include OpenAI usage counters")
        if output_tokens > 1:
            tpot = max(0.0, (e2e_ms - ttft_ms) / (output_tokens - 1))
            inter_token = (tpot,)
        else:
            inter_token = chunk_intervals
        return RequestMeasurement(
            request_id=request_id,
            backend=backend,
            model=model,
            prompt_revision=prompt_revision,
            e2e_ms=e2e_ms,
            ttft_ms=ttft_ms,
            inter_token_latencies_ms=inter_token,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            error=False,
            fallback=backend == "fallback",
        )
    except (HTTPError, URLError, TimeoutError, ValueError):
        e2e_ms = (time.perf_counter() - started) * 1000.0
        return RequestMeasurement(
            request_id=request_id,
            backend="error",
            model=model,
            prompt_revision=prompt_revision,
            e2e_ms=e2e_ms,
            ttft_ms=e2e_ms,
            inter_token_latencies_ms=(),
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            error=True,
            fallback=False,
        )


class KubernetesResourceSampler:
    def __init__(self, namespace: str, selector: str, interval_seconds: float = 2.0):
        self.namespace = namespace
        self.selector = selector
        self.interval_seconds = interval_seconds
        self.samples: list[ResourceMeasurement] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "KubernetesResourceSampler":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds * 2)

    def _run(self) -> None:
        while not self._stop.is_set():
            result = subprocess.run(
                [
                    "kubectl", "top", "pods", "--namespace", self.namespace,
                    "--selector", self.selector, "--no-headers",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    columns = line.split()
                    if len(columns) >= 3:
                        self.samples.append(
                            ResourceMeasurement(parse_cpu(columns[1]), parse_memory(columns[2]))
                        )
            self._stop.wait(self.interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt-revision", required=True)
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--config-revision", required=True)
    parser.add_argument("--namespace", default="ewallet-agents")
    parser.add_argument("--resource-selector", default="app.kubernetes.io/name=vllm")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=64)
    args = parser.parse_args(argv)
    if not (1 <= args.concurrency <= 16 and 1 <= args.requests <= 1000 and 0 <= args.warmup <= 100):
        parser.error("requests/concurrency/warmup are outside bounded limits")
    if len(args.git_sha) != 40 or not args.image_digest.startswith("sha256:"):
        parser.error("full Git SHA and immutable image digest are required")
    prompts = json.loads(args.prompts.read_text(encoding="utf-8"))
    if not isinstance(prompts, list) or not prompts or not all(isinstance(item, str) for item in prompts):
        parser.error("prompts must be a non-empty JSON array of strings")
    api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        parser.error("api-key-file is empty")

    call = lambda index: run_request(
        endpoint=args.endpoint,
        api_key=api_key,
        model=args.model,
        prompt_revision=args.prompt_revision,
        prompt=prompts[index % len(prompts)],
        max_tokens=args.max_tokens,
        timeout_seconds=35.0,
    )
    for index in range(args.warmup):
        call(index)
    started = time.perf_counter()
    with KubernetesResourceSampler(args.namespace, args.resource_selector) as sampler:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            rows = list(executor.map(call, range(args.requests)))
    wall_seconds = time.perf_counter() - started
    summary = summarize(rows, sampler.samples, wall_seconds=wall_seconds)
    summary["runtime_identity"] = {
        "git_sha": args.git_sha,
        "image_digest": args.image_digest,
        "model_revision": args.model_revision,
        "config_revision": args.config_revision,
        "prompt_revision": args.prompt_revision,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
