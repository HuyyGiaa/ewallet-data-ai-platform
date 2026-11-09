"""Gate between two formal sessions: is a finished session complete and valid?

Used by ``scripts/experiments/bench_chain.sh`` before it starts EXP-V2, and after EXP-V2.
An exit code of 0 from the runner is necessary but not sufficient. This reads the session's
own evidence and refuses unless every item below holds:

* ``session.log`` ends with ``session end status=0`` and ``manifest.json`` has ``finished_at``;
* the runs happened in the order AB, BA, AB (baseline-01, optimized-01, optimized-02,
  baseline-02, baseline-03, optimized-03 for three runs);
* every run of both arms has ``summary.json`` with every required level ``MEASURED`` and at
  least one success per level, a ``raw_requests.jsonl`` with at least as many rows per level
  as the summary counts, ``controls.json``, the window files and a Prometheus readback with
  every scrape target ``up``;
* ``comparison/metrics.json`` counts the required runs per arm and its verdict is a
  methodological result (IMPROVED, REGRESSED or NO_SIGNIFICANT_CHANGE; which one does not
  matter), not INCONCLUSIVE, INVALID_COMPARISON or NOT_EXECUTED;
* ``safety/cleanup.txt`` confirms the pool and namespace are absent with no VMs or disks;
* ``production-before.txt`` equals ``production-after.txt``.

The live cloud checks (pool, namespace, VMs, disks, quota) are done by the shell script.

usage: python -m ops.experiments.chain_gate --session <dir> --levels 1,2,4 --runs 3 --output gate.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

VALID_VERDICTS = ("IMPROVED", "REGRESSED", "NO_SIGNIFICANT_CHANGE")
RUN_LINE = re.compile(r"^\S+ run (\d\d) (baseline|optimized) \(")


def expected_order(runs: int) -> list[str]:
    order: list[str] = []
    for number in range(1, runs + 1):
        arms = ("baseline", "optimized") if number % 2 else ("optimized", "baseline")
        order += [f"{arm}-{number:02d}" for arm in arms]
    return order


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_run(session: Path, arm: str, run: str, levels: Sequence[str]) -> list[str]:
    """Problems with one run's evidence (empty when complete)."""

    problems: list[str] = []
    raw = session / arm / "raw" / f"run-{run}"
    metrics = session / arm / "metrics" / f"run-{run}"
    summary = _json(raw / "summary.json")
    if summary is None:
        return [f"{arm}-{run}: summary.json missing or unreadable"]
    rows: Counter[str] = Counter()
    try:
        for line in (raw / "raw_requests.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows[str(json.loads(line).get("level"))] += 1
    except (OSError, ValueError):
        problems.append(f"{arm}-{run}: raw_requests.jsonl missing or unreadable")
    for level in levels:
        data = (summary.get("levels") or {}).get(level)
        if not data:
            problems.append(f"{arm}-{run}: level {level} missing")
            continue
        if data.get("status") != "MEASURED":
            problems.append(f"{arm}-{run}: level {level} {data.get('status')}")
        if int(data.get("succeeded") or 0) == 0:
            problems.append(f"{arm}-{run}: level {level} has no successful request")
        if rows and rows[level] < int(data.get("requests") or 0):
            problems.append(f"{arm}-{run}: level {level} raw rows {rows[level]} < summary {data.get('requests')}")
    for name in ("controls.json",):
        if _json(raw / name) is None:
            problems.append(f"{arm}-{run}: {name} missing")
    for name in ("window-start.txt", "window-end.txt"):
        if not (metrics / name).is_file():
            problems.append(f"{arm}-{run}: {name} missing")
    readback = _json(metrics / "prometheus-readback.json")
    if readback is None:
        problems.append(f"{arm}-{run}: prometheus-readback.json missing")
    else:
        targets = readback.get("targets")
        down = [t for t in targets if t.get("health") != "up"] if isinstance(targets, list) else ["unreadable"]
        if not targets or down:
            problems.append(f"{arm}-{run}: scrape targets not all up: {down or 'none'}")
    return problems


def gate(session: Path, levels: Sequence[str], runs: int) -> dict[str, Any]:
    problems: list[str] = []
    log = (session / "session.log").read_text(encoding="utf-8", errors="replace") \
        if (session / "session.log").is_file() else ""
    ends = [line for line in log.splitlines() if " session end status=" in line]
    if not ends or not ends[-1].endswith("session end status=0"):
        problems.append(f"session did not end with status 0: {ends[-1] if ends else 'no end line'}")
    manifest = _json(session / "manifest.json") or {}
    if not manifest.get("finished_at"):
        problems.append("manifest.json has no finished_at")
    order = [f"{m.group(2)}-{m.group(1)}" for m in map(RUN_LINE.match, log.splitlines()) if m]
    if order != expected_order(runs):
        problems.append(f"run order {order} != {expected_order(runs)}")
    for arm in ("baseline", "optimized"):
        for number in range(1, runs + 1):
            problems += check_run(session, arm, f"{number:02d}", levels)
    comparison = _json(session / "comparison" / "metrics.json") or {}
    if comparison.get("runs") != {"baseline": runs, "optimized": runs}:
        problems.append(f"comparison runs {comparison.get('runs')}")
    if comparison.get("verdict") not in VALID_VERDICTS:
        problems.append(f"comparison verdict {comparison.get('verdict')} is not a methodological result")
    for name in ("comparison.md", "comparison.csv"):
        if not (session / "comparison" / name).is_file():
            problems.append(f"comparison/{name} missing")
    cleanup = (session / "safety" / "cleanup.txt")
    if not cleanup.is_file() or "confirmed absent" not in cleanup.read_text(encoding="utf-8"):
        problems.append("safety/cleanup.txt does not confirm cleanup")
    try:
        same = (session / "production-before.txt").read_bytes() == (session / "production-after.txt").read_bytes()
    except OSError:
        same = False
    if not same:
        problems.append("production-before.txt != production-after.txt")
    return {"schema": "ewallet-bench-chain-gate-v1", "session": str(session), "levels": list(levels),
            "runs_per_arm": runs, "run_order": order, "verdict": comparison.get("verdict"),
            "failures": comparison.get("failures"), "deviations": manifest.get("deviations"),
            "source_sha": manifest.get("source_sha"), "passed": not problems, "problems": problems}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--levels", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = gate(args.session, [x.strip() for x in args.levels.split(",") if x.strip()], args.runs)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("chain gate:", "PASS" if report["passed"] else "BLOCKED", *report["problems"], sep="\n  ")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
