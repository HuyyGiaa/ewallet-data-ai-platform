"""Deterministic event generator including disorder and duplicate scenarios."""

from __future__ import annotations

import random
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

from data_platform.streaming.contracts.events import TransactionEvent


_NAMESPACE = uuid.UUID("f07b6de3-8e21-4da6-b33d-21c228f70c01")


class DeterministicScenarioGenerator:
    def __init__(self, seed: int) -> None:
        self.seed = seed

    def _id(self, kind: str, sequence: int) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{self.seed}:{kind}:{sequence}"))

    def normal(self, sequence: int, event_time: datetime, **overrides) -> TransactionEvent:
        rng = random.Random((self.seed * 1_000_003) + sequence)
        values = {
            "event_id": self._id("event", sequence),
            "transaction_id": self._id("transaction", sequence),
            "user_id": f"user-{(sequence % 17) + 1:03d}",
            "account_id": f"account-{(sequence % 23) + 1:03d}",
            "device_id": f"device-{(sequence % 11) + 1:03d}",
            "merchant_id": f"merchant-{(sequence % 13) + 1:03d}",
            "counterparty_account_id": None,
            "transaction_type": "payment",
            "amount": round(rng.uniform(10.0, 2_000.0), 2),
            "currency": "VND",
            "status": "success",
            "channel": "mobile",
            "old_balance": 10_000.0,
            "new_balance": 9_000.0,
            "event_time": event_time,
            "arrival_time": event_time,
            "schema_version": "1.0",
            "trace_id": self._id("trace", sequence),
        }
        values.update(overrides)
        return TransactionEvent(**values)

    @staticmethod
    def duplicate(event: TransactionEvent, arrival_delay: timedelta) -> TransactionEvent:
        return replace(event, arrival_time=event.arrival_time + arrival_delay)

    def out_of_order(
        self, sequence: int, arrival_time: datetime, event_delay: timedelta, **overrides
    ) -> TransactionEvent:
        return self.normal(
            sequence,
            event_time=arrival_time - event_delay,
            arrival_time=arrival_time,
            **overrides,
        )

    def late(
        self, sequence: int, arrival_time: datetime, event_delay: timedelta, **overrides
    ) -> TransactionEvent:
        return self.out_of_order(sequence, arrival_time, event_delay, **overrides)

    def invalid_payload(self, sequence: int) -> dict[str, object]:
        return {
            "event_id": self._id("invalid", sequence),
            "transaction_id": "",
            "schema_version": "1.0",
            "trace_id": self._id("trace", sequence),
            "error_fixture": "missing required identity",
        }
