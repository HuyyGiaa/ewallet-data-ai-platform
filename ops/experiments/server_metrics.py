"""Server-side latency for one load run, from two Prometheus text snapshots.

The run takes a snapshot before and after; the difference of each histogram is the
traffic of that run alone. The mean (sum / count) is exact. Quantiles are interpolated
inside buckets, so they are only as fine as the buckets: a series whose buckets cannot
resolve them is reported with ``quantiles_resolved: false`` and no quantiles.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

_LINE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+(?P<value>\S+)")
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
_DROP = {"le", "otel_scope_name", "otel_scope_schema_url", "otel_scope_version"}


def parse(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Return every sample keyed by metric name and sorted labels."""
    samples = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match or line.startswith("#"):
            continue
        labels = tuple(sorted(_LABEL.findall(match.group("labels") or "")))
        samples[(match.group("name"), labels)] = float(match.group("value"))
    return samples


def _series(samples: Mapping, family: str) -> dict[tuple, dict[str, Any]]:
    out: dict[tuple, dict[str, Any]] = {}
    for (name, labels), value in samples.items():
        if not name.startswith(family + "_"):
            continue
        suffix = name[len(family) + 1:]
        key = tuple((k, v) for k, v in labels if k not in _DROP)
        item = out.setdefault(key, {"buckets": {}, "sum": 0.0, "count": 0.0})
        if suffix == "bucket":
            item["buckets"][float(dict(labels)["le"])] = value
        elif suffix in ("sum", "count"):
            item[suffix] = value
    return out


def quantile(buckets: Mapping[float, float], q: float) -> float | None:
    """Interpolate a quantile from cumulative buckets; None when buckets cannot resolve it."""
    edges = sorted(buckets)
    total = buckets[edges[-1]] if edges else 0
    if not total:
        return None
    rank, lower, below = q * total, 0.0, 0.0
    for edge in edges:
        if buckets[edge] >= rank:
            if math.isinf(edge):
                return None
            inside = buckets[edge] - below
            return lower + (edge - lower) * ((rank - below) / inside if inside else 0.0)
        lower, below = edge, buckets[edge]
    return None


def delta(before: str, after: str, family: str, where: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Summarise each series of one histogram family over the run, in milliseconds."""
    first, second = _series(parse(before), family), _series(parse(after), family)
    rows = []
    for key, end in second.items():
        labels = dict(key)
        if where and any(labels.get(k) != v for k, v in where.items()):
            continue
        start = first.get(key, {"buckets": {}, "sum": 0.0, "count": 0.0})
        count = end["count"] - start["count"]
        if count <= 0:
            continue
        buckets = {edge: end["buckets"][edge] - start["buckets"].get(edge, 0.0) for edge in end["buckets"]}
        row: dict[str, Any] = {"labels": labels, "count": int(count),
                               "mean_ms": round((end["sum"] - start["sum"]) / count * 1000, 3)}
        finite = sorted(edge for edge in buckets if not math.isinf(edge) and edge > 0)
        resolved = bool(finite) and quantile(buckets, 0.5) is not None and finite[0] <= (row["mean_ms"] / 1000)
        row["quantiles_resolved"] = resolved
        if resolved:
            for name, q in (("p50_ms", 0.5), ("p95_ms", 0.95), ("p99_ms", 0.99)):
                value = quantile(buckets, q)
                row[name] = None if value is None else round(value * 1000, 3)
        rows.append(row)
    return rows


def summarise(before: str, after: str, families: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Summarise several families from one pair of snapshots."""
    return {family: delta(before, after, family) for family in families}


def main(argv: list[str] | None = None) -> int:
    """Write the server-side summary of one run as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--family", action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = summarise(args.before.read_text(encoding="utf-8"), args.after.read_text(encoding="utf-8"), args.family)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
