"""Stable JSON records passed between production PyFlink operators."""

from __future__ import annotations

import json
from dataclasses import dataclass

from data_platform.streaming.contracts.events import TransactionEvent


@dataclass(frozen=True)
class RuntimeEvent:
    event: TransactionEvent | None
    route: str
    retry_count: int = 0
    reason: str = ""
    raw_payload: str = ""
    disposition: str = ""
    bootstrap: bool = False
    online_enabled: bool = True
    device_first_seen_iso: str | None = None
    account_anchor_iso: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "event": self.event.to_dict() if self.event else None,
                "route": self.route,
                "retry_count": self.retry_count,
                "reason": self.reason,
                "raw_payload": self.raw_payload,
                "disposition": self.disposition,
                "bootstrap": self.bootstrap,
                "online_enabled": self.online_enabled,
                "device_first_seen_iso": self.device_first_seen_iso,
                "account_anchor_iso": self.account_anchor_iso,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> "RuntimeEvent":
        values = json.loads(payload)
        if values["event"] is not None:
            values["event"] = TransactionEvent.from_dict(values["event"])
        return cls(**values)


@dataclass(frozen=True)
class EntityEvent:
    view_name: str
    entity_key: str
    entity_value: str
    event: TransactionEvent
    disposition: str
    bootstrap: bool
    online_enabled: bool
    retry_count: int = 0
    device_first_seen_iso: str | None = None
    account_anchor_iso: str | None = None

    @property
    def state_key(self) -> str:
        return f"{self.view_name}:{self.entity_value}"

    def to_json(self) -> str:
        return json.dumps(
            {**self.__dict__, "event": self.event.to_dict()},
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> "EntityEvent":
        values = json.loads(payload)
        values["event"] = TransactionEvent.from_dict(values["event"])
        return cls(**values)


@dataclass(frozen=True)
class OutboundRecord:
    destination: str
    key: str
    payload: str

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: str | bytes) -> "OutboundRecord":
        return cls(**json.loads(payload))
