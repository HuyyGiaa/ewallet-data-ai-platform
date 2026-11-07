"""Build valid Fraud API requests from real snapshot entities.

Synthetic identifiers exist in no online store, so a real backend answers them with
``missing_feature``. This tool draws entity identifiers from the training snapshot
instead. ``verify`` then asks the deployed API once per entity and splits the list into
usable and rejected rows, keeping the counts as evidence: the snapshot and the online
store cover different windows, so not every entity can be served.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ops.experiments.ml_ab_traffic import http_sender

# After the last materialized window; the same instant the accepted N11 request used.
DEFAULT_EVENT_TIMESTAMP = "2026-06-01T00:00:00Z"
SUPPORTED_TYPES = frozenset({"payment", "deposit", "withdraw"})


def entity_rows(rows: Iterable[Mapping[str, Any]], event_timestamp: str) -> list[dict[str, Any]]:
    """Convert snapshot rows to requests, one per user.

    Transfers need a counterparty the snapshot does not hold, so they are skipped rather
    than given an invented one. A payment without a merchant is skipped for the same reason.
    """
    seen: set[str] = set()
    requests = []
    for row in rows:
        kind = row["type"]
        merchant = row.get("merchant_id")
        if row["user_id"] in seen or kind not in SUPPORTED_TYPES or (kind == "payment" and not merchant):
            continue
        seen.add(row["user_id"])
        requests.append({
            "transaction_id": f"load-{row['transaction_id']}",
            "event_timestamp": event_timestamp,
            "user_id": row["user_id"],
            "account_id": row["account_id"],
            "device_id": row["device_id"],
            "merchant_id": merchant if kind == "payment" else None,
            "type": kind,
            "amount": float(row["amount"]),
            "currency": "VND",
            "channel": row["channel"],
        })
    return requests


def verify(
    entities: Iterable[Mapping[str, Any]],
    send: Callable[[Mapping[str, Any]], tuple[int, Mapping[str, Any]]],
) -> tuple[list[Mapping[str, Any]], Counter]:
    """Return the entities the API served, and every status code seen."""
    usable = []
    statuses: Counter = Counter()
    for entity in entities:
        status, _ = send(entity)
        statuses[str(status)] += 1
        if status == 200:
            usable.append(entity)
    return usable, statuses


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    """Write one JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def main() -> int:
    """Build a fixture from a snapshot, or verify one against the API."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--snapshot", type=Path, required=True)
    build.add_argument("--split", default="test")
    build.add_argument("--limit", type=int, default=500)
    build.add_argument("--event-timestamp", default=DEFAULT_EVENT_TIMESTAMP)
    build.add_argument("--out", type=Path, required=True)
    check = sub.add_parser("verify")
    check.add_argument("--entities", type=Path, required=True)
    check.add_argument("--url", required=True)
    check.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        import pandas as pd

        frame = pd.read_parquet(next((args.snapshot / f"split={args.split}").glob("*.parquet")))
        records = frame.astype(object).where(frame.notna(), None).to_dict("records")
        rows = entity_rows(records, args.event_timestamp)[: args.limit]
        write_jsonl(args.out, rows)
        print(json.dumps({"entities": len(rows), "types": Counter(row["type"] for row in rows)}))
        return 0
    entities = [json.loads(line) for line in args.entities.read_text(encoding="utf-8").splitlines() if line.strip()]
    usable, statuses = verify(entities, http_sender(args.url))
    write_jsonl(args.out, usable)
    print(json.dumps({"checked": len(entities), "usable": len(usable), "statuses": statuses}))
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
