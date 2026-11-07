"""Summarise one serving arm and compare fixed replicas with KEDA.

Replica samples give the cost side: pod-seconds show what extra capacity the candidate
used, so a lower latency is never reported without its price. The comparison states
numbers only. Whether KEDA is an improvement stays a judgement on those numbers.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

PROFILES = ("low", "medium", "high")


def replica_stats(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return peak and final replicas, pod-seconds, and reaction times."""
    samples = [row for row in rows if "ready" in row]
    if len(samples) < 2:
        return {"samples": len(samples), "peak": None, "final": None, "pod_seconds": None,
                "seconds_to_peak": None, "seconds_from_peak_to_final": None}
    start = samples[0]["ts"]
    peak = max(row["ready"] for row in samples)
    first_peak = next(row["ts"] for row in samples if row["ready"] == peak)
    last_peak = max(row["ts"] for row in samples if row["ready"] == peak)
    final = samples[-1]["ready"]
    down = next((row["ts"] for row in samples if row["ts"] > last_peak and row["ready"] == final), None)
    pod_seconds = sum(left["ready"] * (right["ts"] - left["ts"]) for left, right in zip(samples, samples[1:]))
    return {
        "samples": len(samples), "peak": peak, "final": final, "pod_seconds": round(pod_seconds, 1),
        "seconds_to_peak": round(first_peak - start, 1) if peak > samples[0]["ready"] else None,
        "seconds_from_peak_to_final": round(down - last_peak, 1) if down and final < peak else None,
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSON-lines file, or nothing when it is absent."""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def arm_summary(arm_dir: Path, target: str) -> dict[str, Any]:
    """Collect each profile's load summary and replica statistics."""
    result: dict[str, Any] = {}
    for profile in PROFILES:
        run = arm_dir / f"{target}-internal-{profile}" / "summary.json"
        result[profile] = {
            "load": json.loads(run.read_text(encoding="utf-8")) if run.is_file() else None,
            "replicas": replica_stats(load_jsonl(arm_dir / f"{profile}-replicas.jsonl")),
        }
    return result


def compare(fixed: Mapping[str, Any], keda: Mapping[str, Any]) -> dict[str, Any]:
    """Report the candidate minus the baseline for each profile that ran in both arms."""
    fields = ("requests_per_second", "average_ms", "p50_ms", "p95_ms", "p99_ms", "failure_rate")
    result: dict[str, Any] = {}
    for profile in PROFILES:
        left, right = fixed[profile]["load"], keda[profile]["load"]
        if not left or not right:
            result[profile] = {"status": "NOT_COMPARABLE"}
            continue
        if left["fixture_sha256"] != right["fixture_sha256"] or left["source_sha"] != right["source_sha"]:
            result[profile] = {"status": "NOT_COMPARABLE", "reason": "arms differ in fixture or source"}
            continue
        result[profile] = {
            "status": "COMPARED",
            **{f"{name}_delta": round(right[name] - left[name], 4) for name in fields},
            "pod_seconds": {"fixed": fixed[profile]["replicas"]["pod_seconds"], "keda": keda[profile]["replicas"]["pod_seconds"]},
        }
    return result


def main() -> int:
    """Write one arm's summary, or compare two arm summaries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm-dir", type=Path)
    parser.add_argument("--target")
    parser.add_argument("--fixed", type=Path)
    parser.add_argument("--keda", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.arm_dir:
        result: dict[str, Any] = arm_summary(args.arm_dir, args.target)
    else:
        result = compare(json.loads(args.fixed.read_text()), json.loads(args.keda.read_text()))
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result)[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
