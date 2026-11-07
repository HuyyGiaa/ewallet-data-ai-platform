"""Summarise one Locust run into the rubric metrics and finish its manifest.

Reads the ``Aggregated`` row of ``locust_stats.csv``. A failed request stays in the
totals, and the run is marked unusable when no request succeeded.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Mapping


def summarise(row: Mapping[str, str]) -> dict[str, float | int]:
    """Convert one Locust statistics row."""
    total = int(row["Request Count"])
    failed = int(row["Failure Count"])
    return {
        "total_requests": total,
        "successful_requests": total - failed,
        "failed_requests": failed,
        "failure_rate": round(failed / total, 6) if total else 0.0,
        "requests_per_second": round(float(row["Requests/s"]), 3),
        "average_ms": round(float(row["Average Response Time"]), 2),
        "p50_ms": float(row["50%"]),
        "p95_ms": float(row["95%"]),
        "p99_ms": float(row["99%"]),
    }


def aggregated(path: Path) -> Mapping[str, str]:
    """Return the Aggregated row of a Locust statistics file."""
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["Name"] == "Aggregated":
                return row
    raise ValueError(f"{path} has no Aggregated row")


def main() -> int:
    """Write ``summary.json`` beside the run and print it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = summarise(aggregated(args.run_dir / "locust_stats.csv"))
    summary["usable"] = summary["successful_requests"] > 0
    manifest = json.loads((args.run_dir / "run-manifest.json").read_text(encoding="utf-8"))
    (args.run_dir / "summary.json").write_text(json.dumps({**manifest, **summary}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    return 0 if summary["usable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
