"""Turn load rows and replica samples into the shared run format.

Output is one measured level named ``load``, so agent experiments use the same comparison
and reports as the benchmarks. Timing that cannot be derived stays ``null``: a run where no
scale-out happened has no scale-out time, rather than a time of zero.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile."""

    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def load_rows(log: str) -> list[dict[str, Any]]:
    """Read every ``LOAD_ROW`` from a Job log."""

    return [json.loads(line.split(" ", 1)[1]) for line in log.splitlines() if line.startswith("LOAD_ROW ")]


def scaling(samples: Sequence[Mapping[str, Any]], load_start: float, load_end: float) -> dict[str, float | int | None]:
    """Derive scale-out time, scale-in time and peak replicas from ready-replica samples."""

    if not samples:
        return {"peak_replicas": None, "scale_out_seconds": None, "scale_in_seconds": None}
    before = [item["ready"] for item in samples if item["ts"] <= load_start]
    start = before[-1] if before else samples[0]["ready"]
    peak = max(item["ready"] for item in samples)
    out = next((item["ts"] - load_start for item in samples if item["ts"] >= load_start and item["ready"] > start), None)
    peak_at = max((item["ts"] for item in samples if item["ready"] == peak), default=load_end)
    back = next((item["ts"] - load_end for item in samples
                 if item["ts"] >= max(load_end, peak_at) and item["ready"] <= start), None) if peak > start else None
    return {"peak_replicas": peak, "scale_out_seconds": out, "scale_in_seconds": back}


def recovery_seconds(rows: Sequence[Mapping[str, Any]], failure_at: float | None) -> float | None:
    """Seconds from the injected failure to the end of the last failed task after it.

    Zero means no task failed. ``None`` means no failure was injected or nothing succeeded
    afterwards, so no recovery was observed.
    """

    if failure_at is None:
        return None
    after = [row for row in rows if row["finished"] >= failure_at]
    failed = [row["finished"] for row in after if not row["ok"]]
    if not failed:
        return 0.0
    last = max(failed)
    return last - failure_at if any(row["ok"] and row["finished"] > last for row in after) else None


def summarize(rows: Sequence[Mapping[str, Any]], samples: Sequence[Mapping[str, Any]], *, workload_sha256: str,
              failure_at: float | None = None) -> dict[str, Any]:
    """Build ``summary.json``; latency uses successes and failures stay in the counts."""

    if not rows:
        return {"workload_sha256": workload_sha256, "levels": {"load": {"status": "NO_REQUESTS", "requests": 0, "succeeded": 0}}}
    ok = [row["e2e_ms"] for row in rows if row["ok"]]
    start, end = min(row["started"] for row in rows), max(row["finished"] for row in rows)
    level = {
        "status": "MEASURED", "requests": len(rows), "succeeded": len(ok),
        "states": {state: sum(1 for row in rows if row["state"] == state) for state in sorted({row["state"] for row in rows})},
        "e2e_ms": {"p50": percentile(ok, 0.5), "p95": percentile(ok, 0.95), "mean": statistics.fmean(ok) if ok else None},
        "requests_per_second": len(ok) / (end - start) if end > start else None,
        "availability_percent": 100 * len(ok) / len(rows),
        "recovery_seconds": recovery_seconds(rows, failure_at),
        **scaling(samples, start, end),
    }
    return {"workload_sha256": workload_sha256, "levels": {"load": level}}


def main() -> int:
    """Write ``summary.json`` from a Job log and replica samples."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--replicas", type=Path, required=True)
    parser.add_argument("--workload-sha256", required=True)
    parser.add_argument("--failure-at", type=float)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.replicas.read_text(encoding="utf-8").splitlines() if line.strip()]
    samples = [row for row in rows if "ready" in row]
    summary = summarize(load_rows(args.log.read_text(encoding="utf-8")), samples,
                        workload_sha256=args.workload_sha256, failure_at=args.failure_at)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
