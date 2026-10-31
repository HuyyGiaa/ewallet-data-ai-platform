"""Envelope for valid events that arrived too late to mutate online state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from data_platform.streaming.contracts.events import TransactionEvent


@dataclass(frozen=True)
class LateEventEnvelope:
    event_id: str
    transaction_id: str
    event_time: datetime
    arrival_time: datetime
    observed_watermark: datetime | None
    schema_version: str
    trace_id: str
    disposition: str
    reconciliation_reason: str
    original_event: dict[str, object]

    @classmethod
    def from_event(
        cls,
        event: TransactionEvent,
        observed_watermark: datetime | None,
        reason: str,
    ) -> "LateEventEnvelope":
        return cls(
            event_id=event.event_id,
            transaction_id=event.transaction_id,
            event_time=event.event_time,
            arrival_time=event.arrival_time,
            observed_watermark=observed_watermark,
            schema_version=event.schema_version,
            trace_id=event.trace_id,
            disposition="TOO_LATE",
            reconciliation_reason=reason,
            original_event=event.to_dict(),
        )
