"""Operate the Feast Online (Redis) store from the immutable offline snapshot.

Subcommands:

* ``apply`` registers the Block A online FeatureViews in a NEW online registry
  (create-only). The offline registry and snapshot are never written.
* ``materialize`` loads ``[start, end]`` through native
  ``FeatureStore.materialize`` with the bounded Dask reader.
* ``materialize-incremental`` continues every view from the end time Feast
  recorded in the registry; it refuses views that were never materialized.
* ``parity`` compares Feast Online values with Feast Offline PIT retrieval at
  the cutover for a deterministic entity sample per FeatureView. An entity whose
  online row is at or past the cutover belongs to the streaming path and is
  reported instead of compared.
* ``state`` is read-only and reports, per view, where the registry stopped and
  the event-time range of the immutable snapshot.

Every subcommand prints one JSON evidence document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

import pandas as pd
import pyarrow.parquet as parquet
from feast import FeatureStore
from fsspec.core import url_to_fs

from ai_platform.features.feast.registry_mirror import mirror_from_environment
from ai_platform.features.feast.retrieval.bounded_dask import (
    bounded_dask_source,
    build_bounded_source_plan,
    build_time_window_plan,
)
from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.features.feast.redis_isolation import (
    RedisIsolationError,
    claim as claim_redis,
    owner_record,
    read_owner,
    target_fingerprint,
)


ONLINE_REPO = Path(__file__).resolve().parent / "online_repo"
MISSING_ENTITY_PROBE = "__parity_missing_entity__"
FOOTER_READ_WORKERS = 16


def _redis_connection_parts(connection_string: str) -> tuple[str, int, int, dict[str, Any]]:
    """Parse Feast's comma-delimited Redis setting without returning credentials."""

    segments = connection_string.removeprefix("redis://").split(",")
    host_port = segments[0]
    if ":" not in host_port:
        raise RuntimeError("Redis connection must include host:port")
    host, raw_port = host_port.rsplit(":", 1)
    if not host:
        raise RuntimeError("Redis connection host is empty")
    try:
        port = int(raw_port)
    except ValueError as error:
        raise RuntimeError("Redis connection port is invalid") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("Redis connection port is invalid")
    options: dict[str, Any] = {}
    for segment in segments[1:]:
        if "=" not in segment:
            raise RuntimeError("Redis connection option is invalid")
        name, raw_value = segment.split("=", 1)
        if name in options:
            raise RuntimeError(f"duplicate Redis connection option: {name}")
        try:
            options[name] = json.loads(raw_value)
        except json.JSONDecodeError:
            options[name] = raw_value
    try:
        database = int(options.get("db", 0))
    except (TypeError, ValueError) as error:
        raise RuntimeError("Redis database is invalid") from error
    if database < 0:
        raise RuntimeError("Redis database is invalid")
    return host, port, database, options


def _redis_client(connection_string: str):
    from redis import Redis

    host, port, database, options = _redis_connection_parts(connection_string)
    return Redis(
        host=host,
        port=port,
        db=database,
        password=options.get("password"),
        username=options.get("username"),
        socket_connect_timeout=5,
        socket_timeout=5,
        decode_responses=True,
    )


def online_store_evidence(connection_string: str) -> dict[str, object]:
    """Read sanitized Redis identity and capacity for the materialization preflight."""

    host, port, database, options = _redis_connection_parts(connection_string)
    client = _redis_client(connection_string)
    memory = client.info("memory")
    server = client.info("server")
    fingerprint = target_fingerprint(host, port, database, str(server.get("run_id", "")))
    ownership = read_owner(client)
    return {
        "logical_id": os.environ["EWALLET_ONLINE_STORE_ID"],
        "secret_name": os.environ["EWALLET_REDIS_SECRET_NAME"],
        "host": host,
        "port": port,
        "db": database,
        "server_run_id": server.get("run_id"),
        "target_fingerprint": fingerprint,
        "used_memory_bytes": int(memory["used_memory"]),
        "maxmemory_bytes": int(memory["maxmemory"]),
        "maxmemory_policy": str(memory["maxmemory_policy"]),
        "ownership": ownership,
        "isolation_status": "OWNED" if ownership else "UNCLAIMED",
    }


