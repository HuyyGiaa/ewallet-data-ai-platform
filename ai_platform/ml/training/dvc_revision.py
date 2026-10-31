"""Prepare a small DVC revision descriptor for a published Training Snapshot.

The descriptor versions immutable GCS provenance; it never copies the lakehouse
or Training Snapshot payload into DVC.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def build_revision_descriptor(
    publication: Mapping[str, object],
) -> dict[str, object]:
    """Validate publication evidence and return the DVC-tracked descriptor."""

    required = {
        "schema_version": "training-snapshot-publication-evidence-v1",
        "status": "PUBLISHED_AND_VERIFIED",
        "mode": "create-only",
    }
    if any(publication.get(key) != value for key, value in required.items()):
        raise ValueError("DVC preparation requires verified publication evidence")

    snapshot_id = publication.get("snapshot_id")
    feast_snapshot_id = publication.get("feast_snapshot_id")
    root = publication.get("training_snapshot_root")
    if (
        not isinstance(snapshot_id, str)
        or not isinstance(feast_snapshot_id, str)
        or snapshot_id == feast_snapshot_id
        or not isinstance(root, str)
        or not root.startswith("gs://")
        or not root.rstrip("/").endswith(f"/{snapshot_id}")
    ):
        raise ValueError("DVC preparation received incompatible snapshot identities")

    sha_fields = (
        "manifest_sha256",
        "completion_marker_sha256",
        "registry_sha256",
    )
    if any(
        not isinstance(publication.get(name), str)
        or not _SHA256.fullmatch(str(publication[name]))
        for name in sha_fields
    ):
        raise ValueError("DVC preparation received an invalid immutable checksum")
    execution_git_sha = publication.get("execution_git_sha")
    if not isinstance(execution_git_sha, str) or not _GIT_SHA.fullmatch(
        execution_git_sha
    ):
        raise ValueError("DVC preparation requires the publication execution Git SHA")

    object_count = _positive_integer(publication.get("object_count"), "object_count")
    objects = publication.get("objects")
    if not isinstance(objects, list) or len(objects) != object_count:
        raise ValueError("DVC preparation received an incomplete object inventory")
    names: set[str] = set()
    inventory: dict[str, Mapping[str, object]] = {}
    for item in objects:
        if not isinstance(item, Mapping):
            raise ValueError("DVC preparation received an invalid object inventory")
        name = item.get("name")
        sha256 = item.get("sha256")
        if (
            not isinstance(name, str)
            or name in names
            or not isinstance(sha256, str)
            or not _SHA256.fullmatch(sha256)
        ):
            raise ValueError("DVC preparation received an invalid object inventory")
        _positive_integer(item.get("generation"), f"objects[{name}].generation")
        size_bytes = item.get("size_bytes")
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
        ):
            raise ValueError("DVC preparation received an invalid object size")
        names.add(name)
        inventory[name] = item
    if "manifest.json" not in names:
        raise ValueError("DVC preparation inventory does not contain manifest.json")
    if inventory["manifest.json"].get("sha256") != publication["manifest_sha256"]:
        raise ValueError("DVC manifest checksum does not match object inventory")

    lineage_names = (
        "dataset_version",
        "source_silver_run_id",
        "source_feature_run_id",
        "source_git_sha",
        "feature_selection_digest",
    )
    if any(
        not isinstance(publication.get(name), str) or not publication.get(name)
        for name in lineage_names
    ):
        raise ValueError("DVC preparation received incomplete source lineage")

    dataset_version = str(publication["dataset_version"])
    registry_uri = publication.get("registry_uri")
    parsed_root = urlparse(root)
    if (
        not isinstance(registry_uri, str)
        or not registry_uri
        or parsed_root.scheme != "gs"
        or not parsed_root.netloc
    ):
        raise ValueError("DVC preparation requires a valid gs:// Registry URI")
    expected_registry_uri = (
        f"gs://{parsed_root.netloc}/features/registries/{dataset_version}/"
        f"{feast_snapshot_id}/registry.db"
    )
    if registry_uri != expected_registry_uri:
        raise ValueError("DVC Registry URI is not bound to the Feast snapshot")

    completion_lineage = publication.get("completion_marker_lineage")
    expected_completion_lineage = {
        "training_snapshot_root": root.rstrip("/"),
        "dataset_version": dataset_version,
        "source_silver_run_id": publication["source_silver_run_id"],
        "source_feature_run_id": publication["source_feature_run_id"],
        "snapshot_id": snapshot_id,
        "source_git_sha": publication["source_git_sha"],
        "feature_selection_digest": publication["feature_selection_digest"],
        "feast_snapshot_id": feast_snapshot_id,
        "registry_uri": registry_uri,
        "registry_generation": publication.get("registry_generation"),
        "registry_sha256": publication["registry_sha256"],
        "manifest_sha256": publication["manifest_sha256"],
    }
    if completion_lineage != expected_completion_lineage:
        raise ValueError("DVC publication and completion-marker lineage mismatch")

    return {
        "schema_version": "training-snapshot-dvc-revision-v1",
        "snapshot_id": snapshot_id,
        "training_snapshot_uri": root.rstrip("/"),
        "feast_snapshot_id": feast_snapshot_id,
        "manifest_sha256": publication["manifest_sha256"],
        "completion_marker": {
            "path": "_control/publication_manifest.json",
            "generation": _positive_integer(
                publication.get("completion_marker_generation"),
                "completion_marker_generation",
            ),
            "sha256": publication["completion_marker_sha256"],
        },
        "registry": {
            "uri": registry_uri,
            "generation": _positive_integer(
                publication.get("registry_generation"), "registry_generation"
            ),
            "sha256": publication["registry_sha256"],
        },
        "execution_git_sha": execution_git_sha,
        "lineage": {name: publication[name] for name in lineage_names},
        "object_count": object_count,
        "publication_evidence_sha256": hashlib.sha256(
            json.dumps(publication, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def write_revision_descriptor(
    publication_evidence: Path, output: Path
) -> dict[str, object]:
    if output.exists():
        raise FileExistsError(f"DVC revision descriptor already exists: {output}")
    publication = json.loads(publication_evidence.read_text(encoding="utf-8"))
    if not isinstance(publication, dict):
        raise ValueError("publication evidence must be a JSON object")
    descriptor = build_revision_descriptor(publication)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(descriptor, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return descriptor
