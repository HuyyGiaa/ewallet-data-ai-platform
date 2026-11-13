"""Export the schemas of real Delta runs from their transaction logs.

Reads only ``_delta_log`` metadata with ``gsutil``; it never reads a data file, so
the output holds column names and types but no customer value. The result is what
the diagrams are generated from, so they show what the pipeline wrote rather than
what a contract says it should write.

The Source zone is Parquet, not Delta. Its schema comes from the dataset manifest
and from the Parquet footer of one shard per dataset: a byte-range read of the
file's last bytes, which hold metadata only. The footer schema must hash to the
manifest's ``schema_sha256``, and the manifest must record a single schema for all
shards, so one footer stands for the whole dataset.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


OBSERVED_SCHEMA_VERSION = "ewallet-observed-schemas-v1"
FIRST_COMMIT = "00000000000000000000.json"
MANIFEST = "_control/dataset_manifest.json"
FOOTER_TAIL_BYTES = 65536
# Physical Source datasets whose logical table has another name. Bronze unions the
# two transaction schema versions into one table (schema evolution, rubric row 6).
SOURCE_LOGICAL_TABLES = {"transactions_v1": "transactions", "transactions_v2": "transactions"}


def gsutil_read(uri: str) -> str:
    """Return one object's text, raising when it cannot be read."""

    result = subprocess.run(["gsutil", "cat", uri], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"cannot read {uri}: {result.stderr[:300]}")
    return result.stdout


def gsutil_list(uri: str) -> list[str]:
    """Return the table directories directly under ``uri``."""

    result = subprocess.run(["gsutil", "ls", uri], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"cannot list {uri}: {result.stderr[:300]}")
    return sorted(line.rstrip("/").rsplit("/", 1)[-1] for line in result.stdout.split() if line.endswith("/"))


def parse_delta_metadata(commit_text: str) -> dict[str, object]:
    """Extract columns and partition columns from a Delta commit.

    Args:
        commit_text: Newline-delimited JSON of the table's first commit.

    Returns:
        Columns in table order and the partition column names.

    Raises:
        ValueError: The commit has no ``metaData`` action.
    """

    for line in commit_text.splitlines():
        if not line.strip():
            continue
        action = json.loads(line)
        if "metaData" in action:
            schema = json.loads(action["metaData"]["schemaString"])
            return {
                "columns": [
                    {"name": item["name"], "type": item["type"], "nullable": bool(item["nullable"])}
                    for item in schema["fields"]
                ],
                "partition_columns": action["metaData"].get("partitionColumns", []),
            }
    raise ValueError("Delta commit has no metaData action")


def export_zone(
    root: str,
    run_id: str,
    list_tables: Callable[[str], list[str]] = gsutil_list,
    read: Callable[[str], str] = gsutil_read,
) -> dict[str, object]:
    """Return the schema of every Delta table under one run root."""

    tables = {}
    for table in list_tables(root):
        tables[table] = parse_delta_metadata(read(f"{root}/{table}/_delta_log/{FIRST_COMMIT}"))
    return {"run_id": run_id, "root": root, "tables": tables}


def gsutil_read_tail(uri: str, size: int) -> bytes:
    """Return the last ``size`` bytes of one object."""

    result = subprocess.run(["gsutil", "cat", "-r", f"-{size}", uri], capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"cannot read the tail of {uri}: {result.stderr[:300]!r}")
    return result.stdout


def parse_parquet_footer(tail: bytes) -> tuple[list[dict[str, object]], str]:
    """Return the columns of a Parquet file and the hash the generator records.

    Args:
        tail: The last bytes of the file; they must contain the whole footer.

    Returns:
        Columns in file order, and the SHA-256 of the Arrow schema text, which is
        how the source generator fingerprints a shard's schema.

    Raises:
        ValueError: The bytes are not a Parquet tail or the footer is truncated.
    """

    import pyarrow as pa
    import pyarrow.parquet as pq

    if len(tail) < 12 or tail[-4:] != b"PAR1":
        raise ValueError("not the tail of a Parquet file")
    footer_length = struct.unpack("<I", tail[-8:-4])[0]
    if footer_length + 8 > len(tail):
        raise ValueError(f"footer of {footer_length} bytes is longer than the tail read")
    metadata = pq.read_metadata(pa.BufferReader(b"PAR1" + tail[-(footer_length + 8):]))
    schema = metadata.schema.to_arrow_schema()
    columns = [{"name": field.name, "type": str(field.type), "nullable": field.nullable} for field in schema]
    return columns, hashlib.sha256(str(schema).encode("utf-8")).hexdigest()


def export_source(
    root: str,
    read: Callable[[str], str] = gsutil_read,
    read_tail: Callable[[str, int], bytes] = gsutil_read_tail,
) -> dict[str, object]:
    """Return the Source inventory: manifest counts plus each dataset's footer schema."""

    manifest = json.loads(read(f"{root}/{MANIFEST}"))
    if manifest.get("status") != "COMPLETE":
        raise ValueError(f"source manifest status is {manifest.get('status')!r}, not COMPLETE")
    tables = {}
    for physical, entry in sorted(manifest["tables"].items()):
        if len(entry["schema_sha256"]) != 1:
            raise ValueError(f"{physical} has {len(entry['schema_sha256'])} schemas across its shards")
        first = min(entry["files"], key=lambda item: item["path"])
        tail = read_tail(f"{root}/{first['path']}", FOOTER_TAIL_BYTES)
        columns, digest = parse_parquet_footer(tail)
        if digest != entry["schema_sha256"][0]:
            raise ValueError(f"{physical} footer schema does not match the manifest schema_sha256")
        tables[physical] = {
            "logical_table": SOURCE_LOGICAL_TABLES.get(physical, physical),
            "rows": entry["rows"],
            "files": len(entry["files"]),
            "bytes": entry["bytes"],
            "schema_sha256": digest,
            "columns": columns,
        }
    return {
        "dataset_version": manifest["dataset_version"],
        "plan_digest": manifest["plan_digest"],
        "root": root,
        "tables": tables,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Export the runs named on the command line to one JSON file."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--bronze-run-id", required=True)
    parser.add_argument("--silver-run-id", required=True)
    parser.add_argument("--gold-run-id", required=True)
    parser.add_argument("--feature-run-id", required=True)
    parser.add_argument("--source-dataset", help="Source dataset version, e.g. canonical-100gb-v1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    runs = {
        "bronze": (args.bronze_run_id, "bronze"),
        "silver": (args.silver_run_id, "silver"),
        "gold": (args.gold_run_id, "gold"),
        "features": (args.feature_run_id, "gold"),
    }
    zones = {
        zone: export_zone(f"gs://{args.bucket}/lakehouse/runs/{run_id}/{folder}", run_id)
        for zone, (run_id, folder) in runs.items()
    }
    document = {
        "schema_version": OBSERVED_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "zones": zones,
    }
    if args.source_dataset:
        document["source"] = export_source(f"gs://{args.bucket}/source/{args.source_dataset}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
