"""Unoptimized F6 offline retrieval benchmark."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd
from feast import __version__ as feast_version

from ai_platform.features.feast import FEATURE_REPO, RUNTIME_DATA_ROOT
from ai_platform.features.feast.retrieval.historical import (
    get_store,
    retrieve_combined,
    retrieve_feature_view,
)
from ai_platform.features.feast.schema import FEATURE_SPECS


def measure(name, rows, feature_count, operation) -> dict:
    started = time.perf_counter()
    output = operation()
    elapsed = time.perf_counter() - started
    return {
        "retrieval": name,
        "entity_rows": rows,
        "feature_columns": feature_count,
        "output_rows": len(output),
        "runtime_seconds": round(elapsed, 4),
        "rows_per_second": round(len(output) / elapsed, 2),
    }


def run_benchmark(
    entity_path: Path = RUNTIME_DATA_ROOT / "validation" / "benchmark_entities",
) -> dict:
    entities = pd.read_parquet(entity_path)
    store = get_store(FEATURE_REPO)
    user_entities = entities[["user_id", "event_timestamp"]]
    results = [
        measure(
            "single_user_behavior",
            len(user_entities),
            len(FEATURE_SPECS["user_behavior"]["features"]),
            lambda: retrieve_feature_view(store, "user_behavior", user_entities),
        ),
        measure(
            "combined_four_views",
            len(entities),
            sum(len(spec["features"]) for spec in FEATURE_SPECS.values()),
            lambda: retrieve_combined(store, entities),
        ),
    ]
    result = {
        "feast_version": feast_version,
        "offline_store": "DaskOfflineStore + FileSource Parquet",
        "optimized": False,
        "results": results,
    }
    print(json.dumps(result, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    result = run_benchmark()
    if args.json_output:
        args.json_output.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
