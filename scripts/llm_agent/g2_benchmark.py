"""G2 matched routing benchmark for one routing mechanism (b2 or o2) through agentgateway.

The workload, request client and per-level summary are O1's, imported unchanged
(scripts/llm_agent/o1_benchmark.py, o1-workload-v1, same sha256), so B2 and O2
send exactly the same requests. Per run:

1. preflight: every primary pod's /metrics must expose the counters this run
   reports; a missing family stops the run (never reported as 0);
2. cold-prefix phase, warm-up (not measured), then one measured pass per
   concurrency level (default 1, 2, 4, 8);
3. per level, each primary pod's own vLLM counters are read before and after
   (kubectl exec into the pod: no port-forward per pod), giving the per-replica
   request and token distribution; the EPP's /metrics too when --epp-metrics
   is given (O2).

Every response must come from the routing path under test (agentgateway's
x-ewallet-backend header: "primary" for b2, "llm-d" for o2). A level with any
other backend (failover) is marked ATTRIBUTION_MISMATCH and is not comparable.

--budget prints the token estimate of one pass (rate-limit preflight) and exits.

usage: EWALLET_GATEWAY_KEY=... python3 scripts/llm_agent/g2_benchmark.py --config b2 \
    --gateway http://127.0.0.1:13200 --context <ctx> --pods vllm-primary-a,vllm-primary-b \
    --resources state/resources.jsonl --stop-file state/STOP --output <evidence>/g2/b2
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import o1_benchmark as o1  # noqa: E402

EXPECTED_BACKEND = {"b2": "primary", "o2": "llm-d"}
MAX_LEVEL = 8
NS = "ewallet-agents"
# Counters summed per pod (deltas per level) and the gauges/info families the
# llm-d EPP reads for vLLM (llm-d-router v1.0.0 defaultEngineConfigs "vllm").
COUNTERS = ("vllm:request_success", "vllm:prompt_tokens", "vllm:generation_tokens", "vllm:prefix_cache_queries",
            "vllm:prefix_cache_hits", "vllm:num_preemptions", "vllm:request_queue_time_seconds_sum",
            "vllm:request_queue_time_seconds_count")
REQUIRED = ("vllm:request_success", "vllm:prompt_tokens", "vllm:generation_tokens")
EPP_SIGNALS = ("vllm:num_requests_waiting", "vllm:num_requests_running", "vllm:kv_cache_usage_perc",
               "vllm:lora_requests_info", "vllm:cache_config_info")
EXEC_SCRAPE = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/metrics', timeout=20).read().decode())"


def parse_metrics(text: str) -> dict[str, float]:
    """Sum every series per family ('_total' folded), skipping comments and NaN-free lines only."""

    totals: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name = line.split("{")[0].split(" ")[0]
        base = name[:-len("_total")] if name.endswith("_total") else name
        try:
            totals[base] = totals.get(base, 0.0) + float(line.rsplit(" ", 1)[1])
        except ValueError:
            continue
    return totals


def exec_scraper(context: str, namespace: str, pod: str) -> Callable[[], str]:
    def scrape() -> str:
        return subprocess.run(["kubectl", "--context", context, "--request-timeout=30s", "-n", namespace, "exec", pod,
                               "-c", "vllm", "--", "python3", "-c", EXEC_SCRAPE],
                              capture_output=True, text=True, check=True, timeout=60).stdout
    return scrape


def url_scraper(url: str) -> Callable[[], str]:
    def scrape() -> str:
        with urlopen(url.rstrip("/") + "/metrics", timeout=20) as response:  # noqa: S310 (local port-forward)
            return response.read().decode()
    return scrape


def signal_support(text: str) -> dict[str, bool]:
    names = {line.split("{")[0].split(" ")[0] for line in text.splitlines() if line and not line.startswith("#")}
    return {s: s in names for s in EPP_SIGNALS}


def retried(scrape: Callable[[], str], attempts: int = 3, pause: float = 5.0) -> str:
    """One transient kubectl exec / port-forward failure must not lose a whole run."""
    for attempt in range(attempts):
        try:
            return scrape()
        except (OSError, subprocess.SubprocessError):
            if attempt == attempts - 1:
                raise
            time.sleep(pause)
    raise AssertionError("unreachable")


def pod_snapshot(scrapers: dict[str, Callable[[], str]]) -> dict[str, dict[str, float]]:
    out = {}
    for pod, scrape in scrapers.items():
        values = parse_metrics(retried(scrape))
        out[pod] = {k: values[k] for k in COUNTERS if k in values}
    return out


def distribution(before: dict[str, dict[str, float]], after: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Per-pod deltas and shares of requests and prompt tokens (2 pods: 0.5/0.5 is even)."""

    pods = sorted(after)
    deltas = {p: {k: after[p].get(k, 0.0) - before.get(p, {}).get(k, 0.0) for k in COUNTERS} for p in pods}
    out: dict[str, Any] = {"per_pod": deltas}
    for key, label in (("vllm:request_success", "requests"), ("vllm:prompt_tokens", "prompt_tokens"),
                       ("vllm:generation_tokens", "generation_tokens")):
        total = sum(d[key] for d in deltas.values())
        shares = {p: (deltas[p][key] / total if total else None) for p in pods}
        out[f"{label}_share"] = shares
        out[f"{label}_max_share"] = max((s for s in shares.values() if s is not None), default=None)
    out["pods_serving"] = sum(1 for d in deltas.values() if d["vllm:request_success"] > 0)
    return out


