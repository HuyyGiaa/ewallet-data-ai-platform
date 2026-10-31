"""Bounded, deterministic replay datasets shared by baseline and optimized runs.

A replay is fully described by ``ReplaySpec``; the same spec always yields the
same ordered (key, payload) list, so both profiles consume identical input.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from data_platform.streaming.generator.scenarios import (
    DeterministicScenarioGenerator,
    EntityCatalog,
    GeneratorProfile,
)


MAX_EVENTS = 1_000_000
PERFORMANCE_RATES = (100, 500, 1000, 2000)
DEFAULT_START = "2026-05-01T00:00:00+00:00"


@dataclass(frozen=True)
class ReplaySpec:
    mode: str
    count: int
    target_rate: float
    event_interval_seconds: float
    users: int
    merchants: int
    invalid_count: int = 0
    seed: int = 42
    start_time: str = DEFAULT_START

    def __post_init__(self) -> None:
        if self.mode not in {"functional", "performance"}:
            raise ValueError(f"unsupported replay mode: {self.mode}")
        if not 0 < self.count <= MAX_EVENTS:
            raise ValueError(f"count must be in (0, {MAX_EVENTS}]; replays are always bounded")
        if self.target_rate <= 0 or self.event_interval_seconds <= 0:
            raise ValueError("target_rate and event_interval_seconds must be positive")
        if self.invalid_count < 0 or self.invalid_count >= self.count:
            raise ValueError("invalid_count must be in [0, count)")
        if datetime.fromisoformat(self.start_time).tzinfo is None:
            raise ValueError("start_time must be timezone-aware")


def functional_spec() -> ReplaySpec:
    # 600 logical events 5 s apart span 50 min of event time, so windows close
    # (watermark = max - 2 min, timers fire after +10 min lateness) and the
    # generator's 30-min-late events are classified TOO_LATE once state exists.
    return ReplaySpec("functional", 600, 50.0, 5.0, users=8, merchants=6, invalid_count=3)


def performance_spec(target_rate: float, duration_seconds: int = 120) -> ReplaySpec:
    if target_rate not in PERFORMANCE_RATES:
        raise ValueError(f"performance target_rate must be one of {PERFORMANCE_RATES}")
    if not 10 <= duration_seconds <= 600:
        raise ValueError("duration_seconds must be in [10, 600]")
    # Event-time spacing follows the target rate (as the generator CLI does), and a
    # larger catalog keeps per-key history bounded so keyed state is not one hot key.
    return ReplaySpec(
        "performance",
        int(target_rate * duration_seconds),
        float(target_rate),
        1.0 / target_rate,
        users=2000,
        merchants=500,
    )


def build_payloads(spec: ReplaySpec) -> tuple[list[tuple[str, str]], dict]:
    """Return ordered (message_key, json_payload) records and their manifest."""
    generator = DeterministicScenarioGenerator(
        spec.seed,
        catalog=EntityCatalog.deterministic(spec.seed, users=spec.users, merchants=spec.merchants),
        profile=GeneratorProfile(seed=spec.seed),
    )
    events = generator.sample(
        spec.count,
        start=datetime.fromisoformat(spec.start_time).astimezone(timezone.utc),
        interval=timedelta(seconds=spec.event_interval_seconds),
    )
    records = [(event.transaction_id, event.to_json()) for event in events]
    # Deterministic malformed records exercise the explicit DLQ path.
    step = max(1, len(records) // (spec.invalid_count + 1))
    for index in range(spec.invalid_count):
        payload = json.dumps(generator.invalid_payload(index), sort_keys=True, separators=(",", ":"))
        records.insert((index + 1) * step + index, (f"invalid-{index}", payload))
    unique = {event.transaction_id for event in events}
    first: dict[str, object] = {}
    for event in events:
        first.setdefault(event.transaction_id, event)
    # Duplicates re-arrive 1 s after their original; only first arrivals define lateness.
    late = {tx for tx, event in first.items() if event.arrival_time > event.event_time}
    digest = hashlib.sha256()
    for key, payload in records:
        digest.update(key.encode() + b"\0" + payload.encode() + b"\n")
    manifest = {
        "spec": asdict(spec),
        "emitted_records": len(records),
        "valid_events": len(events),
        "unique_transactions": len(unique),
        "duplicate_events": len(events) - len(unique),
        "late_arrival_transactions": len(late),
        "invalid_records": spec.invalid_count,
        "payload_sha256": digest.hexdigest(),
    }
    return records, manifest
