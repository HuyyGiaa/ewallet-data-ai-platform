"""Canonical transaction event shared by generator, Redpanda, and Flink."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


UTC = timezone.utc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("streaming timestamps must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class TransactionEvent:
    event_id: str
    transaction_id: str
    user_id: str
    account_id: str
    device_id: str
    merchant_id: str | None
    counterparty_account_id: str | None
    transaction_type: str
    amount: float
    currency: str
    status: str
    channel: str
    old_balance: float
    new_balance: float
    event_time: datetime
    arrival_time: datetime
    schema_version: str
    trace_id: str

    def __post_init__(self) -> None:
        for name in ("event_id", "transaction_id", "user_id", "account_id", "device_id", "trace_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")
        if self.amount < 0:
            raise ValueError("amount must be non-negative")
        if self.transaction_type == "payment" and not self.merchant_id:
            raise ValueError("payment requires merchant_id")
        object.__setattr__(self, "event_time", _utc(self.event_time))
        object.__setattr__(self, "arrival_time", _utc(self.arrival_time))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["event_time"] = self.event_time.isoformat()
        payload["arrival_time"] = self.arrival_time.isoformat()
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TransactionEvent":
        values = dict(payload)
        required = set(cls.__dataclass_fields__)
        missing = required - set(values)
        unknown = set(values) - required
        if missing or unknown:
            raise ValueError(
                f"invalid event fields; missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        try:
            values["event_time"] = datetime.fromisoformat(str(values["event_time"]))
            values["arrival_time"] = datetime.fromisoformat(str(values["arrival_time"]))
        except ValueError as error:
            raise ValueError("event_time and arrival_time must be ISO-8601") from error
        return cls(**values)

    @classmethod
    def from_json(cls, payload: str | bytes) -> "TransactionEvent":
        return cls.from_dict(json.loads(payload))
