"""Cloud Run entrypoint for bounded ML-2 PIT materialization.

The job runs next to the immutable Feast Offline snapshot so the four source
tables are never copied out of the region. It downloads the exported training
source, runs the existing resumable materializer (bounded Dask retrieval) and
the materialized-dataset validator, then uploads the dataset to a create-only
staging prefix. Publication into ``ml/training-snapshots`` stays a separate,
locally reviewed step performed by ``ops.gcp.publish_training_snapshot``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import storage

from ai_platform.ml.training import materialize, validate_materialized


EXECUTION_ID = re.compile(r"^ml2-mat-[0-9]{12}$")


def _split_gcs(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"expected gs:// URI, got {uri!r}")
    bucket, _, prefix = uri[5:].partition("/")
    if not bucket or not prefix:
        raise ValueError(f"URI needs a bucket and prefix: {uri!r}")
    return bucket, prefix.rstrip("/")


def download_prefix(client: storage.Client, uri: str, target: Path) -> int:
    bucket_name, prefix = _split_gcs(uri)
    count = 0
    for blob in client.list_blobs(bucket_name, prefix=prefix + "/"):
        relative = blob.name[len(prefix) + 1 :]
        if not relative or relative.endswith("/"):
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(destination)
        count += 1
    if count == 0:
        raise FileNotFoundError(f"no objects under {uri}")
    return count


def require_absent(client: storage.Client, uri: str) -> None:
    bucket_name, prefix = _split_gcs(uri)
    if any(True for _ in client.list_blobs(bucket_name, prefix=prefix + "/", max_results=1)):
        raise FileExistsError(f"create-only staging prefix already exists: {uri}")


def upload_create_only(client: storage.Client, root: Path, uri: str) -> list[dict]:
    bucket_name, prefix = _split_gcs(uri)
    bucket = client.bucket(bucket_name)
    inventory = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        blob = bucket.blob(f"{prefix}/{relative}")
        blob.upload_from_filename(path, if_generation_match=0)
        inventory.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "generation": blob.generation,
            }
        )
    return inventory


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--output-uri", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--source-silver-run-id", required=True)
    parser.add_argument("--source-feature-run-id", required=True)
    parser.add_argument("--feast-snapshot-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--source-git-sha", required=True)
    parser.add_argument("--feature-selection-digest", required=True)
    parser.add_argument("--chunk-size", type=int, required=True)
    parser.add_argument("--min-available-memory-gib", type=float, default=2.0)
    args = parser.parse_args(argv)
    if not EXECUTION_ID.fullmatch(args.execution_id):
        parser.error("execution id must look like ml2-mat-YYMMDDhhmmss")
    if not args.output_uri.rstrip("/").endswith(args.execution_id):
        parser.error("output URI must end with the execution id")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = storage.Client()
    require_absent(client, args.output_uri)
    started = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="ml2-") as temporary:
        root = Path(temporary)
        source = root / "source"
        dataset = root / "dataset"
        downloaded = download_prefix(client, args.source_uri, source)
        print(f"PASS | downloaded source objects={downloaded}", flush=True)
        common = [
            "--environment", "gcp",
            "--dataset-version", args.dataset_version,
            "--source-silver-run-id", args.source_silver_run_id,
            "--source-feature-run-id", args.source_feature_run_id,
            "--feast-snapshot-id", args.feast_snapshot_id,
            "--snapshot-id", args.snapshot_id,
            "--source-git-sha", args.source_git_sha,
            "--feature-selection-digest", args.feature_selection_digest,
            "--source", str(source),
            "--output", str(dataset),
            "--chunk-size", str(args.chunk_size),
            "--min-available-memory-gib", str(args.min_available_memory_gib),
            "--max-new-chunks", "1000000",
        ]
        if materialize.main(common) != 0:
            raise RuntimeError("materialization failed; see the ERROR line above")
        if (dataset / "_INCOMPLETE").exists():
            raise RuntimeError("materialization left _INCOMPLETE behind")
        validate_materialized.validate(source, dataset)
        manifest = dataset / "manifest.json"
        (dataset / "manifest.sha256").write_text(
            hashlib.sha256(manifest.read_bytes()).hexdigest() + "\n"
        )
        inventory = upload_create_only(client, dataset, args.output_uri)
    evidence = {
        "schema_version": "ml2-cloud-materialization-evidence-v1",
        "status": "PASS",
        "execution_id": args.execution_id,
        "snapshot_id": args.snapshot_id,
        "source_uri": args.source_uri,
        "output_uri": args.output_uri,
        "chunk_size": args.chunk_size,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "objects": inventory,
    }
    bucket_name, prefix = _split_gcs(args.output_uri)
    client.bucket(bucket_name).blob(
        f"{prefix}/_control/cloud-materialization-evidence.json"
    ).upload_from_string(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        content_type="application/json",
        if_generation_match=0,
    )
    print(json.dumps({"status": "PASS", "objects": len(inventory)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
