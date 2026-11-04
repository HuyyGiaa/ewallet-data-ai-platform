"""Supported Feast push path; no raw Redis key writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from data_platform.streaming.sinks.online import OnlineFeatureUpdate


@dataclass(frozen=True)
class OnlineFreshnessPolicy:
    """Separate semantic freshness from optional Redis physical expiry."""

    feature_view_ttl: timedelta
    max_staleness: timedelta
    redis_key_ttl_seconds: int | None

    def __post_init__(self) -> None:
        if self.feature_view_ttl <= timedelta(0) or self.max_staleness <= timedelta(0):
            raise ValueError("freshness durations must be positive")
        if self.max_staleness > self.feature_view_ttl:
            raise ValueError("max staleness cannot exceed FeatureView TTL")
        if self.redis_key_ttl_seconds is not None and self.redis_key_ttl_seconds <= 0:
            raise ValueError("Redis key TTL must be positive when configured")

    @property
    def redis_physical_expiry_enabled(self) -> bool:
        return self.redis_key_ttl_seconds is not None


DEFAULT_ONLINE_FRESHNESS = OnlineFreshnessPolicy(
    feature_view_ttl=timedelta(days=30, minutes=10),
    max_staleness=timedelta(minutes=10),
    redis_key_ttl_seconds=None,
)


@dataclass(frozen=True)
class MaterializationBoundary:
    start: datetime
    end: datetime
    cutover: datetime
    latest_per_entity_only: bool = True

    def __post_init__(self) -> None:
        if not self.start < self.end or self.end != self.cutover:
            raise ValueError("materialization must end exactly at the streaming cutover")


def _default_frame_factory(rows):
    import pandas as pd

    return pd.DataFrame(rows)


def _default_online_mode():
    from feast.data_source import PushMode

    return PushMode.ONLINE


class FeastPushWriter:
    """Write complete FeatureView rows through Feast 0.66 PushSource."""

    def __init__(
        self,
        feature_store,
        frame_factory: Callable = _default_frame_factory,
        online_mode=None,
    ) -> None:
        self.feature_store = feature_store
        self.frame_factory = frame_factory
        self.online_mode = online_mode

    def write(self, update: OnlineFeatureUpdate) -> None:
        mode = self.online_mode if self.online_mode is not None else _default_online_mode()
        self.feature_store.push(
            f"{update.view_name}_push",
            self.frame_factory([update.feast_row()]),
            to=mode,
        )
