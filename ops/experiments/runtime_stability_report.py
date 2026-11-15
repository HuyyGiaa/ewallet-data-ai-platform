"""Summarize a runtime-stability run: driver LOAD_ROW files plus substrate_fleet recordings.

    python -m ops.experiments.runtime_stability_report --rows A.txt [B.txt ...] [--status S.jsonl ...]

Prints one JSON object: per-phase states, rpc errors (with messages), the actor status each
unpaced follow-up arrived in, actors per context (must be 1), latency percentiles, and from the
recordings the longest RESUMING/SUSPENDING stretch per actor and which workers served which
agents (golden actors, whose ids are not asr-*, are excluded). Facts only; the verdict is the
runbook's.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
from typing import Iterable


def rows(paths: Iterable[str]) -> list[dict]:
    out = []
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.startswith("LOAD_ROW "):
                out.append(json.loads(line[len("LOAD_ROW "):]))
    return out


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))], 1)


def summarize_rows(data: list[dict]) -> dict:
    by_phase: dict[str, Counter] = defaultdict(Counter)
    arrivals: Counter = Counter()
    errors: Counter = Counter()
    actors: dict[str, set] = defaultdict(set)
    latency: dict[str, list[float]] = defaultdict(list)
    for r in data:
        phase = r.get("phase", "?")
        by_phase[phase][r.get("state")] += 1
        if "status_at_send" in r:
            arrivals[f"{phase}:{r['status_at_send']}"] += 1
        if r.get("state") == "rpc_error":
            errors[str(r.get("rpc_error_message"))[:160]] += 1
        actors[r.get("context", "?")].add(r.get("actor"))
        if r.get("e2e_ms") is not None and r.get("state") not in ("rpc_error", "not_sent", "transport_error"):
            latency[phase].append(r["e2e_ms"] / 1000)
    return {
        "turns": len(data),
        "states_by_phase": {p: dict(c) for p, c in sorted(by_phase.items())},
        "terminal_without_rpc_error": sum(1 for r in data if r.get("state") in ("completed", "failed", "canceled", "rejected")),
        "rpc_errors": dict(errors),
        "arrival_status": dict(arrivals),
        "contexts": len(actors),
        "contexts_with_more_than_one_actor": sorted(c for c, a in actors.items() if len(a) > 1),
        "latency_s": {p: {"n": len(v), "p50": pct(v, 0.5), "p90": pct(v, 0.9), "max": round(max(v), 1)}
                      for p, v in sorted(latency.items()) if v},
    }


def summarize_status(paths: Iterable[str]) -> dict:
    stretch: dict[str, float] = {}
    start: dict[str, tuple[str, float]] = {}
    served: dict[str, set] = defaultdict(set)
    samples = 0
    last_t = None
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            sample = json.loads(line)
            samples += 1
            t = sample.get("t")
            last_t = t
            for agent, items in (sample.get("actors") or {}).items():
                for a in items:
                    actor = a.get("actor", "")
                    if not actor.startswith("asr-"):
                        continue
                    if a.get("pod"):
                        served[a["pod"]].add(agent)
                    status = a.get("status")
                    if status in ("Resuming", "Suspending"):
                        if actor not in start or start[actor][0] != status:
                            start[actor] = (status, t)
                        stretch[f"{actor}:{status}"] = max(stretch.get(f"{actor}:{status}", 0), t - start[actor][1])
                    else:
                        start.pop(actor, None)
    open_transitional = {a: s for a, (s, _) in start.items()}
    return {
        "samples": samples,
        "longest_transitional_s": dict(sorted(((k, round(v, 1)) for k, v in stretch.items()), key=lambda kv: -kv[1])[:10]),
        "still_transitional_at_end": open_transitional,
        "workers_serving_both_agents": sorted(p for p, a in served.items() if len(a) > 1),
        "worker_agents": {p: sorted(a) for p, a in sorted(served.items())},
        "last_sample_t": last_t,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", nargs="+", required=True)
    parser.add_argument("--status", nargs="*", default=[])
    args = parser.parse_args(argv)
    report = {"rows": summarize_rows(rows(args.rows))}
    if args.status:
        report["status"] = summarize_status(args.status)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
