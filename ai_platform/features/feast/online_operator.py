"""Operate the Feast Online (Redis) store from the immutable offline snapshot.

Subcommands:

* ``apply`` registers the Block A online FeatureViews in a NEW online registry
  (create-only). The offline registry and snapshot are never written.
* ``materialize`` loads ``[start, end]`` through native
  ``FeatureStore.materialize`` with the bounded Dask reader.
* ``materialize-incremental`` continues every view from the end time Feast
  recorded in the registry; it refuses views that were never materialized.
* ``parity`` compares Feast Online values with Feast Offline PIT retrieval at
  the cutover for a deterministic entity sample per FeatureView.

Every subcommand prints one JSON evidence document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import pandas as pd
from feast import FeatureStore

from ai_platform.features.feast.retrieval.bounded_dask import (
    bounded_dask_source,
    build_bounded_source_plan,
    build_time_window_plan,
)
from ai_platform.features.feast.schema import FEATURE_SPECS


ONLINE_REPO = Path(__file__).resolve().parent / "online_repo"
MISSING_ENTITY_PROBE = "__parity_missing_entity__"


def _timestamp(value: str) -> datetime:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must include a UTC offset")
    return parsed.tz_convert("UTC").to_pydatetime()


def get_online_store() -> FeatureStore:
    for name in (
        "EWALLET_FEAST_REGISTRY",
        "EWALLET_FEAST_OFFLINE_DATA_ROOT",
        "EWALLET_REDIS_CONNECTION_STRING",
    ):
        if not os.environ.get(name):
            raise SystemExit(f"{name} is required")
    return FeatureStore(repo_path=str(ONLINE_REPO))


def _registry_absent(uri: str) -> None:
    if not uri.startswith("gs://"):
        if Path(uri).exists():
            raise FileExistsError(f"online registry already exists: {uri}")
        return
    from google.cloud import storage

    bucket, _, name = uri[5:].partition("/")
    if storage.Client().bucket(bucket).blob(name).exists():
        raise FileExistsError(f"online registry already exists: {uri}")


def apply(store: FeatureStore) -> dict[str, object]:
    from ai_platform.features.feast.online_repo.feature_definitions import (
        build_online_feature_objects,
    )

    _registry_absent(os.environ["EWALLET_FEAST_REGISTRY"])
    entities, _, push_sources, views = build_online_feature_objects()
    store.apply([*entities.values(), *push_sources.values(), *views.values()])
    applied = sorted(view.name for view in store.list_feature_views())
    if applied != sorted(FEATURE_SPECS):
        raise RuntimeError(f"unexpected online FeatureViews: {applied}")
    return {"feature_views": applied, "registry": os.environ["EWALLET_FEAST_REGISTRY"]}


def _materialize_view(
    store: FeatureStore, view_name: str, start: datetime, end: datetime
) -> dict[str, object]:
    plan = build_time_window_plan(store, view_name, start, end)
    started = perf_counter()
    with bounded_dask_source(plan) as metrics:
        store.materialize(start, end, feature_views=[view_name])
    view = store.get_feature_view(view_name, allow_registry_cache=False)
    return {
        "view": view_name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "seconds": round(perf_counter() - started, 3),
        "peak_rss_bytes": metrics.get("peak_rss_after_bytes"),
        "registry_intervals": [
            [interval[0].isoformat(), interval[1].isoformat()]
            for interval in view.materialization_intervals
        ],
    }


def materialize(store: FeatureStore, start: datetime, end: datetime) -> dict[str, object]:
    return {
        "mode": "window",
        "views": [_materialize_view(store, name, start, end) for name in FEATURE_SPECS],
    }


def materialize_incremental(store: FeatureStore, end: datetime) -> dict[str, object]:
    results = []
    for name in FEATURE_SPECS:
        view = store.get_feature_view(name, allow_registry_cache=False)
        start = view.most_recent_end_time
        if start is None:
            raise RuntimeError(
                f"{name} was never materialized; run an explicit window first"
            )
        start = start.astimezone(timezone.utc)
        if not start < end:
            results.append({"view": name, "skipped": "already current", "end": start.isoformat()})
            continue
        results.append(_materialize_view(store, name, start, end))
    return {"mode": "incremental", "views": results}


def _sample_entities(
    store: FeatureStore, view_name: str, window_start: datetime, cutover: datetime, size: int
) -> list[str]:
    plan = build_time_window_plan(store, view_name, window_start, cutover)
    source = store.get_feature_view(view_name).batch_source
    from feast.infra.offline_stores import dask as feast_dask

    with bounded_dask_source(plan):
        frame = feast_dask._read_datasource(source, store.repo_path)
        keys = frame[plan.entity_key].dropna().unique().compute()
    ranked = sorted(
        (str(key) for key in keys),
        key=lambda key: hashlib.sha256(f"{view_name}:{key}".encode()).hexdigest(),
    )
    return ranked[:size]


def _same(left: object, right: object) -> bool:
    if left is None or (isinstance(left, float) and math.isnan(left)):
        return right is None or (isinstance(right, float) and math.isnan(right))
    if isinstance(left, float) or isinstance(right, float):
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
    return left == right


def parity(
    store: FeatureStore, cutover: datetime, sample_window_start: datetime, sample_size: int
) -> dict[str, object]:
    views = []
    total = mismatches = 0
    for view_name, spec in FEATURE_SPECS.items():
        entity_key = spec["entity_key"]
        refs = [f"{view_name}:{feature}" for feature in spec["features"]]
        entities = _sample_entities(store, view_name, sample_window_start, cutover, sample_size)
        if not entities:
            raise RuntimeError(f"no {view_name} entities in the sample window")
        online = store.get_online_features(
            features=refs,
            entity_rows=[{entity_key: value} for value in entities],
            full_feature_names=True,
        ).to_dict(include_event_timestamps=True)
        entity_df = pd.DataFrame(
            {entity_key: entities, "event_timestamp": [pd.Timestamp(cutover)] * len(entities)}
        )
        plan = build_bounded_source_plan(store, view_name, entity_df)
        with bounded_dask_source(plan):
            offline = store.get_historical_features(
                entity_df=entity_df, features=refs, full_feature_names=True
            ).to_df()
        offline = offline.set_index(entity_key)
        rows = []
        for index, entity in enumerate(entities):
            for feature in spec["features"]:
                column = f"{view_name}__{feature}"
                online_value = online[column][index]
                offline_value = offline.at[entity, column]
                offline_value = None if pd.isna(offline_value) else offline_value.item() if hasattr(offline_value, "item") else offline_value
                match = _same(offline_value, online_value)
                total += 1
                if not match:
                    mismatches += 1
                    rows.append(
                        {"entity": entity, "feature": column,
                         "offline": offline_value, "online": online_value}
                    )
        online_ts = [
            pd.Timestamp(value).isoformat() if value is not None else None
            for value in online[f"{view_name}__{spec['features'][0]}__ts"]
        ]
        probe = store.get_online_features(
            features=refs,
            entity_rows=[{entity_key: MISSING_ENTITY_PROBE}],
            full_feature_names=True,
        ).to_dict()
        views.append(
            {
                "view": view_name,
                "entity_key": entity_key,
                "entities": entities,
                "features_per_entity": len(spec["features"]),
                "online_event_timestamps": online_ts,
                "missing_entity_all_null": all(
                    probe[f"{view_name}__{feature}"] == [None] for feature in spec["features"]
                ),
                "mismatches": rows,
            }
        )
    return {
        "cutover": cutover.isoformat(),
        "historical_features": sum(len(spec["features"]) for spec in FEATURE_SPECS.values()),
        "comparisons": total,
        "mismatch_count": mismatches,
        "status": "PASS" if total and not mismatches and all(v["missing_entity_all_null"] for v in views) else "FAIL",
        "views": views,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("apply")
    window = commands.add_parser("materialize")
    window.add_argument("--start", type=_timestamp, required=True)
    window.add_argument("--end", type=_timestamp, required=True)
    incremental = commands.add_parser("materialize-incremental")
    incremental.add_argument("--end", type=_timestamp, required=True)
    check = commands.add_parser("parity")
    check.add_argument("--cutover", type=_timestamp, required=True)
    check.add_argument("--sample-window-start", type=_timestamp, required=True)
    check.add_argument("--sample-size", type=int, default=5)
    args = parser.parse_args(argv)

    store = get_online_store()
    started = datetime.now(timezone.utc)
    if args.command == "apply":
        result = apply(store)
    elif args.command == "materialize":
        result = materialize(store, args.start, args.end)
    elif args.command == "materialize-incremental":
        result = materialize_incremental(store, args.end)
    else:
        result = parity(store, args.cutover, args.sample_window_start, args.sample_size)
    evidence = {
        "schema_version": "feast-online-operation-evidence-v1",
        "command": args.command,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "registry": os.environ["EWALLET_FEAST_REGISTRY"],
        "offline_data_root": os.environ["EWALLET_FEAST_OFFLINE_DATA_ROOT"],
        "git_sha": os.environ.get("EWALLET_GIT_SHA", "unknown"),
        **result,
    }
    print(json.dumps(evidence, sort_keys=True, default=str))
    return 1 if evidence.get("status") == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
