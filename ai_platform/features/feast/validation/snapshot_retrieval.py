"""Verify genuine Feast historical retrieval against one immutable Offline snapshot.

The check runs Feast's own ``get_historical_features`` over the snapshot's Parquet
files and compares every returned feature with the row that point-in-time semantics
must select, derived independently from the same Parquet: per entity, the latest
row with ``event_timestamp <= as_of`` inside the FeatureView TTL.

* The snapshot is identified by ``_control/export_manifest.json``. Its SHA-256 must
  equal the pinned digest, and its four tables must be the snapshot's own.
* Entities are sampled deterministically from rows in ``[as_of - window, as_of]``,
  so each sampled entity has a row that Feast must return.
* Two rows of one entity at the selected timestamp make the expected value
  ambiguous. Such an entity fails the check instead of being compared.
* The result is ``PASS`` only with nonzero comparisons, zero mismatches and zero
  ambiguous entities.

The temporary registry and SQLite online store are local and discarded; nothing is
written to the snapshot, a registry in GCS or Redis.

usage: python -m ai_platform.features.feast.validation.snapshot_retrieval \\
         --snapshot-root gs://.../snapshots/<dataset>/<feature-run>/<snapshot> \\
         --expected-manifest-sha256 <64 hex> --as-of 2026-05-20T12:00:00.5Z --output report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.fs as pafs

from ai_platform.features.feast.schema import FEATURE_SPECS

REPORT_SCHEMA = "feast-snapshot-retrieval-v1"
ONLINE_PROJECT = "ewallet_fraud_online"


def _filesystem(uri: str) -> tuple[pafs.FileSystem, str]:
    if uri.startswith("gs://"):
        return pafs.GcsFileSystem(retry_time_limit=timedelta(minutes=5)), uri.removeprefix("gs://")
    return pafs.LocalFileSystem(), str(Path(uri).resolve())


def read_manifest(snapshot_root: str, expected_sha256: str) -> tuple[dict, str]:
    """Return the export manifest and its digest, refusing any other snapshot."""

    filesystem, root = _filesystem(snapshot_root)
    with filesystem.open_input_stream(f"{root}/_control/export_manifest.json") as stream:
        raw = stream.read()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError(f"export manifest digest {digest} is not the pinned {expected_sha256}")
    manifest = json.loads(raw)
    if sorted(manifest.get("tables", {})) != sorted(FEATURE_SPECS):
        raise ValueError("export manifest does not list the four FeatureViews")
    for view, spec in FEATURE_SPECS.items():
        if manifest["tables"][view].get("target") != f"{snapshot_root.rstrip('/')}/{spec['table']}":
            raise ValueError(f"{view}: manifest target is outside this snapshot")
    return manifest, digest


def _window_rows(snapshot_root: str, view: str, start: datetime, end: datetime) -> pd.DataFrame:
    spec = FEATURE_SPECS[view]
    filesystem, root = _filesystem(snapshot_root)
    dataset = ds.dataset(f"{root}/{spec['table']}", filesystem=filesystem, format="parquet")
    timestamp = ds.field("event_timestamp")
    table = dataset.to_table(
        columns=[spec["entity_key"], "event_timestamp", *spec["features"]],
        filter=(timestamp >= pc.scalar(start)) & (timestamp <= pc.scalar(end)),
    )
    return table.to_pandas()


def expected_rows(
    rows: pd.DataFrame, view: str, sample_size: int
) -> tuple[pd.DataFrame, list[str]]:
    """Pick entities deterministically and return their PIT row, plus ambiguous keys."""

    entity_key = FEATURE_SPECS[view]["entity_key"]
    if rows.empty:
        raise ValueError(f"{view}: no rows in the sample window")
    candidates = sorted(
        rows[entity_key].drop_duplicates(),
        key=lambda value: hashlib.sha256(f"{view}:{value}".encode()).hexdigest(),
    )[:sample_size]
    selected = rows[rows[entity_key].isin(candidates)]
    latest = selected.groupby(entity_key)["event_timestamp"].transform("max")
    winners = selected[selected["event_timestamp"] == latest]
    counts = winners.groupby(entity_key).size()
    ambiguous = sorted(counts[counts > 1].index)
    return winners[~winners[entity_key].isin(ambiguous)].set_index(entity_key), ambiguous


def _store(snapshot_root: str, workdir: Path):
    """A local, throwaway FeatureStore whose batch sources are the snapshot."""

    from unittest import mock

    from feast import FeatureStore, RepoConfig

    # The repo module also builds its objects at import time from this variable;
    # the objects applied below are built from ``snapshot_root`` explicitly.
    variable = "EWALLET_FEAST_OFFLINE_DATA_ROOT"
    with mock.patch.dict(os.environ, {variable: os.environ.get(variable, snapshot_root)}):
        from ai_platform.features.feast.online_repo.feature_definitions import (
            build_online_feature_objects,
        )

    config = RepoConfig(
        project=ONLINE_PROJECT,
        registry=str(workdir / "registry.db"),
        provider="local",
        offline_store={"type": "dask"},
        online_store={"type": "sqlite", "path": str(workdir / "online.db")},
        entity_key_serialization_version=3,
    )
    store = FeatureStore(config=config)
    entities, _, pushes, views = build_online_feature_objects(snapshot_root)
    store.apply([*entities.values(), *pushes.values(), *views.values()])
    return store


def _same(expected: object, actual: object) -> bool:
    if expected is None or (isinstance(expected, float) and math.isnan(expected)):
        return actual is None or (isinstance(actual, float) and math.isnan(actual))
    if isinstance(expected, float) or isinstance(actual, float):
        return actual is not None and float(expected) == float(actual)
    return expected == actual


def _plain(value: object) -> object:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if pd.isna(value):
        return None
    return value.item() if hasattr(value, "item") else value


def verify(
    snapshot_root: str,
    expected_sha256: str,
    as_of: datetime,
    window: timedelta,
    sample_size: int,
) -> dict[str, object]:
    """Run Feast PIT retrieval for every view and compare it with the derived rows."""

    from ai_platform.features.feast.retrieval.bounded_dask import (
        bounded_dask_source,
        build_bounded_source_plan,
    )

    manifest, digest = read_manifest(snapshot_root, expected_sha256)
    report_views = []
    comparisons = mismatches = retrieved = 0
    ambiguous_total = 0
    with tempfile.TemporaryDirectory(prefix="feast-snapshot-retrieval-") as temporary:
        store = _store(snapshot_root, Path(temporary))
        for view, spec in FEATURE_SPECS.items():
            entity_key = spec["entity_key"]
            refs = [f"{view}:{feature}" for feature in spec["features"]]
            expected, ambiguous = expected_rows(
                _window_rows(snapshot_root, view, as_of - window, as_of), view, sample_size
            )
            ambiguous_total += len(ambiguous)
            entity_df = pd.DataFrame(
                {entity_key: list(expected.index), "event_timestamp": [pd.Timestamp(as_of)] * len(expected)}
            )
            with bounded_dask_source(build_bounded_source_plan(store, view, entity_df)):
                result = store.get_historical_features(
                    entity_df=entity_df, features=refs, full_feature_names=True
                ).to_df()
            result = result.set_index(entity_key)
            retrieved += len(result)
            failures = []
            for entity in expected.index:
                for feature in spec["features"]:
                    comparisons += 1
                    want = _plain(expected.at[entity, feature])
                    got = _plain(result.at[entity, f"{view}__{feature}"]) if entity in result.index else None
                    if not _same(want, got):
                        mismatches += 1
                        failures.append({"entity": entity, "feature": feature, "expected": want, "feast": got})
            report_views.append({
                "view": view,
                "entity_key": entity_key,
                "entities": list(expected.index),
                "selected_event_timestamps": [
                    pd.Timestamp(stamp).isoformat() for stamp in expected["event_timestamp"]
                ],
                "features_per_entity": len(spec["features"]),
                "ambiguous_entities": ambiguous,
                "mismatches": failures,
            })
    passed = comparisons > 0 and mismatches == 0 and ambiguous_total == 0
    return {
        "schema_version": REPORT_SCHEMA,
        "status": "PASS" if passed else "FAIL",
        "snapshot_root": snapshot_root,
        "manifest_sha256": digest,
        "dataset_version": manifest.get("dataset_version"),
        "source_feature_run_id": (manifest.get("lineage") or {}).get("source_feature_run_id"),
        "snapshot_id": (manifest.get("lineage") or {}).get("snapshot_id"),
        "as_of": as_of.isoformat(),
        "sample_window_start": (as_of - window).isoformat(),
        "historical_features": sum(len(spec["features"]) for spec in FEATURE_SPECS.values()),
        "rows_retrieved": retrieved,
        "comparisons": comparisons,
        "mismatch_count": mismatches,
        "ambiguous_entities": ambiguous_total,
        "method": "Feast FeatureStore.get_historical_features (dask, bounded scan) vs the "
                  "latest Parquet row <= as_of per entity",
        "views": report_views,
    }


def _instant(value: str) -> datetime:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("as_of must include a UTC offset")
    return parsed.tz_convert("UTC").to_pydatetime()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-root", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--as-of", type=_instant, required=True)
    parser.add_argument("--window-hours", type=float, default=6.0)
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = verify(
        args.snapshot_root.rstrip("/"),
        args.expected_manifest_sha256,
        args.as_of.astimezone(timezone.utc),
        timedelta(hours=args.window_hours),
        args.sample_size,
    )
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    summary = {key: report[key] for key in (
        "status", "snapshot_id", "manifest_sha256", "as_of", "rows_retrieved",
        "comparisons", "mismatch_count", "ambiguous_entities")}
    print(json.dumps(summary, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
