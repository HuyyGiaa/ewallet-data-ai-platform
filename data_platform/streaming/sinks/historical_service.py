"""Immutable, run-scoped GCS writer for accepted streaming history."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from data_platform.streaming.contracts.topics import consumer_group, topic_name
from data_platform.streaming.messaging.consumer_loop import StopSignal, poll_messages


class ImmutableGcsBatchWriter:
    def __init__(self, root: str, run_id: str, client=None) -> None:
        parsed = urlparse(root)
        if parsed.scheme != "gs" or not parsed.netloc or "/lakehouse/runs/" in parsed.path:
            raise ValueError("history root must be a dedicated gs:// streaming-history prefix")
        if not run_id:
            raise ValueError("run_id is required")
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self.bucket = client.bucket(parsed.netloc)
        self.prefix = f"{parsed.path.strip('/')}/runs/{run_id}"

    def write(self, partition: int, first_offset: int, messages: list[bytes]) -> str:
        body = b"\n".join(messages) + b"\n"
        digest = hashlib.sha256(body).hexdigest()[:16]
        name = f"{self.prefix}/partition={partition:05d}/offset={first_offset:020d}-{digest}.jsonl"
        blob = self.bucket.blob(name)
        try:
            blob.upload_from_string(body, content_type="application/x-ndjson", if_generation_match=0)
        except Exception as error:
            from google.api_core.exceptions import PreconditionFailed

            if not isinstance(error, PreconditionFailed):
                raise
            blob.reload()
            expected_md5 = base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode("ascii")
            if blob.size != len(body) or blob.md5_hash != expected_md5:
                raise RuntimeError(f"immutable history object conflict: gs://{self.bucket.name}/{name}") from error
        return name


class ImmutableLocalBatchWriter:
    """File-system twin of the GCS writer, for local validation only."""

    def __init__(self, root: str, run_id: str) -> None:
        if not run_id:
            raise ValueError("run_id is required")
        self.prefix = Path(root) / "runs" / run_id

    def write(self, partition: int, first_offset: int, messages: list[bytes]) -> str:
        """Create one object; an identical replay of it is accepted."""

        body = b"\n".join(messages) + b"\n"
        digest = hashlib.sha256(body).hexdigest()[:16]
        path = self.prefix / f"partition={partition:05d}" / f"offset={first_offset:020d}-{digest}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(body)
        except FileExistsError as error:
            if path.read_bytes() != body:
                raise RuntimeError(f"immutable history object conflict: {path}") from error
        return str(path)


def history_writer(root: str, run_id: str):
    """Return the writer for a ``gs://`` root or a local ``file://`` root."""

    if root.startswith("file://"):
        return ImmutableLocalBatchWriter(urlparse(root).path, run_id)
    return ImmutableGcsBatchWriter(root, run_id)


class HistoryBatcher:
    """Buffer per partition and commit a batch only after its object exists.

    Delivery is at-least-once: a crash between the write and the commit replays the
    batch. Replayed records can land in a second object, so readers must deduplicate
    by ``transaction_id``.
    """

    def __init__(self, consumer, writer, batch_size: int, flush_interval_seconds: float, clock=time.monotonic) -> None:
        if batch_size < 1 or flush_interval_seconds <= 0:
            raise ValueError("batch size and flush interval must be positive")
        self.consumer = consumer
        self.writer = writer
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval_seconds
        self.clock = clock
        self.buffered: dict[int, list] = {}
        self.oldest: dict[int, float] = {}
        self.objects: list[str] = []
        self.records = 0

    def _flush(self, partition: int) -> None:
        batch = self.buffered.get(partition)
        if not batch:
            return
        first = batch[0]
        self.objects.append(self.writer.write(first.partition(), first.offset(), [item.value() for item in batch]))
        self.consumer.commit(message=batch[-1], asynchronous=False)
        self.records += len(batch)
        batch.clear()
        self.oldest.pop(partition, None)

    def add(self, message) -> None:
        """Buffer one message and write its partition when the batch is full."""

        partition = message.partition()
        self.buffered.setdefault(partition, []).append(message)
        self.oldest.setdefault(partition, self.clock())
        if len(self.buffered[partition]) >= self.batch_size:
            self._flush(partition)

    def flush_due(self) -> None:
        """Write partitions whose oldest record waited for the flush interval."""

        now = self.clock()
        for partition, since in list(self.oldest.items()):
            if now - since >= self.flush_interval_seconds:
                self._flush(partition)

    def flush_all(self) -> None:
        """Write everything still buffered."""

        for partition in list(self.buffered):
            self._flush(partition)


def run_history_sink(consumer, batcher: HistoryBatcher, stop: StopSignal, max_idle_seconds: float | None = None) -> dict[str, object]:
    """Copy accepted records to immutable storage until stopped or idle.

    Returns:
        Record and object counts for the run log.
    """

    try:
        for message in poll_messages(consumer, stop, max_idle_seconds=max_idle_seconds):
            if message is not None:
                batcher.add(message)
            batcher.flush_due()
    finally:
        batcher.flush_all()
    return {"records": batcher.records, "objects": len(batcher.objects)}


def main(argv=None) -> int:
    """Run the history writer as a service or a bounded verification step."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-servers", required=True)
    parser.add_argument("--history-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--namespace", help="isolated topic and group suffix")
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--flush-interval-seconds", type=float, default=30.0)
    parser.add_argument("--max-idle-seconds", type=float, help="exit after this long without records")
    args = parser.parse_args(argv)
    from confluent_kafka import Consumer

    consumer = Consumer({
        "bootstrap.servers": args.bootstrap_servers,
        "group.id": consumer_group("history", args.namespace),
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })
    consumer.subscribe([topic_name("accepted", args.namespace)])
    batcher = HistoryBatcher(
        consumer, history_writer(args.history_root, args.run_id), args.batch_size, args.flush_interval_seconds
    )
    try:
        summary = run_history_sink(consumer, batcher, StopSignal().install(), args.max_idle_seconds)
    finally:
        consumer.close()
    print(json.dumps({"service": "history", "run_id": args.run_id, **summary}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
