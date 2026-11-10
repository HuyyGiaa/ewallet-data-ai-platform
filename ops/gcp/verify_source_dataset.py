"""Fail-closed, read-only verification of one published source dataset.

Used after publication and as the first gate of the DP1-DP3 campaign. Checking
that ``dataset_manifest.json`` exists is not enough. This gate requires:

* a COMPLETE manifest that satisfies the Bronze source contract (tables, shard
  files, totals, checksums, fraud counts, declared extensions);
* the registry contract for the dataset (merchant_history, shard count, seed,
  generator config and physical-byte tolerance);
* the immutable plan beside it (same digest, same geometry);
* an exact object inventory: every shard manifest and Parquet object present with
  the published size and MD5, and nothing unexpected under the dataset root;
* optionally, the exact SHA-256 of the manifest the campaign was approved against.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from data_platform.config.dataset_identity import (
    APPROVED_DATASETS,
    DatasetIdentity,
    require_approved_dataset,
    resolve_storage_context,
)
from data_platform.storage.scripts.config import MERCHANT_HISTORY_TABLE
from data_platform.generation.src.sharded.planner import canonical_json_sha256
from data_platform.generation.src.sharded.storage import GcloudObjectStore, ObjectStore
from data_platform.ingestion.spark.source_to_bronze import (
    expected_merchant_history,
    manifest_physical_tables,
    validate_source_manifest,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class SourceGateError(ValueError):
    """The published source dataset does not satisfy its contract."""


def list_objects(prefix_uri: str, runner: Callable[..., Any] = subprocess.run) -> dict[str, dict[str, Any]]:
    """Return one listing of the dataset root keyed by object name inside the bucket."""

    completed = runner(
        ["gcloud", "storage", "objects", "list", f"{prefix_uri.rstrip('/')}/**", "--format=json"],
        check=False, text=True, capture_output=True,
    )
    if completed.returncode != 0:
        raise SourceGateError(f"could not list {prefix_uri}: {completed.stderr.strip()}")
    objects = json.loads(completed.stdout or "[]")
    if not isinstance(objects, list):
        raise SourceGateError("gcloud listing must be a JSON array")
    return {str(item["name"]): item for item in objects}


def load_generator_config_digest(identity: DatasetIdentity) -> str | None:
    if identity.generator_config is None:
        return None
    import yaml

    value = yaml.safe_load((REPOSITORY_ROOT / identity.generator_config).read_text(encoding="utf-8"))
    return canonical_json_sha256(value)


def check_registry_contract(
    manifest: Mapping[str, Any], plan: Mapping[str, Any], identity: DatasetIdentity
) -> None:
    """Compare the published dataset with what the registry pins for this version."""

    if plan.get("digest") != manifest.get("plan_digest"):
        raise SourceGateError("plan digest differs from the dataset manifest")
    if plan.get("dataset_version") != manifest.get("dataset_version"):
        raise SourceGateError("plan dataset_version differs from the dataset manifest")
    if int(plan.get("shard_count", -1)) != int(manifest.get("shard_count", -2)):
        raise SourceGateError("plan shard_count differs from the dataset manifest")
    if identity.source_shard_count is not None and int(manifest["shard_count"]) != identity.source_shard_count:
        raise SourceGateError(
            f"expected {identity.source_shard_count} shards, manifest has {manifest['shard_count']}"
        )
    if identity.global_seed is not None and plan.get("global_seed") != identity.global_seed:
        raise SourceGateError(f"expected global seed {identity.global_seed}, plan has {plan.get('global_seed')}")
    digest = load_generator_config_digest(identity)
    if digest is not None and plan.get("base_config_sha256") != digest:
        raise SourceGateError(f"plan was not generated with {identity.generator_config}")
    if identity.size_tolerance_ratio is not None:
        total = int(manifest["total_bytes"])
        low = identity.target_source_bytes * (1 - identity.size_tolerance_ratio)
        high = identity.target_source_bytes * (1 + identity.size_tolerance_ratio)
        if not low <= total <= high:
            raise SourceGateError(f"physical bytes {total} outside the accepted range {low:.0f}..{high:.0f}")


def check_object_inventory(
    manifest: Mapping[str, Any],
    listing: Mapping[str, Mapping[str, Any]],
    *,
    prefix: str,
) -> dict[str, int]:
    """Require exactly the published shard manifests and Parquet objects."""

    expected: dict[str, Mapping[str, Any]] = {}
    for table in manifest_physical_tables(manifest):
        for item in manifest["tables"][table]["files"]:
            expected[f"{prefix}/{item['path']}"] = item
    shard_count = int(manifest["shard_count"])
    control = {
        f"{prefix}/_control/plan.json",
        f"{prefix}/_control/dataset_manifest.json",
        *(f"{prefix}/_control/shards/shard-{shard:05d}.json" for shard in range(shard_count)),
    }
    missing = sorted((set(expected) | control) - set(listing))
    if missing:
        raise SourceGateError(f"{len(missing)} source objects are missing, first: {missing[:3]}")
    unexpected = sorted(set(listing) - set(expected) - control)
    if unexpected:
        raise SourceGateError(f"{len(unexpected)} unexpected source objects, first: {unexpected[:3]}")
    for name, item in expected.items():
        remote = listing[name]
        if int(remote["size"]) != int(item["bytes"]) or remote.get("md5_hash") != item["md5_base64"]:
            raise SourceGateError(f"source object does not match its manifest entry: {name}")
    return {"parquet_objects": len(expected), "shard_manifests": shard_count}


def verify_source_dataset(
    *,
    bucket: str,
    dataset_version: str,
    expected_manifest_sha256: str | None = None,
    store: ObjectStore | None = None,
    lister: Callable[[str], Mapping[str, Mapping[str, Any]]] = list_objects,
) -> dict[str, Any]:
    """Run every check and return a factual report; raise on the first violation."""

    identity = require_approved_dataset(dataset_version)
    resolve_storage_context(dataset_version, bucket)
    store = store or GcloudObjectStore()
    prefix = f"source/{dataset_version}"
    root = f"gs://{bucket}/{prefix}"
    manifest_uri = f"{root}/_control/dataset_manifest.json"
    if not store.exists(manifest_uri):
        raise SourceGateError(f"source dataset is not finalized: {manifest_uri} is absent")
    manifest_text = store.read_text(manifest_uri)
    manifest_sha256 = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
    if expected_manifest_sha256 is not None and manifest_sha256 != expected_manifest_sha256:
        raise SourceGateError(
            f"manifest SHA-256 {manifest_sha256} differs from the approved {expected_manifest_sha256}"
        )
    manifest = validate_source_manifest(
        json.loads(manifest_text),
        expected_dataset_version=dataset_version,
        merchant_history=expected_merchant_history(dataset_version),
    )
    plan = json.loads(store.read_text(f"{root}/_control/plan.json"))
    check_registry_contract(manifest, plan, identity)
    inventory = check_object_inventory(manifest, lister(root), prefix=prefix)
    tables = manifest["tables"]
    return {
        "schema_version": "campaign-source-gate-v1",
        "status": "PASS",
        "dataset_version": dataset_version,
        "source_root": root,
        "manifest_uri": manifest_uri,
        "manifest_sha256": manifest_sha256,
        "plan_digest": manifest["plan_digest"],
        "shard_count": int(manifest["shard_count"]),
        "physical_bytes": int(manifest["total_bytes"]),
        "logical_transactions": int(manifest["logical_transactions"]),
        "physical_transactions": int(manifest["physical_transactions"]),
        "merchant_history_rows": int(tables[MERCHANT_HISTORY_TABLE]["rows"]) if MERCHANT_HISTORY_TABLE in tables else 0,
        "source_extensions": sorted(manifest.get("source_extensions", {})),
        "tables": sorted(tables),
        **inventory,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--dataset-version", required=True, choices=sorted(APPROVED_DATASETS))
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = verify_source_dataset(
            bucket=args.bucket,
            dataset_version=args.dataset_version,
            expected_manifest_sha256=args.expected_manifest_sha256,
        )
    except (SourceGateError, ValueError, KeyError) as error:
        print(f"SOURCE GATE FAILED: {error}", file=sys.stderr)
        return 1
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
