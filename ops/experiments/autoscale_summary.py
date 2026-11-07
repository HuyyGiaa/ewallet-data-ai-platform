"""Scale-up and scale-down timings of one autoscaling run, from real samples.

Inputs are the replica samples (``replica_sampler --hpa``) and the Locust run manifest,
whose ``started_at`` and ``ended_at`` bound the load. Nothing is estimated: every time comes
from a sample, so its resolution is the sampling interval.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _quantity(value: Any) -> float | None:
    """Convert a Kubernetes quantity such as ``29333m`` to a float."""
    if value is None:
        return None
    text = str(value)
    return float(text[:-1]) / 1000 if text.endswith("m") else float(text)


def summarise(samples: Iterable[Mapping[str, Any]], load_start: float, load_end: float,
              min_replicas: int = 1) -> dict[str, Any]:
    """Return the replica timeline facts the experiment reports."""
    rows = sorted((r for r in samples if "ready" in r), key=lambda r: r["ts"])
    if not rows:
        raise ValueError("no usable samples")
    before = [r for r in rows if r["ts"] < load_start]
    during = [r for r in rows if load_start <= r["ts"] <= load_end]
    after = [r for r in rows if r["ts"] > load_end]
    peak = max((r["ready"] for r in during), default=None)
    first_out = next((r["ts"] for r in during if r["ready"] > min_replicas), None)
    at_peak = next((r["ts"] for r in during if peak is not None and r["ready"] == peak), None)
    back = next((r["ts"] for r in after if r["ready"] <= min_replicas), None)
    metric_values = [v for r in during for v in ((r.get("hpa") or {}).get("metrics") or {}).values()]
    numbers = [n for n in (_quantity(v) for v in metric_values) if n is not None]
    return {
        "replicas_at_load_start": before[-1]["ready"] if before else None,
        "peak_ready_replicas": peak,
        "first_scale_out_seconds": None if first_out is None else round(first_out - load_start, 1),
        "seconds_to_peak": None if at_peak is None else round(at_peak - load_start, 1),
        "scale_down_to_min_seconds": None if back is None else round(back - load_end, 1),
        "returned_to_min": back is not None,
        "max_trigger_value": max(numbers) if numbers else None,
        "samples": len(rows),
    }


def main(argv: list[str] | None = None) -> int:
    """Write the timing summary of one run as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--min-replicas", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in args.samples.read_text(encoding="utf-8").splitlines() if line.strip()]
    result = summarise(rows, _epoch(manifest["started_at"]), _epoch(manifest["ended_at"]), args.min_replicas)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
