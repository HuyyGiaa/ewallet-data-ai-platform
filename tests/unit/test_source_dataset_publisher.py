from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from data_platform.config import RuntimeConfig, ServiceConfig, SparkConfig, StorageConfig
from data_platform.generation.src.sharded.manifest import (
    DatasetPublisher,
    IncompleteDatasetError,
    ObjectConflictError,
)
from data_platform.generation.src.sharded.models import (
    ShardFileMetadata,
    ShardManifest,
)
from data_platform.generation.src.sharded.planner import build_shard_plan
from data_platform.generation.src.sharded.storage import (
    GcloudObjectStore,
    ObjectMetadata,
    ObjectStoreError,
)
from ops.gcp.generate_source_dataset import main, resolve_generator_config_path


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


def md5_base64(payload: bytes) -> str:
    return base64.b64encode(hashlib.md5(payload).digest()).decode("ascii")


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.write_calls: list[str] = []

    def exists(self, uri: str) -> bool:
        return uri in self.objects

    def read_text(self, uri: str) -> str:
        return self.objects[uri].decode("utf-8")

    def write_text(self, text: str, uri: str, *, if_absent: bool) -> None:
        self._write(text.encode("utf-8"), uri, if_absent=if_absent)

    def write_file(self, path: Path, uri: str, *, if_absent: bool) -> None:
        self._write(path.read_bytes(), uri, if_absent=if_absent)

    def _write(self, payload: bytes, uri: str, *, if_absent: bool) -> None:
        if if_absent and uri in self.objects:
            raise FileExistsError(uri)
        self.objects[uri] = payload
        self.write_calls.append(uri)

    def stat(self, uri: str) -> ObjectMetadata:
        payload = self.objects[uri]
        return ObjectMetadata(
            uri=uri,
            size=len(payload),
            md5_base64=md5_base64(payload),
            generation="1",
        )


def runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        environment="dev",
        project_id="ewallet-data-ai-platform-hg26",
        region="asia-southeast1",
        storage=StorageConfig(
            scheme="gs",
            source_root="gs://canonical-bucket/source/dev",
            bronze_root="gs://canonical-bucket/lakehouse/dev/bronze",
            silver_root="gs://canonical-bucket/lakehouse/dev/silver",
            gold_root="gs://canonical-bucket/lakehouse/dev/gold",
            feast_root="gs://canonical-bucket/feast/dev",
        ),
        spark=SparkConfig(
            master=None,
            shuffle_partitions=20,
            event_log_uri="gs://temp-bucket/spark-events",
            packages=(),
            configurations={},
        ),
        services=ServiceConfig(
            datahub_gms_url="http://datahub-gms:8080",
            trino_host=None,
            trino_port=None,
        ),
    )


def base_config() -> dict:
    return {"fraud": {"enabled": True}, "random_seed": 42}


def make_plan():
    return build_shard_plan(
        base_config(),
        "r4-pilot-v1",
        7001,
        2,
        30,
        8,
        600,
    )


def make_local_shard(root: Path, plan, shard_id: int) -> tuple[Path, ShardManifest]:
    directory = root / f"shard-{shard_id:05d}"
    directory.mkdir(parents=True)
    files = []
    for index, table in enumerate(PHYSICAL_TABLES, start=1):
        payload = f"{plan.digest}:{shard_id}:{table}".encode("utf-8")
        path = directory / f"{table}.parquet"
        path.write_bytes(payload)
        files.append(
            ShardFileMetadata(
                table=table,
                relative_path=f"shard-{shard_id:05d}/{table}.parquet",
                rows=(12 if table == "fraud_labels" else index + shard_id),
                bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                md5_base64=md5_base64(payload),
                schema_sha256=hashlib.sha256(table.encode("utf-8")).hexdigest(),
            )
        )
    return directory, ShardManifest(
        schema_version=1,
        dataset_version=plan.dataset_version,
        plan_digest=plan.digest,
        shard_id=shard_id,
        derived_seed=1000 + shard_id,
        logical_transactions=600,
        physical_transactions=612,
        fraud_scenario_counts={
            "velocity": 3,
            "amount_anomaly": 3,
            "account_takeover": 3,
            "merchant_burst": 3,
        },
        files=tuple(files),
    )


class DatasetPublisherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeObjectStore()
        self.plan = make_plan()
        self.publisher = DatasetPublisher(
            self.store,
            "gs://canonical-bucket/source/dev/r4-pilot-v1",
            self.plan,
        )
        self.publisher.ensure_plan()

    def test_partial_shard_resumes_and_local_files_are_deleted_after_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            shard_directory, manifest = make_local_shard(root, self.plan, 0)
            first_file = manifest.files[0]
            first_uri = self.publisher.file_uri(0, first_file.table)
            self.store.objects[first_uri] = (
                shard_directory / f"{first_file.table}.parquet"
            ).read_bytes()

            published = self.publisher.publish_shard(
                shard_directory,
                manifest,
                cleanup_local=True,
            )

            self.assertFalse(shard_directory.exists())
            self.assertEqual(0, published.shard_id)
            self.assertNotIn(first_uri, self.store.write_calls)
            self.assertIsNotNone(self.publisher.completed_shard(0))
            for table in PHYSICAL_TABLES:
                self.assertTrue(self.store.exists(self.publisher.file_uri(0, table)))

    def test_conflicting_existing_object_fails_closed_and_keeps_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            shard_directory, manifest = make_local_shard(
                Path(temp_directory), self.plan, 0
            )
            conflict_uri = self.publisher.file_uri(0, manifest.files[0].table)
            self.store.objects[conflict_uri] = b"conflicting bytes"

            with self.assertRaises(ObjectConflictError):
                self.publisher.publish_shard(
                    shard_directory,
                    manifest,
                    cleanup_local=True,
                )

            self.assertTrue(shard_directory.exists())
            self.assertIsNone(self.publisher.completed_shard(0))

    def test_completed_shard_is_verified_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            shard_directory, manifest = make_local_shard(
                Path(temp_directory), self.plan, 0
            )
            first = self.publisher.publish_shard(
                shard_directory, manifest, cleanup_local=False
            )
            writes_before = list(self.store.write_calls)

            completed = self.publisher.completed_shard(0)

            self.assertEqual(first.to_dict(), completed.to_dict())
            self.assertEqual(writes_before, self.store.write_calls)

    def test_completed_shard_rejects_same_size_remote_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            shard_directory, manifest = make_local_shard(
                Path(temp_directory), self.plan, 0
            )
            self.publisher.publish_shard(
                shard_directory, manifest, cleanup_local=False
            )
            file_uri = self.publisher.file_uri(0, manifest.files[0].table)
            original = self.store.objects[file_uri]
            self.store.objects[file_uri] = b"x" * len(original)

            with self.assertRaises(ObjectConflictError):
                self.publisher.completed_shard(0)

    def test_final_manifest_requires_every_shard_and_aggregates_exactly(self) -> None:
        manifest_uri = self.publisher.dataset_manifest_uri
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            second_dir, second = make_local_shard(root, self.plan, 1)
            self.publisher.publish_shard(second_dir, second, cleanup_local=False)
            with self.assertRaises(IncompleteDatasetError):
                self.publisher.finalize()
            self.assertFalse(self.store.exists(manifest_uri))

            first_dir, first = make_local_shard(root, self.plan, 0)
            self.publisher.publish_shard(first_dir, first, cleanup_local=False)
            dataset_manifest = self.publisher.finalize()

        self.assertTrue(self.store.exists(manifest_uri))
        self.assertEqual(1200, dataset_manifest["logical_transactions"])
        self.assertEqual(1224, dataset_manifest["physical_transactions"])
        self.assertEqual(24, dataset_manifest["fraud_rows"])
        self.assertEqual(
            {
                "velocity": 6,
                "amount_anomaly": 6,
                "account_takeover": 6,
                "merchant_burst": 6,
            },
            dataset_manifest["fraud_scenario_counts"],
        )
        self.assertEqual(set(PHYSICAL_TABLES), set(dataset_manifest["tables"]))
        expected_bytes = sum(
            item.bytes for item in first.files + second.files
        )
        self.assertEqual(expected_bytes, dataset_manifest["total_bytes"])
        for table in PHYSICAL_TABLES:
            self.assertEqual(
                [0, 1],
                [
                    item["shard_id"]
                    for item in dataset_manifest["tables"][table]["files"]
                ],
            )
        serialized = json.dumps(dataset_manifest)
        self.assertNotIn(str(Path.home()), serialized)
        self.assertNotIn("credentials", serialized.lower())

    def test_final_manifest_rejects_more_scenarios_than_label_rows(self) -> None:
        one_shard_plan = build_shard_plan(
            base_config(), "r4-one-shard-v1", 7001, 1, 30, 8, 600
        )
        publisher = DatasetPublisher(
            self.store,
            "gs://canonical-bucket/source/dev/r4-one-shard-v1",
            one_shard_plan,
        )
        publisher.ensure_plan()
        with tempfile.TemporaryDirectory() as temp_directory:
            shard_directory, manifest = make_local_shard(
                Path(temp_directory), one_shard_plan, 0
            )
            inconsistent = ShardManifest(
                **{
                    **manifest.__dict__,
                    "fraud_scenario_counts": {
                        "velocity": 4,
                        "amount_anomaly": 3,
                        "account_takeover": 3,
                        "merchant_burst": 3,
                    },
                }
            )
            publisher.publish_shard(
                shard_directory, inconsistent, cleanup_local=False
            )
            with self.assertRaisesRegex(
                ObjectConflictError, "exceed fraud_labels"
            ):
                publisher.finalize()


