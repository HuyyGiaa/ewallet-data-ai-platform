"""Verified, read-only extraction of 5GB Gold ``fact_transactions`` into a local snapshot.

The source table is resolved from the verified campaign (``ops.edai1_verifier`` via
``inspect_storage_layout.load_verified_campaign``), pinned to its current Delta
version, and sampled deterministically by whole daily partitions: the most recent
``days`` ``event_date`` partitions. The extracted row count must equal the Delta
``numRecords`` statistics of exactly those files. The snapshot is written once, sorted
by ``(event_date, transaction_id)``, together with a provenance document.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

COLUMNS = ("transaction_id", "user_id", "account_id", "merchant_id", "device_id", "type", "status",
           "channel", "currency", "amount", "timestamp", "event_date")
SNAPSHOT_NAME = "fact_transactions.parquet"
PROVENANCE_NAME = "provenance.json"


class ExtractionError(RuntimeError):
    """The extracted snapshot does not match its Delta source."""


def select_partitions(partition_values: Sequence[str], days: int) -> list[str]:
    """The most recent ``days`` distinct ``event_date`` values, ascending.

    Raises:
        ValueError: ``days`` is not positive or exceeds the available partitions.
    """

    available = sorted(set(partition_values))
    if days <= 0 or days > len(available):
        raise ValueError(f"days must be within 1..{len(available)}, got {days}")
    return available[-days:]


def file_inventory(actions: Mapping[str, Sequence[Any]], selected: Sequence[str]) -> list[dict[str, object]]:
    """Active files of the selected partitions with their size and Delta row statistics."""

    chosen = set(selected)
    files = []
    for path, size, rows, partition in zip(actions["path"], actions["size_bytes"], actions["num_records"],
                                           actions["partition.event_date"]):
        if str(partition) in chosen:
            files.append({"path": path, "event_date": str(partition), "size_bytes": int(size),
                          "delta_num_records": None if rows is None else int(rows)})
    return sorted(files, key=lambda item: (item["event_date"], item["path"]))


def profile(table: Any) -> dict[str, object]:
    """Distribution facts of the snapshot that justify the workload and the index."""

    import pyarrow.compute as pc

    statuses = {item["values"]: item["counts"] for item in pc.value_counts(table["status"]).to_pylist()}
    merchants = pc.value_counts(pc.drop_null(table["merchant_id"])).to_pylist()
    counts = sorted(item["counts"] for item in merchants)

    def quantile(q: float) -> int:
        return counts[min(len(counts) - 1, int(q * (len(counts) - 1)))] if counts else 0

    return {
        "rows": table.num_rows,
        "event_date_min": str(pc.min(table["event_date"]).as_py()),
        "event_date_max": str(pc.max(table["event_date"]).as_py()),
        "status_counts": dict(sorted(statuses.items())),
        "rows_with_merchant": int(sum(counts)),
        "distinct_merchants": len(counts),
        "rows_per_merchant_quantiles": {"p0": quantile(0.0), "p50": quantile(0.5), "p90": quantile(0.9),
                                        "p99": quantile(0.99), "p100": quantile(1.0)},
    }


def extract(binding: Any, uri: str, *, days: int, output_dir: Path) -> dict[str, object]:
    """Extract the snapshot and its provenance into a new ``output_dir``.

    Raises:
        ExtractionError: The extracted rows differ from the Delta statistics.
        FileExistsError: ``output_dir`` already holds a snapshot.
    """

    import pyarrow.parquet as pq
    from deltalake import DeltaTable

    snapshot = output_dir / SNAPSHOT_NAME
    if snapshot.exists():
        raise FileExistsError(f"{snapshot} exists; snapshots are immutable")
    delta = DeltaTable(uri)
    version = int(delta.version())
    actions = delta.get_add_actions(flatten=True).to_pydict()
    selected = select_partitions([str(value) for value in actions["partition.event_date"]], days)
    files = file_inventory(actions, selected)
    if any(item["delta_num_records"] is None for item in files):
        raise ExtractionError("a selected file has no numRecords statistic; the row count cannot be verified")
    expected_rows = sum(int(item["delta_num_records"]) for item in files)
    table = delta.to_pyarrow_dataset(partitions=[("event_date", "in", selected)]).to_table(columns=list(COLUMNS))
    if table.num_rows != expected_rows:
        raise ExtractionError(f"extracted {table.num_rows} rows, Delta statistics say {expected_rows}")
    table = table.sort_by([("event_date", "ascending"), ("transaction_id", "ascending")])
    output_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, snapshot, compression="zstd")
    provenance = {
        "source": {**binding.to_dict(), "table": "gold.fact_transactions", "uri": uri, "delta_version": version,
                   "table_rows_total": int(sum(int(value or 0) for value in actions["num_records"])),
                   "table_partitions_total": len(set(actions["partition.event_date"]))},
        "sample": {"method": f"most recent {days} event_date partitions (whole partitions, no row sampling)",
                   "days": days, "partitions": selected, "files": files, "expected_rows": expected_rows},
        "snapshot": {"file": SNAPSHOT_NAME, "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                     "bytes": snapshot.stat().st_size, "rows": table.num_rows, "columns": list(COLUMNS),
                     "arrow_schema": [f"{field.name}: {field.type}" for field in table.schema],
                     "sort_order": ["event_date", "transaction_id"]},
        "profile": profile(table),
    }
    (output_dir / PROVENANCE_NAME).write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n",
                                              encoding="utf-8")
    return provenance
