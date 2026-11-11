"""Concrete online-topic consumer using Feast's supported push boundary.

Delivery to Redis is at-least-once. An offset is committed only after the push
returned, so a crash replays updates. Replays are safe because Feast's Redis store
skips rows that are not newer than the stored timestamp of that entity and view.

A malformed update goes to the dead-letter topic, because retrying it can never
succeed and it would block the partition. A Feast or Redis failure stops the
service without committing, so the update is retried after a restart.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Callable

from data_platform.streaming.contracts.topics import consumer_group, topic_name
from data_platform.streaming.feast.online import FeastPushWriter
from data_platform.streaming.messaging.consumer_loop import StopSignal, poll_messages
from data_platform.streaming.retry.routing import FailureEnvelope
from data_platform.streaming.sinks.online import OnlineFeatureUpdate


def parse_update(payload: bytes) -> OnlineFeatureUpdate:
    """Decode one message, raising ``ValueError`` when it is not a valid update."""

    try:
        return OnlineFeatureUpdate.from_json(payload)
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as error:
        raise ValueError(f"malformed online update: {error}") from error


def dead_letter_envelope(payload: bytes, reason: str) -> str:
    """Build the failure record kept for an update that can never be written."""

    return FailureEnvelope(
        {"raw_payload": payload.decode("utf-8", errors="replace")}, "", 0, reason, ""
    ).to_json()


def run_online_writer(
    consumer,
    writer: FeastPushWriter,
    dead_letter: Callable[[str], None],
    stop: StopSignal,
    max_idle_seconds: float | None = None,
) -> dict[str, int]:
    """Push updates to Feast until stopped or idle.

    Args:
        consumer: Kafka consumer subscribed to the online topic.
        writer: Feast push boundary.
        dead_letter: Durably stores a failure envelope before its offset is committed.
        stop: Termination flag.
        max_idle_seconds: End after this long without records; ``None`` never ends.

    Returns:
        Counts of written and dead-lettered updates.
    """

    written = rejected = 0
    for message in poll_messages(consumer, stop, max_idle_seconds=max_idle_seconds):
        if message is None:
            continue
        try:
            update = parse_update(message.value())
        except ValueError as error:
            dead_letter(dead_letter_envelope(message.value(), str(error)))
            rejected += 1
        else:
            writer.write(update)
            written += 1
        consumer.commit(message=message, asynchronous=False)
    return {"written": written, "dead_lettered": rejected}


def _require_local_boundary(
    connection_string: str, registry_uri: str, offline_root: str, namespace: str | None
) -> None:
    """Confine the legacy local runner to loopback and its campaign directory."""

    from ai_platform.features.feast.online_operator import _redis_connection_parts

    host, _port, _database, _options = _redis_connection_parts(connection_string)
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("local verification only accepts a loopback Redis")
    if not namespace or re.fullmatch(r"verify-[a-z0-9][a-z0-9-]{0,46}", namespace) is None:
        raise ValueError("invalid namespace for local verification")
    registry = Path(registry_uri)
    if not registry.is_absolute() or registry.name != "registry.db":
        raise ValueError("local verification requires an absolute run-scoped registry")
    expected = (
        registry.parent
        / "offline-features"
        / "campaigns"
        / namespace
        / "runs"
        / namespace
        / "offline"
    ).resolve()
    if not Path(offline_root).is_absolute() or Path(offline_root).resolve() != expected:
        raise ValueError("local offline Feature Store path differs from the namespace")


def main(argv=None) -> int:
    """Run the Feast writer as a service or a bounded verification step."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-servers", required=True)
    parser.add_argument("--feast-repo", required=True)
    parser.add_argument("--namespace", help="isolated topic and group suffix")
    parser.add_argument("--max-idle-seconds", type=float, help="exit after this long without records")
    args = parser.parse_args(argv)
    from confluent_kafka import Consumer, Producer
    from feast import FeatureStore

    from ai_platform.features.feast.registry_mirror import mirror_from_environment
    from ai_platform.features.feast.online_operator import (
        require_online_store_capacity,
        require_online_store_ownership,
    )

    registry_uri = os.environ.get("EWALLET_FEAST_REGISTRY", "")
    connection_string = os.environ["EWALLET_REDIS_CONNECTION_STRING"]
    mirror_from_environment()
    if registry_uri.startswith("gs://"):
        require_online_store_ownership(connection_string, registry_uri)
        require_online_store_capacity(connection_string)
    else:
        _require_local_boundary(
            connection_string,
            registry_uri,
            os.environ.get("EWALLET_FEAST_OFFLINE_DATA_ROOT", ""),
            args.namespace,
        )
    consumer = Consumer({
        "bootstrap.servers": args.bootstrap_servers,
        "group.id": consumer_group("feast", args.namespace),
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })
    consumer.subscribe([topic_name("online", args.namespace)])
    producer = Producer({"bootstrap.servers": args.bootstrap_servers})
    dlq_topic = topic_name("dlq", args.namespace)

    def dead_letter(envelope: str) -> None:
        producer.produce(dlq_topic, envelope.encode("utf-8"))
        if producer.flush(30) != 0:
            raise RuntimeError("dead-letter record was not delivered")

    writer = FeastPushWriter(FeatureStore(repo_path=args.feast_repo))
    try:
        summary = run_online_writer(
            consumer, writer, dead_letter, StopSignal().install(), args.max_idle_seconds
        )
    finally:
        consumer.close()
    print(json.dumps({"service": "feast-online", **summary}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
