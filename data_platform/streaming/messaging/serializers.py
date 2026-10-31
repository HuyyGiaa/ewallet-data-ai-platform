"""Versioned serializers kept separate from transport code."""

from __future__ import annotations

from data_platform.streaming.contracts.events import TransactionEvent


def serialize_event(event: TransactionEvent) -> bytes:
    return event.to_json().encode("utf-8")


def deserialize_event(payload: bytes | str) -> TransactionEvent:
    return TransactionEvent.from_json(payload)
