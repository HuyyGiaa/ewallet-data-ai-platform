"""Bound Feast's native Dask FileSource scan without changing PIT semantics.

Feast 0.66.0 reads a FileSource without columns or filters and then persists it
several times while joining, sorting and deduplicating.  For the canonical G3
sample this module installs a short-lived source-reader wrapper around one
native ``get_historical_features`` call.  The wrapper returns a lazy Dask
projection filtered to a conservative superset of every row that can win the
PIT lookup.  Feast still performs the authoritative join, TTL filter and
tie-breaking.
"""

from __future__ import annotations

import resource
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from time import perf_counter
from typing import Iterator

import dask.dataframe as dd
import pandas as pd
from feast import FeatureStore
from feast.infra.offline_stores import dask as feast_dask

from ai_platform.features.feast.schema import FEATURE_SPECS


_SOURCE_READER_LOCK = threading.Lock()


def current_rss_bytes() -> int | None:
    """Return current Linux process RSS without adding a runtime dependency."""

    try:
        fields = {}
        with open("/proc/self/status", encoding="utf-8") as stream:
            for line in stream:
                name, separator, value = line.partition(":")
                if separator:
                    fields[name] = value.strip()
        return int(fields["VmRSS"].split()[0]) * 1024
    except (OSError, KeyError, ValueError, IndexError):
        return None


def peak_rss_bytes() -> int:
    # ru_maxrss is KiB on Linux, which is the only supported cloud runtime.
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


@dataclass(frozen=True)
class BoundedSourcePlan:
    view_name: str
    source_path: str
    entity_key: str
    entity_values: tuple[str, ...]
    timestamp_field: str
    start_timestamp: datetime | None
    end_timestamp: datetime
    projected_columns: tuple[str, ...]
    ttl_seconds: float

    def evidence(self) -> dict[str, object]:
        payload = asdict(self)
        payload["start_timestamp"] = (
            None if self.start_timestamp is None else self.start_timestamp.isoformat()
        )
        payload["end_timestamp"] = self.end_timestamp.isoformat()
        payload["entity_values"] = list(self.entity_values)
        payload["projected_columns"] = list(self.projected_columns)
        return payload


def build_bounded_source_plan(
    store: FeatureStore,
    view_name: str,
    entity_df: pd.DataFrame,
) -> BoundedSourcePlan:
    """Build a safe superset predicate for one frozen FeatureView."""

    spec = FEATURE_SPECS[view_name]
    entity_key = spec["entity_key"]
    required = {entity_key, "event_timestamp"}
    missing = sorted(required - set(entity_df.columns))
    if missing:
        raise ValueError(f"bounded retrieval entity columns missing: {missing}")
    if entity_df.empty or entity_df[entity_key].isna().any():
        raise ValueError("bounded retrieval requires non-null eligible entities")

    timestamps = pd.to_datetime(entity_df["event_timestamp"], utc=True)
    if timestamps.isna().any():
        raise ValueError("bounded retrieval contains a null event timestamp")
    feature_view = store.get_feature_view(view_name, allow_registry_cache=False)
    source = feature_view.batch_source
    if source is None or source.timestamp_field is None:
        raise ValueError("bounded retrieval requires a timestamped FileSource")
    if source.timestamp_field != "event_timestamp":
        raise ValueError("frozen FeatureView timestamp binding drifted")

    entity_columns = tuple(column.name for column in feature_view.entity_columns)
    if entity_columns != (entity_key,):
        raise ValueError("frozen FeatureView entity binding drifted")
    # FeatureStore.get_feature_view() may expose the entity field inside schema
    # and does not retain protobuf feature ordering in this object.  Ordering is
    # validated separately from the serialized Registry; here we fail closed on
    # the exact feature set and project in the frozen contract order.
    observed_features = {
        field.name for field in feature_view.schema if field.name != entity_key
    }
    source_features = tuple(spec["features"])
    if observed_features != set(source_features):
        raise ValueError("frozen FeatureView feature set drifted")

    ttl_seconds = feature_view.ttl.total_seconds()
    minimum = timestamps.min()
    maximum = timestamps.max()
    start = None if ttl_seconds == 0 else minimum - feature_view.ttl
    projected = [entity_key, source.timestamp_field]
    if source.created_timestamp_column:
        projected.append(source.created_timestamp_column)
    projected.extend(source_features)
    return BoundedSourcePlan(
        view_name=view_name,
        source_path=str(source.path),
        entity_key=entity_key,
        entity_values=tuple(sorted(str(value) for value in entity_df[entity_key].unique())),
        timestamp_field=source.timestamp_field,
        start_timestamp=None if start is None else start.to_pydatetime(),
        end_timestamp=maximum.to_pydatetime(),
        projected_columns=tuple(projected),
        ttl_seconds=ttl_seconds,
    )


def _apply_plan(frame: dd.DataFrame, plan: BoundedSourcePlan) -> dd.DataFrame:
    missing = sorted(set(plan.projected_columns) - set(frame.columns))
    if missing:
        raise ValueError(f"bounded source columns missing: {missing}")
    frame = frame[list(plan.projected_columns)]
    predicate = frame[plan.entity_key].isin(plan.entity_values)
    if plan.start_timestamp is not None:
        predicate = predicate & (
            frame[plan.timestamp_field] >= plan.start_timestamp
        )
    # Deliberately retain rows at T. Feast 0.66.0 owns the final PIT cutoff;
    # excluding them here could hide a same-timestamp leakage defect.
    predicate = predicate & (frame[plan.timestamp_field] <= plan.end_timestamp)
    return frame[predicate]


@contextmanager
def bounded_dask_source(plan: BoundedSourcePlan) -> Iterator[dict[str, object]]:
    """Scope the optimized reader to one synchronous native Feast call.

    Feast exposes no public hook for FileSource predicates.  The wrapper is
    therefore guarded by a process lock and restored in ``finally``.  G3 is a
    single-task, single-process job; concurrent calls are intentionally not
    supported.
    """

    started = perf_counter()
    metrics: dict[str, object] = {
        **plan.evidence(),
        "rss_before_bytes": current_rss_bytes(),
        "peak_rss_before_bytes": peak_rss_bytes(),
    }
    with _SOURCE_READER_LOCK:
        original = feast_dask._read_datasource

        def reader(data_source, repo_path):
            if str(data_source.path) != plan.source_path:
                raise ValueError(
                    "bounded retrieval source binding mismatch: "
                    f"actual={data_source.path}, expected={plan.source_path}"
                )
            return _apply_plan(original(data_source, repo_path), plan)

        feast_dask._read_datasource = reader
        try:
            yield metrics
        finally:
            feast_dask._read_datasource = original
    metrics.update(
        {
            "runtime_seconds": round(perf_counter() - started, 6),
            "rss_after_bytes": current_rss_bytes(),
            "peak_rss_after_bytes": peak_rss_bytes(),
        }
    )