class GcloudObjectStoreTest(unittest.TestCase):
    def test_immutable_upload_uses_md5_and_generation_precondition(self) -> None:
        runner = Mock()
        runner.return_value = Mock(returncode=0, stdout="", stderr="")
        store = GcloudObjectStore(runner=runner)

        with tempfile.TemporaryDirectory() as temp_directory:
            path = Path(temp_directory) / "part.parquet"
            path.write_bytes(b"deterministic payload")
            store.write_file(
                path,
                "gs://canonical-bucket/source/part.parquet",
                if_absent=True,
            )

        command = runner.call_args.args[0]
        self.assertIn("--if-generation-match=0", command)
        self.assertIn(
            f"--content-md5={md5_base64(b'deterministic payload')}",
            command,
        )

    def test_exists_raises_on_auth_failure_instead_of_treating_as_missing(self) -> None:
        runner = Mock()
        runner.return_value = Mock(
            returncode=1,
            stdout="",
            stderr="ERROR: permission denied for current principal",
        )
        store = GcloudObjectStore(runner=runner)

        with self.assertRaisesRegex(ObjectStoreError, "permission denied"):
            store.exists("gs://canonical-bucket/source/part.parquet")


class DatasetCliTest(unittest.TestCase):
    def test_repo_relative_generator_config_is_resolved_before_legacy_loader(self) -> None:
        expected = (
            Path(__file__).resolve().parents[2]
            / "data_platform/generation/config/settings_fraud_dev.yaml"
        ).resolve()
        self.assertEqual(
            expected,
            resolve_generator_config_path(
                "data_platform/generation/config/settings_fraud_dev.yaml"
            ),
        )

    def test_default_cli_is_dry_run_and_never_creates_store_or_shards(self) -> None:
        stdout = io.StringIO()
        argv = [
            "--config",
            "unused.yaml",
            "--environment",
            "dev",
            "--dataset-version",
            "r4-pilot-v1",
            "--global-seed",
            "7001",
            "--shard-count",
            "2",
            "--users-per-shard",
            "30",
            "--merchants-per-shard",
            "8",
            "--transactions-per-shard",
            "600",
        ]
        with (
            patch(
                "ops.gcp.generate_source_dataset.load_config",
                return_value=base_config(),
            ),
            patch(
                "ops.gcp.generate_source_dataset.load_runtime_config",
                return_value=runtime_config(),
            ),
            patch("ops.gcp.generate_source_dataset.GcloudObjectStore") as store,
            patch("ops.gcp.generate_source_dataset.generate_shard") as generate,
        ):
            result = main(argv, stdout=stdout)

        self.assertEqual(0, result)
        self.assertIn("DRY RUN", stdout.getvalue())
        self.assertIn(
            "gs://canonical-bucket/source/dev/r4-pilot-v1",
            stdout.getvalue(),
        )
        store.assert_not_called()
        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
