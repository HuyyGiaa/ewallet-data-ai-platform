from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch

import pyarrow as pa
import pyarrow.parquet as pq

from ops.gcp.remediate_canonical_size import (
    AmbiguousRollbackState,
    CanonicalSizeRemediator,
    ChecksumMismatchError,
    GenerationConflict,
    GoogleCloudStorageStore,
    InvariantViolation,
    ObjectVersion,
    RemediationError,
    RemediationSpec,
    assert_tables_identical,
    load_verified_plan,
    main,
    parse_args,
)


def _md5_bytes(payload: bytes) -> str:
    return base64.b64encode(hashlib.md5(payload).digest()).decode("ascii")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass
class _StoredGeneration:
    generation: str
    payload: bytes


class FakeVersionedStore:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._next_generation = 1000
        self.objects: dict[str, list[_StoredGeneration]] = {}
        for name, payload in objects.items():
            self.objects[name] = [_StoredGeneration(self._new_generation(), payload)]
        self.reads: list[str] = []
        self.mutations: list[str] = []
        self.fail_on_mutation_number: int | None = None
        self.fail_after_mutation_number: int | None = None
        self.has_versioning = True

    def versioning_enabled(self) -> bool:
        return self.has_versioning

    def _new_generation(self) -> str:
        self._next_generation += 1
        return str(self._next_generation)

    def metadata(self, name: str, *, generation: str | None = None) -> ObjectVersion:
        item = self._resolve(name, generation)
        return ObjectVersion(
            name=name,
            size=len(item.payload),
            md5_base64=_md5_bytes(item.payload),
            generation=item.generation,
        )

    def download_bytes(self, name: str, *, generation: str | None = None) -> bytes:
        self.reads.append(name)
        return self._resolve(name, generation).payload

    def download_to_file(
        self,
        name: str,
        destination: Path,
        *,
        generation: str | None = None,
    ) -> None:
        self.reads.append(name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self._resolve(name, generation).payload)

    def upload_bytes(
        self,
        name: str,
        payload: bytes,
        *,
        if_generation_match: str,
    ) -> ObjectVersion:
        if (
            self.fail_on_mutation_number is not None
            and len(self.mutations) + 1 == self.fail_on_mutation_number
        ):
            raise RuntimeError("injected upload failure")
        current = self.metadata(name)
        if current.generation != str(if_generation_match):
            raise RuntimeError("generation conflict")
        item = _StoredGeneration(self._new_generation(), payload)
        self.objects[name].append(item)
        self.mutations.append(name)
        if (
            self.fail_after_mutation_number is not None
            and len(self.mutations) == self.fail_after_mutation_number
        ):
            raise RuntimeError("injected post-upload failure")
        return self.metadata(name)

    def upload_file(self, name: str, source: Path, *, if_generation_match: str) -> ObjectVersion:
        return self.upload_bytes(
            name,
            source.read_bytes(),
            if_generation_match=if_generation_match,
        )

    def restore_generation(
        self,
        name: str,
        *,
        source_generation: str,
        if_generation_match: str,
    ) -> ObjectVersion:
        payload = self._resolve(name, source_generation).payload
        return self.upload_bytes(name, payload, if_generation_match=if_generation_match)

    def _resolve(self, name: str, generation: str | None) -> _StoredGeneration:
        versions = self.objects[name]
        if generation is None:
            return versions[-1]
        return next(item for item in versions if item.generation == str(generation))


def _parquet_bytes(rows: int = 64) -> bytes:
    table = pa.table(
        {
            "transaction_id": [f"txn-{index:05d}" for index in range(rows)],
            "account_id": [f"acct-{index % 7}" for index in range(rows)],
            "amount": [float(index) + 0.25 for index in range(rows)],
            "timestamp": [index for index in range(rows)],
        }
    )
    with tempfile.NamedTemporaryFile(suffix=".parquet") as handle:
        pq.write_table(
            table,
            handle.name,
            version="2.6",
            compression="snappy",
            use_dictionary=True,
            write_statistics=True,
        )
        return Path(handle.name).read_bytes()


