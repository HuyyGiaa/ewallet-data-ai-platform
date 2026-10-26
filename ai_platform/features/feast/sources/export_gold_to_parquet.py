"""Export the active Gold Delta snapshots to Feast-compatible Parquet.

The adapter is intentionally explicit: Feast 0.66 has no native Delta source,
and the project's Trino layer exposes timestamps at millisecond precision.
delta-rs reads the active Delta snapshot and PyArrow writes Parquet while
retaining the UTC-aware microsecond event timestamp needed by the F5 contract.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.dataset as ds
import pyarrow.fs as fs
from deltalake import DeltaTable

from ai_platform.features.feast import OFFLINE_DATA_ROOT
from ai_platform.features.feast.provenance import (
    FeatureSelectionEvidence,
    load_feature_selection_evidence,
)
from ai_platform.features.feast.repository import (
    build_export_manifest,
    join_data_root,
)
from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.config import RuntimeConfig, load_runtime_config
from data_platform.storage.scripts.config import (
    deltalake_storage_options,
    storage_table_uri,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--environment",
        choices=("local", "dev", "gcp"),
        required=True,
    )
    parser.add_argument("--output-root")
    parser.add_argument("--source-feature-root")
    parser.add_argument("--snapshot-id")
    parser.add_argument(
        "--source-feature-run-id",
        help="Required for cloud exports; must match verified selection evidence.",
    )
    parser.add_argument("--feature-selection-evidence")
    parser.add_argument(
        "--dataset-version",
        default=os.getenv("EWALLET_DATASET_VERSION"),
    )
    return parser.parse_args(argv)


def apply_feature_gold_root(
    config: RuntimeConfig,
    feature_gold_root: str,
) -> RuntimeConfig:
    """Bind the export to one immutable DP3 Gold run."""
    if config.environment != "gcp" or config.storage.scheme != "gs":
        raise ValueError("run-scoped feature export requires the gcp profile")
    if (
        not feature_gold_root.startswith("gs://")
        or "/lakehouse/runs/" not in feature_gold_root
        or not feature_gold_root.endswith("/gold")
    ):
        raise ValueError("feature Gold root must be a run-scoped gs://.../gold path")
    return replace(
        config,
        storage=replace(config.storage, gold_root=feature_gold_root),
    )


def gold_source_uri(config: RuntimeConfig, table_name: str) -> str:
    return storage_table_uri(
        config.storage,
        "gold",
        table_name,
        delta_rs=True,
    )


def _delta_storage_options(config: RuntimeConfig) -> dict[str, str]:
    if config.storage.scheme == "s3a":
        return deltalake_storage_options(config)
    return dict(config.storage.options)


def _filesystem_path(uri: str) -> tuple[fs.FileSystem, str]:
    if "://" not in uri:
        uri = str(Path(uri).resolve())
    filesystem, path = fs.FileSystem.from_uri(uri)
    return filesystem, path.rstrip("/")


def _write_dataset(
    target_uri: str,
    snapshot,
    *,
    replace_existing: bool,
) -> tuple[int, str]:
    filesystem, target_path = _filesystem_path(target_uri)
    target_info = filesystem.get_file_info(target_path)
    if target_info.type != fs.FileType.NotFound and not replace_existing:
        raise FileExistsError(f"immutable Feast snapshot already exists: {target_uri}")
    if target_info.type != fs.FileType.NotFound:
        filesystem.delete_dir(target_path)
    ds.write_dataset(
        snapshot,
        target_path,
        filesystem=filesystem,
        format="parquet",
        max_rows_per_file=1_000_000,
        max_rows_per_group=100_000,
    )
    exported = ds.dataset(target_path, filesystem=filesystem, format="parquet")
    return (
        exported.count_rows(),
        str(exported.schema.field("event_timestamp").type),
    )


def _write_manifest(uri: str, manifest: dict[str, object]) -> None:
    filesystem, path = _filesystem_path(uri)
    parent = str(Path(path).parent)
    filesystem.create_dir(parent, recursive=True)
    with filesystem.open_output_stream(path) as stream:
        stream.write((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))


def bind_cloud_feature_lineage(
    runtime_config: RuntimeConfig,
    *,
    selection: FeatureSelectionEvidence,
) -> RuntimeConfig:
    """Bind cloud Feast reads to one evidence-selected DP3 output root."""

    if runtime_config.environment != "gcp" or runtime_config.storage.scheme != "gs":
        raise ValueError("verified feature selection requires a GCP GCS root")
    return replace(
        runtime_config,
        storage=replace(
            runtime_config.storage,
            gold_root=selection.feature_root,
        ),
    )


def export_gold_snapshots(
    runtime_config: RuntimeConfig,
    output_root: str | Path | None = None,
    *,
    dataset_version: str = "active",
    source_feature_run_id: str | None = None,
    snapshot_id: str | None = None,
    immutable: bool = False,
    selection: FeatureSelectionEvidence | None = None,
) -> dict[str, object]:
    if runtime_config.environment != "local":
        if selection is None:
            raise ValueError("cloud Feast export requires verified selection evidence")
        expected = (
            dataset_version,
            source_feature_run_id,
            runtime_config.storage.gold_root,
        )
        actual = (
            selection.dataset_version,
            selection.feature_run_id,
            selection.feature_root,
        )
        if expected != actual:
            raise ValueError("cloud Feast arguments do not match selection evidence")
        runtime_config = bind_cloud_feature_lineage(
            runtime_config,
            selection=selection,
        )
    default_root = (
        str(OFFLINE_DATA_ROOT)
        if runtime_config.environment == "local"
        else runtime_config.storage.feast_root
    )
    root = str(output_root or default_root)
    storage_options = _delta_storage_options(runtime_config)
    tables = {}
    for view_name, spec in FEATURE_SPECS.items():
        table_name = spec["table"]
        entity_key = spec["entity_key"]
        expected_columns = [entity_key, "event_timestamp", *spec["features"]]
        source = gold_source_uri(runtime_config, table_name)
        target = join_data_root(root, table_name)
        delta_table = DeltaTable(source, storage_options=storage_options)
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
        output_rows, output_timestamp_type = _write_dataset(
            target,
            snapshot,
            replace_existing=not immutable,
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
            "target": target,
            "entity_key": entity_key,
            "source_rows": source_rows,
            "output_rows": output_rows,
            "event_timestamp_type": output_timestamp_type,
        }
        print(f"PASS | export {table_name} | rows={output_rows}")

    manifest = build_export_manifest(
        environment=runtime_config.environment,
        dataset_version=dataset_version,
        tables=tables,
        exported_at=datetime.now(timezone.utc).isoformat(),
        lineage={
            "source_feature_run_id": source_feature_run_id,
            "snapshot_id": snapshot_id,
            "source_feature_root": runtime_config.storage.gold_root,
            "output_root": root,
            "source_silver_run_id": (
                selection.silver_run_id if selection is not None else None
            ),
            "source_git_sha": selection.git_sha if selection is not None else None,
            "spark_profile": (
                selection.spark_profile if selection is not None else None
            ),
            "feature_selection_digest": (
                selection.evidence_digest if selection is not None else None
            ),
            "source_evidence_digest": (
                selection.source_evidence_digest if selection is not None else None
            ),
        },
    )
    manifest_uri = (
        f"{root.rstrip('/')}/_control/export_manifest.json"
        if immutable
        else f"{root.rstrip('/')}_manifest.json"
    )
    _write_manifest(manifest_uri, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_runtime_config(args.environment)
        cloud_lineage = (
            args.source_feature_root,
            args.source_feature_run_id,
            args.snapshot_id,
            args.output_root,
        )
        if args.environment == "gcp":
            if any(value is None for value in (*cloud_lineage, args.dataset_version)):
                raise ValueError(
                    "gcp export requires --source-feature-root, "
                    "--source-feature-run-id, --snapshot-id, --dataset-version, "
                    "and --output-root"
                )
            if args.feature_selection_evidence is None:
                raise ValueError("gcp export requires --feature-selection-evidence")
            config = apply_feature_gold_root(config, args.source_feature_root)
            selection = load_feature_selection_evidence(
                args.feature_selection_evidence
            )
        else:
            selection = None
        dataset_version = args.dataset_version or "active"
        export_gold_snapshots(
            config,
            args.output_root,
            dataset_version=dataset_version,
            source_feature_run_id=args.source_feature_run_id,
            snapshot_id=args.snapshot_id,
            immutable=args.environment == "gcp",
            selection=selection,
        )
    except Exception as exc:
        print(f"ERROR | {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
