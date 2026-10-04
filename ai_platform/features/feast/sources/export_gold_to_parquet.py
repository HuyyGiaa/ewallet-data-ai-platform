"""Export the active Gold Delta snapshots to Feast-compatible Parquet.

The adapter is intentionally explicit: Feast 0.66 has no native Delta source,
and the project's Trino layer exposes timestamps at millisecond precision.
delta-rs reads the active Delta snapshot and PyArrow writes Parquet while
retaining the UTC-aware microsecond event timestamp needed by the F5 contract.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.dataset as ds
from deltalake import DeltaTable

from ai_platform.features.feast import OFFLINE_DATA_ROOT
from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.storage.scripts.config import DELTA_STORAGE_OPTIONS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=OFFLINE_DATA_ROOT)
    return parser.parse_args()


def export_gold_snapshots(output_root: Path) -> dict:
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    tables = {}
    for view_name, spec in FEATURE_SPECS.items():
        table_name = spec["table"]
        entity_key = spec["entity_key"]
        expected_columns = [entity_key, "event_timestamp", *spec["features"]]
        source = f"s3://gold-zone/{table_name}"
        target = output_root / table_name
        delta_table = DeltaTable(source, storage_options=DELTA_STORAGE_OPTIONS)
        snapshot = delta_table.to_pyarrow_dataset()
        if snapshot.schema.names != expected_columns:
            raise ValueError(
                f"{table_name} schema mismatch: {snapshot.schema.names}"
            )
        timestamp_type = str(snapshot.schema.field("event_timestamp").type)
        if timestamp_type != "timestamp[us, tz=UTC]":
            raise TypeError(
                f"{table_name}.event_timestamp is {timestamp_type}, expected "
                "timestamp[us, tz=UTC]"
            )

        source_rows = snapshot.count_rows()
        if target.exists():
            shutil.rmtree(target)
        ds.write_dataset(
            snapshot,
            str(target),
            format="parquet",
            max_rows_per_file=1_000_000,
            max_rows_per_group=100_000,
        )
        exported = ds.dataset(str(target), format="parquet")
        output_rows = exported.count_rows()
        output_timestamp_type = str(
            exported.schema.field("event_timestamp").type
        )
        if output_rows != source_rows:
            raise ValueError(
                f"{table_name} export row mismatch: {output_rows} != {source_rows}"
            )
        if output_timestamp_type != timestamp_type:
            raise ValueError(
                f"{table_name} timestamp changed: "
                f"{timestamp_type} -> {output_timestamp_type}"
            )
        tables[view_name] = {
            "source": source,
            "delta_version": delta_table.version(),
            "target": str(target.relative_to(output_root.parent.parent)),
            "entity_key": entity_key,
            "source_rows": source_rows,
            "output_rows": output_rows,
            "event_timestamp_type": output_timestamp_type,
        }
        print(f"PASS | export {table_name} | rows={output_rows}")

    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_format": "Delta Lake active snapshot",
        "output_format": "Parquet",
        "timestamp_precision": "microsecond",
        "tables": tables,
    }
    (output_root.parent / "offline_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    return manifest


def main() -> int:
    try:
        export_gold_snapshots(parse_args().output_root)
    except Exception as exc:
        print(f"ERROR | {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
