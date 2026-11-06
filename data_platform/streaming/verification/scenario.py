"""Deterministic Redpanda input that exercises every Block A event path.

The scenario runs in phases because lateness is decided by a real Flink watermark:
a late event is only late after earlier events moved the watermark. Topics of an
isolated namespace have one partition, so one source split drives the watermark
and event order is the publish order.

Entities come from a catalog seeded by the namespace. Repeated runs therefore use
different keys, which keeps Redis's newer-timestamp guard from hiding a new run
behind an old one, and no real customer key is touched.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from data_platform.streaming.config.settings import EventTimeSettings
from data_platform.streaming.contracts.events import TransactionEvent
from data_platform.streaming.generator.scenarios import DeterministicScenarioGenerator


MANIFEST_SCHEMA = "block-a-verification-manifest-v1"
# After the snapshot's last event, so a batch row can never be mistaken for it.
DEFAULT_START = datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc)
_USERS = 8
_CLOSING_USER = _USERS - 1
_NORMAL_EVENTS = 12


def namespace_seed(namespace: str) -> int:
    """Return a stable seed unique to one namespace."""

    return int(hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:12], 16)


@dataclass(frozen=True)
class Phase:
    """Records published together, before the watermark settles."""

    name: str
    records: tuple[str, ...]


@dataclass(frozen=True)
class VerificationScenario:
    """Publish plan and the outcome each record must produce."""

    namespace: str
    seed: int
    phases: tuple[Phase, ...]
    expected: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def manifest(self) -> dict[str, object]:
        """Return the JSON document the validator checks results against."""

        payloads = [record for phase in self.phases for record in phase.records]
        digest = hashlib.sha256("\n".join(payloads).encode("utf-8")).hexdigest()
        return {
            "schema_version": MANIFEST_SCHEMA,
            "namespace": self.namespace,
            "seed": self.seed,
            "payload_sha256": digest,
            "published_records": len(payloads),
            "phases": {phase.name: len(phase.records) for phase in self.phases},
            "expected": {name: list(ids) for name, ids in self.expected.items()},
        }


def _dump(event: TransactionEvent) -> str:
    return json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))


def _sequences(count: int) -> list[int]:
    """Return sequences that avoid the user reserved for the closing event."""

    return [value for value in range(count * 2) if value % _USERS != _CLOSING_USER][:count]


def build_scenario(
    namespace: str,
    *,
    start: datetime = DEFAULT_START,
    event_time: EventTimeSettings = EventTimeSettings(),
) -> VerificationScenario:
    """Build the phases for one isolated namespace.

    Args:
        namespace: Isolated topic suffix; it also seeds the entities.
        start: Event time of the first event.
        event_time: Watermark delay and allowed lateness of the job under test.

    Returns:
        A scenario with normal, duplicate, replayed, out-of-order, late-accepted,
        too-late, invalid and closing records.
    """

    seed = namespace_seed(namespace)
    generator = DeterministicScenarioGenerator(seed)
    sequences = iter(_sequences(_NORMAL_EVENTS + 4))
    # Odd milliseconds prove sub-second timestamps survive every hop.
    normal = [
        generator.normal(next(sequences), start + timedelta(minutes=index, milliseconds=137 * index))
        for index in range(_NORMAL_EVENTS)
    ]
    newest = normal[-1].event_time
    watermark = newest - event_time.max_out_of_orderness

    def arriving(sequence: int, at: datetime) -> TransactionEvent:
        return replace(generator.normal(sequence, at), arrival_time=newest)

    out_of_order = arriving(next(sequences), newest - event_time.max_out_of_orderness / 2)
    late_accepted = arriving(next(sequences), watermark - event_time.allowed_lateness / 2)
    too_late = arriving(next(sequences), watermark - event_time.allowed_lateness - timedelta(minutes=5))
    duplicate = replace(normal[2], arrival_time=normal[2].arrival_time + timedelta(seconds=1))
    replayed = replace(normal[4], arrival_time=newest + timedelta(minutes=1))
    invalid = generator.invalid_payload(1)
    closing_time = newest + event_time.max_out_of_orderness + event_time.allowed_lateness + timedelta(minutes=10)
    closing = generator.normal(
        _CLOSING_USER,
        closing_time,
        status="failed",
        transaction_type="deposit",
        merchant_id=None,
        counterparty_account_id=None,
    )
    phases = (
        Phase("normal", tuple(_dump(event) for event in normal) + (_dump(duplicate),)),
        Phase("out_of_order", (_dump(out_of_order),)),
        Phase("late_accepted", (_dump(late_accepted),)),
        Phase("too_late", (_dump(too_late),)),
        Phase("replayed_and_invalid", (_dump(replayed), json.dumps(invalid, sort_keys=True))),
        Phase("closing", (_dump(closing),)),
    )
    expected = {
        "on_time": tuple(event.transaction_id for event in (*normal, out_of_order)),
        "late_accepted": (late_accepted.transaction_id,),
        "too_late": (too_late.transaction_id,),
        "duplicates": (duplicate.transaction_id, replayed.transaction_id),
        "invalid_event_ids": (str(invalid["event_id"]),),
        "closing": (closing.transaction_id,),
    }
    return VerificationScenario(namespace, seed, phases, expected)