def _fixture(*, minimum_final_bytes: int = 0) -> tuple[FakeVersionedStore, RemediationSpec]:
    root = "source/canonical-test"
    parquet_name = f"{root}/transactions_v2/shard-00000.parquet"
    parquet_payload = _parquet_bytes()
    selected_entry = {
        "table": "transactions_v2",
        "relative_path": "transactions_v2/shard-00000.parquet",
        "rows": 64,
        "bytes": len(parquet_payload),
        "sha256": _sha256_bytes(parquet_payload),
        "md5_base64": _md5_bytes(parquet_payload),
        "schema_sha256": "schema-v2",
    }
    other_tables = (
        "users",
        "accounts",
        "merchants",
        "devices",
        "transactions_v1",
        "balance_snapshots",
        "login_events",
        "fraud_labels",
    )
    other_entries = [
        {
            "table": table,
            "relative_path": f"{table}/shard-00000.parquet",
            "rows": 60 if table == "fraud_labels" else 0 if table == "transactions_v1" else 1,
            "bytes": 10 + index,
            "sha256": f"sha-{table}",
            "md5_base64": f"md5-{table}",
            "schema_sha256": f"schema-{table}",
        }
        for index, table in enumerate(other_tables)
    ]
    file_entries = other_entries[:4] + [other_entries[4], selected_entry] + other_entries[5:]
    shard_manifest = {
        "schema_version": 1,
        "dataset_version": "canonical-test",
        "plan_digest": "plan-digest",
        "shard_id": 0,
        "derived_seed": 123,
        "logical_transactions": 60,
        "physical_transactions": 64,
        "fraud_scenario_counts": {"velocity": 2},
        "files": file_entries,
    }
    tables = {}
    for entry in file_entries:
        tables[entry["table"]] = {
            "rows": entry["rows"],
            "bytes": entry["bytes"],
            "schema_sha256": [entry["schema_sha256"]],
            "files": [
                {
                    "shard_id": 0,
                    "path": entry["relative_path"],
                    "rows": entry["rows"],
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                    "md5_base64": entry["md5_base64"],
                }
            ],
        }
    total_bytes = sum(entry["bytes"] for entry in file_entries)
    dataset_manifest = {
        "schema_version": 1,
        "status": "COMPLETE",
        "dataset_version": "canonical-test",
        "plan_digest": "plan-digest",
        "shard_count": 1,
        "logical_transactions": 60,
        "physical_transactions": 64,
        "fraud_label_rows": 60,
        "fraud_rows": 2,
        "fraud_scenario_counts": {"velocity": 2},
        "total_bytes": total_bytes,
        "tables": tables,
    }
    objects = {
        parquet_name: parquet_payload,
        f"{root}/_control/shards/shard-00000.json": (
            json.dumps(shard_manifest, indent=2, sort_keys=True) + "\n"
        ).encode(),
        f"{root}/_control/dataset_manifest.json": (
            json.dumps(dataset_manifest, indent=2, sort_keys=True) + "\n"
        ).encode(),
    }
    spec = RemediationSpec(
        dataset_version="canonical-test",
        selected_shards=(0,),
        expected_shard_count=1,
        expected_current_total_bytes=total_bytes,
        minimum_final_bytes=minimum_final_bytes,
        expected_logical_transactions=60,
        expected_physical_transactions=64,
        expected_fraud_label_rows=60,
        expected_fraud_rows=2,
    )
    return FakeVersionedStore(objects), spec


