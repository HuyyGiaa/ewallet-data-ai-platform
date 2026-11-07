"""Turn Locust CSV output into a B1.4 summary without changing any number.

Reads ``<prefix>_stats.csv`` and ``<prefix>_failures.csv`` and reports total
requests, throughput, p50/p95/p99 latency, failure rate and an error breakdown.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
import json
from pathlib import Path


AGGREGATED = "Aggregated"


@dataclass(frozen=True)
class EndpointStats:
    """One row of Locust's stats CSV."""

    name: str
    requests: int
    failures: int
    requests_per_second: float
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None

    @property
    def failure_rate(self) -> float:
        """Share of requests with an unexpected outcome."""
        return self.failures / self.requests if self.requests else 0.0


def _percentile(value: str) -> float | None:
    return None if value in {"", "N/A"} else float(value)


def read_stats(path: Path) -> list[EndpointStats]:
    """Parse the stats CSV, keeping the aggregated row."""
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            EndpointStats(
                name=row["Name"],
                requests=int(row["Request Count"]),
                failures=int(row["Failure Count"]),
                requests_per_second=float(row["Requests/s"]),
                p50_ms=_percentile(row["50%"]),
                p95_ms=_percentile(row["95%"]),
                p99_ms=_percentile(row["99%"]),
            )
            for row in csv.DictReader(handle)
        ]


def read_failures(path: Path) -> list[dict[str, object]]:
    """Parse the failures CSV; an absent file means no failure was recorded."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            {
                "name": row["Name"],
                "error": row["Error"],
                "occurrences": int(row["Occurrences"]),
            }
            for row in csv.DictReader(handle)
        ]


def build_summary(
    stats: list[EndpointStats],
    failures: list[dict[str, object]],
    *,
    profile: str,
) -> dict[str, object]:
    """Combine parsed rows into the evidence document."""
    aggregated = next((row for row in stats if row.name == AGGREGATED), None)
    if aggregated is None:
        raise ValueError("Locust stats contain no Aggregated row")
    return {
        "profile": profile,
        "measurement_kind": (
            "mock-backed local benchmark, not a production measurement"
            if profile == "local"
            else "integration run against real dependencies"
        ),
        "total_requests": aggregated.requests,
        "total_failures": aggregated.failures,
        "failure_rate": aggregated.failure_rate,
        "requests_per_second": aggregated.requests_per_second,
        "p50_ms": aggregated.p50_ms,
        "p95_ms": aggregated.p95_ms,
        "p99_ms": aggregated.p99_ms,
        "endpoints": [
            {**asdict(row), "failure_rate": row.failure_rate}
            for row in stats
            if row.name != AGGREGATED
        ],
        "errors": failures,
    }


def render_markdown(summary: dict[str, object]) -> str:
    """Render the summary for the evidence folder."""
    lines = [
        "# Locust summary",
        "",
        f"**Measurement:** {summary['measurement_kind']}.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total requests | {summary['total_requests']} |",
        f"| Unexpected outcomes | {summary['total_failures']} |",
        f"| Failure rate | {summary['failure_rate']:.4%} |",
        f"| Throughput (req/s) | {summary['requests_per_second']:.2f} |",
        f"| p50 (ms) | {summary['p50_ms']} |",
        f"| p95 (ms) | {summary['p95_ms']} |",
        f"| p99 (ms) | {summary['p99_ms']} |",
        "",
        "| Endpoint | Requests | Failures | req/s | p50 | p95 | p99 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in summary["endpoints"]:
        lines.append(
            f"| {row['name']} | {row['requests']} | {row['failures']} | "
            f"{row['requests_per_second']:.2f} | {row['p50_ms']} | "
            f"{row['p95_ms']} | {row['p99_ms']} |"
        )
    lines += ["", "## Errors", ""]
    if summary["errors"]:
        lines += ["| Endpoint | Error | Occurrences |", "|---|---|---|"]
        lines += [
            f"| {item['name']} | {item['error']} | {item['occurrences']} |"
            for item in summary["errors"]
        ]
    else:
        lines.append("No unexpected outcomes were recorded.")
    return "\n".join(lines) + "\n"


def main() -> None:
    """Write ``summary.json`` and ``summary.md`` next to the CSV files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-prefix", type=Path, required=True)
    parser.add_argument("--profile", default="local")
    options = parser.parse_args()

    prefix = str(options.csv_prefix)
    summary = build_summary(
        read_stats(Path(f"{prefix}_stats.csv")),
        read_failures(Path(f"{prefix}_failures.csv")),
        profile=options.profile,
    )
    directory = options.csv_prefix.parent
    (directory / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    markdown = render_markdown(summary)
    (directory / "summary.md").write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
