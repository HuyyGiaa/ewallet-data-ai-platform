"""Publish a validated training snapshot with create-only GCS semantics."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from google.api_core.exceptions import NotFound, PreconditionFailed

from ai_platform.features.feast.cloud_boundary import CloudFeastTrainingBoundary
from ai_platform.features.feast.validation.contracts import (
    CANONICAL_COMPARABLE_VALUE_COUNT,
    CANONICAL_SAMPLE_ROW_COUNT,
    HISTORICAL_FEATURE_COUNT,
    require_registry_readback_evidence,
)
from ai_platform.ml.training.contract import MODEL_FEATURE_COLUMNS, OUTPUT_COLUMNS


PUBLICATION_AUTHORIZATION = "CREATE_ONLY_TRAINING_SNAPSHOT"
COMPLETION_MARKER = "_control/publication_manifest.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RemoteObject:
    generation: int
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class LocalObject:
    name: str
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class TrustedRegistryIdentity:
    readback_evidence: Path
    uri: str
    generation: int
    sha256: str
    execution_git_sha: str


class TrainingSnapshotStore(Protocol):
    def list_objects(self, root_uri: str) -> tuple[str, ...]: ...

    def create(self, uri: str, source: Path) -> int: ...

    def read(self, uri: str, *, generation: int) -> RemoteObject: ...


class GCSTrainingSnapshotStore:
    """GCS adapter with explicit create-only writes and pinned readback."""

    def __init__(self, *, project_id: str, data_bucket: str, client=None) -> None:
        if client is None:
            from google.cloud import storage

            client = storage.Client(project=project_id)
        self._bucket_name = data_bucket
        self._client = client
        self._bucket = client.bucket(data_bucket)

    def _object_name(self, uri: str) -> str:
        parsed = urlsplit(uri)
        if parsed.scheme != "gs" or parsed.netloc != self._bucket_name:
            raise ValueError("training snapshot URI is outside the canonical bucket")
        name = parsed.path.lstrip("/")
        if not name:
            raise ValueError("training snapshot URI must identify an object or prefix")
        return name

    def list_objects(self, root_uri: str) -> tuple[str, ...]:
        prefix = self._object_name(root_uri).rstrip("/")
        names: list[str] = []
        exact = self._bucket.blob(prefix)
        try:
            exact.reload()
        except NotFound:
            pass
        else:
            names.append(prefix)
        names.extend(
            blob.name
            for blob in self._client.list_blobs(
                self._bucket,
                prefix=f"{prefix}/",
            )
        )
        return tuple(sorted(set(names)))

    def create(self, uri: str, source: Path) -> int:
        blob = self._bucket.blob(self._object_name(uri))
        try:
            blob.upload_from_filename(
                str(source),
                content_type="application/octet-stream",
                if_generation_match=0,
                retry=None,
            )
        except PreconditionFailed as exc:
            raise ValueError(
                "training snapshot create-only conflict: destination object exists"
            ) from exc
        if blob.generation is None:
            raise ValueError("GCS did not return the created object generation")
        return int(blob.generation)

    def read(self, uri: str, *, generation: int) -> RemoteObject:
        blob = self._bucket.blob(self._object_name(uri))
        blob.reload(if_generation_match=generation)
        observed_generation = int(blob.generation)
        if observed_generation != generation:
            raise ValueError("training snapshot generation changed during readback")
        with tempfile.TemporaryFile() as stream:
            blob.download_to_file(stream, if_generation_match=generation)
            stream.seek(0)
            digest = hashlib.sha256()
            size_bytes = 0
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size_bytes += len(chunk)
        return RemoteObject(
            generation=observed_generation,
            sha256=digest.hexdigest(),
            size_bytes=size_bytes,
        )


def _expected_training_lineage(
    boundary: CloudFeastTrainingBoundary,
) -> dict[str, str]:
    return {
        "dataset_version": boundary.dataset_version,
        "source_silver_run_id": boundary.silver_run_id,
        "source_feature_run_id": boundary.feature_run_id,
        "snapshot_id": boundary.snapshot_id,
        "source_git_sha": boundary.source_git_sha,
        "feature_selection_digest": boundary.feature_selection_digest,
    }


def _expected_pit_lineage(boundary: CloudFeastTrainingBoundary) -> dict[str, str]:
    lineage = _expected_training_lineage(boundary)
    lineage["snapshot_id"] = boundary.resolved_feast_snapshot_id
    return lineage


def require_pit_pass_evidence(
    path: Path,
    boundary: CloudFeastTrainingBoundary,
    *,
    registry_identity: TrustedRegistryIdentity,
) -> Mapping[str, object]:
    if not path.is_file():
        raise ValueError("PIT PASS evidence is missing")
    evidence = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema_version": "feast-pit-validation-evidence-v1",
        "status": "PASS",
        "environment": "gcp",
        "sample_rows": CANONICAL_SAMPLE_ROW_COUNT,
        "distinct_sample_cases": CANONICAL_SAMPLE_ROW_COUNT,
        "historical_features_per_case": HISTORICAL_FEATURE_COUNT,
        "expected_comparable_values": CANONICAL_COMPARABLE_VALUE_COUNT,
        "comparable_values": CANONICAL_COMPARABLE_VALUE_COUNT,
        "mismatch_count": 0,
        "labels_passed_to_feast": False,
    }
    if any(evidence.get(name) != value for name, value in expected.items()):
        raise ValueError("PIT evidence is not a complete canonical PASS")
    lineage = evidence.get("validation_export_lineage")
    if not isinstance(lineage, Mapping):
        raise ValueError("PIT evidence lineage is missing")
    for name, value in _expected_pit_lineage(boundary).items():
        if lineage.get(name) != value:
            raise ValueError(f"PIT evidence lineage mismatch: {name}")
    if lineage.get("execution_git_sha") != registry_identity.execution_git_sha:
        raise ValueError("PIT evidence lineage mismatch: execution_git_sha")
    if registry_identity.uri != boundary.feast_registry_uri:
        raise ValueError("expected Registry URI does not match the snapshot boundary")
    if registry_identity.generation <= 0:
        raise ValueError("expected Registry generation must be positive")
    if not _SHA256.fullmatch(registry_identity.sha256):
        raise ValueError("expected Registry SHA-256 is invalid")
    trusted = require_registry_readback_evidence(
        registry_identity.readback_evidence,
        expected_registry_uri=registry_identity.uri,
        expected_snapshot_id=boundary.resolved_feast_snapshot_id,
        expected_snapshot_root=boundary.feast_snapshot_root,
        expected_execution_git_sha=registry_identity.execution_git_sha,
        expected_registry_generation=registry_identity.generation,
        expected_registry_sha256=registry_identity.sha256,
    )
    registry = evidence.get("registry_readback")
    expected_registry = {
        "status": "READBACK_VERIFIED",
        "registry_uri": registry_identity.uri,
        "registry_generation": registry_identity.generation,
        "registry_sha256": registry_identity.sha256,
    }
    if not isinstance(registry, Mapping) or any(
        registry.get(name) != value for name, value in expected_registry.items()
    ):
        raise ValueError("PIT evidence Registry identity is incompatible")
    # Do not accept a self-asserted PIT status: the identity must also be present
    # in the independently supplied durable readback evidence.
    if any(trusted.get(name) != value for name, value in expected_registry.items()):
        raise ValueError("trusted Registry readback identity is incompatible")
    return evidence


def _local_snapshot_files(
    source: Path,
    boundary: CloudFeastTrainingBoundary,
) -> tuple[LocalObject, ...]:
    source = source.resolve()
    if not source.is_dir():
        raise ValueError("local training snapshot directory is missing")
    if (source / "_INCOMPLETE").exists():
        raise ValueError("local training snapshot is incomplete")
    manifest_path = source / "manifest.json"
    digest_path = source / "manifest.sha256"
    if not manifest_path.is_file() or not digest_path.is_file():
        raise ValueError("training snapshot manifest or SHA-256 file is missing")
    manifest_payload = manifest_path.read_bytes()
    expected_digest = digest_path.read_text(encoding="utf-8").strip()
    if not _SHA256.fullmatch(expected_digest):
        raise ValueError("training snapshot manifest SHA-256 is invalid")
    if hashlib.sha256(manifest_payload).hexdigest() != expected_digest:
        raise ValueError("training snapshot manifest SHA-256 mismatch")
    manifest = json.loads(manifest_payload)
    if manifest.get("schema_version") != "f7-training-dataset-v1":
        raise ValueError("training snapshot manifest schema is invalid")
    lineage = manifest.get("lineage")
    if not isinstance(lineage, Mapping):
        raise ValueError("training snapshot manifest lineage is missing")
    for name, value in _expected_training_lineage(boundary).items():
        if lineage.get(name) != value:
            raise ValueError(f"training snapshot manifest lineage mismatch: {name}")
    schema = manifest.get("schema")
    if not isinstance(schema, Mapping) or schema.get(
        "historical_features"
    ) != HISTORICAL_FEATURE_COUNT or schema.get("model_features") != len(
        MODEL_FEATURE_COLUMNS
    ):
        raise ValueError("training snapshot feature contract drifted")
    if schema.get("output_columns") != 50:
        raise ValueError("training snapshot output-column count drifted")
    if schema.get("ordered_output_columns") != list(OUTPUT_COLUMNS):
        raise ValueError("training snapshot ordered output schema drifted")
    if schema.get("ordered_model_feature_columns") != list(MODEL_FEATURE_COLUMNS):
        raise ValueError("training snapshot ordered feature schema drifted")
    if manifest.get("complete_source_scope") is not True:
        raise ValueError("training snapshot does not cover the complete bounded source")
    rows = manifest.get("materialized_rows")
    if isinstance(rows, bool) or not isinstance(rows, int) or rows <= 0:
        raise ValueError("training snapshot materialized row count is invalid")
    split_summary = manifest.get("split_summary")
    if not isinstance(split_summary, Mapping) or set(split_summary) != {
        "train",
        "validation",
        "test",
    }:
        raise ValueError("training snapshot split summary is incomplete")
    split_rows = []
    for split in ("train", "validation", "test"):
        values = split_summary[split]
        if not isinstance(values, Mapping):
            raise ValueError("training snapshot split summary is invalid")
        count = values.get("rows")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("training snapshot split row count is invalid")
        split_rows.append(count)
    if sum(split_rows) != rows:
        raise ValueError("training snapshot row count does not match split summary")
    validation = manifest.get("validation")
    if not isinstance(validation, Mapping) or validation.get("status") != "PASS":
        raise ValueError("training snapshot streaming validation PASS is missing")
    if validation.get("rows") != rows:
        raise ValueError("training snapshot validation row count mismatch")
    checks = validation.get("checks")
    if not isinstance(checks, Mapping) or not checks or not all(
        value is True for value in checks.values()
    ):
        raise ValueError("training snapshot validation checks are incomplete")

    files: list[LocalObject] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("training snapshot cannot contain symbolic links")
        if path.is_file():
            relative_name = path.relative_to(source).as_posix()
            if relative_name == COMPLETION_MARKER:
                raise ValueError("local snapshot contains reserved completion marker")
            digest, size_bytes = _sha256_file(path)
            files.append(
                LocalObject(
                    name=relative_name,
                    path=path,
                    sha256=digest,
                    size_bytes=size_bytes,
                )
            )
    if not any(item.name.endswith(".parquet") for item in files):
        raise ValueError("training snapshot contains no Parquet data")
    return tuple(files)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size_bytes += len(chunk)
    return digest.hexdigest(), size_bytes


def _object_name(root: str, relative_name: str, bucket: str) -> str:
    prefix = root.removeprefix(f"gs://{bucket}/")
    return f"{prefix}/{relative_name}"


def _verify_remote_object(
    store: TrainingSnapshotStore,
    *,
    root: str,
    item: Mapping[str, object],
) -> None:
    remote = store.read(
        f"{root}/{item['name']}", generation=int(item["generation"])
    )
    if (
        remote.generation != item["generation"]
        or remote.sha256 != item["sha256"]
        or remote.size_bytes != item["size_bytes"]
    ):
        raise ValueError("training snapshot generation or SHA-256 readback mismatch")


def _expected_names(
    root: str,
    bucket: str,
    inventory: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    return tuple(
        sorted(_object_name(root, str(item["name"]), bucket) for item in inventory)
    )


def publish_training_snapshot(
    store: TrainingSnapshotStore,
    *,
    boundary: CloudFeastTrainingBoundary,
    source: Path,
    pit_evidence: Path,
    registry_identity: TrustedRegistryIdentity,
    execution_git_sha: str,
    authorization: str,
) -> dict[str, object]:
    if authorization != PUBLICATION_AUTHORIZATION:
        raise ValueError(
            f"publication authorization must equal {PUBLICATION_AUTHORIZATION!r}"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", execution_git_sha):
        raise ValueError("execution_git_sha must be a full lowercase Git SHA")
    if boundary.feast_snapshot_id is None:
        raise ValueError("publication requires an explicit Feast Offline snapshot ID")
    if boundary.resolved_feast_snapshot_id == boundary.snapshot_id:
        raise ValueError(
            "Training Snapshot ID must differ from Feast Offline snapshot ID"
        )
    require_pit_pass_evidence(
        pit_evidence, boundary, registry_identity=registry_identity
    )
    files = _local_snapshot_files(source, boundary)
    root = boundary.training_snapshot_root.rstrip("/")
    existing = store.list_objects(root)
    if existing:
        raise ValueError("training snapshot destination already exists")

    inventory: list[dict[str, object]] = []
    for local in files:
        uri = f"{root}/{local.name}"
        generation = store.create(uri, local.path)
        remote = store.read(uri, generation=generation)
        if (
            remote.generation != generation
            or remote.sha256 != local.sha256
            or remote.size_bytes != local.size_bytes
        ):
            raise ValueError("training snapshot byte-for-byte readback mismatch")
        inventory.append(
            {
                "name": local.name,
                "generation": generation,
                "sha256": local.sha256,
                "size_bytes": local.size_bytes,
            }
        )

    # The completion marker is the commit point. Re-read every data object at
    # its expected generation immediately before creating it; a same-name
    # concurrent replacement therefore cannot be hidden by a names-only list.
    if store.list_objects(root) != _expected_names(root, boundary.bucket, inventory):
        raise ValueError("training snapshot pre-commit object inventory mismatch")
    for item in inventory:
        _verify_remote_object(store, root=root, item=item)

    manifest = next(item for item in inventory if item["name"] == "manifest.json")
    completion = {
        "schema_version": "training-snapshot-completion-v1",
        "status": "COMPLETE",
        "training_snapshot_root": root,
        **_expected_training_lineage(boundary),
        "feast_snapshot_id": boundary.resolved_feast_snapshot_id,
        "execution_git_sha": execution_git_sha,
        "registry_uri": registry_identity.uri,
        "registry_generation": registry_identity.generation,
        "registry_sha256": registry_identity.sha256,
        "manifest_sha256": manifest["sha256"],
        "object_count": len(inventory),
        "objects": inventory,
    }
    with tempfile.TemporaryDirectory() as temporary:
        marker_path = Path(temporary) / "publication_manifest.json"
        marker_path.write_text(
            json.dumps(completion, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        marker_sha256, marker_size = _sha256_file(marker_path)
        marker_generation = store.create(
            f"{root}/{COMPLETION_MARKER}", marker_path
        )
    marker = {
        "name": COMPLETION_MARKER,
        "generation": marker_generation,
        "sha256": marker_sha256,
        "size_bytes": marker_size,
    }
    _verify_remote_object(store, root=root, item=marker)
    final_inventory = [*inventory, marker]
    if store.list_objects(root) != _expected_names(
        root, boundary.bucket, final_inventory
    ):
        raise ValueError("training snapshot final object inventory mismatch")
    for item in final_inventory:
        _verify_remote_object(store, root=root, item=item)

    return {
        "schema_version": "training-snapshot-publication-evidence-v1",
        "status": "PUBLISHED_AND_VERIFIED",
        "mode": "create-only",
        "training_snapshot_root": root,
        **_expected_training_lineage(boundary),
        "feast_snapshot_id": boundary.resolved_feast_snapshot_id,
        "execution_git_sha": execution_git_sha,
        "historical_feature_count": HISTORICAL_FEATURE_COUNT,
        "model_input_count": len(MODEL_FEATURE_COLUMNS),
        "registry_uri": registry_identity.uri,
        "registry_generation": registry_identity.generation,
        "registry_sha256": registry_identity.sha256,
        "manifest_sha256": manifest["sha256"],
        "completion_marker_generation": marker_generation,
        "completion_marker_sha256": marker_sha256,
        "completion_marker_lineage": {
            "training_snapshot_root": completion["training_snapshot_root"],
            "dataset_version": completion["dataset_version"],
            "source_silver_run_id": completion["source_silver_run_id"],
            "source_feature_run_id": completion["source_feature_run_id"],
            "snapshot_id": completion["snapshot_id"],
            "source_git_sha": completion["source_git_sha"],
            "feature_selection_digest": completion["feature_selection_digest"],
            "feast_snapshot_id": completion["feast_snapshot_id"],
            "registry_uri": completion["registry_uri"],
            "registry_generation": completion["registry_generation"],
            "registry_sha256": completion["registry_sha256"],
            "manifest_sha256": completion["manifest_sha256"],
        },
        "object_count": len(inventory),
        "objects": inventory,
    }


def write_evidence(path: Path, evidence: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, indent=2, sort_keys=True)
        stream.write("\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--data-bucket", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--silver-run-id", required=True)
    parser.add_argument("--feature-run-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--feast-snapshot-id", required=True)
    parser.add_argument("--source-git-sha", required=True)
    parser.add_argument("--feature-selection-digest", required=True)
    parser.add_argument("--spark-profile", default="optimized")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--pit-evidence", type=Path, required=True)
    parser.add_argument(
        "--trusted-registry-readback-evidence", type=Path, required=True
    )
    parser.add_argument("--expected-registry-uri", required=True)
    parser.add_argument("--expected-registry-generation", type=int, required=True)
    parser.add_argument("--expected-registry-sha256", required=True)
    parser.add_argument("--expected-registry-execution-git-sha", required=True)
    parser.add_argument("--execution-git-sha", required=True)
    parser.add_argument("--authorization", required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    boundary = CloudFeastTrainingBoundary(
        bucket=args.data_bucket,
        dataset_version=args.dataset_version,
        silver_run_id=args.silver_run_id,
        feature_run_id=args.feature_run_id,
        snapshot_id=args.snapshot_id,
        source_git_sha=args.source_git_sha,
        spark_profile=args.spark_profile,
        feature_selection_digest=args.feature_selection_digest,
        feast_snapshot_id=args.feast_snapshot_id,
    )
    if args.evidence_output.exists():
        raise FileExistsError(
            f"immutable evidence output already exists: {args.evidence_output}"
        )
    evidence = publish_training_snapshot(
        GCSTrainingSnapshotStore(
            project_id=args.project_id,
            data_bucket=args.data_bucket,
        ),
        boundary=boundary,
        source=args.source,
        pit_evidence=args.pit_evidence,
        registry_identity=TrustedRegistryIdentity(
            readback_evidence=args.trusted_registry_readback_evidence,
            uri=args.expected_registry_uri,
            generation=args.expected_registry_generation,
            sha256=args.expected_registry_sha256,
            execution_git_sha=args.expected_registry_execution_git_sha,
        ),
        execution_git_sha=args.execution_git_sha,
        authorization=args.authorization,
    )
    write_evidence(args.evidence_output, evidence)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
