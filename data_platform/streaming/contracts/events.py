"""Transaction envelope consumed by the existing Flink Block A input parser.

The envelope keeps streaming metadata separate from the current offline
transaction record.  ``to_offline_record`` and ``from_offline_record`` are the
explicit compatibility boundary for physical transaction schemas V1 and V2.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from data_platform.generation.src.fintech_schema import (
    Channel,
    TransactionStatus,
    TransactionType,
    validate_balance,
)


UTC = timezone.utc
STREAMING_SCHEMA_VERSION = "1.0"
OFFLINE_SCHEMA_V1 = "transactions_v1"
OFFLINE_SCHEMA_V2 = "transactions_v2"
_TYPES = {item.value for item in TransactionType}
_STATUSES = {item.value for item in TransactionStatus}
_CHANNELS = {item.value for item in Channel} | {"UNKNOWN"}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("streaming timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _offline_utc(value: datetime) -> datetime:
    """Interpret the offline generator's timezone-naive wall clock as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
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
        for name in (
            "event_id",
            "transaction_id",
            "user_id",
            "account_id",
            "device_id",
            "trace_id",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")
        if self.transaction_type not in _TYPES:
            raise ValueError(f"unsupported transaction_type: {self.transaction_type}")
        if self.status not in _STATUSES:
            raise ValueError(f"unsupported status: {self.status}")
        if self.channel not in _CHANNELS:
            raise ValueError(f"unsupported channel: {self.channel}")
        if self.currency != "VND":
            raise ValueError("currency must be VND")
        if self.schema_version != STREAMING_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {STREAMING_SCHEMA_VERSION}"
            )
        if self.amount <= 0:
            raise ValueError("amount must be positive")
        if min(self.old_balance, self.new_balance) < 0:
            raise ValueError("balances must be non-negative")
        if self.transaction_type == TransactionType.PAYMENT.value and not self.merchant_id:
            raise ValueError("payment requires merchant_id")
        if (
            self.transaction_type == TransactionType.TRANSFER.value
            and not self.counterparty_account_id
        ):
            raise ValueError("transfer requires counterparty_account_id")
        if self.status == TransactionStatus.SUCCESS.value and not validate_balance(
            self.transaction_type, self.old_balance, self.new_balance, self.amount
        ):
            raise ValueError("successful transaction has inconsistent balance semantics")
        object.__setattr__(self, "event_time", _utc(self.event_time))
        object.__setattr__(self, "arrival_time", _utc(self.arrival_time))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["event_time"] = self.event_time.isoformat()
        payload["arrival_time"] = self.arrival_time.isoformat()
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def to_offline_record(self) -> dict[str, Any]:
        """Return the current V2 transaction shape without streaming metadata."""
        return {
            "transaction_id": self.transaction_id,
            "account_id": self.account_id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "type": self.transaction_type,
            "amount": self.amount,
            "currency": self.currency,
            "status": self.status,
            "channel": self.channel,
            "old_balance": self.old_balance,
            "new_balance": self.new_balance,
            "merchant_id": self.merchant_id,
            "counterparty_account_id": self.counterparty_account_id,
            "timestamp": self.event_time,
            "ingested_at": self.arrival_time,
        }

    @classmethod
    def from_offline_record(
        cls,
        payload: Mapping[str, Any],
        *,
        event_id: str,
        trace_id: str,
    ) -> "TransactionEvent":
        """Adapt either offline V1 (no channel) or V2 into the Flink envelope."""
        values = dict(payload)
        return cls(
            event_id=event_id,
            transaction_id=str(values["transaction_id"]),
            user_id=str(values["user_id"]),
            account_id=str(values["account_id"]),
            device_id=str(values["device_id"]),
            merchant_id=values.get("merchant_id"),
            counterparty_account_id=values.get("counterparty_account_id"),
            transaction_type=str(values["type"]),
            amount=float(values["amount"]),
            currency=str(values["currency"]),
            status=str(values["status"]),
            channel=str(values.get("channel") or "UNKNOWN"),
            old_balance=float(values["old_balance"]),
            new_balance=float(values["new_balance"]),
            event_time=_offline_utc(values["timestamp"]),
            arrival_time=_offline_utc(values["ingested_at"]),
            schema_version=STREAMING_SCHEMA_VERSION,
            trace_id=trace_id,
        )

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
