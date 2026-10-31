"""Thin Kafka-compatible publisher with explicit keys and no global client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from data_platform.streaming.contracts.events import TransactionEvent
from data_platform.streaming.contracts.topics import TOPICS, raw_message_key
from data_platform.streaming.messaging.serializers import serialize_event


@dataclass(frozen=True)
class RedpandaConfig:
    bootstrap_servers: str
    client_id: str = "ewallet-stream-generator-v1"
    acks: str = "all"
    enable_idempotence: bool = True


class RedpandaEventPublisher:
    def __init__(self, config: RedpandaConfig, producer_factory: Callable | None = None) -> None:
        if producer_factory is None:
            from confluent_kafka import Producer

            producer_factory = Producer
        self._producer = producer_factory(
            {
                "bootstrap.servers": config.bootstrap_servers,
                "client.id": config.client_id,
                "acks": config.acks,
                "enable.idempotence": config.enable_idempotence,
            }
        )

    def publish(self, event: TransactionEvent, delivery_callback=None) -> None:
        self.publish_payload(
            TOPICS["raw"].name,
            raw_message_key(event),
            serialize_event(event),
            delivery_callback,
        )

    def publish_payload(
        self,
        topic: str,
        key: str,
        payload: bytes | str,
        delivery_callback=None,
        partition: int | None = None,
    ) -> None:
        options = {
            "topic": topic,
            "key": key.encode("utf-8"),
            "value": payload.encode("utf-8") if isinstance(payload, str) else payload,
            "on_delivery": delivery_callback,
        }
        if partition is not None:
            options["partition"] = partition
        self._producer.produce(**options)
        self._producer.poll(0)

    def flush(self, timeout: float = 10.0) -> int:
        return self._producer.flush(timeout)
