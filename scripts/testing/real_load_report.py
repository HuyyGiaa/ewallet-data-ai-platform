"""Summarise one Locust run into the rubric metrics and finish its manifest.

Reads the ``Aggregated`` statistics from ``report.html`` when it exists: Locust writes the
HTML report after the last request, while ``locust_stats.csv`` is its last periodic flush
and can miss the final second. Falls back to the CSV. Also reads the per-second
``locust_stats_history.csv`` for the peak rate. A failed request stays in the totals, and
the run is marked unusable when no request succeeded.
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
        "max_ms": round(float(row.get("Max Response Time") or 0), 2),
    }


def peak_rps(path: Path) -> float | None:
    """Highest Aggregated requests/s in the history; Locust smooths it over a short window."""
    if not path.exists():
        return None
    with path.open(encoding="utf-8", newline="") as handle:
        rates = [float(row["Requests/s"]) for row in csv.DictReader(handle) if row["Name"] == "Aggregated"]
    return round(max(rates), 3) if rates else None


def aggregated(path: Path) -> Mapping[str, str]:
    """Return the Aggregated row of a Locust statistics file."""
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["Name"] == "Aggregated":
                return row
    raise ValueError(f"{path} has no Aggregated row")


def html_aggregated(path: Path) -> Mapping[str, str] | None:
    """Return the final Aggregated row from a Locust HTML report, in the CSV's column names."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    start = text.find("window.templateArgs = ")
    if start < 0:
        return None
    args, _ = json.JSONDecoder().raw_decode(text, text.index("{", start))
    stats = {row["name"]: row for row in args.get("requests_statistics", [])}
    percentiles = {row["name"]: row for row in args.get("response_time_statistics", [])}
    if "Aggregated" not in stats or "Aggregated" not in percentiles:
        return None
    row, pct = stats["Aggregated"], percentiles["Aggregated"]
    return {"Request Count": str(row["num_requests"]), "Failure Count": str(row["num_failures"]),
            "Requests/s": str(row["total_rps"]), "Average Response Time": str(row["avg_response_time"]),
            "50%": str(pct["0.5"]), "95%": str(pct["0.95"]), "99%": str(pct["0.99"]),
            "Max Response Time": str(row["max_response_time"])}


def main() -> int:
    """Write ``summary.json`` beside the run and print it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    final = html_aggregated(args.run_dir / "report.html")
    summary = summarise(final or aggregated(args.run_dir / "locust_stats.csv"))
    summary["statistics_source"] = "report.html" if final else "locust_stats.csv"
    summary["peak_requests_per_second"] = peak_rps(args.run_dir / "locust_stats_history.csv")
    summary["usable"] = summary["successful_requests"] > 0
    manifest = json.loads((args.run_dir / "run-manifest.json").read_text(encoding="utf-8"))
    (args.run_dir / "summary.json").write_text(json.dumps({**manifest, **summary}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    return 0 if summary["usable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
