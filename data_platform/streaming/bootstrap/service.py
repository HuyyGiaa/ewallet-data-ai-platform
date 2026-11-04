"""First-deployment Silver replay feeder with bounded-memory external sorting."""

from __future__ import annotations

import argparse
import heapq
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from data_platform.streaming.bootstrap.silver import (
    BootstrapProvenance,
    CanonicalSilverBootstrapSource,
    DeltaSilverBootstrapReader,
    SilverBootstrapAdapter,
)
from data_platform.streaming.contracts.topics import TOPICS
from data_platform.streaming.messaging.redpanda import RedpandaConfig, RedpandaEventPublisher


def _manifest(source, client=None):
    parsed = urlparse(source.manifest_uri)
    if client is None:
        from google.cloud import storage

        client = storage.Client()
    payload = json.loads(client.bucket(parsed.netloc).blob(parsed.path.lstrip("/")).download_as_text())
    return BootstrapProvenance.validate(payload, source)


def _external_order(events, directory: Path, chunk_size: int = 50_000):
    paths = []
    chunk = []
    for event in events:
        chunk.append(event)
        if len(chunk) >= chunk_size:
            paths.append(_write_chunk(chunk, directory, len(paths)))
            chunk = []
    if chunk:
        paths.append(_write_chunk(chunk, directory, len(paths)))
    streams = [path.open(encoding="utf-8") for path in paths]
    try:
        keyed = []
        for index, stream in enumerate(streams):
            line = stream.readline()
            if line:
                event = json.loads(line)
                heapq.heappush(keyed, (event["event_time"], event["transaction_id"], index, line))
        while keyed:
            _, _, index, line = heapq.heappop(keyed)
            yield line.rstrip("\n")
            next_line = streams[index].readline()
            if next_line:
                event = json.loads(next_line)
                heapq.heappush(keyed, (event["event_time"], event["transaction_id"], index, next_line))
    finally:
        for stream in streams:
            stream.close()


def _write_chunk(events, directory: Path, index: int) -> Path:
    events.sort(key=lambda event: (event.event_time, event.transaction_id))
    path = directory / f"bootstrap-{index:06d}.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        for event in events:
            stream.write(event.to_json() + "\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-servers", required=True)
    parser.add_argument("--silver-root", required=True)
    parser.add_argument("--manifest-uri", required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--cutover", required=True)
    parser.add_argument("--staging-dir")
    args = parser.parse_args(argv)
    cutover = datetime.fromisoformat(args.cutover.replace("Z", "+00:00"))
    if cutover.tzinfo is None:
        raise ValueError("cutover must be timezone-aware")
    cutover = cutover.astimezone(timezone.utc)
    source = CanonicalSilverBootstrapSource(args.silver_root, args.source_run_id, args.dataset_version, manifest_uri=args.manifest_uri)
    _manifest(source)
    reader = DeltaSilverBootstrapReader(source)
    adapter = SilverBootstrapAdapter(source)
    first_seen = adapter.device_first_seen(reader.device_rows())
    replay_start = cutover - timedelta(days=30)
    anchors = adapter.account_anchors(reader.pre_window_rows(replay_start), replay_start)
    publisher = RedpandaEventPublisher(RedpandaConfig(args.bootstrap_servers, client_id="ewallet-bootstrap-v1"))
    context = tempfile.TemporaryDirectory(dir=args.staging_dir)
    try:
        events = (adapter.to_event(row) for row in reader.transaction_rows(replay_start, cutover))
        for event_json in _external_order(events, Path(context.name)):
            event = json.loads(event_json)
            envelope = {
                "kind": "BOOTSTRAP",
                "dataset_version": source.dataset_version,
                "source_run_id": source.source_run_id,
                "cutover": cutover.isoformat(),
                "online_enabled": False,
                "device_first_seen_iso": first_seen.get(event["device_id"]).isoformat() if event["device_id"] in first_seen else None,
                "account_anchor_iso": anchors.get(event["account_id"]).isoformat() if event["account_id"] in anchors else None,
                "event": event,
            }
            publisher.publish_payload(TOPICS["raw"].name, event["transaction_id"], json.dumps(envelope, sort_keys=True, separators=(",", ":")), partition=0)
        marker = {"kind": "BOOTSTRAP_COMPLETE", "dataset_version": source.dataset_version, "source_run_id": source.source_run_id, "cutover": cutover.isoformat()}
        publisher.publish_payload(TOPICS["raw"].name, "bootstrap-control", json.dumps(marker, sort_keys=True), partition=0)
        if publisher.flush(timeout=60.0) != 0:
            raise RuntimeError("bootstrap replay stopped with undelivered records")
    finally:
        context.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
