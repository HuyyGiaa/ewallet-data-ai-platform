"""Validate and flatten provenance from one bounded training snapshot."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ai_platform.ml.tracking.config import SnapshotConfig
from ai_platform.ml.training.contract import MODEL_FEATURE_COLUMNS


COMPLETION_MARKER = Path("_control/publication_manifest.json")


@dataclass(frozen=True)
class SnapshotProvenance:
    dataset_version: str
    silver_run_id: str
    feature_run_id: str
    snapshot_id: str
    training_snapshot_uri: str
    manifest_sha256: str
    row_count: int
    split_counts: dict[str, int]
    positive_count: int
    negative_count: int
    feature_count: int
    feature_schema_sha256: str

    def mlflow_params(self) -> dict[str, str | int]:
        values: dict[str, str | int] = {
            "dataset_version": self.dataset_version,
            "silver_run_id": self.silver_run_id,
            "feature_run_id": self.feature_run_id,
            "snapshot_id": self.snapshot_id,
            "training_snapshot_uri": self.training_snapshot_uri,
            "snapshot_manifest_sha256": self.manifest_sha256,
            "snapshot_row_count": self.row_count,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "feature_count": self.feature_count,
            "feature_schema_sha256": self.feature_schema_sha256,
        }
        values.update(
            {f"{name}_row_count": count for name, count in self.split_counts.items()}
        )
        return values


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def load_completion_inventory(marker_path: Path) -> tuple[dict, dict[str, dict]]:
    """Load the final marker and reject structurally unsafe object inventory."""

    if not marker_path.is_file():
        raise ValueError("training snapshot completion marker is missing")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if not isinstance(marker, dict):
        raise ValueError("training snapshot completion marker is invalid")
    objects = marker.get("objects")
    if (
        not isinstance(objects, list)
        or marker.get("object_count") != len(objects)
        or not objects
    ):
        raise ValueError("training snapshot completion inventory is invalid")
    inventory: dict[str, dict] = {}
    for item in objects:
        if not isinstance(item, dict):
            raise ValueError("training snapshot completion inventory is invalid")
        name = item.get("name")
        sha256 = item.get("sha256")
        size_bytes = item.get("size_bytes")
        generation = item.get("generation")
        if (
            not isinstance(name, str)
            or name.startswith("/")
            or ".." in Path(name).parts
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or not isinstance(generation, int)
            or generation <= 0
            or name in inventory
        ):
            raise ValueError("training snapshot completion inventory is invalid")
        inventory[name] = item
    return marker, inventory


def load_snapshot_provenance(
    dataset_path: Path,
    expected: SnapshotConfig,
    *,
    verify_inventory: bool = True,
) -> SnapshotProvenance:
    dataset_path = Path(dataset_path)
    manifest_path = dataset_path / "manifest.json"
    payload = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(payload).hexdigest()
    if manifest_sha256 != expected.manifest_sha256:
        raise ValueError(
            "snapshot manifest checksum mismatch: "
            f"{manifest_sha256} != {expected.manifest_sha256}"
        )
    _require_completion_marker(
        dataset_path,
        expected=expected,
        manifest_sha256=manifest_sha256,
        verify_inventory=verify_inventory,
    )
    manifest = json.loads(payload)
    if not isinstance(manifest, dict):
        raise ValueError("snapshot manifest must be a JSON object")
    lineage = manifest.get("lineage")
    if not isinstance(lineage, dict):
        raise ValueError("snapshot manifest lineage is required")
    expected_lineage = {
        "dataset_version": expected.dataset_version,
        "source_silver_run_id": expected.silver_run_id,
        "source_feature_run_id": expected.feature_run_id,
        "snapshot_id": expected.snapshot_id,
    }
    for field, wanted in expected_lineage.items():
        if lineage.get(field) != wanted:
            raise ValueError(
                f"snapshot manifest {field}={lineage.get(field)!r} != {wanted!r}"
            )

    split_summary = manifest.get("split_summary")
    if not isinstance(split_summary, dict):
        raise ValueError("snapshot manifest split_summary is required")
    split_counts: dict[str, int] = {}
    positive_count = 0
    negative_count = 0
    for split in ("train", "validation", "test"):
        values = split_summary.get(split)
        if not isinstance(values, dict):
            raise ValueError(f"snapshot split_summary.{split} is required")
        rows = _integer(values.get("rows"), f"split_summary.{split}.rows")
        fraud = _integer(values.get("fraud"), f"split_summary.{split}.fraud")
        normal = _integer(values.get("normal"), f"split_summary.{split}.normal")
        if fraud + normal != rows:
            raise ValueError(f"snapshot split_summary.{split} class counts mismatch")
        split_counts[split] = rows
        positive_count += fraud
        negative_count += normal

    row_count = _integer(manifest.get("materialized_rows"), "materialized_rows")
    if sum(split_counts.values()) != row_count:
        raise ValueError("snapshot split rows do not equal materialized_rows")
    schema = manifest.get("schema")
    if not isinstance(schema, dict):
        raise ValueError("snapshot manifest schema is required")
    feature_count = _integer(schema.get("model_features"), "schema.model_features")
    if feature_count != len(MODEL_FEATURE_COLUMNS):
        raise ValueError(
            f"snapshot feature count {feature_count} != {len(MODEL_FEATURE_COLUMNS)}"
        )
    if schema.get("ordered_model_feature_columns") != list(MODEL_FEATURE_COLUMNS):
        raise ValueError("snapshot ordered model feature contract mismatch")
    if manifest.get("complete_source_scope") is not True:
        raise ValueError("snapshot does not cover the complete bounded source")
    validation = manifest.get("validation")
    if not isinstance(validation, dict) or validation.get("status") != "PASS":
        raise ValueError("snapshot validation PASS is required")
    feature_schema = json.dumps(
        list(MODEL_FEATURE_COLUMNS), separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return SnapshotProvenance(
        dataset_version=expected.dataset_version,
        silver_run_id=expected.silver_run_id,
        feature_run_id=expected.feature_run_id,
        snapshot_id=expected.snapshot_id,
        training_snapshot_uri=expected.training_snapshot_uri,
        manifest_sha256=manifest_sha256,
        row_count=row_count,
        split_counts=split_counts,
        positive_count=positive_count,
        negative_count=negative_count,
        feature_count=feature_count,
        feature_schema_sha256=hashlib.sha256(feature_schema).hexdigest(),
    )


def _require_completion_marker(
    dataset_path: Path,
    *,
    expected: SnapshotConfig,
    manifest_sha256: str,
    verify_inventory: bool,
) -> None:
    marker_path = dataset_path / COMPLETION_MARKER
    marker, inventory = load_completion_inventory(marker_path)
    required = {
        "schema_version": "training-snapshot-completion-v1",
        "status": "COMPLETE",
        "training_snapshot_root": expected.training_snapshot_uri.rstrip("/"),
        "dataset_version": expected.dataset_version,
        "source_silver_run_id": expected.silver_run_id,
        "source_feature_run_id": expected.feature_run_id,
        "snapshot_id": expected.snapshot_id,
        "manifest_sha256": manifest_sha256,
    }
    if not isinstance(marker, dict) or any(
        marker.get(name) != value for name, value in required.items()
    ):
        raise ValueError("training snapshot completion identity is incompatible")
    expected_objects = {
        name: (item["sha256"], item["size_bytes"])
        for name, item in inventory.items()
    }
    if expected_objects.get("manifest.json") != (
        manifest_sha256,
        len((dataset_path / "manifest.json").read_bytes()),
    ):
        raise ValueError("training snapshot manifest inventory mismatch")
    if not verify_inventory:
        return
    actual_names = {
        path.relative_to(dataset_path).as_posix()
        for path in dataset_path.rglob("*")
        if path.is_file() and path != marker_path
    }
    if actual_names != set(expected_objects):
        raise ValueError("training snapshot file inventory mismatch")
    for name, (expected_sha256, expected_size) in expected_objects.items():
        path = dataset_path / name
        digest = hashlib.sha256()
        size_bytes = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size_bytes += len(chunk)
        if digest.hexdigest() != expected_sha256 or size_bytes != expected_size:
            raise ValueError(f"training snapshot object inventory mismatch: {name}")
