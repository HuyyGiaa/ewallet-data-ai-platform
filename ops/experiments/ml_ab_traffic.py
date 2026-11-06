"""Send valid Fraud API requests and compare the observed split with the plan.

Standard library only, so it runs in a Job. Each entity row is sent ``--repeat`` times:
a user that appears on two arms is a stickiness failure. Failed requests stay in the
raw log and in the error count.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import json
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ops.experiments.ab_stats import percentile, split_check

Send = Callable[[Mapping[str, Any]], tuple[int, Mapping[str, Any]]]


def http_sender(url: str, timeout: float = 15.0) -> Send:
    """Return a sender bound to the Fraud API predict URL."""

    def send(payload: Mapping[str, Any]) -> tuple[int, Mapping[str, Any]]:
        """Post one request; a transport failure is status 0."""
        request = Request(url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, {}
        except (URLError, TimeoutError, OSError):
            return 0, {}

    return send


def run_traffic(send: Send, entities: Iterable[Mapping[str, Any]], repeat: int) -> list[dict[str, Any]]:
    """Send every entity ``repeat`` times and return one record per request."""
    records = []
    for entity in entities:
        for attempt in range(repeat):
            started = time.perf_counter()
            status, body = send({**entity, "transaction_id": f"{entity['transaction_id']}-{attempt}"})
            experiment = body.get("experiment") or {}
            records.append({
                "user_id": entity["user_id"], "status": status,
                "latency_seconds": round(time.perf_counter() - started, 6),
                "assigned_arm": experiment.get("assigned_arm"),
                "served_arm": experiment.get("served_arm"),
                "model_version": body.get("model_version"),
                "fallback": experiment.get("fallback"),
                "fraud_probability": body.get("fraud_probability"),
            })
    return records


def summarise(records: list[dict[str, Any]], configured: Mapping[str, int]) -> dict[str, Any]:
    """Aggregate per arm and check split, stickiness and attribution."""
    success = [row for row in records if row["status"] == 200]
    assigned_by_user: dict[str, set[str]] = defaultdict(set)
    for row in success:
        assigned_by_user[row["user_id"]].add(row["assigned_arm"])
    unit_assignment = Counter(next(iter(arms)) for arms in assigned_by_user.values() if len(arms) == 1)
    arms: dict[str, dict[str, Any]] = {}
    for arm in configured:
        rows = [row for row in success if row["served_arm"] == arm]
        latencies = [row["latency_seconds"] for row in rows]
        arms[arm] = {
            "requests": len(rows),
            "model_versions": sorted({str(row["model_version"]) for row in rows}),
            "p50_seconds": percentile(latencies, 0.5) if latencies else None,
            "p95_seconds": percentile(latencies, 0.95) if latencies else None,
            "mean_score": round(sum(r["fraud_probability"] for r in rows) / len(rows), 6) if rows else None,
        }
    return {
        "total_requests": len(records),
        "failed_requests": len(records) - len(success),
        "fallbacks": sum(1 for row in success if row["fallback"]),
        "non_sticky_users": sum(1 for arms_seen in assigned_by_user.values() if len(arms_seen) > 1),
        "version_leaks": sum(1 for arm in arms.values() if len(arm["model_versions"]) > 1),
        "assignment_check": asdict(split_check(unit_assignment, configured)) if unit_assignment else None,
        "arms": arms,
    }


def main() -> int:
    """Run traffic from a JSON-lines entity file and write the raw log and summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--entities", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True, help="experiment split JSON")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    entities = [json.loads(line) for line in args.entities.read_text().splitlines() if line.strip()]
    configured = {arm["name"]: arm["weight"] for arm in json.loads(args.config.read_text())["arms"]}
    records = run_traffic(http_sender(args.url), entities, args.repeat)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    summary = summarise(records, configured)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    check = summary["assignment_check"]
    healthy = check and check["within_tolerance"] and not summary["non_sticky_users"] and not summary["version_leaks"]
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
