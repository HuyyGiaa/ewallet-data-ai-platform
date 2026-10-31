"""Safely plan, apply, or roll back the canonical source size remediation."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


APPROVED_DATASET_VERSION = "canonical-100gb-v1"
APPROVED_CURRENT_TOTAL_BYTES = 99_899_644_424
APPROVED_MINIMUM_FINAL_BYTES = 100_150_000_000
APPROVED_SHARDS = tuple(range(35))
APPROVED_TABLE = "transactions_v2"
APPROVED_SHARD_COUNT = 3_696
APPROVED_LOGICAL_TRANSACTIONS = 369_600_000
APPROVED_PHYSICAL_TRANSACTIONS = 376_992_000
APPROVED_FRAUD_LABEL_ROWS = 369_600_000
APPROVED_FRAUD_ROWS = 7_392_000


class RemediationError(RuntimeError):
    """Base class for fail-closed remediation failures."""


class ChecksumMismatchError(RemediationError):
    """An object does not match its manifest checksum metadata."""


class InvariantViolation(RemediationError):
    """A dataset, schema, value, or count invariant changed."""


class GenerationConflict(RemediationError):
    """An object generation changed after the immutable plan was created."""


class AmbiguousRollbackState(RemediationError):
    """Rollback cannot prove which generation is currently live."""


@dataclass(frozen=True)
class ObjectVersion:
    name: str
    size: int
    md5_base64: str | None
    generation: str


@dataclass(frozen=True)
class RemediationSpec:
    dataset_version: str
    selected_shards: tuple[int, ...]
    expected_shard_count: int
    expected_current_total_bytes: int
    minimum_final_bytes: int
    expected_logical_transactions: int
    expected_physical_transactions: int
    expected_fraud_label_rows: int
    expected_fraud_rows: int
    table: str = APPROVED_TABLE


CANONICAL_SPEC = RemediationSpec(
    dataset_version=APPROVED_DATASET_VERSION,
    selected_shards=APPROVED_SHARDS,
    expected_shard_count=APPROVED_SHARD_COUNT,
    expected_current_total_bytes=APPROVED_CURRENT_TOTAL_BYTES,
    minimum_final_bytes=APPROVED_MINIMUM_FINAL_BYTES,
    expected_logical_transactions=APPROVED_LOGICAL_TRANSACTIONS,
    expected_physical_transactions=APPROVED_PHYSICAL_TRANSACTIONS,
    expected_fraud_label_rows=APPROVED_FRAUD_LABEL_ROWS,
    expected_fraud_rows=APPROVED_FRAUD_ROWS,
)


class VersionedObjectStore(Protocol):
    def versioning_enabled(self) -> bool: ...

    def metadata(self, name: str, *, generation: str | None = None) -> ObjectVersion: ...

    def download_bytes(self, name: str, *, generation: str | None = None) -> bytes: ...

    def download_to_file(
        self,
        name: str,
        destination: Path,
        *,
        generation: str | None = None,
    ) -> None: ...

    def upload_bytes(
        self,
        name: str,
        payload: bytes,
        *,
        if_generation_match: str,
    ) -> ObjectVersion: ...

    def upload_file(
        self,
        name: str,
        source: Path,
        *,
        if_generation_match: str,
    ) -> ObjectVersion: ...

    def restore_generation(
        self,
        name: str,
        *,
        source_generation: str,
        if_generation_match: str,
    ) -> ObjectVersion: ...


class GoogleCloudStorageStore:
    """GCS adapter that makes every mutation generation-conditional."""

    def __init__(self, bucket: Any) -> None:
        self._bucket = bucket

    @classmethod
    def from_project(cls, *, project: str, bucket: str) -> "GoogleCloudStorageStore":
        from google.cloud import storage

        client = storage.Client(project=project)
        return cls(client.bucket(bucket))

    def _blob(self, name: str, generation: str | None = None) -> Any:
        kwargs: dict[str, Any] = {}
        if generation is not None:
            kwargs["generation"] = int(generation)
        return self._bucket.blob(name, **kwargs)

    def versioning_enabled(self) -> bool:
        self._bucket.reload()
        return bool(self._bucket.versioning_enabled)

    @staticmethod
    def _version(blob: Any) -> ObjectVersion:
        return ObjectVersion(
            name=str(blob.name),
            size=int(blob.size),
            md5_base64=blob.md5_hash,
            generation=str(blob.generation),
        )

    def metadata(self, name: str, *, generation: str | None = None) -> ObjectVersion:
        blob = self._blob(name, generation)
        blob.reload()
        return self._version(blob)

    def download_bytes(self, name: str, *, generation: str | None = None) -> bytes:
        return self._blob(name, generation).download_as_bytes()

    def download_to_file(
        self,
        name: str,
        destination: Path,
        *,
        generation: str | None = None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._blob(name, generation).download_to_filename(str(destination))

    def upload_bytes(
        self,
        name: str,
        payload: bytes,
        *,
        if_generation_match: str,
    ) -> ObjectVersion:
        blob = self._blob(name)
        try:
            blob.upload_from_string(
                payload,
                if_generation_match=int(if_generation_match),
            )
        except BaseException as exc:
            self._raise_generation_conflict(name, exc)
            raise
        blob.reload()
        return self._version(blob)

    def upload_file(
        self,
        name: str,
        source: Path,
        *,
        if_generation_match: str,
    ) -> ObjectVersion:
        blob = self._blob(name)
        try:
            blob.upload_from_filename(
                str(source),
                if_generation_match=int(if_generation_match),
            )
        except BaseException as exc:
            self._raise_generation_conflict(name, exc)
            raise
        blob.reload()
        return self._version(blob)

    def restore_generation(
        self,
        name: str,
        *,
        source_generation: str,
        if_generation_match: str,
    ) -> ObjectVersion:
        payload = self.download_bytes(name, generation=source_generation)
        return self.upload_bytes(
            name,
            payload,
            if_generation_match=if_generation_match,
        )

    @staticmethod
    def _raise_generation_conflict(name: str, exc: BaseException) -> None:
        try:
            from google.api_core.exceptions import PreconditionFailed
        except ImportError:
            return
        if isinstance(exc, PreconditionFailed):
            raise GenerationConflict(
                f"GCS generation precondition failed: {name}"
            ) from exc


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5_bytes(payload: bytes) -> str:
    return base64.b64encode(hashlib.md5(payload).digest()).decode("ascii")


def _md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def _load_json(payload: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvariantViolation(f"invalid JSON for {description}") from exc
    if not isinstance(value, dict):
        raise InvariantViolation(f"{description} must be a JSON object")
    return value


def _plan_digest(value: Mapping[str, Any]) -> str:
    unsigned = dict(value)
    unsigned.pop("plan_sha256", None)
    return _sha256_bytes(_canonical_json_bytes(unsigned))


def load_verified_plan(path: Path) -> dict[str, Any]:
    plan = _load_json(path.read_bytes(), "remediation plan")
    expected = str(plan.get("plan_sha256", ""))
    actual = _plan_digest(plan)
    if not expected or actual != expected:
        raise ChecksumMismatchError(
            f"remediation plan digest mismatch: expected={expected}, actual={actual}"
        )
    if plan.get("status") != "PLANNED":
        raise InvariantViolation("remediation plan status must be PLANNED")
    return plan


def _write_immutable_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_canonical_json_bytes(value))
    os.replace(temporary, path)


def assert_tables_identical(original: Any, rewritten: Any) -> None:
    if not original.schema.equals(rewritten.schema, check_metadata=True):
        raise InvariantViolation("Parquet rewrite changed the Arrow schema")
    if original.num_rows != rewritten.num_rows:
        raise InvariantViolation("Parquet rewrite changed the row count")
    if not original.equals(rewritten):
        raise InvariantViolation("Parquet rewrite changed values or row order")


def _validate_dataset_manifest(
    manifest: Mapping[str, Any], spec: RemediationSpec
) -> None:
    expected = {
        "status": "COMPLETE",
        "dataset_version": spec.dataset_version,
        "shard_count": spec.expected_shard_count,
        "logical_transactions": spec.expected_logical_transactions,
        "physical_transactions": spec.expected_physical_transactions,
        "fraud_label_rows": spec.expected_fraud_label_rows,
        "fraud_rows": spec.expected_fraud_rows,
        "total_bytes": spec.expected_current_total_bytes,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise InvariantViolation(
                f"dataset manifest {key} mismatch: "
                f"expected={value!r}, actual={manifest.get(key)!r}"
            )
    tables = manifest.get("tables")
    if not isinstance(tables, Mapping) or spec.table not in tables:
        raise InvariantViolation(f"dataset manifest is missing table {spec.table}")


def _object_record(metadata: ObjectVersion, payload: bytes) -> dict[str, Any]:
    if metadata.md5_base64 is None:
        raise ChecksumMismatchError(f"GCS object has no MD5: {metadata.name}")
    return {
        "name": metadata.name,
        "generation": metadata.generation,
        "size": metadata.size,
        "md5_base64": metadata.md5_base64,
        "sha256": _sha256_bytes(payload),
    }


class CanonicalSizeRemediator:
    def __init__(
        self,
        store: VersionedObjectStore,
        bucket: str,
        spec: RemediationSpec = CANONICAL_SPEC,
        project: str | None = None,
    ) -> None:
        self.store = store
        self.bucket = bucket
        self.spec = spec
        self.project = project
        self.root = f"source/{spec.dataset_version}"

    def _dataset_manifest_name(self) -> str:
        return f"{self.root}/_control/dataset_manifest.json"

    def _shard_manifest_name(self, shard_id: int) -> str:
        return f"{self.root}/_control/shards/shard-{shard_id:05d}.json"

    def _parquet_name(self, shard_id: int) -> str:
        return f"{self.root}/{self.spec.table}/shard-{shard_id:05d}.parquet"

    def plan(self, *, staging_directory: Path, plan_path: Path) -> dict[str, Any]:
        import pyarrow.parquet as pq

        if plan_path.exists():
            raise FileExistsError(plan_path)
        if not self.store.versioning_enabled():
            raise InvariantViolation(
                "GCS object versioning must be enabled for safe rollback"
            )
        staging_directory.mkdir(parents=True, exist_ok=True)
        original_directory = staging_directory / "original"
        rewritten_directory = staging_directory / "rewritten"
        prepared_directory = staging_directory / "prepared-manifests"
        for directory in (original_directory, rewritten_directory, prepared_directory):
            directory.mkdir(parents=True, exist_ok=True)

        dataset_name = self._dataset_manifest_name()
        dataset_meta = self.store.metadata(dataset_name)
        dataset_payload = self.store.download_bytes(
            dataset_name, generation=dataset_meta.generation
        )
        if len(dataset_payload) != dataset_meta.size or (
            dataset_meta.md5_base64 is not None
            and _md5_bytes(dataset_payload) != dataset_meta.md5_base64
        ):
            raise ChecksumMismatchError("dataset manifest metadata mismatch")
        dataset_manifest = _load_json(dataset_payload, "dataset manifest")
        _validate_dataset_manifest(dataset_manifest, self.spec)
        dataset_original_path = original_directory / "dataset_manifest.json"
        dataset_original_path.write_bytes(dataset_payload)

        object_plans: list[dict[str, Any]] = []
        shard_manifest_plans: list[dict[str, Any]] = []
        total_gain = 0

        for shard_id in self.spec.selected_shards:
            shard_name = self._shard_manifest_name(shard_id)
            shard_meta = self.store.metadata(shard_name)
            shard_payload = self.store.download_bytes(
                shard_name, generation=shard_meta.generation
            )
            if len(shard_payload) != shard_meta.size or (
                shard_meta.md5_base64 is not None
                and _md5_bytes(shard_payload) != shard_meta.md5_base64
            ):
                raise ChecksumMismatchError(
                    f"shard manifest metadata mismatch: {shard_name}"
                )
            shard_manifest = _load_json(shard_payload, f"shard manifest {shard_id}")
            self._validate_shard_identity(shard_manifest, shard_id, dataset_manifest)
            file_entry = self._selected_file(shard_manifest, shard_id)

            object_name = self._parquet_name(shard_id)
            object_meta = self.store.metadata(object_name)
            original_path = original_directory / f"shard-{shard_id:05d}.parquet"
            rewritten_path = rewritten_directory / f"shard-{shard_id:05d}.parquet"
            self.store.download_to_file(
                object_name,
                original_path,
                generation=object_meta.generation,
            )
            self._verify_parquet_source(original_path, object_meta, file_entry)

            original_table = pq.read_table(original_path)
            if original_table.num_rows != int(file_entry["rows"]):
                raise InvariantViolation(
                    f"manifest row count changed for shard {shard_id}"
                )
            pq.write_table(
                original_table,
                rewritten_path,
                version="2.6",
                compression="NONE",
                use_dictionary=False,
                write_statistics=True,
                row_group_size=max(1, original_table.num_rows),
            )
            rewritten_table = pq.read_table(rewritten_path)
            assert_tables_identical(original_table, rewritten_table)

            rewritten_size = rewritten_path.stat().st_size
            rewritten_md5 = _md5_file(rewritten_path)
            rewritten_sha256 = _sha256_file(rewritten_path)
            gain = rewritten_size - int(file_entry["bytes"])
            total_gain += gain

            new_shard_manifest = json.loads(json.dumps(shard_manifest))
            new_entry = self._selected_file(new_shard_manifest, shard_id)
            new_entry["bytes"] = rewritten_size
            new_entry["md5_base64"] = rewritten_md5
            new_entry["sha256"] = rewritten_sha256
            new_shard_payload = _canonical_json_bytes(new_shard_manifest)
            prepared_path = prepared_directory / f"shard-{shard_id:05d}.json"
            prepared_path.write_bytes(new_shard_payload)

            object_plans.append(
                {
                    "shard_id": shard_id,
                    "name": object_name,
                    "original": {
                        **_object_record(object_meta, original_path.read_bytes()),
                        "local_path": os.path.relpath(original_path, plan_path.parent),
                    },
                    "staged": {
                        "local_path": os.path.relpath(rewritten_path, plan_path.parent),
                        "size": rewritten_size,
                        "md5_base64": rewritten_md5,
                        "sha256": rewritten_sha256,
                    },
                    "gain_bytes": gain,
                    "rows": original_table.num_rows,
                }
            )
            shard_manifest_plans.append(
                {
                    "shard_id": shard_id,
                    "name": shard_name,
                    "original": _object_record(shard_meta, shard_payload),
                    "staged": {
                        "local_path": os.path.relpath(prepared_path, plan_path.parent),
                        "size": len(new_shard_payload),
                        "md5_base64": _md5_bytes(new_shard_payload),
                        "sha256": _sha256_bytes(new_shard_payload),
                    },
                }
            )

        projected_total = self.spec.expected_current_total_bytes + total_gain
        if projected_total < self.spec.minimum_final_bytes:
            raise InvariantViolation(
                "projected total is below the remediation minimum: "
                f"projected={projected_total}, minimum={self.spec.minimum_final_bytes}"
            )

        plan: dict[str, Any] = {
            "schema_version": 1,
            "status": "PLANNED",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "project": self.project,
            "bucket": self.bucket,
            "spec": asdict(self.spec),
            "encoding": {
                "parquet_version": "2.6",
                "compression": "NONE",
                "use_dictionary": False,
                "write_statistics": True,
            },
            "dataset_manifest": {
                "name": dataset_name,
                "original": {
                    **_object_record(dataset_meta, dataset_payload),
                    "local_path": os.path.relpath(
                        dataset_original_path, plan_path.parent
                    ),
                },
            },
            "objects": object_plans,
            "shard_manifests": shard_manifest_plans,
            "projected_gain_bytes": total_gain,
            "projected_total_bytes": projected_total,
        }
        plan["plan_sha256"] = _plan_digest(plan)
        _write_immutable_json(plan_path, plan)
        return load_verified_plan(plan_path)

    def apply(
        self,
        *,
        plan_path: Path,
        ledger_path: Path,
        execute: bool,
    ) -> dict[str, Any]:
        if not execute:
            raise RemediationError("apply requires explicit --execute")
        plan = load_verified_plan(plan_path)
        self._validate_plan_identity(plan)
        if not self.store.versioning_enabled():
            raise InvariantViolation(
                "GCS object versioning must remain enabled during apply"
            )
        if ledger_path.exists():
            existing = _load_json(ledger_path.read_bytes(), "apply ledger")
            if existing.get("status") == "APPLIED":
                self._verify_ledger_live_generations(existing)
                return existing
            raise RemediationError(
                "apply ledger already exists without APPLIED status; roll back first"
            )

        self._verify_plan_files(plan_path, plan)
        self._preflight_original_generations(plan)
        ledger: dict[str, Any] = {
            "schema_version": 1,
            "status": "APPLYING",
            "plan_sha256": plan["plan_sha256"],
            "dataset_version": self.spec.dataset_version,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "mutations": [],
        }
        _write_json_atomic(ledger_path, ledger)

        try:
            for item in plan["objects"]:
                self._apply_file_record(item, plan_path, ledger, ledger_path)
            for item in plan["shard_manifests"]:
                self._apply_bytes_record(item, plan_path, ledger, ledger_path)

            rebuilt = self._rebuild_dataset_manifest()
            original_path = self._resolve_local_path(
                plan_path, plan["dataset_manifest"]["original"]["local_path"]
            )
            original_manifest = _load_json(
                original_path.read_bytes(), "original dataset manifest"
            )
            self._validate_rebuilt_manifest(original_manifest, rebuilt, plan)
            dataset_payload = _canonical_json_bytes(rebuilt)
            dataset_record = {
                "name": plan["dataset_manifest"]["name"],
                "original": plan["dataset_manifest"]["original"],
                "staged": {
                    "size": len(dataset_payload),
                    "md5_base64": _md5_bytes(dataset_payload),
                    "sha256": _sha256_bytes(dataset_payload),
                },
            }
            self._apply_payload_record(
                dataset_record,
                dataset_payload,
                ledger,
                ledger_path,
                kind="dataset_manifest",
            )
            ledger["status"] = "APPLIED"
            ledger["completed_at"] = datetime.now(timezone.utc).isoformat()
            ledger["final_total_bytes"] = rebuilt["total_bytes"]
            _write_json_atomic(ledger_path, ledger)
            return ledger
        except BaseException as exc:
            ledger["status"] = "FAILED"
            ledger["failed_at"] = datetime.now(timezone.utc).isoformat()
            ledger["error"] = {"type": type(exc).__name__, "message": str(exc)}
            _write_json_atomic(ledger_path, ledger)
            raise

    def _validate_plan_identity(self, plan: Mapping[str, Any]) -> None:
        if self.project is not None and plan.get("project") != self.project:
            raise InvariantViolation("plan project does not match requested project")
        if plan.get("bucket") != self.bucket:
            raise InvariantViolation("plan bucket does not match requested bucket")
        raw_spec = plan.get("spec")
        if not isinstance(raw_spec, Mapping):
            raise InvariantViolation("plan spec is missing")
        expected = asdict(self.spec)
        expected["selected_shards"] = list(self.spec.selected_shards)
        if dict(raw_spec) != expected:
            raise InvariantViolation("plan spec does not match the requested remediation")
        if plan.get("encoding") != {
            "parquet_version": "2.6",
            "compression": "NONE",
            "use_dictionary": False,
            "write_statistics": True,
        }:
            raise InvariantViolation("plan encoding does not match the approved rewrite")
        if int(plan.get("projected_total_bytes", -1)) < self.spec.minimum_final_bytes:
            raise InvariantViolation("plan projected total is below the minimum")
        dataset = plan.get("dataset_manifest")
        if not isinstance(dataset, Mapping) or dataset.get(
            "name"
        ) != self._dataset_manifest_name():
            raise InvariantViolation("plan dataset manifest path is invalid")

        expected_shards = list(self.spec.selected_shards)
        objects = plan.get("objects")
        manifests = plan.get("shard_manifests")
        if not isinstance(objects, list) or [
            item.get("shard_id") for item in objects
        ] != expected_shards:
            raise InvariantViolation("plan Parquet shard selection is invalid")
        if not isinstance(manifests, list) or [
            item.get("shard_id") for item in manifests
        ] != expected_shards:
            raise InvariantViolation("plan shard-manifest selection is invalid")

        for item in objects:
            shard_id = int(item["shard_id"])
            if item.get("name") != self._parquet_name(shard_id):
                raise InvariantViolation("plan Parquet object path is invalid")
        for item in manifests:
            shard_id = int(item["shard_id"])
            if item.get("name") != self._shard_manifest_name(shard_id):
                raise InvariantViolation("plan shard-manifest path is invalid")

        gain = sum(
            int(item["staged"]["size"]) - int(item["original"]["size"])
            for item in objects
        )
        if int(plan.get("projected_gain_bytes", -1)) != gain:
            raise InvariantViolation("plan projected gain is internally inconsistent")
        if int(plan.get("projected_total_bytes", -1)) != (
            self.spec.expected_current_total_bytes + gain
        ):
            raise InvariantViolation("plan projected total is internally inconsistent")

    def _resolve_local_path(self, plan_path: Path, value: str) -> Path:
        return (plan_path.parent / value).resolve()

    def _verify_plan_files(self, plan_path: Path, plan: Mapping[str, Any]) -> None:
        records = list(plan["objects"]) + list(plan["shard_manifests"])
        for record in records:
            staged = record["staged"]
            path = self._resolve_local_path(plan_path, staged["local_path"])
            if (
                not path.is_file()
                or path.stat().st_size != int(staged["size"])
                or _md5_file(path) != staged["md5_base64"]
                or _sha256_file(path) != staged["sha256"]
            ):
                raise ChecksumMismatchError(
                    f"staged remediation artifact changed: {path}"
                )
        original = plan["dataset_manifest"]["original"]
        original_path = self._resolve_local_path(plan_path, original["local_path"])
        if (
            not original_path.is_file()
            or original_path.stat().st_size != int(original["size"])
            or _md5_file(original_path) != original["md5_base64"]
            or _sha256_file(original_path) != original["sha256"]
        ):
            raise ChecksumMismatchError("original dataset manifest staging copy changed")

    def _preflight_original_generations(self, plan: Mapping[str, Any]) -> None:
        records = (
            list(plan["objects"])
            + list(plan["shard_manifests"])
            + [plan["dataset_manifest"]]
        )
        for record in records:
            expected = record["original"]
            current = self.store.metadata(record["name"])
            if (
                current.generation != str(expected["generation"])
                or current.size != int(expected["size"])
                or (
                    current.md5_base64 is not None
                    and current.md5_base64 != expected["md5_base64"]
                )
            ):
                raise GenerationConflict(
                    f"object changed after planning: {record['name']}"
                )

    def _apply_file_record(
        self,
        record: Mapping[str, Any],
        plan_path: Path,
        ledger: dict[str, Any],
        ledger_path: Path,
    ) -> None:
        path = self._resolve_local_path(plan_path, record["staged"]["local_path"])
        mutation = self._record_pending_mutation(
            record, ledger, ledger_path, kind="parquet"
        )
        uploaded = self.store.upload_file(
            record["name"],
            path,
            if_generation_match=str(record["original"]["generation"]),
        )
        self._record_verified_mutation(
            record,
            uploaded,
            mutation,
            ledger,
            ledger_path,
        )

    def _apply_bytes_record(
        self,
        record: Mapping[str, Any],
        plan_path: Path,
        ledger: dict[str, Any],
        ledger_path: Path,
    ) -> None:
        path = self._resolve_local_path(plan_path, record["staged"]["local_path"])
        self._apply_payload_record(
            record,
            path.read_bytes(),
            ledger,
            ledger_path,
            kind="shard_manifest",
        )

    def _apply_payload_record(
        self,
        record: Mapping[str, Any],
        payload: bytes,
        ledger: dict[str, Any],
        ledger_path: Path,
        *,
        kind: str,
    ) -> None:
        mutation = self._record_pending_mutation(
            record, ledger, ledger_path, kind=kind
        )
        uploaded = self.store.upload_bytes(
            record["name"],
            payload,
            if_generation_match=str(record["original"]["generation"]),
        )
        self._record_verified_mutation(
            record,
            uploaded,
            mutation,
            ledger,
            ledger_path,
        )

    def _record_pending_mutation(
        self,
        record: Mapping[str, Any],
        ledger: dict[str, Any],
        ledger_path: Path,
        *,
        kind: str,
    ) -> dict[str, Any]:
        mutation = {
            "state": "PENDING",
            "kind": kind,
            "name": record["name"],
            "old_generation": str(record["original"]["generation"]),
            "old_size": int(record["original"]["size"]),
            "old_md5_base64": record["original"]["md5_base64"],
            "expected_new_size": int(record["staged"]["size"]),
            "expected_new_md5_base64": record["staged"]["md5_base64"],
        }
        ledger["mutations"].append(mutation)
        _write_json_atomic(ledger_path, ledger)
        return mutation

    def _record_verified_mutation(
        self,
        record: Mapping[str, Any],
        uploaded: ObjectVersion,
        mutation: dict[str, Any],
        ledger: dict[str, Any],
        ledger_path: Path,
    ) -> None:
        expected = record["staged"]
        if (
            uploaded.size != int(expected["size"])
            or uploaded.md5_base64 != expected["md5_base64"]
        ):
            raise ChecksumMismatchError(
                f"uploaded object verification failed: {record['name']}"
            )
        mutation.update(
            {
                "state": "APPLIED",
                "new_generation": uploaded.generation,
                "new_size": uploaded.size,
                "new_md5_base64": uploaded.md5_base64,
            }
        )
        _write_json_atomic(ledger_path, ledger)

    def _rebuild_dataset_manifest(self) -> dict[str, Any]:
        shards: list[dict[str, Any]] = []
        for shard_id in range(self.spec.expected_shard_count):
            name = self._shard_manifest_name(shard_id)
            metadata = self.store.metadata(name)
            payload = self.store.download_bytes(
                name, generation=metadata.generation
            )
            if len(payload) != metadata.size or (
                metadata.md5_base64 is not None
                and _md5_bytes(payload) != metadata.md5_base64
            ):
                raise ChecksumMismatchError(
                    f"shard manifest metadata mismatch during rebuild: {name}"
                )
            manifest = _load_json(payload, f"shard manifest {shard_id}")
            if manifest.get("shard_id") != shard_id:
                raise InvariantViolation(f"shard manifest ID mismatch: {name}")
            if manifest.get("dataset_version") != self.spec.dataset_version:
                raise InvariantViolation(f"dataset version mismatch: {name}")
            shards.append(manifest)

        table_order: list[str] = []
        if shards:
            table_order = [str(item["table"]) for item in shards[0]["files"]]
        tables: dict[str, dict[str, Any]] = {}
        for table in table_order:
            table_files: list[tuple[int, Mapping[str, Any]]] = []
            for shard in shards:
                matches = [item for item in shard["files"] if item["table"] == table]
                if len(matches) != 1:
                    raise InvariantViolation(
                        f"shard {shard['shard_id']} has invalid {table} inventory"
                    )
                table_files.append((int(shard["shard_id"]), matches[0]))
            tables[table] = {
                "rows": sum(int(item["rows"]) for _, item in table_files),
                "bytes": sum(int(item["bytes"]) for _, item in table_files),
                "schema_sha256": sorted(
                    {str(item["schema_sha256"]) for _, item in table_files}
                ),
                "files": [
                    {
                        "shard_id": shard_id,
                        "path": item["relative_path"],
                        "rows": int(item["rows"]),
                        "bytes": int(item["bytes"]),
                        "sha256": item["sha256"],
                        "md5_base64": item["md5_base64"],
                    }
                    for shard_id, item in table_files
                ],
            }
        scenario_names = sorted(
            {
                name
                for shard in shards
                for name in shard.get("fraud_scenario_counts", {})
            }
        )
        scenario_counts = {
            name: sum(
                int(shard.get("fraud_scenario_counts", {}).get(name, 0))
                for shard in shards
            )
            for name in scenario_names
        }
        fraud_label_rows = int(tables.get("fraud_labels", {}).get("rows", 0))
        return {
            "schema_version": 1,
            "status": "COMPLETE",
            "dataset_version": self.spec.dataset_version,
            "plan_digest": shards[0]["plan_digest"] if shards else "",
            "shard_count": len(shards),
            "logical_transactions": sum(
                int(shard["logical_transactions"]) for shard in shards
            ),
            "physical_transactions": sum(
                int(shard["physical_transactions"]) for shard in shards
            ),
            "fraud_label_rows": fraud_label_rows,
            "fraud_rows": sum(scenario_counts.values()),
            "fraud_scenario_counts": scenario_counts,
            "total_bytes": sum(
                int(item["bytes"])
                for shard in shards
                for item in shard["files"]
            ),
            "tables": tables,
        }

    def _validate_rebuilt_manifest(
        self,
        original: Mapping[str, Any],
        rebuilt: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> None:
        _validate_dataset_manifest(
            {**rebuilt, "total_bytes": self.spec.expected_current_total_bytes},
            self.spec,
        )
        invariant_keys = (
            "schema_version",
            "status",
            "dataset_version",
            "plan_digest",
            "shard_count",
            "logical_transactions",
            "physical_transactions",
            "fraud_label_rows",
            "fraud_rows",
            "fraud_scenario_counts",
        )
        for key in invariant_keys:
            if rebuilt.get(key) != original.get(key):
                raise InvariantViolation(f"dataset invariant changed: {key}")
        if int(rebuilt["total_bytes"]) != int(plan["projected_total_bytes"]):
            raise InvariantViolation("rebuilt dataset total differs from the plan")
        if set(rebuilt["tables"]) != set(original["tables"]):
            raise InvariantViolation("dataset table inventory changed")
        for table, original_table in original["tables"].items():
            rebuilt_table = rebuilt["tables"][table]
            if rebuilt_table["rows"] != original_table["rows"]:
                raise InvariantViolation(f"table row count changed: {table}")
            if rebuilt_table["schema_sha256"] != original_table["schema_sha256"]:
                raise InvariantViolation(f"table schema hashes changed: {table}")
            original_files = original_table["files"]
            rebuilt_files = rebuilt_table["files"]
            if len(original_files) != len(rebuilt_files):
                raise InvariantViolation(f"table file count changed: {table}")
            for old, new in zip(original_files, rebuilt_files):
                for key in ("shard_id", "path", "rows"):
                    if old[key] != new[key]:
                        raise InvariantViolation(
                            f"table file invariant changed: {table}.{key}"
                        )
                selected = (
                    table == self.spec.table
                    and int(old["shard_id"]) in self.spec.selected_shards
                )
                if not selected:
                    for key in ("bytes", "sha256", "md5_base64"):
                        if old[key] != new[key]:
                            raise InvariantViolation(
                                "unaffected file metadata changed: "
                                f"{table}/{old['shard_id']}.{key}"
                            )

    def _verify_ledger_live_generations(self, ledger: Mapping[str, Any]) -> None:
        for mutation in ledger.get("mutations", []):
            current = self.store.metadata(mutation["name"])
            if current.generation != str(mutation["new_generation"]):
                raise GenerationConflict(
                    f"applied object no longer matches ledger: {mutation['name']}"
                )

    def rollback(
        self,
        *,
        plan_path: Path,
        ledger_path: Path,
        execute: bool,
    ) -> dict[str, Any]:
        if not execute:
            raise RemediationError("rollback requires explicit --execute")
        plan = load_verified_plan(plan_path)
        self._validate_plan_identity(plan)
        if not self.store.versioning_enabled():
            raise InvariantViolation(
                "GCS object versioning must remain enabled during rollback"
            )
        if not ledger_path.is_file():
            raise RemediationError("rollback requires an apply ledger")
        ledger = _load_json(ledger_path.read_bytes(), "apply ledger")
        if ledger.get("plan_sha256") != plan["plan_sha256"]:
            raise ChecksumMismatchError("apply ledger belongs to a different plan")
        if ledger.get("status") == "ROLLED_BACK":
            self._verify_rollback_complete(ledger)
            return ledger
        if ledger.get("status") not in {
            "APPLIED",
            "FAILED",
            "ROLLBACK_FAILED",
            "ROLLING_BACK",
        }:
            raise AmbiguousRollbackState(
                f"cannot roll back ledger status {ledger.get('status')!r}"
            )

        mutations = list(ledger.get("mutations", []))
        priority = {"parquet": 0, "shard_manifest": 1, "dataset_manifest": 2}
        mutations.sort(key=lambda item: (priority[item["kind"]], item["name"]))
        completed = {
            item["name"]: item for item in ledger.setdefault("rollbacks", [])
        }
        ledger["status"] = "ROLLING_BACK"
        _write_json_atomic(ledger_path, ledger)
        try:
            for mutation in mutations:
                name = mutation["name"]
                current = self.store.metadata(name)
                prior = completed.get(name)
                if prior is not None:
                    if current.generation != str(prior["restored_generation"]):
                        raise AmbiguousRollbackState(
                            f"restored object changed after rollback: {name}"
                        )
                    self._verify_original_content(current, mutation)
                    continue

                expected_new = (
                    current.size == int(mutation["expected_new_size"])
                    and current.md5_base64 == mutation["expected_new_md5_base64"]
                )
                recorded_new_generation = mutation.get("new_generation")
                is_recorded_applied = (
                    recorded_new_generation is not None
                    and current.generation == str(recorded_new_generation)
                )
                if is_recorded_applied or (
                    mutation.get("state") == "PENDING" and expected_new
                ):
                    restored = self.store.restore_generation(
                        name,
                        source_generation=str(mutation["old_generation"]),
                        if_generation_match=current.generation,
                    )
                    self._verify_original_content(restored, mutation)
                    rollback_record = {
                        "kind": mutation["kind"],
                        "name": name,
                        "source_generation": str(mutation["old_generation"]),
                        "replaced_generation": current.generation,
                        "restored_generation": restored.generation,
                    }
                    ledger["rollbacks"].append(rollback_record)
                    completed[name] = rollback_record
                    _write_json_atomic(ledger_path, ledger)
                    continue

                if self._is_original_content(current, mutation):
                    rollback_record = {
                        "kind": mutation["kind"],
                        "name": name,
                        "source_generation": str(mutation["old_generation"]),
                        "replaced_generation": str(
                            mutation.get("new_generation", current.generation)
                        ),
                        "restored_generation": current.generation,
                        "already_restored": True,
                    }
                    ledger["rollbacks"].append(rollback_record)
                    completed[name] = rollback_record
                    _write_json_atomic(ledger_path, ledger)
                    continue

                raise AmbiguousRollbackState(
                    f"live object is neither applied nor original: {name}"
                )

            ledger["status"] = "ROLLED_BACK"
            ledger["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
            ledger.pop("rollback_error", None)
            _write_json_atomic(ledger_path, ledger)
            self._verify_rollback_complete(ledger)
            return ledger
        except BaseException as exc:
            ledger["status"] = "ROLLBACK_FAILED"
            ledger["rollback_error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            _write_json_atomic(ledger_path, ledger)
            raise

    def _is_original_content(
        self, metadata: ObjectVersion, mutation: Mapping[str, Any]
    ) -> bool:
        expected_md5 = mutation.get("old_md5_base64")
        if expected_md5 is None or metadata.md5_base64 is None:
            return (
                metadata.generation == str(mutation["old_generation"])
                and metadata.size == int(mutation["old_size"])
            )
        return (
            metadata.size == int(mutation["old_size"])
            and metadata.md5_base64 == expected_md5
        )

    def _verify_original_content(
        self, metadata: ObjectVersion, mutation: Mapping[str, Any]
    ) -> None:
        if not self._is_original_content(metadata, mutation):
            raise ChecksumMismatchError(
                f"rollback verification failed: {mutation['name']}"
            )

    def _verify_rollback_complete(self, ledger: Mapping[str, Any]) -> None:
        rollback_by_name = {
            item["name"]: item for item in ledger.get("rollbacks", [])
        }
        for mutation in ledger.get("mutations", []):
            rollback_record = rollback_by_name.get(mutation["name"])
            if rollback_record is None:
                raise AmbiguousRollbackState(
                    f"rollback ledger is incomplete: {mutation['name']}"
                )
            current = self.store.metadata(mutation["name"])
            if current.generation != str(rollback_record["restored_generation"]):
                raise AmbiguousRollbackState(
                    f"rolled-back object generation changed: {mutation['name']}"
                )
            self._verify_original_content(current, mutation)

    def _validate_shard_identity(
        self,
        manifest: Mapping[str, Any],
        shard_id: int,
        dataset_manifest: Mapping[str, Any],
    ) -> None:
        expected = {
            "dataset_version": self.spec.dataset_version,
            "shard_id": shard_id,
            "plan_digest": dataset_manifest.get("plan_digest"),
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise InvariantViolation(
                    f"shard {shard_id} {key} mismatch: "
                    f"expected={value!r}, actual={manifest.get(key)!r}"
                )

    def _selected_file(
        self, manifest: Mapping[str, Any], shard_id: int
    ) -> dict[str, Any]:
        files = manifest.get("files")
        if not isinstance(files, list):
            raise InvariantViolation(f"shard {shard_id} files must be a list")
        selected = [item for item in files if item.get("table") == self.spec.table]
        if len(selected) != 1 or not isinstance(selected[0], dict):
            raise InvariantViolation(
                f"shard {shard_id} must contain exactly one {self.spec.table} entry"
            )
        expected_path = f"{self.spec.table}/shard-{shard_id:05d}.parquet"
        if selected[0].get("relative_path") != expected_path:
            raise InvariantViolation(
                f"shard {shard_id} has unexpected {self.spec.table} path"
            )
        return selected[0]

    def _verify_parquet_source(
        self,
        path: Path,
        metadata: ObjectVersion,
        manifest_entry: Mapping[str, Any],
    ) -> None:
        actual_size = path.stat().st_size
        actual_md5 = _md5_file(path)
        actual_sha256 = _sha256_file(path)
        expected_size = int(manifest_entry["bytes"])
        expected_md5 = str(manifest_entry["md5_base64"])
        expected_sha256 = str(manifest_entry["sha256"])
        if (
            actual_size != metadata.size
            or actual_size != expected_size
            or (metadata.md5_base64 is not None and actual_md5 != metadata.md5_base64)
            or actual_md5 != expected_md5
            or actual_sha256 != expected_sha256
        ):
            raise ChecksumMismatchError(
                f"source Parquet checksum mismatch: {metadata.name}"
            )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_cloud_arguments(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--project",
            default=os.environ.get("GCP_PROJECT_ID"),
            required=os.environ.get("GCP_PROJECT_ID") is None,
        )
        subparser.add_argument(
            "--bucket",
            default=os.environ.get("EWALLET_DATA_BUCKET"),
            required=os.environ.get("EWALLET_DATA_BUCKET") is None,
        )

    plan_parser = subparsers.add_parser("plan", help="prepare a read-only plan")
    add_cloud_arguments(plan_parser)
    plan_parser.add_argument("--staging-dir", type=Path, required=True)
    plan_parser.add_argument("--plan-out", type=Path, required=True)

    for command in ("apply", "rollback"):
        mutation_parser = subparsers.add_parser(command)
        add_cloud_arguments(mutation_parser)
        mutation_parser.add_argument("--plan", type=Path, required=True)
        mutation_parser.add_argument("--ledger", type=Path, required=True)
        mutation_parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command in {"apply", "rollback"} and not args.execute:
        raise RemediationError(f"{args.command} requires explicit --execute")

    store = GoogleCloudStorageStore.from_project(
        project=args.project,
        bucket=args.bucket,
    )
    remediator = CanonicalSizeRemediator(
        store,
        args.bucket,
        project=args.project,
    )
    if args.command == "plan":
        result = remediator.plan(
            staging_directory=args.staging_dir,
            plan_path=args.plan_out,
        )
        summary = {
            "status": result["status"],
            "plan": str(args.plan_out),
            "plan_sha256": result["plan_sha256"],
            "projected_gain_bytes": result["projected_gain_bytes"],
            "projected_total_bytes": result["projected_total_bytes"],
        }
    elif args.command == "apply":
        result = remediator.apply(
            plan_path=args.plan,
            ledger_path=args.ledger,
            execute=args.execute,
        )
        summary = {
            "status": result["status"],
            "ledger": str(args.ledger),
            "mutation_count": len(result["mutations"]),
            "final_total_bytes": result.get("final_total_bytes"),
        }
    else:
        result = remediator.rollback(
            plan_path=args.plan,
            ledger_path=args.ledger,
            execute=args.execute,
        )
        summary = {
            "status": result["status"],
            "ledger": str(args.ledger),
            "rollback_count": len(result.get("rollbacks", [])),
        }
    print(_canonical_json_bytes(summary).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
