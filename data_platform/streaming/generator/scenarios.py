"""Deterministic, referentially valid streaming scenarios.

This module modernizes the generator recovered from ``codex/streaming-block-a``
at 3fa7c483.  It deliberately has no broker or Flink dependency.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from data_platform.generation.src.fintech_schema import TYPE_WEIGHTS, TransactionType
from data_platform.streaming.contracts.events import (
    STREAMING_SCHEMA_VERSION,
    TransactionEvent,
)


_NAMESPACE = uuid.UUID("f07b6de3-8e21-4da6-b33d-21c228f70c01")
UTC = timezone.utc


@dataclass(frozen=True)
class GeneratorProfile:
    seed: int = 42
    late_arrival_rate: float = 0.12
    duplicate_rate: float = 0.015
    burst_multiplier: int = 30
    realistic_events_per_second: float = 10.0

    def __post_init__(self) -> None:
        if not 0 <= self.late_arrival_rate < 1:
            raise ValueError("late_arrival_rate must be in [0, 1)")
        if not 0 <= self.duplicate_rate < 1:
            raise ValueError("duplicate_rate must be in [0, 1)")
        if self.burst_multiplier < 1 or self.realistic_events_per_second <= 0:
            raise ValueError("emission rates must be positive")

    def emission_rate(self, *, stress: bool = False) -> float:
        """Keep logical behavior independent from optional load-test pressure."""
        multiplier = self.burst_multiplier if stress else 1
        return self.realistic_events_per_second * multiplier


@dataclass(frozen=True)
class EntityCatalog:
    account_to_user: dict[str, str]
    device_to_user: dict[str, str]
    merchant_ids: tuple[str, ...]

    @property
    def account_ids(self) -> tuple[str, ...]:
        return tuple(self.account_to_user)

    def accounts_for(self, user_id: str) -> tuple[str, ...]:
        return tuple(
            account_id
            for account_id, owner in self.account_to_user.items()
            if owner == user_id
        )

    def devices_for(self, user_id: str) -> tuple[str, ...]:
        return tuple(
            device_id for device_id, owner in self.device_to_user.items() if owner == user_id
        )

    @classmethod
    def deterministic(cls, seed: int = 42, users: int = 8) -> "EntityCatalog":
        def identifier(kind: str, index: int) -> str:
            return str(uuid.uuid5(_NAMESPACE, f"catalog:{seed}:{kind}:{index}"))

        account_to_user: dict[str, str] = {}
        device_to_user: dict[str, str] = {}
        for index in range(users):
            user_id = identifier("user", index)
            # Two accounts and one current device per user exercise relationships
            # without reconstructing the full historical source dataset.
            for offset in range(2):
                account_to_user[identifier("account", index * 2 + offset)] = user_id
            device_to_user[identifier("device", index)] = user_id
        merchants = tuple(identifier("merchant", index) for index in range(6))
        return cls(account_to_user, device_to_user, merchants)

    def validate(self, event: TransactionEvent) -> None:
        if self.account_to_user.get(event.account_id) != event.user_id:
            raise ValueError("account does not belong to event user")
        if self.device_to_user.get(event.device_id) != event.user_id:
            raise ValueError("device does not belong to event user")
        if event.merchant_id is not None and event.merchant_id not in self.merchant_ids:
            raise ValueError("merchant does not exist in catalog")
        if (
            event.counterparty_account_id is not None
            and event.counterparty_account_id not in self.account_to_user
        ):
            raise ValueError("counterparty account does not exist in catalog")
        if event.counterparty_account_id == event.account_id:
            raise ValueError("transfer counterparty must differ from source account")


class DeterministicScenarioGenerator:
    def __init__(
        self,
        seed: int = 42,
        *,
        catalog: EntityCatalog | None = None,
        profile: GeneratorProfile | None = None,
    ) -> None:
        self.seed = seed
        self.catalog = catalog or EntityCatalog.deterministic(seed)
        self.profile = profile or GeneratorProfile(seed=seed)

    def _id(self, kind: str, sequence: int) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{self.seed}:{kind}:{sequence}"))

    def _rng(self, sequence: int) -> random.Random:
        return random.Random((self.seed * 1_000_003) + sequence)

    def normal(self, sequence: int, event_time: datetime, **overrides) -> TransactionEvent:
        rng = self._rng(sequence)
        users = tuple(dict.fromkeys(self.catalog.account_to_user.values()))
        user_id = users[sequence % len(users)]
        accounts = self.catalog.accounts_for(user_id)
        account_id = accounts[sequence % len(accounts)]
        device_id = self.catalog.devices_for(user_id)[0]
        transaction_types = tuple(TYPE_WEIGHTS)
        tx_type = rng.choices(transaction_types, weights=tuple(TYPE_WEIGHTS.values()))[0]
        amount = round(rng.uniform(10_000.0, 2_000_000.0), 2)
        old_balance = round(10_000_000.0 + sequence * 100.0, 2)
        merchant_id = None
        counterparty_account_id = None
        if tx_type == TransactionType.DEPOSIT:
            new_balance = old_balance + amount
        else:
            new_balance = old_balance - amount
            if tx_type == TransactionType.PAYMENT:
                merchant_id = self.catalog.merchant_ids[sequence % len(self.catalog.merchant_ids)]
            elif tx_type == TransactionType.TRANSFER:
                candidates = [item for item in self.catalog.account_ids if item != account_id]
                counterparty_account_id = candidates[sequence % len(candidates)]

        values = {
            "event_id": self._id("event", sequence),
            "transaction_id": self._id("transaction", sequence),
            "user_id": user_id,
            "account_id": account_id,
            "device_id": device_id,
            "merchant_id": merchant_id,
            "counterparty_account_id": counterparty_account_id,
            "transaction_type": tx_type.value,
            "amount": amount,
            "currency": "VND",
            "status": "success",
            "channel": ("app", "web", "atm")[sequence % 3],
            "old_balance": old_balance,
            "new_balance": round(new_balance, 2),
            "event_time": event_time,
            "arrival_time": event_time,
            "schema_version": STREAMING_SCHEMA_VERSION,
            "trace_id": self._id("trace", sequence),
        }
        values.update(overrides)
        event = TransactionEvent(**values)
        self.catalog.validate(event)
        return event

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

    def sample(
        self,
        count: int,
        *,
        start: datetime,
        interval: timedelta = timedelta(seconds=1),
    ) -> list[TransactionEvent]:
        """Return bounded logical input with exact configured late/duplicate counts."""
        if count <= 0:
            raise ValueError("count must be positive")
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        ranking = sorted(range(count), key=lambda item: self._id("scenario", item))
        late_sequences = set(ranking[: round(count * self.profile.late_arrival_rate)])
        duplicate_sequences = set(
            ranking[-round(count * self.profile.duplicate_rate) :]
            if round(count * self.profile.duplicate_rate)
            else []
        )
        events: list[TransactionEvent] = []
        for sequence in range(count):
            arrival = start.astimezone(UTC) + sequence * interval
            event = (
                self.late(sequence, arrival, timedelta(minutes=30))
                if sequence in late_sequences
                else self.normal(sequence, arrival)
            )
            events.append(event)
            if sequence in duplicate_sequences:
                events.append(self.duplicate(event, timedelta(seconds=1)))
        return events

    def invalid_payload(self, sequence: int) -> dict[str, object]:
        return {
            "event_id": self._id("invalid", sequence),
            "transaction_id": "",
            "schema_version": STREAMING_SCHEMA_VERSION,
            "trace_id": self._id("trace", sequence),
            "error_fixture": "missing required identity",
        }
