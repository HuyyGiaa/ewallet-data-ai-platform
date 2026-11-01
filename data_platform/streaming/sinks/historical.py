"""Immutable accepted-event contract for the future lakehouse writer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json

from data_platform.streaming.contracts.events import TransactionEvent


@dataclass(frozen=True)
class HistoricalRecord:
    event_id: str
    transaction_id: str
    user_id: str
    account_id: str
    device_id: str
    merchant_id: str | None
    event_time: datetime
    arrival_time: datetime
    schema_version: str
    trace_id: str
    disposition: str
    payload: dict[str, object]

    @classmethod
    def from_event(cls, event: TransactionEvent, disposition: str) -> "HistoricalRecord":
        return cls(
            event_id=event.event_id,
            transaction_id=event.transaction_id,
            user_id=event.user_id,
            account_id=event.account_id,
            device_id=event.device_id,
            merchant_id=event.merchant_id,
            event_time=event.event_time,
            arrival_time=event.arrival_time,
            schema_version=event.schema_version,
            trace_id=event.trace_id,
            disposition=disposition,
            payload=event.to_dict(),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                **self.__dict__,
                "event_time": self.event_time.isoformat(),
                "arrival_time": self.arrival_time.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
