"""Semantic offline-to-online parity result classification."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from data_platform.streaming.contracts.features import Ownership
from data_platform.streaming.feast.online import DEFAULT_ONLINE_FRESHNESS


class ParityStatus(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    STALE = "STALE"
    MISSING = "MISSING"
    NOT_REALTIME_OWNED = "NOT_REALTIME_OWNED"


@dataclass(frozen=True)
class ParityResult:
    status: ParityStatus
    offline_value: object
    online_value: object
    offline_timestamp: datetime | None
    online_timestamp: datetime | None


def _same(left, right) -> bool:
    if isinstance(left, float) and isinstance(right, float):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
    return left == right


def compare_feature(
    offline_value,
    online_value,
    offline_timestamp: datetime | None,
    online_timestamp: datetime | None,
    ownership: Ownership,
    max_staleness: timedelta = DEFAULT_ONLINE_FRESHNESS.max_staleness,
) -> ParityResult:
    if ownership == Ownership.MATERIALIZED_BATCH:
        status = ParityStatus.NOT_REALTIME_OWNED
    elif online_timestamp is None or (online_value is None and offline_value is not None):
        # A row with a timestamp may hold a legitimate null, such as the standard
        # deviation of a single observation; only an absent row or value is missing.
        status = ParityStatus.MISSING
    elif offline_timestamp and offline_timestamp - online_timestamp > max_staleness:
        status = ParityStatus.STALE
    elif _same(offline_value, online_value):
        status = ParityStatus.MATCH
    else:
        status = ParityStatus.MISMATCH
    return ParityResult(status, offline_value, online_value, offline_timestamp, online_timestamp)
