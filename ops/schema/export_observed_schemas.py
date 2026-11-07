"""Export the schemas of real Delta runs from their transaction logs.

Reads only ``_delta_log`` metadata with ``gsutil``; it never reads a data file, so
the output holds column names and types but no customer value. The result is what
the diagrams are generated from, so they show what the pipeline wrote rather than
what a contract says it should write.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


OBSERVED_SCHEMA_VERSION = "ewallet-observed-schemas-v1"
FIRST_COMMIT = "00000000000000000000.json"


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


def main(argv: Sequence[str] | None = None) -> int:
    """Export the runs named on the command line to one JSON file."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--bronze-run-id", required=True)
    parser.add_argument("--silver-run-id", required=True)
    parser.add_argument("--gold-run-id", required=True)
    parser.add_argument("--feature-run-id", required=True)
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
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
