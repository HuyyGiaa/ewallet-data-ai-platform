#!/usr/bin/env python3
"""Read-only, exact Source 5GB skew/cardinality profiler using PyArrow GCS.

Usage, from the repository root:
  uv run --locked --project data_platform/generation python -m ops.gcp.profile_rehearsal_5gb \\
    --output tmp/edai1-5gb-evidence/column_profile.json

Requires pyarrow with GCS support and Google Application Default Credentials.
Reads only the registered rehearsal Source Parquet; creates no cloud objects/jobs.
Peak local RAM may be several GiB because UUID distinct counts are exact.

Scope: this is the single-process scan used for the rehearsal-5gb-v1 evidence. The
dataset identity and expected counts are fixed on purpose so the result is
reproducible. Its in-memory exact counts do not scale to 100 GB; it is not a
distributed profiler.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gc
import json
import math
from pathlib import Path
import statistics
import sys

BUCKET = "ewallet-data-ai-platform-hg26-rehearsal-5gb-data"
VERSION = "rehearsal-5gb-v1"
ROOT = f"{BUCKET}/source/{VERSION}"  # PyArrow GcsFileSystem uses bucket/path, not gs://.
EXPECTED_PHYSICAL = 18_972_000
EXPECTED_LOGICAL = 18_600_000
EXPECTED_DUPLICATES = 372_000


def batches(filesystem, table: str, columns: list[str]):
    import pyarrow.dataset as ds

    dataset = ds.dataset(f"{ROOT}/{table}", filesystem=filesystem, format="parquet")
    missing = set(columns) - set(dataset.schema.names)
    if missing:
        raise RuntimeError(f"Source {table} is missing columns: {sorted(missing)}")
    print(f"[{table}] schema: {dataset.schema.names}", flush=True)
    for batch in dataset.scanner(columns=columns, batch_size=65536, use_threads=True).to_batches():
        yield batch.to_pydict()


def scan_transactions(fs):
    seen_ids: set[str] = set()
    merchant_counts: Counter[str] = Counter()
    peak_hours: Counter[int] = Counter()
    channels: Counter[str] = Counter()
    total_physical = 0
    payment_logical = 0
    schemas: dict[str, list[str]] = {}
    import pyarrow.dataset as ds

    for table in ("transactions_v1", "transactions_v2"):
        d = ds.dataset(f"{ROOT}/{table}", filesystem=fs, format="parquet")
        schemas[table] = list(d.schema.names)
        columns = ["transaction_id", "merchant_id", "type", "timestamp"]
        if table == "transactions_v2":
            columns.append("channel")
        for chunk in batches(fs, table, columns):
            for i, txid in enumerate(chunk["transaction_id"]):
                total_physical += 1
                if txid in seen_ids:
                    continue
                seen_ids.add(txid)
                mid = chunk["merchant_id"][i]
                tx_type = chunk["type"][i]
                if mid is not None and str(tx_type).lower() == "payment":
                    merchant_counts[str(mid)] += 1
                    payment_logical += 1
                dt = chunk["timestamp"][i]
                if dt is not None:
                    peak_hours[int(dt.hour)] += 1
                if table == "transactions_v2":
                    ch = chunk["channel"][i]
                    channels[str(ch) if ch is not None else "NULL"] += 1
            print(f"[{table}] scanned={total_physical:,} physical, unique={len(seen_ids):,}", flush=True)
    logical = len(seen_ids)
    duplicate_rows = total_physical - logical
    del seen_ids
    gc.collect()

    sorted_counts = sorted(merchant_counts.values(), reverse=True)
    if not sorted_counts:
        raise RuntimeError("No payment merchant records found")
    top_n = max(1, math.ceil(len(sorted_counts) * 0.05))
    top_share = sum(sorted_counts[:top_n]) / sum(sorted_counts) * 100
    median = float(statistics.median(sorted_counts))
    result = {
        "physical_transaction_rows": total_physical,
        "unique_transaction_ids": logical,
        "duplicate_rows": duplicate_rows,
        "transaction_id_unique_ratio_percent": round(logical / total_physical * 100, 6),
        "payment_transactions_logical": payment_logical,
        "merchant_skew": {
            "distinct_merchants_with_payments": len(sorted_counts),
            "top_5_percent_merchants_count": top_n,
            "top_5_percent_payment_share_percent": round(top_share, 4),
            "max_transactions_per_merchant": max(sorted_counts),
            "median_transactions_per_merchant": median,
            "max_to_median_ratio": round(max(sorted_counts) / median, 4) if median else None,
            "top_10_counts": sorted_counts[:10],
        },
        "hour_distribution_logical_transactions": dict(sorted(peak_hours.items())),
        "v2_channel_distribution_logical_transactions": dict(sorted(channels.items())),
        "schema": {
            "v1_columns": schemas["transactions_v1"],
            "v2_columns": schemas["transactions_v2"],
            "v1_has_channel": "channel" in schemas["transactions_v1"],
            "v2_has_channel": "channel" in schemas["transactions_v2"],
        },
    }
    return result


def distinct_id_table(fs, table: str, key: str) -> dict:
    seen: set[str] = set()
    total = 0
    for chunk in batches(fs, table, [key]):
        ids = chunk[key]
        total += len(ids)
        seen.update(str(x) for x in ids if x is not None)
        if total % 1_000_000 < len(ids):
            print(f"[{table}] scanned={total:,} unique={len(seen):,}", flush=True)
    result = {
        "physical_rows": total,
        f"unique_{key}": len(seen),
        "unique_ratio_percent": round(len(seen) / total * 100, 6) if total else None,
    }
    del seen
    gc.collect()
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", default="tmp/edai1-5gb-evidence/column_profile.json")
    args = p.parse_args()
    try:
        import pyarrow as pa
        import pyarrow.fs as fs
    except ImportError as e:
        raise SystemExit("PyArrow missing. Run with the data_platform/generation uv project. " + str(e))
    print(f"Read-only Source: gs://{ROOT}", flush=True)
    print(f"PyArrow {pa.__version__}; no Dataproc jobs or cloud writes", flush=True)
    gcs = fs.GcsFileSystem()
    transactions = scan_transactions(gcs)
    users = distinct_id_table(gcs, "users", "user_id")
    logins = distinct_id_table(gcs, "login_events", "login_id")
    skew = transactions["merchant_skew"]["top_5_percent_payment_share_percent"]
    checks = {
        "expected_physical_transaction_rows": transactions["physical_transaction_rows"] == EXPECTED_PHYSICAL,
        "expected_logical_transaction_ids": transactions["unique_transaction_ids"] == EXPECTED_LOGICAL,
        "expected_duplicate_rows": transactions["duplicate_rows"] == EXPECTED_DUPLICATES,
        "merchant_top5_share_in_75_to_85_percent_band": 75.0 <= skew <= 85.0,
        "v1_v2_channel_schema_evolution": (not transactions["schema"]["v1_has_channel"] and transactions["schema"]["v2_has_channel"]),
        "user_ids_unique": users["unique_ratio_percent"] == 100.0,
        "login_ids_unique": logins["unique_ratio_percent"] == 100.0,
    }
    report = {
        "dataset_version": VERSION,
        "source_uri": f"gs://{ROOT}",
        "scan_scope": "ALL source Parquet files (186 shards per physical table)",
        "scanned_at_utc": datetime.now(timezone.utc).isoformat(),
        "transaction_profile": transactions,
        "users_cardinality": users,
        "login_cardinality": logins,
        "checks": checks,
        "status": "PASS" if all(checks.values()) else "REVIEW_REQUIRED",
    }
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n======== REAL COLUMN SCAN SUMMARY ========")
    print("Source:", report["source_uri"])
    print("Transaction IDs:", transactions["unique_transaction_ids"])
    print("Physical transactions:", transactions["physical_transaction_rows"])
    print("Duplicate rows:", transactions["duplicate_rows"])
    print("Transaction unique ratio:", transactions["transaction_id_unique_ratio_percent"], "%")
    print("Distinct payment merchants:", transactions["merchant_skew"]["distinct_merchants_with_payments"])
    print("Top 5% payment share:", skew, "%")
    print("Merchant max/median ratio:", transactions["merchant_skew"]["max_to_median_ratio"])
    print("Users unique ratio:", users["unique_ratio_percent"], "%")
    print("Logins unique ratio:", logins["unique_ratio_percent"], "%")
    print("Schema evolution V1/V2:", checks["v1_v2_channel_schema_evolution"])
    for key, passed in checks.items():
        print(("PASS " if passed else "CHECK"), key)
    print("FINAL STATUS:", report["status"])
    print("Report:", destination)
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
