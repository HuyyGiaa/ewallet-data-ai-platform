"""Exact in-memory reference semantics for the 31 DP3 historical features.

The production PyFlink adapter should implement these same operations in keyed
state. This reference is intentionally deterministic and dependency-light so it
can act as the parity oracle for boundary, cold-start, and bootstrap tests.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from data_platform.streaming.contracts.events import TransactionEvent


@dataclass(frozen=True)
class FeatureSnapshot:
    view_name: str
    entity_key: str
    entity_value: str
    feature_timestamp: datetime
    values: dict[str, float | int | None]


def _window(events: Iterable[TransactionEvent], at: datetime, duration: timedelta):
    lower = at - duration
    return [event for event in events if lower <= event.event_time < at]


def _failed_rate(events: list[TransactionEvent]) -> float | None:
    if not events:
        return None
    return sum(event.status == "failed" for event in events) / len(events)


def _sample_std(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


class HistoricalFeatureState:
    def __init__(
        self,
        device_first_seen: dict[str, datetime] | None = None,
        account_last_seen_before_bootstrap: dict[str, datetime] | None = None,
        history_ttl: timedelta = timedelta(days=30, minutes=10),
    ) -> None:
        self._events: list[TransactionEvent] = []
        self.device_first_seen = dict(device_first_seen or {})
        self._account_anchors = dict(account_last_seen_before_bootstrap or {})
        self.history_ttl = history_ttl
        self.cutover: datetime | None = None
        self._max_event_time: datetime | None = None

    def bootstrap(self, events: Iterable[TransactionEvent], cutover: datetime) -> None:
        history = list(events)
        if any(event.event_time >= cutover for event in history):
            raise ValueError("bootstrap history must be strictly before cutover")
        self._events.extend(history)
        self._events.sort(key=lambda event: (event.event_time, event.transaction_id))
        if history:
            self._max_event_time = max(event.event_time for event in history)
        self.cutover = cutover

    def add(self, event: TransactionEvent) -> None:
        self._events.append(event)
        if self._max_event_time is None or event.event_time > self._max_event_time:
            self._max_event_time = event.event_time
        cutoff = self._max_event_time - self.history_ttl
        retained = []
        for item in self._events:
            if item.event_time >= cutoff:
                retained.append(item)
                continue
            previous = self._account_anchors.get(item.account_id)
            if previous is None or item.event_time > previous:
                self._account_anchors[item.account_id] = item.event_time
        self._events = retained

    def snapshot_group(self, events: list[TransactionEvent]):
        if not events:
            return {}
        event_times = {event.event_time for event in events}
        if len(event_times) != 1:
            raise ValueError("snapshot_group requires one event timestamp")
        snapshots = {event.transaction_id: self.snapshot(event) for event in events}
        for event in sorted(events, key=lambda item: item.transaction_id):
            self.add(event)
        return snapshots

    def snapshot(self, current: TransactionEvent) -> dict[str, FeatureSnapshot]:
        history = [event for event in self._events if event.event_time < current.event_time]
        result = {
            "user_behavior": self._user(current, [e for e in history if e.user_id == current.user_id]),
            "account_behavior": self._account(current, [e for e in history if e.account_id == current.account_id]),
            "device_behavior": self._device(current, [e for e in history if e.device_id == current.device_id]),
        }
        if current.transaction_type == "payment" and current.merchant_id:
            result["merchant_behavior"] = self._merchant(
                current,
                [
                    event
                    for event in history
                    if event.transaction_type == "payment"
                    and event.merchant_id == current.merchant_id
                ],
            )
        return result

    def _snapshot(self, view, key, value, current, values):
        return FeatureSnapshot(view, key, value, current.event_time, values)

    def _user(self, current, history):
        w5 = _window(history, current.event_time, timedelta(minutes=5))
        w1 = _window(history, current.event_time, timedelta(hours=1))
        w24 = _window(history, current.event_time, timedelta(hours=24))
        w30 = _window(history, current.event_time, timedelta(days=30))
        amounts = [event.amount for event in w30]
        return self._snapshot("user_behavior", "user_id", current.user_id, current, {
            "user_tx_count_5m": len(w5),
            "user_tx_count_1h": len(w1),
            "user_tx_count_24h": len(w24),
            "user_amount_sum_1h": sum((event.amount for event in w1), 0.0),
            "user_amount_observation_count_30d": len(amounts),
            "user_avg_amount_30d": statistics.mean(amounts) if amounts else None,
            "user_std_amount_30d": _sample_std(amounts),
            "user_failed_rate_24h": _failed_rate(w24),
            "user_distinct_merchants_24h": len({e.merchant_id for e in w24 if e.merchant_id}),
        })

    def _account(self, current, history):
        w5 = _window(history, current.event_time, timedelta(minutes=5))
        w1 = _window(history, current.event_time, timedelta(hours=1))
        w24 = _window(history, current.event_time, timedelta(hours=24))
        w30 = _window(history, current.event_time, timedelta(days=30))
        amounts = [event.amount for event in w30]
        previous_candidates = [event.event_time for event in history]
        anchor = self._account_anchors.get(current.account_id)
        if anchor is not None and anchor < current.event_time:
            previous_candidates.append(anchor)
        previous = max(previous_candidates, default=None)
        return self._snapshot("account_behavior", "account_id", current.account_id, current, {
            "account_tx_count_5m": len(w5),
            "account_tx_count_1h": len(w1),
            "account_tx_count_24h": len(w24),
            "account_amount_sum_1h": sum((event.amount for event in w1), 0.0),
            "account_amount_sum_24h": sum((event.amount for event in w24), 0.0),
            "account_amount_observation_count_30d": len(amounts),
            "account_avg_amount_30d": statistics.mean(amounts) if amounts else None,
            "account_std_amount_30d": _sample_std(amounts),
            "account_failed_rate_24h": _failed_rate(w24),
            "account_seconds_since_last_tx": (
                (current.event_time - previous).total_seconds() if previous else None
            ),
        })

    def _device(self, current, history):
        w1 = _window(history, current.event_time, timedelta(hours=1))
        w24 = _window(history, current.event_time, timedelta(hours=24))
        first_seen = self.device_first_seen.get(current.device_id)
        age = max(0.0, (current.event_time - first_seen).total_seconds()) if first_seen else 0.0
        return self._snapshot("device_behavior", "device_id", current.device_id, current, {
            "device_age_seconds": age,
            "device_tx_count_1h": len(w1),
            "device_tx_count_24h": len(w24),
            "device_amount_sum_24h": sum((event.amount for event in w24), 0.0),
            "device_failed_rate_24h": _failed_rate(w24),
        })

    def _merchant(self, current, history):
        w10 = _window(history, current.event_time, timedelta(minutes=10))
        w1 = _window(history, current.event_time, timedelta(hours=1))
        w24 = _window(history, current.event_time, timedelta(hours=24))
        return self._snapshot("merchant_behavior", "merchant_id", current.merchant_id, current, {
            "merchant_tx_count_10m": len(w10),
            "merchant_tx_count_1h": len(w1),
            "merchant_tx_count_24h": len(w24),
            "merchant_unique_users_10m": len({event.user_id for event in w10}),
            "merchant_unique_users_1h": len({event.user_id for event in w1}),
            "merchant_amount_sum_1h": sum((event.amount for event in w1), 0.0),
            "merchant_avg_amount_24h": statistics.mean([e.amount for e in w24]) if w24 else None,
        })
