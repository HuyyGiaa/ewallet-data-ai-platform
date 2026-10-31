from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

from google.api_core.exceptions import NotFound, PreconditionFailed

from ai_platform.features.feast.cloud_boundary import CloudFeastTrainingBoundary
from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.ml.training.contract import MODEL_FEATURE_COLUMNS, OUTPUT_COLUMNS
from ops.gcp.publish_training_snapshot import (
    COMPLETION_MARKER,
    GCSTrainingSnapshotStore,
    PUBLICATION_AUTHORIZATION,
    RemoteObject,
    TrustedRegistryIdentity,
    publish_training_snapshot,
)


class FakeStore:
    def __init__(self, *, existing=(), list_error: Exception | None = None) -> None:
        self.objects = {name: b"existing" for name in existing}
        self.generations: dict[str, int] = {name: 1 for name in existing}
        self.list_error = list_error
        self.created: list[str] = []

    def list_objects(self, _root_uri: str) -> tuple[str, ...]:
        if self.list_error is not None:
            raise self.list_error
        return tuple(sorted(self.objects))

    def create(self, uri: str, source: Path) -> int:
        name = urlsplit(uri).path.lstrip("/")
        if name in self.objects:
            raise ValueError("create-only conflict")
        generation = len(self.created) + 11
        self.objects[name] = source.read_bytes()
        self.generations[name] = generation
        self.created.append(name)
        return generation

    def read(self, uri: str, *, generation: int) -> RemoteObject:
        name = urlsplit(uri).path.lstrip("/")
        payload = self.objects[name]
        return RemoteObject(
            generation=self.generations[name],
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )


class PublishTrainingSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.boundary = CloudFeastTrainingBoundary(
            bucket="example-project-data",
            dataset_version="canonical-100gb-v1",
            silver_run_id="canonical-silver-run",
            feature_run_id="canonical-feature-run",
            snapshot_id="canonical-training-snapshot",
            source_git_sha="a" * 40,
            spark_profile="optimized",
            feature_selection_digest="sha256:" + "b" * 64,
            feast_snapshot_id="canonical-feast-offline-recovered",
        )
        self.registry_generation = 1793255806863364
        self.registry_sha256 = "d" * 64
        self.execution_git_sha = "e" * 40

    def _trusted_registry(self, path: Path) -> TrustedRegistryIdentity:
        path.write_text(
            json.dumps(
                {
                    "schema_version": "feast-registry-operation-evidence-v1",
                    "status": "READBACK_VERIFIED",
                    "mode": "readback-only",
                    "registry_uri": self.boundary.feast_registry_uri,
                    "snapshot_id": self.boundary.resolved_feast_snapshot_id,
                    "snapshot_root": self.boundary.feast_snapshot_root,
                    "execution_git_sha": self.execution_git_sha,
                    "registry_generation": self.registry_generation,
                    "registry_sha256": self.registry_sha256,
                    "historical_feature_count": 31,
                    "model_input_count": 41,
                    "feature_views": [{"name": name} for name in FEATURE_SPECS],
                }
            ),
            encoding="utf-8",
        )
        return TrustedRegistryIdentity(
            readback_evidence=path,
            uri=self.boundary.feast_registry_uri,
            generation=self.registry_generation,
            sha256=self.registry_sha256,
            execution_git_sha=self.execution_git_sha,
        )

    def _pit_evidence(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema_version": "feast-pit-validation-evidence-v1",
                    "status": "PASS",
                    "environment": "gcp",
                    "sample_rows": 10,
                    "distinct_sample_cases": 10,
                    "historical_features_per_case": 31,
                    "expected_comparable_values": 310,
                    "comparable_values": 310,
                    "mismatch_count": 0,
                    "labels_passed_to_feast": False,
                    "validation_export_lineage": {
                        "dataset_version": self.boundary.dataset_version,
                        "source_silver_run_id": self.boundary.silver_run_id,
                        "source_feature_run_id": self.boundary.feature_run_id,
                        "snapshot_id": self.boundary.resolved_feast_snapshot_id,
                        "source_git_sha": self.boundary.source_git_sha,
                        "execution_git_sha": self.execution_git_sha,
                        "feature_selection_digest": self.boundary.feature_selection_digest,
                    },
                    "registry_readback": {
                        "status": "READBACK_VERIFIED",
                        "registry_uri": self.boundary.feast_registry_uri,
                        "registry_generation": self.registry_generation,
                        "registry_sha256": self.registry_sha256,
                    },
                }
            ),
            encoding="utf-8",
        )

    def _snapshot(self, root: Path) -> None:
        (root / "split=train").mkdir(parents=True)
        (root / "split=train" / "part-00000.parquet").write_bytes(b"parquet")
        manifest = {
            "schema_version": "f7-training-dataset-v1",
            "lineage": {
                "dataset_version": self.boundary.dataset_version,
                "source_silver_run_id": self.boundary.silver_run_id,
                "source_feature_run_id": self.boundary.feature_run_id,
                "snapshot_id": self.boundary.snapshot_id,
                "source_git_sha": self.boundary.source_git_sha,
                "feature_selection_digest": self.boundary.feature_selection_digest,
            },
            "complete_source_scope": True,
            "materialized_rows": 3,
            "split_summary": {
                split: {"rows": 1, "fraud": 0, "normal": 1}
                for split in ("train", "validation", "test")
            },
            "schema": {
                "output_columns": 50,
                "ordered_output_columns": list(OUTPUT_COLUMNS),
                "historical_features": 31,
                "model_features": 41,
                "ordered_model_feature_columns": list(MODEL_FEATURE_COLUMNS),
            },
            "validation": {"status": "PASS", "rows": 3, "checks": {"all": True}},
        }
        payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        (root / "manifest.json").write_bytes(payload)
        (root / "manifest.sha256").write_text(
            hashlib.sha256(payload).hexdigest() + "\n", encoding="utf-8"
        )

    def _inputs(self, root: Path) -> tuple[Path, Path, TrustedRegistryIdentity]:
        source = root / "dataset"
        source.mkdir()
        self._snapshot(source)
        pit = root / "pit.json"
        self._pit_evidence(pit)
        identity = self._trusted_registry(root / "registry-readback.json")
        return source, pit, identity

    def _publish(self, store: FakeStore, root: Path) -> dict[str, object]:
        source, pit, identity = self._inputs(root)
        return publish_training_snapshot(
            store,
            boundary=self.boundary,
            source=source,
            pit_evidence=pit,
            registry_identity=identity,
            execution_git_sha="f" * 40,
            authorization=PUBLICATION_AUTHORIZATION,
        )

    def test_create_only_publication_revalidates_then_commits_marker_last(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FakeStore()
            result = self._publish(store, Path(temporary))
        self.assertEqual("PUBLISHED_AND_VERIFIED", result["status"])
        self.assertEqual(3, result["object_count"])
        self.assertTrue(store.created[-1].endswith(COMPLETION_MARKER))

    def test_registry_uri_generation_and_checksum_must_match_pit(self) -> None:
        mutations = {
            "URI": ("registry_uri", "gs://wrong/registry.db"),
            "generation": ("registry_generation", self.registry_generation + 1),
            "SHA-256": ("registry_sha256", "0" * 64),
        }
        for label, (field, value) in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, pit, identity = self._inputs(root)
                evidence = json.loads(pit.read_text())
                evidence["registry_readback"][field] = value
                pit.write_text(json.dumps(evidence), encoding="utf-8")
                store = FakeStore()
                with self.assertRaisesRegex(ValueError, "Registry identity"):
                    publish_training_snapshot(
                        store,
                        boundary=self.boundary,
                        source=source,
                        pit_evidence=pit,
                        registry_identity=identity,
                        execution_git_sha="f" * 40,
                        authorization=PUBLICATION_AUTHORIZATION,
                    )
                self.assertEqual([], store.created)

    def test_missing_or_mismatched_trusted_readback_fails_before_writes(self) -> None:
        for mutation in ("missing", "generation", "checksum", "uri"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, pit, identity = self._inputs(root)
                if mutation == "missing":
                    identity.readback_evidence.unlink()
                else:
                    trusted = json.loads(identity.readback_evidence.read_text())
                    field = {
                        "generation": "registry_generation",
                        "checksum": "registry_sha256",
                        "uri": "registry_uri",
                    }[mutation]
                    trusted[field] = 99 if mutation == "generation" else "0" * 64
                    identity.readback_evidence.write_text(json.dumps(trusted))
                store = FakeStore()
                with self.assertRaises(ValueError):
                    publish_training_snapshot(
                        store,
                        boundary=self.boundary,
                        source=source,
                        pit_evidence=pit,
                        registry_identity=identity,
                        execution_git_sha="f" * 40,
                        authorization=PUBLICATION_AUTHORIZATION,
                    )
                self.assertEqual([], store.created)

    def test_explicit_registry_identity_mismatch_fails_before_writes(self) -> None:
        mutations = (
            {"uri": "gs://wrong/registry.db"},
            {"generation": self.registry_generation + 1},
            {"sha256": "0" * 64},
        )
        for changes in mutations:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, pit, identity = self._inputs(root)
                store = FakeStore()
                with self.assertRaises(ValueError):
                    publish_training_snapshot(
                        store,
                        boundary=self.boundary,
                        source=source,
                        pit_evidence=pit,
                        registry_identity=replace(identity, **changes),
                        execution_git_sha="f" * 40,
                        authorization=PUBLICATION_AUTHORIZATION,
                    )
                self.assertEqual([], store.created)

    def test_concurrent_content_generation_mutation_prevents_completion_marker(self) -> None:
        class MutatingStore(FakeStore):
            reads = 0

            def read(self, uri: str, *, generation: int) -> RemoteObject:
                self.reads += 1
                name = urlsplit(uri).path.lstrip("/")
                if self.reads == 4:
                    self.objects[name] = b"concurrent replacement"
                    self.generations[name] += 1
                return super().read(uri, generation=generation)

        with tempfile.TemporaryDirectory() as temporary:
            store = MutatingStore()
            with self.assertRaisesRegex(ValueError, "generation or SHA-256"):
                self._publish(store, Path(temporary))
        self.assertFalse(any(name.endswith(COMPLETION_MARKER) for name in store.created))

    def test_incomplete_or_wrong_source_lineage_fails_before_writes(self) -> None:
        for mutation in ("incomplete", "lineage"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, pit, identity = self._inputs(root)
                if mutation == "incomplete":
                    (source / "_INCOMPLETE").write_text("{}\n")
                else:
                    manifest_path = source / "manifest.json"
                    manifest = json.loads(manifest_path.read_text())
                    manifest["lineage"]["source_silver_run_id"] = "wrong-silver-run"
                    payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
                    manifest_path.write_bytes(payload)
                    (source / "manifest.sha256").write_text(
                        hashlib.sha256(payload).hexdigest() + "\n"
                    )
                store = FakeStore()
                with self.assertRaises(ValueError):
                    publish_training_snapshot(
                        store,
                        boundary=self.boundary,
                        source=source,
                        pit_evidence=pit,
                        registry_identity=identity,
                        execution_git_sha="f" * 40,
                        authorization=PUBLICATION_AUTHORIZATION,
                    )
                self.assertEqual([], store.created)

    def test_existing_destination_and_listing_failure_never_write(self) -> None:
        cases = (
            FakeStore(existing=("ml/training-snapshots/existing/object",)),
            FakeStore(list_error=RuntimeError("listing unavailable")),
        )
        for store in cases:
            with self.subTest(store=store), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises((ValueError, RuntimeError)):
                    self._publish(store, Path(temporary))
                self.assertEqual([], store.created)

    def test_missing_or_extra_precommit_object_blocks_completion_marker(self) -> None:
        class InventoryMutationStore(FakeStore):
            def __init__(self, mutation: str) -> None:
                super().__init__()
                self.mutation = mutation
                self.list_calls = 0

            def list_objects(self, root_uri: str) -> tuple[str, ...]:
                self.list_calls += 1
                names = list(super().list_objects(root_uri))
                if self.list_calls == 2:
                    if self.mutation == "missing":
                        names = names[1:]
                    else:
                        names.append("ml/training-snapshots/rogue-object")
                return tuple(sorted(names))

        for mutation in ("missing", "extra"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                store = InventoryMutationStore(mutation)
                with self.assertRaisesRegex(ValueError, "inventory mismatch"):
                    self._publish(store, Path(temporary))
                self.assertFalse(
                    any(name.endswith(COMPLETION_MARKER) for name in store.created)
                )

    def test_gcs_adapter_streams_with_no_retry_and_generation_pinned_readback(self) -> None:
        client = Mock()
        bucket = client.bucket.return_value
        exact = Mock()
        exact.reload.side_effect = NotFound("absent")
        blob = Mock()
        blob.generation = 41
        bucket.blob.side_effect = [exact, blob, blob]
        client.list_blobs.return_value = []
        store = GCSTrainingSnapshotStore(
            project_id="example-project",
            data_bucket=self.boundary.bucket,
            client=client,
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "object"
            source.write_bytes(b"payload")
            self.assertEqual((), store.list_objects(self.boundary.training_snapshot_root))
            uri = self.boundary.training_snapshot_root + "/manifest.json"
            generation = store.create(uri, source)
            blob.download_to_file.side_effect = lambda stream, **_: stream.write(b"payload")
            remote = store.read(uri, generation=generation)
        blob.upload_from_filename.assert_called_once_with(
            str(source),
            content_type="application/octet-stream",
            if_generation_match=0,
            retry=None,
        )
        blob.reload.assert_called_with(if_generation_match=41)
        blob.download_to_file.assert_called_once()
        self.assertEqual(hashlib.sha256(b"payload").hexdigest(), remote.sha256)

    def test_gcs_concurrent_create_conflict_is_not_retried(self) -> None:
        client = Mock()
        blob = client.bucket.return_value.blob.return_value
        blob.upload_from_filename.side_effect = PreconditionFailed("race")
        store = GCSTrainingSnapshotStore(
            project_id="example-project",
            data_bucket=self.boundary.bucket,
            client=client,
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "object"
            source.write_bytes(b"x")
            with self.assertRaisesRegex(ValueError, "create-only conflict"):
                store.create(
                    self.boundary.training_snapshot_root + "/manifest.json", source
                )
        self.assertEqual(1, blob.upload_from_filename.call_count)


if __name__ == "__main__":
    unittest.main()