def _authorized_target(connection_string: str) -> tuple[str, object, dict[str, object], str]:
    """Resolve policy plus physical target, failing closed before any write."""

    required = (
        "EWALLET_EXECUTION_POLICY",
        "EWALLET_ONLINE_STORE_ID",
        "EWALLET_REDIS_SECRET_NAME",
        "EWALLET_DATASET_VERSION",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RedisIsolationError(
            f"Online Store execution policy requires {', '.join(missing)}"
        )
    from ai_platform.features.feast.materialization_plan import (
        ONLINE_STORE_BINDINGS,
        WindowError,
        validate_execution_policy,
    )

    policy = os.environ["EWALLET_EXECUTION_POLICY"]
    store_id = os.environ["EWALLET_ONLINE_STORE_ID"]
    try:
        validate_execution_policy(policy, store_id)
    except WindowError as error:
        raise RedisIsolationError(str(error)) from error
    binding = ONLINE_STORE_BINDINGS[store_id]
    host, port, database, _ = _redis_connection_parts(connection_string)
    if (
        os.environ["EWALLET_REDIS_SECRET_NAME"] != binding["redis_secret"]
        or host != binding["redis_host"]
        or port != binding["redis_port"]
        or database != binding["redis_db"]
        or os.environ["EWALLET_DATASET_VERSION"] not in binding["dataset_versions"]
    ):
        raise RedisIsolationError("Online Store target differs from the approved binding")
    client = _redis_client(connection_string)
    server = client.info("server")
    run_id = str(server.get("run_id", ""))
    if not run_id:
        raise RedisIsolationError("Redis server identity is missing")
    fingerprint = target_fingerprint(host, port, database, run_id)
    return policy, client, binding, fingerprint


def _expected_ownership(
    connection_string: str, registry_uri: str | None = None
) -> tuple[object, dict[str, str]]:
    required = (
        "EWALLET_REDIS_CAMPAIGN_ID",
        "EWALLET_DATASET_VERSION",
        "EWALLET_FEAST_REGISTRY",
        "EWALLET_ONLINE_STORE_ID",
        "EWALLET_REDIS_SECRET_NAME",
        "EWALLET_FEATURE_RUN_ID",
        "EWALLET_SNAPSHOT_ID",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RedisIsolationError(f"Redis campaign ownership requires {', '.join(missing)}")
    policy, client, binding, fingerprint = _authorized_target(connection_string)
    if policy != "verification" or not binding["requires_campaign_ownership"]:
        raise RedisIsolationError("Online Store is not an approved verification binding")
    expected = owner_record(
        os.environ["EWALLET_REDIS_CAMPAIGN_ID"],
        os.environ["EWALLET_DATASET_VERSION"],
        registry_uri if registry_uri is not None else os.environ["EWALLET_FEAST_REGISTRY"],
        os.environ["EWALLET_ONLINE_STORE_ID"],
        fingerprint,
        os.environ["EWALLET_FEATURE_RUN_ID"],
        os.environ["EWALLET_SNAPSHOT_ID"],
    )
    return client, expected


def claim_online_store(
    connection_string: str, registry_uri: str | None = None
) -> dict[str, object]:
    """Claim an empty verification database for exactly one campaign."""

    client, expected = _expected_ownership(connection_string, registry_uri)
    status = claim_redis(client, expected)
    return {"claim_status": status, **online_store_evidence(connection_string)}


def require_online_store_ownership(
    connection_string: str, registry_uri: str | None = None
) -> None:
    """Fail closed unless the current verification store has the exact owner marker."""

    policy, _client, _binding, _fingerprint = _authorized_target(connection_string)
    if policy == "production":
        return
    client, expected = _expected_ownership(connection_string, registry_uri)
    if read_owner(client) != expected:
        raise RedisIsolationError("verification Redis is not owned by this campaign")


def require_online_store_capacity(connection_string: str) -> None:
    """Require a current no-eviction target with 10% headroom after the estimate."""

    policy, _client, _binding, _fingerprint = _authorized_target(connection_string)
    if policy == "production":
        return
    raw_estimate = os.environ.get("EWALLET_ESTIMATED_REDIS_BYTES", "")
    try:
        estimate = int(raw_estimate)
    except ValueError as error:
        raise RedisIsolationError("EWALLET_ESTIMATED_REDIS_BYTES must be positive") from error
    if estimate <= 0:
        raise RedisIsolationError("EWALLET_ESTIMATED_REDIS_BYTES must be positive")
    evidence = online_store_evidence(connection_string)
    used = int(evidence["used_memory_bytes"])
    maximum = int(evidence["maxmemory_bytes"])
    if evidence["maxmemory_policy"] != "noeviction" or maximum <= 0:
        raise RedisIsolationError("verification Redis must have bounded noeviction capacity")
    if maximum - used - estimate < int(maximum * 0.10):
        raise RedisIsolationError("verification Redis estimate would leave less than 10% headroom")


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
    return {"feature_views": applied}


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


def _file_event_time_range(filesystem, path: str) -> tuple[datetime, datetime]:
    """Read one file's event-time bounds from its Parquet footer statistics."""

    with filesystem.open(path, "rb") as stream:
        metadata = parquet.read_metadata(stream)
    column = metadata.schema.to_arrow_schema().get_field_index("event_timestamp")
    if column < 0:
        raise RuntimeError(f"{path} has no event_timestamp column")
    bounds = []
    for index in range(metadata.num_row_groups):
        statistics = metadata.row_group(index).column(column).statistics
        if statistics is None or not statistics.has_min_max:
            raise RuntimeError(f"{path} has no event_timestamp statistics")
        bounds.append((statistics.min, statistics.max))
    if not bounds:
        raise RuntimeError(f"{path} has no row groups")
    return min(low for low, _ in bounds), max(high for _, high in bounds)


def source_event_time_range(path: str) -> dict[str, object]:
    """Return the event-time range of a snapshot table without reading its rows.

    Args:
        path: Local or ``gs://`` directory that holds the table's Parquet files.

    Returns:
        File count and the minimum and maximum event timestamps in UTC.
    """

    filesystem, root = url_to_fs(path)
    files = sorted(filesystem.glob(f"{root.rstrip('/')}/*.parquet"))
    if not files:
        raise RuntimeError(f"no Parquet files under {path}")
    with ThreadPoolExecutor(FOOTER_READ_WORKERS) as pool:
        ranges = list(pool.map(lambda name: _file_event_time_range(filesystem, name), files))
    return {
        "source_files": len(files),
        "source_min_event_timestamp": pd.Timestamp(min(low for low, _ in ranges)).isoformat(),
        "source_max_event_timestamp": pd.Timestamp(max(high for _, high in ranges)).isoformat(),
    }


def require_snapshot_binding(store: FeatureStore) -> None:
    """Refuse to run when a registered source is outside the configured snapshot."""

    root = os.environ["EWALLET_FEAST_OFFLINE_DATA_ROOT"].rstrip("/")
    for name in FEATURE_SPECS:
        source = store.get_feature_view(name, allow_registry_cache=False).batch_source
        if not str(source.path).startswith(f"{root}/"):
            raise RuntimeError(f"{name} reads {source.path}, outside snapshot {root}")


def state(store: FeatureStore) -> dict[str, object]:
    """Report registry progress and snapshot range per view; writes nothing."""

    views = []
    for name in FEATURE_SPECS:
        view = store.get_feature_view(name, allow_registry_cache=False)
        end = view.most_recent_end_time
        path = str(view.batch_source.path)
        views.append(
            {
                "view": name,
                "materialized_end": end.astimezone(timezone.utc).isoformat() if end else None,
                "source_path": path,
                **source_event_time_range(path),
            }
        )
    return {
        "online_store": online_store_evidence(
            os.environ["EWALLET_REDIS_CONNECTION_STRING"]
        ),
        "views": views,
    }


def materialize_incremental(store: FeatureStore, end: datetime) -> dict[str, object]:
    require_snapshot_binding(store)
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


def _online_timestamp(value: object) -> pd.Timestamp | None:
    """Convert a Feast ``__ts`` value to UTC.

    ``to_dict(include_event_timestamps=True)`` returns epoch seconds as an integer.
    ``pd.Timestamp`` alone would read that integer as nanoseconds after 1970.
    """

    if value is None:
        return None
    if isinstance(value, int):
        return pd.Timestamp(value, unit="s", tz="UTC")
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _owned_by_streaming(value: object, cutover: datetime) -> bool:
    """Whether an online row is at or past the cutover, so batch does not own it.

    Feast reports whole seconds here, so a row a fraction after the cutover floors
    to the cutover itself; ``>=`` keeps that row out of the batch comparison.
    """

    stamp = _online_timestamp(value)
    return stamp is not None and stamp >= pd.Timestamp(cutover)


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
        raw_timestamps = online[f"{view_name}__{spec['features'][0]}__ts"]
        superseded = []
        for index, entity in enumerate(entities):
            if _owned_by_streaming(raw_timestamps[index], cutover):
                superseded.append(entity)
                continue
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
            stamp.isoformat() if (stamp := _online_timestamp(value)) is not None else None
            for value in raw_timestamps
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
                "superseded_by_streaming": superseded,
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
    commands.add_parser("claim")
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
    commands.add_parser("state")
    args = parser.parse_args(argv)

    registry_uri = os.environ.get("EWALLET_FEAST_REGISTRY", "")
    mirror = mirror_from_environment()
    generation_before = mirror.generation if mirror else None
    if args.command == "apply" and mirror is not None and mirror.generation != 0:
        raise FileExistsError(f"online registry already exists: {registry_uri}")
    connection_string = os.environ["EWALLET_REDIS_CONNECTION_STRING"]
    started = datetime.now(timezone.utc)
    if args.command == "claim":
        result = claim_online_store(connection_string, registry_uri)
    else:
        store = get_online_store()
    if args.command == "claim":
        pass
    elif args.command == "apply":
        require_online_store_ownership(connection_string, registry_uri)
        result = apply(store)
    elif args.command == "materialize":
        require_online_store_ownership(connection_string, registry_uri)
        require_online_store_capacity(connection_string)
        result = materialize(store, args.start, args.end)
    elif args.command == "materialize-incremental":
        require_online_store_ownership(connection_string, registry_uri)
        require_online_store_capacity(connection_string)
        result = materialize_incremental(store, args.end)
    elif args.command == "state":
        require_online_store_ownership(connection_string, registry_uri)
        result = state(store)
    else:
        require_online_store_ownership(connection_string, registry_uri)
        result = parity(store, args.cutover, args.sample_window_start, args.sample_size)
    generation_after = generation_before
    if mirror is not None and args.command not in {"claim", "parity", "state"}:
        generation_after = mirror.push()
    evidence = {
        "schema_version": "feast-online-operation-evidence-v1",
        "command": args.command,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "registry": registry_uri,
        "registry_generation_before": generation_before,
        "registry_generation_after": generation_after,
        "offline_data_root": os.environ["EWALLET_FEAST_OFFLINE_DATA_ROOT"],
        "git_sha": os.environ.get("EWALLET_GIT_SHA", "unknown"),
        **result,
    }
    print(json.dumps(evidence, sort_keys=True, default=str))
    return 1 if evidence.get("status") == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
