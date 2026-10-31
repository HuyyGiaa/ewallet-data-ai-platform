"""Dependency-free metric names and counters for local parity tests/adapters."""

from __future__ import annotations

from collections import Counter


METRIC_NAMES = (
    "input_events_total",
    "output_events_total",
    "on_time_total",
    "late_accepted_total",
    "too_late_total",
    "late_reconciliation_total",
    "duplicates_detected_total",
    "duplicates_removed_total",
    "retry_total",
    "dlq_total",
    "online_updates_total",
    "historical_records_total",
    "historical_write_failures_total",
    "online_write_attempts_total",
    "online_write_failures_total",
    "bootstrap_complete_total",
    "checkpoint_failures_total",
    "parity_match_total",
    "parity_mismatch_total",
    "parity_stale_total",
    "parity_missing_total",
)


class BlockAMetrics:
    def __init__(self) -> None:
        self._counts = Counter({name: 0 for name in METRIC_NAMES})

    def increment(self, name: str, value: int = 1) -> None:
        if name not in METRIC_NAMES:
            raise KeyError(name)
        self._counts[name] += value

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts)
