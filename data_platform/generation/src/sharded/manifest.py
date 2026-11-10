"""Publish shard outputs and promote a dataset only after complete validation."""

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from data_platform.generation.src.offline.merchant_history import (
    HISTORY_SOURCE,
    MERCHANT_HISTORY_TABLE,
)

from .generator import LEGACY_PHYSICAL_TABLES, PHYSICAL_TABLES
from .models import ShardFileMetadata, ShardManifest, ShardPlan
from .storage import ObjectStore, file_md5_base64


class ObjectConflictError(RuntimeError):
    """An immutable remote object differs from the expected local content."""


class IncompleteDatasetError(RuntimeError):
    """The final dataset marker cannot be written because shards are missing."""


# Written to the dataset manifest only when the extension is present, so a
# legacy nine-table manifest stays byte-identical to what was published.
MERCHANT_HISTORY_EXTENSION = {
    "table": MERCHANT_HISTORY_TABLE,
    "provenance": HISTORY_SOURCE,
    "generator": "data_platform.generation.src.offline.merchant_history",
    "semantics": (
        "Synthetic full merchant snapshots on a fixed schedule from the first "
        "transaction instant; the last snapshot equals merchants. Not observed "
        "real-world merchant history."
    ),
}


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


class DatasetPublisher:
    """Own the immutable object layout for one source dataset version."""

    def __init__(
        self,
        store: ObjectStore,
        dataset_root: str,
        plan: ShardPlan,
        physical_tables: tuple[str, ...] = PHYSICAL_TABLES,
    ) -> None:
        allowed = (LEGACY_PHYSICAL_TABLES, LEGACY_PHYSICAL_TABLES + (MERCHANT_HISTORY_TABLE,))
        if tuple(physical_tables) not in allowed:
            raise ValueError(f"unsupported physical table contract: {physical_tables}")
        self.store = store
        self.dataset_root = dataset_root.rstrip("/")
        self.plan = plan
        self.physical_tables = tuple(physical_tables)

    @property
    def plan_uri(self) -> str:
        return f"{self.dataset_root}/_control/plan.json"

    @property
    def dataset_manifest_uri(self) -> str:
        return f"{self.dataset_root}/_control/dataset_manifest.json"

    def shard_manifest_uri(self, shard_id: int) -> str:
        return f"{self.dataset_root}/_control/shards/shard-{shard_id:05d}.json"

    def file_uri(self, shard_id: int, table: str) -> str:
        return f"{self.dataset_root}/{table}/shard-{shard_id:05d}.parquet"

    def ensure_plan(self) -> None:
        expected = self.plan.to_dict()
        if self.store.exists(self.plan_uri):
            actual = json.loads(self.store.read_text(self.plan_uri))
            if actual != expected:
                raise ObjectConflictError(
                    "remote source plan conflicts with requested immutable plan"
                )
            return
        text = _json_text(expected)
        self.store.write_text(text, self.plan_uri, if_absent=True)
        if json.loads(self.store.read_text(self.plan_uri)) != expected:
            raise ObjectConflictError("remote source plan verification failed")

    def dataset_complete(self) -> bool:
        return self.store.exists(self.dataset_manifest_uri)

    def _validate_manifest_identity(
        self,
        manifest: ShardManifest,
        shard_id: int,
    ) -> None:
        if manifest.dataset_version != self.plan.dataset_version:
            raise ObjectConflictError("shard dataset version conflicts with plan")
        if manifest.plan_digest != self.plan.digest:
            raise ObjectConflictError("shard plan digest conflicts with plan")
        if manifest.shard_id != shard_id:
            raise ObjectConflictError("shard ID conflicts with manifest path")
        tables = tuple(item.table for item in manifest.files)
        if tables != self.physical_tables:
            raise ObjectConflictError(
                "shard physical tables changed: "
                f"expected {self.physical_tables}, got {tables}"
            )

    def completed_shard(self, shard_id: int) -> ShardManifest | None:
        uri = self.shard_manifest_uri(shard_id)
        if not self.store.exists(uri):
            return None
        manifest = ShardManifest.from_dict(json.loads(self.store.read_text(uri)))
        self._validate_manifest_identity(manifest, shard_id)
        def verify_item(item: ShardFileMetadata) -> None:
            file_uri = self.file_uri(shard_id, item.table)
            if not self.store.exists(file_uri):
                raise ObjectConflictError(
                    f"completed shard references missing object: {file_uri}"
                )
            metadata = self.store.stat(file_uri)
            if metadata.size != item.bytes or (
                metadata.md5_base64 is not None
                and metadata.md5_base64 != item.md5_base64
            ):
                raise ObjectConflictError(
                    f"completed shard object content changed: {file_uri}"
                )

        with ThreadPoolExecutor(max_workers=min(8, len(manifest.files))) as executor:
            tuple(executor.map(verify_item, manifest.files))
        return manifest

    def _published_manifest(self, manifest: ShardManifest) -> ShardManifest:
        return replace(
            manifest,
            files=tuple(
                replace(
                    item,
                    relative_path=f"{item.table}/shard-{manifest.shard_id:05d}.parquet",
                )
                for item in manifest.files
            ),
        )

    def _verify_or_write_file(
        self,
        local_path: Path,
        remote_uri: str,
        expected: ShardFileMetadata,
    ) -> None:
        local_md5 = file_md5_base64(local_path)
        if local_md5 != expected.md5_base64:
            raise ObjectConflictError(
                f"local shard checksum differs from manifest: {local_path.name}"
            )
        if self.store.exists(remote_uri):
            metadata = self.store.stat(remote_uri)
            if metadata.size != expected.bytes or (
                metadata.md5_base64 is not None
                and metadata.md5_base64 != local_md5
            ):
                raise ObjectConflictError(
                    f"immutable object conflicts with generated shard: {remote_uri}"
                )
            return

        try:
            self.store.write_file(local_path, remote_uri, if_absent=True)
        except FileExistsError:
            pass
        metadata = self.store.stat(remote_uri)
        if metadata.size != expected.bytes or (
            metadata.md5_base64 is not None and metadata.md5_base64 != local_md5
        ):
            raise ObjectConflictError(f"uploaded object verification failed: {remote_uri}")

    def publish_shard(
        self,
        local_directory: Path,
        manifest: ShardManifest,
        *,
        cleanup_local: bool,
    ) -> ShardManifest:
        self._validate_manifest_identity(manifest, manifest.shard_id)
        completed = self.completed_shard(manifest.shard_id)
        if completed is not None:
            if completed.to_dict() != self._published_manifest(manifest).to_dict():
                raise ObjectConflictError("completed shard differs from local manifest")
            if cleanup_local:
                shutil.rmtree(local_directory)
            return completed

        def publish_item(item: ShardFileMetadata) -> None:
            local_path = local_directory / f"{item.table}.parquet"
            if not local_path.is_file():
                raise FileNotFoundError(local_path)
            self._verify_or_write_file(
                local_path,
                self.file_uri(manifest.shard_id, item.table),
                item,
            )

        with ThreadPoolExecutor(max_workers=min(8, len(manifest.files))) as executor:
            tuple(executor.map(publish_item, manifest.files))

        published = self._published_manifest(manifest)
        manifest_uri = self.shard_manifest_uri(manifest.shard_id)
        self.store.write_text(
            _json_text(published.to_dict()),
            manifest_uri,
            if_absent=True,
        )
        verified = self.completed_shard(manifest.shard_id)
        if verified is None or verified.to_dict() != published.to_dict():
            raise ObjectConflictError("remote shard manifest verification failed")
        if cleanup_local:
            shutil.rmtree(local_directory)
        return verified

    def finalize(self) -> dict[str, Any]:
        shards: list[ShardManifest] = []
        missing: list[int] = []
        for shard_id in range(self.plan.shard_count):
            manifest = self.completed_shard(shard_id)
            if manifest is None:
                missing.append(shard_id)
            else:
                shards.append(manifest)
        if missing:
            raise IncompleteDatasetError(
                "dataset cannot be finalized; missing shard manifests: "
                + ", ".join(str(item) for item in missing)
            )

        tables: dict[str, dict[str, Any]] = {}
        for table in self.physical_tables:
            table_files = [
                (shard.shard_id, next(item for item in shard.files if item.table == table))
                for shard in shards
            ]
            tables[table] = {
                "rows": sum(item.rows for _, item in table_files),
                "bytes": sum(item.bytes for _, item in table_files),
                "schema_sha256": sorted(
                    {item.schema_sha256 for _, item in table_files}
                ),
                "files": [
                    {
                        "shard_id": shard_id,
                        "path": item.relative_path,
                        "rows": item.rows,
                        "bytes": item.bytes,
                        "sha256": item.sha256,
                        "md5_base64": item.md5_base64,
                    }
                    for shard_id, item in table_files
                ],
            }

        scenario_names = sorted(
            {name for shard in shards for name in shard.fraud_scenario_counts}
        )
        scenario_counts = {
            name: sum(shard.fraud_scenario_counts.get(name, 0) for shard in shards)
            for name in scenario_names
        }
        fraud_label_rows = tables["fraud_labels"]["rows"]
        fraud_rows = sum(scenario_counts.values())
        if fraud_rows > fraud_label_rows:
            raise ObjectConflictError(
                "fraud scenario totals exceed fraud_labels row count"
            )
        dataset_manifest: dict[str, Any] = {
            "schema_version": 1,
            "status": "COMPLETE",
            "dataset_version": self.plan.dataset_version,
            "plan_digest": self.plan.digest,
            "shard_count": self.plan.shard_count,
            "logical_transactions": sum(
                shard.logical_transactions for shard in shards
            ),
            "physical_transactions": sum(
                shard.physical_transactions for shard in shards
            ),
            "fraud_label_rows": fraud_label_rows,
            "fraud_rows": fraud_rows,
            "fraud_scenario_counts": scenario_counts,
            "total_bytes": sum(
                item.bytes for shard in shards for item in shard.files
            ),
            "tables": tables,
        }
        if MERCHANT_HISTORY_TABLE in self.physical_tables:
            dataset_manifest["source_extensions"] = {
                MERCHANT_HISTORY_TABLE: dict(MERCHANT_HISTORY_EXTENSION)
            }

        if self.store.exists(self.dataset_manifest_uri):
            actual = json.loads(self.store.read_text(self.dataset_manifest_uri))
            if actual != dataset_manifest:
                raise ObjectConflictError("completed dataset manifest changed")
            return actual
        self.store.write_text(
            _json_text(dataset_manifest),
            self.dataset_manifest_uri,
            if_absent=True,
        )
        actual = json.loads(self.store.read_text(self.dataset_manifest_uri))
        if actual != dataset_manifest:
            raise ObjectConflictError("dataset manifest verification failed")
        return actual
