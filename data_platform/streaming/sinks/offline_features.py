"""Persist complete streaming FeatureView updates as immutable Feast Parquet rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import numbers
import os
from datetime import timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.parquet as pq

from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.streaming.contracts.topics import consumer_group, topic_name
from data_platform.streaming.messaging.consumer_loop import StopSignal, poll_messages
from data_platform.streaming.sinks.online import OnlineFeatureUpdate


class ImmutableStore(Protocol):
    """Create-only object storage used by local and GCS implementations."""

    def put(self, name: str, body: bytes, content_type: str) -> tuple[str, str, str | None]:
        """Return ``(uri, CREATED|REUSED, local_path)`` or raise on conflict."""


class LocalImmutableStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, name: str, body: bytes, content_type: str) -> tuple[str, str, str | None]:
        del content_type
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(body)
            status = "CREATED"
        except FileExistsError as error:
            if path.read_bytes() != body:
                raise RuntimeError(f"immutable offline feature conflict: {path}") from error
            status = "REUSED"
        return path.as_uri(), status, str(path)


class GcsImmutableStore:
    def __init__(self, bucket: str, prefix: str, client=None) -> None:
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self.bucket = client.bucket(bucket)
        self.prefix = prefix.strip("/")

    def put(self, name: str, body: bytes, content_type: str) -> tuple[str, str, str | None]:
        object_name = f"{self.prefix}/{name}" if self.prefix else name
        blob = self.bucket.blob(object_name)
        try:
            blob.upload_from_string(body, content_type=content_type, if_generation_match=0)
            status = "CREATED"
        except Exception as error:
            from google.api_core.exceptions import PreconditionFailed

            if not isinstance(error, PreconditionFailed):
                raise
            if blob.download_as_bytes() != body:
                raise RuntimeError(
                    f"immutable offline feature conflict: gs://{self.bucket.name}/{object_name}"
                ) from error
            status = "REUSED"
        return f"gs://{self.bucket.name}/{object_name}", status, None


def _store(root: str) -> ImmutableStore:
    parsed = urlparse(root)
    if parsed.scheme == "file" and parsed.path:
        return LocalImmutableStore(Path(parsed.path))
    if parsed.scheme == "gs" and parsed.netloc:
        return GcsImmutableStore(parsed.netloc, parsed.path)
    raise ValueError("offline feature root must be file:// or gs://")


def _identifier(value: str, name: str) -> str:
    if not value or len(value) > 80 or any(not (char.isalnum() or char in "-_.") for char in value):
        raise ValueError(f"invalid {name}")
    return value


def _row_table(update: OnlineFeatureUpdate) -> pa.Table:
    spec = FEATURE_SPECS[update.view_name]
    timestamp = update.feature_timestamp
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("feature_timestamp must be UTC-aware")
    timestamp = timestamp.astimezone(timezone.utc)
    if not update.entity_value:
        raise ValueError("entity_value is required")
    integer = set(spec["integer_features"])
    fields = [pa.field(str(spec["entity_key"]), pa.string(), nullable=False)]
    arrays: list[pa.Array] = [pa.array([update.entity_value], type=pa.string())]
    fields.append(pa.field("event_timestamp", pa.timestamp("us", tz="UTC"), nullable=False))
    arrays.append(pa.array([timestamp], type=pa.timestamp("us", tz="UTC")))
    for feature in spec["features"]:
        value = update.values[feature]
        if feature in integer:
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, numbers.Integral)
            ):
                raise ValueError(f"{feature} must be an integer or null")
            field_type = pa.int64()
            normalized = None if value is None else int(value)
        else:
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, numbers.Real)
            ):
                raise ValueError(f"{feature} must be numeric or null")
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{feature} must be finite or null")
            field_type = pa.float64()
            normalized = None if value is None else float(value)
        fields.append(pa.field(feature, field_type, nullable=True))
        arrays.append(pa.array([normalized], type=field_type))
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def _parquet_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(
        table,
        sink,
        compression="zstd",
        version="2.6",
        coerce_timestamps="us",
        allow_truncated_timestamps=False,
        write_statistics=True,
    )
    return sink.getvalue().to_pybytes()


def logical_feature_row_id(
    view_name: str,
    entity_key: str,
    entity_value: str,
    feature_timestamp,
) -> str:
    """Return the immutable identity shared by the writer and verifier."""

    if feature_timestamp.tzinfo is None or feature_timestamp.utcoffset() is None:
        raise ValueError("feature_timestamp must be UTC-aware")
    timestamp = feature_timestamp.astimezone(timezone.utc).isoformat()
    logical = "\0".join((view_name, entity_key, entity_value, timestamp))
    return hashlib.sha256(logical.encode("utf-8")).hexdigest()


def logical_row_id(update: OnlineFeatureUpdate) -> str:
    """Return the deterministic row identity for a complete update."""

    return logical_feature_row_id(
        update.view_name,
        update.entity_key,
        update.entity_value,
        update.feature_timestamp,
    )


class OfflineFeatureObjectWriter:
    """Write one immutable logical row and one Kafka-offset provenance manifest."""

    def __init__(
        self,
        root: str,
        campaign_id: str,
        run_id: str,
        store: ImmutableStore | None = None,
    ) -> None:
        self.campaign_id = _identifier(campaign_id, "campaign_id")
        self.run_id = _identifier(run_id, "run_id")
        self.store = store or _store(root)
        self.prefix = f"campaigns/{self.campaign_id}/runs/{self.run_id}"

    @property
    def offline_root(self) -> str:
        return f"{self.prefix}/offline"

    def write(
        self,
        update: OnlineFeatureUpdate,
        topic: str,
        partition: int,
        offset: int,
    ) -> dict[str, object]:
        """Persist one complete update; identical recovery is accepted."""

        if partition < 0 or offset < 0 or not topic:
            raise ValueError("Kafka topic, partition and offset are required")
        table = _row_table(update)
        timestamp = update.feature_timestamp.astimezone(timezone.utc).isoformat()
        logical_id = logical_row_id(update)
        parquet = _parquet_bytes(table)
        parquet_sha = hashlib.sha256(parquet).hexdigest()
        spec = FEATURE_SPECS[update.view_name]
        row_name = f"{self.prefix}/offline/{spec['table']}/row-{logical_id}.parquet"
        parquet_uri, parquet_write, parquet_path = self.store.put(
            row_name, parquet, "application/vnd.apache.parquet"
        )
        manifest = {
            "schema_version": "streaming-offline-feature-row-v1",
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "view_name": update.view_name,
            "entity_key": update.entity_key,
            "entity_value": update.entity_value,
            "event_timestamp": timestamp,
            "source_event_id": update.source_event_id,
            "logical_row_id": logical_id,
            "parquet_uri": parquet_uri,
            "parquet_sha256": parquet_sha,
            "parquet_bytes": len(parquet),
            "kafka": {"topic": topic, "partition": partition, "offset": offset},
        }
        manifest_body = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        manifest_name = (
            f"{self.prefix}/_provenance/{update.view_name}/partition={partition:05d}/"
            f"offset={offset:020d}.json"
        )
        manifest_uri, manifest_write, manifest_path = self.store.put(
            manifest_name, manifest_body, "application/json"
        )
        return {
            **manifest,
            "manifest_uri": manifest_uri,
            "parquet_write": parquet_write,
            "manifest_write": manifest_write,
            "parquet_path": parquet_path,
            "manifest_path": manifest_path,
        }


def run_offline_feature_writer(
    consumer,
    writer: OfflineFeatureObjectWriter,
    stop: StopSignal,
    topic: str,
    max_idle_seconds: float | None = None,
) -> dict[str, object]:
    """Persist updates and commit each offset only after row and manifest exist."""

    writes = []
    for message in poll_messages(consumer, stop, max_idle_seconds=max_idle_seconds):
        if message is None:
            continue
        try:
            update = OnlineFeatureUpdate.from_json(message.value())
        except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as error:
            raise ValueError(f"malformed complete feature update: {error}") from error
        result = writer.write(update, topic, message.partition(), message.offset())
        consumer.commit(message=message, asynchronous=False)
        writes.append(result)
    return {
        "rows_persisted": len(writes),
        "objects_created": sum(
            item[name] == "CREATED"
            for item in writes
            for name in ("parquet_write", "manifest_write")
        ),
        "objects_reused": sum(
            item[name] == "REUSED"
            for item in writes
            for name in ("parquet_write", "manifest_write")
        ),
        "writes": writes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap-servers", required=True)
    parser.add_argument("--offline-root", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--namespace")
    parser.add_argument("--max-idle-seconds", type=float)
    args = parser.parse_args(argv)
    if args.offline_root.startswith("gs://"):
        from data_platform.config.dataset_identity import validate_feature_store_storage

        required = {
            name: os.environ.get(name, "")
            for name in (
                "EWALLET_DATASET_VERSION",
                "EWALLET_FEAST_OFFLINE_DATA_ROOT",
                "EWALLET_HISTORY_ROOT",
                "EWALLET_FEAST_REGISTRY",
            )
        }
        validate_feature_store_storage(
            dataset_version=required["EWALLET_DATASET_VERSION"],
            offline_feature_root=args.offline_root,
            feast_offline_data_root=required["EWALLET_FEAST_OFFLINE_DATA_ROOT"],
            history_root=required["EWALLET_HISTORY_ROOT"],
            online_registry_uri=required["EWALLET_FEAST_REGISTRY"],
            campaign_id=args.campaign_id,
            run_id=args.run_id,
        )
    from confluent_kafka import Consumer

    topic = topic_name("online", args.namespace)
    consumer = Consumer({
        "bootstrap.servers": args.bootstrap_servers,
        "group.id": consumer_group("offline", args.namespace),
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })
    consumer.subscribe([topic])
    writer = OfflineFeatureObjectWriter(
        args.offline_root, args.campaign_id, args.run_id
    )
    try:
        summary = run_offline_feature_writer(
            consumer,
            writer,
            StopSignal().install(),
            topic,
            args.max_idle_seconds,
        )
    finally:
        consumer.close()
    print(json.dumps({
        "service": "feast-offline",
        "campaign_id": args.campaign_id,
        "run_id": args.run_id,
        "offline_root": writer.offline_root,
        **summary,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
