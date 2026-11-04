"""O1 matched benchmark for one vLLM serving configuration (through agentgateway).

The workload is generated deterministically (WORKLOAD_VERSION) and its sha256
is recorded, so every configuration runs exactly the same requests:
short and long chats, a repeated shared prefix, unique long prefixes, a
non-streamed chat and real tool calls (streamed). Per configuration:

1. cold-prefix phase: three sequential requests sharing a prefix the server has
   never seen (first = cold, next two = warm);
2. warm-up pass (not measured);
3. one measured pass per concurrency level (default 1, 2, 4), stopping at once
   when the resource guard writes the stop file.

Every request is recorded raw (TTFT, inter-token gaps, TPOT, E2E, usage,
backend, tool-call validity). vLLM's own /metrics are scraped before and after
each level (prefix-cache queries/hits, queue time, preemptions). Summaries reuse
ai_platform.llm_agent.benchmark. Never prints the API key.

usage: EWALLET_GATEWAY_KEY=... python3 scripts/llm_agent/o1_benchmark.py \
    --config b0-prime --gateway http://127.0.0.1:13000 --metrics http://127.0.0.1:18000 \
    --resources state/resources.jsonl --stop-file state/STOP --output <evidence>/o1/b0-prime
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time
from typing import Any
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_platform.llm_agent.benchmark import (  # noqa: E402
    RequestMeasurement, ResourceMeasurement, percentile, summarize)
from smoke_tool_call import TOOLS, validate_tool_calls  # noqa: E402

WORKLOAD_VERSION = "o1-workload-v1"
MODEL = "ewallet-fraud-llm"
MAX_LEVEL = 4

POLICY = ("E-wallet fraud review policy. Section {n}: a reviewer checks the account age, the device "
          "fingerprint history, the velocity of top-ups and transfers in the last hour, merchant "
          "category risk, and whether the beneficiary was added recently. Escalate when two or more "
          "signals are elevated; never invent customer data; cite the signals you used. ")
QUESTIONS = ["Which two signals matter most for a new account sending many small transfers?",
             "What should a reviewer do when only the device fingerprint is unusual?",
             "Summarize when to escalate, in one sentence.",
             "Is a recently added beneficiary alone enough to escalate?"]


def _messages(system: str | None, user: str) -> list[dict[str, str]]:
    return ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]


def build_workload() -> dict[str, Any]:
    """Deterministic workload; only its content defines the digest."""

    shared = "".join(POLICY.format(n=i) for i in range(1, 9))          # ~700 tokens, reused
    cold = "".join(POLICY.format(n=i).upper() for i in range(20, 28))   # never sent before the cold phase
    def unique(k: int) -> str:
        return "".join(POLICY.format(n=100 * k + i) for i in range(1, 9))
    long_context = " ".join(f"Transaction {i}: amount {17 * i % 900 + 10} USD, merchant M{i % 37}, "
                            f"device D{i % 11}, minutes since previous {i % 13}." for i in range(1, 61))
    tool_user = "Use feature_lookup for transaction {tx}, then summarize the returned fixture."
    def item(item_id, category, messages, *, max_tokens, stream=True, tools=False):
        body = {"model": MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": 0}
        if tools:
            body.update(tools=TOOLS, tool_choice="required")
        return {"id": item_id, "category": category, "stream": stream, "body": body}
    measured = [
        item("short-1", "short_chat", _messages(None, "In one sentence, what is a chargeback?"), max_tokens=64),
        item("short-2", "short_chat", _messages(None, "Name one sign of account takeover."), max_tokens=64),
        item("long-1", "long_chat", _messages(None, long_context + " Which transactions look unusual and why?"),
             max_tokens=128),
        item("long-2", "long_chat", _messages(None, long_context + " Group these by merchant risk."), max_tokens=128),
        *(item(f"prefix-repeat-{i}", "prefix_repeat", _messages(shared, QUESTIONS[i]), max_tokens=64)
          for i in range(3)),
        *(item(f"prefix-unique-{k}", "prefix_unique", _messages(unique(k), QUESTIONS[3]), max_tokens=64)
          for k in (1, 2)),
        item("tool-1", "tool_call", _messages(None, tool_user.format(tx="tx-o1-001")), max_tokens=64, tools=True),
        item("tool-2", "tool_call", _messages(None, tool_user.format(tx="tx-o1-002")), max_tokens=64, tools=True),
        item("nonstream-1", "nonstream_chat", _messages(None, "Explain velocity checks in two sentences."),
             max_tokens=64, stream=False),
    ]
    cold_phase = [item(f"cold-prefix-{i}", "cold_prefix", _messages(cold, QUESTIONS[i]), max_tokens=32)
                  for i in range(3)]
    warmup = [item("warmup-1", "warmup", _messages(None, "Say hello."), max_tokens=16),
              item("warmup-2", "warmup", _messages(None, tool_user.format(tx="tx-warm")), max_tokens=32, tools=True)]
    workload = {"version": WORKLOAD_VERSION, "measured": measured, "cold_prefix": cold_phase, "warmup": warmup}
    workload["sha256"] = hashlib.sha256(json.dumps(workload, sort_keys=True).encode()).hexdigest()
    return workload


def one_request(endpoint: str, key: str, item: dict[str, Any], *, timeout: float, level: int) -> dict[str, Any]:
    body = dict(item["body"], stream=item["stream"])
    if item["stream"]:
        body["stream_options"] = {"include_usage": True}
    request = Request(endpoint.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    started = time.monotonic()
    token_times: list[float] = []
    usage, backend, status, error = None, "unknown", None, None
    message: dict[str, Any] = {"content": ""}
    calls: dict[int, dict[str, Any]] = {}
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 (local port-forward only)
            status = response.status
            backend = response.headers.get("x-ewallet-backend") or "unknown"
            if item["stream"]:
                for raw in response:
                    line = raw.decode().strip()
                    if not line.startswith("data:") or line == "data: [DONE]":
                        continue
                    chunk = json.loads(line[5:])
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content") or delta.get("tool_calls"):
                            token_times.append(time.monotonic())
                        message["content"] += delta.get("content") or ""
                        for part in delta.get("tool_calls") or []:
                            call = calls.setdefault(part.get("index", 0), {"function": {"name": "", "arguments": ""}})
                            call.update({k: part[k] for k in ("id", "type") if part.get(k)})
                            fn = part.get("function") or {}
                            call["function"]["name"] += fn.get("name") or ""
                            call["function"]["arguments"] += fn.get("arguments") or ""
            else:
                payload = json.load(response)
                token_times.append(time.monotonic())
                usage = payload.get("usage")
                message = payload["choices"][0]["message"]
    except Exception as exc:  # recorded as an error row, never as a success
        error = f"{type(exc).__name__}: {exc}"[:300]
    ended = time.monotonic()
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    outputs = int((usage or {}).get("completion_tokens") or 0)
    tool_errors = validate_tool_calls(message) if item["body"].get("tools") and not error else []
    failed = bool(error) or status != 200 or not usage or outputs == 0 or bool(tool_errors)
    ttft = (token_times[0] if token_times else ended) - started
    gaps = [(b - a) * 1000 for a, b in zip(token_times, token_times[1:])]
    return {"id": item["id"], "category": item["category"], "level": level, "stream": item["stream"],
            "status": status, "backend": backend, "error": error, "tool_call_errors": tool_errors,
            "failed": failed, "timeout": bool(error and "timed out" in error),
            "ttft_ms": ttft * 1000, "e2e_ms": (ended - started) * 1000, "itl_ms": gaps,
            "tpot_ms": ((ended - token_times[0]) * 1000 / (outputs - 1)) if token_times and outputs > 1 else None,
            "input_tokens": int((usage or {}).get("prompt_tokens") or 0), "output_tokens": outputs if not failed else 0,
            "started_at": time.time() - (ended - started)}


def scrape(metrics_url: str) -> dict[str, float]:
    """Sum every series of the vLLM metric families O1 reports ({} if unreachable)."""
    try:
        return _scrape(metrics_url)
    except OSError as exc:
        print(f"metrics scrape failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {}


def _scrape(metrics_url: str) -> dict[str, float]:
    keep = ("vllm:prefix_cache_queries", "vllm:prefix_cache_hits", "vllm:request_queue_time_seconds_sum",
            "vllm:request_queue_time_seconds_count", "vllm:num_preemptions", "vllm:request_success",
            "vllm:time_to_first_token_seconds_sum", "vllm:time_to_first_token_seconds_count")
    totals: dict[str, float] = {}
    with urlopen(metrics_url.rstrip("/") + "/metrics", timeout=30) as response:  # noqa: S310
        for line in response.read().decode().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            name = line.split("{")[0].split(" ")[0]
            base = name[:-len("_total")] if name.endswith("_total") else name
            if base in keep:
                totals[base] = totals.get(base, 0.0) + float(line.rsplit(" ", 1)[1])
    return totals


def delta(before: dict[str, float], after: dict[str, float]) -> dict[str, Any]:
    d = {k: after.get(k, 0.0) - before.get(k, 0.0) for k in set(before) | set(after)}
    queries, hits = d.get("vllm:prefix_cache_queries", 0.0), d.get("vllm:prefix_cache_hits", 0.0)
    count = d.get("vllm:request_queue_time_seconds_count", 0.0)
    return {"raw": d, "prefix_cache_hit_rate": hits / queries if queries else None,
            "queue_time_mean_ms": 1000 * d.get("vllm:request_queue_time_seconds_sum", 0.0) / count if count else None,
            "preemptions": d.get("vllm:num_preemptions", 0.0)}


def p(values: list[float], q: float) -> float | None:
    return percentile(values, q) if values else None


def summarize_level(rows: list[dict[str, Any]], resources: list[dict[str, Any]], wall: float,
                    server: dict[str, Any], allocated_cpu: float) -> dict[str, Any]:
    measurements = [RequestMeasurement(
        request_id=r["id"], backend=r["backend"], model=MODEL, prompt_revision=WORKLOAD_VERSION,
        e2e_ms=r["e2e_ms"], ttft_ms=r["ttft_ms"], inter_token_latencies_ms=tuple(r["itl_ms"]),
        input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
        total_tokens=r["input_tokens"] + r["output_tokens"], error=r["failed"], fallback=r["backend"] == "fallback")
        for r in rows]
    samples = [ResourceMeasurement(cpu_millicores=s["vllm_cpu_m"], memory_mib=s["vllm_mem_mib"])
               for s in resources if s.get("vllm_mem_mib")]
    # summarize() insists on resource rows; without real guard samples the
    # resources field is reported as missing, never as a made-up value.
    core = summarize(measurements, samples or [ResourceMeasurement(cpu_millicores=0.0, memory_mib=1.0)],
                     wall_seconds=wall)
    if not samples:
        core["resources"] = None
    ok = [r for r in rows if not r["failed"]]
    itl = [g for r in ok for g in r["itl_ms"]]
    tpot = [r["tpot_ms"] for r in ok if r["tpot_ms"] is not None]
    e2e = [r["e2e_ms"] for r in ok]
    by_category = {}
    for category in sorted({r["category"] for r in rows}):
        sub = [r for r in ok if r["category"] == category]
        by_category[category] = {"n": len([r for r in rows if r["category"] == category]), "ok": len(sub),
                                 "ttft_p50_ms": p([r["ttft_ms"] for r in sub], 0.5),
                                 "e2e_p50_ms": p([r["e2e_ms"] for r in sub], 0.5)}
    out_tps = core["throughput"]["output_tokens_per_second"]
    return {
        "requests": len(rows), "succeeded": len(ok), "error_rate": core["error_rate"],
        "timeout_rate": sum(r["timeout"] for r in rows) / len(rows),
        "tool_calls_valid": sum(1 for r in rows if r["category"] == "tool_call" and not r["failed"]),
        "ttft_ms": {"p50": p([r["ttft_ms"] for r in ok], 0.5), "p95": p([r["ttft_ms"] for r in ok], 0.95)},
        "itl_ms": {"p50": p(itl, 0.5), "p95": p(itl, 0.95)},
        "tpot_ms": {"p50": p(tpot, 0.5), "p95": p(tpot, 0.95)},
        "e2e_ms": {"p50": p(e2e, 0.5), "p95": p(e2e, 0.95), "p99": p(e2e, 0.99)},
        "requests_per_second": core["throughput"]["requests_per_second"],
        "output_tokens_per_second": out_tps,
        "output_tokens_per_second_per_allocated_cpu": out_tps / allocated_cpu,
        "wall_seconds": wall, "server": server, "resources": core["resources"],
        "backends": sorted({r["backend"] for r in rows}), "by_category": by_category,
    }


def run_pass(endpoint, key, items, level, timeout, stop_file, raw_file):
    rows: list[dict[str, Any]] = []
    def go(item):
        if stop_file.exists():
            return None
        row = one_request(endpoint, key, item, timeout=timeout, level=level)
        with raw_file.open("a") as handle:
            handle.write(json.dumps(row) + "\n")
        return row
    with ThreadPoolExecutor(max_workers=level) as pool:
        rows = [r for r in pool.map(go, items) if r is not None]
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--metrics", required=True, help="vLLM primary /metrics through a local port-forward")
    parser.add_argument("--levels", default="1,2,4")
    parser.add_argument("--timeout", type=float, default=220.0)
    parser.add_argument("--allocated-cpu", type=float, default=2.0, help="vLLM primary CPU request")
    parser.add_argument("--resources", type=Path, required=True, help="JSONL written by the resource guard")
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--print-workload-digest", action="store_true")
    args = parser.parse_args(argv)
    workload = build_workload()
    if args.print_workload_digest:
        print(workload["sha256"])
        return 0
    for url in (args.gateway, args.metrics):
        if not url.startswith("http://127.0.0.1:"):
            parser.error("local port-forwards only: http://127.0.0.1:<port>")
    levels = [int(x) for x in args.levels.split(",")]
    if any(not 1 <= level <= MAX_LEVEL for level in levels):
        parser.error(f"concurrency levels must be 1-{MAX_LEVEL}")
    key = os.environ.get("EWALLET_GATEWAY_KEY") or parser.error("EWALLET_GATEWAY_KEY missing")
    args.output.mkdir(parents=True, exist_ok=True)
    raw = args.output / "raw_requests.jsonl"
    (args.output / "workload.json").write_text(json.dumps(workload, indent=1) + "\n")
    report: dict[str, Any] = {"config": args.config, "workload_version": WORKLOAD_VERSION,
                              "workload_sha256": workload["sha256"], "levels": {}, "stopped_by_guard": False}

    before = scrape(args.metrics)
    cold_rows = [one_request(args.gateway, key, item, timeout=args.timeout, level=0) for item in workload["cold_prefix"]]
    report["cold_prefix"] = {"rows": [{k: r[k] for k in ("id", "ttft_ms", "e2e_ms", "failed", "input_tokens")}
                                      for r in cold_rows], "server": delta(before, scrape(args.metrics))}
    for item in workload["warmup"]:
        one_request(args.gateway, key, item, timeout=args.timeout, level=0)

    for level in levels:
        if args.stop_file.exists():
            report["stopped_by_guard"] = True
            report["levels"][str(level)] = {"status": "BLOCKED_RESOURCE_GUARD"}
            continue
        mark = sum(1 for _ in args.resources.open()) if args.resources.exists() else 0
        before = scrape(args.metrics)
        started = time.monotonic()
        rows = run_pass(args.gateway, key, workload["measured"], level, args.timeout, args.stop_file, raw)
        wall = time.monotonic() - started
        server = delta(before, scrape(args.metrics))
        samples = [json.loads(line) for i, line in enumerate(args.resources.open()) if i >= mark] \
            if args.resources.exists() else []
        if any(r["status"] == 429 or "429" in (r["error"] or "") for r in rows):
            # The gateway's rate limit answered, not the model: no performance data.
            report["levels"][str(level)] = {"status": "BLOCKED_GATEWAY_RATE_LIMIT",
                                            "rate_limited": sum("429" in (r["error"] or "") for r in rows)}
            break
        if args.stop_file.exists() and len(rows) < len(workload["measured"]):
            report["stopped_by_guard"] = True
            report["levels"][str(level)] = {"status": "BLOCKED_RESOURCE_GUARD", "completed": len(rows)}
            continue
        report["levels"][str(level)] = dict(status="MEASURED", **summarize_level(rows, samples, wall, server,
                                                                                 args.allocated_cpu))
    (args.output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({lvl: {k: v.get(k) for k in ("status", "error_rate", "ttft_ms", "e2e_ms",
                                                  "output_tokens_per_second")}
                      for lvl, v in report["levels"].items()}, indent=1))
    measured = [v for v in report["levels"].values() if v.get("status") == "MEASURED"]
    return 0 if measured and all(v["error_rate"] == 0 for v in measured) else 1


if __name__ == "__main__":
    raise SystemExit(main())
