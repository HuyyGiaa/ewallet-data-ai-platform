"""Render the O1 report (markdown) from an O1 evidence directory.

Reads comparison.json/.md (o1_compare.py), every <config>/summary.json and
window.json, capacity.json, mlops-health-{before,during,after}.json and
cleanup.txt. It only reports what those files contain; a missing file is
reported as missing.

usage: python3 scripts/llm_agent/o1_report.py <evidence-dir> > report.md
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


def load(path: Path) -> Any:
    return json.loads(path.read_text()) if path.exists() else None


def fmt(value: Any, digits: int = 0) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def main(argv: list[str]) -> int:
    evidence = Path(argv[1])
    o1 = evidence / "o1"
    comparison = load(o1 / "comparison.json") or {}
    configs = [p.name for p in sorted(o1.iterdir()) if (p / "summary.json").exists()] if o1.is_dir() else []
    out = [f"# O1 results — `{evidence.name}`", ""]
    out += [f"Selected: **{comparison.get('selected', 'MISSING')}** "
            f"(baseline valid: {comparison.get('baseline_valid')}; issues: {comparison.get('baseline_issues')})", ""]

    capacity = load(evidence / "capacity.json")
    if capacity:
        out += ["## Capacity (safe budget per node, after headroom)", "",
                "| Node | Role | Allocatable CPU / GiB | Requested CPU / GiB | Safe CPU / GiB |", "|---|---|---|---|---|"]
        for n in capacity["nodes"]:
            out.append(f"| {n['node']} | {'O1' if n['o1'] else 'MLOps'} | {n['allocatable_cpu']:.2f} / "
                       f"{n['allocatable_mem_gib']:.2f} | {n['requested_cpu']:.2f} / {n['requested_mem_gib']:.2f} | "
                       f"{n['safe_cpu']:.2f} / {n['safe_mem_gib']:.2f} |")
        out += ["", f"Verdict: {capacity['verdict']}; placement: "
                    + ", ".join(f"{p['name']}→{p['node']}" for p in capacity["placement"]), ""]

    out += ["## Matched benchmark (same workload, resources, replicas)", ""]
    table = o1 / "comparison.md"
    out += [table.read_text().strip() if table.exists() else "comparison.md MISSING", ""]

    out += ["## Cold vs warm prefix (TTFT ms of 3 requests sharing a new prefix)", "",
            "| Config | 1st (cold) | 2nd | 3rd |", "|---|---|---|---|"]
    for name in configs:
        rows = (load(o1 / name / "summary.json") or {}).get("cold_prefix", {}).get("rows", [])
        cells = [("FAILED" if r["failed"] else fmt(r["ttft_ms"])) for r in rows] + ["-"] * (3 - len(rows))
        out.append(f"| {name} | " + " | ".join(cells[:3]) + " |")
    out.append("")

    out += ["## Candidates against the selection rules", ""]
    for name, result in (comparison.get("candidates") or {}).items():
        gains = result.get("gains") or {}
        pct = lambda v: "-" if v is None else f"{v * 100:+.1f} %"  # noqa: E731  (not measured -> "-", never 0 %)
        out.append(f"- **{name}**: qualifies={result['qualifies']}; tokens/s {pct(gains.get('output_tokens_per_second'))}, "
                   f"TTFT p50 {pct(gains.get('ttft_p50'))}, E2E p95 {pct(gains.get('e2e_p95'))} "
                   f"(level {gains.get('level', '-')}); reasons: {result['reasons'] or 'none'}")
    out.append("")

    out += ["## Resource isolation and MLOps coexistence (per config window)", "",
            "| Config | Guard samples | MLOps regressions | Guard stop | vLLM restarts | CPU throttled periods | "
            "MLOps node CPU / mem peak % | MLOps pods CPU peak (m) | Gateway CPU peak (m) |", "|---|---|---|---|---|---|---|---|---|"]
    for name in configs:
        w = load(o1 / name / "window.json") or {}
        t = w.get("throttling") or {}
        out.append(f"| {name} | {w.get('samples', '-')} | {len(w.get('mlops_regressions', [])) if w else '-'} | "
                   f"{'yes' if w.get('guard_stop') else 'no'} | {w.get('vllm_restarts', '-')} | "
                   f"{t.get('nr_throttled', '-')}/{t.get('nr_periods', '-')} | {fmt(w.get('mlops_node_cpu_pct_peak'))} / "
                   f"{fmt(w.get('mlops_node_mem_pct_peak'))} | {fmt(w.get('mlops_cpu_m_peak'))} | {fmt(w.get('gateway_cpu_m_peak'))} |")
    out.append("")
    for stage in ("before", "during", "after"):
        health = load(evidence / f"mlops-health-{stage}.json")
        out.append(f"- MLOps health {stage}: " + ("MISSING" if health is None else
                   f"healthy={health['healthy']}, pods={len(health['pods'])}, "
                   f"InferenceServices={health['inferenceservices']}"))
    cleanup = evidence / "cleanup.txt"
    out += ["", "## Cleanup readback", "", "```", cleanup.read_text().strip() if cleanup.exists() else "MISSING", "```", ""]
    out += ["## Raw evidence", "", f"- per request: `o1/<config>/raw_requests.jsonl`",
            "- per level summary: `o1/<config>/summary.json`; window: `o1/<config>/window.json`",
            "- guard samples: `state/resources.jsonl`; workload: `o1/<config>/workload.json`",
            "- G1 gates on the kept config: `ops/`", ""]
    sys.stdout.write("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
