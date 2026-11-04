"""Compare O1 candidates with B0-prime on matched workloads and pick one, or NO_GAIN.

Reads <dir>/<config>/summary.json (o1_benchmark.py) and <dir>/<config>/window.json
(MLOps regressions, guard stops, OOM/restarts, throttling for that config's
window). Selection rules, fixed before measuring:

- same workload digest as B0-prime and every level measured (no guard stop);
- 0 errors, 0 timeouts and every tool call valid at every level;
- no MLOps regression, no vLLM restart/OOM in the window;
- gain: output tokens/s +10 % or more, or TTFT p50 -10 % or more, at the highest
  common concurrency level, without losing more than 5 % throughput;
- tail: E2E p95 no more than 10 % worse than B0-prime at any level.

The best qualifying candidate (largest throughput gain) is selected; none -> NO_GAIN.

usage: python3 scripts/llm_agent/o1_compare.py <o1-evidence-dir> [--configs b0-prime,c1-...]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

BASE = "b0-prime"
GAIN, MAX_TPUT_LOSS, MAX_TAIL = 0.10, 0.05, 0.10


def _pct(new: float | None, old: float | None) -> float | None:
    return None if new is None or not old else (new - old) / old


def evaluate(base: dict[str, Any], cand: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    if cand["workload_sha256"] != base["workload_sha256"]:
        reasons.append("different workload")
    levels = sorted(set(base["levels"]) & set(cand["levels"]), key=int)
    for level in levels:
        c = cand["levels"][level]
        if c.get("status") != "MEASURED":
            reasons.append(f"level {level}: {c.get('status')}")
            continue
        if c["error_rate"] or c["timeout_rate"]:
            reasons.append(f"level {level}: errors {c['error_rate']:.0%}, timeouts {c['timeout_rate']:.0%}")
        if c["tool_calls_valid"] < base["levels"][level].get("tool_calls_valid", 0):
            reasons.append(f"level {level}: fewer valid tool calls")
        tail = _pct(c["e2e_ms"]["p95"], base["levels"][level]["e2e_ms"]["p95"])
        if tail is not None and tail > MAX_TAIL:
            reasons.append(f"level {level}: E2E p95 +{tail:.0%}")
    if window.get("mlops_regressions"):
        reasons.append(f"MLOps regressions: {window['mlops_regressions'][:3]}")
    if window.get("guard_stop"):
        reasons.append(f"resource guard: {window['guard_stop']}")
    if window.get("vllm_restarts") or window.get("oom_or_eviction_events"):
        reasons.append("vLLM restart/OOM/eviction in the window")
    measured = [lv for lv in levels if cand["levels"][lv].get("status") == "MEASURED"
                and base["levels"][lv].get("status") == "MEASURED"]
    top = measured[-1] if measured else None
    gains = {}
    if top:
        b, c = base["levels"][top], cand["levels"][top]
        gains = {"level": int(top),
                 "output_tokens_per_second": _pct(c["output_tokens_per_second"], b["output_tokens_per_second"]),
                 "ttft_p50": _pct(c["ttft_ms"]["p50"], b["ttft_ms"]["p50"]),
                 "e2e_p95": _pct(c["e2e_ms"]["p95"], b["e2e_ms"]["p95"])}
        tput, ttft = gains["output_tokens_per_second"] or 0.0, gains["ttft_p50"] or 0.0
        if not (tput >= GAIN or (ttft <= -GAIN and tput >= -MAX_TPUT_LOSS)):
            reasons.append(f"no defensible gain at level {top} (tokens/s {tput:+.0%}, TTFT p50 {ttft:+.0%})")
    else:
        reasons.append("no matched level")
    return {"qualifies": not reasons, "reasons": reasons, "gains": gains}


def table(summaries: dict[str, dict[str, Any]]) -> str:
    rows = ["| Config | Level | TTFT p50/p95 (ms) | ITL p50/p95 (ms) | TPOT p50 (ms) | E2E p50/p95 (ms) | req/s | out tok/s "
            "| tok/s per CPU | queue mean (ms) | prefix hit | errors | vLLM CPU mean/peak (m) | vLLM RAM peak (MiB) |",
            "|" + "---|" * 15]
    f = lambda v, d=0: "-" if v is None else f"{v:.{d}f}"  # noqa: E731
    for name, s in summaries.items():
        for level, v in sorted(s["levels"].items(), key=lambda kv: int(kv[0])):
            if v.get("status") != "MEASURED":
                rows.append(f"| {name} | {level} | {v.get('status')} |" + " |" * 12)
                continue
            res = v.get("resources") or {}
            rows.append(
                f"| {name} | {level} | {f(v['ttft_ms']['p50'])}/{f(v['ttft_ms']['p95'])} | "
                f"{f(v['itl_ms']['p50'])}/{f(v['itl_ms']['p95'])} | {f(v['tpot_ms']['p50'])} | "
                f"{f(v['e2e_ms']['p50'])}/{f(v['e2e_ms']['p95'])} | {f(v['requests_per_second'], 3)} | "
                f"{f(v['output_tokens_per_second'], 2)} | {f(v['output_tokens_per_second_per_allocated_cpu'], 2)} | "
                f"{f(v['server']['queue_time_mean_ms'])} | {f(v['server']['prefix_cache_hit_rate'], 2)} | "
                f"{v['error_rate']:.0%} | {f(res.get('cpu_millicores_mean'))}/{f(res.get('cpu_millicores_peak'))} | "
                f"{f(res.get('memory_mib_peak'))} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--configs", default="b0-prime,c1-thread-bind,c2-batched-tokens,c3-no-prefix-cache")
    args = parser.parse_args(argv)
    names = [n for n in args.configs.split(",") if (args.evidence / n / "summary.json").exists()]
    if BASE not in names:
        raise SystemExit("B0-prime summary missing: nothing to compare against")
    summaries = {n: json.loads((args.evidence / n / "summary.json").read_text()) for n in names}
    windows = {n: json.loads((args.evidence / n / "window.json").read_text())
               if (args.evidence / n / "window.json").exists() else {} for n in names}
    base_ok = evaluate(summaries[BASE], summaries[BASE], windows[BASE])
    base_ok["reasons"] = [r for r in base_ok["reasons"] if not r.startswith("no defensible gain")]
    results = {n: evaluate(summaries[BASE], summaries[n], windows[n]) for n in names if n != BASE}
    qualifying = sorted((n for n, r in results.items() if r["qualifies"]),
                        key=lambda n: -(results[n]["gains"].get("output_tokens_per_second") or 0))
    report = {"baseline": BASE, "baseline_valid": not base_ok["reasons"], "baseline_issues": base_ok["reasons"],
              "candidates": results, "selected": qualifying[0] if qualifying and not base_ok["reasons"] else "NO_GAIN",
              "cold_prefix": {n: s.get("cold_prefix") for n, s in summaries.items()},
              "rules": {"gain": GAIN, "max_throughput_loss": MAX_TPUT_LOSS, "max_tail_regression": MAX_TAIL}}
    (args.evidence / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    (args.evidence / "comparison.md").write_text(table(summaries) + "\n")
    print(table(summaries))
    print(json.dumps({"selected": report["selected"], "baseline_valid": report["baseline_valid"],
                      **{n: {"qualifies": r["qualifies"], "gains": r["gains"], "reasons": r["reasons"]}
                         for n, r in results.items()}}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
