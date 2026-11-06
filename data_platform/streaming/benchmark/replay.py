"""Create an isolated namespace and publish one bounded replay at a target rate.

Usage (inside the bench container, see docs/streaming/flink_local_benchmark.md):

    python -m data_platform.streaming.benchmark.replay --mode functional \
        --namespace bench-func-baseline-01 --out evidence/<run>/input

``--dry-run`` writes the manifest and payload digest without a broker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from data_platform.streaming.benchmark.dataset import (
    PERFORMANCE_RATES,
    build_payloads,
    functional_spec,
    performance_spec,
)
from data_platform.streaming.contracts.topics import TOPICS, topic_name
from data_platform.streaming.contracts.events import TransactionEvent


BENCH_TOPICS = ("raw", "retry", "dlq", "late", "accepted", "online")
INITIAL_WATERMARK_MS = -(2**63)
MAX_OUT_OF_ORDERNESS_MS = 120_000
ALLOWED_LATENESS_MS = 600_000


def instrument_records(records) -> tuple[list[tuple[str, str]], int]:
    """Wrap valid benchmark events with a deterministic pre-event watermark."""
    result, max_event_ms = [], None
    for key, payload in records:
        try:
            event = TransactionEvent.from_json(payload)
        except (KeyError, TypeError, ValueError):
            result.append((key, payload))
            continue
        event_ms = int(event.event_time.timestamp() * 1000)
        logical_watermark = (
            INITIAL_WATERMARK_MS
            if max_event_ms is None
            else max_event_ms - MAX_OUT_OF_ORDERNESS_MS - 1
        )
        wrapped = json.dumps(
            {
                "kind": "BENCHMARK_EVENT",
                "event": event.to_dict(),
                "logical_watermark_ms": logical_watermark,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        result.append((key, wrapped))
        max_event_ms = event_ms if max_event_ms is None else max(max_event_ms, event_ms)
    if max_event_ms is None:
        raise ValueError("benchmark dataset contains no valid events")
    return result, max_event_ms


def payload_digest(records) -> str:
    digest = hashlib.sha256()
    for key, payload in records:
        digest.update(key.encode() + b"\0" + payload.encode() + b"\n")
    return digest.hexdigest()


def create_topics(bootstrap_servers: str, namespace: str, partitions: int | None = None) -> list[str]:
    """Create fresh namespaced topics; refuse to reuse any existing one.

    ``partitions`` overrides the production count, for runs that need a single
    ordered split.
    """
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": bootstrap_servers})
    existing = set(admin.list_topics(timeout=10).topics)
    names = [topic_name(key, namespace) for key in BENCH_TOPICS]
    reused = sorted(set(names) & existing)
    if reused:
        raise RuntimeError(f"namespace already used (warm state risk): {reused}")
    # LogAppendTime gives every record a broker-clock timestamp, which is what
    # the raw -> accepted processing-latency measurement compares.
    futures = admin.create_topics(
        [
            NewTopic(
                topic_name(key, namespace),
                num_partitions=partitions or TOPICS[key].partitions,
                replication_factor=1,
                config={"message.timestamp.type": "LogAppendTime"},
            )
            for key in BENCH_TOPICS
        ]
    )
    for future in futures.values():
        future.result(timeout=30)
    return names


def publish(
    records, bootstrap_servers: str, topic: str, target_rate: float, *, partition: int | None = None
) -> dict:
    from confluent_kafka import Producer

    producer = Producer(
        {
            "bootstrap.servers": bootstrap_servers,
            "client.id": "ewallet-bench-replay",
            "acks": "all",
            "enable.idempotence": True,
            "linger.ms": 5,
        }
    )
    failures: list[str] = []

    def on_delivery(error, _message) -> None:
        if error is not None:
            failures.append(str(error))

    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    for index, (key, payload) in enumerate(records):
        due = started + index / target_rate
        delay = due - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        while True:
            try:
                routing = {} if partition is None else {"partition": partition}
                producer.produce(
                    topic, key=key.encode(), value=payload.encode(),
                    on_delivery=on_delivery, **routing,
                )
                break
            except BufferError:
                producer.poll(0.05)
        producer.poll(0)
    sent = time.perf_counter()
    remaining = producer.flush(60)
    acked = time.perf_counter()
    if remaining or failures:
        raise RuntimeError(f"replay incomplete: undelivered={remaining}, failures={failures[:5]}")
    count = len(records)
    send_seconds = sent - started
    return {
        "target_rate_eps": target_rate,
        "records_published": count,
        "send_seconds": round(send_seconds, 3),
        "acked_seconds": round(acked - started, 3),
        # Measured, not assumed: the generator may fall behind the target.
        "achieved_send_rate_eps": round(count / send_seconds, 2) if send_seconds > 0 else None,
        "achieved_acked_rate_eps": round(count / (acked - started), 2),
        "fixed_partition": partition,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def publish_drain_markers(
    bootstrap_servers: str, topic: str, max_event_ms: int, *, partitions: list[int]
) -> dict:
    """Advance every active benchmark partition beyond all feature timers."""
    from confluent_kafka import Producer

    marker_ms = max_event_ms + ALLOWED_LATENESS_MS + MAX_OUT_OF_ORDERNESS_MS + 1_000
    producer = Producer({"bootstrap.servers": bootstrap_servers, "acks": "all"})
    payload = json.dumps(
        {"kind": "BENCHMARK_WATERMARK", "event_time_ms": marker_ms},
        sort_keys=True,
        separators=(",", ":"),
    )
    for partition in partitions:
        producer.produce(
            topic,
            key=f"benchmark-watermark-{partition}".encode(),
            value=payload.encode(),
            partition=partition,
        )
    remaining = producer.flush(60)
    if remaining:
        raise RuntimeError(f"drain markers incomplete: undelivered={remaining}")
    return {
        "marker_event_time_ms": marker_ms,
        "partitions": partitions,
        "records_published": len(partitions),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--mode", choices=("functional", "performance"), required=True)
    result.add_argument("--rate", type=float, help=f"performance target rate, one of {PERFORMANCE_RATES}")
    result.add_argument("--duration-seconds", type=int, default=120)
    result.add_argument("--namespace", required=True)
    result.add_argument("--bootstrap-servers", default="redpanda:29092")
    result.add_argument("--out", type=Path, required=True)
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--create-topics-only", action="store_true")
    result.add_argument("--skip-create-topics", action="store_true")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.mode == "functional":
        spec = functional_spec()
    else:
        if args.rate is None:
            raise SystemExit("--rate is required in performance mode")
        spec = performance_spec(args.rate, args.duration_seconds)
    logical_records, manifest = build_payloads(spec)
    records, max_event_ms = instrument_records(logical_records)
    manifest["transport_payload_sha256"] = payload_digest(records)
    manifest["namespace"] = args.namespace
    manifest["raw_topic"] = topic_name("raw", args.namespace)
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        if not args.skip_create_topics:
            manifest["topics_created"] = create_topics(args.bootstrap_servers, args.namespace)
        if not args.create_topics_only:
            # Functional correctness must not depend on Kafka's key-to-partition
            # assignment or on source parallelism.  One ordered partition gives
            # baseline and optimized the exact same watermark progression.  Perf
            # runs remain distributed so they can exercise all source subtasks.
            partition = 0 if args.mode == "functional" else None
            manifest["publish"] = publish(
                records,
                args.bootstrap_servers,
                manifest["raw_topic"],
                spec.target_rate,
                partition=partition,
            )
            partitions = list(range(TOPICS["raw"].partitions))
            manifest["drain"] = publish_drain_markers(
                args.bootstrap_servers,
                manifest["raw_topic"],
                max_event_ms,
                partitions=partitions,
            )
            if args.mode == "performance":
                speedup = spec.event_interval_seconds * spec.target_rate
                horizon_s = (MAX_OUT_OF_ORDERNESS_MS + ALLOWED_LATENESS_MS) / 1000
                warmup_s = horizon_s / speedup
                manifest["phases"] = {
                    "event_time_speedup": speedup,
                    "warmup_seconds": warmup_s,
                    "steady_state_seconds": max(0.0, args.duration_seconds - warmup_s),
                    "drain": "partition-wide watermark markers after producer completion",
                }
    (args.out / "input_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(json.dumps({k: manifest.get(k) for k in ("namespace", "emitted_records", "payload_sha256", "publish")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
