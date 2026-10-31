"""Bounded poison-event handling using Redpanda retry/DLQ topics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json

from data_platform.streaming.contracts.events import TransactionEvent


class FailureClass(str, Enum):
    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"


@dataclass(frozen=True)
class FailureEnvelope:
    original_event: dict[str, object]
    original_event_time: str
    retry_count: int
    error_reason: str
    trace_id: str

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: str | bytes) -> "FailureEnvelope":
        return cls(**json.loads(payload))


@dataclass(frozen=True)
class FailureRoute:
    destination: str
    envelope: FailureEnvelope


def route_failure(
    event: TransactionEvent,
    reason: str,
    failure_class: FailureClass,
    retry_count: int,
    max_retries: int = 3,
) -> FailureRoute:
    if retry_count < 0:
        raise ValueError("retry_count must be non-negative")
    destination = (
        "retry"
        if failure_class == FailureClass.RETRYABLE and retry_count < max_retries
        else "dlq"
    )
    return FailureRoute(
        destination,
        FailureEnvelope(
            original_event=event.to_dict(),
            original_event_time=event.event_time.isoformat(),
            retry_count=retry_count,
            error_reason=reason,
            trace_id=event.trace_id,
        ),
    )


def route_invalid_payload(
    payload: dict[str, object],
    reason: str,
    trace_id: str,
) -> FailureRoute:
    """Route a schema-invalid/poison payload directly to the canonical DLQ."""
    raw_time = payload.get("event_time")
    return FailureRoute(
        destination="dlq",
        envelope=FailureEnvelope(
            original_event=dict(payload),
            original_event_time=str(raw_time) if raw_time is not None else "",
            retry_count=0,
            error_reason=reason,
            trace_id=trace_id,
        ),
    )
