"""Synthetic prediction payloads for the load scenarios.

Every identifier is generated from a seeded ``random.Random``, so a seed fixes
the request sequence of a user. No customer data is used.
"""

from __future__ import annotations

from datetime import datetime, timezone
import random


# Two minutes after the mock feature rows, inside the freshness window.
EVENT_TIMESTAMP = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc).isoformat()
SHARED_TRANSACTION_ID = "synthetic-shared-transaction"
ENTITY_POOL_SIZE = 1_000
_CHANNELS = ("app", "web", "atm")


def _entity(prefix: str, rng: random.Random) -> str:
    return f"synthetic-{prefix}-{rng.randrange(ENTITY_POOL_SIZE)}"


def valid_payload(rng: random.Random, sequence: int) -> dict[str, object]:
    """Return a valid request; the type decides the conditional entities."""
    transaction_type = rng.choice(("payment", "transfer", "withdraw", "deposit"))
    return {
        "transaction_id": f"synthetic-tx-{sequence}",
        "event_timestamp": EVENT_TIMESTAMP,
        "user_id": _entity("user", rng),
        "account_id": _entity("account", rng),
        "device_id": _entity("device", rng),
        "merchant_id": _entity("merchant", rng) if transaction_type == "payment" else None,
        "counterparty_account_id": (
            _entity("account", rng) if transaction_type == "transfer" else None
        ),
        "type": transaction_type,
        "amount": round(rng.uniform(1_000.0, 50_000_000.0), 2),
        "currency": "VND",
        "channel": rng.choice(_CHANNELS),
    }


def shared_payload() -> dict[str, object]:
    """Return the one request every user sends in the concurrent scenario."""
    return {
        "transaction_id": SHARED_TRANSACTION_ID,
        "event_timestamp": EVENT_TIMESTAMP,
        "user_id": "synthetic-user-shared",
        "account_id": "synthetic-account-shared",
        "device_id": "synthetic-device-shared",
        "merchant_id": "synthetic-merchant-shared",
        "counterparty_account_id": None,
        "type": "payment",
        "amount": 125_000.0,
        "currency": "VND",
        "channel": "app",
    }


def _without_field(payload: dict[str, object], field: str) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != field}


def invalid_payload(rng: random.Random, sequence: int) -> dict[str, object]:
    """Return a request that violates exactly one schema rule."""
    payload = valid_payload(rng, sequence)
    violations = (
        {**payload, "amount": 0},
        {**payload, "amount": -1.0},
        {**payload, "currency": "USD"},
        {**payload, "type": "cashout"},
        {**payload, "channel": "mobile"},
        {**payload, "event_timestamp": "2026-10-29T12:00:00"},
        {**payload, "unexpected_field": True},
        {**payload, "type": "payment", "merchant_id": None},
        {**payload, "type": "transfer", "counterparty_account_id": None},
        _without_field(payload, "user_id"),
    )
    return rng.choice(violations)


def replay_seed(base_seed: int, user_index: int) -> int:
    """Derive a distinct, reproducible seed for one simulated user."""
    return base_seed * 1_000_003 + user_index
