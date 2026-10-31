"""Create an isolated namespace and publish one bounded replay at a target rate.

Usage (inside the bench container, see docs/streaming/flink_local_benchmark.md):

    python -m data_platform.streaming.benchmark.replay --mode functional \
        --namespace bench-func-baseline-01 --out evidence/<run>/input

``--dry-run`` writes the manifest and payload digest without a broker.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from data_platform.streaming.benchmark.dataset import (
    PERFORMANCE_RATES,
    build_payloads,
    functional_spec,
    performance_spec,
)
from data_platform.streaming.contracts.topics import TOPICS, topic_name


BENCH_TOPICS = ("raw", "retry", "dlq", "late", "accepted", "online")


def create_topics(bootstrap_servers: str, namespace: str) -> list[str]:
    """Create fresh namespaced topics; refuse to reuse any existing one."""
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
                num_partitions=TOPICS[key].partitions,
                replication_factor=1,
                config={"message.timestamp.type": "LogAppendTime"},
            )
            for key in BENCH_TOPICS
        ]
    )
    for future in futures.values():
        future.result(timeout=30)
    return names


def publish(records, bootstrap_servers: str, topic: str, target_rate: float) -> dict:
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

    started = time.perf_counter()
    for index, (key, payload) in enumerate(records):
        due = started + index / target_rate
        delay = due - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        while True:
            try:
                producer.produce(topic, key=key.encode(), value=payload.encode(), on_delivery=on_delivery)
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
    records, manifest = build_payloads(spec)
    manifest["namespace"] = args.namespace
    manifest["raw_topic"] = topic_name("raw", args.namespace)
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        if not args.skip_create_topics:
            manifest["topics_created"] = create_topics(args.bootstrap_servers, args.namespace)
        if not args.create_topics_only:
            manifest["publish"] = publish(records, args.bootstrap_servers, manifest["raw_topic"], spec.target_rate)
    (args.out / "input_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(json.dumps({k: manifest.get(k) for k in ("namespace", "emitted_records", "payload_sha256", "publish")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