class PlanTest(unittest.TestCase):
    def test_plan_requires_object_versioning_before_any_read(self) -> None:
        store, spec = _fixture()
        store.has_versioning = False
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvariantViolation, "versioning"):
                CanonicalSizeRemediator(store, "bucket", spec).plan(
                    staging_directory=Path(directory),
                    plan_path=Path(directory) / "plan.json",
                )
        self.assertEqual([], store.reads)
        self.assertEqual([], store.mutations)

    def test_plan_is_read_only_and_writes_digest_protected_immutable_json(self) -> None:
        store, spec = _fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "remediation-plan.json"
            plan = CanonicalSizeRemediator(store, "bucket", spec).plan(
                staging_directory=root,
                plan_path=plan_path,
            )

            self.assertEqual([], store.mutations)
            self.assertEqual("PLANNED", plan["status"])
            self.assertEqual(1, len(plan["objects"]))
            self.assertEqual(
                {
                    "source/canonical-test/_control/dataset_manifest.json",
                    "source/canonical-test/_control/shards/shard-00000.json",
                    "source/canonical-test/transactions_v2/shard-00000.parquet",
                },
                set(store.reads),
            )
            self.assertEqual(0o444, os.stat(plan_path).st_mode & 0o777)
            self.assertEqual(plan, load_verified_plan(plan_path))
            with self.assertRaises(FileExistsError):
                CanonicalSizeRemediator(store, "bucket", spec).plan(
                    staging_directory=root,
                    plan_path=plan_path,
                )

    def test_plan_rejects_manifest_checksum_mismatch(self) -> None:
        store, spec = _fixture()
        parquet_name = "source/canonical-test/transactions_v2/shard-00000.parquet"
        store.objects[parquet_name][-1].payload += b"corrupt"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ChecksumMismatchError):
                CanonicalSizeRemediator(store, "bucket", spec).plan(
                    staging_directory=Path(directory),
                    plan_path=Path(directory) / "plan.json",
                )
        self.assertEqual([], store.mutations)

    def test_plan_rejects_insufficient_projected_total(self) -> None:
        store, spec = _fixture(minimum_final_bytes=10**12)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvariantViolation, "projected total"):
                CanonicalSizeRemediator(store, "bucket", spec).plan(
                    staging_directory=Path(directory),
                    plan_path=Path(directory) / "plan.json",
                )
        self.assertEqual([], store.mutations)

    def test_table_comparison_rejects_schema_or_value_changes(self) -> None:
        original = pa.table({"value": pa.array([1, 2], type=pa.int64())})
        changed_value = pa.table({"value": pa.array([1, 3], type=pa.int64())})
        changed_schema = pa.table({"value": pa.array([1, 2], type=pa.int32())})

        with self.assertRaisesRegex(InvariantViolation, "values or row order"):
            assert_tables_identical(original, changed_value)
        with self.assertRaisesRegex(InvariantViolation, "schema"):
            assert_tables_identical(original, changed_schema)


class ApplyTest(unittest.TestCase):
    def _create_plan(self, root: Path) -> tuple[FakeVersionedStore, RemediationSpec, Path]:
        store, spec = _fixture()
        plan_path = root / "remediation-plan.json"
        CanonicalSizeRemediator(store, "bucket", spec).plan(
            staging_directory=root,
            plan_path=plan_path,
        )
        store.reads.clear()
        return store, spec, plan_path

    def test_apply_requires_unchanged_original_generations_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, spec, plan_path = self._create_plan(root)
            object_name = "source/canonical-test/transactions_v2/shard-00000.parquet"
            current = store.metadata(object_name)
            store.upload_bytes(
                object_name,
                store.download_bytes(object_name),
                if_generation_match=current.generation,
            )
            store.mutations.clear()

            with self.assertRaises(GenerationConflict):
                CanonicalSizeRemediator(store, "bucket", spec).apply(
                    plan_path=plan_path,
                    ledger_path=root / "apply-ledger.json",
                    execute=True,
                )

            self.assertEqual([], store.mutations)

    def test_apply_mutates_only_selected_file_manifest_and_dataset_manifest_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, spec, plan_path = self._create_plan(root)
            ledger_path = root / "apply-ledger.json"

            ledger = CanonicalSizeRemediator(store, "bucket", spec).apply(
                plan_path=plan_path,
                ledger_path=ledger_path,
                execute=True,
            )

            expected = [
                "source/canonical-test/transactions_v2/shard-00000.parquet",
                "source/canonical-test/_control/shards/shard-00000.json",
                "source/canonical-test/_control/dataset_manifest.json",
            ]
            self.assertEqual(expected, store.mutations)
            self.assertEqual("APPLIED", ledger["status"])
            self.assertEqual(3, len(ledger["mutations"]))
            rebuilt = json.loads(
                store.download_bytes(expected[-1]).decode("utf-8")
            )
            original = load_verified_plan(plan_path)
            self.assertEqual(original["projected_total_bytes"], rebuilt["total_bytes"])
            self.assertEqual(60, rebuilt["logical_transactions"])
            self.assertEqual(64, rebuilt["physical_transactions"])
            self.assertEqual(60, rebuilt["fraud_label_rows"])
            self.assertEqual(2, rebuilt["fraud_rows"])

    def test_partial_apply_persists_failed_ledger_for_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, spec, plan_path = self._create_plan(root)
            ledger_path = root / "apply-ledger.json"
            store.fail_on_mutation_number = 2

            with self.assertRaisesRegex(RuntimeError, "injected upload failure"):
                CanonicalSizeRemediator(store, "bucket", spec).apply(
                    plan_path=plan_path,
                    ledger_path=ledger_path,
                    execute=True,
                )

            ledger = json.loads(ledger_path.read_text())
            self.assertEqual("FAILED", ledger["status"])
            self.assertEqual(2, len(ledger["mutations"]))
            self.assertEqual(
                "source/canonical-test/transactions_v2/shard-00000.parquet",
                ledger["mutations"][0]["name"],
            )
            self.assertEqual("APPLIED", ledger["mutations"][0]["state"])
            self.assertEqual("PENDING", ledger["mutations"][1]["state"])

    def test_manifest_validation_rejects_changed_dataset_counts(self) -> None:
        store, spec = _fixture()
        remediator = CanonicalSizeRemediator(store, "bucket", spec)
        original = json.loads(
            store.download_bytes(
                "source/canonical-test/_control/dataset_manifest.json"
            )
        )
        rebuilt = json.loads(json.dumps(original))
        rebuilt["logical_transactions"] += 1
        plan = {"projected_total_bytes": original["total_bytes"]}

        with self.assertRaisesRegex(InvariantViolation, "logical_transactions"):
            remediator._validate_rebuilt_manifest(original, rebuilt, plan)

    def test_manifest_validation_rejects_unaffected_file_metadata_change(self) -> None:
        store, spec = _fixture()
        remediator = CanonicalSizeRemediator(store, "bucket", spec)
        original = json.loads(
            store.download_bytes(
                "source/canonical-test/_control/dataset_manifest.json"
            )
        )
        rebuilt = json.loads(json.dumps(original))
        rebuilt["tables"]["users"]["files"][0]["sha256"] = "changed"
        plan = {"projected_total_bytes": original["total_bytes"]}

        with self.assertRaisesRegex(InvariantViolation, "unaffected file"):
            remediator._validate_rebuilt_manifest(original, rebuilt, plan)

    def test_plan_identity_rejects_object_outside_approved_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, spec, plan_path = self._create_plan(root)
            plan = load_verified_plan(plan_path)
            plan["objects"][0]["name"] = "unapproved/object.parquet"

            with self.assertRaisesRegex(InvariantViolation, "object path"):
                CanonicalSizeRemediator(store, "bucket", spec)._validate_plan_identity(
                    plan
                )


