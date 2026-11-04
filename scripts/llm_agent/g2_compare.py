"""G2: matched B2 (standard routing) vs O2 (llm-d routing) comparison and verdict.

Inputs (written by g2_experiment.sh / g2_benchmark.py): <dir>/{b2,o2}/summary.json,
<dir>/{b2,o2}/serving.json (deploy readback) and, for O2, epp.log plus pods.json.

Fairness gate first; any failure makes the verdict INCONCLUSIVE, never a gain:
same workload digest, same levels, both runs fully MEASURED with every response
from the path under test, identical serving readback (image digest, args, env,
resources, replicas, node class, token policy) and fresh vLLM pods per run.

Rules, fixed before any run and the same as O1's (rules in the output):
  gain at a level        tokens/s >= +10 %, or TTFT p50 <= -10 % with tokens/s >= -5 %;
                         and E2E p95 <= +10 % and no more errors than B2;
  regression at a level  tokens/s <= -10 %, TTFT p50 >= +10 %, E2E p95 >= +10 %, or more errors.
Verdict: GAIN (a gain at a loaded level 4 or 8, no regression anywhere), MIXED
(gains and regressions), NO_GAIN (otherwise), INCONCLUSIVE (fairness failed).

usage: python3 scripts/llm_agent/g2_compare.py <evidence>/g2
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

RULES = {"gain": 0.10, "max_throughput_loss": 0.05, "max_tail_regression": 0.10, "loaded_levels": [4, 8]}
SERVING_KEYS = ("image", "args", "env", "resources", "replicas", "node_class", "token_rate", "request_rate")


def rel(new: float | None, old: float | None) -> float | None:
    return None if new is None or not old else new / old - 1


def fairness(b2: dict[str, Any], o2: dict[str, Any], serving: dict[str, dict[str, Any]]) -> list[str]:
    issues = []
    if b2.get("workload_sha256") != o2.get("workload_sha256"):
        issues.append("workload digest differs")
    if sorted(b2.get("levels", {})) != sorted(o2.get("levels", {})):
        issues.append("levels differ")
    for name, run in (("b2", b2), ("o2", o2)):
        for level, data in run.get("levels", {}).items():
            if data.get("status") != "MEASURED":
                issues.append(f"{name} level {level}: {data.get('status')}")
        if run.get("status") != "MEASURED":
            issues.append(f"{name}: run {run.get('status')}")
    sb, so = serving.get("b2") or {}, serving.get("o2") or {}
    if not sb or not so:
        issues.append("serving readback missing")
    else:
        for key in SERVING_KEYS:
            if sb.get(key) != so.get(key):
                issues.append(f"serving {key} differs")
        for name, s in (("b2", sb), ("o2", so)):
            if s.get("replicas") != 2 or len(s.get("pods", [])) != 2:
                issues.append(f"{name}: not exactly two primary replicas")
            if not s.get("fresh_pods"):
                issues.append(f"{name}: vLLM pods not restarted before the run")
    return issues


def compare_level(b: dict[str, Any], o: dict[str, Any]) -> dict[str, Any]:
    tps = rel(o["output_tokens_per_second"], b["output_tokens_per_second"])
    ttft = rel(o["ttft_ms"]["p50"], b["ttft_ms"]["p50"])
    tail = rel(o["e2e_ms"]["p95"], b["e2e_ms"]["p95"])
    more_errors = o["error_rate"] > b["error_rate"] or o["timeout_rate"] > b["timeout_rate"]
    gain = ((tps is not None and tps >= RULES["gain"])
            or (ttft is not None and ttft <= -RULES["gain"] and tps is not None and tps >= -RULES["max_throughput_loss"]))
    gain = gain and (tail is None or tail <= RULES["max_tail_regression"]) and not more_errors
    regression = [label for label, hit in (
        ("tokens/s", tps is not None and tps <= -RULES["gain"]),
        ("TTFT p50", ttft is not None and ttft >= RULES["gain"]),
        ("E2E p95", tail is not None and tail >= RULES["max_tail_regression"]),
        ("errors", more_errors)) if hit]
    return {"output_tokens_per_second": tps, "ttft_p50": ttft, "e2e_p95": tail,
            "error_rate": {"b2": b["error_rate"], "o2": o["error_rate"]}, "gain": gain, "regression": regression}


def balance(run: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for level, data in run.get("levels", {}).items():
        dist = data.get("distribution") or {}
        out[level] = {"pods_serving": dist.get("pods_serving"), "requests_share": dist.get("requests_share"),
                      "requests_max_share": dist.get("requests_max_share"),
                      "prompt_tokens_max_share": dist.get("prompt_tokens_max_share")}
    shares = [v["requests_max_share"] for v in out.values() if v["requests_max_share"] is not None]
    return {"per_level": out, "both_pods_served": any((v["pods_serving"] or 0) == 2 for v in out.values()),
            "mean_imbalance": (sum(s - 0.5 for s in shares) / len(shares)) if shares else None}


def epp_decisions(log_text: str, pods: dict[str, str]) -> dict[str, Any]:
    """Count the EPP's own 'Request handled' decisions per picked pod (v=3 JSON log).

    llm-d-router v1.0.0 logs OTel-style JSON: the message is under "body" and
    the endpoint is "<pod ip>:<port>" (G2 attempt 3); "msg" is accepted too.
    """

    counts: dict[str, int] = {}
    for line in log_text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if "Request handled" not in (entry.get("body"), entry.get("msg")):
            continue
        endpoint = str(entry.get("endpoint", ""))
        name = pods.get(endpoint.rsplit(":", 1)[0], endpoint or "unknown")
        counts[name] = counts.get(name, 0) + 1
    return {"decisions": sum(counts.values()), "per_pod": counts}


def compare(directory: Path) -> dict[str, Any]:
    def load(path: Path) -> Any:
        return json.loads(path.read_text()) if path.exists() else None
    b2, o2 = load(directory / "b2/summary.json") or {}, load(directory / "o2/summary.json") or {}
    serving = {name: load(directory / name / "serving.json") for name in ("b2", "o2")}
    issues = fairness(b2, o2, serving)
    levels = {}
    for level in sorted(set(b2.get("levels", {})) & set(o2.get("levels", {})), key=int):
        b, o = b2["levels"][level], o2["levels"][level]
        if b.get("status") == "MEASURED" and o.get("status") == "MEASURED":
            levels[level] = compare_level(b, o)
    gains = [lvl for lvl, r in levels.items() if r["gain"]]
    regressions = [lvl for lvl, r in levels.items() if r["regression"]]
    if issues:
        verdict = "INCONCLUSIVE"
    elif gains and regressions:
        verdict = "MIXED"
    elif any(int(lvl) in RULES["loaded_levels"] for lvl in gains):
        verdict = "GAIN"
    else:
        verdict = "NO_GAIN"
    pods_file = directory / "o2/pods.json"
    pod_ips = {}
    if pods_file.exists():
        for pod in json.loads(pods_file.read_text()).get("items", []):
            pod_ips[pod["status"].get("podIP", "")] = pod["metadata"]["name"]
    epp_log = directory / "o2/epp.log"
    return {"verdict": verdict, "fairness_issues": issues, "rules": RULES, "levels": levels,
            "gain_levels": gains, "regression_levels": regressions,
            "balance": {"b2": balance(b2), "o2": balance(o2)},
            "epp_decisions": epp_decisions(epp_log.read_text(), pod_ips) if epp_log.exists() else None}


def table(directory: Path) -> str:
    header = ("| Run | Lvl | TTFT p50/p95 (ms) | ITL p50/p95 (ms) | TPOT p50 (ms) | E2E p50/p95 (ms) | req/s | out tok/s "
              "| errors | requests per pod | vLLM CPU mean/peak per pod (m) |")
    # One delimiter cell per header cell (O1 erratum E2: a mismatch breaks the table).
    lines = [header, "|" + "---|" * (header.count("|") - 1)]
    def f(v, d=0):
        return "-" if v is None else f"{v:.{d}f}"
    for name in ("b2", "o2"):
        path = directory / name / "summary.json"
        if not path.exists():
            continue
        for level, s in json.loads(path.read_text())["levels"].items():
            if s.get("status") not in ("MEASURED", "ATTRIBUTION_MISMATCH"):
                lines.append(f"| {name} | {level} | {s.get('status')} |  |  |  |  |  |  |  |  |")
                continue
            per_pod = (s.get("distribution") or {}).get("per_pod", {})
            reqs = "/".join(f"{d['vllm:request_success']:.0f}" for d in per_pod.values())
            res = (s.get("resources") or {}).get("vllm_pods", {})
            cpu = " ; ".join(f"{f((v['cpu_m'] or {}).get('mean'))}/{f((v['cpu_m'] or {}).get('peak'))}" for v in res.values())
            ms = lambda key, q: (s.get(key) or {}).get(q)  # noqa: E731
            lines.append(f"| {name} | {level} | {f(ms('ttft_ms', 'p50'))}/{f(ms('ttft_ms', 'p95'))} | "
                         f"{f(ms('itl_ms', 'p50'))}/{f(ms('itl_ms', 'p95'))} | {f(ms('tpot_ms', 'p50'))} | "
                         f"{f(ms('e2e_ms', 'p50'))}/{f(ms('e2e_ms', 'p95'))} | {f(s.get('requests_per_second'), 3)} | "
                         f"{f(s['output_tokens_per_second'], 2)} | {s['error_rate']:.0%} | {reqs or '-'} | {cpu or '-'} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    directory = Path(argv[1])
    result = compare(directory)
    (directory / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    (directory / "comparison.md").write_text(table(directory))
    print(json.dumps({k: result[k] for k in ("verdict", "fairness_issues", "gain_levels", "regression_levels")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