def epp_delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    keep = [k for k in after if any(s in k for s in ("request_total", "scheduler_attempts", "request_error",
                                                       "extract_errors", "poll_errors", "ready_endpoints"))]
    return {k: after[k] - before.get(k, 0.0) if "ready_endpoints" not in k else after[k] for k in sorted(keep)}


def budget(workload: dict[str, Any], levels: list[int]) -> dict[str, int]:
    """Conservative token estimate of one pass (chars/3 for prompts, plus every max_tokens)."""

    def cost(items):
        return sum(len(json.dumps(i["body"]["messages"])) // 3 + 300 * bool(i["body"].get("tools"))
                   + i["body"]["max_tokens"] for i in items)
    per_level = cost(workload["measured"])
    once = cost(workload["cold_prefix"]) + cost(workload["warmup"])
    return {"per_level": per_level, "levels": len(levels), "pass_total": once + per_level * len(levels)}


def check_attribution(rows: list[dict[str, Any]], expected: str) -> list[str]:
    return sorted({f"{r['id']}: backend={r['backend']}" for r in rows
                   if r["status"] is not None and r["backend"] != expected})


def resource_window(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Per-pod CPU mean/peak and memory peak from the G2 guard samples of one level."""

    rows = [s for s in samples if s.get("vllm_pods")]
    if not rows:
        return None
    def stats(values):
        values = [v for v in values if v is not None]
        return {"mean": sum(values) / len(values), "peak": max(values)} if values else None
    pods = sorted({p for s in rows for p in s["vllm_pods"]})
    out = {"samples": len(rows), "vllm_pods": {p: {"cpu_m": stats([s["vllm_pods"].get(p, {}).get("cpu_m") for s in rows]),
                                                   "mem_mib_peak": max(s["vllm_pods"].get(p, {}).get("mem_mib", 0)
                                                                       for s in rows)} for p in pods},
           "vllm_total_cpu_m": stats([s["vllm_cpu_m"] for s in rows]),
           "gateway_cpu_m": stats([s.get("gateway_cpu_m") for s in rows])}
    router = [s["router"] for s in rows if s.get("router")]
    if router:
        out["router"] = {c: {"cpu_m": stats([r.get(c, {}).get("cpu_m") for r in router]),
                             "mem_mib_peak": max(r.get(c, {}).get("mem_mib", 0) for r in router)}
                         for c in sorted({c for r in router for c in r})}
    return out


def main(argv: list[str] | None = None, *, scrapers: dict[str, Callable[[], str]] | None = None,
         request: Callable[..., dict[str, Any]] = o1.one_request) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", choices=sorted(EXPECTED_BACKEND))
    parser.add_argument("--gateway")
    parser.add_argument("--context")
    parser.add_argument("--namespace", default=NS)
    parser.add_argument("--pods", help="comma-separated primary vLLM pod names")
    parser.add_argument("--epp-metrics", help="EPP /metrics through a local port-forward (o2)")
    parser.add_argument("--levels", default="1,2,4,8")
    parser.add_argument("--timeout", type=float, default=220.0)
    parser.add_argument("--allocated-cpu", type=float, default=4.0, help="CPU requests of the two primaries")
    parser.add_argument("--resources", type=Path)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--budget", action="store_true")
    args = parser.parse_args(argv)
    levels = [int(x) for x in args.levels.split(",")]
    if any(not 1 <= level <= MAX_LEVEL for level in levels):
        parser.error(f"concurrency levels must be 1-{MAX_LEVEL}")
    workload = o1.build_workload()
    if args.budget:
        print(json.dumps(budget(workload, levels)))
        return 0
    for name in ("config", "gateway", "pods", "resources", "stop_file", "output"):
        if getattr(args, name) is None:
            parser.error(f"--{name.replace('_', '-')} is required")
    for url in filter(None, (args.gateway, args.epp_metrics)):
        if not url.startswith("http://127.0.0.1:"):
            parser.error("local port-forwards only: http://127.0.0.1:<port>")
    pods = [p for p in args.pods.split(",") if p]
    if len(pods) != 2:
        parser.error("G2 compares routing over exactly two primary pods")
    if scrapers is None:
        if not args.context:
            parser.error("--context is required to read the pods' metrics")
        scrapers = {p: exec_scraper(args.context, args.namespace, p) for p in pods}
    epp_url = url_scraper(args.epp_metrics) if args.epp_metrics else None
    epp = (lambda: retried(epp_url)) if epp_url else None
    key = os.environ.get("EWALLET_GATEWAY_KEY") or parser.error("EWALLET_GATEWAY_KEY missing")
    expected = EXPECTED_BACKEND[args.config]
    args.output.mkdir(parents=True, exist_ok=True)
    raw = args.output / "raw_requests.jsonl"
    (args.output / "workload.json").write_text(json.dumps(workload, indent=1) + "\n")
    report: dict[str, Any] = {"config": args.config, "expected_backend": expected, "pods": pods,
                              "workload_version": o1.WORKLOAD_VERSION, "workload_sha256": workload["sha256"],
                              "levels": {}, "stopped_by_guard": False}

    # 1. Metrics preflight: no benchmark without the counters this report is built on.
    texts = {p: retried(s) for p, s in scrapers.items()}
    missing = {p: [m for m in REQUIRED if m not in parse_metrics(t)] for p, t in texts.items()}
    report["vllm_signal_support"] = {p: signal_support(t) for p, t in texts.items()}
    if any(missing.values()):
        report["status"] = "BLOCKED_METRICS_MISSING"
        report["missing_metrics"] = missing
        (args.output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "missing": missing}))
        return 1
    if epp:
        (args.output / "epp-metrics-before.txt").write_text(epp())

    # 2. Cold prefix (sequential), warm-up, measured levels.
    before = pod_snapshot(scrapers)
    cold = [request(args.gateway, key, item, timeout=args.timeout, level=0) for item in workload["cold_prefix"]]
    report["cold_prefix"] = {"rows": [{k: r[k] for k in ("id", "ttft_ms", "e2e_ms", "failed", "input_tokens", "backend")}
                                      for r in cold], "distribution": distribution(before, pod_snapshot(scrapers))}
    warm = [request(args.gateway, key, item, timeout=args.timeout, level=0) for item in workload["warmup"]]
    report["warmup"] = [{k: r[k] for k in ("id", "failed", "backend")} for r in warm]
    for level in levels:
        if args.stop_file.exists():
            report["stopped_by_guard"] = True
            report["levels"][str(level)] = {"status": "BLOCKED_RESOURCE_GUARD"}
            continue
        mark = sum(1 for _ in args.resources.open()) if args.resources.exists() else 0
        before = pod_snapshot(scrapers)
        epp_before = parse_metrics(epp()) if epp else None
        started = time.monotonic()
        rows = o1.run_pass(args.gateway, key, workload["measured"], level, args.timeout, args.stop_file, raw) \
            if request is o1.one_request else _run_pass(request, args, key, workload["measured"], level, raw)
        wall = time.monotonic() - started
        dist = distribution(before, pod_snapshot(scrapers))
        samples = [json.loads(line) for i, line in enumerate(args.resources.open()) if i >= mark] \
            if args.resources.exists() else []
        if any(r["status"] == 429 or "429" in (r["error"] or "") for r in rows):
            report["levels"][str(level)] = {"status": "BLOCKED_GATEWAY_RATE_LIMIT",
                                            "rate_limited": sum(r["status"] == 429 or "429" in (r["error"] or "")
                                                                for r in rows)}
            break
        if args.stop_file.exists() and len(rows) < len(workload["measured"]):
            report["stopped_by_guard"] = True
            report["levels"][str(level)] = {"status": "BLOCKED_RESOURCE_GUARD", "completed": len(rows)}
            continue
        mismatch = check_attribution(rows, expected)
        summary = o1.summarize_level(rows, [], wall, {}, args.allocated_cpu)
        # O1's single-pod server and resource fields are replaced by per-pod ones.
        summary.pop("server")
        summary.pop("resources")
        level_report = dict(status="ATTRIBUTION_MISMATCH" if mismatch else "MEASURED", **summary,
                            attribution_mismatch=mismatch, distribution=dist, resources=resource_window(samples))
        if epp_before is not None:
            level_report["epp"] = epp_delta(epp_before, parse_metrics(epp()))
        report["levels"][str(level)] = level_report
    if epp:
        (args.output / "epp-metrics-after.txt").write_text(epp())
    measured = [v for v in report["levels"].values() if v.get("status") == "MEASURED"]
    report["status"] = "MEASURED" if len(measured) == len(levels) else "INCOMPLETE"
    (args.output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({lvl: {k: v.get(k) for k in ("status", "error_rate", "ttft_ms", "e2e_ms",
                                                  "output_tokens_per_second")}
                      | {"requests_share": (v.get("distribution") or {}).get("requests_share")}
                      for lvl, v in report["levels"].items()}, indent=1))
    return 0 if report["status"] == "MEASURED" and all(v["error_rate"] == 0 for v in measured) else 1


def _run_pass(request, args, key, items, level, raw_file):
    """o1.run_pass with an injected request function (offline tests)."""
    from concurrent.futures import ThreadPoolExecutor
    def go(item):
        if args.stop_file.exists():
            return None
        row = request(args.gateway, key, item, timeout=args.timeout, level=level)
        with raw_file.open("a") as handle:
            handle.write(json.dumps(row) + "\n")
        return row
    with ThreadPoolExecutor(max_workers=level) as pool:
        return [r for r in pool.map(go, items) if r is not None]


if __name__ == "__main__":
    raise SystemExit(main())
