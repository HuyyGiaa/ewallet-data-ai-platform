"""Validate the immutable canonical source dataset before DP1 submission."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


PHYSICAL_TABLES = (
    "users",
    "accounts",
    "merchants",
    "devices",
    "transactions_v1",
    "transactions_v2",
    "balance_snapshots",
    "login_events",
    "fraud_labels",
)
CANONICAL_SHARDS = 3696
CANONICAL_LOGICAL_TRANSACTIONS = 369_600_000
CANONICAL_PHYSICAL_TRANSACTIONS = 376_992_000
CANONICAL_FRAUD_LABEL_ROWS = 369_600_000
CANONICAL_FRAUD_ROWS = 7_392_000
CANONICAL_MINIMUM_BYTES = 100_000_000_000
CANONICAL_MAXIMUM_BYTES = 105_000_000_000
_SHARD_MANIFEST = re.compile(r"_control/shards/shard-([0-9]{5})\.json$")


@dataclass(frozen=True)
class ExpectedObject:
    size: int
    md5_base64: str


@dataclass(frozen=True)
class GateResult:
    dataset_version: str
    shard_manifests: int
    parquet_objects: int
    total_bytes: int
    md5_fallback_reloads: int


def _require_equal(actual: Any, expected: Any, description: str) -> None:
    if actual != expected:
        raise ValueError(
            f"{description} mismatch: expected={expected!r}, actual={actual!r}"
        )


def validate_canonical_manifest(
    manifest: Mapping[str, Any],
    *,
    dataset_version: str,
    shard_count: int,
    logical_transactions: int,
    physical_transactions: int,
    fraud_label_rows: int,
    fraud_rows: int,
    minimum_bytes: int,
    maximum_bytes: int,
) -> dict[str, ExpectedObject]:
    """Validate canonical totals and derive the exact expected Parquet inventory."""
    _require_equal(manifest.get("status"), "COMPLETE", "dataset manifest status")
    _require_equal(
        manifest.get("dataset_version"), dataset_version, "dataset version"
    )
    _require_equal(manifest.get("shard_count"), shard_count, "shard count")
    _require_equal(
        manifest.get("logical_transactions"),
        logical_transactions,
        "logical transaction count",
    )
    _require_equal(
        manifest.get("physical_transactions"),
        physical_transactions,
        "physical transaction count",
    )
    _require_equal(
        manifest.get("fraud_label_rows"), fraud_label_rows, "fraud label count"
    )
    _require_equal(manifest.get("fraud_rows"), fraud_rows, "fraud row count")

    scenario_counts = manifest.get("fraud_scenario_counts")
    if not isinstance(scenario_counts, Mapping):
        raise ValueError("fraud scenario counts must be an object")
    _require_equal(
        sum(int(value) for value in scenario_counts.values()),
        fraud_rows,
        "fraud scenario total",
    )

    tables = manifest.get("tables")
    if not isinstance(tables, Mapping) or set(tables) != set(PHYSICAL_TABLES):
        raise ValueError("source physical table inventory is not exactly nine tables")

    expected: dict[str, ExpectedObject] = {}
    total_bytes = 0
    for table in PHYSICAL_TABLES:
        metadata = tables[table]
        if not isinstance(metadata, Mapping):
            raise ValueError(f"table metadata must be an object: {table}")
        files = metadata.get("files")
        if not isinstance(files, list) or len(files) != shard_count:
            raise ValueError(f"table does not contain exactly {shard_count} files: {table}")
        if not metadata.get("schema_sha256"):
            raise ValueError(f"table schema hashes are missing: {table}")

        table_rows = 0
        table_bytes = 0
        shard_ids: set[int] = set()
        for item in files:
            if not isinstance(item, Mapping):
                raise ValueError(f"file metadata must be an object: {table}")
            shard_id = int(item["shard_id"])
            shard_ids.add(shard_id)
            path = PurePosixPath(str(item["path"]))
            if path.is_absolute() or ".." in path.parts or path.parts[0] != table:
                raise ValueError(f"unsafe source object path: {path}")
            if path.suffix != ".parquet":
                raise ValueError(f"source data object is not Parquet: {path}")
            if not item.get("sha256") or not item.get("md5_base64"):
                raise ValueError(f"source checksums are missing: {path}")
            relative_name = path.as_posix()
            if relative_name in expected:
                raise ValueError(f"duplicate source object path: {relative_name}")
            size = int(item["bytes"])
            expected[relative_name] = ExpectedObject(
                size=size,
                md5_base64=str(item["md5_base64"]),
            )
            table_rows += int(item["rows"])
            table_bytes += size
        _require_equal(shard_ids, set(range(shard_count)), f"{table} shard IDs")
        _require_equal(table_rows, int(metadata["rows"]), f"{table} row total")
        _require_equal(table_bytes, int(metadata["bytes"]), f"{table} byte total")
        total_bytes += table_bytes

    _require_equal(
        len(expected), shard_count * len(PHYSICAL_TABLES), "Parquet object count"
    )
    _require_equal(
        int(tables["transactions_v1"]["rows"])
        + int(tables["transactions_v2"]["rows"]),
        physical_transactions,
        "physical transaction table total",
    )
    _require_equal(
        int(tables["fraud_labels"]["rows"]),
        fraud_label_rows,
        "fraud label table total",
    )
    _require_equal(total_bytes, int(manifest.get("total_bytes", -1)), "total bytes")
    if not minimum_bytes <= total_bytes <= maximum_bytes:
        raise ValueError(
            "total bytes outside canonical range: "
            f"expected={minimum_bytes}..{maximum_bytes}, actual={total_bytes}"
        )
    return expected


def verify_gcs_inventory(
    bucket,
    *,
    dataset_prefix: str,
    expected_objects: Mapping[str, ExpectedObject],
    expected_shard_count: int,
) -> GateResult:
    """Verify one paginated listing, reloading only entries whose MD5 is absent."""
    prefix = dataset_prefix.strip("/")
    parquet: dict[str, Any] = {}
    shard_manifests: set[int] = set()

    for blob in bucket.list_blobs(prefix=f"{prefix}/"):
        relative_name = blob.name[len(prefix) + 1 :]
        shard_match = _SHARD_MANIFEST.fullmatch(relative_name)
        if shard_match:
            shard_manifests.add(int(shard_match.group(1)))
            continue
        if relative_name.startswith("_control/") or not relative_name.endswith(
            ".parquet"
        ):
            continue
        if relative_name in parquet:
            raise ValueError(f"duplicate object returned by GCS listing: {blob.name}")
        parquet[relative_name] = blob

    expected_shards = set(range(expected_shard_count))
    if shard_manifests != expected_shards:
        missing = sorted(expected_shards - shard_manifests)
        unexpected = sorted(shard_manifests - expected_shards)
        raise ValueError(
            "shard manifest inventory mismatch: "
            f"missing={missing[:10]}, unexpected={unexpected[:10]}"
        )

    expected_names = set(expected_objects)
    actual_names = set(parquet)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        raise ValueError(
            "Parquet object inventory mismatch: "
            f"missing={missing[:10]}, unexpected={unexpected[:10]}"
        )

    fallback_reloads = 0
    for name, expected in expected_objects.items():
        blob = parquet[name]
        if int(blob.size) != expected.size:
            raise ValueError(
                f"GCS object size mismatch: {name}; "
                f"expected={expected.size}, actual={blob.size}"
            )
        if blob.md5_hash is None:
            blob.reload()
            fallback_reloads += 1
        if blob.md5_hash is None:
            raise ValueError(f"GCS object MD5 unavailable after fallback reload: {name}")
        if blob.md5_hash != expected.md5_base64:
            raise ValueError(
                f"GCS object MD5 mismatch: {name}; "
                f"expected={expected.md5_base64}, actual={blob.md5_hash}"
            )

    return GateResult(
        dataset_version=dataset_prefix.rstrip("/").rsplit("/", 1)[-1],
        shard_manifests=len(shard_manifests),
        parquet_objects=len(parquet),
        total_bytes=sum(item.size for item in expected_objects.values()),
        md5_fallback_reloads=fallback_reloads,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        default=os.environ.get("GCP_PROJECT_ID"),
        help="GCP project used by the Storage client.",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("EWALLET_DATA_BUCKET"),
        help="GCS data bucket (defaults to EWALLET_DATA_BUCKET).",
    )
    parser.add_argument(
        "--dataset-version",
        default=os.environ.get("DATASET_VERSION", "canonical-100gb-v1"),
    )
    args = parser.parse_args(argv)
    if not args.bucket:
        parser.error("--bucket or EWALLET_DATA_BUCKET is required")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    from google.cloud import storage

    args = parse_args(argv)
    client = storage.Client(project=args.project)
    bucket = client.bucket(args.bucket)
    dataset_prefix = f"source/{args.dataset_version}"
    manifest_name = f"{dataset_prefix}/_control/dataset_manifest.json"
    manifest = json.loads(bucket.blob(manifest_name).download_as_text())
    if not isinstance(manifest, Mapping):
        raise ValueError("dataset manifest must be a JSON object")
    expected = validate_canonical_manifest(
        manifest,
        dataset_version=args.dataset_version,
        shard_count=CANONICAL_SHARDS,
        logical_transactions=CANONICAL_LOGICAL_TRANSACTIONS,
        physical_transactions=CANONICAL_PHYSICAL_TRANSACTIONS,
        fraud_label_rows=CANONICAL_FRAUD_LABEL_ROWS,
        fraud_rows=CANONICAL_FRAUD_ROWS,
        minimum_bytes=CANONICAL_MINIMUM_BYTES,
        maximum_bytes=CANONICAL_MAXIMUM_BYTES,
    )
    result = verify_gcs_inventory(
        bucket,
        dataset_prefix=dataset_prefix,
        expected_objects=expected,
        expected_shard_count=CANONICAL_SHARDS,
    )
    print("PASS: canonical source final gate")
    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
