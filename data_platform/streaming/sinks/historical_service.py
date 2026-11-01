"""Immutable, run-scoped GCS writer for accepted streaming history."""

from __future__ import annotations

import argparse
import base64
import hashlib
from urllib.parse import urlparse

from data_platform.streaming.contracts.topics import CONSUMER_GROUPS, TOPICS


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-servers", required=True)
    parser.add_argument("--history-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--batch-size", type=int, default=250)
    args = parser.parse_args(argv)
    from confluent_kafka import Consumer

    consumer = Consumer({"bootstrap.servers": args.bootstrap_servers, "group.id": CONSUMER_GROUPS["history"], "enable.auto.commit": False, "auto.offset.reset": "earliest"})
    consumer.subscribe([TOPICS["accepted"].name])
    writer = ImmutableGcsBatchWriter(args.history_root, args.run_id)
    buffered = {}
    try:
        while True:
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                raise RuntimeError(str(message.error()))
            batch = buffered.setdefault(message.partition(), [])
            batch.append(message)
            if len(batch) >= args.batch_size:
                first = batch[0]
                writer.write(first.partition(), first.offset(), [item.value() for item in batch])
                consumer.commit(message=batch[-1], asynchronous=False)
                batch.clear()
    finally:
        for batch in buffered.values():
            if batch:
                first = batch[0]
                writer.write(first.partition(), first.offset(), [item.value() for item in batch])
                consumer.commit(message=batch[-1], asynchronous=False)
        consumer.close()


if __name__ == "__main__":
    raise SystemExit(main())
