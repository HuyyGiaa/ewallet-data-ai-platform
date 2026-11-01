"""Timestamp, watermark, and lateness policy."""

from __future__ import annotations

from enum import Enum

from data_platform.streaming.config.settings import EventTimeSettings
from data_platform.streaming.contracts.events import TransactionEvent


class EventDisposition(str, Enum):
    ON_TIME = "ON_TIME"
    LATE_ACCEPTED = "LATE_ACCEPTED"
    TOO_LATE = "TOO_LATE"


class EventTimeTracker:
    def __init__(self, settings: EventTimeSettings) -> None:
        self.settings = settings
        self.max_event_time = None

    @property
    def watermark(self):
        if self.max_event_time is None:
            return None
        return self.max_event_time - self.settings.max_out_of_orderness

    def classify(self, event: TransactionEvent) -> EventDisposition:
        watermark = self.watermark
        if watermark is None or event.event_time >= watermark:
            return EventDisposition.ON_TIME
        if event.event_time >= watermark - self.settings.allowed_lateness:
            return EventDisposition.LATE_ACCEPTED
        return EventDisposition.TOO_LATE

    def observe(self, event: TransactionEvent) -> EventDisposition:
        disposition = self.classify(event)
        if self.max_event_time is None or event.event_time > self.max_event_time:
            self.max_event_time = event.event_time
        return disposition
