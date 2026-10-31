"""Redpanda topology and stable partition keys."""

from __future__ import annotations

from dataclasses import dataclass

from .events import TransactionEvent


@dataclass(frozen=True)
class TopicContract:
    name: str
    partitions: int
    retention_hours: int
    purpose: str


TOPICS = {
    "raw": TopicContract("ewallet.transactions.raw.v1", 12, 168, "canonical input"),
    "retry": TopicContract("ewallet.transactions.retry.v1", 12, 72, "bounded retry"),
    "dlq": TopicContract(
        "ewallet.transactions.dlq.v1", 6, 720, "invalid/retry-exhausted/poison"
    ),
    "late": TopicContract(
        "ewallet.transactions.late.v1", 12, 720, "valid too-late reconciliation"
    ),
    "accepted": TopicContract("ewallet.transactions.accepted.v1", 12, 720, "historical sink input"),
    "online": TopicContract("ewallet.features.online.v1", 12, 72, "Feast online updates"),
}

CONSUMER_GROUPS = {
    "flink": "ewallet-block-a-flink-v1",
    "history": "ewallet-block-a-history-v1",
    "reconciliation": "ewallet-block-a-late-reconciliation-v1",
    "feast": "ewallet-block-a-feast-online-v1",
}


def raw_message_key(event: TransactionEvent) -> str:
    """Co-locate retries/duplicates before Flink's entity fan-out."""
    return event.transaction_id


def entity_rekey(event: TransactionEvent) -> dict[str, str]:
    keys = {
        "user_behavior": f"user:{event.user_id}",
        "account_behavior": f"account:{event.account_id}",
        "device_behavior": f"device:{event.device_id}",
    }
    if event.transaction_type == "payment" and event.merchant_id:
        keys["merchant_behavior"] = f"merchant:{event.merchant_id}"
    return keys


def topic_name(key: str, namespace: str | None = None) -> str:
    """Benchmark runs suffix every topic so they never share production names."""
    base = TOPICS[key].name
    return base if namespace is None else f"{base}.{namespace}"


def consumer_group(key: str, namespace: str | None = None) -> str:
    base = CONSUMER_GROUPS[key]
    return base if namespace is None else f"{base}.{namespace}"