class RollbackTest(unittest.TestCase):
    def _applied(
        self, root: Path
    ) -> tuple[FakeVersionedStore, RemediationSpec, Path, Path]:
        store, spec = _fixture()
        plan_path = root / "remediation-plan.json"
        ledger_path = root / "apply-ledger.json"
        remediator = CanonicalSizeRemediator(store, "bucket", spec)
        remediator.plan(staging_directory=root, plan_path=plan_path)
        remediator.apply(
            plan_path=plan_path,
            ledger_path=ledger_path,
            execute=True,
        )
        store.mutations.clear()
        return store, spec, plan_path, ledger_path

    def test_rollback_restores_file_manifest_and_dataset_manifest_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, spec, plan_path, ledger_path = self._applied(root)

            ledger = CanonicalSizeRemediator(store, "bucket", spec).rollback(
                plan_path=plan_path,
                ledger_path=ledger_path,
                execute=True,
            )

            self.assertEqual(
                [
                    "source/canonical-test/transactions_v2/shard-00000.parquet",
                    "source/canonical-test/_control/shards/shard-00000.json",
                    "source/canonical-test/_control/dataset_manifest.json",
                ],
                store.mutations,
            )
            self.assertEqual("ROLLED_BACK", ledger["status"])
            self.assertEqual(3, len(ledger["rollbacks"]))

    def test_rollback_is_idempotent_after_verified_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, spec, plan_path, ledger_path = self._applied(root)
            remediator = CanonicalSizeRemediator(store, "bucket", spec)
            remediator.rollback(
                plan_path=plan_path,
                ledger_path=ledger_path,
                execute=True,
            )
            store.mutations.clear()

            ledger = remediator.rollback(
                plan_path=plan_path,
                ledger_path=ledger_path,
                execute=True,
            )

            self.assertEqual([], store.mutations)
            self.assertEqual("ROLLED_BACK", ledger["status"])

    def test_partial_apply_failure_can_be_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, spec = _fixture()
            plan_path = root / "remediation-plan.json"
            ledger_path = root / "apply-ledger.json"
            remediator = CanonicalSizeRemediator(store, "bucket", spec)
            remediator.plan(staging_directory=root, plan_path=plan_path)
            store.fail_on_mutation_number = 2
            with self.assertRaises(RuntimeError):
                remediator.apply(
                    plan_path=plan_path,
                    ledger_path=ledger_path,
                    execute=True,
                )
            store.fail_on_mutation_number = None
            store.mutations.clear()

            ledger = remediator.rollback(
                plan_path=plan_path,
                ledger_path=ledger_path,
                execute=True,
            )

            self.assertEqual("ROLLED_BACK", ledger["status"])
            self.assertEqual(
                ["source/canonical-test/transactions_v2/shard-00000.parquet"],
                store.mutations,
            )

    def test_rollback_recovers_upload_that_succeeded_before_client_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, spec = _fixture()
            plan_path = root / "remediation-plan.json"
            ledger_path = root / "apply-ledger.json"
            remediator = CanonicalSizeRemediator(store, "bucket", spec)
            remediator.plan(staging_directory=root, plan_path=plan_path)
            store.fail_after_mutation_number = 1
            with self.assertRaisesRegex(RuntimeError, "post-upload failure"):
                remediator.apply(
                    plan_path=plan_path,
                    ledger_path=ledger_path,
                    execute=True,
                )
            store.fail_after_mutation_number = None
            ledger = json.loads(ledger_path.read_text())
            self.assertEqual("PENDING", ledger["mutations"][0]["state"])
            store.mutations.clear()

            rolled_back = remediator.rollback(
                plan_path=plan_path,
                ledger_path=ledger_path,
                execute=True,
            )

            self.assertEqual("ROLLED_BACK", rolled_back["status"])
            self.assertEqual(
                ["source/canonical-test/transactions_v2/shard-00000.parquet"],
                store.mutations,
            )

    def test_rollback_refuses_an_unrecorded_live_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, spec, plan_path, ledger_path = self._applied(root)
            object_name = "source/canonical-test/transactions_v2/shard-00000.parquet"
            current = store.metadata(object_name)
            store.upload_bytes(
                object_name,
                b"unexpected third-party content",
                if_generation_match=current.generation,
            )
            store.mutations.clear()

            with self.assertRaises(AmbiguousRollbackState):
                CanonicalSizeRemediator(store, "bucket", spec).rollback(
                    plan_path=plan_path,
                    ledger_path=ledger_path,
                    execute=True,
                )

            self.assertEqual([], store.mutations)


