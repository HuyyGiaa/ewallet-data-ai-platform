"""Read back what the benchmark Prometheus and Grafana show for one run window.

Runs on the operator's machine through local port-forwards, after a run's measured window.
For every dashboard query (``bench_dashboard.queries()``) it asks Prometheus for the run's
window and records how many series and points came back, so the evidence says which
panels had real data and which were empty. It also records the active scrape targets (one
per pod, which shows that two replicas are scraped separately) and, when given, checks
Grafana's provisioned dashboard and its Prometheus data source.

Read-only. It never changes Prometheus or Grafana, and it never fails the benchmark:
missing data is reported, not hidden.

usage: python -m ops.experiments.prom_readback --prometheus http://127.0.0.1:13090 \
    --start 2026-11-09T01:00:00Z --end 2026-11-09T01:20:00Z --run-id baseline-01 \
    --output <evidence>/baseline/metrics/run-01/prometheus-readback.json [--grafana http://127.0.0.1:13030]
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from ops.experiments import bench_dashboard

Fetch = Callable[[str], Any]


def http_json(url: str) -> Any:
    """GET a local URL and decode JSON."""

    with urlopen(url, timeout=30) as response:  # noqa: S310 (local port-forward only)
        return json.load(response)


def _when(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def substitute(expr: str, run_id: str, window_seconds: int) -> str:
    """Fill the dashboard variables the way Grafana does for this run."""

    # Inside a PromQL string a regex metacharacter needs a doubled backslash, and '-' none
    # (re.escape would emit '\-', which PromQL rejects).
    value = re.sub(r"([.^$*+?()\[\]{}|])", r"\\\\\1", run_id) if run_id else ".*"
    return expr.replace("$run_id", value).replace("$__range", f"{max(window_seconds, 60)}s")


def check_query(fetch: Fetch, base: str, item: dict[str, str], *, start: float, end: float,
                run_id: str) -> dict[str, Any]:
    """Run one panel query; returns its series count, point count and label sets."""

    expr = substitute(item["expr"], run_id, int(end - start))
    instant = "increase(" in item["expr"] and "$__range" in item["expr"]
    if instant:
        url = f"{base}/api/v1/query?" + urlencode({"query": expr, "time": end})
    else:
        url = f"{base}/api/v1/query_range?" + urlencode({"query": expr, "start": start, "end": end, "step": 15})
    result: dict[str, Any] = {**item, "query": expr, "mode": "instant" if instant else "range"}
    try:
        body = fetch(url)
    except (HTTPError, URLError, OSError, ValueError) as error:
        return {**result, "status": "QUERY_ERROR", "error": f"{type(error).__name__}: {error}"[:200]}
    series = body.get("data", {}).get("result", [])
    points = sum(len(entry.get("values", [])) or (1 if entry.get("value") else 0) for entry in series)
    labels = [{key: value for key, value in entry.get("metric", {}).items()
               if key in ("pod", "run_id", "arm", "container", "endpoint_name", "status", "job", "error_code")}
              for entry in series][:12]
    return {**result, "status": "DATA" if points else "EMPTY", "series": len(series), "points": points,
            "labels": labels}


def targets(fetch: Fetch, base: str) -> list[dict[str, Any]]:
    """Active scrape targets, one row per pod and port."""

    body = fetch(f"{base}/api/v1/targets?state=active")
    return [{"job": entry.get("labels", {}).get("job"), "pod": entry.get("labels", {}).get("pod"),
             "node": entry.get("labels", {}).get("node"), "run_id": entry.get("labels", {}).get("run_id"),
             "health": entry.get("health"), "last_scrape": entry.get("lastScrape"),
             "scrape_url": entry.get("scrapeUrl"), "last_error": entry.get("lastError")}
            for entry in body.get("data", {}).get("activeTargets", [])]


def grafana(fetch: Fetch, base: str) -> dict[str, Any]:
    """Check Grafana health, the provisioned dashboard and its data source path to Prometheus."""

    report: dict[str, Any] = {}
    for name, path in (("health", "/api/health"), ("dashboard", f"/api/dashboards/uid/{bench_dashboard.UID}"),
                       ("datasource_up", "/api/datasources/proxy/uid/bench-prom/api/v1/query?query=up")):
        try:
            body = fetch(base + path)
        except (HTTPError, URLError, OSError, ValueError) as error:
            report[name] = {"status": "ERROR", "error": f"{type(error).__name__}: {error}"[:200]}
            continue
        if name == "health":
            report[name] = {"status": "OK", "database": body.get("database"), "version": body.get("version")}
        elif name == "dashboard":
            model = body.get("dashboard", {})
            report[name] = {"status": "OK", "title": model.get("title"), "uid": model.get("uid"),
                            "panels": len([p for p in model.get("panels", []) if p.get("type") not in ("row", "text")])}
        else:
            report[name] = {"status": "OK", "series": len(body.get("data", {}).get("result", []))}
    return report


def readback(fetch: Fetch, prometheus: str, *, start: str, end: str, run_id: str,
             grafana_url: str | None = None) -> dict[str, Any]:
    """Build the whole readback report for one run."""

    begin, finish = _when(start), _when(end)
    panels = [check_query(fetch, prometheus.rstrip("/"), item, start=begin, end=finish, run_id=run_id)
              for item in bench_dashboard.queries()]
    report: dict[str, Any] = {
        "schema": "ewallet-bench-prometheus-readback-v1", "run_id": run_id, "window": {"start": start, "end": end},
        "dashboard_uid": bench_dashboard.UID, "panels": panels,
        "summary": {"queries": len(panels), "with_data": sum(p["status"] == "DATA" for p in panels),
                    "empty": [p["panel"] + " / " + p["legend"] for p in panels if p["status"] != "DATA"]},
    }
    try:
        report["targets"] = targets(fetch, prometheus.rstrip("/"))
    except (HTTPError, URLError, OSError, ValueError) as error:
        report["targets_error"] = f"{type(error).__name__}: {error}"[:200]
    if grafana_url:
        report["grafana"] = grafana(fetch, grafana_url.rstrip("/"))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """Write the readback JSON; always exits 0 unless the arguments are wrong."""

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prometheus", required=True)
    parser.add_argument("--grafana")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    for url in filter(None, (args.prometheus, args.grafana)):
        if not url.startswith("http://127.0.0.1:"):
            parser.error("local port-forwards only: http://127.0.0.1:<port>")
    report = readback(http_json, args.prometheus, start=args.start, end=args.end, run_id=args.run_id,
                      grafana_url=args.grafana)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = report["summary"]
    print(f"prometheus readback {args.run_id}: {summary['with_data']}/{summary['queries']} queries with data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
