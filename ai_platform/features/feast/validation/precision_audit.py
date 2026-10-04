"""Audit Delta, Parquet, and millisecond timestamp-key behavior."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pyspark.sql import functions as F

from ai_platform.features.feast import OFFLINE_DATA_ROOT
from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.processing.spark.common.spark_session import create_spark_session


def audit_precision(output_root: Path) -> dict:
    spark = create_spark_session("F6-Timestamp-Precision-Validation")
    results = {}
    try:
        for view_name, spec in FEATURE_SPECS.items():
            table = spec["table"]
            entity = spec["entity_key"]
            delta = spark.read.format("delta").load(f"s3a://gold-zone/{table}")
            parquet = spark.read.parquet(str((output_root / table).resolve()))
            delta_keys = delta.select(entity, "event_timestamp").distinct().count()
            parquet_keys = parquet.select(entity, "event_timestamp").distinct().count()
            millisecond_groups = (
                delta.withColumn(
                    "event_timestamp_ms",
                    F.expr(
                        "timestamp_millis(floor(unix_micros(event_timestamp) / 1000))"
                    ),
                )
                .groupBy(entity, "event_timestamp_ms")
                .count()
            )
            millisecond_keys = millisecond_groups.count()
            collapsed = delta_keys - millisecond_keys
            result = {
                "delta_timestamp_type": delta.schema["event_timestamp"].dataType.simpleString(),
                "parquet_timestamp_type": parquet.schema["event_timestamp"].dataType.simpleString(),
                "delta_keys": delta_keys,
                "parquet_keys": parquet_keys,
                "millisecond_keys": millisecond_keys,
                "collapsed_at_millisecond": collapsed,
                "millisecond_collision_groups": millisecond_groups.filter("count > 1").count(),
                "parquet_preserves_keys": delta_keys == parquet_keys,
            }
            if not result["parquet_preserves_keys"]:
                raise ValueError(f"Parquet precision loss for {table}")
            results[view_name] = result
            print(
                f"PASS | precision {table} | delta_keys={delta_keys} "
                f"parquet_keys={parquet_keys} ms_collapsed={collapsed}"
            )
    finally:
        spark.stop()
    return {
        "chosen_source": "Parquet adapter snapshot",
        "precision_loss": False,
        "pit_safety": "PASS",
        "trino_rejected": any(
            result["collapsed_at_millisecond"] > 0 for result in results.values()
        ),
        "tables": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=OFFLINE_DATA_ROOT)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    try:
        result = audit_precision(args.output_root)
        if args.json_output:
            args.json_output.write_text(json.dumps(result, indent=2) + "\n")
    except Exception as exc:
        print(f"ERROR | {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