class CliTest(unittest.TestCase):
    def test_plan_cli_parses_required_paths_without_execute(self) -> None:
        args = parse_args(
            [
                "plan",
                "--project",
                "project-id",
                "--bucket",
                "bucket-name",
                "--staging-dir",
                "/tmp/stage",
                "--plan-out",
                "/tmp/stage/plan.json",
            ]
        )
        self.assertEqual("plan", args.command)
        self.assertFalse(hasattr(args, "execute"))

    def test_apply_without_execute_fails_before_creating_cloud_client(self) -> None:
        with patch(
            "ops.gcp.remediate_canonical_size.GoogleCloudStorageStore.from_project"
        ) as factory:
            with self.assertRaisesRegex(RemediationError, "--execute"):
                main(
                    [
                        "apply",
                        "--project",
                        "project-id",
                        "--bucket",
                        "bucket-name",
                        "--plan",
                        "/tmp/plan.json",
                        "--ledger",
                        "/tmp/ledger.json",
                    ]
                )
        factory.assert_not_called()


class GoogleCloudStorageStoreTest(unittest.TestCase):
    def test_versioning_status_is_reloaded_from_bucket(self) -> None:
        bucket = Mock()
        bucket.versioning_enabled = True
        store = GoogleCloudStorageStore(bucket)

        self.assertTrue(store.versioning_enabled())
        bucket.reload.assert_called_once_with()

    def test_upload_uses_exact_generation_precondition(self) -> None:
        blob = Mock()
        blob.name = "object"
        blob.size = 3
        blob.md5_hash = _md5_bytes(b"new")
        blob.generation = 22
        bucket = Mock()
        bucket.blob.return_value = blob
        store = GoogleCloudStorageStore(bucket)

        result = store.upload_bytes(
            "object",
            b"new",
            if_generation_match="21",
        )

        blob.upload_from_string.assert_called_once_with(
            b"new",
            if_generation_match=21,
        )
        blob.reload.assert_called_once_with()
        self.assertEqual("22", result.generation)


if __name__ == "__main__":
    unittest.main()
