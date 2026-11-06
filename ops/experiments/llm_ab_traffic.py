"""Send chat traffic to a weighted model name and verify attribution on the gateway.

Every request is kept. The backend is read from ``x-ewallet-backend`` on the response and
the model from its body, so the split reported is the one that happened. Standard library
only; the key comes from ``EWALLET_GATEWAY_KEY``.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ops.experiments.ab_stats import percentile, split_check

Send = Callable[[str], dict[str, Any]]


def http_sender(gateway: str, key: str, timeout: float = 120.0) -> Callable[[str], dict[str, Any]]:
    """Return a sender for one model name on one gateway."""

    def send(model: str) -> dict[str, Any]:
        """Send one request and return its record; failures are records too."""
        body = {"model": model, "max_tokens": 32, "temperature": 0,
                "messages": [{"role": "user", "content": "Summarise the fraud review process in one sentence."}]}
        request = Request(f"{gateway}/v1/chat/completions", data=json.dumps(body).encode(), headers={
            "content-type": "application/json", "authorization": f"Bearer {key}"})
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=timeout) as response:
                backend = response.headers.get("x-ewallet-backend", "")
                payload = json.loads(response.read())
                return {"status": response.status, "backend": backend, "served_model": payload.get("model", ""),
                        "completion_tokens": (payload.get("usage") or {}).get("completion_tokens", 0),
                        "latency_seconds": round(time.perf_counter() - started, 4)}
        except HTTPError as error:
            return {"status": error.code, "backend": "", "served_model": "", "completion_tokens": 0,
                    "latency_seconds": round(time.perf_counter() - started, 4)}
        except (URLError, TimeoutError, OSError):
            return {"status": 0, "backend": "", "served_model": "", "completion_tokens": 0,
                    "latency_seconds": round(time.perf_counter() - started, 4)}

    return send


def summarise(records: Sequence[Mapping[str, Any]], expected: Mapping[str, int]) -> dict[str, Any]:
    """Aggregate per backend; an unexpected backend fails the attribution check."""
    success = [row for row in records if row["status"] == 200]
    counts = Counter(row["backend"] for row in success)
    per_backend = {}
    for backend in counts:
        rows = [row for row in success if row["backend"] == backend]
        latencies = [row["latency_seconds"] for row in rows]
        per_backend[backend] = {
            "requests": len(rows),
            "served_models": sorted({row["served_model"] for row in rows}),
            "p50_seconds": percentile(latencies, 0.5),
            "p95_seconds": percentile(latencies, 0.95),
        }
    return {
        "requests": len(records),
        "failed": len(records) - len(success),
        "unexpected_backends": sorted(set(counts) - set(expected)),
        "split_check": asdict(split_check(counts, expected)) if success else None,
        "backends": per_backend,
    }


def main() -> int:
    """Run traffic and write the raw log and the summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--expect", action="append", required=True, help="backendLabel=weight")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    expected = {item.split("=")[0]: int(item.split("=")[1]) for item in args.expect}
    send = http_sender(args.gateway, os.environ["EWALLET_GATEWAY_KEY"])
    records = [send(args.model) for _ in range(args.requests)]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "requests.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
    summary = summarise(records, expected)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    check = summary["split_check"]
    return 0 if check and check["within_tolerance"] and not summary["failed"] and not summary["unexpected_backends"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
